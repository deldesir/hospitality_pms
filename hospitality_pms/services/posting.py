"""ERPNext financial posting.

This is the boundary between the operational subledger and the system of record.
ERPNext owns the ledger (HPMS-DEC-002); the folio is what the hotel works with
during a stay. Posting turns the second into the first, once.

The contract every function here keeps:

**Idempotent.** Each posting carries a key derived from what it represents, not
from when it ran. For an invoice that is the *set of charge rows being posted*,
fingerprinted - so retrying the same batch reuses the same invoice, while a
charge added later forms a different batch and gets a supplementary invoice of
its own. The key is `unique` on the posting log, so two concurrent attempts
cannot both create a document.

**Faithful.** What ERPNext ends up holding is what the folio says. The invoice's
grand total is checked against the batch before it is submitted, and a payment
that settles a bill says which bill it settles. Neither the tax nor the
allocation is re-derived here: the folio is the guest's bill and posting's job
is to carry it across, not to recompute it and hope the two agree.

**Traceable.** Every attempt writes a Financial Posting Log row,
including failures, with the payload that was sent. A finance user must be able
to answer "what did we send, when, and what came back" without reading code.

**Retryable where safe.** A failed posting stays Failed with its error and can
be retried under the same key. A succeeded posting is never re-sent.

Not solved here: a failure recorded by `_mark_failed` still lives in the
caller's transaction, so a rollback takes the Failed row with it (P1-14). That
needs one durability mechanism chosen across every external side effect and
belongs to the wave that decides it.
"""

import hashlib
import json
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate

from hospitality_pms.services import durability
from hospitality_pms.services.base import lock_and_get_doc, lock_and_read, require_role
from hospitality_pms.services.exceptions import (
	ConfigurationError,
	PostingError,
	ReconciliationError,
	throw,
)
from hospitality_pms.services.guests import ensure_customer
from hospitality_pms.services.property import (
	get_business_date,
	get_company,
	get_property,
	require_property_access,
)
from hospitality_pms.setup.posting_service import POSTING_SERVICE_USER

POSTING_LOG = "Financial Posting Log"
PROFILE_DOCTYPE = "Posting Profile"
FOLIO_DOCTYPE = "Guest Folio"

PENDING = "Pending"
POSTED = "Posted"
FAILED = "Failed"
CANCELLED = "Cancelled"
RECONCILED = "Reconciled"

#: Folio payment methods mapped onto ERPNext Mode of Payment names. A site may
#: rename these, so the mapping falls back to the raw name and the posting log
#: records what was actually used.
PAYMENT_MODE_MAP = {
	"Cash": "Cash",
	"Credit Card": "Credit Card",
	"Debit Card": "Credit Card",
	"Bank Transfer": "Bank Draft",
	"Cheque": "Cheque",
	"Online Gateway": "Credit Card",
}

#: How many ERP document names one finality query asks about. The close-time
#: check reads in batches rather than a row at a time, so the number of
#: statements is bounded by the number of DocTypes involved and not by the size
#: of the night's population (16.7.5-R1F).
FINALITY_BATCH = 200

