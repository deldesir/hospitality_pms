"""Availability: what can actually be sold, per night, per room type.

This is the authoritative availability decision (SAS section 3.5). The frontend
may show an optimistic view, but nothing is ever held or confirmed on the
strength of a client-side calculation.

Nights, not days
----------------
A stay from the 10th to the 12th consumes the nights of the 10th and the 11th.
The departure date is never consumed. Every function here works in nights, and
`nights_between` is the single place that rule is expressed.

What consumes inventory
-----------------------
1. Rooms that are not sellable at all - inactive, out of order, out of service,
   under maintenance, or with a blocking inventory status.
2. Room blocks covering the night - either a specific room, or a quantity of a
   room type (maintenance, group holds, house use).
3. Confirmed and in-house reservations. Reservations arrive in HPMS-0.9.0;
   until then `_sold_by_night` returns nothing and the arithmetic still holds.

Overbooking is added back on top, and only up to the property's configured
limit (SAS section 3.5).

Snapshot reads and current reads
-------------------------------
Every count here is a plain read by default, and must stay that way: the search
screen, the calendar and the room rack ask these questions constantly and must
never take a row lock to answer them.

That default is wrong for the handful of callers that are about to *commit*
inventory. Those hold a lock while they decide - and **a lock serialises without
refreshing**. Under REPEATABLE READ a transaction's consistent read view is
established at its first read and answers every plain read afterwards, so a
caller that queued behind another writer still counts the rooms as they stood
before that writer committed, grants itself the room that has just gone, and
oversells the house. No amount of locking fixes it, because within one
transaction there is no plain read that will ever see the winner's commit (N1;
`tests/test_locking.py` pins the hazard, `services/base.py` documents it).

So `check_availability`, `check_demand` and `get_availability` take `current`.
Left `False`, nothing changes. Set `True`, the sold count comes from a locking
read and is therefore the latest committed state as of the moment the lock was
granted - the same contract `lock_and_read`/`lock_and_find` offer for a single
row. It is set only on the paths that commit inventory: `reservations.confirm`,
`change_line_interval`, `add_room_line`, `change_line_room_type` and
`stays.extend_stay`.

Only the *sold* count is made current, because that is the number those callers
themselves change. Physical rooms and room blocks are configuration, changed by
operations that do not race these.

**What a current read costs, and the open question it raises.** A locking read
locks every row it examines until the transaction ends, and MariaDB chooses the
index, so the rows locked are a range rather than the handful counted. That is
harmless between the commit paths themselves - they already queue on the Room
Type row before they get here - but it is *not* harmless against a caller that is
holding its own reservation's rows (`lock_and_get_doc` locks a document's child
rows) while it waits for that same Room Type lock. Those two can close a cycle,
and on this bench they do: see HPMS-QA-16.7.2-C in
`tests/test_reservation_modification_concurrency.py`. Resolving it means deciding
whether the Room Type lock should be taken *before* the Reservation rather than
after it - a change to the documented chain, and not one to make in passing.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from hospitality_pms.services.base import lock_documents, require_role
from hospitality_pms.services.exceptions import AvailabilityError, OverbookingError, throw
from hospitality_pms.services.property import get_property, get_settings
from hospitality_pms.services.rooms import BLOCKING_INVENTORY, BLOCKING_MAINTENANCE

ROOM_DOCTYPE = "Hotel Room"
ROOM_TYPE_DOCTYPE = "Room Type"
BLOCK_DOCTYPE = "Room Block"
RESERVATION_ROOM_DOCTYPE = "Reservation Room"

#: Reservation states that hold inventory. Used from HPMS-0.9.0 onward.
HOLDING_RESERVATION_STATES = ("Confirmed", "Guaranteed", "Checked In")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def nights_between(arrival, departure) -> list:
	"""The nights a stay consumes: arrival inclusive, departure exclusive."""
	arrival = getdate(arrival)
	departure = getdate(departure)

	if departure <= arrival:
		throw(
			_("Departure {0} must be after arrival {1}.").format(departure, arrival),
			exc=AvailabilityError,
		)

	nights = []
	current = arrival

	while current < departure:
		nights.append(current)
		current += timedelta(days=1)

	return nights


# ---------------------------------------------------------------------------
# Inventory components
# ---------------------------------------------------------------------------


def stay_nights(arrival, departure) -> list:
	"""The nights a stay consumes: none for a same-day (day-use or hourly) stay, which holds a
	room for hours of one date instead and is counted by `services.short_stay`."""
	if getdate(arrival) == getdate(departure):
		return []
	return nights_between(arrival, departure)


def short_lines_on(
	property_name: str,
	date,
	room_type: str | None = None,
	*,
	exclude_reservation: str | None = None,
	exclude_line: str | None = None,
	current: bool = False,
) -> list:
	"""Holding same-day reservation lines on `date`: the day-use and hourly stays."""
	if not frappe.db.table_exists(RESERVATION_ROOM_DOCTYPE) or not frappe.db.has_column(RESERVATION_ROOM_DOCTYPE, "arrival_time"):
		return []
	date = getdate(date)
	filters = {
		"property": property_name,
		"reservation_status": ("in", HOLDING_RESERVATION_STATES),
		"arrival_date": date,
		"departure_date": date,
	}
	if room_type:
		filters["room_type"] = room_type
	if exclude_reservation:
		filters["parent"] = ("!=", exclude_reservation)
	if exclude_line:
		filters["name"] = ("!=", exclude_line)
	fields = ["name", "parent", "room_type", "rooms", "arrival_time", "departure_time", "assigned_room"]
	if current:
		return frappe.db.get_values(RESERVATION_ROOM_DOCTYPE, filters, fields, as_dict=True, order_by=None, for_update=True) or []
	return frappe.get_all(RESERVATION_ROOM_DOCTYPE, filters=filters, fields=fields, limit_page_length=0)


def _minutes(value) -> int | None:
	if value in (None, ""):
		return None
	if isinstance(value, timedelta):
		return int(value.total_seconds() // 60)
	if hasattr(value, "hour"):
		return value.hour * 60 + value.minute
	parts = str(value).split(":")
	return int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else None


def _short_held_by_night(property_name: str, nights: list, room_type, exclude_reservation, current) -> dict:
	"""Rooms short stays hold against overnight arrivals, per night: those whose hours run
	past the property's check-in time on that date."""
	held = defaultdict(lambda: defaultdict(int))
	if not nights:
		return held
	check_in = _minutes(get_property(property_name).check_in_time) or 14 * 60
	for night in nights:
		for row in short_lines_on(property_name, night, room_type, exclude_reservation=exclude_reservation, current=current):
			if (_minutes(row.departure_time) or 24 * 60) > check_in:
				held[night][row.room_type] += int(row.rooms or 1)
	return held


