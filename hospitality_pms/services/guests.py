"""Guest identity: naming, duplicate detection, Customer linking and merging.

Guest is the primary guest record (HPMS-DEC-011). An ERPNext
Customer is created or linked only when money has to move (HPMS-DEC-012), so
most guests never get one.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from hospitality_pms.services.base import lock_and_read, lock_document, require_role
from hospitality_pms.services.exceptions import ConfigurationError, HospitalityPMSError, throw
from hospitality_pms.services.property import resolve_operational_date

GUEST_DOCTYPE = "Guest"
MERGE_LOG_DOCTYPE = "Guest Merge Log"

#: Roles allowed to merge guest records. A merge rewrites history across
#: reservations, stays and folios, so it is not an everyday front desk action.
MERGE_ROLES = ("Hospitality Administrator", "System Manager", "Hotel Manager", "Guest Relations Officer")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def compose_guest_name(first_name: str | None, middle_name: str | None, last_name: str | None) -> str:
	"""Build the display name from its parts.

	Kept here rather than in the controller so imports, channel bookings and
	the merge service all produce identical names.
	"""
	parts = [(first_name or "").strip(), (middle_name or "").strip(), (last_name or "").strip()]

	return " ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

#: Confidence weights. An identity document or a contact channel is a much
#: stronger signal than a common name, so they are scored far higher.
MATCH_WEIGHTS = {
	"id_number": 60,
	"email_id": 30,
	"mobile_no": 30,
	"name_and_dob": 25,
	"name_only": 10,
}

DUPLICATE_THRESHOLD = 30


def find_duplicates(
	first_name: str | None = None,
	last_name: str | None = None,
	email_id: str | None = None,
	mobile_no: str | None = None,
	id_number: str | None = None,
	date_of_birth=None,
	exclude: str | None = None,
	limit: int = 10,
) -> list[dict]:
	"""Candidate existing guests for the details being entered.

	Returns scored candidates rather than deciding: merging the wrong two
	guests is far more damaging than creating a duplicate, so a human confirms.
	"""
	scores: dict[str, int] = {}
	reasons: dict[str, list[str]] = {}

	def add(guest: str, weight: int, reason: str):
		if not guest or guest == exclude:
			return
		scores[guest] = scores.get(guest, 0) + weight
		reasons.setdefault(guest, []).append(reason)

	if id_number:
		rows = frappe.get_all(
			"Guest Identification",
			filters={"id_number": id_number.strip(), "parenttype": GUEST_DOCTYPE},
			fields=["parent"],
			limit=limit * 2,
		)
		for row in rows:
			add(row["parent"], MATCH_WEIGHTS["id_number"], _("Same identification number"))

	if email_id:
		for guest in frappe.get_all(
			GUEST_DOCTYPE, filters={"email_id": email_id.strip()}, pluck="name", limit=limit * 2
		):
			add(guest, MATCH_WEIGHTS["email_id"], _("Same email address"))

	if mobile_no:
		for guest in frappe.get_all(
			GUEST_DOCTYPE, filters={"mobile_no": mobile_no.strip()}, pluck="name", limit=limit * 2
		):
			add(guest, MATCH_WEIGHTS["mobile_no"], _("Same mobile number"))

	if first_name and last_name:
		filters = {"first_name": first_name.strip(), "last_name": last_name.strip()}
		name_matches = frappe.get_all(
			GUEST_DOCTYPE, filters=filters, fields=["name", "date_of_birth"], limit=limit * 2
		)

		for row in name_matches:
			if date_of_birth and row.get("date_of_birth") and str(row["date_of_birth"]) == str(date_of_birth):
				add(row["name"], MATCH_WEIGHTS["name_and_dob"], _("Same name and date of birth"))
			else:
				add(row["name"], MATCH_WEIGHTS["name_only"], _("Same name"))

	candidates = [guest for guest, score in scores.items() if score >= DUPLICATE_THRESHOLD]

	if not candidates:
		return []

	details = frappe.get_all(
		GUEST_DOCTYPE,
		filters={"name": ("in", candidates)},
		fields=["name", "guest_name", "email_id", "mobile_no", "nationality", "total_stays", "last_stay_on"],
	)

	for row in details:
		row["match_score"] = scores[row["name"]]
		row["match_reasons"] = reasons[row["name"]]

	return sorted(details, key=lambda row: row["match_score"], reverse=True)[:limit]


# ---------------------------------------------------------------------------
# ERPNext Customer linking
# ---------------------------------------------------------------------------


def ensure_customer(guest: str, company: str | None = None) -> str:
	"""Return the guest's ERPNext Customer, creating one if needed.

	Called only when a financial document is about to be raised. Creating a
	Customer for every walk-in would pollute the receivables ledger with
	records that never carry a balance.
	"""
	# Locked and read in one operation. Locking and then re-reading with a
	# plain `get_value` answered from the pre-lock snapshot (N1): the waiter
	# still saw no customer after the winner had committed one, and tried to
	# create a second. ERPNext names a Customer after the guest, so that second
	# insert collides on the primary key - and the posting that needed the
	# customer fails, for a customer that exists.
	existing = lock_and_read(GUEST_DOCTYPE, guest, "customer")["customer"]

	if existing:
		return existing

	doc = frappe.get_doc(GUEST_DOCTYPE, guest)

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": doc.guest_name,
			"customer_type": "Individual",
			"customer_group": _default_customer_group(),
			"territory": _default_territory(),
			"mobile_no": doc.mobile_no,
			"email_id": doc.email_id,
		}
	).insert(ignore_permissions=True)

	frappe.db.set_value(GUEST_DOCTYPE, guest, "customer", customer.name, update_modified=False)

	return customer.name


#: Custom Field on Customer (setup/pos_bridge.py): the room(s) the guest is in while in
#: house, blank otherwise. The till shows it beside the name.
CUSTOMER_ROOM_FIELD = "hpms_room"

#: Stay statuses that count as "in house" here; `stays` imports this module, so the
#: constants cannot come from there.
_IN_HOUSE_STAY_STATES = ("In House", "Due Out")


def sync_customer_room(guest: str) -> str | None:
	"""Keep the guest's Customer labelled with the room while they are in house.

	Called after check-in, check-out and a room move. The label is what a till reads to
	tell two guests of one name apart (POS Next's customer annotation hook, served by
	`pos_bridge.annotate_till_customers`).

	A real save rather than a column write when it can be: the till learns of a Customer
	through that document's own hooks, at once over its live channel and, for a till that
	was offline, through `modified` at its next delta sync. Should the save be refused (a
	Customer that no longer passes ERPNext's own validation), the column is written on
	its own and the till catches up at its next sync; the stay never depends on it.
	"""
	customer = frappe.db.get_value(GUEST_DOCTYPE, guest, "customer")
	if not customer or not frappe.db.has_column("Customer", CUSTOMER_ROOM_FIELD):
		return None
	rooms = frappe.get_all(
		"Stay",
		filters={"guest": guest, "stay_status": ("in", _IN_HOUSE_STAY_STATES)},
		order_by="checked_in_on desc",
		pluck="room",
	)
	label = ", ".join(dict.fromkeys(room for room in rooms if room))
	current = frappe.db.get_value("Customer", customer, CUSTOMER_ROOM_FIELD) or ""
	if current == label:
		return label or None
	try:
		doc = frappe.get_doc("Customer", customer)
		doc.set(CUSTOMER_ROOM_FIELD, label)
		doc.flags.ignore_permissions = True
		doc.save()
	except Exception:
		frappe.log_error(title="Guest Customer room label", message=frappe.get_traceback())
		frappe.db.set_value("Customer", customer, CUSTOMER_ROOM_FIELD, label)
	return label or None


def _default_customer_group() -> str:
	group = frappe.db.get_single_value("Selling Settings", "customer_group")

	if group:
		return group

	default = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")

	if not default:
		throw(_("No Customer Group is configured in ERPNext."), exc=ConfigurationError)

	return default


def _default_territory() -> str:
	territory = frappe.db.get_single_value("Selling Settings", "territory")

	if territory:
		return territory

	default = frappe.db.get_value("Territory", {"is_group": 0}, "name")

	if not default:
		throw(_("No Territory is configured in ERPNext."), exc=ConfigurationError)

	return default


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def get_guest_link_fields() -> list[tuple[str, str]]:
	"""Every (doctype, fieldname) that links to Guest.

	Discovered from the schema rather than hard-coded, so a merge keeps working
	as reservations, stays, folios and guest requests are added in later builds
	without anyone remembering to update this list.
	"""
	rows = frappe.get_all(
		"DocField",
		filters={"fieldtype": "Link", "options": GUEST_DOCTYPE},
		fields=["parent", "fieldname"],
	)

	custom = frappe.get_all(
		"Custom Field",
		filters={"fieldtype": "Link", "options": GUEST_DOCTYPE},
		fields=["dt as parent", "fieldname"],
	)

	seen = set()
	fields = []

	for row in list(rows) + list(custom):
		key = (row["parent"], row["fieldname"])

		# Skip the guest DocType's own self-references and anything whose table
		# does not exist yet.
		if key in seen or row["parent"] == GUEST_DOCTYPE:
			continue

		seen.add(key)
		fields.append(key)

	return fields


def merge_guests(source: str, target: str, reason: str) -> dict:
	"""Merge `source` into `target`, repointing every reference.

	The source record is kept and marked merged rather than deleted: reservation
	and folio history must stay auditable for the ten year retention period
	(SAS section 8), and a deleted guest would strand it.
	"""
	require_role(MERGE_ROLES)

	if source == target:
		throw(_("A guest cannot be merged into itself."))

	if not reason or not reason.strip():
		throw(_("A reason is required to merge guest records."))

	# Lock in a deterministic order so two simultaneous merges of the same pair
	# cannot interleave.
	for guest in sorted([source, target]):
		lock_document(GUEST_DOCTYPE, guest)

	source_doc = frappe.get_doc(GUEST_DOCTYPE, source)
	frappe.get_doc(GUEST_DOCTYPE, target)  # existence check

	snapshot = json.dumps(source_doc.as_dict(no_nulls=True), default=str, indent=1)

	for doctype, fieldname in get_guest_link_fields():
		if not frappe.db.table_exists(doctype):
			continue

		# Deliberately unscoped, and it must stay that way. The merge has to
		# repoint *every* reference: narrowing this read to the caller's
		# properties would leave rows elsewhere pointing at a guest the code
		# below retires, which is an orphan, not a privacy control.
		affected = frappe.get_all(doctype, filters={fieldname: source}, pluck="name")
		if not affected:
			continue

		for record in affected:
			frappe.db.set_value(doctype, record, fieldname, target, update_modified=False)

	frappe.get_doc(
		{
			"doctype": MERGE_LOG_DOCTYPE,
			"merged_into": target,
			"merged_from": source,
			"merged_from_name": source_doc.guest_name,
			"merged_by": frappe.session.user,
			"merged_at": now_datetime(),
			"reason": reason.strip(),
			"merged_fields": snapshot,
		}
	).insert(ignore_permissions=True)

	# The merged record stays readable but is taken out of operational use.
	frappe.db.set_value(
		GUEST_DOCTYPE,
		source,
		{"notes": _("Merged into {0} on {1}.").format(target, now_datetime())},
		update_modified=True,
	)

	# No counts. Until 16.7.4 this returned `references_moved` - a per-DocType
	# tally of the rows just repointed, built from the unscoped read above - and
	# 16.7.3 made merge reachable from a browser, so a Hotel Manager restricted
	# to one property learned how many reservations, stays and folios the guest
	# had across the whole estate. Worse, `get_guest_link_fields` discovers link
	# fields from the schema, so the tally silently grew to include DocTypes the
	# caller cannot read at all: a Guest Relations Officer holds no read on
	# Financial Posting Log, Payment Transaction, Key Card or Guest Registration,
	# and was counted rows in every one of them.
	#
	# Scoping the counts was considered and rejected. Scoping by property leaves
	# the DocType axis open; filtering by DocType leaves the property axis open;
	# a single total is still an estate-wide fact and is recoverable by
	# subtraction. And any filter is a standing obligation to re-review it every
	# time a DocType gains a `Link: Guest` field, which the schema discovery
	# above guarantees will happen unnoticed.
	#
	# The field had no consumer: the dialog discards the response and shows a
	# fixed confirmation, and nothing in the backend read it. A value nobody
	# reads has no requirement to satisfy, so its correct scope is none. If merge
	# volume is ever genuinely needed, it belongs in a property-scoped read of
	# `Guest Merge Log`, which is already audited and reader-restricted - not in
	# the return value of a mutation.
	return {"source": source, "target": target}


# ---------------------------------------------------------------------------
# Operational checks
# ---------------------------------------------------------------------------


def assert_not_blacklisted(guest: str):
	"""Refuse an operation for a blacklisted guest, without saying why (P2-6).

	`blacklist_reason` sits at permlevel 3 precisely so it does not reach a
	front desk screen: it can carry incident detail, police references or HR
	material, and the agent needs to know *that* they must refuse, not what the
	guest is alleged to have done. `is_blacklisted` is permlevel 2, which the
	front office does hold, and that is the part the agent needs.

	This function used to read both fields with `frappe.db.get_value` - which
	applies no permlevel filtering at all, being a raw column read - and
	interpolate the reason straight into the message. The permlevel design was
	correct and this one call defeated it.

	Only the flag is read now. Whoever needs the reason reads the Guest record
	through the ordinary document path, where the permlevel applies and the
	roles that own blacklisting can see it.
	"""
	if not frappe.db.get_value(GUEST_DOCTYPE, guest, "is_blacklisted"):
		return

	throw(
		_(
			"This guest cannot be checked in. Please contact a manager, who can review the "
			"guest's record."
		),
		exc=HospitalityPMSError,
	)


def may_see_blacklist() -> bool:
	"""Whether this user is cleared for the blacklist flag itself (permlevel 2).

	The one place this question is answered. `is_blacklisted` is permlevel 2 with
	a deliberately narrower reader set than Guest itself: the desk holds it so it
	can refuse a check-in, and housekeeping, maintenance, kitchen and revenue do
	not.

	It exists because the permlevel is only enforced on the *document* path.
	Anything that reads the column through `frappe.get_all`, `frappe.db.get_value`
	or raw SQL bypasses it silently, so every aggregate that wants to report the
	flag has to ask this first. 16.7.0's architecture review found exactly that
	hole in the arrivals board (`front_office.get_guest_flags`), which is the
	second time a permission-free column read has defeated this design -
	`assert_not_blacklisted` above records the first.
	"""
	return 2 in frappe.get_meta(GUEST_DOCTYPE).get_permlevel_access("read")


def may_see_blacklist_reason() -> bool:
	"""Whether this user may read *why* a guest is blacklisted (permlevel 3).

	A strictly smaller set than `may_see_blacklist`: the reason can carry incident
	detail, police references or HR material, which is why it never travels on an
	operational board and is never interpolated into a refusal message.
	"""
	return 3 in frappe.get_meta(GUEST_DOCTYPE).get_permlevel_access("read")


#: Alert severities, weakest first. The order is the DocType's own Select order
#: and is what makes "the worst alert on this guest" a defined question.
ALERT_SEVERITY_ORDER = ("Info", "Warning", "Critical")


def get_active_alert_summary(
	guests: list[str], property_name: str | None = None, business_date=None
) -> dict[str, dict]:
	"""How many active alerts each guest carries, and how bad the worst one is.

	`{guest: {"count": int, "severity": str}}` - a count and a grade, never a
	body. Alert text is free-form and a guest can be standing on the other side
	of the counter reading the screen, so a board is told how many alerts there
	are and how serious the worst is; whoever needs to read one opens the guest,
	where the endpoint that returns them applies its own permissions.

	The severity is here because a bare count fuses an allergy note with "likes a
	high floor", and staff learn to ignore a number that means both. The grade is
	what makes it a signal.

	One query for the whole board rather than `get_active_alerts` per row: that
	one loads a cached Guest document each time, which is an N+1 the moment a
	board has a hundred rows.

	`business_date` may be passed by a caller that has already resolved the
	property's operating day, which spares a second read of the same column. The
	validity window is answered on that day for the same reason
	`get_active_alerts` does it: a hotel still working the 8th must still see an
	alert that runs to the 8th.
	"""
	guests = [g for g in set(guests or []) if g]
	if not guests:
		return {}

	if business_date:
		today = frappe.utils.getdate(business_date)
	elif property_name:
		today = resolve_operational_date(property_name)
	else:
		today = frappe.utils.getdate()

	rows = frappe.get_all(
		"Guest Alert",
		filters={
			"parent": ("in", guests),
			"parenttype": GUEST_DOCTYPE,
			"is_active": 1,
		},
		fields=["parent", "valid_upto", "severity"],
		limit_page_length=0,
	)

	summary: dict[str, dict] = {}

	for row in rows:
		if row["valid_upto"] and frappe.utils.getdate(row["valid_upto"]) < today:
			continue

		entry = summary.setdefault(row["parent"], {"count": 0, "severity": ""})
		entry["count"] += 1

		if _is_worse(row["severity"], entry["severity"]):
			entry["severity"] = row["severity"]

	return summary


def _is_worse(candidate: str | None, current: str | None) -> bool:
	"""Whether `candidate` outranks `current` on `ALERT_SEVERITY_ORDER`.

	An unrecognised or empty severity never outranks a known one: a row the
	DocType's Select has since changed under must not quietly become the worst
	alert on the guest.
	"""
	if candidate not in ALERT_SEVERITY_ORDER:
		return False

	if current not in ALERT_SEVERITY_ORDER:
		return True

	return ALERT_SEVERITY_ORDER.index(candidate) > ALERT_SEVERITY_ORDER.index(current)


def get_active_alerts(guest: str, property_name: str | None = None) -> list[dict]:
	"""Alerts a front desk agent must see when serving this guest.

	`valid_upto` is a Date, not a timestamp: it says which *day* an alert
	stops applying, so it is answered on the property's operating day where
	there is one. A hotel still working the 8th must still see an alert that
	runs to the 8th, even though the calendar says the 10th.

	A Guest belongs to no single property, so without property context there
	is no business day to use and the calendar date is the honest fallback.
	"""
	doc = frappe.get_cached_doc(GUEST_DOCTYPE, guest)
	today = (
		resolve_operational_date(property_name) if property_name else frappe.utils.getdate()
	)

	return [
		{
			"alert_type": row.alert_type,
			"severity": row.severity,
			"alert": row.alert,
			"valid_upto": row.valid_upto,
		}
		for row in doc.alerts
		if row.is_active and (not row.valid_upto or frappe.utils.getdate(row.valid_upto) >= today)
	]