#: Withdrawing the PMS's claim on a posting is a finance decision about the
#: ledger, not an operational one. Deliberately narrower than
#: `RECONCILIATION_ROLES`: a Night Auditor may *see* that a posting has gone
#: stale and must not be the one who writes off the claim.
WITHDRAWAL_ROLES = (
	"Finance Manager",
	"Hospitality Administrator",
	"System Manager",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def get_posting_profile(property_name: str) -> str:
	"""The active posting profile for a property.

	Refuses rather than guessing: posting to a default account nobody chose is
	how a hotel's revenue ends up in the wrong place for a month.
	"""
	profile = frappe.db.get_value(
		PROFILE_DOCTYPE, {"property": property_name, "is_active": 1}, "name"
	)

	if not profile:
		throw(
			_("Property {0} has no active posting profile. Configure one before posting.").format(
				property_name
			),
			exc=ConfigurationError,
		)

	return profile


def resolve_charge_item(profile: str, charge_type: str) -> dict:
	"""The ERPNext item and accounts a folio charge type posts as."""
	profile_doc = frappe.get_cached_doc(PROFILE_DOCTYPE, profile)

	for row in profile_doc.charge_items:
		if row.charge_type == charge_type and row.is_active:
			return {
				"item": row.item,
				"income_account": row.income_account or profile_doc.default_income_account,
				"cost_center": row.cost_center or profile_doc.default_cost_center,
				"tax_template": row.tax_template or profile_doc.default_tax_template,
			}

	if not profile_doc.default_item:
		throw(
			_("Charge type {0} is not mapped to an item, and the posting profile has no default.").format(
				_(charge_type)
			),
			exc=ConfigurationError,
		)

	return {
		"item": profile_doc.default_item,
		"income_account": profile_doc.default_income_account,
		"cost_center": profile_doc.default_cost_center,
		"tax_template": profile_doc.default_tax_template,
	}


# ---------------------------------------------------------------------------
# The posting log
# ---------------------------------------------------------------------------


def _erp_document_is_live(erp_doctype: str | None, erp_document: str | None) -> bool:
	"""Whether the ERP document a posting log row names is still in the ledger.

	A Posted row records what posting did *at the time*. Nothing propagates an
	ERPNext cancellation back into the PMS - there is no `on_cancel` hook and no
	code in this app cancels an ERP document at all, because
	`checkout.reverse_checkout` deliberately leaves a submitted invoice standing
	for finance to decide on. So finance cancelling an invoice out of band is
	*expected* operation, and the log alone cannot answer whether the ledger
	still holds the posting it describes.

	It could not even ask: `_claim` used to read `name` and `posting_status` and
	nothing else, so a Posted row against a cancelled invoice was indistinguishable
	from a healthy one. That is what let a folio with 820 of charges outside the
	ledger be told, by every route it has, that it was already invoiced.

	`docstatus == 1`, not `!= 2`: a draft has never entered the ledger and a
	deleted document reads `None`, and neither is evidence that anything posted.
	The same predicate `_submitted_invoices` already applies, kept as one
	definition of "in the ledger" for the whole module.
	"""
	if not erp_doctype or not erp_document:
		return False

	# `ignore=True` because `erp_doctype` is *stored data* - a Link to DocType that
	# `_mark_posted` wrote at posting time - and a DocType renamed or removed by a
	# later patch leaves rows naming the old one. Without it `get_value` raises
	# ProgrammingError(1146) on the missing table, and this predicate is called per
	# posting row inside `reconcile_folio`, which `night_audit.reconcile` runs over
	# every folio on the property: one unreadable row would take down the whole
	# day's reconciliation. `services/base.py`'s `may_read_doctype` fails closed on
	# a stored DocType name for the same reason, citing this bench's own rename
	# patch. Missing table, missing column and "Invalid DocType" are all absorbed
	# (`frappe/database/database.py:682-691`), and absorbed to `None` - which is not
	# `1`, so the row reads as not live. Failing closed here means "no evidence the
	# ledger holds this", which is the safe direction.
	return frappe.db.get_value(erp_doctype, erp_document, "docstatus", ignore=True) == 1


def _stale_rows(rows: list[dict]) -> list[dict]:
	"""Which of these posting-log rows claim a posting the ledger does not hold.

	Takes rows rather than a folio so the two callers can each fetch them the way
	that suits: `reconcile_folio` already reads the log once for its failure list
	and partitions that, while `post_folio_invoice` asks only on the branch where
	it has nothing to post. One definition of the predicate either way.
	"""
	return [
		row
		for row in rows
		if row["posting_status"] in (POSTED, RECONCILED)
		and not _erp_document_is_live(row["erp_doctype"], row["erp_document"])
	]


# ---------------------------------------------------------------------------
# Finality (16.7.5-R1F)
# ---------------------------------------------------------------------------
#
# `_erp_document_is_live` answers "is this document in the ledger", one document
# at a time, from whatever snapshot the caller's transaction is holding. That is
# the right shape for reconciliation, which is reading a folio it has just
# loaded anyway.
#
# It is the wrong shape for the Night Audit's close. Close needs a *current*
# answer about a *set* of documents, taken once, immediately before the business
# date moves - and it must not pay a locking round trip per row to get it, which
# is what R1E's reviewers refused. So the set is read in batches grouped by
# DocType, in a deterministic order, and the whole check is over before the date
# advances.
#
# It also needs a stronger question than liveness. A credit note is a Sales
# Invoice with `is_return` set and `return_against` naming the original
# (`sales_and_purchase_return.py:461-464`), with negated quantities, so its
# `grand_total` is negative. Where its receivable lands depends on
# `update_outstanding_for_self`: unset, ERPNext books the GL entry against
# `return_against` (`sales_invoice.py:1673-1676`); set, against the credit note
# itself. And ERPNext *forces* it set whenever the return exceeds what the
# original still owes (`accounts_controller.py:220-223`) - which for a hotel
# folio is the ordinary case, not the exception, because the folio was settled
# at checkout and the invoice's outstanding is already zero.
#
# Either way the original keeps `docstatus == 1` while the money it charged has
# been credited back, and either way the return carries `return_against`. So the
# check keys on `return_against`, which survives the flip, and `docstatus == 1`
# alone is not proof that an invoice still stands for what it stood for when
# reconciliation accepted it.


def submitted_returns(invoices: list[str], *, current: bool = False) -> list[dict]:
	"""The submitted credit notes raised against these Sales Invoices.

	Only submitted returns count - a draft credit note has reversed nothing,
	exactly as a draft invoice has posted nothing.

	Read in batches with a deterministic order. A folio's invoices are usually
	one or two, but a night's population is not, and this is asked once for the
	whole set rather than once per document.

	`current=True` takes the read under a lock, and `close` needs it. A plain
	`SELECT` is answered from the snapshot the transaction opened at its first
	read, so a credit note committed after `close` started but before it checked
	would be invisible - and the invoice it credits stays `docstatus == 1`, so
	the liveness arm would not catch it either. Locking is the only version of
	this read that can see it (N1, `base.lock_and_read`).
	"""
	if not invoices:
		return []

	rows: list[dict] = []

	for chunk in _chunks(sorted(set(invoices))):
		rows.extend(
			frappe.db.get_values(
				"Sales Invoice",
				{"return_against": ("in", chunk), "docstatus": 1, "is_return": 1},
				["name", "return_against", "grand_total"],
				as_dict=True,
				order_by="name asc",
				for_update=bool(current),
			)
			or []
		)

	return rows


def submitted_returns_against(invoices: list[str], *, current: bool = False) -> dict[str, float]:
	"""What each of these Sales Invoices has had credited back against it.

	The signed total, because that is what ERPNext stores: a return's
	`grand_total` is negative.
	"""
	totals: dict[str, float] = {name: 0.0 for name in invoices}

	for row in submitted_returns(invoices, current=current):
		totals[row["return_against"]] = flt(totals.get(row["return_against"], 0.0)) + flt(
			row["grand_total"]
		)

	return totals


def posting_rows_for_finality(
	property_name: str, *, since, folios: list[str] | None = None
) -> list[dict]:
	"""Every posting whose ERP document this audit period is accountable for.

	Two clauses, mirroring `night_audit.reconciliation_population` - and
	deliberately *not* joined to `Guest Folio`:

	* postings dated into this audit window, whatever the folio behind them is
	  doing now. This is the clause that closes 16.7.5-R1F. The window is the
	  posting log's own `business_date`, which is stamped when the posting is
	  claimed and never revised, so a folio moving to Under Review afterwards
	  cannot take its accounting out of the set;
	* postings belonging to the folios reconciliation examined, whatever date
	  they carry. The safety net, so an older document supporting a folio still
	  under examination is checked too.

	Paginated on `name` to exhaustion, keyed on the primary key rather than on
	`modified`, for the reason `reconciliation_population` is: a row changing
	underneath the cursor must not be able to skip past it.
	"""
	rows: dict[str, dict] = {}

	for page in _finality_pages(property_name, since=since):
		for row in page:
			rows[row["name"]] = row

	for chunk in _chunks(sorted(set(folios or []))):
		for row in frappe.get_all(
			POSTING_LOG,
			filters={
				"property": property_name,
				"folio": ("in", chunk),
				"posting_status": ("in", (POSTED, RECONCILED)),
				"erp_document": ("is", "set"),
			},
			fields=_FINALITY_FIELDS,
			order_by="name asc",
		):
			rows[row["name"]] = row

	return [rows[name] for name in sorted(rows)]


#: What a finality row needs to know about itself. `amount` is carried so the
#: evidence a close refused on can be read without re-joining the log.
_FINALITY_FIELDS = [
	"name",
	"folio",
	"posting_type",
	"posting_status",
	"erp_doctype",
	"erp_document",
	"amount",
]


def _finality_pages(property_name: str, *, since):
	"""The window clause, paginated on `name`."""
	cursor = ""

	while True:
		page = frappe.get_all(
			POSTING_LOG,
			filters={
				"property": property_name,
				"business_date": (">=", since),
				"posting_status": ("in", (POSTED, RECONCILED)),
				"erp_document": ("is", "set"),
				"name": (">", cursor),
			},
			fields=_FINALITY_FIELDS,
			order_by="name asc",
			limit=FINALITY_BATCH,
		)

		if not page:
			return

		yield page
		cursor = page[-1]["name"]


def _chunks(values: list[str], size: int = FINALITY_BATCH):
	for start in range(0, len(values), size):
		yield values[start : start + size]


def finality_evidence(rows: list[dict], *, accepted_folios: set[str] | None = None) -> list[dict]:
	"""Turn posting-log rows into the evidence a close will be checked against.

	The snapshot is taken *here*, while reconciliation is looking at the ledger,
	so that close compares like with like rather than forming a second opinion.

	`accepted_folios` is what stops that snapshot laundering an existing credit
	note, and it is not an optimisation - it is the difference between a delta
	test and an absolute one.

	The liveness arm is absolute: `docstatus` must be 1, whenever it is asked. The
	credited-back arm can only be a delta, because a folio may legitimately have
	been credited and adjusted long before this audit ran, and refusing every one
	of those would refuse days that are correct. So the baseline is recorded as
	the ledger's current answer **only for folios this reconciliation actually
	examined and found to agree** - for those, the credit note is already
	accounted for in a variance of zero. For every other posting the baseline is
	zero, so any return at all reads as a break.

	Without that distinction the arm could never fire on a first run: the
	baseline would be derived from the same read it is compared against, and a
	folio moved out of the population - the exact escape R1F exists for - would
	have its credit note recorded as the accepted state and never questioned
	again.
	"""
	accepted = accepted_folios or set()
	invoices = [
		row["erp_document"] for row in rows if row["erp_doctype"] == "Sales Invoice" and row["erp_document"]
	]
	returned = submitted_returns_against(invoices)

	return [
		{
			"posting_log": row["name"],
			"folio": row["folio"],
			"posting_type": row["posting_type"],
			"erp_doctype": row["erp_doctype"],
			"erp_document": row["erp_document"],
			"amount": flt(row["amount"]),
			"returned_total": (
				flt(returned.get(row["erp_document"], 0.0)) if row["folio"] in accepted else 0.0
			),
		}
		for row in rows
	]


def finality_breaks(evidence: list[dict], *, current: bool = False) -> list[dict]:
	"""Which of the postings reconciliation accepted the ledger no longer supports.

	Every answer is one of four, and each is a refusal:

	* **withdrawn log row** - the claim itself is gone or unreadable. A row whose
	  status is now `Cancelled` is *not* a break: that is finance having
	  explicitly withdrawn the claim through `withdraw_stale_posting`, which is
	  the supported recovery, and the folio variance it leaves behind is what
	  then blocks the close until the money is put right;
	* **redirected log row** - the log now names a different document than the
	  one reconciliation accepted;
	* **not in the ledger** - cancelled, deleted, or never submitted;
	* **credited back** - still submitted, and reversed by a return raised since.

	`current=True` takes the read under a lock, which is what close does after it
	holds the audit and the property. Everything else reads the ordinary way.
	"""
	if not evidence:
		return []

	breaks: list[dict] = []
	claimed = _current_log_claims([row["posting_log"] for row in evidence])
	live: list[dict] = []

	# Memoised per folio, not per evidence row. A folio with an invoice and two
	# payments withdrawn would otherwise reconcile three times, and this runs on
	# the close path while the audit and property rows are held.
	settled_folios: dict[str | None, bool] = {}

	for row in sorted(evidence, key=lambda item: item["posting_log"]):
		claim = claimed.get(row["posting_log"])

		if claim is None:
			breaks.append({**row, "reason": "log_missing"})
			continue

		if claim["posting_status"] == CANCELLED:
			# Withdrawn by finance. That records what happened; it does not undo
			# it. The money this posting carried is still not in the ledger, so
			# the withdrawal only stops being a break once the folio it belongs
			# to agrees with the ledger again.
			#
			# Skipping it outright was wrong, and wrong in the one direction that
			# matters: it made `withdraw_stale_posting` a way to close the day.
			# Cancel the invoice, withdraw the claim, close - no re-reconciliation,
			# no variance, and the charges still stamped against a document that
			# is gone.
			if row["folio"] not in settled_folios:
				settled_folios[row["folio"]] = _folio_agrees_with_ledger(row["folio"])

			if not settled_folios[row["folio"]]:
				breaks.append({**row, "reason": "withdrawn_unsettled"})

			continue

		if claim["erp_document"] != row["erp_document"] or (
			claim["erp_doctype"] or claim["posting_type"]
		) != row["erp_doctype"]:
			breaks.append({**row, "reason": "log_redirected"})
			continue

		live.append(row)

	gone = _documents_not_in_ledger(live, current=current)
	breaks.extend(gone)

	# Keyed off the *liveness* breaks only. Built from every break, a row already
	# refused for a missing or redirected log would have suppressed the returns
	# check on a second, healthy row naming the same invoice.
	missing = {(row["erp_doctype"], row["erp_document"]) for row in gone}
	standing = [
		row
		for row in live
		if row["erp_doctype"] == "Sales Invoice"
		and (row["erp_doctype"], row["erp_document"]) not in missing
	]

	breaks.extend(_documents_credited_back(standing, current=current))

	return breaks


def _folio_agrees_with_ledger(folio: str | None) -> bool:
	"""Whether this folio and ERPNext currently say the same thing.

	Asked only about folios carrying a withdrawn claim, which are rare and
	exceptional by construction, so the cost of a full `reconcile_folio` is paid
	on a handful of rows rather than on the night's population.

	A posting with no folio behind it cannot be settled by settling one, so it
	is never treated as resolved.
	"""
	if not folio:
		return False

	return bool(reconcile_folio(folio)["is_reconciled"])


def _current_log_claims(logs: list[str]) -> dict[str, dict]:
	"""What the posting log says about these rows *now*, read in batches."""
	claims: dict[str, dict] = {}

	for chunk in _chunks(sorted(set(logs))):
		for row in frappe.get_all(
			POSTING_LOG,
			filters={"name": ("in", chunk)},
			fields=["name", "posting_status", "posting_type", "erp_doctype", "erp_document"],
			order_by="name asc",
		):
			claims[row["name"]] = row

	return claims


def _documents_not_in_ledger(rows: list[dict], *, current: bool) -> list[dict]:
	"""Batched `docstatus` read, grouped by DocType, in a deterministic order.

	One statement per DocType per batch, rather than the per-row `FOR UPDATE`
	R1E was refused for. The ordering - DocTypes alphabetically, names within a
	DocType alphabetically - is what keeps two concurrent closes from taking the
	same locks in opposite orders.
	"""
	grouped: dict[str, set[str]] = {}

	for row in rows:
		if not row["erp_doctype"] or not row["erp_document"]:
			continue

		grouped.setdefault(row["erp_doctype"], set()).add(row["erp_document"])

	submitted: set[tuple[str, str]] = set()

	for doctype in sorted(grouped):
		for chunk in _chunks(sorted(grouped[doctype])):
			# `ignore=True` for the reason `_erp_document_is_live` gives: `erp_doctype`
			# is stored data, and a DocType a later patch renamed or removed leaves rows
			# naming the old one. The read comes back `None`, nothing joins `submitted`,
			# and every row against that DocType reads as not in the ledger - failing
			# closed, which is the safe direction for a gate on the business date.
			found = (
				frappe.db.get_values(
					doctype,
					{"name": ("in", chunk)},
					["name", "docstatus"],
					as_dict=True,
					order_by="name asc",
					for_update=bool(current),
					ignore=True,
				)
				or []
			)

			submitted.update(
				(doctype, found_row["name"]) for found_row in found if found_row["docstatus"] == 1
			)

	return [
		{**row, "reason": "not_in_ledger"}
		for row in rows
		if row["erp_doctype"]
		and (row["erp_doctype"], row["erp_document"]) not in submitted
	]


def _documents_credited_back(rows: list[dict], *, current: bool = False) -> list[dict]:
	"""Invoices still submitted, but reversed by a return raised since.

	The case `docstatus` cannot see.

	**More credited than the baseline, not merely different from it.** Returns
	are stored negative, so the test is `<`. Inequality was wrong in a way that
	had no exit: a credit note raised in error and then *cancelled* moves the
	total back toward zero, which is the ledger becoming more correct, and it
	refused the close for it. Reconciling again could not clear it, because the
	baseline is written once; `withdraw_stale_posting` could not, because the
	invoice is live again and no longer credited; and `reopen` needs a closed
	audit. The business date would have been stuck permanently.
	"""
	if not rows:
		return []

	precision = frappe.get_precision("Sales Invoice", "grand_total") or 2
	now = submitted_returns_against([row["erp_document"] for row in rows], current=current)

	return [
		{**row, "reason": "credited_back", "returned_now": flt(now.get(row["erp_document"], 0.0), precision)}
		for row in rows
		if flt(now.get(row["erp_document"], 0.0), precision) < flt(row["returned_total"], precision)
	]


def withdraw_stale_posting(log: str, reason: str) -> dict:
	"""Withdraw the PMS's claim that a posting is in the ledger. Finance only.

	The supported recovery for the condition every guard in this module refuses
	on. Without it those guards are a trap: `mark_reconciled` will not stamp a
	row whose document has left the ledger, `retry_posting` will not re-send it,
	`post_folio_invoice` will not raise a replacement while the folio's rows are
	still stamped, and the Night Audit will not close over it - so "re-run
	reconciliation", which is what R1E told finance to do, could not have worked.

	What it does is deliberately small, and deliberately not a repair:

	* it refuses a posting whose document *is* still in the ledger. This is not
	  a way to disown a live invoice;
	* it moves the log row to `Cancelled` - the status this module already means
	  "this row claims nothing" - and records who withdrew it and why;
	* it leaves the folio's charge rows stamped and ERPNext untouched.

	It therefore does not make the folio agree with the ledger, and must not:
	the money that invoice carried is genuinely not in ERPNext any more, and
	after the withdrawal `reconcile_folio` says so as a plain variance instead of
	a stale posting.

	**And the Night Audit keeps refusing until it does.** `finality_breaks` does
	not treat a withdrawn claim as resolved; it asks whether the folio behind it
	now agrees with the ledger, and only then lets it pass. Without that, this
	function was a close button: cancel the invoice, withdraw the claim, close -
	no re-reconciliation, no variance, and the charges still stamped against a
	document that had gone. Recording that money left the ledger is not the same
	as putting it back, and only one of the two entitles a business date to move.
	"""
	require_role(WITHDRAWAL_ROLES)

	if not reason or not reason.strip():
		throw(_("A reason is required to withdraw a posting."), exc=PostingError)

	entry = lock_and_read(
		POSTING_LOG,
		log,
		["name", "property", "folio", "posting_type", "posting_status", "erp_doctype", "erp_document", "amount"],
	)

	if not entry:
		throw(_("Posting {0} does not exist.").format(log), exc=PostingError)

	require_property_access(entry["property"])

	if entry["posting_status"] not in (POSTED, RECONCILED):
		throw(
			_("Posting {0} does not claim an accounting document, so there is nothing to withdraw.").format(
				log
			),
			exc=PostingError,
		)

	# Two conditions qualify, and they are the two `finality_breaks` refuses on.
	# The document having left the ledger is the obvious one. The second is a
	# Sales Invoice that is still submitted and has been credited back: ERPNext
	# leaves the original standing and books the reversal against it, so the
	# claim is just as empty and there is no cancellation to point at. Without
	# this second case a credit-noted folio had no way out at all.
	credited = (
		flt(submitted_returns_against([entry["erp_document"]]).get(entry["erp_document"]))
		if entry["erp_doctype"] == "Sales Invoice"
		else 0.0
	)

	if _erp_document_is_live(entry["erp_doctype"], entry["erp_document"]) and not credited:
		throw(
			_(
				"Posting {0} names a document that is still in the ledger and has not been "
				"credited back. A posting that is live cannot be withdrawn."
			).format(log),
			exc=PostingError,
		)

	frappe.db.set_value(
		POSTING_LOG,
		log,
		{
			"posting_status": CANCELLED,
			"error_message": _("Withdrawn by finance: {0}").format(reason.strip()),
		},
		update_modified=True,
	)

	_note_withdrawal(log, reason.strip())

	if entry["folio"]:
		# The folio's own audit trail, through the helper that already writes it.
		# A withdrawal is a decision about that folio's money and belongs beside
		# the reversals and adjustments, not only in a technical log.
		from hospitality_pms.services import folio as folio_service

		folio_service._log(
			entry["folio"],
			entry["property"],
			"Posting claim withdrawn",
			amount=flt(entry["amount"]),
			reason=reason.strip(),
			details={"posting_log": log, "posting_type": entry["posting_type"]},
		)

	return {
		"log": log,
		"folio": entry["folio"],
		"posting_status": CANCELLED,
		"withdrawn_by": frappe.session.user,
	}


#: The durable posting operations a stranded Night Audit may re-arm. Both are
#: SAFE_RETRY in retry.HANDLERS - their failure mode is an explicit refusal
#: (e.g. a tax template that was missing at build time), so nothing reached
#: ERPNext and there is no side effect to double. RECONCILE_FIRST money
#: operations are deliberately excluded: a re-arm of one of those would be a
#: licence to move money twice, which is what reconciliation exists to prevent.
REARMABLE_POSTING_OPERATIONS = {
	"post_folio_invoice": "Sales Invoice",
	"post_folio_payment": "Payment Entry",
}


def rearm_abandoned_posting(operation_key: str, reason: str) -> dict:
	"""Re-arm an Abandoned folio-posting operation after its cause is fixed.

	The supported exit from F-NA1: a folio posting that failed on a fixable
	configuration error (the Minibar tax-template case is the live example)
	exhausts its five attempts and becomes Abandoned. `run_durably` then refuses
	the key for ever, `reschedule_now` will not revive a terminal row, and the
	Night Audit's `_assert_no_unresolved_posting_failures` blocks close on it -
	so correcting the configuration afterwards changes nothing and the day cannot
	close. Without this there is no operator path from "posting abandoned" to
	"day closes".

	It is deliberately narrow, and distinct from the deferred Finance Recovery:

	* finance only (`WITHDRAWAL_ROLES`), with a mandatory reason recorded on the
	  ledger row for the audit trail;
	* the property is re-checked from the operation's own record, so a key from
	  another property cannot be re-armed here;
	* only the two SAFE_RETRY folio postings qualify - never a money operation
	  whose outcome could be ambiguous;
	* it refuses if the operation names an ERP document that is still live, so it
	  can never be a way to post a second invoice for one that already exists.
	  For these operations the external side effect is a same-database document,
	  so its absence is authoritative (the F-DURA reasoning).

	After re-arming, the operation is dispatched once through its normal handler,
	so a corrected configuration posts immediately rather than waiting on the
	scheduler and leaving the auditor unsure whether it worked.
	"""
	require_role(WITHDRAWAL_ROLES)

	if not reason or not reason.strip():
		throw(_("A reason is required to re-arm a posting operation."), exc=PostingError)

	record = durability.get_operation(operation_key)

	if not record:
		throw(_("Operation {0} does not exist.").format(operation_key), exc=PostingError)

	require_property_access(record["property"])

	erp_doctype = REARMABLE_POSTING_OPERATIONS.get(record["operation"])
	if not erp_doctype:
		throw(
			_("Operation {0} is not a folio posting and cannot be re-armed here.").format(operation_key),
			exc=PostingError,
		)

	if record["queue_status"] != durability.ABANDONED:
		throw(
			_("Operation {0} is {1}, not Abandoned; only an abandoned posting is re-armed.").format(
				operation_key, record["queue_status"]
			),
			exc=PostingError,
		)

	# Side-effect ambiguity guard. A SAFE_RETRY posting that failed at build time
	# carries no external_reference, but if one is present and still live the work
	# did reach ERPNext and this is a reconciliation decision, not a re-arm.
	if record.get("external_reference") and _erp_document_is_live(
		erp_doctype, record["external_reference"]
	):
		throw(
			_(
				"Operation {0} names a {1} that is still in the ledger; it needs reconciliation, "
				"not a re-arm."
			).format(operation_key, erp_doctype),
			exc=ReconciliationError,
		)

	durability.rearm_operation(
		operation_key, reason=_("Re-armed by finance: {0}").format(reason.strip())
	)

	# Dispatch once now, through the same handler the scheduler uses, so the
	# corrected posting is attempted immediately under the caller's action.
	from hospitality_pms.services import retry as retry_service

	outcome = retry_service.dispatch(durability.get_operation(operation_key))

	return {
		"operation": operation_key,
		"status": outcome.get("status"),
		"rearmed_by": frappe.session.user,
	}


def _note_withdrawal(log: str, reason: str):
	"""Keep the withdrawal on the payload, beside `_authority`."""
	try:
		payload = json.loads(frappe.db.get_value(POSTING_LOG, log, "payload") or "{}")
	except (ValueError, TypeError):
		payload = {}

	if not isinstance(payload, dict):
		payload = {"payload": payload}

	payload["_withdrawal"] = {
		"withdrawn_by": frappe.session.user,
		"withdrawn_on": str(now_datetime()),
		"reason": reason,
	}

	frappe.db.set_value(
		POSTING_LOG, log, "payload", json.dumps(payload, default=str, indent=1), update_modified=False
	)


def _stale_posting_message(log: str) -> str:
	"""Why we are refusing - without naming the accounting document.

	The document's name is deliberately absent. This message travels to the browser
	in `_server_messages` and no endpoint catches it, so naming a Sales Invoice
	here would hand it to every caller who can reach a refusal - including
	`api/checkout.retry_posting`'s Night Auditor, Hotel Manager and General
	Manager, none of whom hold `Sales Invoice` read, and whose success path on that
	same endpoint exists *only* to withhold that name from them (16.7.5-R1B:
	permission on a root DocType does not authorise fields joined from another).
	An error message is not a lesser channel than a response body.

	Gating the message at the raise site would be wrong rather than merely
	insufficient: `_claim` is reached from inside `erp_posting_authority`, where the
	session user is the posting service identity, so a permission question asked
	here would be answered for the wrong user. Omitting the name is the only
	version that stays correct wherever the raise sites move.

	The posting log's own name is what finance acts on, and every role that can
	reach this refusal may read `Financial Posting Log`. The endpoint projections
	add the document name for callers permitted to see it.

	"is not in the ledger", not "is no longer in": `_erp_document_is_live` treats a
	draft and a deleted row as not live too, and neither has ever been in the
	ledger. The sentence has to be true of all three.
	"""
	return _(
		"Posting {0} is recorded as posted, but the accounting document it names is "
		"not in the ledger. This needs reconciliation before anything further is "
		"posted against it."
	).format(log)


def record_external_document(
	doc, posting_type: str, erp_document: str, amount: float, idempotency_key: str, *, actor: str | None = None
) -> str:
	"""Enter a document ERPNext already holds in the posting log as posted.

	A point-of-sale sale charged to the room, or a receipt taken at the till against it:
	ERPNext wrote the document, the folio only learns of it. Once logged, folio payments
	allocate against the invoice, checkout does not raise the receipt again, and the
	reconciliation counts both as this folio's. Nothing is sent to ERPNext.
	"""
	log, already_done = _claim(
		doc.property,
		posting_type,
		idempotency_key,
		folio=doc.name,
		stay=doc.stay,
		reservation=doc.reservation,
		guest=doc.guest,
		amount=amount,
		payload={"source": "pos", "document": erp_document},
	)
	if not already_done:
		_mark_posted(log, posting_type, erp_document, actor=actor)
	return log


def record_external_invoice(doc, invoice: str, amount: float, idempotency_key: str, *, actor: str | None = None) -> str:
	return record_external_document(doc, "Sales Invoice", invoice, amount, idempotency_key, actor=actor)


def cancel_external_posting(idempotency_key: str, reason: str) -> None:
	"""The ERPNext document behind an external posting is being cancelled: say so on the log."""
	log = frappe.db.get_value(POSTING_LOG, {"idempotency_key": idempotency_key}, "name")
	if log:
		frappe.db.set_value(POSTING_LOG, log, {"posting_status": CANCELLED, "error_message": reason}, update_modified=True)


cancel_external_invoice = cancel_external_posting


def _claim(
	property_name: str,
	posting_type: str,
	idempotency_key: str,
	*,
	folio: str | None = None,
	stay: str | None = None,
	reservation: str | None = None,
	guest: str | None = None,
	amount: float = 0,
	payload: dict | None = None,
) -> tuple[str, bool]:
	"""Claim the right to perform this posting.

	Returns `(log_name, already_done)`. The idempotency key is unique on the
	log, so this is what serialises two concurrent attempts: the loser gets a
	duplicate entry error, rolls back its savepoint and reads the winner's row.
	"""
	existing = frappe.db.get_value(
		POSTING_LOG,
		{"idempotency_key": idempotency_key},
		["name", "posting_status", "erp_doctype", "erp_document"],
		as_dict=True,
	)

	if existing:
		done = existing["posting_status"] in (POSTED, RECONCILED)

		# A Posted row whose document has left the ledger is not evidence that
		# the work was done, and it is not licence to do it again either. The
		# folio's rows are still stamped, the key is still claimed, and re-posting
		# under it would either double-post or post a batch that no longer matches
		# what the stamps say. So it stops here and says which document went.
		#
		# Deliberately a refusal rather than `already_done = False`: there is one
		# safe answer and it is "a human decides what happened".
		if done and not _erp_document_is_live(existing["erp_doctype"], existing["erp_document"]):
			throw(_stale_posting_message(existing["name"]), exc=ReconciliationError)

		return existing["name"], done

	doc = frappe.get_doc(
		{
			"doctype": POSTING_LOG,
			"property": property_name,
			"posting_type": posting_type,
			"posting_status": PENDING,
			"idempotency_key": idempotency_key,
			"folio": folio,
			"stay": stay,
			"reservation": reservation,
			"guest": guest,
			"amount": amount,
			"business_date": get_business_date(property_name),
			"attempts": 0,
			"payload": json.dumps(payload, default=str, indent=1) if payload else None,
		}
	).insert(ignore_permissions=True)

	return doc.name, False


def _mark_posted(log: str, erp_doctype: str, erp_document: str, *, actor: str | None = None):
	"""Record the posting, against the person who caused it.

	`actor` is the human whose hotel operation this was. It has to be passed
	in, because by the time an ERPNext document exists the session is running
	as the posting service identity (see `erp_posting_authority`) and
	`frappe.session.user` would name the service, not the front desk agent who
	checked the guest out.

	Both are kept: the human in `posted_by`, and the identity the ledger
	document was actually written under alongside it, so "who did this" and
	"what authority wrote it" are separately answerable.
	"""
	executed_as = frappe.session.user

	frappe.db.set_value(
		POSTING_LOG,
		log,
		{
			"posting_status": POSTED,
			"erp_doctype": erp_doctype,
			"erp_document": erp_document,
			"posted_on": now_datetime(),
			"posted_by": actor or executed_as,
			"error_message": None,
		},
		update_modified=True,
	)

	if actor and executed_as != actor:
		_note_service_execution(log, actor, executed_as)


def _note_service_execution(log: str, actor: str, executed_as: str):
	"""Leave the trust boundary visible on the posting log's payload.

	Written into the existing `payload` field rather than a new column: the
	posting log is the record of what was sent and under what authority, and
	a reader needs both names in one place.
	"""
	try:
		payload = json.loads(frappe.db.get_value(POSTING_LOG, log, "payload") or "{}")
	except (ValueError, TypeError):
		payload = {}

	if not isinstance(payload, dict):
		payload = {"payload": payload}

	payload["_authority"] = {"initiated_by": actor, "executed_as": executed_as}

	frappe.db.set_value(
		POSTING_LOG, log, "payload", json.dumps(payload, default=str, indent=1), update_modified=False
	)


def _mark_failed(log: str, error: str):
	attempts = flt(frappe.db.get_value(POSTING_LOG, log, "attempts")) + 1

	frappe.db.set_value(
		POSTING_LOG,
		log,
		{
			"posting_status": FAILED,
			"attempts": attempts,
			"last_attempt_on": now_datetime(),
			"error_message": error,
		},
		update_modified=True,
	)


def get_posting(idempotency_key: str) -> dict | None:
	"""The posting log row for a key, if it exists."""
	return frappe.db.get_value(
		POSTING_LOG,
		{"idempotency_key": idempotency_key},
		["name", "posting_status", "erp_doctype", "erp_document", "amount", "error_message"],
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Sales Invoice
# ---------------------------------------------------------------------------


def _invoice_batch_key(folio: str, rows) -> str:
	"""The identity of one immutable set of charge rows.

	`folio-invoice:{folio}` was the key for the entire life of the folio, which
	made the first invoice the only invoice: once it existed, every later call
	returned it as a duplicate and an authorised late charge could never reach
	ERPNext at all (P2-4).

	Identity belongs to *what is being posted*, so it is derived from the row
	names themselves. Sorted, so the order rows happen to come back in cannot
	change it; hashed, so the key fits the column however many rows there are.

	Deliberately not a counter. A counter would have to be allocated, and two
	concurrent retries of the same batch could allocate two different numbers
	and post the same charges twice. A fingerprint of the rows needs no
	allocation: the same rows always produce the same key, from any process, at
	any time.
	"""
	fingerprint = hashlib.sha1("|".join(sorted(row.name for row in rows)).encode()).hexdigest()

	return f"folio-invoice:{folio}:{fingerprint[:16]}"


def _latest_invoice_posting(folio: str) -> dict | None:
	"""The most recent successful invoice posting for a folio."""
	rows = frappe.get_all(
		POSTING_LOG,
		filters={
			"folio": folio,
			"posting_type": "Sales Invoice",
			"posting_status": ("in", (POSTED, RECONCILED)),
		},
		fields=["name", "posting_status", "erp_doctype", "erp_document", "amount", "error_message"],
		order_by="creation desc",
		limit=1,
	)

	return rows[0] if rows else None


def _run_durable_posting(erp_doctype: str, idempotency_key: str, run_kwargs: dict):
	"""run_durably for a same-database ERP posting, healing a stale phantom resolve.

	Returns `(durable_result, is_duplicate)`.

	`run_durably` was built for a genuinely external side effect - a payment
	gateway that keeps its record when our transaction rolls back. An ERPNext
	document is not that: it is created on the caller's own connection, so it
	rolls back with the caller while the durable ledger row, committed on a second
	connection, survives. When that happens the ledger says Resolved for an invoice
	or payment that no longer exists, and the old code returned it as a duplicate -
	a phantom naming a document that was never committed. The folio's charges then
	stay unstamped, so it can never invoice or close, and a payment phantom is worse
	still: the folio closes (close gates on charges, not payments) with a Payment
	Entry that never reached the ledger.

	The heal is safe precisely because ERPNext shares our database: if the ledger
	claims a document that `_erp_document_is_live` cannot find, that is proof the
	work did not survive, so reopening the operation and re-posting cannot double
	anything. This reasoning holds ONLY for same-database postings; it must never be
	applied to a provider operation.
	"""
	durable = durability.run_durably(**run_kwargs)

	if durable.performed:
		return durable, False

	# The ledger says already resolved. Trust it only if the document it names is
	# actually in the ledger.
	if _erp_document_is_live(erp_doctype, durable.external_reference):
		return durable, True

	# Resolved but the named document does not exist: the recording transaction
	# rolled back. Reopen and re-post under the folio lock the caller already holds.
	durability.reopen_absent_operation(
		idempotency_key,
		reason=_("Resolved {0} {1} was not in the ledger; re-posting.").format(
			erp_doctype, durable.external_reference or ""
		),
	)

	durable = durability.run_durably(**run_kwargs)

	if not durable.performed:
		# Another worker resolved it again between the reopen and here. Fail closed
		# rather than return a phantom; the operator retries or finance reconciles.
		throw(
			_(
				"Posting {0} could not be completed: its durable record is resolved but the "
				"{1} it names is not in the ledger. Retry, or ask finance to reconcile."
			).format(idempotency_key, erp_doctype),
			exc=ReconciliationError,
		)

	return durable, False


def post_folio_invoice(folio: str, *, business_date=None, submit: bool = True) -> dict:
	"""Raise the ERPNext Sales Invoice for a folio's currently unposted lines.

	Reversed charges and their compensating lines are both included so the
	invoice reconciles line-for-line against the folio. A charge already posted
	on an earlier invoice is skipped, and the rows that remain form one batch
	with its own identity - so a retry re-posts nothing and a later charge gets
	a supplementary invoice of its own.
	"""
	# Locked and read together: the batch is decided from this row set, and a
	# pre-lock read could miss a charge another transaction committed while
	# this one waited - leaving it on no invoice at all (N1).
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)

	chargeable = [row for row in doc.charges if not row.is_posted_to_erp]

	if not chargeable:
		# Nothing new to post. If this folio has already been invoiced that is
		# a retry arriving after the work was done, which is a no-op rather
		# than an error; if it has not, there is genuinely nothing to invoice.
		existing = _latest_invoice_posting(folio)

		if existing:
			# ...unless one of this folio's invoices has been withdrawn from the ledger
			# since. Then "already done" is false, and answering `duplicate: True`
			# while pointing at an invoice as the reason is the single most misleading
			# thing this module can say - it was the whole of the folio's remediation
			# route, and it reported health.
			#
			# Every invoice posting is checked, not just the newest. Gating on
			# `_latest_invoice_posting` alone was the first version of this guard and it
			# had a hole exactly where the batch key was invented to help: a folio with
			# a supplementary invoice (P2-4's late charge) whose *first* batch was
			# cancelled still had a live newest row, so the old misleading answer
			# survived for precisely the folios most likely to have one.
			#
			# It still does not re-post. Every charge row is stamped
			# `is_posted_to_erp`, so `chargeable` is empty and there is no batch to
			# raise an invoice from; clearing those stamps is a repair with its own
			# audit and approval, not something a read-path retry may do.
			stale = _stale_rows(
				frappe.get_all(
					POSTING_LOG,
					filters={
						"folio": folio,
						"posting_type": "Sales Invoice",
						"posting_status": ("in", (POSTED, RECONCILED)),
					},
					fields=["name", "posting_status", "erp_doctype", "erp_document"],
					order_by="creation asc",
				)
			)

			if stale:
				throw(_stale_posting_message(stale[0]["name"]), exc=ReconciliationError)

			return {**existing, "duplicate": True}

		throw(_("Folio {0} has no charges to invoice.").format(folio), exc=PostingError)

	idempotency_key = _invoice_batch_key(folio, chargeable)

	log, already_done = _claim(
		doc.property,
		"Sales Invoice",
		idempotency_key,
		folio=folio,
		stay=doc.stay,
		reservation=doc.reservation,
		guest=doc.guest,
		amount=sum(flt(row.total_amount) for row in chargeable),
	)

	if already_done:
		posting = get_posting(idempotency_key)
		return {**posting, "duplicate": True}

	durable, is_duplicate = _run_durable_posting(
		"Sales Invoice",
		idempotency_key,
		dict(
			property_name=doc.property,
			integration_type="Other",
			operation="post_folio_invoice",
			operation_key=idempotency_key,
			reference_doctype=FOLIO_DOCTYPE,
			reference_name=folio,
			payload={"folio": folio, "rows": [row.name for row in chargeable]},
			call=lambda: _build_and_submit_invoice(
				doc, chargeable, log, business_date=business_date, submit=submit
			),
			reference_of=lambda result: result["erp_document"],
		),
	)

	if is_duplicate:
		# The ledger says this batch already reached ERPNext AND the named invoice
		# is live, so the posting log is refreshed from the ledger rather than a
		# second invoice being raised for the same charges.
		return {
			"log": log,
			"erp_doctype": "Sales Invoice",
			"erp_document": durable.external_reference,
			"amount": flt(doc.total_charges),
			"duplicate": True,
		}

	return {**durable.result, "duplicate": False}


@contextmanager
def erp_posting_authority(property_name: str, *, what: str):
	"""The trust boundary between an authorised hotel operation and ERPNext.

	Posting to the ledger is the *system's* act on behalf of an operation the
	hotel has already authorised - not the front desk agent's act. That
	distinction is the whole of UAT-004.

	A Front Office Agent may check a guest out. Checkout raises a Payment
	Entry, and ERPNext's own validation reads the Sales Invoice it allocates
	against, so `check_doctype_permission` refused the agent with a bare
	`PermissionError`. Granting the front desk Sales Invoice and Payment Entry
	read would fix the symptom by giving every agent in the estate the run of
	the accounts - a far worse position than the one it repairs.

	So the elevation is here instead, and it is deliberately small:

	* it is entered only from inside a posting operation, never from an
	  endpoint, and it is not whitelisted;
	* the caller must already hold the folio's own property, re-checked here
	  against the property read from the record rather than from the request;
	* the company is derived from that property, so no caller can point a
	  posting at another company's books;
	* it is released in a `finally`, so nothing downstream inherits it.

	What it does *not* do is make the caller an accounting user. Outside this
	block the agent still cannot read one Sales Invoice, and the tests in
	`test_uat_remediation.py` assert exactly that.
	"""
	# 1. Authorise as the real caller, before any elevation exists. The
	#    property comes from the folio, so this re-asks the Wave-1 boundary
	#    question against the record rather than against anything the client
	#    sent, and a cross-property caller is refused here with a message.
	require_property_access(property_name)

	# 2. The company follows from that property. Nothing the caller supplies
	#    can point the posting at another company's books.
	company = get_company(property_name)

	if not company:
		throw(
			_("Property {0} has no company, so {1} cannot be posted.").format(property_name, what),
			exc=ConfigurationError,
		)

	# 3. Only now does the identity change, and only for the length of one
	#    document operation. ERPNext asks `frappe.has_permission("Sales
	#    Invoice", "read", ...)` while it builds a Payment Entry; that call
	#    answers for the session user and ignores `ignore_permissions`, so a
	#    flag cannot satisfy it - an identity has to. The service identity
	#    holds that one permission and nothing else, which is why it is not
	#    Administrator and not a person.
	actor = frappe.session.user
	previous_flag = frappe.flags.ignore_permissions

	frappe.flags.ignore_permissions = True
	frappe.set_user(POSTING_SERVICE_USER)
	try:
		yield company, actor
	finally:
		# 4. Restored unconditionally. A posting that raises part way through
		#    must not leave the session holding the service identity.
		frappe.set_user(actor)
		frappe.flags.ignore_permissions = previous_flag


def _build_and_submit_invoice(doc, chargeable, log: str, *, business_date=None, submit: bool = True) -> dict:
	with erp_posting_authority(doc.property, what=_("a sales invoice")) as (company, actor):
		return _invoice_within_authority(
			doc, chargeable, log, company, actor, business_date=business_date, submit=submit
		)


def _classify_batch(doc, chargeable) -> dict:
	"""Decide the ERPNext document a batch of charge rows must become.

	A folio can carry credits as well as charges, and ERPNext refuses a Sales
	Invoice item with a negative `rate` when `allow_negative_rates_for_items` is
	off (F-FIN5). So a batch that nets a credit cannot be posted as a bag of
	signed items - each credit has to be represented the way ERPNext accepts it,
	and which way depends on what the credit *is*:

	* a **reversal of a charge already invoiced** is a return of money the ledger
	  has already booked. Its correct ERPNext form is a credit note - a Sales
	  Invoice with `is_return` set and `return_against` naming the original - so
	  the reversal lands against the very document it undoes. A batch of these
	  must be *only* these, all against one invoice: a credit note cannot also
	  carry new charges or a reversal of a different invoice, and mixing them
	  would need two documents, so that is refused here rather than half-posted;

	* every other credit - a service-recovery `Discount`, or a reversal whose
	  original is still *in this same batch* and so was never invoiced on its own
	  - reduces a bill the same invoice is raising. It becomes an invoice-level
	  discount: the positive rows are the items, and the credits are folded into
	  `discount_amount`, so no item rate is ever negative and the grand total
	  still equals the folio. Their tax (a reversal carries negative tax) is
	  accumulated into the tax rows regardless, so the ledger holds the folio's
	  *net* output VAT, not the gross.

	Returns a plan: `is_return`, `return_against`, the rows that become line
	`items`, and the `discount_amount` that represents the folded credits.
	"""
	from hospitality_pms.services.folio import CREDIT_CHARGE_TYPES

	charges_by_name = {row.name: row for row in doc.charges}

	external_reversals: list = []  # reversal of a charge on a prior invoice -> credit note
	credits: list = []  # discounts and in-batch reversals -> discount fold
	positives: list = []

	for row in chargeable:
		original = charges_by_name.get(row.reversal_of) if row.reversal_of else None

		if original is not None and original.is_posted_to_erp:
			external_reversals.append((row, original))
			continue

		if flt(row.amount) < 0 or row.charge_type in CREDIT_CHARGE_TYPES:
			credits.append(row)
		else:
			positives.append(row)

	precision = frappe.get_precision("Sales Invoice", "grand_total") or 2

	if external_reversals:
		# A credit note, and nothing but a credit note. Anything else in the
		# batch belongs on a different document.
		if positives or credits:
			throw(
				_(
					"This folio batch reverses a charge that was already invoiced and also carries "
					"other charges. A reversal of an invoiced charge posts as a credit note against "
					"that invoice, which cannot also hold new charges; post the new charges on their "
					"own first."
				),
				exc=PostingError,
			)

		against = {original.sales_invoice for _, original in external_reversals}

		if len(against) != 1 or not next(iter(against)):
			throw(
				_(
					"This batch reverses charges from more than one invoice, which cannot be a single "
					"credit note. Reverse the charges of one invoice at a time."
				),
				exc=PostingError,
			)

		return {
			"is_return": True,
			"return_against": next(iter(against)),
			"items": [row for row, _ in external_reversals],
			"discount_amount": 0.0,
		}

	net_gross = flt(sum(flt(row.amount) for row in positives), precision)
	discount_total = flt(sum(-flt(row.amount) for row in credits), precision)

	if discount_total > net_gross:
		throw(
			_(
				"The credits on this folio batch ({0}) exceed its charges ({1}), so it cannot be "
				"invoiced for a positive amount. If this reverses an already-invoiced charge, reverse "
				"that specific charge so it can post as a credit note against the original invoice."
			).format(discount_total, net_gross),
			exc=PostingError,
		)

	return {
		"is_return": False,
		"return_against": None,
		"items": positives,
		"discount_amount": discount_total,
	}


def _invoice_within_authority(doc, chargeable, log: str, company: str, actor: str, *, business_date=None, submit: bool = True) -> dict:
	profile = get_posting_profile(doc.property)
	profile_doc = frappe.get_cached_doc(PROFILE_DOCTYPE, profile)

	customer = doc.customer or ensure_customer(doc.guest, company)

	if not doc.customer:
		frappe.db.set_value(FOLIO_DOCTYPE, doc.name, "customer", customer, update_modified=False)

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.company = company
	invoice.currency = doc.currency
	invoice.posting_date = business_date or get_business_date(doc.property)
	invoice.set_posting_time = 1
	invoice.due_date = invoice.posting_date
	invoice.cost_center = profile_doc.default_cost_center
	invoice.debit_to = profile_doc.default_receivable_account or None

	# The folio is the bill the guest was handed, to the fils, and the
	# receivable has to be that number. ERPNext rounds `grand_total` to the
	# nearest whole unit by default and drives `outstanding_amount` from the
	# rounded figure, so a folio of 251.50 became a 252.00 - or, with banker's
	# rounding, a 52.50 became a 52.00 - receivable that could never agree with
	# the folio and left a few fils adrift on every fractional invoice.
	invoice.disable_rounded_total = 1
	invoice.remarks = _("Hospitality folio {0}").format(doc.name)

	# A batch that nets a credit cannot be a bag of signed items: ERPNext refuses
	# a negative item rate. `_classify_batch` decides how each credit is
	# represented instead - a credit note against the invoice it reverses, or an
	# invoice-level discount - so no item rate is ever negative (F-FIN5).
	plan = _classify_batch(doc, chargeable)

	if plan["is_return"]:
		invoice.is_return = 1
		invoice.return_against = plan["return_against"]

	taxes: dict[str, dict] = {}

	# Tax is accumulated from *every* row in the batch, item or folded credit, so
	# the ledger holds the folio's net output VAT. A reversal carries negative
	# tax that must cancel the original's, even though its net is folded into the
	# discount rather than posted as an item.
	for row in chargeable:
		mapping = resolve_charge_item(profile, _tax_charge_type(doc, row))
		_accumulate_tax(taxes, profile_doc, mapping, row)

	for row in plan["items"]:
		mapping = resolve_charge_item(profile, _tax_charge_type(doc, row))
		qty = flt(row.quantity) or 1

		# A credit note carries a negative qty and a *positive* rate, which is
		# how ERPNext represents a return without a negative rate; an ordinary
		# item is the folio figure as-is.
		if plan["is_return"]:
			item_qty = -abs(qty)
			rate = abs(flt(row.amount)) / qty
		else:
			item_qty = qty
			rate = flt(row.amount) / qty

		invoice.append(
			"items",
			{
				"item_code": row.item or mapping["item"],
				"description": row.description,
				"qty": item_qty,
				"rate": rate,
				"amount": flt(row.amount),
				"income_account": mapping["income_account"],
				"cost_center": mapping["cost_center"],
			},
		)

	# Credits on a net-positive batch reduce the bill through an invoice-level
	# discount applied on the grand total, so the item rates stay positive and
	# the grand total still equals the folio.
	if flt(plan["discount_amount"]) > 0:
		invoice.apply_discount_on = "Grand Total"
		invoice.discount_amount = flt(plan["discount_amount"])

	for head, bucket in taxes.items():
		invoice.append(
			"taxes",
			{
				# `Actual` because the amount is not being derived here - it is
				# the tax the folio already charged the guest, and it is that
				# figure, not a recomputation of it, that has to reach the
				# ledger. ERPNext posts an Actual row to `account_head` exactly
				# as it posts a calculated one, so the GL effect is the same as
				# any other tax; only the source of the number differs.
				"charge_type": "Actual",
				"account_head": head,
				"description": bucket["description"],
				"tax_amount": flt(bucket["amount"], bucket["precision"]),
				"cost_center": bucket["cost_center"],
			},
		)

	invoice.flags.ignore_permissions = True
	invoice.insert()

	_assert_invoice_matches_folio(invoice, chargeable)

	if submit:
		invoice.submit()

	# Stamp the folio lines so a second invoice cannot pick them up.
	for row in chargeable:
		frappe.db.set_value(
			"Folio Charge",
			row.name,
			{"is_posted_to_erp": 1, "sales_invoice": invoice.name},
			update_modified=False,
		)

	_mark_posted(log, "Sales Invoice", invoice.name, actor=actor)

	return {
		"log": log,
		"erp_doctype": "Sales Invoice",
		"erp_document": invoice.name,
		"amount": flt(invoice.grand_total),
		"customer": customer,
	}


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------
#
# The folio is the authority on what the guest was charged, tax included. It
# records one net amount and one tax amount per line, and posting's job is to
# put both into ERPNext unchanged - not to re-derive the tax from a rate and
# hope the two agree.
#
# So the tax template is used for what it uniquely knows, the account head, and
# the amount comes from the folio. `_assert_invoice_matches_folio` then proves
# the two halves add up to what the guest was billed before anything is
# submitted.


def _tax_charge_type(doc, row) -> str:
	"""Which charge type's mapping governs this row.

	A reversal is posted as an `Adjustment`, but it reverses a specific charge
	and its negative tax belongs to the same head that received the original.
	Resolving it as an Adjustment would send it to the profile default - or,
	when there is none, refuse a reversal that is perfectly well defined.
	"""
	if not row.reversal_of:
		return row.charge_type

	original = next((line for line in doc.charges if line.name == row.reversal_of), None)

	return original.charge_type if original else row.charge_type


def _accumulate_tax(taxes: dict, profile_doc, mapping: dict, row):
	"""Add one folio line's tax to the bucket for its account head."""
	tax_amount = flt(row.tax_amount)

	if not tax_amount:
		return

	head = _resolve_tax_head(mapping, row.charge_type)

	bucket = taxes.setdefault(
		head["account_head"],
		{
			"amount": 0.0,
			"description": head["description"],
			"cost_center": head["cost_center"] or profile_doc.default_cost_center,
			"precision": frappe.get_precision("Sales Taxes and Charges", "tax_amount") or 2,
		},
	)

	bucket["amount"] += tax_amount


def _resolve_tax_head(mapping: dict, charge_type: str) -> dict:
	"""The single tax account a charge type's template posts to.

	Refuses in both directions rather than guessing, because either guess loses
	money somewhere a reader would not look:

	* No template on a line that bears tax - posting it without the tax is
	  precisely P1-10, silently dropping the hotel's output VAT.
	* A template with more than one head - the folio holds one tax figure per
	  line and no breakdown, so splitting it would be inventing an allocation
	  between two liabilities. The configuration is named in the error instead.
	"""
	template = mapping.get("tax_template")

	if not template:
		throw(
			_(
				"Charge type {0} carries tax but is not mapped to a tax template, so there is "
				"no account to post the tax to. Map it in the posting profile."
			).format(_(charge_type)),
			exc=ConfigurationError,
		)

	return resolve_single_tax_head(template, charge_type)


def resolve_single_tax_head(template: str, charge_type: str) -> dict:
	"""The single tax account a configured template posts to, or refuse.

	The resolvability half of `_resolve_tax_head`, split out so the Posting
	Profile can apply the *same* rule when it is saved. Posting refuses a
	template with more than one head at invoice-build time - during checkout or
	Night Audit - because a folio line holds one tax figure and no breakdown, so
	splitting it would invent an allocation between two liabilities. Sharing this
	function means the profile refuses at save exactly what posting would refuse
	later, with no second rule to drift from this one.
	"""
	rows = frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
		fields=["account_head", "description", "cost_center"],
		order_by="idx asc",
	)

	if len(rows) != 1:
		throw(
			_(
				"Tax template {0} has {1} tax heads. A folio line records a single tax amount, "
				"which cannot be divided between them without inventing an allocation. Use a "
				"template with one head for charge type {2}."
			).format(template, len(rows), _(charge_type)),
			exc=ConfigurationError,
		)

	return rows[0]