def _physical_rooms(property_name: str, room_type: str | None = None) -> list[dict]:
	"""Active rooms and their current blocking state."""
	filters = {"property": property_name, "is_active": 1}
	if room_type:
		filters["room_type"] = room_type

	return frappe.get_all(
		ROOM_DOCTYPE,
		filters=filters,
		fields=["name", "room_type", "maintenance_status", "inventory_status", "occupancy_status"],
		limit_page_length=0,
	)


def _is_sellable(room: dict) -> bool:
	"""Whether a room is sellable at all, ignoring dates.

	Housekeeping status is deliberately not consulted: a dirty room is still
	sellable inventory, it just needs cleaning before the guest walks in.
	Treating dirty as unavailable would collapse availability every morning.
	"""
	return (
		room.get("maintenance_status") not in BLOCKING_MAINTENANCE
		and room.get("inventory_status") not in BLOCKING_INVENTORY
	)


def _blocks(property_name: str, arrival, departure, room_type: str | None = None) -> list[dict]:
	"""Submitted, active room blocks overlapping the stay's nights."""
	last_night = add_days(getdate(departure), -1)

	filters = {
		"property": property_name,
		"docstatus": 1,
		"status": "Active",
		"from_date": ("<=", last_night),
		"to_date": (">=", getdate(arrival)),
	}

	blocks = frappe.get_all(
		BLOCK_DOCTYPE,
		filters=filters,
		fields=["name", "room", "room_type", "rooms_blocked", "from_date", "to_date", "block_type"],
		limit_page_length=0,
	)

	if room_type:
		blocks = [b for b in blocks if not b["room_type"] or b["room_type"] == room_type]

	return blocks


