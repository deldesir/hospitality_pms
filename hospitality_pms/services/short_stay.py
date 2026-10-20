"""Day use and hourly stays: a room sold for part of one day.

A hotel near a bus station or an airport sells rooms by the day (a shower and a nap between
flights, a pool afternoon, a meeting) and by the hour (two, three or six hours), next to its
overnight trade. The PMS counted rooms by the night and refused a departure on the arrival
date, so neither existed.

The model here keeps the night engine untouched and adds a second currency of inventory:

* A **short stay** is a reservation line whose departure date equals its arrival date, with
  an arrival and a departure time on that date. Its rate plan says which kind it is: *Day
  Use* (one price for the day's hours) or *Hourly* (a price per block of `block_hours`,
  `blocks` of them). The line consumes **no night**: the night audit never charges it.
* Its charge is posted to the folio **at check-in** (charge types *Day Use* and *Hourly*,
  mapped by the Posting Profile like any other), and it is settled at checkout like a night.
* **Inventory** is the room type's sellable rooms on that date, less the rooms overnight
  guests hold through the hours asked for, less other short stays overlapping those hours.
  An overnight guest arriving that day holds a room from the property's check-in time; one
  departing that day holds it until check-out time. Symmetrically, a short stay whose hours
  run past check-in time holds a room against overnight arrivals on its date.
* **Assignment** applies the same rules to specific rooms, so a desk is never offered a room
  the engine would refuse.

Everything a short stay does not change (deposits, policies, folios, housekeeping on
checkout, guest records, posting) is left to the services that own it.
"""

import datetime as dt

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from hospitality_pms.services.availability import (
	HOLDING_RESERVATION_STATES,
	RESERVATION_ROOM_DOCTYPE,
	_blocked_by_night,
	_blocks,
	_is_sellable,
	_physical_rooms,
	short_lines_on,
	stay_nights,
)
from hospitality_pms.services.exceptions import AvailabilityError, HospitalityPMSError, throw
from hospitality_pms.services.property import get_property

NIGHT = "Night"
DAY_USE = "Day Use"
HOURLY = "Hourly"
SHORT_KINDS = (DAY_USE, HOURLY)
RATE_PLAN_DOCTYPE = "Rate Plan"
DEFAULT_CHECK_IN = 14 * 60
DEFAULT_CHECK_OUT = 12 * 60


def minutes(value) -> int | None:
	"""A Time value (timedelta from the database, a string from a form, a time) as minutes."""
	if value in (None, ""):
		return None
	if isinstance(value, dt.timedelta):
		return int(value.total_seconds() // 60)
	if isinstance(value, dt.time):
		return value.hour * 60 + value.minute
	if isinstance(value, dt.datetime):
		return value.hour * 60 + value.minute
	parts = str(value).split(":")
	return int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else None


def as_time(total_minutes: int) -> str:
	return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}:00"


def plan_kind(rate_plan: str | None) -> str:
	if not rate_plan or not frappe.db.has_column(RATE_PLAN_DOCTYPE, "stay_kind"):
		return NIGHT
	return frappe.db.get_value(RATE_PLAN_DOCTYPE, rate_plan, "stay_kind") or NIGHT


def is_short_plan(rate_plan: str | None) -> bool:
	return plan_kind(rate_plan) in SHORT_KINDS


def is_same_day(arrival, departure) -> bool:
	return getdate(arrival) == getdate(departure)


def house_hours(property_name: str) -> tuple[int, int]:
	"""Check-in and check-out time of the property, in minutes."""
	prop = get_property(property_name)
	return (minutes(prop.check_in_time) or DEFAULT_CHECK_IN, minutes(prop.check_out_time) or DEFAULT_CHECK_OUT)


def window_for(rate_plan: str, *, arrival_time=None, departure_time=None, blocks: int = 1) -> tuple[int, int]:
	"""The hours a short stay on this plan holds a room, in minutes of the day.

	Day use: the plan's hours, narrowed by what the booking says. Hourly: from the arrival
	time, `blocks` blocks of the plan's block hours, unless a departure time is given.
	"""
	plan = frappe.db.get_value(
		RATE_PLAN_DOCTYPE, rate_plan, ["stay_kind", "block_hours", "day_use_from", "day_use_until"], as_dict=True
	)
	if not plan or (plan.stay_kind or NIGHT) not in SHORT_KINDS:
		throw(
			_("Rate plan {0} sells nights: a stay on it needs a departure date after its arrival.").format(rate_plan),
			exc=HospitalityPMSError,
		)
	if plan.stay_kind == DAY_USE:
		start = minutes(arrival_time) if arrival_time else minutes(plan.day_use_from)
		end = minutes(departure_time) if departure_time else minutes(plan.day_use_until)
		if start is None or end is None:
			throw(
				_("Day-use plan {0} has no hours: set Day Use From and Until on the plan, or give the booking its times.").format(rate_plan),
				exc=HospitalityPMSError,
			)
	else:
		start = minutes(arrival_time)
		if start is None:
			throw(_("An hourly stay needs an arrival time."), exc=HospitalityPMSError)
		hours = cint(plan.block_hours)
		if hours <= 0:
			throw(_("Hourly plan {0} has no block length (Block Hours).").format(rate_plan), exc=HospitalityPMSError)
		end = minutes(departure_time) if departure_time else start + hours * 60 * max(cint(blocks), 1)
	if end <= start:
		throw(_("The departure time must be after the arrival time on the same day."), exc=HospitalityPMSError)
	if end > 24 * 60:
		throw(_("A short stay ends on its own day; these hours run past midnight."), exc=HospitalityPMSError)
	return start, end