def _assert_invoice_matches_folio(invoice, chargeable):
	"""Refuse to submit an invoice that does not say what the folio says.

	The invariant this whole wave exists to establish: for one batch of charge
	rows, the ERPNext grand total equals the folio's net plus its tax. Checked
	after `insert()`, because that is when ERPNext has computed its own totals,
	and before `submit()`, so a mismatch leaves nothing in the ledger.

	Compared at the currency's own precision rather than by exact equality:
	these are floats that have been through two rounding regimes, and 251.5 and
	251.49999999999997 are the same money.
	"""
	precision = frappe.get_precision("Sales Invoice", "grand_total") or 2

	expected = flt(sum(flt(row.total_amount) for row in chargeable), precision)
	actual = flt(invoice.grand_total, precision)

	if expected == actual:
		return

	throw(
		_(
			"Folio total {0} and Sales Invoice total {1} disagree, so the invoice has not been "
			"submitted. Check the tax templates mapped to this folio's charge types."
		).format(expected, actual),
		exc=PostingError,
	)


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------


def post_folio_payment(folio: str, payment_row: str, *, business_date=None) -> dict:
	"""Raise the ERPNext Payment Entry for one folio payment line.

	Posted per line rather than in aggregate so a refund, a deposit and a
	settlement each reconcile against their own ledger entry.
	"""
	# Locked and read together. The allocation below is decided from how much
	# each of this folio's invoices still owes, and this lock is what serialises
	# two payments on the same folio so they cannot both allocate against the
	# same outstanding amount (N1).
	doc = lock_and_get_doc(FOLIO_DOCTYPE, folio)
	row = next((line for line in doc.payments if line.name == payment_row), None)

	if not row:
		throw(_("Payment {0} does not belong to folio {1}.").format(payment_row, folio), exc=PostingError)

	idempotency_key = f"folio-payment:{payment_row}"

	log, already_done = _claim(
		doc.property,
		"Payment Entry",
		idempotency_key,
		folio=folio,
		stay=doc.stay,
		guest=doc.guest,
		amount=row.amount,
	)

	if already_done:
		posting = get_posting(idempotency_key)
		return {**posting, "duplicate": True}

	durable, is_duplicate = _run_durable_posting(
		"Payment Entry",
		idempotency_key,
		dict(
			property_name=doc.property,
			integration_type="Other",
			operation="post_folio_payment",
			operation_key=idempotency_key,
			reference_doctype=FOLIO_DOCTYPE,
			reference_name=folio,
			payload={"folio": folio, "payment_row": payment_row},
			call=lambda: _build_and_submit_payment(doc, row, log, business_date=business_date),
			reference_of=lambda result: result["erp_document"],
		),
	)

	if is_duplicate:
		return {
			"log": log,
			"erp_doctype": "Payment Entry",
			"erp_document": durable.external_reference,
			"amount": abs(flt(row.amount)),
			"duplicate": True,
		}

	return {**durable.result, "duplicate": False}