def _blocked_by_night(blocks: list[dict], nights: list, rooms_by_name: dict) -> tuple[dict, dict]:
	"""Split blocks into specific-room and quantity effects, per night.

	Specific-room blocks are tracked as a set of room names so they cannot be
	double counted against a room that is already unsellable. Type-level blocks
	are plain quantities.
	"""
	rooms_blocked = defaultdict(set)
	quantity_blocked = defaultdict(lambda: defaultdict(int))

	for block in blocks:
		block_from = getdate(block["from_date"])
		block_to = getdate(block["to_date"])

		for night in nights:
			if not (block_from <= night <= block_to):
				continue

			if block["room"]:
				rooms_blocked[night].add(block["room"])
			elif block["room_type"]:
				quantity_blocked[night][block["room_type"]] += max(int(block["rooms_blocked"] or 1), 1)

	return rooms_blocked, quantity_blocked


def _sold_by_night(
	property_name: str,
	nights: list,
	room_type: str | None = None,
	exclude_reservation: str | None = None,
	*,
	current: bool = False,
) -> dict:
	"""Rooms already committed to reservations, per night, per room type.

	Returns an empty result until the reservation build lands, so availability
	is correct at every stage rather than importing a DocType that does not
	exist yet.

	`current` decides whether the rows come from this transaction's snapshot or
	from a locking read - see the module docstring. A caller deciding under a
	lock must pass `True`, or it counts the house as it was before the writer it
	just queued behind committed (N1).
	"""
	if not frappe.db.table_exists(RESERVATION_ROOM_DOCTYPE):
		return defaultdict(lambda: defaultdict(int))

	filters = {
		"property": property_name,
		"reservation_status": ("in", HOLDING_RESERVATION_STATES),
		"arrival_date": ("<=", nights[-1]),
		"departure_date": (">", nights[0]),
	}

	if room_type:
		filters["room_type"] = room_type

	if exclude_reservation:
		filters["parent"] = ("!=", exclude_reservation)

	fields = ["room_type", "arrival_date", "departure_date", "rooms"]

	if current:
		# `SELECT ... FOR UPDATE`, so InnoDB answers from the current row
		# versions rather than from this transaction's read view, and so the
		# rows counted stay counted until this transaction ends - a rival
		# cannot commit a booking into the interval between this count and the
		# write it justifies.
		rows = (
			frappe.db.get_values(
				RESERVATION_ROOM_DOCTYPE,
				filters,
				fields,
				as_dict=True,
				order_by=None,
				for_update=True,
			)
			or []
		)
	else:
		rows = frappe.get_all(
			RESERVATION_ROOM_DOCTYPE, filters=filters, fields=fields, limit_page_length=0
		)

	sold = defaultdict(lambda: defaultdict(int))

	for row in rows:
		for night in stay_nights(row["arrival_date"], row["departure_date"]):
			if night in nights:
				sold[night][row["room_type"]] += int(row.get("rooms") or 1)

	return sold


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_availability(
	property_name: str,
	arrival,
	departure,
	room_type: str | None = None,
	exclude_reservation: str | None = None,
	*,
	current: bool = False,
) -> dict:
	"""Availability per night, per room type.

	Returns a structure the search screen and the room rack both render from:

	    {
	      "nights": [...],
	      "room_types": {
	         "DLX": {
	            "physical": 40,
	            "by_night": {date: {"sellable": .., "blocked": .., "sold": .., "available": ..}},
	            "min_available": 12,
	         }
	      }
	    }

	`min_available` is what a booking decision uses: a stay is only sellable if
	every one of its nights has room.

	`current` is passed straight to `_sold_by_night`: leave it alone for anything
	a screen renders, set it for a caller that is about to commit inventory under
	a lock (see the module docstring).
	"""
	nights = nights_between(arrival, departure)
	rooms = _physical_rooms(property_name, room_type)
	rooms_by_name = {room["name"]: room for room in rooms}

	blocks = _blocks(property_name, arrival, departure, room_type)
	rooms_blocked, quantity_blocked = _blocked_by_night(blocks, nights, rooms_by_name)
	sold = _sold_by_night(property_name, nights, room_type, exclude_reservation, current=current)
	short_held = _short_held_by_night(property_name, nights, room_type, exclude_reservation, current)

	overbooking = int(get_property(property_name).overbooking_limit or 0)

	by_type: dict[str, dict] = {}

	for room in rooms:
		by_type.setdefault(
			room["room_type"],
			{"physical": 0, "sellable_rooms": set(), "by_night": {}, "min_available": None},
		)
		by_type[room["room_type"]]["physical"] += 1

		if _is_sellable(room):
			by_type[room["room_type"]]["sellable_rooms"].add(room["name"])

	for type_name, bucket in by_type.items():
		for night in nights:
			# A room blocked for this night is removed from the sellable set,
			# which keeps a block on an already out-of-order room from being
			# counted twice.
			sellable = bucket["sellable_rooms"] - rooms_blocked.get(night, set())

			blocked_count = len(bucket["sellable_rooms"]) - len(sellable)
			quantity = quantity_blocked.get(night, {}).get(type_name, 0)
			sold_count = sold.get(night, {}).get(type_name, 0) + short_held.get(night, {}).get(type_name, 0)

			available = len(sellable) - quantity - sold_count

			bucket["by_night"][str(night)] = {
				"sellable": len(sellable),
				"blocked": blocked_count + quantity,
				"sold": sold_count,
				"available": max(available, 0),
				"available_with_overbooking": max(available + overbooking, 0),
			}

			current_min = bucket["min_available"]
			bucket["min_available"] = available if current_min is None else min(current_min, available)

		bucket["min_available"] = max(bucket["min_available"] or 0, 0)
		bucket["overbooking_limit"] = overbooking
		# The set is an implementation detail; do not ship it to the client.
		del bucket["sellable_rooms"]

	return {
		"property": property_name,
		"arrival": str(getdate(arrival)),
		"departure": str(getdate(departure)),
		"nights": [str(night) for night in nights],
		"room_types": by_type,
	}