def apply_window(line, rate_plan: str | None) -> None:
	"""Write the resolved hours onto a reservation line (a form may have left them blank)."""
	start, end = window_for(
		rate_plan, arrival_time=line.get("arrival_time"), departure_time=line.get("departure_time"), blocks=line.get("blocks") or 1
	)
	line.arrival_time = as_time(start)
	line.departure_time = as_time(end)
	line.blocks = max(cint(line.get("blocks")), 1)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
	return a_start < b_end and b_start < a_end


def charge_type_for(kind: str) -> str:
	return DAY_USE if kind == DAY_USE else HOURLY


def check_short_availability(
	property_name: str,
	room_type: str,
	date,
	start: int,
	end: int,
	rooms: int = 1,
	*,
	exclude_reservation: str | None = None,
	exclude_line: str | None = None,
	current: bool = False,
) -> dict:
	"""Raise unless `rooms` of `room_type` are free on `date` between the two times.

	Same contract as `availability.check_availability`: a caller about to commit inventory
	holds the room-type lock and passes `current=True`.
	"""
	date = getdate(date)
	rooms = max(int(rooms or 1), 1)
	check_in, check_out = house_hours(property_name)
	physical = [room for room in _physical_rooms(property_name, room_type) if _is_sellable(room)]
	blocks = _blocks(property_name, date, date + dt.timedelta(days=1), room_type)
	rooms_blocked, quantity_blocked = _blocked_by_night(blocks, [date], {room["name"]: room for room in physical})
	sellable = len([room for room in physical if room["name"] not in rooms_blocked.get(date, set())])
	sellable -= quantity_blocked.get(date, {}).get(room_type, 0)

	taken = 0
	for row in _overnight_lines_touching(property_name, date, room_type, exclude_reservation, exclude_line, current):
		arrival, departure = getdate(row.arrival_date), getdate(row.departure_date)
		count = max(int(row.rooms or 1), 1)
		if arrival <= date < departure:                      # holds the night: in the room all day
			if arrival == date and end <= check_in:           # ...except an arrival still to come
				continue
			taken += count
		elif departure == date and start < check_out:        # leaving today, still in until check-out
			taken += count
	for row in short_lines_on(property_name, date, room_type, exclude_reservation=exclude_reservation, exclude_line=exclude_line, current=current):
		if overlaps(start, end, minutes(row.arrival_time) or 0, minutes(row.departure_time) or 0):
			taken += max(int(row.rooms or 1), 1)
	available = sellable - taken
	if available < rooms:
		throw(
			_("Only {0} room(s) of type {1} are free on {2} between {3} and {4}; {5} requested.").format(
				max(available, 0), room_type, date, as_time(start)[:5], as_time(end)[:5], rooms
			),
			exc=AvailabilityError,
		)
	return {"room_type": room_type, "rooms": rooms, "min_available": available, "overbooking_limit": 0}


def _overnight_lines_touching(property_name, date, room_type, exclude_reservation, exclude_line, current) -> list:
	"""Holding overnight lines that hold the night of `date` or depart on it."""
	filters = {
		"property": property_name,
		"reservation_status": ("in", HOLDING_RESERVATION_STATES),
		"arrival_date": ("<=", date),
		"departure_date": (">=", date),
	}
	if room_type:
		filters["room_type"] = room_type
	if exclude_reservation:
		filters["parent"] = ("!=", exclude_reservation)
	if exclude_line:
		filters["name"] = ("!=", exclude_line)
	fields = ["name", "arrival_date", "departure_date", "rooms", "assigned_room"]
	if current:
		rows = frappe.db.get_values(RESERVATION_ROOM_DOCTYPE, filters, fields, as_dict=True, order_by=None, for_update=True) or []
	else:
		rows = frappe.get_all(RESERVATION_ROOM_DOCTYPE, filters=filters, fields=fields, limit_page_length=0)
	return [row for row in rows if getdate(row.arrival_date) != getdate(row.departure_date)]


