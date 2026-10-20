"""Reservation lifecycle, pricing and inventory holding.

The rule this module exists to guarantee: **a reservation may only reach a
holding state if, at that exact moment and under a lock, the inventory is
there.** Everything else here supports that.

Concurrency
-----------
Two agents selling the last room at the same instant must not both succeed.
Confirmation locks the Room Type rows first, then re-reads availability, then
writes. Because the lock is held for the rest of the transaction, the second
agent's read happens after the first agent's write and correctly sees zero.

Snapshots
---------
The rate breakdown is stored on the reservation. Re-pricing a confirmed
reservation from today's rate grid would silently change what the guest owes.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, get_time, getdate, now_datetime, nowdate

from hospitality_pms.services.availability import (
	authorise_overbooking,
	check_availability,
	check_demand,
	lock_room_type,
	nights_between,
	overbooking_evidence,
)
from hospitality_pms.services.base import (
	assert_transition,
	lock_and_find,
	lock_and_get_doc,
	lock_and_read,
	lock_document,
	require_role,
	transaction,
)
from hospitality_pms.services.availability import stay_nights
from hospitality_pms.services.exceptions import (
	InvalidStateTransitionError,
	HospitalityPMSError,
	PermissionDeniedError,
	throw,
)
from hospitality_pms.services.property import get_business_date, resolve_operational_date
from hospitality_pms.services.rates import (
	get_applicable_rate_plans,
	get_cancellation_charge,
	get_rate_breakdown,
	validate_restrictions,
)
from hospitality_pms.services.rooms import (
	assert_assignable,
	assert_room_unoccupied,
	occupancy_applies,
)

RESERVATION_DOCTYPE = "Reservation"
RESERVATION_LOG_DOCTYPE = "Reservation Log"

DRAFT = "Draft"
TENTATIVE = "Tentative"
CONFIRMED = "Confirmed"
GUARANTEED = "Guaranteed"
WAITLISTED = "Waitlisted"
CHECKED_IN = "Checked In"
CHECKED_OUT = "Checked Out"
CLOSED = "Closed"
CANCELLED = "Cancelled"
NO_SHOW = "No Show"

#: The reservation state machine, from Workflow Matrix section 2.
TRANSITIONS = {
	DRAFT: {TENTATIVE, CONFIRMED, CANCELLED},
	TENTATIVE: {CONFIRMED, WAITLISTED, CANCELLED},
	WAITLISTED: {TENTATIVE, CONFIRMED, CANCELLED},
	CONFIRMED: {GUARANTEED, CHECKED_IN, CANCELLED, NO_SHOW},
	GUARANTEED: {CHECKED_IN, CANCELLED, NO_SHOW},
	CHECKED_IN: {CHECKED_OUT},
	# CHECKED_OUT -> CHECKED_IN exists solely for the role-gated reverse-checkout
	# path (checkout.reverse_checkout): reversing a departure puts a stay back
	# In House, so its reservation must return to a holding state or the room line
	# drops out of the availability sold-count and the room can be oversold. The
	# authority for the reversal is require_role in reverse_checkout, not this edge.
	CHECKED_OUT: {CLOSED, CHECKED_IN},
	CLOSED: set(),
	CANCELLED: set(),
	NO_SHOW: {CANCELLED},
}

#: States that consume inventory. Kept in step with the availability engine's
#: HOLDING_RESERVATION_STATES - if these two ever disagree, availability is
#: wrong, so they are asserted equal at import time below.
HOLDING_STATES = (CONFIRMED, GUARANTEED, CHECKED_IN)

#: Cancelling inside the policy window, or after arrival, needs a manager.
CANCEL_OVERRIDE_ROLES = (
	"Front Office Manager",
	"Reservation Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)

NO_SHOW_ROLES = (
	"Night Auditor",
	"Front Office Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)


def _assert_states_agree():
	from hospitality_pms.services import availability

	if set(availability.HOLDING_RESERVATION_STATES) != set(HOLDING_STATES):
		raise RuntimeError(
			"Reservation holding states and availability holding states disagree; "
			"availability would be computed against the wrong reservations."
		)


_assert_states_agree()


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def price_reservation(doc, *, check_restrictions: bool = True) -> dict:
	"""Resolve and attach the rate snapshot for every room line.

	Called on validate for a reservation that is still editable. Once a
	reservation holds inventory its lines are not re-priced automatically.
	"""
	totals = {"room_charges_total": 0.0, "total_rooms": 0, "total_adults": 0, "total_children": 0}

	# The snapshot is one flat table on the reservation: Frappe has no
	# grandchild tables, so it cannot hang off the room line itself. Each row
	# carries `room_line` to say which line it belongs to.
	doc.set("rate_lines", [])

	for line in doc.rooms:
		breakdown = get_rate_breakdown(
			doc.property,
			line.room_type,
			line.arrival_date or doc.arrival_date,
			line.departure_date or doc.departure_date,
			rate_plan=line.rate_plan or doc.rate_plan,
			adults=line.adults,
			children=line.children,
			extra_beds=line.extra_beds,
			rooms=line.rooms,
			check_restrictions=check_restrictions,
			arrival_time=line.get("arrival_time"),
			departure_time=line.get("departure_time"),
			blocks=line.get("blocks") or 1,
		)
		if breakdown.get("stay_kind"):          # a short stay: keep the hours the price was made for
			line.arrival_time = breakdown["arrival_time"]
			line.departure_time = breakdown["departure_time"]
			line.blocks = breakdown["blocks"]

		line.rate_plan = breakdown["rate_plan"]
		line.nights = breakdown["nights"]
		line.room_rate = breakdown["average_nightly_rate"]
		line.total_amount = breakdown["total_amount"]

		for rate_line in breakdown["lines"]:
			doc.append("rate_lines", {**rate_line, "room_line": line.name})

		totals["room_charges_total"] += breakdown["total_amount"]
		totals["total_rooms"] += int(line.rooms or 1)
		totals["total_adults"] += int(line.adults or 0) * int(line.rooms or 1)
		totals["total_children"] += int(line.children or 0) * int(line.rooms or 1)

	doc.room_charges_total = flt(totals["room_charges_total"], 2)
	doc.total_amount = flt(totals["room_charges_total"], 2)
	doc.total_rooms = totals["total_rooms"]
	doc.total_adults = totals["total_adults"]
	doc.total_children = totals["total_children"]

	return totals


def sync_room_lines(doc):
	"""Keep the denormalised fields on Reservation Room in step with the parent.

	Availability filters Reservation Room directly, without joining to the
	reservation, so `property`, `reservation_status` and the dates have to be
	correct on every line or the engine will miscount.
	"""
	for line in doc.rooms:
		line.property = doc.property
		line.reservation_status = doc.reservation_status
		line.arrival_date = line.arrival_date or doc.arrival_date
		line.departure_date = line.departure_date or doc.departure_date
		line.nights = len(stay_nights(line.arrival_date, line.departure_date))
		if not line.get("arrival_time") and doc.get("arrival_time"):
			line.arrival_time = doc.arrival_time
		if not line.get("departure_time") and doc.get("departure_time"):
			line.departure_time = doc.departure_time
		line.rooms = max(int(line.rooms or 1), 1)


def _propagate_status(reservation: str, status: str):
	"""Push a status change down to the room lines already in the database."""
	for line in frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name"):
		frappe.db.set_value("Reservation Room", line, "reservation_status", status, update_modified=False)



def _first_night_amount(doc) -> float:
	"""The net rate of the earliest night in the snapshot.

	Read from the stored snapshot, not recomputed, so a first-night charge uses
	the rate the guest was actually sold.
	"""
	if not doc.rate_lines:
		return 0.0

	earliest = min(doc.rate_lines, key=lambda row: getdate(row.rate_date))

	return flt(earliest.net_rate)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _transition(doc, target: str, *, reason: str | None = None, details: dict | None = None):
	"""Apply a state change: validate against **current** state, write, propagate, log.

	The status is re-read here under the row lock rather than taken from
	`doc.reservation_status`, and that is the whole defence against P1-4.

	A caller's document is loaded at the top of its operation. By the time the
	operation reaches this point it may have waited behind another transaction
	that changed the very status this transition is legal or illegal against -
	and under REPEATABLE READ the caller's copy still shows the pre-wait value
	(N1). Evaluating the state machine against that copy is what let two
	concurrent confirms both see `Draft`, and what let a confirm overwrite a
	cancellation that had already committed.

	Reading it with `lock_and_read` makes this function the single point where
	every reservation state change is decided, for every caller, against the
	state that is actually in the database.
	"""
	previous = lock_and_read(RESERVATION_DOCTYPE, doc.name, "reservation_status")["reservation_status"]

	assert_transition(previous, target, TRANSITIONS, _("Reservation"))

	frappe.db.set_value(RESERVATION_DOCTYPE, doc.name, "reservation_status", target, update_modified=True)
	doc.reservation_status = target

	_propagate_status(doc.name, target)

	frappe.get_doc(
		{
			"doctype": RESERVATION_LOG_DOCTYPE,
			"property": doc.property,
			"reservation": doc.name,
			"from_status": previous,
			"to_status": target,
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": reason,
			"details": json.dumps(details, default=str) if details else None,
		}
	).insert(ignore_permissions=True)

	return target


def confirm(reservation: str, *, allow_overbooking: bool = False, reason: str | None = None) -> str:
	"""Confirm a reservation, holding inventory for every night.

	This is the point where inventory is actually committed, so it is the point
	that has to be race-safe.
	"""
	# Locked first, then read from that same locking read, so every guard below
	# - the room lines, the availability check, the credit draw - is evaluated
	# against the reservation as it stands now rather than as it stood when
	# this request started (N1). Permission is checked on the locked document:
	# an unauthorised caller is refused a moment later and its lock goes with
	# the rolled-back transaction.
	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	# Refuse an illegal transition before any inventory or credit is touched.
	# _transition re-asserts this under the lock as well; doing it here too
	# means the loser of a race never draws corporate credit it will have to
	# roll back.
	assert_transition(doc.reservation_status, CONFIRMED, TRANSITIONS, _("Reservation"))

	if not doc.rooms:
		throw(_("A reservation must have at least one room line before it can be confirmed."))

	# Selling past capacity is authorised before any inventory is locked, so an
	# override nobody may make costs nothing and blocks nobody (P1-3).
	overbooking_reason = authorise_overbooking(reason) if allow_overbooking else None

	# A quantity is how a booking is asked for; it is not how three rooms are
	# run. Normalised here, while the reservation is still editable and before
	# any inventory is committed, so everything downstream - assignment,
	# check-in, the Stay, the folio, the deposit - has one row per real room
	# (P1-6).
	if normalise_room_lines(doc):
		doc.save(ignore_permissions=True)

	# Lock every room type this reservation touches before reading availability.
	lock_room_type(doc.property, [line.room_type for line in doc.rooms])

	# Summed per night across the whole booking. Asking once per row would ask
	# for one room three times, which a house with one room left would grant.
	#
	# `current=True` because the lock above serialises without refreshing: a
	# caller that queued behind another confirmation still counts the house as it
	# stood before that confirmation committed, and sells the room it just took
	# (N1). The lock and the current read are two halves of one guarantee, and
	# this module's own docstring described only the first half.
	checks = check_demand(
		doc.property,
		doc.rooms,
		allow_overbooking=allow_overbooking,
		exclude_reservation=reservation,
		current=True,
	)

	# Validate any room a line was pre-assigned with (RES-4). `assigned_room` can be
	# set on a draft through the create payload, a channel import or Desk without
	# ever passing the clash, property and type checks - and confirm otherwise checks
	# only room-type demand, so two lines could hold one physical room, or a line
	# could hold a room of the wrong type or another property, surfacing only at the
	# desk on arrival. assign_room applies exactly those checks under the room's lock
	# and writes the same value back when it is unchanged; it is legal here because
	# the reservation is still in an interval-editable state. Room-type locks are
	# already held above, so this preserves the Reservation -> Room Type -> Hotel Room
	# order. Sorted by the room being locked so the Hotel Room locks are taken in a
	# deterministic order - the same order a concurrent same-type change_room takes
	# its sorted room locks - or a multi-room pre-assigned booking could deadlock a
	# room move that shares two of its rooms.
	for line in sorted(
		(row for row in doc.rooms if row.assigned_room), key=lambda row: row.assigned_room
	):
		assign_room(reservation, line.name, line.assigned_room)

	# A corporate booking draws on the account's credit. This runs inside the
	# same locked transaction as the availability check, so the credit movement
	# and the booking decision commit together or not at all.
	_consume_corporate_credit(doc)

	details = {"rooms": [{"room_type": line.room_type, "rooms": line.rooms} for line in doc.rooms]}

	if overbooking_reason:
		# Recorded on the transition itself rather than in a log of its own, so
		# an auditor reading the reservation's history sees the oversell in the
		# same place as the decision it belonged to.
		details.update(overbooking_evidence(checks, overbooking_reason))
		details["business_date"] = str(get_business_date(doc.property))

	_transition(doc, CONFIRMED, reason=reason, details=details)

	frappe.db.set_value(
		RESERVATION_DOCTYPE,
		reservation,
		{"confirmed_on": now_datetime(), "confirmed_by": frappe.session.user},
		update_modified=False,
	)

	return CONFIRMED


def guarantee(reservation: str, guarantee_type: str, *, reason: str | None = None) -> str:
	"""Move a confirmed reservation to guaranteed once its guarantee is in place."""
	if not guarantee_type or guarantee_type == "None":
		throw(_("A guarantee type is required to guarantee a reservation."))

	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	frappe.db.set_value(
		RESERVATION_DOCTYPE,
		reservation,
		{"guarantee_type": guarantee_type, "guaranteed_on": now_datetime()},
		update_modified=False,
	)

	return _transition(doc, GUARANTEED, reason=reason, details={"guarantee_type": guarantee_type})


def make_tentative(reservation: str, *, reason: str | None = None) -> str:
	doc = frappe.get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	return _transition(doc, TENTATIVE, reason=reason)


def waitlist(reservation: str, *, reason: str | None = None) -> str:
	doc = frappe.get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	return _transition(doc, WAITLISTED, reason=reason)


def _assert_no_unfinished_stay(reservation: str, target: str):
	"""Refuse to end a booking that has already been partly consumed.

	`stays.check_in` moves a Reservation to Checked In only once *every* room
	line is in house - a multi-room booking is not checked in until the whole
	party arrives - so a three-room booking with two guests upstairs still reads
	`Confirmed` or `Guaranteed` at the header, and `TRANSITIONS` allows both
	`Cancelled` and `No Show` from either. The state machine alone therefore
	permits ending a booking whose guests are in their rooms: their Stays would
	stay `In House` with their folios open against a cancelled reservation,
	`_release_corporate_credit` would hand back credit for nights actually
	consumed, and `_propagate_status` would stamp the ending status onto every
	room line, so the party vanishes from the arrivals board while the people are
	still in the hotel.

	**Any Stay that is not `Closed` blocks.** `Expected`, `In House`, `Due Out`
	and `Checked Out` all mean a room of this booking has been taken up:
	`In House` and `Due Out` have a guest in the room; `Checked Out` has one who
	slept there and whose folio is not yet put to bed; `Expected` means a
	check-in is in flight (`check_in` inserts the Stay as `Expected` and moves it
	to `In House` in the same transaction), and `check_in` itself already treats
	anything other than `Closed` as "this room line is already checked in".
	`Closed` is the one status that is history rather than a live stay - a
	finished, settled stay - so a booking whose stays are all closed may still be
	cancelled.

	Read with `lock_and_find`, and called under the Reservation row lock the
	caller already holds. Both halves matter (see `base.lock_document`, N1): the
	lock is what stops `stays.check_in` - whose first act is to lock this same
	Reservation row - from creating a Stay between this check and the transition,
	and the *locking* read is what makes the answer current instead of answered
	from a snapshot this transaction opened before a check-in that has since
	committed. When nothing matches, the locking read holds the gap, so the
	answer stays true until this transaction ends.
	"""
	# Imported here, not at module scope: `services.stays` imports this module,
	# and an eager import either way round would be circular.
	from hospitality_pms.services import stays as stay_service

	live = lock_and_find(
		stay_service.STAY_DOCTYPE,
		{"reservation": reservation, "stay_status": ("!=", stay_service.CLOSED)},
		["name"],
	)

	if not live:
		return

	message = (
		_(
			"Reservation {0} has rooms that are already checked in, so it cannot be recorded "
			"as a no-show. Check those stays out, or resolve the room lines that arrived, first."
		)
		if target == NO_SHOW
		else _(
			"Reservation {0} has rooms that are already checked in, so it cannot be cancelled. "
			"Check those stays out, or resolve the room lines that arrived, first."
		)
	)

	throw(message.format(reservation), exc=InvalidStateTransitionError)


def cancel(reservation: str, reason: str, *, waive_charge: bool = False) -> dict:
	"""Cancel a reservation and compute what the policy charges.

	A cancellation inside the free window, or after arrival, is a policy
	exception and needs a manager (Roles Matrix section 3).
	"""
	if not reason or not reason.strip():
		throw(_("A reason is required to cancel a reservation."))

	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	# Refused here as well as inside _transition, so a cancellation that has
	# already lost the race never releases credit it never consumed.
	assert_transition(doc.reservation_status, CANCELLED, TRANSITIONS, _("Reservation"))

	# Beside the transition guard, and inside the lock `lock_and_get_doc` above
	# took: a check made before the lock would be decided against a snapshot a
	# concurrent check-in can commit into, and the room could be occupied by the
	# time this transaction cancelled the booking. Held here, the check-in waits
	# on the Reservation row until this transaction ends, so no Stay can appear
	# between this refusal and `_transition`. Before the credit release, too, so a
	# refused cancellation moves no money.
	_assert_no_unfinished_stay(reservation, CANCELLED)

	first_night = _first_night_amount(doc)

	charge = get_cancellation_charge(
		flt(doc.total_amount),
		doc.cancellation_policy,
		doc.arrival_date,
		first_night_amount=first_night,
	)

	if charge and waive_charge:
		require_role(CANCEL_OVERRIDE_ROLES)
		charge = 0.0

	if getdate(doc.arrival_date) < getdate(get_business_date(doc.property)):
		# Cancelling a reservation whose arrival has already passed is an
		# exception, not routine housekeeping.
		require_role(CANCEL_OVERRIDE_ROLES)

	# Releasing before the transition so a failure here aborts the cancellation
	# rather than leaving the booking cancelled with its credit still consumed.
	_release_corporate_credit(doc, charge)

	_transition(doc, CANCELLED, reason=reason, details={"cancellation_charge": charge})

	frappe.db.set_value(
		RESERVATION_DOCTYPE,
		reservation,
		{
			"cancelled_on": now_datetime(),
			"cancelled_by": frappe.session.user,
			"cancellation_reason": reason.strip(),
			"cancellation_charge": charge,
		},
		update_modified=False,
	)

	return {"status": CANCELLED, "cancellation_charge": charge}


def mark_no_show(reservation: str, *, reason: str | None = None) -> dict:
	"""Record a no-show, which the Night Audit normally raises.

	Only allowed once the arrival date has passed relative to the property's
	business date - a guest cannot be a no-show on a day that has not ended.
	"""
	require_role(NO_SHOW_ROLES)

	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)

	business_date = get_business_date(doc.property)

	if getdate(doc.arrival_date) > getdate(business_date):
		throw(
			_("Reservation {0} arrives on {1}, which is after the business date {2}.").format(
				reservation, getdate(doc.arrival_date), getdate(business_date)
			),
			exc=InvalidStateTransitionError,
		)

	# Under the lock `lock_and_get_doc` above took, for the reason given at the
	# same call in `cancel`: a guest who has physically arrived is not a no-show,
	# and only a check made while this transaction holds the Reservation row can
	# still be true when `_transition` runs.
	_assert_no_unfinished_stay(reservation, NO_SHOW)

	first_night = _first_night_amount(doc)

	charge = get_cancellation_charge(
		flt(doc.total_amount),
		doc.no_show_policy,
		doc.arrival_date,
		first_night_amount=first_night,
	)

	# A no-show ends the booking exactly as a cancellation does, so it must return
	# the corporate credit the confirmation consumed, less the no-show charge the
	# company still owes - the same release cancel() performs. It has to run here,
	# BEFORE the transition, because _release_corporate_credit early-returns once
	# the status leaves the holding states; the sanctioned follow-up NO_SHOW ->
	# CANCELLED then releases nothing (the status is already No Show), which left
	# the credit consumed forever and eventually locked the corporate account out
	# of every future booking for money the hotel will never bill.
	_release_corporate_credit(doc, charge)

	_transition(doc, NO_SHOW, reason=reason, details={"no_show_charge": charge})

	frappe.db.set_value(
		RESERVATION_DOCTYPE,
		reservation,
		{
			"no_show_on": now_datetime(),
			"cancellation_charge": charge,
			# A no-show ends a booking just as a cancellation does, so it records
			# who and why in the same fields. Without this the actor and reason
			# existed only in the reservation log, which reporting cannot read
			# without crossing that log's own permission boundary.
			"cancelled_by": frappe.session.user,
			"cancellation_reason": reason or _("Recorded as a no-show"),
		},
		update_modified=False,
	)

	return {"status": NO_SHOW, "no_show_charge": charge}



def deposit_allocation(doc) -> dict[str, float]:
	"""Split one deposit across the rooms that will carry it.

	A deposit belongs to a booking, and a booking is now one row per physical
	room, each of which opens its own folio at check-in. Crediting the whole
	deposit to each of those folios credited a 300 deposit three times (P1-6).

	The split is proportional to what each room is worth, so the guest who
	booked a suite and a single does not see the deposit halved between them,
	and it is computed the same way every time it is asked for: the shares are
	rounded down to currency precision and the last row takes the remainder, so
	the parts add back up to the deposit exactly rather than to 99.99.

	A pure function of what is stored. Nothing here decides *when* a share is
	posted; that is check-in's business, one room at a time.
	"""
	deposit = flt(doc.deposit_received)
	lines = list(doc.rooms)

	if deposit <= 0 or not lines:
		return {line.name: 0.0 for line in lines}

	precision = frappe.get_precision("Folio Payment", "amount") or 2
	weights = [flt(line.total_amount) for line in lines]
	basis = sum(weights)

	if basis <= 0:
		# Nothing priced yet - an equal split is the only defensible answer.
		weights = [1.0] * len(lines)
		basis = float(len(lines))

	allocation: dict[str, float] = {}
	running = 0.0

	for line, weight in zip(lines[:-1], weights[:-1], strict=True):
		share = flt(deposit * weight / basis, precision)
		allocation[line.name] = share
		running += share

	# The last row absorbs the rounding remainder, which is what makes the
	# aggregate exact rather than approximately right.
	allocation[lines[-1].name] = flt(deposit - running, precision)

	return allocation


def deposit_credited(reservation: str) -> float:
	"""What the folios of this booking have already been credited.

	Reversed payments are excluded. A reversal is how a deposit credited to the
	wrong folio is undone, and counting it as still credited made the remaining
	budget in `deposit_share` too small - so the room it should have gone to was
	capped out of its share and the guest was asked for the deposit twice.
	"""
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(payment.amount), 0)
			from `tabFolio Payment` payment
			inner join `tabGuest Folio` folio on folio.name = payment.parent
			where folio.reservation = %s
			  and payment.payment_type = %s
			  and payment.is_reversed = 0
			""",
			(reservation, DEPOSIT_PAYMENT_TYPE),
		)[0][0]
	)