def check_availability(
	property_name: str,
	room_type: str,
	arrival,
	departure,
	rooms: int = 1,
	*,
	allow_overbooking: bool = False,
	exclude_reservation: str | None = None,
	current: bool = False,
) -> dict:
	"""Raise unless `rooms` of `room_type` can be sold for every night.

	Callers that are about to commit inventory must hold the room-type row
	first - see `lock_room_type` - **and pass `current=True`**. The lock alone is
	not enough: it serialises the two callers but leaves the loser counting from
	the read view it opened before the winner committed, so both are told the
	last room is free (N1, and the module docstring). Checking without either is
	fine for a search, never for a confirmation.
	"""
	rooms = max(int(rooms or 1), 1)

	availability = get_availability(
		property_name,
		arrival,
		departure,
		room_type,
		exclude_reservation=exclude_reservation,
		current=current,
	)

	bucket = availability["room_types"].get(room_type)

	if not bucket:
		throw(
			_("Room type {0} has no active rooms in property {1}.").format(room_type, property_name),
			exc=AvailabilityError,
		)

	limit_key = "available_with_overbooking" if allow_overbooking else "available"
	overbooking = bucket.get("overbooking_limit", 0)

	for night, figures in bucket["by_night"].items():
		if figures[limit_key] < rooms:
			throw(
				_("Only {0} room(s) of type {1} are available on {2}; {3} requested.").format(
					figures[limit_key], room_type, night, rooms
				),
				exc=AvailabilityError,
			)

	return {
		"room_type": room_type,
		"rooms": rooms,
		"min_available": bucket["min_available"],
		"overbooking_limit": overbooking,
	}