def _build_and_submit_payment(doc, row, log: str, *, business_date=None) -> dict:
	with erp_posting_authority(doc.property, what=_("a payment entry")) as (company, actor):
		return _payment_within_authority(doc, row, log, company, actor, business_date=business_date)


def _payment_within_authority(doc, row, log: str, company: str, actor: str, *, business_date=None) -> dict:
	profile = get_posting_profile(doc.property)
	profile_doc = frappe.get_cached_doc(PROFILE_DOCTYPE, profile)

	customer = doc.customer or ensure_customer(doc.guest, company)

	is_receipt = flt(row.amount) > 0
	mode_of_payment = _resolve_mode_of_payment(row.payment_method)

	receivable = profile_doc.default_receivable_account or _default_receivable(company)
	funds_account = _resolve_funds_account(company, mode_of_payment)

	entry = frappe.new_doc("Payment Entry")
	entry.payment_type = "Receive" if is_receipt else "Pay"
	entry.company = company
	entry.posting_date = business_date or get_business_date(doc.property)
	entry.party_type = "Customer"
	entry.party = customer
	entry.paid_amount = abs(flt(row.amount))
	entry.received_amount = abs(flt(row.amount))
	entry.source_exchange_rate = 1
	entry.target_exchange_rate = 1
	entry.mode_of_payment = mode_of_payment
	entry.reference_no = row.reference or row.provider_reference or row.name
	entry.reference_date = entry.posting_date

	# Money moves FROM the customer's receivable INTO cash or bank on a
	# receipt, and the other way on a refund. Getting these round the wrong way
	# posts the payment to the wrong side of the ledger.
	if is_receipt:
		entry.paid_from = receivable
		entry.paid_to = funds_account
	else:
		entry.paid_from = funds_account
		entry.paid_to = receivable

	entry.remarks = _("Hospitality folio {0} payment {1}").format(doc.name, row.name)
	# This receipt settles the folio; the POS bridge mirrors till receipts onto folios and
	# must not mirror the folio's own settlement back (pos_bridge.FOLIO_SETTLEMENT_FLAG).
	entry.flags.hospitality_folio_settlement = True

	# A receipt is money the guest handed over against their bill, so it has to
	# say which bill. Submitted without references, ERPNext could only record it
	# as an unapplied customer advance: the Sales Invoice stayed fully
	# outstanding and the Payment Ledger showed a guest who had paid in full as
	# still owing the whole amount (P1-11).
	#
	# A refund is money going the other way and settles nothing, so it is left
	# unreferenced - which is what ERPNext expects of it.
	allocations = _allocate_against_folio_invoices(doc.name, abs(flt(row.amount))) if is_receipt else []

	for allocation in allocations:
		entry.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": allocation["invoice"],
				"allocated_amount": allocation["amount"],
			},
		)

	# Set the account currencies explicitly rather than calling
	# `set_missing_values()`: that helper expects the party account fields to
	# have been wired up by the Desk form first, and fails outside it.
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	entry.paid_from_account_currency = _account_currency(entry.paid_from, company_currency)
	entry.paid_to_account_currency = _account_currency(entry.paid_to, company_currency)

	entry.flags.ignore_permissions = True
	entry.insert()
	entry.submit()

	frappe.db.set_value(
		"Folio Payment",
		row.name,
		{"is_posted_to_erp": 1, "payment_entry": entry.name},
		update_modified=False,
	)

	_mark_posted(log, "Payment Entry", entry.name, actor=actor)

	return {
		"log": log,
		"erp_doctype": "Payment Entry",
		"erp_document": entry.name,
		"amount": abs(flt(row.amount)),
		# Reported rather than left to be discovered in ERPNext: a surplus is a
		# real decision the desk has taken - the guest paid more than they owed
		# - and it should be visible to the caller that made it, not only to
		# whoever reads the Payment Ledger later.
		"allocated_amount": flt(entry.total_allocated_amount),
		"unallocated_amount": flt(entry.unallocated_amount),
		"allocations": [
			{"invoice": ref.reference_name, "amount": flt(ref.allocated_amount)}
			for ref in entry.references
		],
	}


