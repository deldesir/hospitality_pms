"""Fixture builders shared by the Wave-1 regression suites.

Every builder is idempotent on name and records what it created, so a suite can
tear its own world down in one call. That matters more here than in an ordinary
Frappe suite: the concurrency tests spawn separate processes that commit, and a
committed row is not covered by `IntegrationTestCase`'s rollback.

Naming
------
Everything is prefixed `WV1` and suffixed with a per-suite tag, so two suites
running in the same database never collide and a stray row is obviously test
residue rather than something an operator made.
"""

import frappe
from frappe.utils import add_days, nowdate

from hospitality_pms.services import folio as folio_service

#: Deletion order. Children before parents, money before the folio that holds
#: it, and operational records before the property they hang off - the reverse
#: of the order anything is created in.
TEARDOWN_ORDER = (
	"Folio Log",
	"Corporate Credit Log",
	"Reservation Log",
	"Room Status Log",
	"Payment Transaction",
	"PMS Integration Failure Queue",
	"Guest Folio",
	"Stay",
	"Reservation",
	"Corporate Account",
	"Payment Provider",
	"Booking Channel",
	"Daily Rate",
	"Rate Plan",
	"Hotel Room",
	"Room Type",
	"Guest",
	"User",
	"Posting Profile",
	"Sales Taxes and Charges Template",
	"Account",
	"Item",
	"Property",
)

#: Records a suite never creates directly but a service creates on its behalf -
#: audit trails, logs, queue entries. They are found by `property`, which every
#: operational DocType carries, and purged with the property they belong to.
#:
#: Without this the audit rows outlive their subjects. That is not merely untidy:
#: Frappe's naming series can hand a later run the same reservation name, and
#: the orphaned log rows then attach to it and make a fresh test look like it
#: transitioned six times.
CASCADE_BY_PROPERTY = (
	"Folio Log",
	"Corporate Credit Log",
	"Reservation Log",
	"Room Status Log",
	"Financial Posting Log",
	"PMS Integration Failure Queue",
	"PMS Integration Log",
	"Payment Transaction",
	"Housekeeping Task",
	"Night Audit",
	"Guest Folio",
	"Stay",
	"Reservation",
	"Room Block",
	"Corporate Account",
	"Payment Provider",
	"Booking Channel",
	"Daily Rate",
	"Rate Plan",
	"Hotel Room",
	"Room Type",
)


