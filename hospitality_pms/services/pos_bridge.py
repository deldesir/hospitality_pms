"""Charge a point-of-sale credit sale to the guest's room.

A hotel's bar or restaurant sells at a POS Next till. When the cashier puts the bill on
the guest's account instead of taking payment (a "Pay on Account" credit sale: a
submitted, unpaid Sales Invoice), the amount belongs on the guest's folio, to be settled
at checkout together with the room.

Revenue and stock are booked once, by the POS invoice itself. The folio receives a charge
line that is already posted to ERPNext (`is_posted_to_erp`, `sales_invoice`), so the
checkout invoice skips it; and the invoice is entered in the financial posting log, so the
checkout payment allocates against it like any folio invoice. The guest's balance includes
the bar bill; the ledger sees one invoice, one receivable, one settlement.

Enabled per till with the POS Profile field "Charge credit sales to the guest's room". On
such a till a credit sale to a customer who is not in house is refused: the till exists to
charge rooms, and an open credit to a stranger is a mistake, not a feature.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import posting as posting_service
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services.base import lock_and_get_doc
from hospitality_pms.services.exceptions import FolioError, throw

CHARGE_TYPE = "Point of Sale"
TILL_FLAG = "hpms_charge_to_room"           # Custom Field on POS Profile
INVOICE_STAY_FIELD = "hpms_stay"            # Custom Fields on Sales Invoice
INVOICE_FOLIO_FIELD = "hpms_folio"


def is_room_charge_till(pos_profile: str | None) -> bool:
	if not pos_profile or not frappe.db.has_column("POS Profile", TILL_FLAG):
		return False
	return bool(cint(frappe.db.get_value("POS Profile", pos_profile, TILL_FLAG)))


def charge_key(invoice: str) -> str:
	return f"pos-invoice:{invoice}"


def find_in_house_stays(customer: str) -> list[dict]:
	"""The stays in house whose guest is this ERPNext customer, newest check-in first.

	A guest's Customer is created the first time money moves (see
	`guests.ensure_customer`), and a folio may carry a customer of its own (a company
	paying for the stay). Both routes are read, so the till finds the guest whichever way
	the Customer came to exist.
	"""
	in_house = (stay_service.IN_HOUSE, stay_service.DUE_OUT)
	guests = frappe.get_all("Guest", filters={"customer": customer}, pluck="name")
	stays = []
	if guests:
		stays += frappe.get_all(
			"Stay",
			filters={"stay_status": ("in", in_house), "guest": ("in", guests)},
			fields=["name", "guest", "room", "folio", "property", "checked_in_on"],
		)
	seen = {s["name"] for s in stays}
	for folio_stay in frappe.get_all(
		folio_service.FOLIO_DOCTYPE,
		filters={"customer": customer, "folio_type": "Master", "folio_status": ("in", folio_service.POSTABLE_STATES)},
		pluck="stay",
	):
		if folio_stay and folio_stay not in seen:
			row = frappe.db.get_value(
				"Stay", folio_stay, ["name", "guest", "room", "folio", "property", "checked_in_on", "stay_status"], as_dict=True
			)
			if row and row.stay_status in in_house:
				stays.append(row)
				seen.add(folio_stay)
	return sorted(stays, key=lambda s: s.get("checked_in_on") or "", reverse=True)


def resolve_stay(invoice) -> dict:
	"""The one stay this sale belongs to, or a refusal that names the fix."""
	if invoice.get(INVOICE_STAY_FIELD):
		row = frappe.db.get_value(
			"Stay", invoice.get(INVOICE_STAY_FIELD), ["name", "guest", "room", "folio", "property", "stay_status"], as_dict=True
		)
		if not row or row.stay_status not in (stay_service.IN_HOUSE, stay_service.DUE_OUT):
			throw(_("Stay {0} is not in house.").format(invoice.get(INVOICE_STAY_FIELD)), exc=FolioError)
		return row
	stays = find_in_house_stays(invoice.customer)
	if not stays:
		throw(
			_(
				"{0} is not a guest in house, so this sale cannot be charged to a room. "
				"Take payment, or pick the guest's own customer record."
			).format(invoice.customer_name or invoice.customer),
			exc=FolioError,
		)
	if len(stays) > 1:
		throw(
			_(
				"{0} has {1} rooms in house ({2}). Charge this sale from the PMS (Room Service Order), "
				"where the room is chosen."
			).format(invoice.customer_name or invoice.customer, len(stays), ", ".join(s["room"] or s["name"] for s in stays)),
			exc=FolioError,
		)
	return stays[0]


def _describe(invoice) -> str:
	items = ", ".join(f"{flt(row.qty):g} × {row.item_name or row.item_code}" for row in invoice.items[:4])
	if len(invoice.items) > 4:
		items += ", …"
	return f"{invoice.pos_profile or 'POS'} {invoice.name}: {items}"


def charge_invoice_to_room(invoice) -> dict | None:
	"""Post the unpaid part of a submitted POS invoice to the guest's folio, once.

	Returns the charge result, or None when the sale is not a room charge (the till is
	not set up for it, the invoice is not a POS sale, or nothing is outstanding).
	"""
	if not cint(invoice.is_pos) or invoice.docstatus != 1 or cint(invoice.is_return):
		return None
	if not is_room_charge_till(invoice.pos_profile):
		return None
	outstanding = flt(invoice.outstanding_amount)
	if outstanding <= 0:
		return None
	stay = resolve_stay(invoice)
	folio = stay.get("folio") or folio_service.get_folio_for_stay(stay["name"])
	if not folio:
		throw(_("Stay {0} has no folio to charge.").format(stay["name"]), exc=FolioError)
	result = folio_service.post_charge(
		folio,
		CHARGE_TYPE,
		_describe(invoice),
		outstanding,
		idempotency_key=charge_key(invoice.name),
		reference_doctype="Sales Invoice",
		reference_name=invoice.name,
	)
	if not result.get("duplicate"):
		# The invoice *is* the ERPNext posting of this line: the checkout invoice must skip
		# it, and the checkout payment must find it. Same transaction as the charge.
		frappe.db.set_value(
			"Folio Charge", result["row"], {"is_posted_to_erp": 1, "sales_invoice": invoice.name}, update_modified=False
		)
		folio_doc = frappe.get_doc(folio_service.FOLIO_DOCTYPE, folio)
		posting_service.record_external_invoice(
			folio_doc, invoice.name, outstanding, charge_key(invoice.name), actor=frappe.session.user
		)
		invoice.db_set({INVOICE_STAY_FIELD: stay["name"], INVOICE_FOLIO_FIELD: folio}, update_modified=False)
		folio_service._log(
			folio,
			folio_doc.property,
			"Point of sale charged to room",
			amount=outstanding,
			details={"sales_invoice": invoice.name, "pos_profile": invoice.pos_profile, "room": stay.get("room")},
		)
	return {**result, "folio": folio, "stay": stay["name"]}


def credit_return_to_room(invoice) -> dict | None:
	"""A POS return against a room-charged sale takes the same amount back off the folio.

	POS Next books a linked return with `update_outstanding_for_self = 0`, so the credit
	sits on the original invoice: the checkout payment allocates against what is then
	still outstanding, and the folio must agree. An adjustment line, signed and reasoned,
	is what the folio allows for that.
	"""
	if not cint(invoice.is_return) or not invoice.return_against or invoice.docstatus != 1:
		return None
	original = frappe.db.get_value(
		"Sales Invoice", invoice.return_against, [INVOICE_STAY_FIELD, INVOICE_FOLIO_FIELD], as_dict=True
	)
	if not original or not original.get(INVOICE_FOLIO_FIELD):
		return None
	folio = original[INVOICE_FOLIO_FIELD]
	status = frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "folio_status")
	if status not in folio_service.POSTABLE_STATES:
		throw(
			_("Folio {0} is {1}: the sale was settled at checkout, refund the guest directly.").format(folio, _(status)),
			exc=FolioError,
		)
	amount = -abs(flt(invoice.grand_total))
	result = folio_service.post_charge(
		folio,
		"Adjustment",
		_("Return {0} against {1}").format(invoice.name, invoice.return_against),
		amount,
		idempotency_key=charge_key(invoice.name),
		reason=_("Point of sale return {0}").format(invoice.name),
		reference_doctype="Sales Invoice",
		reference_name=invoice.name,
	)
	if not result.get("duplicate"):
		frappe.db.set_value(
			"Folio Charge", result["row"], {"is_posted_to_erp": 1, "sales_invoice": invoice.name}, update_modified=False
		)
		invoice.db_set({INVOICE_STAY_FIELD: original.get(INVOICE_STAY_FIELD), INVOICE_FOLIO_FIELD: folio}, update_modified=False)
	return {**result, "folio": folio}


def release_cancelled_invoice(invoice) -> None:
	"""A cancelled room-charged invoice comes off the folio, by reversal, while the folio is
	still open. After checkout the sale has been settled with the room and cannot be
	cancelled: a return is the way back."""
	folio = invoice.get(INVOICE_FOLIO_FIELD)
	if not folio:
		return
	doc = lock_and_get_doc(folio_service.FOLIO_DOCTYPE, folio)
	row = next((c for c in doc.charges if c.idempotency_key == charge_key(invoice.name)), None)
	if not row:
		return
	if doc.folio_status not in folio_service.POSTABLE_STATES:
		throw(
			_("Folio {0} is {1}: {2} was settled with the room and cannot be cancelled. Issue a return.").format(
				folio, _(doc.folio_status), invoice.name
			),
			exc=FolioError,
		)
	if not row.is_reversed:
		folio_service.reverse_charge(folio, row.name, _("Sales Invoice {0} cancelled").format(invoice.name))
	posting_service.cancel_external_invoice(charge_key(invoice.name), _("Sales Invoice {0} cancelled").format(invoice.name))


# -- hooks (Sales Invoice doc_events) ----------------------------------------


def on_submit(doc, method=None):
	if cint(doc.is_return):
		credit_return_to_room(doc)
	else:
		charge_invoice_to_room(doc)


def before_cancel(doc, method=None):
	release_cancelled_invoice(doc)
