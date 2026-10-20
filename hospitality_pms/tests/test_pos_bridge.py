"""The bar till charges the room, and the ledger sees the sale once.

A POS Next till with "Charge credit sales to the guest's room" puts a Pay-on-Account sale
on the guest's folio. What these tests defend: the folio line is already posted (the POS
invoice is its ERPNext document), the checkout invoice leaves it out, the checkout payment
allocates against the POS invoice, a stranger cannot run a tab on a room till, and a
cancellation or a return takes the amount back off the folio while it is still open.

Money the other way: a receipt taken at the till against the bill lands on the folio as a
payment already posted, checkout settles only the rest, and cancelling the receipt puts the
money back while the folio is open and is refused once the guest has settled. A return
refunded partly in cash credits the folio with the unpaid share only. The checkout receipt
is never counted in a till shift. And the guest's Customer carries the room while in house.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, now_datetime, nowdate

from hospitality_pms.services import checkout as checkout_service
from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import guests as guest_service
from hospitality_pms.services import pos_bridge
from hospitality_pms.services import posting as posting_service
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services.exceptions import FolioError
from hospitality_pms.tests.posting_world import PostingWorld, payment_references

POS_NEXT_INSTALLED = "pos_next" in frappe.get_installed_apps()


@unittest.skipUnless(POS_NEXT_INSTALLED, "the room-charge bridge serves POS Next tills")
class TestRoomCharge(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = PostingWorld("POSB", "PB")
		fx = cls.world.fixtures
		cls.room_type = fx.room_type(cls.world.property, "BAR", base_rate=100)
		# Check-ins commit and stay in house, one vacant room per check-in across the class.
		cls.rooms = fx.rooms(cls.world.property, cls.room_type, count=32)
		cls.rate_plan = fx.rate_plan(cls.world.property, cls.room_type, base_rate=100)
		cls.beer = fx.erp_item("BEER")
		cls.till = cls._pos_profile("Bar", charge_to_room=1)
		cls.shop = cls._pos_profile("Shop", charge_to_room=0)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in (cls.till, cls.shop):
			frappe.delete_doc("POS Profile", name, force=True, ignore_permissions=True)
		cls.world.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _pos_profile(cls, label: str, *, charge_to_room: int) -> str:
		company = cls.world.company
		name = f"WV2 {label} {cls.world.tag}"
		if frappe.db.exists("POS Profile", name):
			frappe.db.set_value("POS Profile", name, pos_bridge.TILL_FLAG, charge_to_room)
			return name
		doc = frappe.get_doc(
			{
				"doctype": "POS Profile",
				"name": name,
				"company": company,
				"currency": frappe.get_cached_value("Company", company, "default_currency"),
				"warehouse": frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name"),
				"write_off_account": frappe.db.get_value(
					"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
				),
				"write_off_cost_center": frappe.get_cached_value("Company", company, "cost_center"),
				"write_off_limit": 1,
				"payments": [{"mode_of_payment": "Cash", "default": 1}],
				"applicable_for_users": [{"user": frappe.session.user, "default": 1}],
				pos_bridge.TILL_FLAG: charge_to_room,
			}
		)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc.name

	# -- a guest in house, with the Customer a till can sell to ---------------

	def free_room(self) -> str:
		"""Check-ins commit, so a guest stays in house across tests: take the next vacant room
		that housekeeping has not marked dirty (a checkout in an earlier test leaves one)."""
		for room in self.rooms:
			state = frappe.db.get_value("Hotel Room", room, ["occupancy_status", "housekeeping_status"], as_dict=True)
			if state.occupancy_status == "Vacant" and state.housekeeping_status in ("Clean", "Inspected"):
				return room
		raise AssertionError("no vacant, clean room left for this test")

	def check_in(self, first_name: str) -> dict:
		fx = self.world.fixtures
		guest = fx.guest(first_name)
		reservation = fx.reservation(self.world.property, self.room_type, guest, rate_plan=self.rate_plan)
		reservation_service.confirm(reservation)
		line = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")[0]
		result = stay_service.check_in(reservation, line, self.free_room(), skip_id_check=True)
		customer = guest_service.ensure_customer(guest, self.world.company)
		frappe.db.commit()
		return {"guest": guest, "customer": customer, "stay": result["stay"], "folio": result["folio"]}

	def sale(self, customer: str, pos_profile: str, *, qty: float = 2, rate: float = 150, paid: float = 0):
		"""A submitted POS sale. `paid` 0 is a sale on the customer's account (a payment row of
		zero, as a till's partial payment leaves it), anything else is cash taken at the till."""
		inv = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.world.company,
				"customer": customer,
				"is_pos": 1,
				"pos_profile": pos_profile,
				"update_stock": 0,
				"items": [{"item_code": self.beer, "qty": qty, "rate": rate}],
			}
		)
		inv.append("payments", {"mode_of_payment": "Cash", "amount": paid})
		inv.flags.ignore_permissions = True
		inv.insert()
		inv.submit()
		frappe.db.commit()
		return frappe.get_doc("Sales Invoice", inv.name)

	def charges(self, folio: str, charge_type: str | None = None) -> list[dict]:
		filters = {"parent": folio}
		if charge_type:
			filters["charge_type"] = charge_type
		return frappe.get_all(
			"Folio Charge",
			filters=filters,
			fields=["name", "charge_type", "amount", "total_amount", "is_posted_to_erp", "sales_invoice", "is_reversed", "idempotency_key"],
			order_by="idx asc",
		)

	def payments(self, folio: str) -> list[dict]:
		return frappe.get_all(
			"Folio Payment",
			filters={"parent": folio},
			fields=["name", "payment_method", "amount", "is_posted_to_erp", "payment_entry", "is_reversed", "idempotency_key"],
			order_by="idx asc",
		)

	def till_receipt(self, invoice: str, amount: float) -> str:
		"""Money taken at the till against a submitted invoice, the way POS Next takes it."""
		from pos_next.api.partial_payments import create_payment_entry

		name = create_payment_entry(invoice, amount, mode_of_payment="Cash")
		frappe.db.commit()
		return name

	def balance(self, folio: str) -> float:
		return flt(frappe.db.get_value("Guest Folio", folio, "balance"))

	def open_till_shift(self) -> str:
		"""The test user running the till, as a cashier who also works the desk would."""
		shift = frappe.get_doc(
			{
				"doctype": "POS Opening Shift",
				"period_start_date": now_datetime(),
				"posting_date": nowdate(),
				"company": self.world.company,
				"pos_profile": self.till,
				"user": frappe.session.user,
				"status": "Open",
				"balance_details": [{"mode_of_payment": "Cash", "amount": 0}],
			}
		)
		shift.flags.ignore_permissions = True
		shift.insert()
		shift.submit()
		frappe.db.commit()

		def close():
			# The sales and receipts of the shift link to it, so it cannot be cancelled or
			# deleted; closing it is what stops it being the user's open shift.
			frappe.db.set_value("POS Opening Shift", shift.name, "status", "Closed", update_modified=False)
			frappe.db.commit()

		self.addCleanup(close)
		return shift.name

	# -- tests ---------------------------------------------------------------

	def test_check_in_gives_the_guest_a_customer_the_till_can_sell_to(self):
		fx = self.world.fixtures
		guest = fx.guest("Arrives")
		reservation = fx.reservation(self.world.property, self.room_type, guest, rate_plan=self.rate_plan)
		reservation_service.confirm(reservation)
		line = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")[0]
		stay_service.check_in(reservation, line, self.free_room(), skip_id_check=True)
		frappe.db.commit()
		customer = frappe.db.get_value("Guest", guest, "customer")
		self.assertTrue(customer)
		self.assertEqual(frappe.db.get_value("Customer", customer, "customer_name"), frappe.db.get_value("Guest", guest, "guest_name"))

	def test_credit_sale_lands_on_the_folio_already_posted(self):
		g = self.check_in("Tab")
		inv = self.sale(g["customer"], self.till)
		self.assertEqual(flt(inv.outstanding_amount), flt(inv.grand_total))

		rows = self.charges(g["folio"], pos_bridge.CHARGE_TYPE)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(flt(row["amount"]), flt(inv.grand_total))
		self.assertEqual(row["is_posted_to_erp"], 1)
		self.assertEqual(row["sales_invoice"], inv.name)
		self.assertEqual(flt(frappe.db.get_value("Guest Folio", g["folio"], "balance")), flt(inv.grand_total))

		log = frappe.db.get_value(
			posting_service.POSTING_LOG,
			{"idempotency_key": pos_bridge.charge_key(inv.name)},
			["posting_status", "erp_document", "folio"],
			as_dict=True,
		)
		self.assertEqual(log["posting_status"], posting_service.POSTED)
		self.assertEqual(log["erp_document"], inv.name)
		self.assertEqual(log["folio"], g["folio"])
		self.assertEqual(inv.get(pos_bridge.INVOICE_STAY_FIELD), g["stay"])
		self.assertEqual(inv.get(pos_bridge.INVOICE_FOLIO_FIELD), g["folio"])

	def test_charging_twice_posts_once(self):
		g = self.check_in("Twice")
		inv = self.sale(g["customer"], self.till)
		again = pos_bridge.charge_invoice_to_room(inv)
		self.assertTrue(again["duplicate"])
		self.assertEqual(len(self.charges(g["folio"], pos_bridge.CHARGE_TYPE)), 1)

	def test_cash_sale_is_not_a_room_charge(self):
		g = self.check_in("Cash")
		inv = self.sale(g["customer"], self.till, paid=300)
		self.assertEqual(flt(inv.outstanding_amount), 0)
		self.assertEqual(self.charges(g["folio"], pos_bridge.CHARGE_TYPE), [])

	def test_a_till_without_the_flag_sells_on_account_as_before(self):
		g = self.check_in("Shop")
		inv = self.sale(g["customer"], self.shop)
		self.assertGreater(flt(inv.outstanding_amount), 0)
		self.assertEqual(self.charges(g["folio"], pos_bridge.CHARGE_TYPE), [])

	def test_a_stranger_cannot_run_a_tab_on_a_room_till(self):
		stranger = frappe.get_doc(
			{"doctype": "Customer", "customer_name": f"Walk-in {self.world.tag}", "customer_type": "Individual",
			 "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			 "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Customer", stranger.name, force=True, ignore_permissions=True))
		with self.assertRaises(FolioError):
			self.sale(stranger.name, self.till)
		frappe.db.rollback()

	def test_two_rooms_in_house_need_the_pms(self):
		g = self.check_in("Double")
		fx = self.world.fixtures
		second = fx.reservation(self.world.property, self.room_type, g["guest"], rate_plan=self.rate_plan)
		reservation_service.confirm(second)
		line = frappe.get_all("Reservation Room", filters={"parent": second}, pluck="name")[0]
		stay_service.check_in(second, line, self.free_room(), skip_id_check=True)
		frappe.db.commit()
		with self.assertRaises(FolioError):
			self.sale(g["customer"], self.till)
		frappe.db.rollback()

	def test_checkout_invoice_skips_the_bar_and_the_payment_settles_it(self):
		g = self.check_in("Settle")
		inv = self.sale(g["customer"], self.till)
		bar = flt(inv.grand_total)
		self.world.charge(g["folio"], "Minibar", 40, 2, f"settle-minibar-{inv.name}")

		posted = posting_service.post_folio_invoice(g["folio"])
		checkout_invoice = frappe.get_doc("Sales Invoice", posted["erp_document"])
		self.assertAlmostEqual(flt(checkout_invoice.grand_total), 42.0, places=2)
		self.assertNotIn(self.beer, [row.item_code for row in checkout_invoice.items])

		payment = self.world.payment(g["folio"], bar + 42, f"settle-{inv.name}")
		entry = posting_service.post_folio_payment(g["folio"], payment["row"])["erp_document"]
		refs = {r["reference_name"]: flt(r["allocated_amount"]) for r in payment_references(entry)}
		self.assertAlmostEqual(refs.get(inv.name, 0), bar, places=2)
		self.assertAlmostEqual(refs.get(checkout_invoice.name, 0), 42.0, places=2)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")), 0)
		self.assertAlmostEqual(flt(frappe.db.get_value("Guest Folio", g["folio"], "balance")), 0, places=2)

	def test_cancelling_the_sale_reverses_the_charge_while_the_folio_is_open(self):
		g = self.check_in("Cancel")
		inv = self.sale(g["customer"], self.till)
		inv.cancel()
		frappe.db.commit()
		rows = self.charges(g["folio"])
		original = next(r for r in rows if r["charge_type"] == pos_bridge.CHARGE_TYPE)
		self.assertEqual(original["is_reversed"], 1)
		self.assertTrue(any(r["idempotency_key"] == f"reversal:{original['name']}" for r in rows))
		self.assertAlmostEqual(flt(frappe.db.get_value("Guest Folio", g["folio"], "balance")), 0, places=2)
		self.assertEqual(
			frappe.db.get_value(posting_service.POSTING_LOG, {"idempotency_key": pos_bridge.charge_key(inv.name)}, "posting_status"),
			posting_service.CANCELLED,
		)

	def test_a_return_takes_the_amount_back_off_the_folio(self):
		g = self.check_in("Return")
		inv = self.sale(g["customer"], self.till)
		ret = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.world.company,
				"customer": g["customer"],
				"is_pos": 1,
				"is_return": 1,
				"return_against": inv.name,
				"update_outstanding_for_self": 0,
				"pos_profile": self.till,
				"update_stock": 0,
				"items": [{"item_code": self.beer, "qty": -1, "rate": 150}],
				"payments": [{"mode_of_payment": "Cash", "amount": 0}],
			}
		)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		frappe.db.commit()
		adjustments = self.charges(g["folio"], "Adjustment")
		self.assertEqual(len(adjustments), 1)
		self.assertAlmostEqual(flt(adjustments[0]["amount"]), -150.0, places=2)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Guest Folio", g["folio"], "balance")), flt(inv.grand_total) - 150.0, places=2
		)

	def test_a_return_refunded_partly_in_cash_credits_the_folio_with_the_unpaid_share(self):
		g = self.check_in("Partly")
		inv = self.sale(g["customer"], self.till, paid=100)
		self.assertAlmostEqual(flt(inv.outstanding_amount), 200.0, places=2)
		self.assertAlmostEqual(flt(self.charges(g["folio"], pos_bridge.CHARGE_TYPE)[0]["amount"]), 200.0, places=2)
		# One beer back: 150 of goods, of which the till refunds the paid share (50) in cash
		# and credits the receivable for the rest (100).
		ret = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.world.company,
				"customer": g["customer"],
				"is_pos": 1,
				"is_return": 1,
				"return_against": inv.name,
				"update_outstanding_for_self": 0,
				"pos_profile": self.till,
				"update_stock": 0,
				"items": [{"item_code": self.beer, "qty": -1, "rate": 150}],
				"payments": [{"mode_of_payment": "Cash", "amount": -50}],
			}
		)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		frappe.db.commit()
		adjustments = self.charges(g["folio"], "Adjustment")
		self.assertEqual(len(adjustments), 1)
		self.assertAlmostEqual(flt(adjustments[0]["amount"]), -100.0, places=2)
		self.assertAlmostEqual(self.balance(g["folio"]), 100.0, places=2)
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")), 100.0, places=2)

	def test_a_return_refunded_in_full_at_the_till_leaves_the_folio_alone(self):
		g = self.check_in("Refunded")
		inv = self.sale(g["customer"], self.till, qty=2, rate=100, paid=100)
		self.assertAlmostEqual(self.balance(g["folio"]), 100.0, places=2)
		# One of the two back, and the till hands the whole 100 back in cash: the guest still
		# owes the other one, so the folio does not move.
		ret = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.world.company,
				"customer": g["customer"],
				"is_pos": 1,
				"is_return": 1,
				"return_against": inv.name,
				"update_outstanding_for_self": 0,
				"pos_profile": self.till,
				"update_stock": 0,
				"items": [{"item_code": self.beer, "qty": -1, "rate": 100}],
				"payments": [{"mode_of_payment": "Cash", "amount": -100}],
			}
		)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		frappe.db.commit()
		self.assertEqual(self.charges(g["folio"], "Adjustment"), [])
		self.assertAlmostEqual(self.balance(g["folio"]), 100.0, places=2)
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")), 100.0, places=2)

	def test_a_receipt_at_the_till_lands_on_the_folio_and_checkout_settles_the_rest(self):
		g = self.check_in("Collect")
		inv = self.sale(g["customer"], self.till)
		bar = flt(inv.grand_total)
		receipt = self.till_receipt(inv.name, 100)

		rows = self.payments(g["folio"])
		self.assertEqual(len(rows), 1)
		self.assertAlmostEqual(flt(rows[0]["amount"]), 100.0, places=2)
		self.assertEqual(rows[0]["is_posted_to_erp"], 1)
		self.assertEqual(rows[0]["payment_entry"], receipt)
		self.assertEqual(rows[0]["payment_method"], "Cash")
		self.assertAlmostEqual(self.balance(g["folio"]), bar - 100, places=2)
		log = frappe.db.get_value(
			posting_service.POSTING_LOG,
			{"idempotency_key": pos_bridge.receipt_key(receipt, inv.name)},
			["posting_status", "erp_doctype", "erp_document", "amount"],
			as_dict=True,
		)
		self.assertEqual((log["posting_status"], log["erp_doctype"], log["erp_document"]), (posting_service.POSTED, "Payment Entry", receipt))
		self.assertAlmostEqual(flt(log["amount"]), 100.0, places=2)

		# Checkout: the folio invoice carries the minibar only; the payment covers the
		# minibar and what is still owed on the bar bill, and nothing more.
		self.world.charge(g["folio"], "Minibar", 40, 2, f"collect-minibar-{inv.name}")
		posted = posting_service.post_folio_invoice(g["folio"])
		checkout_invoice = posted["erp_document"]
		payment = self.world.payment(g["folio"], (bar - 100) + 42, f"collect-{inv.name}")
		entry = posting_service.post_folio_payment(g["folio"], payment["row"])["erp_document"]
		refs = {r["reference_name"]: flt(r["allocated_amount"]) for r in payment_references(entry)}
		self.assertAlmostEqual(refs.get(inv.name, 0), bar - 100, places=2)
		self.assertAlmostEqual(refs.get(checkout_invoice, 0), 42.0, places=2)
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")), 0, places=2)
		self.assertAlmostEqual(self.balance(g["folio"]), 0, places=2)
		# The folio's own settlement is not mirrored back onto it.
		self.assertEqual(len(self.payments(g["folio"])), 2)

	def test_the_pms_checkout_receipt_is_not_a_second_receipt_on_the_folio(self):
		g = self.check_in("Twice2")
		inv = self.sale(g["customer"], self.till)
		payment = self.world.payment(g["folio"], flt(inv.grand_total), f"own-{inv.name}")
		posting_service.post_folio_payment(g["folio"], payment["row"])
		frappe.db.commit()
		rows = self.payments(g["folio"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["name"], payment["row"])

	def test_cancelling_the_till_receipt_puts_the_money_back_on_the_folio(self):
		g = self.check_in("Undo")
		inv = self.sale(g["customer"], self.till)
		bar = flt(inv.grand_total)
		receipt = self.till_receipt(inv.name, bar)
		self.assertAlmostEqual(self.balance(g["folio"]), 0, places=2)

		entry = frappe.get_doc("Payment Entry", receipt)
		entry.flags.ignore_permissions = True
		entry.cancel()
		frappe.db.commit()

		rows = self.payments(g["folio"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["is_reversed"], 1)
		self.assertAlmostEqual(self.balance(g["folio"]), bar, places=2)
		self.assertEqual(
			frappe.db.get_value(posting_service.POSTING_LOG, {"idempotency_key": pos_bridge.receipt_key(receipt, inv.name)}, "posting_status"),
			posting_service.CANCELLED,
		)
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")), bar, places=2)

	def test_a_till_receipt_cannot_be_cancelled_once_the_guest_has_settled(self):
		g = self.check_in("Settled")
		inv = self.sale(g["customer"], self.till)
		receipt = self.till_receipt(inv.name, flt(inv.grand_total))
		folio_service.transition(g["folio"], folio_service.READY)
		folio_service.transition(g["folio"], folio_service.SETTLED)
		folio_service.transition(g["folio"], folio_service.CLOSED)
		frappe.db.commit()
		entry = frappe.get_doc("Payment Entry", receipt)
		entry.flags.ignore_permissions = True
		with self.assertRaises(FolioError):
			entry.cancel()
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Payment Entry", receipt, "docstatus"), 1)

	def test_a_receipt_after_the_folio_closed_is_noted_and_left_to_receivables(self):
		g = self.check_in("Later")
		inv = self.sale(g["customer"], self.till)
		# The folio was settled and closed with the bar invoice still open in ERPNext (its
		# desk receipt never posted): what the till collects later belongs to receivables.
		self.world.payment(g["folio"], flt(inv.grand_total), f"later-{inv.name}")
		folio_service.transition(g["folio"], folio_service.READY)
		folio_service.transition(g["folio"], folio_service.SETTLED)
		folio_service.transition(g["folio"], folio_service.CLOSED)
		frappe.db.commit()
		receipt = self.till_receipt(inv.name, flt(inv.grand_total))
		self.assertEqual([p for p in self.payments(g["folio"]) if p["payment_entry"] == receipt], [])
		self.assertEqual(frappe.db.get_value("Payment Entry", receipt, "docstatus"), 1)
		self.assertTrue(
			frappe.db.exists("Folio Log", {"folio": g["folio"], "action": "Point of sale receipt after settlement"})
		)

	def test_the_checkout_receipt_is_not_counted_in_the_cashiers_open_till_shift(self):
		shift = self.open_till_shift()
		g = self.check_in("Shift")
		inv = self.sale(g["customer"], self.till)
		payment = self.world.payment(g["folio"], flt(inv.grand_total), f"shift-{inv.name}")
		entry = posting_service.post_folio_payment(g["folio"], payment["row"])["erp_document"]
		frappe.db.commit()
		self.assertFalse(frappe.db.get_value("Payment Entry", entry, "posa_pos_opening_shift"))
		# whereas money the same user takes at the till does belong to the drawer
		receipt = self.till_receipt(self.sale(g["customer"], self.till, qty=1).name, 150)
		self.assertEqual(frappe.db.get_value("Payment Entry", receipt, "posa_pos_opening_shift"), shift)

	def test_check_in_labels_the_customer_with_the_room_and_check_out_clears_it(self):
		g = self.check_in("Labelled")
		room = frappe.db.get_value("Stay", g["stay"], "room")
		self.assertEqual(frappe.db.get_value("Customer", g["customer"], guest_service.CUSTOMER_ROOM_FIELD), room)
		shown = [{"name": g["customer"], "customer_name": "x"}, {"name": "nobody-" + self.world.tag}]
		pos_bridge.annotate_till_customers(shown, self.till)
		self.assertEqual(shown[0].get("note"), f"Room {room}")
		self.assertNotIn("note", shown[1])

		checkout_service.check_out(g["stay"], post_to_erp=False)
		frappe.db.commit()
		self.assertFalse(frappe.db.get_value("Customer", g["customer"], guest_service.CUSTOMER_ROOM_FIELD))
		shown = [{"name": g["customer"]}]
		pos_bridge.annotate_till_customers(shown, self.till)
		self.assertNotIn("note", shown[0])