#: Who may sell a room the house does not have.
#:
#: Taken from the approved matrix rather than invented: these are exactly the
#: roles that may write `Room Inventory Restriction` - revenue, reservations
#: and front office management, plus hotel management and administration.
#: Overselling is a decision of the same kind, made by the same people.
#:
#: An ordinary Reservation Agent is deliberately absent. Selling past the
#: house's capacity commits the hotel to walking a guest, which is a
#: management decision and not a booking one.
OVERBOOKING_ROLES = (
	"Revenue Manager",
	"Reservation Manager",
	"Front Office Manager",
	"Hotel Manager",
	"General Manager",
	"Hospitality Administrator",
	"System Manager",
)


def authorise_overbooking(reason: str | None) -> str:
	"""Refuse an overbooking override that is switched off, unauthorised or unexplained.

	The configured numeric limit is enforced by `check_availability` and is not
	revisited here - it was already correct. What was missing was everything
	around it (P1-3):

	* `PMS Settings.enable_overbooking` existed and was read nowhere, so the
	  operator's switch for the whole feature did nothing.
	* No role was required, so an ordinary Reservation Agent oversold the house.
	* `reason` was optional, so `None` was accepted for a decision that commits
	  the hotel to walking a guest.

	Returns the cleaned reason, so the caller records the same text it was
	authorised against.

	Called before the target document is loaded: an override nobody is entitled
	to make should be refused before any inventory is touched or locked.
	"""
	if not get_settings().enable_overbooking:
		throw(
			_(
				"Overbooking is not enabled for this system. An administrator must turn it on "
				"in PMS Settings before a room can be sold beyond the house's capacity."
			),
			exc=OverbookingError,
		)

	require_role(OVERBOOKING_ROLES)

	cleaned = (reason or "").strip()

	if not cleaned:
		throw(
			_("A reason is required to sell beyond the available rooms."),
			exc=OverbookingError,
		)

	return cleaned


def overbooking_evidence(checks: list[dict], reason: str) -> dict:
	"""The audit record of an override: what was used, how far, and why.

	`checks` are the `check_availability` results for the lines being sold.
	`min_available` is what the house genuinely had, so anything sold past it
	is the override's actual impact rather than merely the limit it was allowed
	to consume.
	"""
	return {
		"overbooking_override": True,
		"overbooking_reason": reason,
		"overbooking_limit": max((int(check["overbooking_limit"] or 0) for check in checks), default=0),
		"overbooking_rooms": sum(
			max(int(check["rooms"]) - int(check["min_available"]), 0) for check in checks
		),
	}


