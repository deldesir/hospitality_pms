"""Check-in, stay management and the in-house board.

Check-in is the moment a booking becomes a physical guest in a physical room.
It touches four things that must all move together or not at all: the
reservation status, the room's occupancy, a new Stay record and a new Folio.
Everything here runs inside the caller's transaction so a failure half way
cannot leave a guest checked in with no folio to charge.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime

from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import short_stay
from hospitality_pms.services.availability import stay_nights
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import rooms as room_service
from hospitality_pms.services.availability import (
	authorise_overbooking,
	check_availability,
	lock_room_type,
	nights_between,
	overbooking_evidence,
)
from hospitality_pms.services.base import (
	STAY_ORCHESTRATION,
	assert_transition,
	lock_and_get_doc,
	lock_and_read,
	lock_document,
	require_permission,
	require_role,
	service_context,
	transaction,
)
from hospitality_pms.services.exceptions import (
	HospitalityPMSError,
	InvalidStateTransitionError,
	throw,
)
from hospitality_pms.services.guests import assert_not_blacklisted, ensure_customer
from hospitality_pms.services.property import get_business_date, get_property

STAY_DOCTYPE = "Stay"

EXPECTED = "Expected"
IN_HOUSE = "In House"
DUE_OUT = "Due Out"
CHECKED_OUT = "Checked Out"
CLOSED = "Closed"

#: Stay state machine, from Workflow Matrix section 3.
TRANSITIONS = {
	EXPECTED: {IN_HOUSE},
	IN_HOUSE: {DUE_OUT, CHECKED_OUT},
	DUE_OUT: {CHECKED_OUT, IN_HOUSE},
	CHECKED_OUT: {CLOSED, IN_HOUSE},
	CLOSED: set(),
}

#: The stay statuses in which a guest is **physically in their room**.
#:
#: The one definition of active physical occupancy, and the reason room
#: authority has a single answer rather than one per caller (HPMS-UAT-16.7.5-B01).
#: Read by `rooms.active_stay_in_room`, which is what every physical-room
#: mutation asks before it puts a guest anywhere.
#:
#: `Expected` is deliberately absent: a stay is created `Expected` and becomes
#: `In House` in the same check-in, and nobody is in the room until it does.
#: `Checked Out` and `Closed` are absent because the guest has gone - a departed
#: stay must release its room, or one checkout would sterilise a room forever.
#:
#: `Due Out` is the member this product got wrong, and the reason room 402
#: happened. It means "leaving today", not "left": the guest is still in the bed,
#: their luggage is still in the wardrobe, and `mark_due_out` puts every
#: departing stay here from the Night Audit each morning. Treating it as vacant
#: made a guest invisible on the one day the desk is most likely to re-let their
#: room.
ACTIVE_OCCUPANCY_STATES = (IN_HOUSE, DUE_OUT)

#: Checking a guest into a room that is not clean is a manager decision, and
#: the reason is recorded on the stay (SAS section 3.2).
READINESS_OVERRIDE_ROLES = (
	"Front Office Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)

#: Reversing a checkout re-opens settled money, so it needs both front office
#: and finance authority (Workflow Matrix section 3).
CHECKOUT_REVERSAL_ROLES = (
	"Front Office Manager",
	"Finance Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------


def check_in(
	reservation: str,
	room_line: str,
	room: str,
	*,
	allow_unready_room: bool = False,
	readiness_reason: str | None = None,
	skip_id_check: bool = False,
	companions: list[dict] | None = None,
	billing_instructions: str | None = None,
) -> dict:
	"""Check a reservation's room line into a specific room.

	Returns the stay and folio it created. Safe to call twice: the second call
	finds the line already checked in and refuses rather than creating a second
	stay for the same guest.
	"""
	# Editing a booking and physically checking a guest in are different
	# authorities. A Reservation Agent holds `Reservation.write` and, by the
	# approved matrix, no `Stay.create` - and used to be able to check a guest
	# in anyway, because this gate consulted only the first of the two. Both
	# are required, and both are asserted before any state is touched.
	require_permission(STAY_DOCTYPE, "create")

	# Locked and read together: `_assert_reservation_ready` below decides on
	# `reservation_status`, and reading it from a pre-lock snapshot would let a
	# reservation cancelled a moment ago still be checked in (N1).
	reservation_doc = lock_and_get_doc(reservation_service.RESERVATION_DOCTYPE, reservation)
	reservation_doc.check_permission("write")

	lock_document("Hotel Room", room)

	line = next((row for row in reservation_doc.rooms if row.name == room_line), None)
	if not line:
		throw(_("Room line {0} does not belong to reservation {1}.").format(room_line, reservation))

	if frappe.db.exists(STAY_DOCTYPE, {"reservation_room_line": room_line, "stay_status": ("!=", CLOSED)}):
		throw(
			_("This room line is already checked in."),
			exc=InvalidStateTransitionError,
		)

	_assert_reservation_ready(reservation_doc)
	_assert_arrival_is_due(reservation_doc, line.arrival_date)

	if reservation_doc.guest:
		assert_not_blacklisted(reservation_doc.guest)

	if not skip_id_check:
		_assert_identity_captured(reservation_doc.property, reservation_doc.guest)

	_assert_deposit_satisfied(reservation_doc)

	# Room readiness. The override relaxes housekeeping only, and is recorded.
	if allow_unready_room:
		require_role(READINESS_OVERRIDE_ROLES)

		if not readiness_reason or not readiness_reason.strip():
			throw(_("A reason is required to check in to a room that is not ready."))

		if not get_property(reservation_doc.property).allow_vacant_dirty_check_in:
			throw(
				_("This property does not permit check-in to a room that is not ready."),
				exc=HospitalityPMSError,
			)

	room_service.assert_assignable(room, allow_unready_housekeeping=allow_unready_room)

	# And whether the room still has its previous guest in it
	# (HPMS-UAT-16.7.5-B01).
	#
	# `assert_assignable` above asks the room's own occupancy flag, and that flag
	# reads `Due Out` for every guest the Night Audit marked as leaving today -
	# a state deliberately outside `OCCUPIED_STATES`, because the room *is*
	# re-lettable once they have gone. It cannot tell whether they have gone.
	# Room 402's flag went one worse and read `Vacant`, because the second guest
	# checked out of it while the first was still there.
	#
	# Asked under the Hotel Room lock taken at the top of this function, and with
	# a **current** read: the lock stops two check-ins picking this room at once
	# but does not refresh what this transaction sees, so a plain read would be
	# answered from the view opened before the rival's check-in committed (N1).
	#
	# No `occupancy_applies` gate here, unlike `assign_room`. A check-in is
	# always now - `_assert_arrival_is_due` has already refused a future arrival -
	# so there is no date on which today's occupant is somebody else's problem.
	room_service.assert_room_unoccupied(room, property_name=reservation_doc.property)

	# Assigning through the reservation service re-checks the clash, property and
	# type rules. Run it even when the line was already assigned this room (RES-4):
	# a pre-set assigned_room can arrive through the create payload, a channel
	# import or Desk without ever passing those checks, and the old fast path
	# skipped them exactly when the room had not been validated. assign_room writes
	# the same value back when it is unchanged, so this is a no-op beyond the checks.
	reservation_service.assign_room(reservation, room_line, room, allow_unready=allow_unready_room)

	# The line says what it sells (written from its rate plan when the reservation was
	# validated): a night, a day use or hourly blocks. The stay copies it, and the charge
	# below reads it; nothing here infers the kind from the dates.
	kind = line.get("stay_kind") or short_stay.NIGHT

	# Every precondition above has now passed, so this is the one moment at
	# which a Stay may legitimately come into existence. The context is opened
	# around the insert alone: an unrelated save later in this request must not
	# inherit the authority (P1-2).
	stay = _insert_stay(
		{
			"doctype": STAY_DOCTYPE,
			"property": reservation_doc.property,
			"stay_status": EXPECTED,
			"reservation": reservation,
			"reservation_room_line": room_line,
			"guest": reservation_doc.guest,
			"room": room,
			"room_type": line.room_type,
			"arrival_date": line.arrival_date,
			"departure_date": line.departure_date,
			"nights": len(stay_nights(line.arrival_date, line.departure_date)),
			"stay_kind": kind,
			"arrival_time": line.get("arrival_time"),
			"departure_time": line.get("departure_time"),
			"blocks": line.get("blocks") or 0,
			"adults": line.adults,
			"children": line.children,
			"extra_beds": line.extra_beds,
			"rate_plan": line.rate_plan,
			"room_rate": line.room_rate,
			"readiness_override": 1 if allow_unready_room else 0,
			"readiness_override_reason": readiness_reason,
			"special_requests": line.special_requests or reservation_doc.special_requests,
		}
	)

	for companion in companions or []:
		stay.append("companions", companion)

	if companions:
		stay.save(ignore_permissions=True)

	folio = folio_service.open_folio(
		reservation_doc.property,
		reservation_doc.guest,
		stay=stay.name,
		reservation=reservation,
		room=room,
		billing_instructions=billing_instructions or reservation_doc.internal_notes,
	)

	# A guest in house will carry a balance, so their ERPNext Customer exists from this
	# moment: the bar or restaurant till sells to that Customer and the sale finds the stay
	# (services/pos_bridge.py). A walk-in who never checks in still gets none. Never a
	# reason to refuse a check-in: a collision or a permission gap is logged and the
	# Customer is made the first time money moves, as before.
	try:
		ensure_customer(reservation_doc.guest, get_property(reservation_doc.property).company)
	except Exception:
		frappe.log_error(title="Guest Customer at check-in", message=frappe.get_traceback())

	frappe.db.set_value(
		STAY_DOCTYPE,
		stay.name,
		{
			"folio": folio,
			"stay_status": IN_HOUSE,
			"checked_in_on": now_datetime(),
			"checked_in_by": frappe.session.user,
		},
		update_modified=True,
	)

	if kind != short_stay.NIGHT:
		# A day-use or hourly stay consumes no night, so the night audit never charges it:
		# the room is charged now, once, at the rate the booking was sold for.
		folio_service.post_charge(
			folio,
			short_stay.charge_type_for(kind),
			_("{0}: room {1} on {2}, {3} to {4}").format(
				_(kind), room, line.arrival_date, str(line.arrival_time or "")[:5], str(line.departure_time or "")[:5]
			),
			flt(line.room_rate),
			idempotency_key=f"short-stay:{stay.name}",
			reference_doctype=STAY_DOCTYPE,
			reference_name=stay.name,
		)

	room_service.mark_occupied(
		room, reference_doctype=STAY_DOCTYPE, reference_name=stay.name
	)

	# The reservation follows only once every one of its lines is in house;
	# a multi-room booking is not checked in until the whole party arrives.
	if _all_lines_checked_in(reservation):
		reservation_service._transition(
			reservation_doc,
			reservation_service.CHECKED_IN,
			reason=_("Guest checked in"),
			details={"stay": stay.name, "room": room},
		)
		frappe.db.set_value(
			reservation_service.RESERVATION_DOCTYPE,
			reservation,
			"checked_in_on",
			now_datetime(),
			update_modified=False,
		)

	# Any deposit already taken on the reservation moves onto the folio, so the
	# guest is not asked to pay it twice - but only this room's share of it.
	# The key carries the room line for the same reason: idempotency is scoped
	# per folio, so one key for the whole booking was unique on three different
	# folios and let the deposit through three times (P1-6).
	deposit = reservation_service.deposit_share(reservation_doc, room_line)
	if deposit > 0:
		folio_service.post_payment(
			folio,
			deposit,
			"Bank Transfer",
			payment_type="Deposit",
			idempotency_key=f"reservation-deposit:{reservation}:{room_line}",
			reference=reservation,
		)

	return {"stay": stay.name, "folio": folio, "room": room}


def _insert_stay(values: dict):
	"""Create the Stay, inside the orchestration context that authorises it.

	The single place in the application where a Stay is created. `Stay`'s
	controller refuses an insert outside this context, so anything that needs
	to create a stay in future has to come through here - and therefore through
	the preconditions that make a stay safe to create.
	"""
	# The last line of defence for physical occupancy, and the reason it is here
	# rather than only in `check_in`.
	#
	# `check_in` asks `assert_room_unoccupied` itself, and that is the check that
	# produces the readable refusal; this one will normally never fire. It is
	# placed here because this function is the documented single door through
	# which a Stay comes into existence, which makes it the only place a guard
	# cannot be bypassed by a *future* caller that forgets to ask. A room guard
	# that lives only in today's callers protects only today's callers
	# (defence in depth, and the same reasoning that put the orchestration
	# context here in the first place).
	#
	# Deliberately not gated on a date: a Stay is created by check-in, which is
	# always now.
	room = values.get("room")

	if room:
		room_service.assert_room_unoccupied(room, property_name=values.get("property"))

	with service_context(STAY_ORCHESTRATION):
		return frappe.get_doc(values).insert(ignore_permissions=True)


def _assert_reservation_ready(doc):
	if doc.reservation_status not in (reservation_service.CONFIRMED, reservation_service.GUARANTEED):
		throw(
			_("Reservation {0} is {1} and cannot be checked in.").format(doc.name, _(doc.reservation_status)),
			exc=InvalidStateTransitionError,
		)


def _assert_arrival_is_due(doc, arrival=None):
	"""A guest cannot check in before the business date reaches their arrival.

	The **room line's** arrival, not the booking's. The header is the earliest
	arrival across the rooms and never a copy of one of them (HPMS-DEC-154), so
	the two diverge the moment a multi-room booking's rooms do - and after
	`reservations.change_line_interval` moves one room out by a week, the header
	still says today. Checked against the header, that room would pass this gate,
	get a Stay dated in the future, and have its room marked Occupied now.

	Falls back to the header when no line arrival is supplied, so a caller that
	is asking about the booking as a whole still gets an answer.
	"""
	arrival = getdate(arrival or doc.arrival_date)
	business_date = get_business_date(doc.property)

	if arrival > getdate(business_date):
		throw(
			_("This room of reservation {0} arrives on {1}; the business date is {2}.").format(
				doc.name, arrival, getdate(business_date)
			),
			exc=InvalidStateTransitionError,
		)


def _assert_identity_captured(property_name: str, guest: str | None):
	"""Refuse check-in without an identification document when required."""
	if not get_property(property_name).require_id_at_check_in:
		return

	if not guest:
		throw(_("A guest record is required to check in."))

	has_id = frappe.db.exists(
		"Guest Identification",
		{"parent": guest, "parenttype": "Guest"},
	)

	if not has_id:
		throw(
			_("This property requires guest identification before check-in."),
			exc=HospitalityPMSError,
		)


def _assert_deposit_satisfied(doc):
	"""Refuse check-in when a required deposit has not been received."""
	required = flt(doc.deposit_required)

	if required <= 0:
		return

	if flt(doc.deposit_received) + 0.005 < required:
		throw(
			_("A deposit of {0} is required; {1} has been received.").format(
				required, flt(doc.deposit_received)
			),
			exc=HospitalityPMSError,
		)


def _all_lines_checked_in(reservation: str) -> bool:
	lines = frappe.get_all("Reservation Room", filters={"parent": reservation}, pluck="name")

	checked_in = frappe.get_all(
		STAY_DOCTYPE,
		filters={
			"reservation": reservation,
			"stay_status": ("in", (IN_HOUSE, DUE_OUT, CHECKED_OUT)),
		},
		pluck="reservation_room_line",
	)

	return set(lines).issubset(set(filter(None, checked_in)))


# ---------------------------------------------------------------------------
# Stay operations
# ---------------------------------------------------------------------------


def change_room(
	stay: str,
	new_room: str,
	reason: str,
	*,
	allow_unready: bool = False,
	allow_overbooking: bool = False,
) -> dict:
	"""Move an in-house guest to another room.

	Takes the inventory row first, then the Stay, then both Hotel Rooms in a
	deterministic order, so two simultaneous moves cannot cross over each other -
	and neither can this and a reservation-side date change (see below).
	"""
	if not reason or not reason.strip():
		throw(_("A reason is required to change room."))

	# The inventory row, **before** the Stay and before either Hotel Room.
	#
	# This function writes `Reservation Room.assigned_room` at the end, and until
	# 16.7.2 it did so without taking that row's lock, on the reasoning that
	# nothing was being decided from the row. That reasoning was wrong in the way
	# that matters: an `UPDATE` takes the row's exclusive lock whether or not
	# anybody called `lock_document`, so declining to call it removed the
	# documentation and not the lock - and took it last, from a function already
	# holding the Hotel Rooms.
	#
	# Against the documented chain (Reservation -> Reservation Room -> Room Type
	# -> Hotel Room -> Stay) that is an inversion, and it closed a real cycle:
	# `reservations.change_line_interval` holds the Reservation Room row and then
	# asks for the assigned Hotel Room, while this held the Hotel Rooms and then
	# wanted the row. InnoDB broke it with a deadlock (1213) rather than either
	# caller being refused for a reason a human could read.
	#
	# Locked first instead. Every counterpart - `change_line_interval`,
	# `_lock_stay_chain` for `extend_stay` and `shorten_stay` - takes this row
	# before any Hotel Room, so whichever caller wins the row runs to completion
	# and the other queues behind it. The line is found with a plain read because
	# `Stay.reservation_room_line` is set at check-in and never changes; every
	# value decided on afterwards comes from the locking read.
	line_name = frappe.db.get_value(STAY_DOCTYPE, stay, "reservation_room_line")
	line = reservation_service.lock_inventory_line(line_name) if line_name else None

	# Current under lock: the status guard below decides whether the guest is
	# still in the room at all, and a pre-lock read of it could move a guest
	# who checked out while this request waited (N1).
	doc = lock_and_get_doc(STAY_DOCTYPE, stay)
	doc.check_permission("write")

	if doc.stay_status not in (IN_HOUSE, DUE_OUT):
		throw(
			_("Only an in-house stay can change room; this stay is {0}.").format(_(doc.stay_status)),
			exc=InvalidStateTransitionError,
		)

	if new_room == doc.room:
		throw(_("The guest is already in room {0}.").format(new_room))

	# A cross-type move consumes a room in the destination *type*, and until now
	# nothing checked that type had one to give (RES-3). `assert_assignable` and
	# `_assert_room_free` below prove the specific physical room is free, but a room
	# held for a future arrival is physically free today - so a move into it left
	# the destination type oversold for the nights the line still holds, with no
	# `check_availability` and no overbooking authority anywhere on this path. The
	# type is locked here, before either Hotel Room, in the documented chain
	# position (Reservation Room -> Room Type -> Hotel Room), and the availability
	# itself is checked below once the interval is known.
	old_type = line["room_type"] if line else doc.room_type
	target_type = frappe.db.get_value("Hotel Room", new_room, "room_type")
	cross_type = target_type != old_type

	if cross_type:
		if allow_overbooking:
			authorise_overbooking(reason)
		lock_room_type(doc.property, sorted({old_type, target_type}))

	for room in sorted([doc.room, new_room]):
		lock_document("Hotel Room", room)

	room_service.assert_assignable(new_room, allow_unready_housekeeping=allow_unready)

	new_property, new_type = frappe.db.get_value("Hotel Room", new_room, ["property", "room_type"])
	if new_property != doc.property:
		throw(_("Room {0} belongs to another property.").format(new_room))

	# The room the guest is being moved into must not already be promised to
	# somebody else for these nights.
	#
	# This was missing entirely. `assert_assignable` asks only about the room's
	# own state - sellable, not out of order, clean enough - and a room reserved
	# for tomorrow's arrival is all of those things today. So a move into it
	# committed, and left two `Reservation Room` rows naming one physical room
	# over overlapping intervals: exactly the state `_assert_room_free` exists to
	# make impossible, reached by the one writer that never asked it.
	#
	# Asked over the **inventory row's** interval, which is the authoritative one
	# (a Stay's departure date on its own is a display value), and excluding this
	# line, which must not be treated as its own rival. Safe to ask here and not
	# merely useful: the row is locked above, so the answer cannot go stale
	# between this check and the write below.
	interval_start = getdate(line["arrival_date"] if line else doc.arrival_date)
	interval_end = getdate(line["departure_date"] if line else doc.departure_date)

	reservation_service._assert_room_free(
		new_room,
		interval_start,
		interval_end,
		exclude_line=line_name,
		remedy=_("Choose another room, or move the booking that already holds this one."),
	)

	# And the destination must not have a guest in it right now
	# (HPMS-UAT-16.7.5-B01).
	#
	# The two checks above are the two that were satisfied for room 402 while
	# somebody was asleep in it: `assert_assignable` reads a flag that says
	# `Due Out` for every departing guest, and `_assert_room_free` reads an
	# interval whose departure-exclusive overlap does not catch the guest whose
	# departure date is today. A room move is a physical placement exactly like a
	# check-in, so it asks the same question the same way.
	#
	# `exclude_stay` is this stay: a guest moving out of a room must not be found
	# as their own rival in it. That matters because this is asked about
	# `new_room` only - the room being vacated needs no permission from anybody -
	# but a caller moving a guest back into a room they still nominally hold
	# would otherwise be refused on their own account.
	#
	# Under the Hotel Room locks taken above, with a current read, for the reason
	# recorded on `_assert_room_free`: the lock serialises but does not refresh.
	room_service.assert_room_unoccupied(new_room, exclude_stay=stay, property_name=doc.property)

	# The destination type must actually have a room to give for the nights the
	# line still holds (RES-3). Checked over today-forward - past nights are already
	# consumed - with a current read under the room-type lock taken above, and
	# honouring the overbooking authority granted at the top. Same-type moves skip
	# this: the line already counts against that type, so its own count is unchanged.
	if cross_type:
		check_start = max(getdate(get_business_date(doc.property)), interval_start)
		if check_start < interval_end:
			check_availability(
				doc.property,
				target_type,
				check_start,
				interval_end,
				rooms=1,
				allow_overbooking=allow_overbooking,
				exclude_reservation=line["parent"] if line else doc.reservation,
				current=True,
			)

	previous_room = doc.room

	# The vacated room becomes dirty; the new room becomes occupied.
	room_service.set_statuses(
		previous_room,
		{room_service.OCCUPANCY: "Vacant", room_service.HOUSEKEEPING: "Dirty"},
		reason=_("Guest moved to room {0}").format(new_room),
		reference_doctype=STAY_DOCTYPE,
		reference_name=stay,
	)
	room_service.mark_occupied(new_room, reference_doctype=STAY_DOCTYPE, reference_name=stay)

	doc.append(
		"room_moves",
		{
			"from_room": previous_room,
			"to_room": new_room,
			"moved_on": now_datetime(),
			"moved_by": frappe.session.user,
			"reason": reason.strip(),
		},
	)
	doc.room = new_room
	doc.room_type = new_type
	doc.save(ignore_permissions=True)

	if doc.folio:
		frappe.db.set_value(folio_service.FOLIO_DOCTYPE, doc.folio, "room", new_room, update_modified=False)

	# The inventory row has to follow the guest, and until 16.7.2 it did not.
	#
	# `Reservation Room` is the authoritative record of which physical room is
	# promised to whom, and `reservations._assert_room_free` reads *only* that
	# table - a checked-in guest is counted there because check-in does not move a
	# booking out of the holding states. So a move that updated the Stay and not
	# the row left the protection guarding the room the guest had left, and
	# reporting the room they were now asleep in as free. Found on this bench:
	# one stay with the guest in 503 while its row still said 504, and
	# `_assert_room_free("DOHA01-503")` answering "free". The next assignment of
	# that room would have put two guests in it.
	#
	# The row is locked at the top of this function, before the Stay and before
	# either Hotel Room, and this is the write that lock exists for. Taking it
	# here instead - last, from a caller already holding the Hotel Rooms - is what
	# deadlocked against `change_line_interval`; the reasoning that no lock was
	# needed is recorded and corrected in the comment at the top.
	#
	# `room_type` moves with the room (16.7.3). Leaving it naming the type the
	# booking was made for, while the guest sleeps in a room of another one, made
	# the two records contradict each other and made availability wrong twice over
	# every remaining night: `_sold_by_night` counts the line against the *old*
	# type, so the origin type is held back from sale, and the destination type
	# reports a room free that has a guest in it. `_assert_room_free` protects the
	# specific room, so the clash surfaces at the next assignment rather than as a
	# double-booked door - but by then the desk has already sold it.
	#
	# Written with `set_value`, exactly as `assigned_room` above is. `room_type` is
	# one of `LOCKED_ROOM_LINE_FIELDS`, so a `doc.save()` here would be refused by
	# `_guard_room_line_immutability` - the guard exists to stop the type being
	# edited on the reservation form, and directs the caller to the front office
	# operation, which is this one. `set_value` also keeps `price_reservation` out
	# of it: a room move is operational and must not reprice a guest who has
	# already been quoted. It cannot run in any case, because a checked-in
	# booking is past `_is_editable()`, but the booked rate stays on the line and
	# on the Stay either way, and `Stay.room_rate` is what the night audit posts.
	if doc.reservation_room_line:
		frappe.db.set_value(
			reservation_service.RESERVATION_ROOM_DOCTYPE,
			doc.reservation_room_line,
			{"assigned_room": new_room, "room_type": new_type},
			update_modified=False,
		)

		# A cross-type move changes what the booking holds, and until now that was
		# invisible in the reservation's own history: `change_room` is the only
		# inventory service that wrote nothing to `Reservation Log`. Logged only
		# when the type actually moves, so same-type moves - the ordinary case -
		# do not add a row that says nothing.
		if line and line["room_type"] != new_type:
			reservation_service.log_room_type_move(
				line["parent"],
				room_line=doc.reservation_room_line,
				from_type=line["room_type"],
				to_type=new_type,
				assigned_room=new_room,
			)

	return {"stay": stay, "from_room": previous_room, "to_room": new_room}


def extend_stay(
	stay: str, new_departure, *, allow_overbooking: bool = False, reason: str | None = None
) -> dict:
	"""Extend a stay, and extend the inventory it holds with it.

	The two used to come apart. Only the Stay's own departure date moved, and
	`Reservation Room` - which is what availability counts, what confirmation
	reduces and what the room-clash check reads - kept the original date. So a
	guest extended to the 15th and the property cheerfully reported its only
	room free from the 12th, sold it to somebody else, and had two guests for
	one room (P1-5).

	Both dates move here, in one transaction, behind the inventory lock chain
	documented in `services.reservations`.
	"""
	# Authorised before anything is read or locked, so an override nobody may
	# make costs nothing (Wave 1, P1-3).
	overbooking_reason = authorise_overbooking(reason) if allow_overbooking else None

	new_departure = getdate(new_departure)

	# Plain reads, used only to decide what to lock.
	target = frappe.db.get_value(
		STAY_DOCTYPE,
		stay,
		["property", "reservation", "reservation_room_line", "room_type", "room"],
		as_dict=True,
	)

	if not target:
		throw(_("Stay {0} not found.").format(stay), exc=HospitalityPMSError)

	doc, line = _lock_stay_chain(stay, target)
	doc.check_permission("write")

	if doc.stay_status not in (IN_HOUSE, DUE_OUT):
		throw(
			_("Only an in-house stay can be extended; this stay is {0}.").format(_(doc.stay_status)),
			exc=InvalidStateTransitionError,
		)

	# A day-use or hourly stay was charged in full at check-in and holds no night. Moving
	# its departure would keep that charge, give the line nights, and have the audit post
	# tonight at the day price: a night is a reservation of its own, on a night plan.
	if (doc.get("stay_kind") or short_stay.NIGHT) != short_stay.NIGHT:
		throw(
			_("A {0} stay ends on its own day and cannot be extended into a night: book the night as a new reservation.").format(
				_(doc.stay_kind)
			),
			exc=InvalidStateTransitionError,
		)

	current_departure = getdate(line["departure_date"] if line else doc.departure_date)

	if new_departure <= current_departure:
		throw(_("The new departure must be after the current departure of {0}.").format(current_departure))

	# Only the added nights are checked; the nights already in house are the
	# guest's by right.
	#
	# `current=True` because `_lock_stay_chain` above holds the Room Type row and
	# a lock serialises without refreshing: an extension that queued behind a
	# confirmation would otherwise count the house as it stood before that
	# confirmation committed, and take the room it had just sold (N1).
	check = check_availability(
		doc.property,
		doc.room_type,
		current_departure,
		new_departure,
		rooms=1,
		allow_overbooking=allow_overbooking,
		current=True,
	)

	# Room-type capacity and this particular room are different questions, and
	# an overbooking override answers only the first. Selling one more room of a
	# type the house does not have is a management decision; putting two guests
	# in room 101 is not something any override makes true.
	reservation_service._assert_room_free(
		doc.room, current_departure, new_departure, exclude_line=target["reservation_room_line"]
	)

	if line:
		reservation_service.set_line_interval(line["name"], departure=new_departure)

	doc.departure_date = new_departure
	doc.nights = len(nights_between(doc.arrival_date, new_departure))

	# Appended *before* the save. It used to be appended after, so every
	# extension the hotel ever made left no record of itself at all (N3).
	_log_note(
		doc,
		_("Stay extended from {0} to {1}{2}").format(
			current_departure,
			new_departure,
			_(" (overbooking override: {0})").format(overbooking_reason) if overbooking_reason else "",
		),
	)

	# The save runs inside the orchestration context because the Stay
	# controller refuses a date moved by a bare document edit; this function
	# is the supported route, and the context is what says so.
	with service_context(STAY_ORCHESTRATION):
		doc.save(ignore_permissions=True)

	# Due Out means "leaving today"; a guest who has just extended is not.
	# Written through the transition service because the Stay controller
	# refuses a status edited on the document itself.
	if doc.stay_status == DUE_OUT:
		transition(stay, IN_HOUSE, reason=_("Stay extended"))

	if overbooking_reason:
		_log_overbooking_override(doc, [check], overbooking_reason, new_departure)

	return {"stay": stay, "departure_date": str(new_departure), "nights": doc.nights}


def _lock_stay_chain(stay: str, target: dict):
	"""Take the inventory locks in the one documented order, then load the Stay.

	Reservation, its room line, the room type, the physical room, and finally
	the Stay itself. One order for every caller, so two operations touching the
	same booking queue behind each other instead of deadlocking.
	"""
	if target.get("reservation"):
		lock_document(reservation_service.RESERVATION_DOCTYPE, target["reservation"])

	line = None
	if target.get("reservation_room_line"):
		line = reservation_service.lock_inventory_line(target["reservation_room_line"])

	if target.get("room_type"):
		lock_room_type(target["property"], target["room_type"])

	if target.get("room"):
		lock_document("Hotel Room", target["room"])

	return lock_and_get_doc(STAY_DOCTYPE, stay), line


def shorten_stay(stay: str, new_departure, reason: str) -> dict:
	"""Shorten a stay, and release the nights the guest is no longer taking.

	The mirror of the extension defect: only the Stay moved, so a guest who
	left four days early left four nights unsellable behind them (P2-5).

	Money already posted is left exactly as it is. A night the guest slept and
	was charged for is history; reversing it because the booking got shorter
	would be the system deciding a refund on its own.
	"""
	if not reason or not reason.strip():
		throw(_("A reason is required to shorten a stay."))

	new_departure = getdate(new_departure)

	target = frappe.db.get_value(
		STAY_DOCTYPE,
		stay,
		["property", "reservation", "reservation_room_line", "room_type", "room"],
		as_dict=True,
	)

	if not target:
		throw(_("Stay {0} not found.").format(stay), exc=HospitalityPMSError)

	doc, line = _lock_stay_chain(stay, target)
	doc.check_permission("write")

	current_departure = getdate(line["departure_date"] if line else doc.departure_date)

	if new_departure <= getdate(doc.arrival_date):
		throw(_("The departure must be after the arrival date."))

	if new_departure >= current_departure:
		throw(_("The new departure must be before the current departure."))

	business_date = get_business_date(doc.property)
	if new_departure < getdate(business_date):
		throw(_("A stay cannot be shortened to a date before the business date."))

	if line:
		reservation_service.set_line_interval(line["name"], departure=new_departure)

	doc.departure_date = new_departure
	doc.nights = len(nights_between(doc.arrival_date, new_departure))

	_log_note(
		doc,
		_("Stay shortened from {0} to {1}: {2}").format(
			current_departure, new_departure, reason.strip()
		),
	)

	with service_context(STAY_ORCHESTRATION):
		doc.save(ignore_permissions=True)

	return {"stay": stay, "departure_date": str(new_departure), "nights": doc.nights}


def add_companion(stay: str, companion: dict) -> str:
	doc = frappe.get_doc(STAY_DOCTYPE, stay)
	doc.check_permission("write")

	doc.append("companions", companion)
	doc.save(ignore_permissions=True)

	return stay


def add_note(stay: str, note: str, note_type: str = "Operational") -> str:
	"""Record an operational note on the stay, for shift handover."""
	doc = frappe.get_doc(STAY_DOCTYPE, stay)
	doc.check_permission("write")

	_log_note(doc, note, note_type)
	doc.save(ignore_permissions=True)

	return stay


def _log_overbooking_override(doc, checks: list[dict], reason: str, new_departure):
	"""Record an oversell made through a stay extension.

	Written to the Reservation Log rather than to a new DocType, because that
	is where the booking's history already lives and where an auditor looking
	for "was this room oversold" will look. The stay's own note table is not
	used: notes appended after `doc.save()` are never persisted (N3), which is
	a separate defect and not one to build an audit trail on top of.

	`from_status` and `to_status` are both the reservation's current status -
	an override changes no reservation state - which is the same shape
	`reservations.assign_room` already writes.
	"""
	if not doc.reservation:
		return

	status = frappe.db.get_value(reservation_service.RESERVATION_DOCTYPE, doc.reservation, "reservation_status")

	frappe.get_doc(
		{
			"doctype": reservation_service.RESERVATION_LOG_DOCTYPE,
			"property": doc.property,
			"reservation": doc.reservation,
			"from_status": status,
			"to_status": status,
			"changed_by": frappe.session.user,
			"changed_at": now_datetime(),
			"reason": reason,
			"details": json.dumps(
				{
					**overbooking_evidence(checks, reason),
					"action": "Stay extended",
					"stay": doc.name,
					"room": doc.room,
					"new_departure": str(new_departure),
					"business_date": str(get_business_date(doc.property)),
				},
				default=str,
			),
		}
	).insert(ignore_permissions=True)


def _log_note(doc, note: str, note_type: str = "Operational"):
	doc.append(
		"stay_notes",
		{
			"note_type": note_type,
			"note": note,
			"noted_by": frappe.session.user,
			"noted_on": now_datetime(),
		},
	)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def transition(stay: str, target: str, *, reason: str | None = None) -> str:
	"""Move a stay's state against the approved machine."""
	# The transition is asserted against the status read by the lock, not one
	# this transaction saw beforehand (N1).
	doc = lock_and_get_doc(STAY_DOCTYPE, stay)

	if doc.stay_status == CHECKED_OUT and target == IN_HOUSE:
		require_role(CHECKOUT_REVERSAL_ROLES)

		if not reason or not reason.strip():
			throw(_("A reason is required to reverse a checkout."))

	assert_transition(doc.stay_status, target, TRANSITIONS, _("Stay"))

	frappe.db.set_value(STAY_DOCTYPE, stay, "stay_status", target, update_modified=True)

	return target


