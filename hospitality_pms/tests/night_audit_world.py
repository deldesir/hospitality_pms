"""A property that can actually run a Night Audit, for the Wave-4 suites.

An audit needs more standing behind it than most tests: a property with a
business date, rooms to be occupied, a guest in one of them so there is a room
charge to post, and a posting profile so reconciliation has real ERPNext
documents to read rather than an approximation of them.

Built on the Wave-2 `PostingWorld`, because the reconciliation half of a Night
Audit *is* the Wave-2 ERP reconciliation, and rebuilding a second version of
that setup here would be two chances to configure it differently.
"""

import frappe
from frappe.utils import add_days, getdate

from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services.property import BUSINESS_DATE_FLAG
from hospitality_pms.tests.posting_world import PostingWorld

ROOM_RATE = 100.0


class NightAuditWorld(PostingWorld):
	"""A postable property, plus rooms and an occupant."""

	def __init__(self, tag: str, code: str, *, rooms: int = 4):
		super().__init__(tag, code)

		self.room_type = self.fixtures.room_type(self.property, base_rate=ROOM_RATE)
		self.rooms = self.fixtures.rooms(self.property, self.room_type, count=rooms)
		self.rate_plan = self.fixtures.rate_plan(
			self.property, self.room_type, base_rate=ROOM_RATE
		)

		frappe.db.commit()

	# -- business date ----------------------------------------------------

	@property
	def business_date(self):
		return getdate(frappe.db.get_value("Property", self.property, "business_date"))

	def set_business_date(self, value):
		"""Move the property's date directly, for arranging a test's premise.

		Uses the same guarded flag the Night Audit uses, because the Property
		controller refuses any other writer - which is the control this wave
		exists to protect, not one to work around.
		"""
		frappe.flags[BUSINESS_DATE_FLAG] = True
		try:
			frappe.db.set_value("Property", self.property, "business_date", getdate(value))
		finally:
			frappe.flags[BUSINESS_DATE_FLAG] = False

		frappe.clear_document_cache("Property", self.property)
		frappe.db.commit()

	# -- occupancy --------------------------------------------------------

	def check_in_guest(self, room_index: int = 0, *, nights: int = 3) -> dict:
		"""A guest in house across the audit date, so a room charge is owed."""
		guest = self.fixtures.guest(f"Sleeper{room_index}")
		arrival = self.business_date

		reservation = self.fixtures.reservation(
			self.property,
			self.room_type,
			guest,
			rate_plan=self.rate_plan,
			arrival=arrival,
			nights=nights,
		)
		reservation_service.confirm(reservation)

		line = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")[0]
		result = stay_service.check_in(reservation, line, self.rooms[room_index])

		frappe.db.commit()

		return result

	# -- folios for the reconciliation population -------------------------

	def settled_folio(self, *, charge: float = 50.0, posted_to_erp: bool = True) -> str:
		"""A folio in a settled state, of the kind reconciliation examines.

		`posted_to_erp` False leaves the money in the folio and not in ERPNext,
		which is exactly the variance a Night Audit must refuse to close over.
		"""
		guest = self.fixtures.guest("Departed")
		folio = self.fixtures.folio(self.property, guest)

		self.charge(folio, "Room Charge", charge, 0, f"settled:{folio}")

		if posted_to_erp:
			from hospitality_pms.services import posting as posting_service

			posting_service.post_folio_invoice(folio)

		folio_service.post_payment(
			folio, charge, "Cash", idempotency_key=f"{self.tag}:settle:{folio}"
		)

		if posted_to_erp:
			from hospitality_pms.services import posting as posting_service

			row = frappe.get_all("Folio Payment", filters={"parent": folio}, pluck="name")[0]
			posting_service.post_folio_payment(folio, row)

		folio_service.transition(folio, folio_service.UNDER_REVIEW, reason="Checkout review")
		folio_service.transition(folio, folio_service.READY, reason="Checkout")
		folio_service.transition(folio, folio_service.SETTLED, reason="Settled")

		frappe.db.commit()

		return folio

    # -- cheap bulk folios, for the population-size tests -----------------

	def bulk_settled_folios(self, count: int, *, business_date=None) -> list[str]:
		"""Many settled folios with no money in them at all.

		Reconciliation still has to look at every one of them, and a folio with
		nothing in it reconciles trivially - which is the point: the >200 tests
		are about *population*, not about arithmetic, and building two hundred
		real ERP invoices would test ERPNext's throughput rather than this
		wave's defect.
		"""
		guest = self.fixtures.guest("Bulk")
		names = []

		for index in range(count):
			folio = folio_service.open_folio(self.property, guest)
			self.fixtures.track("Guest Folio", folio)

			frappe.db.set_value(
				folio_service.FOLIO_DOCTYPE, folio, "folio_status", "Settled", update_modified=False
			)
			names.append(folio)

		frappe.db.commit()

		return names

	def checked_out_stay(self, room_index: int = 0, *, charge: float = 90.0) -> dict:
		"""A guest who has been through the supported checkout, end to end.

		The premise `reverse_checkout` needs: a Checked Out stay whose folio is
		Settled or Closed and whose money is in ERPNext, so reversing it exercises
		the real path rather than a folio arranged to look like one.
		"""
		from hospitality_pms.services import checkout as checkout_service

		result = self.check_in_guest(room_index, nights=2)
		stay = result["stay"] if isinstance(result, dict) else result
		folio = folio_service.get_folio_for_stay(stay)

		self.charge(folio, "Room Charge", charge, 0, f"{self.tag}:stay-charge:{folio}")
		folio_service.post_payment(
			folio, charge, "Cash", idempotency_key=f"{self.tag}:stay-settle:{folio}"
		)

		checkout_service.check_out(stay)
		frappe.db.commit()

		return {"stay": stay, "folio": folio, "room": self.rooms[room_index]}

	def credit_note(self, invoice: str) -> str:
		"""A submitted ERPNext credit note against an invoice, raised the way finance does.

		Through `make_sales_return`, so the document carries the `is_return` and
		`return_against` wiring ERPNext's own return handling depends on, rather
		than a hand-built negative invoice that would only look like one.
		"""
		try:
			# ERPNext moved the mapping functions to mapper.py (May 2026)
			from erpnext.accounts.doctype.sales_invoice.mapper import make_sales_return
		except ImportError:
			from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		note = make_sales_return(invoice)
		note.flags.ignore_permissions = True
		note.insert()
		note.submit()

		# Deliberately *not* committed, unlike the folio builders above. A credit
		# note is the damage a test arranges, not part of the world it shares: left
		# committed it survives `tearDown`'s rollback and every later test in the
		# class reconciles against a folio ERPNext has credited back.
		return note.name

	def folio_with_unposted_charge(self, amount: float = 25.0) -> str:
		"""A settled folio whose money never reached ERPNext."""
		guest = self.fixtures.guest("Variance")
		folio = self.fixtures.folio(self.property, guest)

		self.charge(folio, "Room Charge", amount, 0, f"variance:{folio}")

		frappe.db.set_value(
			folio_service.FOLIO_DOCTYPE, folio, "folio_status", "Settled", update_modified=False
		)
		frappe.db.commit()

		return folio