def deposit_share(doc, room_line: str) -> float:
	"""One room's share of the deposit, capped by what is left to credit.

	The allocation alone would already sum to the deposit, but it is derived
	from line values that can move after a room has been checked in - a stay
	shortened on Tuesday re-prices its row. The cap makes the invariant hold
	regardless: the folios of one booking can never be credited more than the
	deposit that was actually received.

	Read under the reservation lock the caller already holds, so two rooms
	arriving at once cannot both see the same room left in the budget.
	"""
	share = flt(deposit_allocation(doc).get(room_line, 0))

	if share <= 0:
		return 0.0

	remaining = flt(doc.deposit_received) - deposit_credited(doc.name)

	return max(min(share, remaining), 0.0)


def normalise_room_lines(doc) -> bool:
	"""Turn a booked quantity into one operational row per physical room.

	A `Reservation Room` row is the unit everything operational hangs off: it
	holds one assigned room, produces one Stay, opens one Folio and takes one
	share of the deposit. A row saying `rooms = 3` can do exactly one of each,
	which is why the second and third rooms of a three-room booking could never
	be checked in, and why the whole booking was marked Checked In as soon as
	the first guest arrived (P1-6).

	Splitting rather than teaching check-in to consume a row three times: the
	quantity was the wrong representation to run a hotel from, and every
	downstream fix would have had to know about it.

	Returns whether anything changed, so the caller only saves when it must.
	Idempotent - a booking already at one room per row is left alone.

	Pricing is untouched by design. `price_reservation` computes a line's total
	as the per-room total multiplied by its quantity, so three rows of one come
	to exactly what one row of three did; the re-price on save proves it rather
	than assuming it.
	"""
	extra = []
	changed = False

	for line in doc.rooms:
		quantity = max(int(line.rooms or 1), 1)

		if quantity == 1:
			continue

		changed = True
		line.rooms = 1

		for _copy in range(quantity - 1):
			values = {
				field: line.get(field)
				for field in (
					"room_type",
					"arrival_date",
					"departure_date",
					"property",
					"reservation_status",
					"rate_plan",
					"adults",
					"children",
					"extra_beds",
					"room_rate",
					"special_requests",
				)
			}
			# Deliberately not copied: a physical room belongs to one row, and
			# duplicating an assignment would promise one room to two guests.
			extra.append({**values, "rooms": 1, "assigned_room": None})

	for values in extra:
		doc.append("rooms", values)

	return changed