def _allocate_against_folio_invoices(folio: str, amount: float) -> list[dict]:
	"""Split a receipt across this folio's outstanding invoices, oldest first.

	Oldest first because a folio can hold more than one invoice once a late
	charge has produced a supplementary one (P2-4), and settling the newest
	while an older one stays open would be an arbitrary choice that makes AR
	ageing meaningless. Posting order is the order the guest incurred the
	charges, so it is the order they are settled in.

	Never allocates more than an invoice still owes. Whatever is left over is
	returned to ERPNext as an unallocated advance, which is its standard
	treatment of a genuine overpayment - and the caller is told about it.
	"""
	remaining = flt(amount)
	allocations: list[dict] = []

	if remaining <= 0:
		return allocations

	invoices = frappe.get_all(
		POSTING_LOG,
		filters={
			"folio": folio,
			"posting_type": "Sales Invoice",
			"posting_status": ("in", (POSTED, RECONCILED)),
			"erp_document": ("is", "set"),
		},
		order_by="creation asc",
		pluck="erp_document",
	)

	for invoice in invoices:
		# A locking current read: `outstanding_amount` is exactly the mutable
		# figure this decision turns on, and a plain read could allocate twice
		# against money that has already been settled.
		current = lock_and_read("Sales Invoice", invoice, ["docstatus", "outstanding_amount"])

		if current["docstatus"] != 1:
			continue

		outstanding = flt(current["outstanding_amount"])

		if outstanding <= 0:
			continue

		take = min(remaining, outstanding)

		if take <= 0:
			break

		allocations.append({"invoice": invoice, "amount": take})
		remaining -= take

		if remaining <= 0:
			break

	return allocations


