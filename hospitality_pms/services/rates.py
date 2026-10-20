"""Rate resolution: what a room costs on a given night, and whether it may be sold.

The server is the only authority on price (SAD section 5.2). A reservation
stores the resolved breakdown as a snapshot, so re-reading a reservation years
later shows what the guest was actually quoted, not what the rate grid says
today.

Resolution order for one night, most specific first:

1. `Daily Rate` for the exact property / plan / room type / date.
2. The rate plan's own room-type row.
3. The room type's fallback `base_rate`.

Restrictions are gathered from all three levels and the strictest wins - a
date-level stop sell is never softened by a permissive plan-level rule.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from hospitality_pms.services.availability import nights_between
from hospitality_pms.services.exceptions import RateError, throw
from hospitality_pms.services.property import get_property, resolve_operational_date

RATE_PLAN_DOCTYPE = "Rate Plan"
DAILY_RATE_DOCTYPE = "Daily Rate"
INVENTORY_RESTRICTION_DOCTYPE = "Room Inventory Restriction"
ROOM_TYPE_DOCTYPE = "Room Type"

#: Rate types that are sold at zero and must never silently fall back to a
#: positive base rate.
ZERO_RATE_TYPES = ("Complimentary", "House Use")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------------------
# Rate plan selection
# ---------------------------------------------------------------------------


def get_applicable_rate_plans(property_name: str, room_type: str, on_date=None) -> list[dict]:
	"""Active rate plans that cover this room type on this date.

	`on_date` is the night being priced, and pricing always supplies it. The
	fallback is the property's operating day rather than the calendar's, for
	the same reason contract validity is: a hotel still working yesterday must
	not have a rate plan expire on it a day early.
	"""
	on_date = resolve_operational_date(property_name, on_date)

	plans = frappe.get_all(
		RATE_PLAN_DOCTYPE,
		filters={
			"property": property_name,
			"is_active": 1,
			"valid_from": ("<=", on_date),
		},
		fields=["name", "rate_plan_name", "rate_type", "valid_upto", "display_order", "currency"],
		order_by="display_order asc, rate_plan_name asc",
		limit_page_length=0,
	)

	applicable = []

	for plan in plans:
		if plan["valid_upto"] and getdate(plan["valid_upto"]) < on_date:
			continue

		if not _plan_covers_room_type(plan["name"], room_type):
			continue

		applicable.append(plan)

	return applicable


def _plan_covers_room_type(rate_plan: str, room_type: str) -> bool:
	return bool(
		frappe.db.exists(
			"Rate Plan Room Type",
			{"parent": rate_plan, "room_type": room_type, "is_active": 1},
		)
	)


def resolve_rate_plan(property_name: str, room_type: str, arrival, rate_plan: str | None = None) -> str:
	"""Pick the rate plan to price with.

	An explicitly requested plan is validated rather than silently replaced: a
	front desk agent who chose a corporate plan must be told it does not apply,
	not quietly given the rack rate.
	"""
	if rate_plan:
		if not _plan_covers_room_type(rate_plan, room_type):
			throw(
				_("Rate plan {0} does not cover room type {1}.").format(rate_plan, room_type),
				exc=RateError,
			)
		return rate_plan

	plans = get_applicable_rate_plans(property_name, room_type, arrival)

	if not plans:
		throw(
			_("No active rate plan covers room type {0} on {1}.").format(room_type, getdate(arrival)),
			exc=RateError,
		)

	# Standard plans are the default sell; anything else is chosen deliberately.
	standard = [plan for plan in plans if plan["rate_type"] == "Standard"]

	return (standard or plans)[0]["name"]


# ---------------------------------------------------------------------------
# Per-night rate lookup
# ---------------------------------------------------------------------------


def _daily_rate(property_name: str, rate_plan: str, room_type: str, night) -> dict | None:
	return frappe.db.get_value(
		DAILY_RATE_DOCTYPE,
		{
			"property": property_name,
			"rate_plan": rate_plan,
			"room_type": room_type,
			"rate_date": getdate(night),
		},
		[
			"name",
			"rate",
			"extra_adult_charge",
			"extra_child_charge",
			"min_length_of_stay",
			"max_length_of_stay",
			"closed",
			"closed_to_arrival",
			"closed_to_departure",
			"stop_sell",
			"rooms_to_sell",
		],
		as_dict=True,
	)


def _plan_room_type_row(rate_plan: str, room_type: str) -> dict | None:
	return frappe.db.get_value(
		"Rate Plan Room Type",
		{"parent": rate_plan, "room_type": room_type},
		[
			"base_rate",
			"extra_adult_charge",
			"extra_child_charge",
			"extra_bed_charge",
			"single_occupancy_rate",
			"is_active",
		],
		as_dict=True,
	)


def _room_type_fallback(room_type: str) -> dict:
	return (
		frappe.db.get_value(
			ROOM_TYPE_DOCTYPE,
			room_type,
			[
				"base_rate",
				"extra_adult_charge",
				"extra_child_charge",
				"extra_bed_charge",
				"base_occupancy",
				"max_occupancy",
				"max_adults",
				"max_children",
				"max_extra_beds",
			],
			as_dict=True,
		)
		or {}
	)


# ---------------------------------------------------------------------------
# Restrictions
# ---------------------------------------------------------------------------


def _plan_restrictions(rate_plan: str, room_type: str, night) -> list[dict]:
	"""Plan-level restriction rows in force for this room type and night."""
	rows = frappe.get_all(
		"Rate Restriction",
		filters={"parent": rate_plan, "parenttype": RATE_PLAN_DOCTYPE},
		fields=[
			"min_length_of_stay",
			"max_length_of_stay",
			"closed_to_arrival",
			"closed_to_departure",
			"min_advance_days",
			"max_advance_days",
			"applies_from",
			"applies_upto",
			"days_of_week",
			"room_type",
		],
	)

	night = getdate(night)
	weekday = WEEKDAYS[night.weekday()]

	in_force = []

	for row in rows:
		if row["room_type"] and row["room_type"] != room_type:
			continue
		if row["applies_from"] and getdate(row["applies_from"]) > night:
			continue
		if row["applies_upto"] and getdate(row["applies_upto"]) < night:
			continue
		if row["days_of_week"]:
			days = {day.strip() for day in row["days_of_week"].split(",") if day.strip()}
			if days and weekday not in days:
				continue

		in_force.append(row)

	return in_force


def _inventory_restriction(property_name: str, room_type: str, night) -> dict | None:
	"""Property-level restriction for a night, room-type specific or global."""
	for filters in (
		{"property": property_name, "room_type": room_type, "restriction_date": getdate(night)},
		{"property": property_name, "room_type": ("in", ("", None)), "restriction_date": getdate(night)},
	):
		row = frappe.db.get_value(
			INVENTORY_RESTRICTION_DOCTYPE,
			filters,
			[
				"stop_sell",
				"closed_to_arrival",
				"closed_to_departure",
				"min_length_of_stay",
				"rooms_to_sell",
			],
			as_dict=True,
		)

		if row:
			return row

	return None


def validate_restrictions(
	property_name: str,
	room_type: str,
	arrival,
	departure,
	rate_plan: str,
	*,
	booked_on=None,
):
	"""Raise if any restriction forbids this stay.

	Every night is checked, not just arrival: a stay that spans a closed date
	cannot be sold even when its arrival night is open.
	"""
	nights = nights_between(arrival, departure)
	length_of_stay = len(nights)
	arrival_date = getdate(arrival)
	departure_date = getdate(departure)
	booked_on = getdate(booked_on or nowdate())

	advance_days = (arrival_date - booked_on).days

	for night in nights:
		is_arrival_night = night == arrival_date

		daily = _daily_rate(property_name, rate_plan, room_type, night)
		inventory = _inventory_restriction(property_name, room_type, night)

		if daily:
			if daily["closed"] or daily["stop_sell"]:
				throw(
					_("Room type {0} is closed for sale on {1}.").format(room_type, night),
					exc=RateError,
				)
			if is_arrival_night and daily["closed_to_arrival"]:
				throw(_("Arrivals are closed on {0}.").format(night), exc=RateError)
			if daily["min_length_of_stay"] and length_of_stay < daily["min_length_of_stay"]:
				throw(
					_("A minimum stay of {0} night(s) applies on {1}.").format(
						daily["min_length_of_stay"], night
					),
					exc=RateError,
				)
			if daily["max_length_of_stay"] and length_of_stay > daily["max_length_of_stay"]:
				throw(
					_("A maximum stay of {0} night(s) applies on {1}.").format(
						daily["max_length_of_stay"], night
					),
					exc=RateError,
				)

		if inventory:
			if inventory["stop_sell"]:
				throw(_("Sale is stopped for {0} on {1}.").format(room_type, night), exc=RateError)
			if is_arrival_night and inventory["closed_to_arrival"]:
				throw(_("Arrivals are closed on {0}.").format(night), exc=RateError)
			if inventory["min_length_of_stay"] and length_of_stay < inventory["min_length_of_stay"]:
				throw(
					_("A minimum stay of {0} night(s) applies on {1}.").format(
						inventory["min_length_of_stay"], night
					),
					exc=RateError,
				)

		for rule in _plan_restrictions(rate_plan, room_type, night):
			if is_arrival_night and rule["closed_to_arrival"]:
				throw(_("This rate plan is closed to arrival on {0}.").format(night), exc=RateError)
			if rule["min_length_of_stay"] and length_of_stay < rule["min_length_of_stay"]:
				throw(
					_("This rate plan requires a minimum stay of {0} night(s).").format(
						rule["min_length_of_stay"]
					),
					exc=RateError,
				)
			if rule["max_length_of_stay"] and length_of_stay > rule["max_length_of_stay"]:
				throw(
					_("This rate plan allows a maximum stay of {0} night(s).").format(
						rule["max_length_of_stay"]
					),
					exc=RateError,
				)
			if rule["min_advance_days"] and advance_days < rule["min_advance_days"]:
				throw(
					_("This rate plan must be booked at least {0} day(s) in advance.").format(
						rule["min_advance_days"]
					),
					exc=RateError,
				)
			if rule["max_advance_days"] and advance_days > rule["max_advance_days"]:
				throw(
					_("This rate plan cannot be booked more than {0} day(s) in advance.").format(
						rule["max_advance_days"]
					),
					exc=RateError,
				)

	# Departure restrictions look at the departure date itself, which is not a
	# consumed night, so it is checked separately.
	departure_daily = _daily_rate(property_name, rate_plan, room_type, departure_date)
	if departure_daily and departure_daily["closed_to_departure"]:
		throw(_("Departures are closed on {0}.").format(departure_date), exc=RateError)

	departure_inventory = _inventory_restriction(property_name, room_type, departure_date)
	if departure_inventory and departure_inventory["closed_to_departure"]:
		throw(_("Departures are closed on {0}.").format(departure_date), exc=RateError)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def get_rate_breakdown(
	property_name: str,
	room_type: str,
	arrival,
	departure,
	*,
	rate_plan: str | None = None,
	adults: int = 2,
	children: int = 0,
	extra_beds: int = 0,
	rooms: int = 1,
	check_restrictions: bool = True,
	arrival_time=None,
	departure_time=None,
	blocks: int = 1,
) -> dict:
	"""The per-night breakdown for one room of `room_type`.

	Returns the lines a reservation snapshots, plus the totals. Amounts are per
	room; `rooms` only multiplies the grand total, because each room of the
	same type on the same dates prices identically.
	"""
	rate_plan = resolve_rate_plan(property_name, room_type, arrival, rate_plan)
	from hospitality_pms.services.short_stay import SHORT_KINDS, line_kind, short_breakdown

	# The plan's kind and the dates must agree (a day-use plan never prices a night, a
	# night plan never prices a same-day stay); a short stay is then priced by its plan's
	# day or blocks, never by nights.
	if line_kind(rate_plan, arrival, departure) in SHORT_KINDS:
		return short_breakdown(
			property_name,
			room_type,
			arrival,
			rate_plan,
			arrival_time=arrival_time,
			departure_time=departure_time,
			blocks=blocks,
			adults=adults,
			children=children,
			extra_beds=extra_beds,
			rooms=rooms,
		)

	if check_restrictions:
		validate_restrictions(property_name, room_type, arrival, departure, rate_plan)

	plan = frappe.db.get_value(
		RATE_PLAN_DOCTYPE, rate_plan, ["rate_type", "currency", "includes_taxes"], as_dict=True
	)
	plan_row = _plan_room_type_row(rate_plan, room_type) or {}
	fallback = _room_type_fallback(room_type)

	base_occupancy = int(fallback.get("base_occupancy") or 2)
	chargeable_adults = max(int(adults or 0) - base_occupancy, 0)

	currency = plan.get("currency") or get_property(property_name).currency
	is_zero_rated = plan.get("rate_type") in ZERO_RATE_TYPES

	lines = []
	total = 0.0

	for night in nights_between(arrival, departure):
		daily = _daily_rate(property_name, rate_plan, room_type, night)

		if is_zero_rated:
			nightly = 0.0
			extra_adult = extra_child = extra_bed = 0.0
		else:
			nightly = _first_positive(
				daily.get("rate") if daily else None,
				plan_row.get("base_rate"),
				fallback.get("base_rate"),
			)

			if nightly is None:
				throw(
					_("No rate is configured for room type {0} on {1}.").format(room_type, night),
					exc=RateError,
				)

			extra_adult = flt(
				_first_positive(
					daily.get("extra_adult_charge") if daily else None,
					plan_row.get("extra_adult_charge"),
					fallback.get("extra_adult_charge"),
				)
				or 0
			)
			extra_child = flt(
				_first_positive(
					daily.get("extra_child_charge") if daily else None,
					plan_row.get("extra_child_charge"),
					fallback.get("extra_child_charge"),
				)
				or 0
			)
			extra_bed = flt(plan_row.get("extra_bed_charge") or fallback.get("extra_bed_charge") or 0)

		supplements = (
			chargeable_adults * extra_adult
			+ int(children or 0) * extra_child
			+ int(extra_beds or 0) * extra_bed
		)

		net = flt(nightly) + supplements
		total += net

		lines.append(
			{
				"rate_date": str(getdate(night)),
				"room_type": room_type,
				"rate_plan": rate_plan,
				"rate": flt(nightly),
				"extra_adult_charge": chargeable_adults * extra_adult,
				"extra_child_charge": int(children or 0) * extra_child,
				"extra_bed_charge": int(extra_beds or 0) * extra_bed,
				"discount_amount": 0.0,
				"net_rate": net,
				"is_complimentary": 1 if is_zero_rated else 0,
			}
		)

	rooms = max(int(rooms or 1), 1)

	return {
		"rate_plan": rate_plan,
		"rate_type": plan.get("rate_type"),
		"currency": currency,
		"includes_taxes": bool(plan.get("includes_taxes")),
		"room_type": room_type,
		"nights": len(lines),
		"rooms": rooms,
		"lines": lines,
		"total_per_room": flt(total, 2),
		"total_amount": flt(total * rooms, 2),
		"average_nightly_rate": flt(total / len(lines), 2) if lines else 0.0,
	}


def _first_positive(*values):
	"""First value that is set, including a deliberate zero.

	A daily rate of 0 is a real decision (a free night in a package) and must
	not fall through to the plan's base rate, so this tests for None rather
	than truthiness.
	"""
	for value in values:
		if value is not None:
			return flt(value)

	return None


def get_cancellation_charge(
	reservation_total: float,
	policy: str | None,
	arrival,
	at=None,
	first_night_amount: float | None = None,
) -> float:
	"""What a cancellation costs under a policy, at a point in time.

	`first_night_amount` is required by first-night policies. It is passed in
	rather than recomputed because the charge must use the rate the guest was
	sold, which lives on the reservation, not today's rate grid.
	"""
	if not policy:
		return 0.0

	rule = frappe.db.get_value(
		"Rate Policy",
		policy,
		["charge_basis", "charge_value", "free_cancellation_hours"],
		as_dict=True,
	)

	if not rule:
		return 0.0

	at = frappe.utils.get_datetime(at or frappe.utils.now_datetime())
	arrival_dt = frappe.utils.get_datetime(f"{getdate(arrival)} 00:00:00")
	hours_before = (arrival_dt - at).total_seconds() / 3600

	if rule["free_cancellation_hours"] and hours_before >= rule["free_cancellation_hours"]:
		return 0.0

	basis = rule["charge_basis"]
	value = flt(rule["charge_value"])

	if basis == "No Charge":
		return 0.0
	if basis == "Fixed Amount":
		return flt(value, 2)
	if basis == "Percentage of Stay":
		return flt(reservation_total * value / 100, 2)
	if basis == "Full Stay":
		return flt(reservation_total, 2)
	if basis == "First Night":
		if first_night_amount is None:
			throw(
				_("Policy {0} charges the first night, but no first night amount was supplied.").format(
					policy
				),
				exc=RateError,
			)
		return flt(first_night_amount, 2)

	return 0.0


def get_first_night_charge(breakdown: dict) -> float:
	"""The first night's net rate, used by first-night cancellation policies."""
	lines = breakdown.get("lines") or []

	return flt(lines[0]["net_rate"], 2) if lines else 0.0