def _corporate_account(doc) -> str | None:
	"""The corporate account a reservation bills to, if it resolves to a real one.

	`corporate_account` is a Data field rather than a Link, so a typo or a
	stale name is possible. An unresolvable value is ignored rather than
	blocking the booking: refusing to sell a room because a reference is stale
	would be a worse failure than not tracking the credit.
	"""
	name = (doc.get("corporate_account") or "").strip()

	if not name or not frappe.db.exists("Corporate Account", name):
		return None

	return name


def _consume_corporate_credit(doc):
	"""Draw the reservation's value against its corporate account's credit."""
	account = _corporate_account(doc)

	if not account:
		return

	from hospitality_pms.services.corporate import consume_credit

	consume_credit(account, flt(doc.total_amount), reservation=doc.name)


def _release_corporate_credit(doc, charge: float = 0.0):
	"""Return credit when a booking is cancelled.

	Only the amount that will not now be billed is released: a cancellation
	charge is still owed by the account, so it stays consumed.

	Nothing is released for a booking that never consumed anything. Credit is
	drawn at confirmation, so a Draft, Tentative or Waitlisted reservation is
	not holding any - and releasing against it wrote a `Credit released`
	movement for money that was never taken, leaving the ledger showing a
	release with no matching consumption.
	"""
	if doc.reservation_status not in HOLDING_STATES:
		return

	account = _corporate_account(doc)

	if not account:
		return

	from hospitality_pms.services.corporate import release_credit

	releasable = max(flt(doc.total_amount) - flt(charge), 0.0)

	if releasable <= 0:
		return

	release_credit(account, releasable, reason=_("Reservation {0} cancelled").format(doc.name))


# ---------------------------------------------------------------------------
# Room assignment
# ---------------------------------------------------------------------------