def mark_due_out(property_name: str, business_date=None) -> list[str]:
	"""Flag in-house stays departing on the business date.

	Run by the Night Audit so the morning shift sees who is leaving.
	"""
	business_date = getdate(business_date or get_business_date(property_name))

	candidates = frappe.get_all(
		STAY_DOCTYPE,
		filters={
			"property": property_name,
			"stay_status": IN_HOUSE,
			"departure_date": ("<=", business_date),
		},
		pluck="name",
	)

	marked = []

	for stay in candidates:
		# Each stay in its own savepoint: the Night Audit runs this over the whole
		# property, and one stay that cannot be flagged must not roll back the ones
		# already flagged and strand the auditor mid-step.
		with transaction():
			# Current under lock. The plain list read above is a snapshot; a late
			# checkout committing on another till between that read and here would
			# otherwise be blindly overwritten back to Due Out - resurrecting a
			# Checked Out stay with no lock, no transition check and its checkout
			# timestamp still set. Re-read the status under the row lock and skip
			# anything no longer In House.
			current = lock_and_read(STAY_DOCTYPE, stay, ["stay_status", "room"])

			if current["stay_status"] != IN_HOUSE:
				continue

			frappe.db.set_value(STAY_DOCTYPE, stay, "stay_status", DUE_OUT, update_modified=True)

			room = current["room"]
			if room:
				# force=True: an active Stay is the authority for physical
				# occupancy, so it outranks the denormalised room flag. If the flag
				# drifted (the room-402 class leaves it Vacant), an unforced Due Out
				# transition would raise and abort the audit step. Forcing still
				# locks, validates the value and logs, so the correction is visible.
				room_service.set_status(
					room,
					room_service.OCCUPANCY,
					"Due Out",
					reason=_("Departing on {0}").format(business_date),
					reference_doctype=STAY_DOCTYPE,
					reference_name=stay,
					force=True,
				)

		marked.append(stay)

	return marked


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_in_house(property_name: str) -> list[dict]:
	"""The in-house board."""
	return frappe.get_all(
		STAY_DOCTYPE,
		filters={"property": property_name, "stay_status": ("in", (IN_HOUSE, DUE_OUT))},
		fields=[
			"name",
			"guest",
			"guest_name",
			"room",
			"room_type",
			"arrival_date",
			"departure_date",
			"nights",
			"adults",
			"children",
			"stay_status",
			"folio",
			"room_rate",
		],
		order_by="room asc",
		limit_page_length=0,
	)