def check_demand(
	property_name: str,
	lines,
	*,
	allow_overbooking: bool = False,
	exclude_reservation: str | None = None,
	current: bool = False,
) -> list[dict]:
	"""Check what a whole booking needs, night by night, not line by line.

	Once a three-room booking is three rows of one room each, asking
	`check_availability` once per row asks "is there a room free?" three times -
	and a house with one room left answers yes to all three. The demand has to
	be summed per room type per night before it is compared with anything.

	`current` carries the same meaning as it does on `check_availability`, and
	`confirm` - the one place a booking actually takes its inventory - sets it.
	Summing the demand correctly and then comparing it with a stale count sells
	the same room twice just as surely as not summing it at all.

	Returns one result per room type, in the shape `overbooking_evidence`
	expects, so the Wave-1 override audit keeps working unchanged.
	"""
	demand: dict[str, dict] = defaultdict(lambda: defaultdict(int))
	short_lines = []

	for line in lines:
		if getdate(line.arrival_date) == getdate(line.departure_date):
			short_lines.append(line)      # a day-use or hourly line: its own day slot, below
			continue
		for night in nights_between(line.arrival_date, line.departure_date):
			demand[line.room_type][night] += max(int(line.rooms or 1), 1)

	limit_key = "available_with_overbooking" if allow_overbooking else "available"
	checks = []
	if short_lines:
		from hospitality_pms.services.short_stay import check_short_availability, minutes

		for line in short_lines:
			checks.append(
				check_short_availability(
					property_name,
					line.room_type,
					line.arrival_date,
					minutes(line.arrival_time) or 0,
					minutes(line.departure_time) or 24 * 60,
					line.rooms,
					exclude_reservation=exclude_reservation,
					exclude_line=line.name,
					current=current,
				)
			)

	for room_type, per_night in demand.items():
		nights = sorted(per_night)

		availability = get_availability(
			property_name,
			nights[0],
			add_days(nights[-1], 1),
			room_type,
			exclude_reservation=exclude_reservation,
			current=current,
		)
		bucket = availability["room_types"].get(room_type)

		if not bucket:
			throw(
				_("Room type {0} has no active rooms in property {1}.").format(room_type, property_name),
				exc=AvailabilityError,
			)

		for night, needed in per_night.items():
			# `by_night` is keyed by the string form of the date, not the date.
			figures = bucket["by_night"].get(str(night))

			if not figures or figures[limit_key] < needed:
				available = figures[limit_key] if figures else 0

				throw(
					_("Only {0} room(s) of type {1} are available on {2}; {3} requested.").format(
						available, room_type, night, needed
					),
					exc=AvailabilityError,
				)

		checks.append(
			{
				"room_type": room_type,
				"rooms": max(per_night.values()),
				"min_available": bucket["min_available"],
				"overbooking_limit": bucket.get("overbooking_limit", 0),
			}
		)

	return checks


def lock_room_type(property_name: str, room_types) -> list[str]:
	"""Serialise inventory decisions for one or more room types.

	Two agents confirming the last room at the same moment must not both read
	"1 available". Locking the Room Type rows makes the read-check-write
	sequence atomic for the rest of the transaction; sorting the keys keeps
	concurrent multi-type bookings from deadlocking (HPMS-DEC-053).
	"""
	if isinstance(room_types, str):
		room_types = [room_types]

	return lock_documents(ROOM_TYPE_DOCTYPE, room_types)


def _promised_rooms(
	property_name: str, arrival, departure, *, exclude_line: str | None = None, arrival_time=None, departure_time=None
) -> set[str]:
	"""Rooms already given to a holding reservation line overlapping this interval.

	Overlap is `arrival < other.departure AND departure > other.arrival`, the
	same test `reservations._assert_room_free` applies under lock, so the list
	and the authority answer the same question.
	"""
	filters = {
		"property": property_name,
		"assigned_room": ("is", "set"),
		"reservation_status": ("in", HOLDING_RESERVATION_STATES),
		"arrival_date": ("<", getdate(departure)),
		"departure_date": (">", getdate(arrival)),
	}

	if exclude_line:
		filters["name"] = ("!=", exclude_line)

	promised = set(frappe.get_all("Reservation Room", filters=filters, pluck="assigned_room"))
	from hospitality_pms.services.short_stay import clashing_rooms

	return promised | clashing_rooms(
		property_name, arrival, departure, arrival_time=arrival_time, departure_time=departure_time, exclude_line=exclude_line
	)