def assign_room(reservation: str, room_line: str, room: str, *, allow_unready: bool = False) -> str:
	"""Assign a specific room to a reservation line.

	Locks the room so two reservations cannot be given the same room, and
	re-checks assignability under that lock.

	Only while the booking is still expecting the guest. Once a line is checked
	in, `Reservation Room.assigned_room` and `Stay.room` are two records of the
	same physical fact, and this function writes with `frappe.db.set_value` - past
	the controller guard and past validation - so it would move the guest on paper
	and leave the room's occupancy, its housekeeping state and the folio pointing
	at the room they are actually in. Moving a guest who has arrived is
	`stays.change_room`, which moves all four together.
	"""
	# A locking read, because the status and the room lines below are decided on
	# and both can have moved while this request queued for the lock (N1). The
	# room's own property and type are read plainly further down: that is
	# configuration an administrator changes, never contended front-desk state.
	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	line = next((row for row in doc.rooms if row.name == room_line), None)
	if not line:
		throw(_("Room line {0} does not belong to reservation {1}.").format(room_line, reservation))

	_assert_modifiable(doc, INTERVAL_EDITABLE_STATES, _("Room assignment"))
	_assert_line_not_in_stay(
		room_line,
		_("Move the guest with a room change instead, so the room, its occupancy and the folio move with them."),
	)

	lock_document("Hotel Room", room)

	room_property, room_type = frappe.db.get_value("Hotel Room", room, ["property", "room_type"])

	if room_property != doc.property:
		throw(_("Room {0} belongs to another property.").format(room))

	if room_type != line.room_type:
		throw(
			_("Room {0} is a {1}, but this line is for {2}.").format(room, room_type, line.room_type),
		)

	_assert_room_free(
		room,
		line.arrival_date,
		line.departure_date,
		exclude_line=room_line,
		arrival_time=line.get("arrival_time"),
		departure_time=line.get("departure_time"),
	)
	assert_assignable(room, allow_unready_housekeeping=allow_unready, arrival=line.arrival_date)

	# And whether anybody is actually *in* it, which neither of the two lines
	# above can answer (HPMS-UAT-16.7.5-B01).
	#
	# `_assert_room_free` asks the inventory table, whose overlap test is
	# departure-exclusive, so the guest whose departure date is today raises no
	# clash against an assignment arriving today. `assert_assignable` asks the
	# room's own flag, which the Night Audit moves to `Due Out` for every
	# departing guest and which a later guest's checkout can leave saying
	# `Vacant`. Both were satisfied for room 402 while a guest was asleep in it.
	#
	# Asked last, and asked under the Hotel Room lock taken above, so the answer
	# cannot go stale between this check and the write below. A **current** read
	# for the same reason `_assert_room_free` uses one: the lock serialises this
	# against a rival assignment but does not refresh what this transaction can
	# see, and a plain read here would be answered from the view opened before
	# the rival committed its check-in (N1).
	#
	# Scoped to `arrival` the same way `assert_assignable` is. Pre-assigning next
	# Tuesday's arrival to a room that is full tonight is ordinary front-office
	# work; whether tonight's guest will still be there on Tuesday is answered by
	# their own inventory interval, and a full house must still be able to
	# pre-assign.
	if occupancy_applies(room, line.arrival_date):
		assert_room_unoccupied(room, property_name=doc.property)

	frappe.db.set_value("Reservation Room", room_line, "assigned_room", room, update_modified=False)

	frappe.get_doc(
		{
			"doctype": RESERVATION_LOG_DOCTYPE,
			"property": doc.property,
			"reservation": reservation,
			"from_status": doc.reservation_status,
			"to_status": doc.reservation_status,
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": _("Room {0} assigned").format(room),
			"details": json.dumps({"room": room, "room_line": room_line}),
		}
	).insert(ignore_permissions=True)

	return room


def _assert_room_free(
	room: str,
	arrival,
	departure,
	exclude_line: str | None = None,
	*,
	remedy: str | None = None,
	arrival_time=None,
	departure_time=None,
):
	"""Refuse a room already promised to an overlapping stay.

	`remedy` is appended to the refusal by callers for whom the way out is not
	obvious. Assignment can simply pick another room; a *move* cannot, because the
	room is already promised to this line and dropping it silently could break a
	connecting pair or a VIP's usual room.

	Overlap is `arrival < other.departure AND departure > other.arrival`,
	which correctly allows a same-day turnover: one guest departs on the 12th
	and another arrives on the 12th.

	Scoped to the *line*, not the reservation. Excluding the whole reservation
	was safe only while a booking held one row; once a three-room booking is
	three rows, excluding the parent would let two of its own rooms be given
	the same physical room and nobody would notice until both guests arrived.

	`Reservation Room` is the authoritative interval - a checked-in guest is
	still counted here, because check-in does not move a booking out of the
	holding states.
	"""
	filters = {
		"assigned_room": room,
		"reservation_status": ("in", HOLDING_STATES),
		"arrival_date": ("<", getdate(departure)),
		"departure_date": (">", getdate(arrival)),
	}

	if exclude_line:
		filters["name"] = ("!=", exclude_line)

	# A **current** read, not a snapshot read. `lock_document` on the room
	# serialises two agents clicking the same room, but locking does not
	# refresh what this transaction can see: a plain `get_all` is answered
	# from the snapshot opened before the rival committed its assignment, so
	# both agents find the room free and both take it. This is the N1 pattern
	# Wave 1 established, applied to the assignment interval.
	clash = lock_and_find("Reservation Room", filters, ["parent", "name"])
	if not clash:
		# the hours of a day-use or hourly stay, on this day or on one of this stay's days
		from hospitality_pms.services.short_stay import clashing_rooms

		property_name = frappe.db.get_value("Hotel Room", room, "property")
		if room in clashing_rooms(
			property_name,
			arrival,
			departure,
			arrival_time=arrival_time,
			departure_time=departure_time,
			exclude_line=exclude_line,
			current=True,
		):
			throw(
				_("Room {0} is held by another stay during these hours. {1}").format(room, remedy or "").strip(),
				exc=HospitalityPMSError,
			)

	if clash:
		# Two whole templates rather than one message with a sentence appended to
		# it: a translator needs the complete sentence, and Arabic is a
		# first-release requirement.
		message = (
			_("Room {0} is already assigned to reservation {1} for these dates. {2}").format(
				room, clash["parent"], remedy
			)
			if remedy
			else _("Room {0} is already assigned to reservation {1} for these dates.").format(
				room, clash["parent"]
			)
		)

		throw(message, exc=HospitalityPMSError)


# ---------------------------------------------------------------------------
# The inventory chain
# ---------------------------------------------------------------------------
#
# `Reservation Room` is the single authoritative record of what is sold. It is
# what `availability._sold_by_night` counts, what `_assert_room_free` checks and
# what confirmation reduces. A Stay is the operational face of one of those
# rows, never a second inventory holder - counting both would count a
# checked-in guest twice.
#
# Everything that changes an interval therefore locks the same chain in the same
# order. One order, so two operations touching overlapping rows queue rather
# than deadlock:
#
#     1. Reservation        the aggregate whose dates are recalculated
#     2. Reservation Room   the interval itself
#     3. Room Type          the type-level capacity check
#     4. Hotel Room         the physical room
#     5. Stay               the operational record
#
# Reading a row to decide *what* to lock is fine; every value a decision is made
# from is read back from the locking read (Wave 1, N1).

RESERVATION_ROOM_DOCTYPE = "Reservation Room"
RESERVATION_RATE_LINE_DOCTYPE = "Reservation Rate Line"
DEPOSIT_PAYMENT_TYPE = "Deposit"


def lock_inventory_line(room_line: str) -> dict:
	"""Lock one Reservation Room row and return its current state.

	The interval, and everything a move has to decide on beside it: the quantity
	the line carries, the room promised to it, the plan its restrictions are
	evaluated against, and the rate a move must preserve. All from the one locking
	read, because a second plain read of a locked row is answered from the
	pre-lock snapshot (N1).
	"""
	return lock_and_read(
		RESERVATION_ROOM_DOCTYPE,
		room_line,
		[
			"name",
			"parent",
			"room_type",
			"rooms",
			"arrival_date",
			"departure_date",
			"assigned_room",
			"rate_plan",
			"room_rate",
			"total_amount",
			"nights",
		],
	)


def set_line_interval(room_line: str, *, arrival=None, departure=None):
	"""Move an inventory interval, and keep the reservation's own dates true.

	The only supported way to change what a booking holds. A Stay's departure
	date on its own is a display value; this is the row availability counts.
	"""
	values = {}

	if arrival:
		values["arrival_date"] = getdate(arrival)

	if departure:
		values["departure_date"] = getdate(departure)

	if not values:
		return

	current = frappe.db.get_value(
		RESERVATION_ROOM_DOCTYPE, room_line, ["parent", "arrival_date", "departure_date"], as_dict=True
	)

	values["nights"] = len(
		nights_between(
			values.get("arrival_date", current["arrival_date"]),
			values.get("departure_date", current["departure_date"]),
		)
	)

	frappe.db.set_value(RESERVATION_ROOM_DOCTYPE, room_line, values, update_modified=False)

	refresh_header_dates(current["parent"])


def refresh_header_dates(reservation: str):
	"""Recompute the reservation's own dates from the rooms it holds.

	The header is a summary of its children, not a copy of one of them. Setting
	it from whichever room was just changed would shorten a whole booking
	because one guest of three left early.
	"""
	span = frappe.db.sql(
		"""
		select min(arrival_date) as arrival, max(departure_date) as departure
		from `tabReservation Room`
		where parent = %s
		""",
		reservation,
		as_dict=True,
	)[0]

	if not span["arrival"] or not span["departure"]:
		return

	frappe.db.set_value(
		RESERVATION_DOCTYPE,
		reservation,
		{
			"arrival_date": getdate(span["arrival"]),
			"departure_date": getdate(span["departure"]),
			"nights": len(nights_between(span["arrival"], span["departure"])),
		},
		update_modified=False,
	)


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------
#
# `Reservation._guard_holding_immutability` refuses a document save that would
# change a booking's dates, its room lines, or a line's type or assignment once
# the booking holds inventory, and its docstring names what was missing: "a
# service operation ("move/rebook") that does not exist yet". This is that
# operation, and the four editable-state line operations beside it.
#
# These are not an exemption from the guard. Each one re-checks under the lock
# chain first and then writes the guarded columns with `frappe.db.set_value`,
# which runs no validation - the same property `set_line_interval` and
# `_transition` already rely on. The guard keeps standing in front of every
# other route, which is the point: a generic `doc.save()` is still refused.
#
# Nothing here re-prices a booking that holds inventory. `_is_editable` already
# decided that question - past a holding state the guest has been quoted these
# amounts, and re-running today's rate grid would silently change what they
# owe - so where a re-price is legitimate the document is saved and the
# *controller* does it. This module never calls `price_reservation` itself.