class Fixtures:
	"""A world to test in, and the means to remove it again."""

	def __init__(self, tag: str):
		self.tag = tag
		self.created: list[tuple[str, str]] = []
		self._settings: dict[str, object] = {}

	# -- lifecycle ------------------------------------------------------

	def set_setting(self, fieldname: str, value):
		"""Change a PMS Setting for the duration of the suite, and remember it.

		Committed and cache-invalidated, because the worker processes read
		settings from their own connections and `get_settings()` goes through
		`frappe.get_cached_doc`.
		"""
		if fieldname not in self._settings:
			self._settings[fieldname] = frappe.db.get_single_value("PMS Settings", fieldname)

		frappe.db.set_single_value("PMS Settings", fieldname, value)
		frappe.clear_document_cache("PMS Settings", "PMS Settings")
		frappe.db.commit()

	def track(self, doctype: str, name: str) -> str:
		self.created.append((doctype, name))
		return name

	def track_fresh(self, doctype: str, name: str, history: dict[str, str]) -> str:
		"""Track a just-created record and clear any history left under its name.

		Frappe rewinds a naming series when its last document is deleted
		(`revert_series_if_last`), so teardown hands the *next* run the same
		name. Audit rows are deliberately not deleted with their subject, so
		without this a brand-new reservation inherits the transitions of the
		one that briefly held its name two runs ago.

		A record created a moment ago has no history by definition, so anything
		found under its name is provably residue. `history` maps each audit
		DocType to the field that points back here.
		"""
		for doctype_name, fieldname in history.items():
			_purge(doctype_name, {fieldname: name})

		return self.track(doctype, name)

	def reset_property_records(self, property_name: str, doctypes):
		"""Clear operational records under a property between tests.

		Suites whose workers commit cannot rely on `IntegrationTestCase`'s
		rollback, so a test that confirms a booking leaves it confirmed for the
		next one - and an inventory test whose premise is "the house is full"
		then starts from a house that is already over-full.
		"""
		for doctype in doctypes:
			_purge(doctype, {"property": property_name})

		frappe.db.commit()

	def teardown(self):
		"""Delete everything created, most dependent first.

		Runs as Administrator with every guard relaxed, because the point is to
		remove test residue - including rows the app's own retention rules
		would refuse to delete, which is correct behaviour in production and
		merely in the way here.
		"""
		frappe.set_user("Administrator")
		frappe.db.rollback()

		try:
			self._teardown_records()
		finally:
			# Settings are global. A record this suite could not delete is
			# untidy; a site left with multi-property operation switched on
			# because a delete raised on the way past is a behaviour change, so
			# the restore happens whatever else went wrong.
			self._restore_settings()

		self.created.clear()
		frappe.db.commit()

	def _restore_settings(self):
		for fieldname, value in self._settings.items():
			frappe.db.set_single_value("PMS Settings", fieldname, value)

		frappe.clear_document_cache("PMS Settings", "PMS Settings")
		self._settings.clear()
		frappe.db.commit()

	def _teardown_records(self):
		# Real ERPNext documents first, and through the ORM, because a
		# submitted invoice owns GL Entries that only cancel-and-delete
		# removes. Deleting the posting log underneath them would leave the
		# ledger holding test money with nothing pointing at it.
		self._teardown_erp_documents()

		# Everything a service created under one of this suite's properties,
		# before the properties themselves go.
		for doctype, name in self.created:
			if doctype == "Property":
				for dependent in CASCADE_BY_PROPERTY:
					_purge(dependent, {"property": name})

		by_doctype: dict[str, list[str]] = {}
		for doctype, name in self.created:
			by_doctype.setdefault(doctype, []).append(name)

		for doctype in TEARDOWN_ORDER:
			for name in reversed(by_doctype.pop(doctype, [])):
				_force_delete(doctype, name)

		# Anything the order does not mention, deleted last and best-effort.
		for doctype, names in by_doctype.items():
			for name in reversed(names):
				_force_delete(doctype, name)

	def _teardown_erp_documents(self):
		"""Cancel and delete every ERPNext document this suite's postings made.

		Payment Entries before Sales Invoices, because an allocated payment
		holds a reference to the invoice and ERPNext will not let the invoice
		go first. Customers last, once nothing bills them.
		"""
		properties = [name for doctype, name in self.created if doctype == "Property"]

		if not properties:
			return

		erp_docs = frappe.get_all(
			"Financial Posting Log",
			filters={"property": ("in", properties), "erp_document": ("is", "set")},
			fields=["erp_doctype", "erp_document"],
		)

		# Payment Entry before Sales Invoice.
		order = {"Payment Entry": 0, "Sales Invoice": 1}
		for row in sorted(erp_docs, key=lambda r: order.get(r["erp_doctype"], 2)):
			_cancel_and_delete(row["erp_doctype"], row["erp_document"])

		for guest in [name for doctype, name in self.created if doctype == "Guest"]:
			customer = frappe.db.get_value("Guest", guest, "customer")
			if customer:
				_cancel_and_delete("Customer", customer)

	# -- setup ----------------------------------------------------------

	def property(self, code: str, *, business_date=None, **overrides) -> str:
		"""A property, cloned from the site's own so it is realistic.

		Company, currency, country and language are taken from an existing
		property rather than invented, because those are ERPNext records the
		test has no business creating.
		"""
		name = f"WV1{code}{self.tag}"[:20]

		if frappe.db.exists("Property", name):
			return self.track("Property", name)

		# The first release refuses a second active property unless multi
		# property operation is on. Test worlds are always additional
		# properties, so the suite turns it on and teardown puts it back.
		self.set_setting("enable_multi_property", 1)

		template = frappe.get_all(
			"Property",
			filters={"name": ("not like", "WV1%")},
			fields=["company", "currency", "country", "time_zone", "default_language"],
			limit=1,
		)

		if not template:
			raise RuntimeError("no seed Property exists on this site to take company/currency from")

		doc = frappe.get_doc(
			{
				"doctype": "Property",
				"property_code": name,
				"property_name": f"Wave 1 {code}",
				"is_active": 1,
				"business_date": business_date or nowdate(),
				"check_in_time": "14:00:00",
				"check_out_time": "12:00:00",
				**template[0],
				**overrides,
			}
		).insert(ignore_permissions=True)

		return self.track("Property", doc.name)

	def room_type(self, property_name: str, code: str = "STD", *, base_rate: float = 100.0) -> str:
		name = f"{property_name}-{code}"

		if not frappe.db.exists("Room Type", name):
			frappe.get_doc(
				{
					"doctype": "Room Type",
					"room_type_code": name,
					"room_type_name": f"Wave 1 {code}",
					"property": property_name,
					"base_occupancy": 2,
					"max_occupancy": 2,
					"max_adults": 2,
					"base_rate": base_rate,
					"is_active": 1,
				}
			).insert(ignore_permissions=True)

		return self.track("Room Type", name)

	def rooms(self, property_name: str, room_type: str, count: int = 1) -> list[str]:
		names = []

		for index in range(1, count + 1):
			name = f"{room_type}-{index:02d}"

			if not frappe.db.exists("Hotel Room", name):
				frappe.get_doc(
					{
						"doctype": "Hotel Room",
						"room_code": name,
						"room_number": f"{index:02d}",
						"property": property_name,
						"room_type": room_type,
						"occupancy_status": "Vacant",
						"housekeeping_status": "Clean",
						"maintenance_status": "Operational",
						"inventory_status": "Available",
						"is_sellable": 1,
					}
				).insert(ignore_permissions=True)

			names.append(self.track("Hotel Room", name))

		return names

	def rate_plan(self, property_name: str, room_type: str, *, base_rate: float = 100.0, code: str = "RP", **overrides) -> str:
		name = f"{property_name}-{code}"

		if not frappe.db.exists("Rate Plan", name):
			doc = frappe.get_doc(
				{
					"doctype": "Rate Plan",
					"rate_plan_code": name,
					"rate_plan_name": "Wave 1 Rack",
					"property": property_name,
					"rate_type": "Standard",
					"valid_from": add_days(nowdate(), -365),
					"is_active": 1,
					"room_types": [{"room_type": room_type, "base_rate": base_rate, "is_active": 1}],
					**overrides,
				}
			)
			doc.insert(ignore_permissions=True)

		return self.track("Rate Plan", name)

	def guest(self, first_name: str = "Wave", **overrides) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Guest",
				"first_name": first_name,
				"last_name": f"One{self.tag}",
				**overrides,
			}
		).insert(ignore_permissions=True)

		return self.track("Guest", doc.name)

	def corporate_account(self, property_name: str, *, credit_limit: float = 1000.0) -> str:
		name = f"{property_name}-CORP"

		if not frappe.db.exists("Corporate Account", name):
			frappe.get_doc(
				{
					"doctype": "Corporate Account",
					"account_code": name,
					"account_name": "Wave 1 Corporate",
					"property": property_name,
					"account_type": "Corporate",
					"is_active": 1,
					"credit_limit": credit_limit,
					"credit_used": 0,
					"credit_available": credit_limit,
					"credit_status": "Active",
					"billing_rule": "Company Pays All",
				}
			).insert(ignore_permissions=True)

		return self.track("Corporate Account", name)

	def reservation(
		self,
		property_name: str,
		room_type: str,
		guest: str,
		*,
		rate_plan: str | None = None,
		arrival=None,
		nights: int = 1,
		rooms: int = 1,
		corporate_account: str | None = None,
	) -> str:
		arrival = arrival or frappe.db.get_value("Property", property_name, "business_date")

		doc = frappe.get_doc(
			{
				"doctype": "Reservation",
				"property": property_name,
				"reservation_status": "Draft",
				"reservation_type": "Individual",
				"guest": guest,
				"arrival_date": arrival,
				"departure_date": add_days(arrival, nights),
				"rate_plan": rate_plan,
				"corporate_account": corporate_account,
				"rooms": [
					{
						"room_type": room_type,
						"rooms": rooms,
						"adults": 1,
						"arrival_date": arrival,
						"departure_date": add_days(arrival, nights),
					}
				],
			}
		).insert(ignore_permissions=True)

		return self.track_fresh("Reservation", doc.name, {"Reservation Log": "reservation"})

	def folio(self, property_name: str, guest: str, **kwargs) -> str:
		name = folio_service.open_folio(property_name, guest, **kwargs)

		return self.track_fresh(
			"Guest Folio",
			name,
			{"Folio Log": "folio", "Financial Posting Log": "folio"},
		)

	def payment_provider(self, property_name: str, provider: str = "Manual") -> str:
		name = f"{property_name}-PP"

		if not frappe.db.exists("Payment Provider", name):
			frappe.get_doc(
				{
					"doctype": "Payment Provider",
					"provider_code": name,
					"provider_name": "Wave 1 Provider",
					"property": property_name,
					"provider": provider,
					"is_active": 1,
					"is_default": 1,
				}
			).insert(ignore_permissions=True)

		return self.track("Payment Provider", name)

	def captured_payment(
		self, property_name: str, provider: str, *, amount: float = 100.0, folio: str | None = None
	) -> str:
		"""A transaction the provider has already captured, ready to refund."""
		doc = frappe.get_doc(
			{
				"doctype": "Payment Transaction",
				"property": property_name,
				"provider": provider,
				"transaction_status": "Captured",
				"transaction_type": "Payment",
				"idempotency_key": f"capture:{self.tag}:{frappe.generate_hash(length=10)}",
				"folio": folio,
				"amount": amount,
				"refunded_amount": 0,
				"provider_reference": f"FAKE-ORIGINAL-{frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)

		return self.track("Payment Transaction", doc.name)

	# -- ERP posting setup ----------------------------------------------
	#
	# Wave 2 posts real Sales Invoices and Payment Entries, so a suite needs a
	# real chart of accounts behind it. These build the minimum: a tax account,
	# a Sales Taxes and Charges Template, an item, and a Posting Profile that
	# maps charge types onto them.

	def tax_account(self, company: str, label: str, *, rate: float = 0) -> str:
		"""A tax account under the company's Duties and Taxes group."""
		parent = frappe.db.get_value(
			"Account", {"company": company, "is_group": 1, "account_name": "Duties and Taxes"}, "name"
		)

		if not parent:
			raise RuntimeError(f"{company} has no Duties and Taxes group to hang a tax account off")

		account_name = f"WV2 {label} {self.tag}"
		existing = frappe.db.get_value(
			"Account", {"company": company, "account_name": account_name}, "name"
		)

		if existing:
			return self.track("Account", existing)

		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": company,
				"account_type": "Tax",
				"root_type": "Liability",
				"tax_rate": rate,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)

		return self.track("Account", doc.name)

	def tax_template(self, company: str, label: str, account: str, rate: float) -> str:
		"""A single-row `On Net Total` Sales Taxes and Charges Template.

		One row on purpose: the folio records one tax figure per charge line, so
		a template that split tax across several heads could not be reproduced
		from it without inventing an apportionment. Posting refuses such a
		template rather than guessing, and this fixture builds the shape the
		product actually supports.
		"""
		title = f"WV2 {label} {self.tag}"
		existing = frappe.db.get_value(
			"Sales Taxes and Charges Template", {"company": company, "title": title}, "name"
		)

		if existing:
			return self.track("Sales Taxes and Charges Template", existing)

		doc = frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": title,
				"company": company,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account,
						"description": title,
						"rate": rate,
					}
				],
			}
		).insert(ignore_permissions=True)

		return self.track("Sales Taxes and Charges Template", doc.name)

	def multi_row_tax_template(self, company: str, label: str, accounts: list[str]) -> str:
		"""A template with more than one tax head, which posting must refuse."""
		title = f"WV2 {label} {self.tag}"
		existing = frappe.db.get_value(
			"Sales Taxes and Charges Template", {"company": company, "title": title}, "name"
		)

		if existing:
			return self.track("Sales Taxes and Charges Template", existing)

		doc = frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": title,
				"company": company,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account,
						"description": f"{title} {index}",
						"rate": 5,
					}
					for index, account in enumerate(accounts, start=1)
				],
			}
		).insert(ignore_permissions=True)

		return self.track("Sales Taxes and Charges Template", doc.name)

	def erp_item(self, code: str) -> str:
		name = f"WV2-{code}-{self.tag}"

		if not frappe.db.exists("Item", name):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": name,
					"item_name": f"Wave 2 {code}",
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"is_sales_item": 1,
				}
			).insert(ignore_permissions=True)

		return self.track("Item", name)

	def posting_profile(
		self,
		property_name: str,
		company: str,
		*,
		charge_map: list[dict],
		default_item: str,
		default_tax_template: str | None = None,
	) -> str:
		"""An active Posting Profile mapping charge types onto items and taxes."""
		name = f"WV2-{property_name}"[:20]

		if frappe.db.exists("Posting Profile", name):
			return self.track("Posting Profile", name)

		doc = frappe.get_doc(
			{
				"doctype": "Posting Profile",
				"profile_code": name,
				"profile_name": f"Wave 2 profile for {property_name}",
				"property": property_name,
				"company": company,
				"is_active": 1,
				"default_item": default_item,
				"default_tax_template": default_tax_template,
				"default_income_account": frappe.db.get_value(
					"Account", {"company": company, "is_group": 0, "root_type": "Income"}, "name"
				),
				"default_cost_center": frappe.db.get_value(
					"Cost Center", {"company": company, "is_group": 0}, "name"
				),
				"default_receivable_account": frappe.get_cached_value(
					"Company", company, "default_receivable_account"
				),
				"charge_items": [{**row, "is_active": 1} for row in charge_map],
			}
		).insert(ignore_permissions=True)

		return self.track("Posting Profile", doc.name)

	def booking_channel(self, property_name: str, room_type: str, rate_plan: str) -> str:
		"""An active channel that pushes both availability and rates."""
		name = f"WV3-{self.tag}"[:20]

		if frappe.db.exists("Booking Channel", name):
			return self.track("Booking Channel", name)

		doc = frappe.get_doc(
			{
				"doctype": "Booking Channel",
				"channel_code": name,
				"channel_name": f"Wave 3 channel {self.tag}",
				"property": property_name,
				"channel_type": "OTA Direct",
				"provider": "Generic Channel Manager",
				"is_active": 1,
				"push_availability": 1,
				"push_rates": 1,
				"default_rate_plan": rate_plan,
				"room_mappings": [
					{
						"room_type": room_type,
						"rate_plan": rate_plan,
						"channel_room_code": "WV3-STD",
						"is_active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		return self.track("Booking Channel", doc.name)

	def user(self, handle: str, roles: list[str], *, properties: list[str] | None = None) -> str:
		"""An operational user with real roles and real User Permissions.

		Property restriction is expressed as a Frappe User Permission, not as
		an app-level convention, because that is the mechanism the product
		actually relies on (HPMS-DEC-052) and the one the cross-property tests
		need to exercise.
		"""
		email = f"wv1-{handle}-{self.tag}@example.com".lower()

		if not frappe.db.exists("User", email):
			doc = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": f"Wave1 {handle}",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
					"roles": [{"role": role} for role in roles],
				}
			).insert(ignore_permissions=True)
			self.track("User", doc.name)
		else:
			doc = frappe.get_doc("User", email)
			doc.set("roles", [{"role": role} for role in roles])
			doc.save(ignore_permissions=True)
			self.track("User", doc.name)

		for property_name in properties or []:
			if not frappe.db.exists(
				"User Permission",
				{"user": email, "allow": "Property", "for_value": property_name},
			):
				frappe.get_doc(
					{
						"doctype": "User Permission",
						"user": email,
						"allow": "Property",
						"for_value": property_name,
						"apply_to_all_doctypes": 1,
					}
				).insert(ignore_permissions=True)

		return email


def _purge(doctype: str, filters: dict):
	"""Delete matching rows and their child rows, underneath the ORM.

	Deliberately below `delete_doc`: these are audit and log records whose
	controllers are written to refuse deletion, which is right in production
	and merely obstructive when clearing up after a test.
	"""
	if not frappe.db.table_exists(doctype):
		return

	try:
		names = frappe.get_all(doctype, filters=filters, pluck="name")
	except Exception:
		# The DocType has no such field - nothing of ours can be in it.
		frappe.db.rollback()
		return

	if not names:
		return

	for table in frappe.get_meta(doctype).get_table_fields():
		frappe.db.delete(table.options, {"parent": ("in", names), "parenttype": doctype})

	frappe.db.delete(doctype, {"name": ("in", names)})


def _cancel_and_delete(doctype: str, name: str):
	"""Take a submitted ERPNext document out, ledger entries and all."""
	if not frappe.db.exists(doctype, name):
		return

	try:
		doc = frappe.get_doc(doctype, name)

		if doc.docstatus == 1:
			doc.flags.ignore_permissions = True
			doc.cancel()

		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
	except Exception:
		# Left behind rather than forced: an ERPNext document that will not
		# cancel usually has a real link still pointing at it, and tearing it
		# out underneath the ORM would leave the ledger inconsistent - which is
		# worse than a stray test row a human can see and remove.
		frappe.db.rollback()


def _force_delete(doctype: str, name: str):
	"""Remove a row, ignoring the retention guards that protect real records."""
	if not frappe.db.exists(doctype, name):
		return

	if doctype == "User":
		frappe.db.delete("User Permission", {"user": name})
		frappe.db.delete("Has Role", {"parent": name, "parenttype": "User"})
		frappe.db.delete("User", {"name": name})
		return

	try:
		frappe.delete_doc(
			doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True, delete_permanently=True
		)
	except Exception:
		# A guard we cannot satisfy from here (a submitted link, a controller
		# that refuses on trash for a reason the test does not care about).
		# The row is test residue either way, so take it out underneath the ORM.
		frappe.db.rollback()
		frappe.db.delete(doctype, {"name": name})