def get_assignable_rooms(
	property_name: str,
	room_type: str | None,
	arrival,
	departure,
	*,
	allow_unready_housekeeping: bool = False,
	exclude_line: str | None = None,
	exclude_stay: str | None = None,
	arrival_time=None,
	departure_time=None,
) -> list[dict]:
	"""Specific rooms that can be assigned for the whole stay.

	Used by room assignment at confirmation and check-in. A room is offered
	only if it is sellable, unblocked for every night, physically ready unless
	overridden, **and not already promised to somebody else for these dates**.

	That last clause is the one this list was missing (N8). It filtered on the
	room's own state and never asked the question room assignment exists to
	answer, so it offered the room a guest was sleeping in and the room the
	afternoon arrival had been given that morning.

	The list is advisory - `reservations.assign_room` is the authority and
	re-checks all of this under a lock, because a room can be taken between
	rendering a list and clicking it. But the two must agree on the rules, or
	the agent is being offered a choice the system will then refuse.

	`exclude_line` is the line being assigned: a line must not be treated as
	its own rival when it is re-picking a room it already holds.

	`exclude_stay` is the stay being moved, for a room change: a guest must not
	be refused their own room as a destination-side rival.
	"""
	from hospitality_pms.services.rooms import (
		OCCUPIED_STATES,
		READY_HOUSEKEEPING,
		rooms_with_active_stays,
	)

	nights = stay_nights(arrival, departure) or [getdate(arrival)]   # a short stay: its one date
	rooms = _physical_rooms(property_name, room_type)
	promised = _promised_rooms(
		property_name, arrival, departure, exclude_line=exclude_line, arrival_time=arrival_time, departure_time=departure_time
	)

	# A guest in the room right now blocks an assignment starting today; it
	# says nothing about a stay beginning after they are due to leave, which
	# their own inventory interval already covers.
	business_date = getdate(get_property(property_name).business_date)
	occupancy_matters = getdate(arrival) <= business_date

	# Who is *actually* in a room, which neither of the two filters above can
	# answer (HPMS-UAT-16.7.5-B01).
	#
	# `promised` asks the inventory table, whose overlap test is
	# departure-exclusive so that a same-day turnover is not a clash. Correct for
	# selling nights, and blind to the guest whose departure date is today: their
	# line does not overlap an assignment arriving today, so they raise no clash
	# while they are still in the room. `occupancy_status` asks the room's own
	# flag, which `mark_due_out` moves to `Due Out` from the Night Audit every
	# morning - a state that is deliberately not in `OCCUPIED_STATES`, because a
	# Due Out room *is* re-lettable once its guest has gone. Neither notices that
	# they have not gone.
	#
	# So the Stay is asked directly, and it is asked with the same date rule the
	# occupancy flag already uses rather than a new one:
	#
	# * **Arrival today or earlier** - an immediate assignment, check-in or room
	#   move. Physical occupancy wins outright: whoever is in the room is in it
	#   now, whatever their dates say and whatever the flag says. This is the
	#   case that was broken, and it covers the Due Out guest, the overdue guest
	#   and room 402's persisted state alike.
	# * **Arrival in the future** - today's occupant is irrelevant. A guest
	#   departing today must not sterilise the room for next Tuesday, and a room
	#   that is full all week is held back by the *inventory* interval, which is
	#   the authority for future nights and needs no help here. Suppressing this
	#   filter for future dates is therefore not a relaxation; it is declining to
	#   answer a question with the wrong record.
	#
	# The list stays advisory either way. The mutations re-ask under the room's
	# lock through `rooms.assert_room_unoccupied`, because a room can be taken
	# between rendering this list and clicking it.
	occupied_now = (
		rooms_with_active_stays(property_name, exclude_stay=exclude_stay)
		if occupancy_matters
		else set()
	)

	blocks = _blocks(property_name, arrival, departure, room_type)
	rooms_blocked, _quantity = _blocked_by_night(blocks, nights, {r["name"]: r for r in rooms})

	blocked_any_night = set()
	for night in nights:
		blocked_any_night |= rooms_blocked.get(night, set())

	candidates = []

	for room in rooms:
		if not _is_sellable(room) or room["name"] in blocked_any_night:
			continue

		if room["name"] in promised or room["name"] in occupied_now:
			continue

		details = frappe.db.get_value(
			ROOM_DOCTYPE,
			room["name"],
			[
				"name",
				"room_number",
				"room_type",
				"floor",
				"housekeeping_status",
				"occupancy_status",
				"is_accessible",
				"is_smoking",
			],
			as_dict=True,
		)

		if occupancy_matters and details["occupancy_status"] in OCCUPIED_STATES:
			continue

		details["ready"] = details["housekeeping_status"] in READY_HOUSEKEEPING

		if not details["ready"] and not allow_unready_housekeeping:
			continue

		candidates.append(details)

	return sorted(candidates, key=lambda row: row["room_number"] or "")