#: Statuses in which a booking is still live: it either holds inventory or is
#: on its way to doing so. Deliberately the complement of the controller's
#: TERMINAL_STATES within `TRANSITIONS`, and asserted to be exactly that by
#: `tests/test_reservation_modification.py` - the constant cannot be imported
#: from the controller, which imports this module.
LIVE_STATES = (DRAFT, TENTATIVE, WAITLISTED, CONFIRMED, GUARANTEED, CHECKED_IN)

#: Statuses in which a room line's interval may still be moved.
#:
#: Checked In is absent, and that is the substance of the rule rather than
#: caution: once a guest is in the room the dates belong to their Stay, and
#: `stays.extend_stay` / `stays.shorten_stay` are the operations that move
#: them - they move the Stay *and* this line, together. Moving the line from
#: here would leave the two disagreeing, which is P1-5 from the other side.
INTERVAL_EDITABLE_STATES = (DRAFT, TENTATIVE, WAITLISTED, CONFIRMED, GUARANTEED)

#: Statuses in which a room line may be added, removed, re-typed or re-planned.
#:
#: Exactly the statuses `Reservation._is_editable()` accepts, because these are
#: the operations the controller refuses beyond them - and because each one
#: re-prices, which is only safe while the snapshot is still a working draft.
#: Past that, "cancel and rebook" is the sanctioned path (Workflow Matrix
#: section 2) and no service here overrides it.
LINE_EDITABLE_STATES = (DRAFT, TENTATIVE, WAITLISTED)

#: The only fields `update_reservation_details` may write.
#:
#: An allow list rather than a deny list, following `api.guests
#: .GUEST_WRITABLE_FIELDS`: a field added to the Reservation DocType in a later
#: build is not silently writable from the workspace just because nobody
#: remembered to exclude it. Everything absent from it is absent for a reason -
#: `arrival_date`, `departure_date`, `property` and `guest` are the controller's
#: guarded columns and move only through `change_line_interval`; the totals, the
#: rate snapshot and the deposit are derived; `reservation_status` belongs to
#: `_transition`; and the lifecycle stamps are written by the operations that
#: earn them.
#:
#: `internal_notes` is the reservation's notes field. `market_segment` exists on
#: the DocType (a Data column) and is included. `guarantee_type` is listed but
#: never written here - see `update_reservation_details`.
DETAIL_WRITABLE_FIELDS = (
	"booking_source",
	"market_segment",
	"arrival_time",
	"special_requests",
	"internal_notes",
	"guarantee_type",
)

#: The one field in that list that a different service owns.
DELEGATED_DETAIL_FIELD = "guarantee_type"

ROOM_DOCTYPE = "Hotel Room"


def _assert_modifiable(doc, allowed: tuple[str, ...], what: str):
	"""Refuse an edit the booking's current status does not permit.

	`doc` must have been loaded by a locking read, because this is the archetypal
	check-then-act: a booking cancelled by another till while this request queued
	for the lock is still `Confirmed` in a snapshot opened before that commit, and
	a status read from there would let the cancelled booking be edited (N1).
	"""
	if doc.reservation_status in allowed:
		return

	throw(
		_("{0} cannot be changed while reservation {1} is {2}.").format(
			what, doc.name, _(doc.reservation_status)
		),
		exc=InvalidStateTransitionError,
	)


def log_room_type_move(
	reservation: str,
	*,
	room_line: str,
	from_type: str,
	to_type: str,
	assigned_room: str,
):
	"""Record that a room move carried an inventory line onto another room type.

	Called by `stays.change_room`, which is the one operation that may move a
	guest who has already arrived and so the one that can change what a booking
	holds without going through any of the reservation modification services.
	Until 16.7.3 it wrote nothing here at all: the booking's own history showed a
	line silently changing type, with the reason recorded only on the Stay.

	Written from scalars rather than through `_log_modification`, because
	`change_room` holds the Reservation Room row and the Hotel Rooms but
	deliberately never locks the Reservation (see the chain comment at the top of
	`change_room`). Loading the reservation document here to satisfy a logging
	helper would add a read this path has no other reason to take, and the
	status is wanted only as the descriptive context an audit row carries.

	`from`/`to` name the room types; the keys published by the workspace history
	endpoint are the allow-listed `room_line`, `room_type` and `assigned_room`.
	"""
	status = frappe.db.get_value(RESERVATION_DOCTYPE, reservation, ["property", "reservation_status"], as_dict=True)

	if not status:
		return

	frappe.get_doc(
		{
			"doctype": RESERVATION_LOG_DOCTYPE,
			"property": status["property"],
			"reservation": reservation,
			"from_status": status["reservation_status"],
			"to_status": status["reservation_status"],
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": _("Guest moved to a room of another type"),
			"details": json.dumps(
				{
					"room_line": room_line,
					"room_type": to_type,
					"assigned_room": assigned_room,
					"from": from_type,
					"to": to_type,
				},
				default=str,
			),
		}
	).insert(ignore_permissions=True)


def _log_modification(doc, reason: str, details: dict):
	"""Record a change that is not a state transition.

	The shape `assign_room` already writes: `from_status` and `to_status` both
	the booking's current status, because a modification moves no state. Written
	to `Reservation Log` rather than to a table of its own so an auditor reading
	a booking's history finds an edit in the same place as the transitions it
	sits between - and so the ten-year retention that log already carries covers
	it (SAS section 8).

	Actor, timestamp, before/after and reason, as CLAUDE.md requires of a
	sensitive action.
	"""
	frappe.get_doc(
		{
			"doctype": RESERVATION_LOG_DOCTYPE,
			"property": doc.property,
			"reservation": doc.name,
			"from_status": doc.reservation_status,
			"to_status": doc.reservation_status,
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": reason,
			"details": json.dumps(details, default=str),
		}
	).insert(ignore_permissions=True)


def _resync_if_editable(reservation: str):
	"""Let the controller re-derive what it owns, while it still may.

	A service that moved an interval has written the one column availability
	counts. The rest of the snapshot - each line's nights and value, the
	booking's totals, the rate lines - belongs to `Reservation.validate`, and
	whether it may be recomputed at all is `Reservation._is_editable`'s decision
	and not this module's.

	So the document is saved and the controller decides. For a Draft, Tentative
	or Waitlisted booking it re-prices, which is what makes an edited quote add
	up. For a Confirmed or Guaranteed one nothing is written at all - not even a
	save whose only effect would be to touch `modified` - so the booked rate,
	the line totals and therefore `deposit_allocation` come through a date change
	untouched.

	Re-read under the lock rather than reusing the caller's document: the caller's
	copy still holds the interval as it was before `set_line_interval` wrote it.
	"""
	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)

	if not doc._is_editable():
		return

	doc.save(ignore_permissions=True)


# -- details ----------------------------------------------------------------


def _clean_detail(doc, fieldname: str, value):
	"""Coerce one inbound detail value to what its column actually holds.

	The frontend posts strings. A `Select` in particular has to be checked here
	rather than left to the ORM, because `guarantee_type` is delegated to
	`guarantee()`, which writes with `frappe.db.set_value` and so runs no field
	validation at all - an unchecked value would be stored verbatim.
	"""
	field = doc.meta.get_field(fieldname)

	if value is None:
		return None

	if field.fieldtype == "Time":
		try:
			return get_time(value)
		except Exception:
			throw(_("{0} is not a valid time.").format(value))

	text = str(value).strip()

	if field.fieldtype == "Select":
		options = [option.strip() for option in (field.options or "").split("\n") if option.strip()]

		if text and text not in options:
			throw(
				_("{0} is not one of the permitted values for {1}.").format(
					text, _(field.label or fieldname)
				)
			)

	# An empty string is a field being cleared, which is a legitimate edit;
	# stored as NULL so "no value" has one representation.
	return text or None


def update_reservation_details(reservation: str, changes: dict) -> dict:
	"""Correct the non-inventory details of a booking, in one transaction.

	The fields here are the ones `_guard_holding_immutability` does *not* guard -
	who is coming, how to reach them, what they asked for - so the document path
	is the correct one: the controller owns the derived fields and a
	`db.set_value` shortcut would skip them.

	What the controller cannot own is *which* fields a client may name. It sees a
	document, not a request, so `DETAIL_WRITABLE_FIELDS` is enforced here and a
	key outside it is refused rather than dropped: a caller naming a field on an
	existing booking wants it changed, and saving something other than what was
	asked for is worse than refusing.

	One transaction, in the savepoint sense as well as the request sense. The
	guarantee delegation below is a second write that can legitimately fail, and
	a service caller that catches the failure must not be left with half of an
	edit committed.
	"""
	changes = frappe.parse_json(changes) if isinstance(changes, str) else changes

	if not isinstance(changes, dict) or not changes:
		throw(_("No changes were supplied."))

	refused = sorted(set(changes) - set(DETAIL_WRITABLE_FIELDS))

	if refused:
		throw(
			_("These fields cannot be changed here: {0}.").format(", ".join(refused)),
			exc=PermissionDeniedError,
		)

	# Locked and read together, because every guard below decides on the
	# reservation as it stands now rather than as it stood when this request
	# started (N1).
	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	_assert_modifiable(doc, LIVE_STATES, _("Reservation details"))

	requested_guarantee = (
		_clean_detail(doc, DELEGATED_DETAIL_FIELD, changes[DELEGATED_DETAIL_FIELD])
		if DELEGATED_DETAIL_FIELD in changes
		else None
	)

	applied: dict[str, dict] = {}

	for fieldname in DETAIL_WRITABLE_FIELDS:
		if fieldname == DELEGATED_DETAIL_FIELD or fieldname not in changes:
			continue

		value = _clean_detail(doc, fieldname, changes[fieldname])

		# Compared as text so a Time coerced from "15:30" and the stored value
		# do not read as a change on every save.
		if str(doc.get(fieldname) or "") == str(value or ""):
			continue

		applied[fieldname] = {"from": doc.get(fieldname), "to": value}
		doc.set(fieldname, value)

	status = doc.reservation_status

	with transaction():
		if applied:
			doc.save(ignore_permissions=True)
			_log_modification(doc, _("Reservation details updated"), {"changed": applied})

		# Ordered deliberately: the plain fields first, then the guarantee. The
		# other way round, `guarantee()` would have moved the status in the
		# database while this document still held the old one, and
		# `_guard_status_change` would refuse the save that followed.
		if requested_guarantee:
			status = _delegate_guarantee(doc, requested_guarantee)

	return {"reservation": reservation, "status": status, "changed": sorted(applied)}


