"""Guest Folio: the operational hospitality subledger.

The folio is **not** the general ledger (HPMS-DEC-003). ERPNext remains the
system of record; the folio is what the front desk works with during a stay and
what checkout reconciles against the invoice it raises.

Two properties are non-negotiable here:

**Idempotency.** Every charge and payment carries an idempotency key. A retried
night audit, a replayed gateway callback or a double-clicked button must post
once. `post_charge` and `post_payment` return the existing line unchanged when
the key has been seen (HPMS-DEC-031).

**No silent edits.** A posted charge is never modified or deleted. Corrections
are reversals: a new, negative line that references the original, with a reason
and an actor. That is what makes the folio reconcilable.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate

from hospitality_pms.services.base import (
	FINANCIAL_POSTING,
	assert_transition,
	lock_and_get_doc,
	require_role,
	service_context,
)
from hospitality_pms.services.exceptions import FolioError, throw
from hospitality_pms.services.property import assert_posting_allowed, get_business_date

FOLIO_DOCTYPE = "Guest Folio"
FOLIO_CHARGE_DOCTYPE = "Folio Charge"
FOLIO_LOG_DOCTYPE = "Folio Log"

OPEN = "Open"
UNDER_REVIEW = "Under Review"
DISPUTED = "Disputed"
READY = "Ready for Settlement"
PARTIALLY_SETTLED = "Partially Settled"
SETTLED = "Settled"
CLOSED = "Closed"

#: Folio state machine, from Workflow Matrix section 4.
TRANSITIONS = {
	OPEN: {UNDER_REVIEW, DISPUTED, READY, CLOSED},
	UNDER_REVIEW: {DISPUTED, READY, OPEN},
	DISPUTED: {UNDER_REVIEW, READY},
	READY: {PARTIALLY_SETTLED, SETTLED, DISPUTED, UNDER_REVIEW},
	PARTIALLY_SETTLED: {SETTLED, DISPUTED, READY},
	SETTLED: {CLOSED, UNDER_REVIEW},
	CLOSED: {UNDER_REVIEW},
}

#: States in which new charges may still be posted. A settled or closed folio
#: is refused, which is what stops a late minibar charge landing after the
#: guest has paid and gone.
POSTABLE_STATES = (OPEN, UNDER_REVIEW, DISPUTED, READY, PARTIALLY_SETTLED)

#: Reversals, adjustments and reopening are finance decisions
#: (Roles Matrix section 4).
FINANCE_ROLES = (
	"Finance Manager",
	"Accounts User",
	"Hospitality Administrator",
	"System Manager",
)

ADJUSTMENT_ROLES = (
	"Finance Manager",
	"Front Office Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)

REOPEN_ROLES = (
	"Finance Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)

#: Charge types that reduce the balance rather than increase it.
CREDIT_CHARGE_TYPES = ("Discount",)

#: The only charge types whose amount may be negative.
#:
#: Every other type describes something the guest consumed, so a negative one
#: is not a charge at all - it is a correction wearing a charge's clothes, and
#: corrections have their own workflows: `post_adjustment` and `reverse_charge`
#: both demand an elevated role and a recorded reason, and a `Discount` is
#: entered positive and stored negative by the rule below.
#:
#: Without this an ordinary agent could post `Room Charge -100` and move the
#: balance exactly as far as an adjustment would, with no role check, no
#: reason and nothing in the audit trail to distinguish it from a real charge
#: (N4).
SIGNED_CHARGE_TYPES = ("Adjustment", "Discount")


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def open_folio(
	property_name: str,
	guest: str,
	*,
	stay: str | None = None,
	reservation: str | None = None,
	room: str | None = None,
	folio_type: str = "Master",
	parent_folio: str | None = None,
	billing_instructions: str | None = None,
) -> str:
	"""Open a folio. One master folio per stay.

	Returns the existing master folio if the stay already has one, so a
	repeated check-in attempt cannot leave a guest with two folios.
	"""
	if stay and folio_type == "Master":
		existing = frappe.db.get_value(
			FOLIO_DOCTYPE, {"stay": stay, "folio_type": "Master"}, "name"
		)
		if existing:
			return existing

	doc = frappe.get_doc(
		{
			"doctype": FOLIO_DOCTYPE,
			"property": property_name,
			"guest": guest,
			"stay": stay,
			"reservation": reservation,
			"room": room,
			"folio_type": folio_type,
			"parent_folio": parent_folio,
			"folio_status": OPEN,
			"opened_on": now_datetime(),
			"billing_instructions": billing_instructions,
		}
	).insert(ignore_permissions=True)

	_log(doc.name, property_name, "Folio opened", to_status=OPEN)

	return doc.name


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_charge(
	folio: str,
	charge_type: str,
	description: str,
	amount: float,
	*,
	idempotency_key: str,
	quantity: float = 1,
	unit_price: float | None = None,
	tax_amount: float = 0,
	item: str | None = None,
	payer: str = "Guest",
	charge_date=None,
	business_date=None,
	reason: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> dict:
	"""Post a charge to a folio, exactly once per idempotency key.

	The key is the caller's promise about identity: the night audit uses
	`{stay}:room-charge:{business_date}`, so re-running the audit for a date
	that already posted returns the original line instead of charging twice.
	"""
	if not idempotency_key:
		throw(_("An idempotency key is required to post a charge."), exc=FolioError)

	# Locked and read in one operation, so the duplicate-key check below is
	# made against the folio's *current* rows. Locking and then reading
	# separately let two concurrent posts of one key both conclude the key was
	# unused - each answering from the snapshot it opened before it started
	# waiting - and both post (N1, N5).
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)

	existing = _find_by_key(doc.charges, idempotency_key)
	if existing:
		return {**existing, "duplicate": True}

	if doc.folio_status not in POSTABLE_STATES:
		throw(
			_("Folio {0} is {1} and cannot take new charges.").format(folio, _(doc.folio_status)),
			exc=FolioError,
		)

	amount = flt(amount)
	tax_amount = flt(tax_amount)

	_assert_sign_is_valid(charge_type, amount, tax_amount)

	charge_business_date = business_date or get_business_date(doc.property)

	# Corrections keep their way in: `SIGNED_CHARGE_TYPES` are the adjustment
	# and discount workflows, which already demand an elevated role and a
	# reason. Everything else is ordinary trading and belongs to an open day.
	if charge_type not in SIGNED_CHARGE_TYPES:
		assert_posting_allowed(
			doc.property, charge_business_date, what=_("a charge"), exc=FolioError
		)

	# Discounts are entered as positive numbers by operators and stored
	# negative, so the balance arithmetic is a plain sum everywhere else.
	if charge_type in CREDIT_CHARGE_TYPES and amount > 0:
		amount = -amount

	row = doc.append(
		"charges",
		{
			"charge_date": charge_date or nowdate(),
			"business_date": charge_business_date,
			"charge_type": charge_type,
			"description": description,
			"item": item,
			"quantity": quantity,
			"unit_price": unit_price if unit_price is not None else amount,
			"amount": amount,
			"tax_amount": tax_amount,
			"total_amount": amount + tax_amount,
			"payer": payer,
			"posted_by": frappe.session.user,
			"posted_on": now_datetime(),
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"idempotency_key": idempotency_key,
		},
	)

	_recalculate(doc)

	with service_context(FINANCIAL_POSTING):
		doc.save(ignore_permissions=True)

	# `reason` is carried through to the audit row rather than only validated.
	# `post_adjustment` demanded one, checked it, and then called this function
	# - which had no parameter to receive it - so every adjustment and discount
	# recorded its amount, its actor and its day, and never why (UAT-001).
	_log(
		folio,
		doc.property,
		f"Charge posted: {charge_type}",
		amount=row.total_amount,
		reason=reason,
		details={"description": description, "idempotency_key": idempotency_key},
	)

	return {"row": row.name, "amount": row.amount, "total_amount": row.total_amount, "duplicate": False}


def post_payment(
	folio: str,
	amount: float,
	payment_method: str,
	*,
	idempotency_key: str,
	payment_type: str = "Payment",
	reference: str | None = None,
	provider_reference: str | None = None,
	payer: str = "Guest",
	payment_date=None,
	business_date=None,
) -> dict:
	"""Record a payment or deposit against a folio, exactly once per key.

	Gateway callbacks are replayed routinely, so this is the guard that stops a
	guest being credited twice for one card capture.
	"""
	if not idempotency_key:
		throw(_("An idempotency key is required to post a payment."), exc=FolioError)

	# Same reasoning as post_charge: the duplicate check has to see the rows as
	# they are now, not as they were before this transaction started waiting.
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)

	existing = _find_by_key(doc.payments, idempotency_key)
	if existing:
		return {**existing, "duplicate": True}

	if doc.folio_status == CLOSED:
		throw(_("Folio {0} is closed and cannot take payments.").format(folio), exc=FolioError)

	amount = flt(amount)

	if amount <= 0:
		throw(_("A payment amount must be greater than zero."), exc=FolioError)

	payment_business_date = business_date or get_business_date(doc.property)

	# A refund is money going back to the guest - a correction, and one that may
	# legitimately settle something from a closed day. Ordinary receipts belong
	# to an open business date.
	if payment_type != "Refund":
		assert_posting_allowed(
			doc.property, payment_business_date, what=_("a payment"), exc=FolioError
		)

	# A refund reduces what has been received.
	if payment_type == "Refund":
		amount = -amount

	row = doc.append(
		"payments",
		{
			"payment_date": payment_date or nowdate(),
			"business_date": payment_business_date,
			"payment_type": payment_type,
			"payment_method": payment_method,
			"amount": amount,
			"reference": reference,
			"provider_reference": provider_reference,
			"payer": payer,
			"received_by": frappe.session.user,
			"received_on": now_datetime(),
			"idempotency_key": idempotency_key,
		},
	)

	_recalculate(doc)

	with service_context(FINANCIAL_POSTING):
		doc.save(ignore_permissions=True)

	_log(
		folio,
		doc.property,
		f"Payment recorded: {payment_type}",
		amount=amount,
		details={"method": payment_method, "idempotency_key": idempotency_key},
	)

	return {"row": row.name, "amount": amount, "balance": doc.balance, "duplicate": False}


def _find_by_key(rows, idempotency_key: str) -> dict | None:
	"""The row already posted under this key, if any.

	Scans the rows the caller already holds rather than issuing its own query,
	and that is the point: those rows came from `lock_and_get_doc`, so they are
	the folio's current children. A fresh `frappe.db.get_value` here would be a
	plain read answered from the pre-lock snapshot, which is exactly how two
	concurrent posts of one key both decided the key was free.
	"""
	row = next((row for row in rows if row.idempotency_key == idempotency_key), None)

	return {"row": row.name, "amount": row.amount} if row else None


def _assert_sign_is_valid(charge_type: str, amount: float, tax_amount: float):
	"""Refuse a negative amount on a charge type that has no negative meaning (N4)."""
	if charge_type in SIGNED_CHARGE_TYPES:
		return

	if amount >= 0 and tax_amount >= 0:
		return

	throw(
		_(
			"A {0} charge cannot be negative. Use an adjustment, a discount or a reversal "
			"of the original charge, which record who approved the correction and why."
		).format(_(charge_type)),
		exc=FolioError,
	)


# ---------------------------------------------------------------------------
# Reversal and adjustment
# ---------------------------------------------------------------------------


def reverse_charge(folio: str, charge_row: str, reason: str) -> dict:
	"""Reverse a posted charge with a compensating line.

	The original is never edited or deleted: an auditor must be able to see
	what was charged, that it was reversed, by whom and why (SAS section 7).
	"""
	require_role(FINANCE_ROLES)

	if not reason or not reason.strip():
		throw(_("A reason is required to reverse a charge."), exc=FolioError)

	# Current under lock: `is_reversed` below is the guard against reversing a
	# charge twice, and reading it from a pre-lock snapshot would let two
	# concurrent reversals both see it unset.
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)
	original = next((row for row in doc.charges if row.name == charge_row), None)

	if not original:
		throw(_("Charge {0} does not belong to folio {1}.").format(charge_row, folio), exc=FolioError)

	if original.is_reversed:
		throw(_("Charge {0} has already been reversed.").format(charge_row), exc=FolioError)

	original.is_reversed = 1
	original.reversed_by = frappe.session.user
	original.reversed_on = now_datetime()
	original.reversal_reason = reason.strip()

	doc.append(
		"charges",
		{
			"charge_date": nowdate(),
			"business_date": get_business_date(doc.property),
			"charge_type": "Adjustment",
			"description": _("Reversal of {0}").format(original.description),
			"quantity": 1,
			"unit_price": -flt(original.amount),
			"amount": -flt(original.amount),
			"tax_amount": -flt(original.tax_amount),
			"total_amount": -flt(original.total_amount),
			"payer": original.payer,
			"posted_by": frappe.session.user,
			"posted_on": now_datetime(),
			"reversal_of": charge_row,
			"reversal_reason": reason.strip(),
			"idempotency_key": f"reversal:{charge_row}",
		},
	)

	_recalculate(doc)

	with service_context(FINANCIAL_POSTING):
		doc.save(ignore_permissions=True)

	_log(
		folio,
		doc.property,
		"Charge reversed",
		amount=-flt(original.total_amount),
		reason=reason.strip(),
		details={"charge_row": charge_row},
	)

	return {"reversed": charge_row, "balance": doc.balance}


def reverse_payment(folio: str, payment_row: str, reason: str, *, check_role: bool = True) -> dict:
	"""Reverse a recorded payment: the row stays, marked reversed with the reason, and the
	balance no longer counts it.

	No compensating line, unlike a charge: `_recalculate` leaves reversed payments out,
	so the row itself is the record. `check_role` is False only for the POS bridge, where
	the authority is ERPNext's own cancel permission on the Payment Entry being withdrawn.
	"""
	if check_role:
		require_role(FINANCE_ROLES)
	if not reason or not reason.strip():
		throw(_("A reason is required to reverse a payment."), exc=FolioError)
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)
	original = next((row for row in doc.payments if row.name == payment_row), None)
	if not original:
		throw(_("Payment {0} does not belong to folio {1}.").format(payment_row, folio), exc=FolioError)
	if original.is_reversed:
		throw(_("Payment {0} has already been reversed.").format(payment_row), exc=FolioError)
	if doc.folio_status == CLOSED:
		throw(_("Folio {0} is closed; its payments cannot be reversed.").format(folio), exc=FolioError)
	original.is_reversed = 1
	original.reversal_reason = reason.strip()
	_recalculate(doc)
	with service_context(FINANCIAL_POSTING):
		doc.save(ignore_permissions=True)
	_log(
		folio,
		doc.property,
		"Payment reversed",
		amount=-flt(original.amount),
		reason=reason.strip(),
		details={"payment_row": payment_row, "payment_entry": original.payment_entry},
	)
	return {"reversed": payment_row, "balance": doc.balance}


def post_adjustment(
	folio: str,
	amount: float,
	description: str,
	reason: str,
	*,
	idempotency_key: str,
	payer: str = "Guest",
) -> dict:
	"""Post a manual adjustment. Always audited, always reasoned.

	The key comes from the caller for the same reason as everywhere else: this
	used to mint a random one per call, so a retried adjustment adjusted twice.
	"""
	require_role(ADJUSTMENT_ROLES)

	if not reason or not reason.strip():
		throw(_("A reason is required to adjust a folio."), exc=FolioError)

	return post_charge(
		folio,
		"Adjustment",
		description,
		amount,
		payer=payer,
		reason=reason.strip(),
		idempotency_key=idempotency_key,
	)


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def _recalculate(doc):
	"""Recompute the folio totals from its lines.

	Totals are derived, never entered. Reversed originals stay in the list and
	their compensating lines cancel them out, so the sum is correct without
	special-casing.
	"""
	charges = sum(flt(row.total_amount) for row in doc.charges)
	taxes = sum(flt(row.tax_amount) for row in doc.charges)
	adjustments = sum(flt(row.total_amount) for row in doc.charges if row.charge_type == "Adjustment")
	payments = sum(flt(row.amount) for row in doc.payments if not row.is_reversed)

	doc.total_charges = flt(charges, 2)
	doc.total_taxes = flt(taxes, 2)
	doc.total_adjustments = flt(adjustments, 2)
	doc.total_payments = flt(payments, 2)
	doc.balance = flt(charges - payments, 2)


def recalculate(folio: str) -> float:
	"""Public recompute, for repair and reconciliation paths."""
	doc = frappe.get_doc(FOLIO_DOCTYPE, folio)
	_recalculate(doc)
	doc.save(ignore_permissions=True)

	return doc.balance


def get_balance(folio: str) -> float:
	return flt(frappe.db.get_value(FOLIO_DOCTYPE, folio, "balance"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def transition(folio: str, target: str, *, reason: str | None = None) -> str:
	"""Move the folio state, validating against the approved machine."""
	# Current under lock. Every guard below - the state machine, the unposted
	# charge check, the outstanding balance check - is decided from this
	# document, so it has to be the folio as it stands now.
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)
	previous = doc.folio_status

	assert_transition(previous, target, TRANSITIONS, _("Folio"))

	if previous == CLOSED and target == UNDER_REVIEW:
		# Reopening a closed folio is a controlled exception, because it
		# reopens something ERPNext has already been told about.
		require_role(REOPEN_ROLES)

		if not reason or not reason.strip():
			throw(_("A reason is required to reopen a closed folio."), exc=FolioError)

	# Closing is the final state, and a closed folio is never revisited. Letting
	# one close with charges that never reached ERPNext would strand that
	# revenue outside the ledger, which is exactly the discrepancy the night
	# audit then has to chase. Settled is deliberately not guarded: money can
	# legitimately be collected before the posting run catches up.
	if target == CLOSED:
		unposted = [row.name for row in doc.charges if not row.is_posted_to_erp]

		if unposted:
			throw(
				_(
					"Folio {0} has {1} charge(s) that have not reached ERPNext. Post the folio "
					"before closing it."
				).format(folio, len(unposted)),
				exc=FolioError,
			)

	if target in (SETTLED, CLOSED) and abs(flt(doc.balance)) > 0.005:
		throw(
			_("Folio {0} has an outstanding balance of {1} and cannot be {2}.").format(
				folio, flt(doc.balance, 2), _(target)
			),
			exc=FolioError,
		)

	frappe.db.set_value(FOLIO_DOCTYPE, folio, "folio_status", target, update_modified=True)

	if target == CLOSED:
		frappe.db.set_value(FOLIO_DOCTYPE, folio, "closed_on", now_datetime(), update_modified=False)

	_log(folio, doc.property, "Status changed", from_status=previous, to_status=target, reason=reason)

	return target


# ---------------------------------------------------------------------------
# Split and merge
# ---------------------------------------------------------------------------


def split_folio(folio: str, charge_rows: list[str], *, folio_type: str = "Split", payer: str = "Company") -> str:
	"""Move selected charges onto a new folio.

	Used for company-pay / guest-pay separation. The charges are moved, not
	copied: a charge exists on exactly one folio, or the two folios would sum
	to more than the guest owes.
	"""
	require_role(ADJUSTMENT_ROLES)

	source = lock_and_get_doc(FOLIO_DOCTYPE, folio)

	if source.folio_status in (SETTLED, CLOSED):
		throw(_("A {0} folio cannot be split.").format(_(source.folio_status)), exc=FolioError)

	moving = [row for row in source.charges if row.name in set(charge_rows)]

	if not moving:
		throw(_("Select at least one charge to move."), exc=FolioError)

	_assert_charges_movable(moving, source.charges)

	target = open_folio(
		source.property,
		source.guest,
		stay=source.stay,
		reservation=source.reservation,
		room=source.room,
		folio_type=folio_type,
		parent_folio=folio,
	)

	# Just created by open_folio above, so there is nothing stale to fear; it is
	# locked all the same because rows are about to be written onto it.
	target_doc = lock_and_get_doc(FOLIO_DOCTYPE, target)

	for row in moving:
		values = row.as_dict()
		for field in ("name", "parent", "parentfield", "parenttype", "idx", "creation", "modified", "owner", "modified_by"):
			values.pop(field, None)

		values["payer"] = payer
		# The idempotency_key is deliberately carried over unchanged. It is the
		# moved charge's logical identity for its whole lifetime, and a room charge
		# posted under `room-charge:{stay}:{business_date}` must keep it so a Night
		# Audit re-run recognises the charge wherever the split has moved it. Minting
		# a new `split:{...}` key here is what let the same room night be billed twice
		# across two folios (F-FIN7); the cross-folio room-charge check in
		# `night_audit.post_room_charges` is the other half of that guard.
		target_doc.append("charges", values)

	source.charges = [row for row in source.charges if row.name not in set(charge_rows)]

	_recalculate(source)
	_recalculate(target_doc)

	# The rows are moving to another folio, not being destroyed, so the folio's
	# "corrections are reversals" guard is told this is a move.
	source.flags.hpms_moving_rows = True

	with service_context(FINANCIAL_POSTING):
		source.save(ignore_permissions=True)
		target_doc.save(ignore_permissions=True)

	_log(folio, source.property, "Charges split out", details={"to_folio": target, "rows": charge_rows})
	_log(target, source.property, "Charges split in", details={"from_folio": folio, "rows": charge_rows})

	return target


def merge_folio(source_folio: str, target_folio: str) -> str:
	"""Move every charge and payment from one folio into another."""
	require_role(ADJUSTMENT_ROLES)

	if source_folio == target_folio:
		throw(_("A folio cannot be merged into itself."), exc=FolioError)

	# Locked in name order so two merges touching the same pair cannot deadlock
	# against each other, and read from those same locking reads.
	locked = {name: lock_and_get_doc(FOLIO_DOCTYPE, name) for name in sorted([source_folio, target_folio])}

	source = locked[source_folio]
	target = locked[target_folio]

	for state_holder in (source, target):
		if state_holder.folio_status in (SETTLED, CLOSED):
			throw(
				_("Folio {0} is {1} and cannot be merged.").format(
					state_holder.name, _(state_holder.folio_status)
				),
				exc=FolioError,
			)

	# A merge moves every row, so a reversal and its original always travel
	# together; the one thing it must still refuse is a row already in ERPNext,
	# whose Financial Posting Log link ties it to the folio it was posted from
	# (read by reconcile_folio / _allocate_against_folio_invoices).
	_assert_no_posted_rows(source.charges, source.payments)

	for table in ("charges", "payments"):
		for row in source.get(table):
			values = row.as_dict()
			for field in ("name", "parent", "parentfield", "parenttype", "idx", "creation", "modified", "owner", "modified_by"):
				values.pop(field, None)
			# Original key preserved, for the reason recorded in `split_folio`: a
			# charge keeps one idempotency identity for its lifetime (F-FIN7).
			target.append(table, values)

		source.set(table, [])

	_recalculate(source)
	_recalculate(target)

	source.flags.hpms_moving_rows = True

	with service_context(FINANCIAL_POSTING):
		source.save(ignore_permissions=True)
		target.save(ignore_permissions=True)

	transition(source_folio, CLOSED, reason=_("Merged into {0}").format(target_folio))

	_log(target_folio, target.property, "Folio merged in", details={"from_folio": source_folio})

	return target_folio


def _assert_charges_movable(moving, all_charges):
	"""Refuse to move charges whose identity or ledger linkage a move would break.

	Two rows may not be moved between folios:

	* one already posted to ERPNext (`is_posted_to_erp`), because the Financial
	  Posting Log ties it to the folio it was posted from and `reconcile_folio` /
	  `_allocate_against_folio_invoices` read that link - move the row and the
	  reconciliation can no longer find its money;
	* one leg of a reversal without the other. A reversed original and its
	  compensating line are a single accounting fact; splitting them apart leaves
	  one folio holding half a correction and the totals on both wrong.
	"""
	moving_names = {row.name for row in moving}

	for row in moving:
		if row.is_posted_to_erp:
			throw(
				_(
					"Charge {0} has already been posted to ERPNext and cannot be moved between "
					"folios. Reverse it first if it belongs elsewhere."
				).format(row.name),
				exc=FolioError,
			)

		if row.reversal_of and row.reversal_of not in moving_names:
			throw(
				_("A reversal cannot be moved without the charge it reverses. Move both together."),
				exc=FolioError,
			)

		if row.is_reversed:
			reversal = next((other for other in all_charges if other.reversal_of == row.name), None)

			if reversal and reversal.name not in moving_names:
				throw(
					_("A reversed charge cannot be moved without its reversal. Move both together."),
					exc=FolioError,
				)


def _assert_no_posted_rows(charges, payments):
	"""Refuse a merge while either folio still holds a row posted to ERPNext."""
	for row in charges:
		if row.is_posted_to_erp:
			throw(
				_(
					"Charge {0} has already been posted to ERPNext and cannot be moved between folios."
				).format(row.name),
				exc=FolioError,
			)

	for row in payments:
		if row.is_posted_to_erp:
			throw(
				_(
					"Payment {0} has already been posted to ERPNext and cannot be moved between folios."
				).format(row.name),
				exc=FolioError,
			)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _log(
	folio: str,
	property_name: str,
	action: str,
	*,
	from_status: str | None = None,
	to_status: str | None = None,
	amount: float | None = None,
	reason: str | None = None,
	details: dict | None = None,
):
	import json

	frappe.get_doc(
		{
			"doctype": FOLIO_LOG_DOCTYPE,
			"property": property_name,
			"folio": folio,
			"action": action,
			"from_status": from_status,
			"to_status": to_status,
			"amount": amount,
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": reason,
			"details": json.dumps(details, default=str) if details else None,
		}
	).insert(ignore_permissions=True)


def get_folio_for_stay(stay: str) -> str | None:
	return frappe.db.get_value(FOLIO_DOCTYPE, {"stay": stay, "folio_type": "Master"}, "name")
