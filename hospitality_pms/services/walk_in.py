"""Walk-in check-in: a guest at the desk with no booking, in one operation.

A walk-in is **not a second check-in engine**. It is the ordinary booking
pipeline compressed into a single call, because the guest is standing at the
desk and the agent has no time to drive four screens:

	existing Guest -> normal Reservation (type and source "Walk In")
	-> ReservationService.confirm() -> StayService.check_in() -> Stay + Folio

Everything that makes a check-in safe already exists and is owned elsewhere.
This module coordinates; it reimplements nothing:

* **Inventory.** `reservations.confirm` locks the Room Type rows, re-reads
  availability under that lock, applies overbooking policy and rate
  restrictions, and drives the state machine. A walk-in never passes
  `allow_overbooking`: selling a room the house does not have is a management
  decision, not a side effect of someone appearing in the lobby.
* **Pricing.** The Reservation controller prices the booking through
  `price_reservation` / `get_rate_breakdown`. A caller-supplied room rate is
  never accepted here, so an agent cannot type a rate into the walk-in form
  and bypass the rate grid (HPMS-DEC-030, server authority).
* **The physical room.** `stays.check_in` owns duplicate-active-stay
  prevention, the blacklist check, the identification requirement, the deposit
  requirement, room readiness and who may override it, Stay creation, Folio
  creation, the room's move to Occupied, the Reservation's move to Checked In
  and the transfer of any deposit onto the folio. It also assigns the room, via
  `reservations.assign_room`, under a row lock on the room.

Why arrival is the business date
--------------------------------
A walk-in arrives "today", but "today" for a property is its business date, not
the server's calendar date. A property that has not yet run Night Audit is
still operating on yesterday's date, and `stays.check_in` refuses an arrival
that is after the business date. Deriving arrival from `nowdate()` would make
walk-ins fail at exactly the hotels that are running late - the ones busiest at
the desk.

Why the whole thing is one savepoint
------------------------------------
Confirmation holds inventory. Check-in is what puts a body in the room. If
confirmation succeeded and check-in then failed - a dirty room, an unpaid
deposit, missing ID - a Confirmed walk-in reservation would be left behind
holding a room for a guest who is still standing in the lobby, invisible to the
arrivals board because nobody expects a walk-in to be pending. Every step here
runs inside one savepoint so a failure anywhere unwinds the whole unit.
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate

from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services.base import transaction
from hospitality_pms.services.exceptions import throw
from hospitality_pms.services.property import get_business_date, get_property

ROOM_DOCTYPE = "Hotel Room"
ROOM_LINE_DOCTYPE = "Reservation Room"

#: A walk-in is recorded as its own reservation type and its own booking
#: source. Both matter to reporting: the type drives the operational mix, the
#: source drives channel contribution, and a walk-in that arrives labelled
#: "Individual/Direct" makes the house look like it converted a booking it
#: never received.
WALK_IN_TYPE = "Walk In"
WALK_IN_SOURCE = "Walk In"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def get_context(property_name: str) -> dict:
	"""What the walk-in form needs before the agent types anything.

	The business date lives here rather than in the frontend because it is the
	one date the whole operation hangs off, and a browser clock is not an
	acceptable source for it. Handing the frontend a ready default departure
	(one night) also keeps the common case - "a room for tonight" - to a single
	confirmation rather than a date picker.
	"""
	business_date = get_business_date(property_name)

	return {
		"property": property_name,
		"business_date": str(business_date),
		"default_departure_date": str(getdate(add_days(business_date, 1))),
		"currency": get_property(property_name).currency,
	}


# ---------------------------------------------------------------------------
# Guards that must run before anything is created
# ---------------------------------------------------------------------------


def _assert_stay_dates(arrival, departure):
	"""Refuse a stay that does not span at least one night.

	Checked before the reservation is built so a mistyped departure costs a
	message rather than an insert, a price lookup and a rollback.
	"""
	if getdate(departure) < getdate(arrival):
		throw(
			_("Departure {0} must not be before the arrival date {1}.").format(
				getdate(departure), getdate(arrival)
			)
		)
	# a departure on the arrival date is a day-use or hourly stay: the reservation controller
	# accepts it only on such a rate plan, with its hours


def _assert_room_matches(property_name: str, room: str, room_type: str):
	"""Refuse a room that is not this property's, or not of this room type.

	`reservations.assign_room` enforces both of these too, but it only runs
	after the reservation exists and has been confirmed. Catching the mismatch
	here means the agent is told "that room is a Deluxe, you asked for a
	Standard" instead of watching a confirmed booking be rolled back. This is a
	shape check only - whether the room is sellable, clean and free is decided
	under a lock by the room and stay services, and is deliberately not
	pre-judged here.
	"""
	details = frappe.db.get_value(ROOM_DOCTYPE, room, ["property", "room_type"], as_dict=True)

	if not details:
		throw(_("Room {0} does not exist.").format(room))

	if details.property != property_name:
		throw(_("Room {0} belongs to another property.").format(room))

	if details.room_type != room_type:
		throw(
			_("Room {0} is a {1}, but this walk-in is for {2}.").format(
				room, details.room_type, room_type
			)
		)


# ---------------------------------------------------------------------------
# The walk-in
# ---------------------------------------------------------------------------


def create_walk_in(
	*,
	property_name: str,
	guest: str,
	departure_date,
	room_type: str,
	room: str,
	adults: int = 1,
	children: int = 0,
	rate_plan: str | None = None,
	billing_instructions: str | None = None,
	allow_unready_room: bool = False,
	readiness_reason: str | None = None,
	special_requests: str | None = None,
	arrival_time=None,
	departure_time=None,
	blocks: int = 1,
) -> dict:
	"""Book and check in a guest who is standing at the desk.

	Creates one normal Reservation with a single room line, confirms it through
	`reservations.confirm` and checks it in through `stays.check_in`. The guest
	must already exist as a Guest record; creating the guest is a separate step
	so that identification capture, blacklist screening and duplicate matching
	all happen where they are owned.

	Every blocker raised downstream - blacklist, missing identification, an
	unpaid deposit, a room that is not ready, no availability, a stop-sell -
	propagates to the caller unchanged. A walk-in is a fast path, never a
	permissive one: nothing here waives a deposit, skips an identity check or
	invokes a manager override on the agent's behalf. If the desk needs an
	override, a manager performs it explicitly through the service that owns it.
	"""
	# Checked explicitly rather than left to the ORM: a missing departure would
	# reach `getdate(None)`, which silently answers "today" and would book a
	# stay of zero nights.
	if not guest:
		throw(_("A guest record is required for a walk-in."))

	if not departure_date:
		throw(_("A departure date is required for a walk-in."))

	if not room_type or not room:
		throw(_("A room type and a specific room are required for a walk-in."))

	# "Today" is the property's operating day, never the server's calendar day.
	arrival_date = get_business_date(property_name)
	departure_date = getdate(departure_date)

	_assert_stay_dates(arrival_date, departure_date)
	_assert_room_matches(property_name, room, room_type)

	adults = max(int(adults or 1), 1)
	children = max(int(children or 0), 0)

	# One savepoint around create + confirm + check-in. A walk-in that got as
	# far as Confirmed and then failed at check-in would silently hold a room
	# for a guest nobody is expecting: it is not on the arrivals board a front
	# desk actually watches, and no agent is standing in it. Rolling the unit
	# back returns the inventory immediately. The savepoint makes that true for
	# every caller - request, Desk action or background job - rather than
	# relying on the HTTP handler to roll back for us, and nothing here commits.
	with transaction():
		reservation_doc = frappe.get_doc(
			{
				"doctype": reservation_service.RESERVATION_DOCTYPE,
				"property": property_name,
				"reservation_status": reservation_service.DRAFT,
				"reservation_type": WALK_IN_TYPE,
				"booking_source": WALK_IN_SOURCE,
				"guest": guest,
				"arrival_date": arrival_date,
				"departure_date": departure_date,
				"rate_plan": rate_plan,
				"special_requests": special_requests,
				# No room_rate and no total: the controller prices the line from
				# the rate grid on validate. A rate that arrived from a browser
				# is not a rate.
				"rooms": [
					{
						"room_type": room_type,
						"rooms": 1,
						"arrival_date": arrival_date,
						"departure_date": departure_date,
						"adults": adults,
						"children": children,
						"rate_plan": rate_plan,
						"special_requests": special_requests,
						"arrival_time": arrival_time,
						"departure_time": departure_time,
						"blocks": max(int(blocks or 1), 1),
					}
				],
			}
		).insert()

		room_line = reservation_doc.rooms[0].name

		# Confirmation is what commits inventory, under the room-type lock. It
		# is called, never bypassed, and never with allow_overbooking.
		reservation_service.confirm(
			reservation_doc.name,
			reason=_("Walk-in booked at the front desk"),
		)

		# Check-in owns the room assignment, every arrival guard, the Stay, the
		# Folio, the room's occupancy transition and the deposit transfer.
		checked_in = stay_service.check_in(
			reservation_doc.name,
			room_line,
			room,
			allow_unready_room=allow_unready_room,
			readiness_reason=readiness_reason,
			billing_instructions=billing_instructions,
		)

	return _summarise(reservation_doc.name, room_line, checked_in)


def _summarise(reservation: str, room_line: str, checked_in: dict) -> dict:
	"""Everything the front desk needs on the confirmation screen.

	Read back from the database rather than from the in-memory documents: the
	services write status, rate and totals with `frappe.db.set_value`, so the
	documents this module still holds are stale by the time check-in returns.
	"""
	header = frappe.db.get_value(
		reservation_service.RESERVATION_DOCTYPE,
		reservation,
		[
			"guest",
			"guest_name",
			"arrival_date",
			"departure_date",
			"nights",
			"currency",
			"total_amount",
			"reservation_status",
		],
		as_dict=True,
	)

	line = frappe.db.get_value(
		ROOM_LINE_DOCTYPE, room_line, ["room_type", "rate_plan", "room_rate"], as_dict=True
	)

	return {
		"reservation": reservation,
		"room_line": room_line,
		"stay": checked_in["stay"],
		"folio": checked_in["folio"],
		"room": checked_in["room"],
		"guest": header.guest,
		"guest_name": header.guest_name,
		"arrival_date": str(getdate(header.arrival_date)),
		"departure_date": str(getdate(header.departure_date)),
		"nights": int(header.nights or 0),
		"room_type": line.room_type,
		"rate_plan": line.rate_plan,
		"room_rate": flt(line.room_rate),
		"total_amount": flt(header.total_amount),
		"currency": header.currency,
		"reservation_status": header.reservation_status,
		"stay_status": frappe.db.get_value(
			stay_service.STAY_DOCTYPE, checked_in["stay"], "stay_status"
		),
	}