def _account_currency(account: str | None, fallback: str) -> str:
	"""An account's currency, falling back to the company's."""
	if not account:
		return fallback

	return frappe.get_cached_value("Account", account, "account_currency") or fallback


def _default_receivable(company: str) -> str:
	"""The company's receivable account, when the profile does not name one."""
	account = frappe.get_cached_value("Company", company, "default_receivable_account")

	if not account:
		throw(
			_("Company {0} has no default receivable account.").format(company),
			exc=ConfigurationError,
		)

	return account


def _resolve_funds_account(company: str, mode_of_payment: str | None) -> str:
	"""The cash or bank account money actually lands in.

	Prefers the account configured against the Mode of Payment, which is how
	ERPNext expects a hotel to separate cash drawer from card settlement, and
	falls back to the company defaults.
	"""
	if mode_of_payment:
		account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mode_of_payment, "company": company},
			"default_account",
		)
		if account:
			return account

	for fieldname in ("default_cash_account", "default_bank_account"):
		account = frappe.get_cached_value("Company", company, fieldname)
		if account:
			return account

	throw(
		_("Company {0} has no default cash or bank account to receive payments into.").format(company),
		exc=ConfigurationError,
	)


def _resolve_mode_of_payment(payment_method: str) -> str | None:
	"""Map a folio payment method onto an ERPNext Mode of Payment.

	Returns None rather than raising when the site has no matching mode: the
	payment entry is still correct without it, and refusing to record money the
	hotel has actually taken would be worse than a missing label.
	"""
	candidate = PAYMENT_MODE_MAP.get(payment_method, payment_method)

	if frappe.db.exists("Mode of Payment", candidate):
		return candidate

	if frappe.db.exists("Mode of Payment", payment_method):
		return payment_method

	return None


