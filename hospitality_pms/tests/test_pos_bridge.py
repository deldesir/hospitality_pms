"""The bar till charges the room, and the ledger sees the sale once.

A POS Next till with "Charge credit sales to the guest's room" puts a Pay-on-Account sale
on the guest's folio. What these tests defend: the folio line is already posted (the POS
invoice is its ERPNext document), the checkout invoice leaves it out, the checkout payment
allocates against the POS invoice, a stranger cannot run a tab on a room till, and a
cancellation or a return takes the amount back off the folio while it is still open.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

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
		cls.rooms = fx.rooms(cls.world.property, cls.room_type, count=2)
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
				pos_bridge.TILL_FLAG: charge_to_room,
			}
		)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc.name

	# -- a guest in house, with the Customer a till can sell to ---------------

	def check_in(self, first_name: str, room_index: int = 0) -> dict:
		fx = self.world.fixtures
		guest = fx.guest(first_name)
		reservation = fx.reservation(self.world.property, self.room_type, guest, rate_plan=self.rate_plan)
		reservation_service.confirm(reservation)
		line = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")[0]
		result = stay_service.check_in(reservation, line, self.rooms[room_index], skip_id_check=True)
		customer = guest_service.ensure_customer(guest, self.world.company)
		frappe.db.commit()
		return {"guest": guest, "customer": customer, "stay": result["stay"], "folio": result["folio"]}

	def sale(self, customer: str, pos_profile: str, *, qty: float = 2, rate: float = 150, paid: float = 0):
		"""A submitted POS sale. `paid` 0 is a Pay-on-Account credit sale (POS Next's flag),
		anything else is cash taken at the till."""
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
		if paid:
			inv.append("payments", {"mode_of_payment": "Cash", "amount": paid})
		else:
			inv.flags.pos_next_credit_sale = 1
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

	# -- tests ---------------------------------------------------------------

	def test_check_in_gives_the_guest_a_customer_the_till_can_sell_to(self):
		fx = self.world.fixtures
		guest = fx.guest("Arrives")
		reservation = fx.reservation(self.world.property, self.room_type, guest, rate_plan=self.rate_plan)
		reservation_service.confirm(reservation)
		line = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")[0]
		stay_service.check_in(reservation, line, self.rooms[1], skip_id_check=True)
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
		g = self.check_in("Double", room_index=0)
		fx = self.world.fixtures
		second = fx.reservation(self.world.property, self.room_type, g["guest"], rate_plan=self.rate_plan)
		reservation_service.confirm(second)
		line = frappe.get_all("Reservation Room", filters={"parent": second}, pluck="name")[0]
		stay_service.check_in(second, line, self.rooms[1], skip_id_check=True)
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
			}
		)
		ret.flags.pos_next_credit_sale = 1
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