def _delegate_guarantee(doc, guarantee_type: str) -> str:
	"""Hand a guarantee change to the service that owns it.

	`guarantee_type` looks like a detail and is not one. Recording a guarantee is
	a state change - `reservations.guarantee` writes the field, stamps
	`guaranteed_on` and transitions the booking to Guaranteed - and a booking
	whose guarantee type says "Credit Card" while its status still says
	"Confirmed" is exactly the inconsistency that service exists to prevent. So
	this never writes the field; it calls the service, or refuses.

	Refused rather than delegated when the booking cannot reach Guaranteed, which
	by `TRANSITIONS` means anything other than Confirmed. `assert_transition`
	would refuse it a moment later anyway, but it would say "Reservation cannot
	move from Draft to Guaranteed" to a user who only edited a form field.
	"""
	if guarantee_type == (doc.guarantee_type or None):
		return doc.reservation_status

	if doc.reservation_status != CONFIRMED:
		throw(
			_(
				"A guarantee is recorded on a confirmed reservation through the guarantee "
				"operation, which moves it to {0}. Reservation {1} is {2}."
			).format(_(GUARANTEED), doc.name, _(doc.reservation_status)),
			exc=InvalidStateTransitionError,
		)

	return guarantee(doc.name, guarantee_type, reason=_("Guarantee recorded with a details update"))


# -- the move/rebook operation ----------------------------------------------


def _lock_line_chain(reservation: str, room_line: str, *, room_types: tuple[str, ...] = ()):
	"""Take the inventory locks in the one documented order, then read current.

	Reservation, its room line, every room type the operation touches, and the
	physical room - the chain documented above `lock_inventory_line`.
	`stays._lock_stay_chain` takes the same locks in the same sequence from the
	other end, so a stay extension and a reservation move queue behind each other
	instead of deadlocking.

	The room types are locked in one sorted call rather than one at a time, so a
	move that touches two types cannot cross over another caller holding them the
	other way round (HPMS-DEC-053).

	Returns the reservation document and the line's **current** interval, both
	from locking reads. Every value the caller decides on comes from here.
	"""
	# A plain read, used only to decide what to lock and to prove the line
	# belongs to the reservation the caller named. Both are immutable facts about
	# the row - a child row never changes parent - so a snapshot read is the
	# right answer for them, and everything decided on afterwards is re-read
	# below under the lock.
	parent = frappe.db.get_value(RESERVATION_ROOM_DOCTYPE, room_line, "parent")

	if not parent:
		throw(_("Room line {0} does not exist.").format(room_line), exc=HospitalityPMSError)

	if parent != reservation:
		throw(
			_("Room line {0} does not belong to reservation {1}.").format(room_line, reservation),
			exc=HospitalityPMSError,
		)

	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	line = lock_inventory_line(room_line)
	lock_room_type(doc.property, [line["room_type"], *room_types])

	if line["assigned_room"]:
		lock_document(ROOM_DOCTYPE, line["assigned_room"])

	return doc, line


def _assert_line_not_in_stay(room_line: str, remedy: str):
	"""Refuse to change a room whose guest has already taken it up.

	`_assert_no_unfinished_stay`'s reasoning, one level down where this operation
	works. **Any Stay that is not `Closed` blocks**, for the reasons set out
	there: `Expected` means a check-in is in flight, `In House` and `Due Out`
	have a guest in the room, `Checked Out` has one who slept there and whose
	folio is not yet put to bed. Only `Closed` is history.

	Scoped to the line rather than to the booking, because that is the scope the
	rule actually has. `check_in` moves a Reservation to Checked In only once
	every line is in house, so a three-room booking with one guest upstairs still
	reads Confirmed - and the two rooms that have not arrived may legitimately
	move their dates while the one that has may not.

	Read with `lock_and_find` under the Reservation row lock the caller holds.
	Both halves matter: the lock stops `check_in` - whose first act is to lock
	this same Reservation row - from creating a Stay between this check and the
	write, and the locking read is what makes the answer current rather than
	answered from a snapshot opened before a check-in that has since committed.

	`remedy` names the supported operation for the caller's own case, because the
	rule is the same for a date change and for a room assignment but the way out
	of it is not.
	"""
	from hospitality_pms.services import stays as stay_service

	live = lock_and_find(
		stay_service.STAY_DOCTYPE,
		{"reservation_room_line": room_line, "stay_status": ("!=", stay_service.CLOSED)},
		["name", "stay_status"],
	)

	if not live:
		return

	throw(
		_("Room line {0} is already checked in as stay {1}. {2}").format(
			room_line, live["name"], remedy
		),
		exc=InvalidStateTransitionError,
	)


def _added_nights(current_arrival, current_departure, new_arrival, new_departure) -> list[tuple]:
	"""The nights a move newly requires, as at most two intervals.

	The nights a line already holds are the booking's by right: they have been
	counted against inventory since it was confirmed, and this line is sitting in
	the very figures a re-check would read. Asking for them again refuses moves
	that are obviously fine - extending the last room in the house by one night
	fails because the house is full on the nights the booking itself is
	occupying.

	What must be checked is the set difference `new - current`. Both are
	intervals, so the difference is at most two of them: whatever the move
	reaches back before the current arrival, and whatever it reaches past the
	current departure.

	    current            [10 ---- 12)
	    departure out      [10 ------------ 14)   added        [12 -- 14)
	    arrival back  [ 8 -------- 12)            added  [ 8 -- 10)
	    both ends     [ 8 ----------------- 14)   added  [ 8 -- 10) [12 -- 14)
	    shifted on              [11 ---- 13)      added           [12 -- 13)
	    shifted clear                    [20 -- 22)  added  [20 -- 22)  (all of it)
	    shortened          [10 -- 11)             added  (nothing)

	`min` and `max` against the *new* bounds are what make the disjoint case fall
	out rather than needing a branch of its own: when the move lands clear of
	where it was, one segment collapses and the other is the whole new interval.

	Nights the move gives up need no check at all. They are released by the write
	itself, and `refresh_header_dates` puts the booking's own span back in step.

	**Do not "improve" this into checking the whole new interval.** Checking the
	difference is not merely cheaper, it is the only form that is *possible* here.
	The added nights are by definition the ones this line does not already hold,
	so its own hold can never be double-counted against it - which is what makes
	the check correct without excluding the line from the availability engine. And
	the engine cannot exclude a line: `_sold_by_night` takes `exclude_reservation`
	and filters on `parent`, so excluding this line would excuse every *other*
	room of the same booking as well, and a three-room booking would be told the
	house had three more rooms than it has. Whole-interval checking would need an
	`exclude_line` that does not exist; the difference needs nothing.
	"""
	segments = []

	before = min(current_arrival, new_departure)
	if new_arrival < before:
		segments.append((new_arrival, before))

	after = max(current_departure, new_arrival)
	if after < new_departure:
		segments.append((after, new_departure))

	return segments


def _line_rate_snapshot(reservation: str, room_line: str) -> list[dict]:
	"""The stored rate rows belonging to one room line, earliest night first.

	The snapshot is one flat table on the reservation - Frappe has no grandchild
	tables - and every row carries `room_line` to say which room it priced.
	"""
	return frappe.get_all(
		RESERVATION_RATE_LINE_DOCTYPE,
		filters={"parent": reservation, "room_line": room_line},
		fields=["name", "rate_date"],
		order_by="rate_date asc",
	)


def _assert_snapshot_can_be_redated(reservation: str, room_line: str, nights: int):
	"""Refuse a move whose stored rate cannot be carried onto the new nights.

	A held booking's rate is preserved rather than recomputed, so the only honest
	way to move its dates is to carry each night's stored amount onto the night it
	now describes. That needs exactly one snapshot row per night the line holds.
	Any other number means the snapshot already disagrees with the line, and moving
	would entrench the disagreement instead of surfacing it.

	No rows at all is a different case and passes: there is nothing to carry, and
	nothing is made worse by the move.
	"""
	rows = _line_rate_snapshot(reservation, room_line)

	if rows and len(rows) != nights:
		throw(
			_(
				"The stored rate for room line {0} covers {1} night(s) but the line holds {2}, so its "
				"dates cannot be corrected. Cancel and rebook."
			).format(room_line, len(rows), nights),
			exc=HospitalityPMSError,
		)


def _redate_rate_snapshot(reservation: str, room_line: str, arrival, departure) -> int:
	"""Carry a preserved rate onto the nights it now describes.

	Bookkeeping, not re-pricing. Every amount is left exactly as it is and only
	`rate_date` moves, in order, onto the new nights - so the guest owes what they
	were quoted, `sum(net_rate)` still reconciles to `total_amount`, and the
	snapshot stops describing an interval they no longer have.

	It matters beyond tidiness. `_first_night_amount` takes the *earliest* row of
	the snapshot, so a booking shifted forward would otherwise have had its First
	Night cancellation charge computed from a night it never held.

	Only reached for a move of equal length, which is what makes the one-for-one
	mapping possible in the first place.
	"""
	rows = _line_rate_snapshot(reservation, room_line)
	nights = nights_between(arrival, departure)

	if not rows or len(rows) != len(nights):
		return 0

	for row, night in zip(rows, nights, strict=True):
		frappe.db.set_value(
			RESERVATION_RATE_LINE_DOCTYPE, row["name"], "rate_date", night, update_modified=False
		)

	return len(rows)