# ---------------------------------------------------------------------------
# Retry and reconciliation
# ---------------------------------------------------------------------------


def retry_posting(log: str) -> dict:
	"""Retry a failed posting under its original key.

	Only a Failed row is retried. A Posted row is never re-sent, which is the
	whole point of the key.
	"""
	entry = frappe.get_doc(POSTING_LOG, log)

	if entry.posting_status in (POSTED, RECONCILED):
		# Refusing to retry is the right answer only while the document is still
		# there. Reporting `retried: False` on the authority of a cancelled invoice
		# tells the one person looking at the reconciliation queue that the row is
		# fine, which is how six of them sat untouched.
		if not _erp_document_is_live(entry.erp_doctype, entry.erp_document):
			throw(_stale_posting_message(log), exc=ReconciliationError)

		return {"log": log, "status": entry.posting_status, "retried": False}

	if entry.posting_status == CANCELLED:
		throw(_("Posting {0} was cancelled and cannot be retried.").format(log), exc=PostingError)

	if not entry.folio:
		throw(_("Posting {0} has no folio to retry against.").format(log), exc=PostingError)

	if entry.posting_type == "Sales Invoice":
		result = post_folio_invoice(entry.folio)
	elif entry.posting_type == "Payment Entry":
		payment_row = entry.idempotency_key.split(":", 1)[-1]
		result = post_folio_payment(entry.folio, payment_row)
	else:
		throw(_("Posting type {0} cannot be retried automatically.").format(entry.posting_type), exc=PostingError)

	return {**result, "retried": True}


def folio_erp_documents(folio: str, posting_type: str, *, live_only: bool = True) -> list[str]:
	"""ERPNext documents this folio successfully posted, oldest first.

	Read from the posting log, which records what posting actually did, rather
	than from the folio's own rows, which record only what posting *intended*.

	The log records what posting did *at the time*, though, and it is never
	revised when ERPNext later withdraws the document - so by default the names
	are filtered down to the ones still in the ledger. `live_only=False` returns
	the log's own answer unfiltered, which is what a reconciliation report needs
	in order to say *which* documents went and a caller deciding whether money
	moved must never use.
	"""
	rows = frappe.get_all(
		POSTING_LOG,
		filters={
			"folio": folio,
			"posting_type": posting_type,
			"posting_status": ("in", (POSTED, RECONCILED)),
			"erp_document": ("is", "set"),
		},
		fields=["erp_doctype", "erp_document"],
		order_by="creation asc",
	)

	if not live_only:
		return [row["erp_document"] for row in rows]

	# `erp_doctype` off the row rather than `posting_type`: the two agree on every
	# row `_mark_posted` has ever written, but only one of them is the field that
	# says what the document actually is.
	return [
		row["erp_document"]
		for row in rows
		if _erp_document_is_live(row["erp_doctype"] or posting_type, row["erp_document"])
	]