def clashing_rooms(
	property_name: str,
	arrival,
	departure,
	*,
	arrival_time=None,
	departure_time=None,
	exclude_line: str | None = None,
	current: bool = False,
) -> set[str]:
	"""Assigned rooms a line with these dates (and hours, when same-day) cannot have.

	The question `availability._promised_rooms` and `reservations._assert_room_free` both
	ask; answered here once so the picker and the authority agree:

	* a short stay on D clashes with a room held overnight through D (unless the holder
	  arrives on D and the short stay ends before check-in time), with a room whose guest
	  departs on D while the short stay starts before check-out time, and with another short
	  stay on D whose hours overlap;
	* an overnight stay clashes with a short stay on any of its nights' dates, except one on
	  its arrival date that ends before check-in time.
	"""
	arrival, departure = getdate(arrival), getdate(departure)
	check_in, check_out = house_hours(property_name)
	rivals: set[str] = set()
	if arrival == departure:
		start, end = minutes(arrival_time) or 0, minutes(departure_time) or 24 * 60
		for row in _overnight_lines_touching(property_name, arrival, None, None, exclude_line, current):
			if not row.assigned_room:
				continue
			a, d = getdate(row.arrival_date), getdate(row.departure_date)
			if a <= arrival < d and not (a == arrival and end <= check_in):
				rivals.add(row.assigned_room)
			elif d == arrival and start < check_out:
				rivals.add(row.assigned_room)
		for row in short_lines_on(property_name, arrival, None, exclude_line=exclude_line, current=current):
			if row.assigned_room and overlaps(start, end, minutes(row.arrival_time) or 0, minutes(row.departure_time) or 0):
				rivals.add(row.assigned_room)
		return rivals
	for night in stay_nights(arrival, departure):
		for row in short_lines_on(property_name, night, None, exclude_line=exclude_line, current=current):
			if not row.assigned_room:
				continue
			if getdate(night) == arrival and (minutes(row.departure_time) or 24 * 60) <= check_in:
				continue
			rivals.add(row.assigned_room)
	return rivals


def short_breakdown(
	property_name: str,
	room_type: str,
	date,
	rate_plan: str,
	*,
	arrival_time=None,
	departure_time=None,
	blocks: int = 1,
	adults: int = 2,
	children: int = 0,
	extra_beds: int = 0,
	rooms: int = 1,
) -> dict:
	"""The price of a short stay, in the shape `rates.get_rate_breakdown` returns.

	One rate line dated the stay's day. Day use: the room type's rate on the plan, once.
	Hourly: that rate per block, `blocks` times. Occupancy supplements apply once, as on a
	night. Complimentary and house-use plans price at zero as they do for nights.
	"""
	from hospitality_pms.services.rates import ZERO_RATE_TYPES, _first_positive, _plan_room_type_row, _room_type_fallback

	plan = frappe.db.get_value(
		RATE_PLAN_DOCTYPE, rate_plan, ["rate_type", "currency", "includes_taxes", "stay_kind", "block_hours"], as_dict=True
	)
	start, end = window_for(rate_plan, arrival_time=arrival_time, departure_time=departure_time, blocks=blocks)
	plan_row = _plan_room_type_row(rate_plan, room_type) or {}
	fallback = _room_type_fallback(room_type)
	currency = plan.get("currency") or get_property(property_name).currency
	units = max(cint(blocks), 1) if plan.stay_kind == HOURLY else 1
	is_zero_rated = plan.get("rate_type") in ZERO_RATE_TYPES
	if is_zero_rated:
		rate = 0.0
		extra_adult_total = extra_child_total = extra_bed_total = 0.0
	else:
		unit_rate = _first_positive(plan_row.get("base_rate"), fallback.get("base_rate"))
		if unit_rate is None:
			throw(_("No rate is configured for room type {0} on plan {1}.").format(room_type, rate_plan), exc=HospitalityPMSError)
		base_occupancy = int(fallback.get("base_occupancy") or 2)
		chargeable_adults = max(int(adults or 0) - base_occupancy, 0)
		extra_adult = flt(_first_positive(plan_row.get("extra_adult_charge"), fallback.get("extra_adult_charge")) or 0)
		extra_child = flt(_first_positive(plan_row.get("extra_child_charge"), fallback.get("extra_child_charge")) or 0)
		extra_bed = flt(plan_row.get("extra_bed_charge") or fallback.get("extra_bed_charge") or 0)
		rate = flt(unit_rate) * units
		extra_adult_total = chargeable_adults * extra_adult
		extra_child_total = int(children or 0) * extra_child
		extra_bed_total = int(extra_beds or 0) * extra_bed
	net = flt(rate) + extra_adult_total + extra_child_total + extra_bed_total
	line = {
		"rate_date": str(getdate(date)),
		"room_type": room_type,
		"rate_plan": rate_plan,
		"rate": flt(rate),
		"extra_adult_charge": extra_adult_total,
		"extra_child_charge": extra_child_total,
		"extra_bed_charge": extra_bed_total,
		"discount_amount": 0.0,
		"net_rate": net,
		"is_complimentary": 1 if is_zero_rated else 0,
	}
	rooms = max(int(rooms or 1), 1)
	return {
		"rate_plan": rate_plan,
		"rate_type": plan.get("rate_type"),
		"currency": currency,
		"includes_taxes": bool(plan.get("includes_taxes")),
		"room_type": room_type,
		"nights": 0,
		"rooms": rooms,
		"lines": [line],
		"total_per_room": flt(net, 2),
		"total_amount": flt(net * rooms, 2),
		"average_nightly_rate": flt(net, 2),
		"stay_kind": plan.stay_kind,
		"arrival_time": as_time(start),
		"departure_time": as_time(end),
		"blocks": units,
	}