def change_line_interval(
	reservation: str,
	room_line: str,
	arrival=None,
	departure=None,
	allow_overbooking: bool = False,
	reason: str | None = None,
) -> dict:
	"""Move one room line's dates, re-checking inventory under lock.

	The move/rebook operation `_guard_holding_immutability` names as missing. A
	booking's dates are not a form field once it holds inventory: moving them is
	selling nights the house may not have, and the only safe way to do it is to
	lock the chain, ask availability again for what is actually being added, and
	write inside that same lock.

	The booked rate is deliberately untouched. The guest has been quoted these
	amounts, and a date change is not a re-quote; `_resync_if_editable` re-prices
	only a booking that is still a working draft.

	**A held booking may therefore be shifted, not lengthened or shortened.**
	Preserving a two-night price across a three-night stay would leave
	`line.total_amount`, the booking's totals and every `Reservation Rate Line`
	describing an interval the guest does not have - and `_first_night_amount`
	would then compute a First Night cancellation charge from a night they never
	held. Deciding what an extra night costs is a pricing decision and this build
	makes none: a longer or shorter held booking is cancel-and-rebook before
	arrival, and `stays.extend_stay` / `stays.shorten_stay` once the guest is in
	the room. Draft, Tentative and Waitlisted keep full freedom, because the next
	save re-prices them.

	Passing one of `arrival` / `departure` moves that end alone.
	"""
	# Authorised before anything is read or locked, so an override nobody may
	# make costs nothing and blocks nobody (P1-3).
	overbooking_reason = authorise_overbooking(reason) if allow_overbooking else None

	doc, line = _lock_line_chain(reservation, room_line)
	doc.check_permission("write")

	_assert_modifiable(doc, INTERVAL_EDITABLE_STATES, _("Room line dates"))
	_assert_line_not_in_stay(
		room_line,
		_("Extend or shorten the stay instead, so the stay and the inventory it holds move together."),
	)

	current_arrival = getdate(line["arrival_date"])
	current_departure = getdate(line["departure_date"])
	new_arrival = getdate(arrival) if arrival else current_arrival
	new_departure = getdate(departure) if departure else current_departure

	if new_departure <= new_arrival:
		throw(
			_("Arrival date {0} must be before departure date {1}.").format(new_arrival, new_departure)
		)

	if (new_arrival, new_departure) == (current_arrival, current_departure):
		throw(
			_("Room line {0} already runs from {1} to {2}.").format(
				room_line, current_arrival, current_departure
			)
		)

	current_nights = len(nights_between(current_arrival, current_departure))
	new_nights = len(nights_between(new_arrival, new_departure))
	is_holding = doc.reservation_status in HOLDING_STATES

	# The length guard. See the docstring: with no re-price available, a changed
	# night count would leave the money describing an interval the guest no longer
	# has.
	if is_holding and new_nights != current_nights:
		throw(
			_(
				"Reservation {0} is {1}, so room line {2} can be moved but not re-priced: {3} night(s) "
				"cannot become {4}. Cancel and rebook before arrival, or extend or shorten the stay "
				"once the guest is in the room."
			).format(reservation, _(doc.reservation_status), room_line, current_nights, new_nights),
			exc=InvalidStateTransitionError,
		)

	if is_holding:
		_assert_snapshot_can_be_redated(reservation, room_line, current_nights)

	# A quantity is how a booking is asked for, not how rooms are run: past
	# confirmation every operational row is one physical room (P1-6). A holding
	# row that still carries a quantity was never normalised, and moving it would
	# quietly re-sell that quantity against a row nothing downstream can operate.
	#
	# A draft line legitimately carries one, though, so the quantity - never a
	# hardcoded 1 - is what the availability check below asks for. `extend_stay`
	# may hardcode 1 because a Stay is always exactly one room; a reservation line
	# carries no such guarantee until `normalise_room_lines` has run.
	quantity = max(int(line["rooms"] or 1), 1)

	if quantity != 1 and is_holding:
		throw(
			_(
				"Room line {0} holds {1} rooms in one row and cannot be moved; it was never "
				"normalised to one row per room."
			).format(room_line, quantity),
			exc=HospitalityPMSError,
		)

	added = _added_nights(current_arrival, current_departure, new_arrival, new_departure)
	business_date = getdate(get_business_date(doc.property))

	checks = []

	for segment_start, segment_end in added:
		# A night that has already been operated cannot be newly acquired: its
		# night audit has run, so no room charge will ever post for it. Only the
		# *added* nights are tested, so a stay already running from before the
		# business date keeps the nights it holds.
		if segment_start < business_date:
			throw(
				_("Reservation {0} cannot take the night of {1}, which is before the business date {2}.").format(
					reservation, segment_start, business_date
				)
			)

		checks.append(
			check_availability(
				doc.property,
				line["room_type"],
				segment_start,
				segment_end,
				rooms=quantity,
				allow_overbooking=allow_overbooking,
				# Under the Room Type lock this move already holds - and
				# therefore from the current row versions, not from the read
				# view this request opened before the caller it queued behind
				# committed its own booking (N1).
				current=True,
			)
		)

	# Restrictions are a property of the interval as a whole, not of the nights
	# being added to it: a min-LOS of three, a closed-to-arrival Friday, a
	# closed-to-departure Sunday and a rate plan that expires next month are all
	# questions about where the stay now starts, ends and runs. Availability said
	# the rooms exist; this says they may be sold on these dates at all.
	#
	# Reachable from nowhere else on this path: `validate_restrictions` runs inside
	# `get_rate_breakdown`, and a held booking is not priced - so before this call
	# a move was the one way into the booking system that never asked.
	#
	# Refused, with no override. Overriding a restriction is a revenue decision
	# with its own authority, and inventing one here would be new authorisation
	# surface. The messages are already written for a human.
	rate_plan = line["rate_plan"] or doc.rate_plan

	if rate_plan:
		validate_restrictions(doc.property, line["room_type"], new_arrival, new_departure, rate_plan)

	# The physical room, over the **whole** new interval rather than the added
	# nights. Room-type capacity and one specific room are different questions,
	# and an overbooking override answers only the first: selling one more room of
	# a type the house does not have is a management decision, while putting two
	# guests in room 101 is not something any override makes true.
	#
	# The interval difference is the right scope for capacity, because the line's
	# own hold is counted in those figures. It is the wrong scope for a promise
	# about one room, which has to hold for every night of the stay - a shift moves
	# the middle as well as the ends. Excluding this line, which must not be
	# treated as its own rival.
	#
	# Refused rather than dropped or kept: a pre-assignment may be a connecting
	# pair or a VIP's usual room, so giving it up is the desk's decision and not
	# this service's.
	if line["assigned_room"]:
		_assert_room_free(
			line["assigned_room"],
			new_arrival,
			new_departure,
			exclude_line=room_line,
			remedy=_("Release the room from this room line before moving its dates."),
		)
		assert_assignable(line["assigned_room"], arrival=new_arrival)

	set_line_interval(room_line, arrival=new_arrival, departure=new_departure)

	# Same nights, same money, corrected dates.
	redated = (
		_redate_rate_snapshot(reservation, room_line, new_arrival, new_departure) if is_holding else 0
	)

	details = {
		"room_line": room_line,
		"room_type": line["room_type"],
		"assigned_room": line["assigned_room"],
		"from": {
			"arrival": str(current_arrival),
			"departure": str(current_departure),
			"nights": current_nights,
		},
		"to": {"arrival": str(new_arrival), "departure": str(new_departure), "nights": new_nights},
		"added_nights": [[str(start), str(end)] for start, end in added],
		# The rate that was preserved rather than recomputed. Recorded because a
		# preserved rate is otherwise untraceable: after the move nothing else on
		# the booking says these figures were quoted for a different interval.
		"preserved_rate": {
			"rate_plan": rate_plan,
			"room_rate": flt(line["room_rate"]),
			"total_amount": flt(line["total_amount"]),
			"rate_lines_redated": redated,
		},
	}

	if overbooking_reason:
		details.update(overbooking_evidence(checks, overbooking_reason))
		details["business_date"] = str(business_date)

	_log_modification(doc, reason or _("Room line dates changed"), details)

	_resync_if_editable(reservation)

	return {
		"reservation": reservation,
		"room_line": room_line,
		"arrival_date": str(new_arrival),
		"departure_date": str(new_departure),
		"added_nights": details["added_nights"],
		"rate_lines_redated": redated,
	}


# -- editable-state line operations -----------------------------------------


def _line_or_throw(doc, room_line: str):
	"""The row of a locked document, or a refusal naming what was asked for."""
	row = next((line for line in doc.rooms if line.name == room_line), None)

	if not row:
		throw(
			_("Room line {0} does not belong to reservation {1}.").format(room_line, doc.name),
			exc=HospitalityPMSError,
		)

	return row


def _plan_prices_room_type(rate_plan: str, room_type: str, property_name: str, on_date) -> bool:
	"""Whether a rate plan can price a room type at all, per the rate service."""
	return any(
		plan["name"] == rate_plan
		for plan in get_applicable_rate_plans(property_name, room_type, on_date)
	)