def get_stays_for_room_charge(property_name: str, business_date) -> list[dict]:
	"""In-house stays that owe a room charge for a business date.

	A guest departing on the business date does not consume that night, so the
	filter is `departure_date > business_date` - the same night arithmetic the
	availability engine uses.
	"""
	business_date = getdate(business_date)

	return frappe.get_all(
		STAY_DOCTYPE,
		filters={
			"property": property_name,
			"stay_status": ("in", (IN_HOUSE, DUE_OUT)),
			"arrival_date": ("<=", business_date),
			"departure_date": (">", business_date),
		},
		fields=["name", "folio", "room", "room_type", "room_rate", "rate_plan", "guest", "adults", "children"],
		limit_page_length=0,
	)


def room_charge_posted(stay: str, business_date) -> bool:
	"""Whether this stay already has a room charge for a business date, on any folio.

	A room charge is one fact per stay per night, but `folio.post_charge`'s
	idempotency check scans only the single folio it is posting to. Once a room
	charge is split onto a company folio the master folio no longer carries it, so a
	per-folio check would let a Night Audit re-run for that date post the room a
	second time. This asks the stable Stay/date identity instead - the charge's
	`reference` to its Stay, which folio surgery preserves - so a re-run recognises
	the charge wherever the split has moved it (F-FIN7).

	Reversed rows count: the original room charge stays on the folio after a
	reversal and remains the charge's idempotency anchor, so re-running the audit
	must not resurrect a room charge finance deliberately reversed.
	"""
	return bool(
		frappe.db.exists(
			folio_service.FOLIO_CHARGE_DOCTYPE,
			{
				"charge_type": "Room Charge",
				"reference_doctype": STAY_DOCTYPE,
				"reference_name": stay,
				"business_date": getdate(business_date),
			},
		)
	)
