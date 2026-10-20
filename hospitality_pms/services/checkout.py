"""Checkout: settle the folio, post to ERPNext, release the room.

Checkout is where the operational and financial sides have to agree. The order
matters and is deliberate:

1. Validate the folio - nothing is settled while charges are disputed.
2. Post the invoice to ERPNext, so the ledger has the revenue.
3. Post the payments, so the ledger has the money.
4. Settle and close the folio.
5. Close the stay, release the room to Vacant Dirty and raise the housekeeping
   task.

Financial steps come before operational ones on purpose. If posting fails, the
guest is still in house and the room is still theirs; nothing is lost. If the
order were reversed, a posting failure would leave a room sold to the next
guest while the previous guest's revenue was never recorded.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import guests as guest_service
from hospitality_pms.services import posting as posting_service
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import rooms as room_service
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services.base import lock_and_get_doc, lock_document, require_role
from hospitality_pms.services.exceptions import FolioError, InvalidStateTransitionError, throw
from hospitality_pms.services.property import get_property

STAY_DOCTYPE = "Stay"

#: Reversing a checkout re-opens a settled folio and an ERPNext invoice.
REVERSAL_ROLES = stay_service.CHECKOUT_REVERSAL_ROLES


#: What a departure blocker is *about*, as a stable identifier.
#:
#: The blocker messages themselves are translated and several of them interpolate
#: an amount, which makes them unusable as a basis for any decision: matching on
#: the word "balance" stops working on an Arabic session, and Arabic is a
#: first-release requirement. These kinds are the machine-readable form of the
#: same list, in the same order, and they name a category without naming a figure.
#:
#: Their first consumer is `api.checkout.summary`, which uses them to decide which
#: sentences a caller who may not read Guest Folio is entitled to (16.7.5-R1B).
BLOCKER_STAY_STATUS = "stay_status"
BLOCKER_FOLIO_DISPUTED = "folio_disputed"
BLOCKER_OUTSTANDING_BALANCE = "outstanding_balance"
BLOCKER_RELATED_FOLIO_BALANCE = "related_folio_balance"

#: The kinds whose sentence is about the folio rather than the stay. Everything
#: here is Guest Folio's to disclose - including the disputed flag, which is a
#: `folio_status` and not a fact about the guest's stay.
FOLIO_BLOCKER_KINDS = (
	BLOCKER_FOLIO_DISPUTED,
	BLOCKER_OUTSTANDING_BALANCE,
	BLOCKER_RELATED_FOLIO_BALANCE,
)


def get_departure_blockers(stay_status: str, folio, related_folios: list[dict]) -> list[str]:
	"""What stands between this stay and the door, worst first.

	One rule, two readers: `get_checkout_summary()` calls it for a single stay
	with loaded documents, and the departures board calls it for a whole day
	with rows fetched in bulk. Keeping it here is what stops the board from
	telling the desk a guest is ready to leave while the checkout screen
	refuses them (SAD section 6).

	`folio` is anything with `folio_status` and `balance` — a Document or a
	plain row from a bulk query.
	"""
	return [message for message, _kind in _departure_blockers(stay_status, folio, related_folios)]


def _departure_blockers(stay_status: str, folio, related_folios: list[dict]) -> list[tuple[str, str]]:
	"""Every blocker as `(message, kind)`, worst first.

	The single construction of the list. `get_departure_blockers` and
	`get_checkout_summary` are both views of it, which is what keeps a message and
	its kind from ever disagreeing about what they describe - a message paired with
	the wrong kind would redact the wrong sentence.
	"""
	blockers: list[tuple[str, str]] = []

	if stay_status not in (stay_service.IN_HOUSE, stay_service.DUE_OUT):
		blockers.append((_("The stay is {0}.").format(_(stay_status)), BLOCKER_STAY_STATUS))

	folio_status = folio.get("folio_status") if isinstance(folio, dict) else folio.folio_status
	balance = folio.get("balance") if isinstance(folio, dict) else folio.balance

	if folio_status == folio_service.DISPUTED:
		blockers.append(
			(_("The folio is disputed and must be resolved first."), BLOCKER_FOLIO_DISPUTED)
		)

	if abs(flt(balance)) > 0.005:
		blockers.append(
			(
				_("The folio has an outstanding balance of {0}.").format(flt(balance, 2)),
				BLOCKER_OUTSTANDING_BALANCE,
			)
		)

	for row in related_folios or []:
		if abs(flt(row["balance"])) > 0.005:
			blockers.append(
				(
					_("Split folio {0} still has a balance of {1}.").format(
						row["name"], flt(row["balance"], 2)
					),
					BLOCKER_RELATED_FOLIO_BALANCE,
				)
			)

	return blockers


def get_checkout_summary(stay: str) -> dict:
	"""What the guest owes and what stands in the way of leaving.

	Read-only. The front desk opens this before taking payment, so it must not
	change anything.
	"""
	doc = frappe.get_doc(STAY_DOCTYPE, stay)

	# Checked here as well as at the endpoint. This is the function that
	# actually assembles the payload that leaked - guest name, room, charges,
	# payments, balance and blockers - and it is reachable from more than one
	# caller, so the guard belongs with the disclosure rather than only in
	# front of it (P1-15).
	doc.check_permission("read")

	folio = doc.folio or folio_service.get_folio_for_stay(stay)

	if not folio:
		throw(_("Stay {0} has no folio.").format(stay), exc=FolioError)

	folio_doc = frappe.get_doc(folio_service.FOLIO_DOCTYPE, folio)

	# Other folios split off this stay must also be settled, or a company-pay
	# balance would walk out of the door with the guest.
	related = frappe.get_all(
		folio_service.FOLIO_DOCTYPE,
		filters={"stay": stay, "name": ("!=", folio)},
		fields=["name", "folio_type", "balance", "folio_status"],
	)

	detail = _departure_blockers(doc.stay_status, folio_doc, related)
	blockers = [message for message, _kind in detail]

	return {
		"stay": stay,
		"folio": folio,
		"guest_name": doc.guest_name,
		"room": doc.room,
		"arrival_date": doc.arrival_date,
		"departure_date": doc.departure_date,
		"currency": folio_doc.currency,
		"total_charges": flt(folio_doc.total_charges, 2),
		"total_payments": flt(folio_doc.total_payments, 2),
		"balance": flt(folio_doc.balance, 2),
		"related_folios": related,
		"blockers": blockers,
		# Paired positionally with `blockers`, so a caller that may not be told the
		# amounts can still say which sentences are about money. `api.checkout.summary`
		# is what needs it; see `_disclose_checkout` there for why the redaction is
		# done at the boundary and not in this function.
		"blocker_kinds": [kind for _message, kind in detail],
		"can_check_out": not blockers,
	}


def check_out(
	stay: str,
	*,
	post_to_erp: bool = True,
	allow_open_balance: bool = False,
	reason: str | None = None,
) -> dict:
	"""Check a guest out.

	`allow_open_balance` is the city-ledger case: a corporate account whose
	balance is transferred to accounts receivable rather than collected at the
	desk. It needs a manager, because letting a guest leave owing money is a
	credit decision.
	"""
	# Current under lock: a stay checked out by another till a moment ago must
	# not be checked out again from this transaction's earlier view (N1).
	doc = lock_and_get_doc(STAY_DOCTYPE, stay)
	doc.check_permission("write")

	if doc.stay_status not in (stay_service.IN_HOUSE, stay_service.DUE_OUT):
		throw(
			_("Stay {0} is {1} and cannot be checked out.").format(stay, _(doc.stay_status)),
			exc=InvalidStateTransitionError,
		)

	summary = get_checkout_summary(stay)
	folio = summary["folio"]

	if summary["blockers"]:
		# Decided on the blocker *kinds*, not on the rendered sentences
		# (16.7.5-R1B). This used to be `all(_("balance") in blocker ...)`, which
		# is a substring test against translated text and wrong in both
		# directions: on a locale where the standalone word does not appear inside
		# the whole-sentence translation, a manager with a valid reason is refused
		# a legitimate corporate city-ledger checkout; on one where it appears in
		# the disputed-folio sentence, a *disputed* folio walks out on the city
		# ledger. Arabic is a first-release requirement, so neither is theoretical.
		#
		# `BLOCKER_FOLIO_DISPUTED` is deliberately absent from this set. The city
		# ledger moves a balance to accounts receivable; it is not a way to leave
		# with a dispute unresolved.
		outstanding_only = bool(summary["blocker_kinds"]) and all(
			kind in (BLOCKER_OUTSTANDING_BALANCE, BLOCKER_RELATED_FOLIO_BALANCE)
			for kind in summary["blocker_kinds"]
		)

		if not (allow_open_balance and outstanding_only):
			throw(
				_("This stay cannot be checked out: {0}").format(" ".join(summary["blockers"])),
				exc=FolioError,
			)

		require_role(REVERSAL_ROLES)

		if not reason or not reason.strip():
			throw(_("A reason is required to check out with an open balance."))

	lock_document(folio_service.FOLIO_DOCTYPE, folio)

	posting_result = None
	if post_to_erp:
		posting_result = _post_to_erp(folio)

	_settle_folio(folio, allow_open_balance=allow_open_balance)

	# --- operational release -------------------------------------------
	frappe.db.set_value(
		STAY_DOCTYPE,
		stay,
		{
			"stay_status": stay_service.CHECKED_OUT,
			"checked_out_on": now_datetime(),
			"checked_out_by": frappe.session.user,
		},
		update_modified=True,
	)

	if doc.room:
		room_service.mark_checked_out(
			doc.room, reference_doctype=STAY_DOCTYPE, reference_name=stay
		)
	# The room label on the guest's Customer comes off (the till stops showing it).
	try:
		guest_service.sync_customer_room(doc.guest)
	except Exception:
		frappe.log_error(title="Guest Customer at check-out", message=frappe.get_traceback())

	housekeeping_task = _raise_housekeeping_task(doc)

	_advance_reservation(doc)

	return {
		"stay": stay,
		"folio": folio,
		"posting": posting_result,
		"housekeeping_task": housekeeping_task,
		"balance": flt(frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "balance"), 2),
	}


def _post_to_erp(folio: str) -> dict:
	"""Raise the invoice and the payment entries for a folio."""
	result = {"invoice": None, "payments": []}

	folio_doc = frappe.get_doc(folio_service.FOLIO_DOCTYPE, folio)

	if [row for row in folio_doc.charges if not row.is_posted_to_erp]:
		result["invoice"] = posting_service.post_folio_invoice(folio)

	for row in folio_doc.payments:
		if not row.is_posted_to_erp:
			result["payments"].append(posting_service.post_folio_payment(folio, row.name))

	return result


def _settle_folio(folio: str, *, allow_open_balance: bool = False):
	"""Walk the folio to Settled and Closed.

	The transitions are taken one at a time through FolioService so the state
	machine and its audit log see every step, rather than jumping straight to
	Closed.
	"""
	status = frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "folio_status")

	if status in (folio_service.OPEN, folio_service.UNDER_REVIEW, folio_service.DISPUTED):
		if status != folio_service.UNDER_REVIEW:
			folio_service.transition(folio, folio_service.UNDER_REVIEW, reason=_("Checkout review"))
		folio_service.transition(folio, folio_service.READY, reason=_("Checkout"))
		status = folio_service.READY

	balance = flt(frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "balance"))

	if abs(balance) > 0.005:
		if not allow_open_balance:
			throw(
				_("Folio {0} still has a balance of {1}.").format(folio, flt(balance, 2)),
				exc=FolioError,
			)

		# The balance moves to the city ledger: the folio stays open for
		# finance rather than being falsely marked settled.
		folio_service._log(
			folio,
			frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "property"),
			"Checked out with open balance",
			amount=balance,
		)
		return

	if status != folio_service.SETTLED:
		folio_service.transition(folio, folio_service.SETTLED, reason=_("Checkout settlement"))

	folio_service.transition(folio, folio_service.CLOSED, reason=_("Checkout"))


def _raise_housekeeping_task(stay_doc) -> str | None:
	"""Create the cleaning task a checkout generates (SAS section 3.9).

	The Housekeeping Task DocType arrives in HPMS-0.18.0. Until then the room
	is left Vacant Dirty, which is the state housekeeping picks up from, and
	this returns None rather than pretending a task exists.
	"""
	if not frappe.db.table_exists("Housekeeping Task"):
		return None

	if not frappe.db.get_single_value(
		"PMS Settings", "auto_create_housekeeping_task_on_checkout"
	):
		return None

	if not stay_doc.room:
		return None

	# Delegated rather than built here: HousekeepingService owns the defaults
	# (scheduled date, inspection requirement, credits) and the deduplication
	# that stops a retried checkout queueing the same room twice.
	from hospitality_pms.services import housekeeping as housekeeping_service

	return housekeeping_service.create_task(
		stay_doc.property,
		stay_doc.room,
		task_type="Departure Clean",
		priority="High",
		source_stay=stay_doc.name,
	)


def _advance_reservation(stay_doc):
	"""Move the reservation to Checked Out once every stay on it has left."""
	if not stay_doc.reservation:
		return

	open_stays = frappe.db.count(
		STAY_DOCTYPE,
		{
			"reservation": stay_doc.reservation,
			"stay_status": ("in", (stay_service.IN_HOUSE, stay_service.DUE_OUT)),
		},
	)

	if open_stays:
		return

	reservation_doc = frappe.get_doc(
		reservation_service.RESERVATION_DOCTYPE, stay_doc.reservation
	)

	if reservation_doc.reservation_status != reservation_service.CHECKED_IN:
		return

	reservation_service._transition(
		reservation_doc, reservation_service.CHECKED_OUT, reason=_("Guest checked out")
	)

	frappe.db.set_value(
		reservation_service.RESERVATION_DOCTYPE,
		stay_doc.reservation,
		"checked_out_on",
		now_datetime(),
		update_modified=False,
	)


def _restore_reservation_after_reversal(stay_doc):
	"""Undo the reservation advance a checkout made, when a checkout is reversed.

	Only acts when the reservation is actually Checked Out - if another stay on
	the booking is still in house, `_advance_reservation` never moved it and there
	is nothing to restore. The transition propagates Checked In down to every room
	line, which is what returns the line to the availability sold-count.
	"""
	if not stay_doc.reservation:
		return

	status = frappe.db.get_value(
		reservation_service.RESERVATION_DOCTYPE, stay_doc.reservation, "reservation_status"
	)

	if status != reservation_service.CHECKED_OUT:
		return

	reservation_doc = frappe.get_doc(
		reservation_service.RESERVATION_DOCTYPE, stay_doc.reservation
	)

	reservation_service._transition(
		reservation_doc, reservation_service.CHECKED_IN, reason=_("Checkout reversed")
	)

	frappe.db.set_value(
		reservation_service.RESERVATION_DOCTYPE,
		stay_doc.reservation,
		"checked_out_on",
		None,
		update_modified=False,
	)


def reverse_checkout(stay: str, reason: str) -> dict:
	"""Undo a checkout.

	A genuine exception: it re-opens a closed folio and, if an invoice was
	raised, leaves it standing. The invoice is deliberately NOT cancelled here
	- cancelling a submitted ERPNext invoice is a finance decision with its own
	approval, and doing it silently would break the ledger the hotel reports
	from. The reversal is recorded so finance can act on it.
	"""
	require_role(REVERSAL_ROLES)

	if not reason or not reason.strip():
		throw(_("A reason is required to reverse a checkout."))

	doc = lock_and_get_doc(STAY_DOCTYPE, stay)

	if doc.stay_status != stay_service.CHECKED_OUT:
		throw(
			_("Stay {0} is {1}; only a checked out stay can be reversed.").format(
				stay, _(doc.stay_status)
			),
			exc=InvalidStateTransitionError,
		)

	folio = doc.folio or folio_service.get_folio_for_stay(stay)

	if folio:
		status = frappe.db.get_value(folio_service.FOLIO_DOCTYPE, folio, "folio_status")
		if status in (folio_service.CLOSED, folio_service.SETTLED):
			folio_service.transition(
				folio, folio_service.UNDER_REVIEW, reason=_("Checkout reversed: {0}").format(reason.strip())
			)

	# Physical-room authority has to agree before the guest is put back. A
	# checkout releases the room to Vacant Dirty; it can be cleaned and re-let to
	# a walk-in before anyone reverses the checkout. Re-occupying it blindly is
	# exactly how one room ends up with two active stays - and how that night's
	# room charge is posted to two folios at the next audit. Refuse the reversal
	# if another active stay now holds the room, under the room's lock, with the
	# same commit-time guard every placement path uses.
	if doc.room:
		lock_document(room_service.ROOM_DOCTYPE, doc.room)
		room_service.assert_room_unoccupied(
			doc.room, exclude_stay=stay, property_name=doc.property
		)

	stay_service.transition(stay, stay_service.IN_HOUSE, reason=reason.strip())

	# `checked_out_on` is load-bearing for occupancy authority: `active_stay_in_room`
	# (and every write-time placement guard through it) excludes any stay that
	# carries the timestamp. check_out stamped it; a reversal that leaves it set
	# makes the reversed stay invisible to the authority, so the room reads
	# assignable while the guest is back in it (the room-402 double-occupancy
	# class). Clearing it is what makes the reversal a true inverse of check_out.
	frappe.db.set_value(
		STAY_DOCTYPE,
		stay,
		{"checked_out_on": None, "checked_out_by": None},
		update_modified=True,
	)

	if doc.room:
		room_service.mark_occupied(
			doc.room, reference_doctype=STAY_DOCTYPE, reference_name=stay
		)

	# The checkout advanced the reservation to Checked Out once its last stay left
	# (_advance_reservation) and propagated that onto every room line. A line in
	# Checked Out is invisible to the availability sold-count, so a reversed stay
	# whose reservation is not restored drops out of inventory and the room can be
	# oversold. Put it back to Checked In - a holding state - so the night the
	# guest is once again occupying is counted again.
	_restore_reservation_after_reversal(doc)

	stay_service.add_note(
		stay, _("Checkout reversed: {0}").format(reason.strip()), note_type="Operational"
	)

	# Through `folio_erp_documents` rather than a hand-rolled copy of the same
	# query. The local version asked the posting log which invoices were Posted and
	# reported every one of them as standing - so a folio whose invoices finance had
	# already cancelled was reversed with a note telling finance to go and handle
	# two documents they had withdrawn themselves. It also missed Reconciled rows,
	# which are Posted rows somebody has since signed off.
	#
	# Both lists are returned, because they call for opposite actions: a standing
	# invoice needs a credit note or a cancellation, a cancelled one needs the PMS
	# side reconciling to it.
	# One log read, partitioned here. Asking `folio_erp_documents` twice would run
	# the same query twice and pay the liveness lookups on the second pass as well.
	invoiced = posting_service.folio_erp_documents(folio, "Sales Invoice", live_only=False)
	standing = [
		name for name in invoiced if posting_service._erp_document_is_live("Sales Invoice", name)
	]
	cancelled = [name for name in invoiced if name not in standing]

	note = _("Any submitted invoice is left standing and must be handled by finance.")

	if cancelled:
		# "not in the ledger", not "cancelled": a draft and a deleted document are
		# both counted here too, and neither has ever been in it. The key keeps the
		# name `cancelled_invoices` because cancellation is overwhelmingly what puts
		# a document in this list, but the sentence an operator reads must be true of
		# all three.
		note = _(
			"Any submitted invoice is left standing and must be handled by finance. "
			"{0} invoice(s) this folio posted are not in the ledger and need "
			"reconciliation."
		).format(len(cancelled))

	return {
		"stay": stay,
		"folio": folio,
		"standing_invoices": standing,
		"cancelled_invoices": cancelled,
		"needs_erp_reconciliation": bool(cancelled),
		"note": note,
	}