def add_room_line(
	reservation: str,
	room_type: str,
	arrival,
	departure,
	adults: int = 1,
	children: int = 0,
	rate_plan: str | None = None,
) -> dict:
	"""Add one more room to a booking that is still being put together.

	One room per call, `rooms = 1`, deliberately: a `Reservation Room` row is
	what holds an assignment, produces a Stay, opens a folio and takes a share of
	the deposit, and a row that says three can do exactly one of each (P1-6). A
	party of three rooms is three calls.
	"""
	arrival = getdate(arrival)
	departure = getdate(departure)

	if departure <= arrival:
		throw(_("Arrival date {0} must be before departure date {1}.").format(arrival, departure))

	doc = lock_and_get_doc(RESERVATION_DOCTYPE, reservation)
	doc.check_permission("write")

	_assert_modifiable(doc, LINE_EDITABLE_STATES, _("Room lines"))

	lock_room_type(doc.property, room_type)

	business_date = getdate(get_business_date(doc.property))

	if arrival < business_date:
		throw(
			_("A room cannot be added from {0}, which is before the business date {1}.").format(
				arrival, business_date
			)
		)

	# The incremental demand, which for a booking in one of these statuses is
	# exactly the room being added: an editable booking holds no inventory at all
	# (`HOLDING_RESERVATION_STATES`), so the lines already on it are neither
	# counted against the new room nor double-counted with it. Confirmation is
	# what commits the booking, and `check_demand` re-checks the whole of it there
	# - summed per night, under lock. This check exists so an agent is told now
	# rather than at the end.
	#
	# Current under the Room Type lock all the same: telling an agent a room is
	# there because a rival's committed booking is missing from this
	# transaction's snapshot is a wrong answer even when nothing is held yet.
	check_availability(doc.property, room_type, arrival, departure, rooms=1, current=True)

	doc.append(
		"rooms",
		{
			"room_type": room_type,
			"rooms": 1,
			"arrival_date": arrival,
			"departure_date": departure,
			"adults": max(int(adults or 1), 1),
			"children": max(int(children or 0), 0),
			"rate_plan": rate_plan,
		},
	)

	# The document path, which is correct here: the controller validates the room
	# type against the property, widens the header to span the new line and
	# re-prices the booking, and none of that is this service's to reimplement.
	doc.save(ignore_permissions=True)

	line = doc.rooms[-1]

	_log_modification(
		doc,
		_("Room line added"),
		{
			"room_line": line.name,
			"room_type": room_type,
			"arrival": str(arrival),
			"departure": str(departure),
			"adults": line.adults,
			"children": line.children,
			"rate_plan": line.rate_plan,
		},
	)

	return {"reservation": reservation, "room_line": line.name, "total_amount": flt(doc.total_amount)}


def remove_room_line(reservation: str, room_line: str) -> dict:
	"""Drop one room from a booking that is still being put together.

	Only this line's inventory goes. The rows that remain keep their own dates,
	types, plans and assignments, and the header is re-derived from what is left
	- so removing the longest room shortens the booking and removing a short one
	does not.
	"""
	doc, line = _lock_line_chain(reservation, room_line)
	doc.check_permission("write")

	_assert_modifiable(doc, LINE_EDITABLE_STATES, _("Room lines"))

	if len(doc.rooms) <= 1:
		throw(
			_("A reservation must keep at least one room line. Cancel the reservation instead."),
			exc=HospitalityPMSError,
		)

	removed = _line_or_throw(doc, room_line)
	details = {
		"room_line": room_line,
		"room_type": removed.room_type,
		"arrival": str(getdate(removed.arrival_date)),
		"departure": str(getdate(removed.departure_date)),
		"total_amount": flt(removed.total_amount),
	}

	doc.set("rooms", [row for row in doc.rooms if row.name != room_line])
	doc.save(ignore_permissions=True)

	_log_modification(doc, _("Room line removed"), details)

	return {"reservation": reservation, "room_line": room_line, "total_amount": flt(doc.total_amount)}


def change_line_room_type(reservation: str, room_line: str, room_type: str) -> dict:
	"""Move one room line onto a different room type, while that is still free.

	Editable statuses only, and not because the availability check is any harder
	past them. A holding booking has been quoted a price for the type it holds,
	and moving it compounds an inventory question with a pricing one - does the
	quoted rate follow the guest into a better room? - that this build makes no
	decision about. Cancel and rebook.
	"""
	doc, line = _lock_line_chain(reservation, room_line, room_types=(room_type,))
	doc.check_permission("write")

	_assert_modifiable(doc, LINE_EDITABLE_STATES, _("Room type"))

	if room_type == line["room_type"]:
		throw(_("Room line {0} is already a {1}.").format(room_line, room_type))

	quantity = max(int(line["rooms"] or 1), 1)

	check_availability(
		doc.property,
		room_type,
		line["arrival_date"],
		line["departure_date"],
		rooms=quantity,
		# The new type's row is locked by `_lock_line_chain` above; this is the
		# read that has to be current under it (N1).
		current=True,
	)

	row = _line_or_throw(doc, room_line)
	previous_type = row.room_type
	previous_room = row.assigned_room

	row.room_type = room_type

	# A physical room belongs to one type, so an assignment made against the old
	# one cannot serve the new one - and leaving it would promise a standard room
	# to a suite booking. Dropped rather than re-picked: choosing a room is
	# `assign_room`'s decision, made against its own clash and readiness rules.
	row.assigned_room = None

	# The line's plan must be able to price the new type. Dropped when it cannot,
	# so the rate service resolves an applicable plan instead of `resolve_rate_plan`
	# refusing the whole operation over a plan the caller never chose for this
	# type. Kept when it can, because an explicitly chosen plan is not silently
	# replaced.
	if row.rate_plan and not _plan_prices_room_type(
		row.rate_plan, room_type, doc.property, row.arrival_date
	):
		row.rate_plan = None

	doc.save(ignore_permissions=True)

	_log_modification(
		doc,
		_("Room type changed"),
		{
			"room_line": room_line,
			"from": previous_type,
			"to": room_type,
			"assignment_released": previous_room,
			"rate_plan": row.rate_plan,
		},
	)

	return {
		"reservation": reservation,
		"room_line": room_line,
		"room_type": room_type,
		"assignment_released": previous_room,
		"total_amount": flt(doc.total_amount),
	}


def set_line_rate_plan(reservation: str, room_line: str, rate_plan: str) -> dict:
	"""Put one room line on a different rate plan, and let the rate service price it.

	The rate is never a parameter of this function, and that is the whole point:
	the caller chooses a *plan*, `price_reservation` asks `get_rate_breakdown`
	what that plan costs for these nights and this occupancy, and whatever the
	caller may have believed the nightly rate to be is overwritten by the answer.
	Re-pricing here is expected rather than avoided - the booking is still a
	working draft, which is exactly when the snapshot is allowed to move.
	"""
	if not rate_plan or not str(rate_plan).strip():
		throw(_("A rate plan is required."))

	rate_plan = str(rate_plan).strip()

	doc, line = _lock_line_chain(reservation, room_line)
	doc.check_permission("write")

	_assert_modifiable(doc, LINE_EDITABLE_STATES, _("Rate plan"))

	row = _line_or_throw(doc, room_line)
	previous_plan = row.rate_plan

	row.rate_plan = rate_plan
	doc.save(ignore_permissions=True)

	row = _line_or_throw(doc, room_line)

	_log_modification(
		doc,
		_("Rate plan changed"),
		{
			"room_line": room_line,
			"from": previous_plan,
			"to": row.rate_plan,
			"room_rate": flt(row.room_rate),
			"total_amount": flt(row.total_amount),
		},
	)

	return {
		"reservation": reservation,
		"room_line": room_line,
		"rate_plan": row.rate_plan,
		"room_rate": flt(row.room_rate),
		"total_amount": flt(doc.total_amount),
	}


# ---------------------------------------------------------------------------
# Queries# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_arrivals(property_name: str, on_date=None) -> list[dict]:
	"""Reservations due to arrive on a date.

	Defaults to the property's operating day, not the calendar's. A hotel
	mid-way between night audits is still working yesterday, and an arrivals
	list that disagrees sends the desk looking for guests who are not due.
	"""
	return frappe.get_all(
		RESERVATION_DOCTYPE,
		filters={
			"property": property_name,
			"arrival_date": resolve_operational_date(property_name, on_date),
			"reservation_status": ("in", (CONFIRMED, GUARANTEED)),
		},
		fields=[
			"name",
			"guest",
			"guest_name",
			"arrival_date",
			"departure_date",
			"total_rooms",
			"total_adults",
			"reservation_status",
			"total_amount",
			"deposit_received",
		],
		order_by="guest_name asc",
		limit_page_length=0,
	)


def get_departures(property_name: str, on_date=None) -> list[dict]:
	"""Reservations due to depart on a date, on the property's operating day."""
	return frappe.get_all(
		RESERVATION_DOCTYPE,
		filters={
			"property": property_name,
			"departure_date": resolve_operational_date(property_name, on_date),
			"reservation_status": CHECKED_IN,
		},
		fields=[
			"name",
			"guest",
			"guest_name",
			"arrival_date",
			"departure_date",
			"total_rooms",
			"reservation_status",
			"total_amount",
		],
		order_by="guest_name asc",
		limit_page_length=0,
	)


def get_unresolved_arrivals(property_name: str, business_date=None) -> list[dict]:
	"""Arrivals that never checked in, for the Night Audit exception list."""
	business_date = getdate(business_date or get_business_date(property_name))

	return frappe.get_all(
		RESERVATION_DOCTYPE,
		filters={
			"property": property_name,
			"arrival_date": ("<=", business_date),
			"reservation_status": ("in", (CONFIRMED, GUARANTEED)),
		},
		fields=["name", "guest_name", "arrival_date", "total_rooms", "reservation_status", "guarantee_type"],
		order_by="arrival_date asc",
		limit_page_length=0,
	)


def find_by_external_reference(property_name: str, external_reference: str) -> str | None:
	"""Duplicate protection for channel imports (SAS section 3.4)."""
	if not external_reference:
		return None

	return frappe.db.get_value(
		RESERVATION_DOCTYPE,
		{
			"property": property_name,
			"external_reference": external_reference,
			"reservation_status": ("not in", (CANCELLED,)),
		},
		"name",
	)