def reconcile_folio(folio: str) -> dict:
	"""Compare a folio against what ERPNext actually holds.

	Returns the difference rather than correcting it. An automatic correction
	here would paper over the very discrepancy finance needs to see.

	Every ERP figure below is read from the ERP document itself - the invoice's
	`grand_total` and `outstanding_amount`, the Payment Entry's `paid_amount`
	and its allocations. The previous version added up the folio's own charge
	rows whenever they carried a Sales Invoice reference, which asks the folio
	whether the folio was invoiced and can only ever answer yes. That is how a
	folio of 110 whose invoice said 100 was reported as reconciled with a
	variance of zero: the tax row had a reference on it, so it was counted, and
	the ten riyals that never reached ERPNext were confirmed present by the one
	report meant to find them (P1-10).

	Cancelled and draft documents are not counted. A cancelled invoice has been
	reversed out of the ledger and a draft has never entered it, so neither is
	evidence that anything was posted.
	"""
	doc = frappe.get_doc(FOLIO_DOCTYPE, folio)
	precision = frappe.get_precision("Sales Invoice", "grand_total") or 2

	invoices = _submitted_invoices(folio)
	entries = _submitted_payment_entries(folio)

	# Net of what has been credited back. An invoice that a credit note has
	# reversed is still `docstatus == 1` and still sums into `grand_total`, so
	# without this the folio and the ledger were reported as agreeing on money
	# ERPNext had already taken out again - the original invoice standing in as
	# proof of a posting it no longer represents (16.7.5-R1F).
	#
	# The credit note is found from ERPNext's own `return_against`, not from the
	# posting log: finance raises it in ERPNext, so nothing in the PMS ever wrote
	# a row for it and a log-side lookup would find nothing to net.
	returns = submitted_returns([row["name"] for row in invoices])
	erp_returned = sum(flt(row["grand_total"]) for row in returns)

	erp_invoiced = sum(flt(row["grand_total"]) for row in invoices) + erp_returned
	invoice_outstanding = sum(flt(row["outstanding_amount"]) for row in invoices)

	# Signed: a refund is money leaving, and netting it here is what makes
	# `payment_variance` comparable with the folio's own signed total.
	erp_paid = sum(
		flt(row["paid_amount"]) * (1 if row["payment_type"] == "Receive" else -1) for row in entries
	)

	erp_allocated = _allocated_against(
		[row["name"] for row in entries], [row["name"] for row in invoices]
	)

	# An invoice in a currency the folio does not use cannot be compared with
	# it, and silently adding the numbers together would be worse than saying so.
	currency_mismatch = [row["name"] for row in invoices if row["currency"] != doc.currency]

	unposted_charges = [row.name for row in doc.charges if not row.is_posted_to_erp]
	unposted_payments = [row.name for row in doc.payments if not row.is_posted_to_erp]

	# One query, partitioned in Python, rather than two: the `failed_postings` fetch
	# was already being made, so widening its filter adds no round trip.
	#
	# It is not free overall, and the earlier version of this comment claiming so
	# was wrong. `_erp_document_is_live` below is a primary-key lookup **per
	# Posted or Reconciled row**, and `night_audit.reconcile` calls this function
	# for every folio in the property's population, paginated to exhaustion. The
	# cost is therefore O(folios x postings) single-row reads per audit, not zero.
	# Accepted rather than optimised: the alternative is a batched existence query
	# per doctype, which is worth doing if an audit ever measures slow, and is not
	# worth the second code path until it does.
	log_rows = frappe.get_all(
		POSTING_LOG,
		filters={"folio": folio, "posting_status": ("in", (FAILED, POSTED, RECONCILED))},
		fields=[
			"name",
			"posting_type",
			"posting_status",
			"erp_doctype",
			"erp_document",
			"amount",
			"error_message",
			"attempts",
		],
		order_by="creation asc",
	)

	failures = [
		{field: row[field] for field in ("name", "posting_type", "error_message", "attempts")}
		for row in log_rows
		if row["posting_status"] == FAILED
	]

	# The third answer. Not "reconciled" and not "never posted" - posted, and then
	# withdrawn from the ledger by someone outside this application.
	#
	# Without it this report was mute rather than wrong: `_submitted_invoices`
	# correctly refuses to count a cancelled invoice, so the variance was right,
	# but the drop is a silent `continue` and `erp_invoices` came back empty. A
	# folio with 820 posted-then-cancelled read as "820 never reached ERPNext"
	# beside `unposted_charges: []` - two statements that cannot both be true and
	# nothing in the payload to reconcile them. Finance was pointed at a posting
	# job when the answer was an accounting decision.
	stale_postings = [
		{
			"log": row["name"],
			"posting_type": row["posting_type"],
			"posting_status": row["posting_status"],
			"erp_doctype": row["erp_doctype"],
			"erp_document": row["erp_document"],
			"amount": flt(row["amount"], precision),
		}
		for row in _stale_rows(log_rows)
	]

	charge_variance = flt(flt(doc.total_charges) - erp_invoiced, precision)
	payment_variance = flt(flt(doc.total_payments) - erp_paid, precision)

	# What the folio says is still owed, and what ERPNext says is still owed,
	# must be the same number. This is the check that catches a payment which
	# reached ERPNext but settled nothing: the money is there, the variance is
	# zero, and the invoice is still fully outstanding (P1-11).
	folio_outstanding = max(flt(doc.total_charges) - flt(doc.total_payments), 0.0)
	settlement_variance = flt(invoice_outstanding - folio_outstanding, precision)

	return {
		"folio": folio,
		"folio_charges": flt(doc.total_charges, precision),
		"folio_payments": flt(doc.total_payments, precision),
		"erp_invoiced": flt(erp_invoiced, precision),
		"erp_returned": flt(erp_returned, precision),
		"erp_credit_notes": [row["name"] for row in returns],
		"erp_paid": flt(erp_paid, precision),
		"erp_allocated_payments": flt(erp_allocated, precision),
		"unallocated_payments": flt(erp_paid - erp_allocated, precision),
		"invoice_outstanding": flt(invoice_outstanding, precision),
		"charge_variance": charge_variance,
		"payment_variance": payment_variance,
		"settlement_variance": settlement_variance,
		"erp_invoices": [row["name"] for row in invoices],
		"erp_payment_entries": [row["name"] for row in entries],
		"currency_mismatch": currency_mismatch,
		"unposted_charges": unposted_charges,
		"unposted_payments": unposted_payments,
		"failed_postings": failures,
		"stale_postings": stale_postings,
		"needs_erp_reconciliation": bool(stale_postings),
		"is_reconciled": (
			charge_variance == 0
			and payment_variance == 0
			and settlement_variance == 0
			and not currency_mismatch
			and not unposted_charges
			and not unposted_payments
			and not failures
			and not stale_postings
		),
	}


def _submitted_invoices(folio: str) -> list[dict]:
	"""The folio's Sales Invoices that are actually in the ledger."""
	rows = []

	# `live_only=False`: the `docstatus == 1` gate is right here, and asking
	# `folio_erp_documents` to apply the same predicate first would buy one extra
	# query per document to learn what the row fetch below already returns.
	for name in folio_erp_documents(folio, "Sales Invoice", live_only=False):
		row = frappe.db.get_value(
			"Sales Invoice",
			name,
			["name", "docstatus", "grand_total", "outstanding_amount", "currency"],
			as_dict=True,
		)

		# Missing, draft or cancelled: no evidence of anything posted.
		if row and row["docstatus"] == 1:
			rows.append(row)

	return rows


def _submitted_payment_entries(folio: str) -> list[dict]:
	rows = []

	for name in folio_erp_documents(folio, "Payment Entry", live_only=False):
		row = frappe.db.get_value(
			"Payment Entry",
			name,
			["name", "docstatus", "payment_type", "paid_amount"],
			as_dict=True,
		)

		if row and row["docstatus"] == 1:
			rows.append(row)

	return rows


def _allocated_against(payment_entries: list[str], invoices: list[str]) -> float:
	"""How much of those payments ERPNext applied to those invoices.

	Not the same question as "was a Payment Entry submitted". An unapplied
	customer advance answers yes to that one while leaving the invoice entirely
	unpaid, which is precisely the state P1-11 put every folio payment in.
	"""
	if not payment_entries or not invoices:
		return 0.0

	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"parent": ("in", payment_entries),
			"parenttype": "Payment Entry",
			"reference_doctype": "Sales Invoice",
			"reference_name": ("in", invoices),
			"docstatus": 1,
		},
		fields=["allocated_amount"],
	)

	return sum(flt(row["allocated_amount"]) for row in rows)


def mark_reconciled(log: str) -> str:
	"""Flag a posting as checked off by finance.

	Refuses a row whose ERP document has left the ledger. Reconciled is treated
	as interchangeable with Posted by `_claim`, `_latest_invoice_posting` and
	`folio_erp_documents`, so without this guard the reconciliation dashboard
	could stamp a stale row and permanently launder it past every check above -
	the one write on this path that makes the condition unrecoverable.
	"""
	entry = frappe.db.get_value(
		POSTING_LOG, log, ["posting_status", "erp_doctype", "erp_document"], as_dict=True
	)

	if not entry:
		throw(_("Posting {0} does not exist.").format(log), exc=PostingError)

	# Only rows that claim a posting are checked. A Failed row names no document
	# and finance signing one off is a decision about a posting that never
	# happened, which is theirs to make.
	if entry["posting_status"] in (POSTED, RECONCILED) and not _erp_document_is_live(
		entry["erp_doctype"], entry["erp_document"]
	):
		throw(_stale_posting_message(log), exc=ReconciliationError)

	frappe.db.set_value(
		POSTING_LOG,
		log,
		{
			"posting_status": RECONCILED,
			"reconciled_on": now_datetime(),
			"reconciled_by": frappe.session.user,
		},
		update_modified=True,
	)

	return RECONCILED


def get_failed_postings(property_name: str, limit: int = 100) -> list[dict]:
	"""The failed posting queue, for the reconciliation dashboard.

	Read from two places, because a posting can fail in two different ways.

	A Financial Posting Log row marked Failed is one whose transaction survived
	- the posting failed but the request went on. A posting that took its whole
	transaction down with it leaves no such row at all, and used to leave
	nothing whatsoever (P1-14); that one is found in the durable operation
	ledger instead.

	Night Audit calls this to decide whether the day's revenue reached ERPNext,
	so an answer that omitted the second kind would be confidently wrong.

	Because the two halves come from two DocTypes, every row says which DocType
	its own `name` belongs to. Callers that record a row as a reference - Night
	Audit raises one as an exception with a Dynamic Link - cannot otherwise tell
	them apart, and assuming the posting log for both wrote a link that resolved
	to nothing (16.7.6-R1G).
	"""
	logged = frappe.get_all(
		POSTING_LOG,
		filters={"property": property_name, "posting_status": FAILED},
		fields=[
			"name",
			"posting_type",
			"folio",
			"amount",
			"attempts",
			"last_attempt_on",
			"error_message",
			"business_date",
		],
		order_by="last_attempt_on desc",
		limit=limit,
	)

	for row in logged:
		row["reference_doctype"] = POSTING_LOG

	seen = {row["folio"] for row in logged if row["folio"]}

	for row in durability.failed_posting_operations(property_name, limit=limit):
		if row["reference_name"] in seen:
			continue

		logged.append(
			{
				"name": row["name"],
				"reference_doctype": durability.OPERATION_LEDGER,
				"posting_type": _POSTING_TYPE_BY_OPERATION.get(row["operation"], row["operation"]),
				"folio": row["reference_name"],
				"amount": None,
				"attempts": row["attempts"],
				"last_attempt_on": row["modified"],
				"error_message": row["last_error"],
				"business_date": None,
				"durable_operation": row["operation_key"],
				"operation_status": row["queue_status"],
			}
		)

	return logged[:limit]


#: Ledger operation names mapped back onto the posting vocabulary the
#: reconciliation screen already speaks.
_POSTING_TYPE_BY_OPERATION = {
	"post_folio_invoice": "Sales Invoice",
	"post_folio_payment": "Payment Entry",
}


def closed_folios_with_unposted_charges(property_name: str, limit: int = 100) -> list[dict]:
	"""Folios that closed with revenue still not in the ledger.

	The ones finance most needs to see: the guest has gone, the folio is
	settled, and a charge never reached ERPNext. Lived as raw SQL inside
	`api.checkout.reconciliation` until 16.7.5; moved here because a query that
	decides what finance is shown is a business rule, and `CLAUDE.md` puts
	those in `services/`.
	"""
	return frappe.db.sql(
		"""
		select distinct f.name, f.guest_name, f.folio_status, f.total_charges, f.balance
		from `tabGuest Folio` f
		inner join `tabFolio Charge` c on c.parent = f.name
		where f.property = %(property)s
		  and f.folio_status in ('Settled', 'Closed')
		  and ifnull(c.is_posted_to_erp, 0) = 0
		order by f.modified desc
		limit %(limit)s
		""",
		{"property": property_name, "limit": int(limit)},
		as_dict=True,
	)
