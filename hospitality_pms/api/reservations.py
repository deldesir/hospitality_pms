"""Reservation endpoints for the operational frontend.

Every state change is a POST that delegates to ReservationService, which owns
the transition table, the availability lock and the audit log. Nothing here
decides whether a transition is allowed.
"""

import frappe
from frappe import _
from frappe.utils import getdate

from hospitality_pms.services import reservations as service
from hospitality_pms.services.base import authorise_document, require_permission
from hospitality_pms.services.property import resolve_operational_date, resolve_property
from hospitality_pms.services.rates import get_rate_breakdown
from hospitality_pms.utils.params import clean_bool, clean_int, clean_str

RESERVATION_DOCTYPE = "Reservation"

LIST_FIELDS = (
	"name",
	"reservation_status",
	"reservation_type",
	"guest",
	"guest_name",
	"arrival_date",
	"departure_date",
	"nights",
	"total_rooms",
	"total_adults",
	"total_children",
	"total_amount",
	"currency",
	"booking_source",
	"external_reference",
	"deposit_required",
	"deposit_received",
)


@frappe.whitelist(methods=["GET"])
def list_reservations(
	property: str | None = None,
	status: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	search: str | None = None,
	limit: int = 50,
	start: int = 0,
) -> dict:
	"""Filtered reservation list.

	Filtering happens on the server: an operational property can hold six
	figures of reservations and the frontend must never pull them to filter
	locally (SAD section 13).
	"""
	require_permission(RESERVATION_DOCTYPE, "read")
	property_name = resolve_property(property)

	filters = {"property": property_name}

	if status:
		filters["reservation_status"] = ("in", status.split(",")) if "," in status else status

	# Overlap, not containment: a stay spanning the window belongs in it.
	if from_date:
		filters["departure_date"] = (">=", getdate(from_date))
	if to_date:
		filters["arrival_date"] = ("<=", getdate(to_date))

	or_filters = None
	if search:
		pattern = f"%{search.strip()}%"
		or_filters = {"guest_name": ("like", pattern), "name": ("like", pattern)}

	limit = min(int(limit or 50), 200)

	records = frappe.get_list(
		RESERVATION_DOCTYPE,
		filters=filters,
		or_filters=or_filters,
		fields=list(LIST_FIELDS),
		order_by="arrival_date asc, guest_name asc",
		limit_page_length=limit,
		limit_start=int(start or 0),
	)

	return {
		"reservations": records,
		"has_more": len(records) == limit,
		"property": property_name,
	}


@frappe.whitelist(methods=["GET"])
def get_reservation(reservation: str) -> dict:
	"""One reservation with its room lines and rate snapshot."""
	doc = authorise_document(RESERVATION_DOCTYPE, reservation, "read")

	# The snapshot is stored flat on the reservation (Frappe has no grandchild
	# tables), so it is regrouped per room line for the screen.
	rates_by_line: dict[str, list[dict]] = {}
	for rate in doc.rate_lines:
		rates_by_line.setdefault(rate.room_line, []).append(
			{
				"rate_date": rate.rate_date,
				"rate": rate.rate,
				"extra_adult_charge": rate.extra_adult_charge,
				"extra_child_charge": rate.extra_child_charge,
				"extra_bed_charge": rate.extra_bed_charge,
				"net_rate": rate.net_rate,
			}
		)

	return {
		"reservation": {field: doc.get(field) for field in LIST_FIELDS},
		"guarantee_type": doc.guarantee_type,
		"special_requests": doc.special_requests,
		"cancellation_charge": doc.cancellation_charge,
		"rooms": [
			{
				"name": line.name,
				"room_type": line.room_type,
				"rooms": line.rooms,
				"arrival_date": line.arrival_date,
				"departure_date": line.departure_date,
				"nights": line.nights,
				"adults": line.adults,
				"children": line.children,
				"extra_beds": line.extra_beds,
				"rate_plan": line.rate_plan,
				"assigned_room": line.assigned_room,
				"room_rate": line.room_rate,
				"total_amount": line.total_amount,
				"rate_lines": rates_by_line.get(line.name, []),
			}
			for line in doc.rooms
		],
		"allowed_transitions": sorted(service.TRANSITIONS.get(doc.reservation_status, set())),
	}


@frappe.whitelist(methods=["GET"])
def quote(
	room_type: str,
	arrival: str,
	departure: str,
	property: str | None = None,
	rate_plan: str | None = None,
	adults: int = 2,
	children: int = 0,
	extra_beds: int = 0,
	rooms: int = 1,
	arrival_time: str | None = None,
	departure_time: str | None = None,
	blocks: int = 1,
) -> dict:
	"""Price a stay without creating anything.

	The same service the reservation uses, so the quote a guest is given and
	the amount they are charged cannot diverge.
	"""
	require_permission(RESERVATION_DOCTYPE, "read")
	property_name = resolve_property(property)

	return get_rate_breakdown(
		property_name,
		room_type,
		arrival,
		departure,
		rate_plan=rate_plan,
		adults=int(adults or 2),
		children=int(children or 0),
		extra_beds=int(extra_beds or 0),
		rooms=int(rooms or 1),
		arrival_time=arrival_time or None,
		departure_time=departure_time or None,
		blocks=int(blocks or 1),
	)


@frappe.whitelist(methods=["POST"])
def create_reservation(reservation: dict | str) -> dict:
	"""Create a reservation from the frontend payload.

	The document controller prices and validates it; this endpoint only shapes
	the input and enforces the permission.
	"""
	require_permission(RESERVATION_DOCTYPE, "create")

	payload = frappe.parse_json(reservation) if isinstance(reservation, str) else dict(reservation)
	payload["doctype"] = RESERVATION_DOCTYPE
	payload["property"] = resolve_property(payload.get("property"))

	# The status is service-owned; a caller may not post itself into a holding
	# state and skip the availability check.
	payload["reservation_status"] = payload.get("reservation_status") or service.DRAFT
	if payload["reservation_status"] not in (service.DRAFT, service.TENTATIVE):
		frappe.throw(
			_("A new reservation starts as Draft or Tentative. Confirm it to hold inventory."),
			frappe.PermissionError,
		)

	doc = frappe.get_doc(payload).insert()

	return get_reservation(doc.name)


@frappe.whitelist(methods=["POST"])
def confirm(reservation: str, allow_overbooking: int = 0, reason: str | None = None) -> dict:
	"""Confirm a reservation, holding inventory under a lock."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	service.confirm(reservation, allow_overbooking=bool(int(allow_overbooking or 0)), reason=reason)

	return get_reservation(reservation)


@frappe.whitelist(methods=["POST"])
def guarantee(reservation: str, guarantee_type: str, reason: str | None = None) -> dict:
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	service.guarantee(reservation, guarantee_type, reason=reason)

	return get_reservation(reservation)


@frappe.whitelist(methods=["POST"])
def cancel(reservation: str, reason: str, waive_charge: int = 0) -> dict:
	"""Cancel a reservation and return what the policy charges."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.cancel(reservation, reason, waive_charge=bool(int(waive_charge or 0)))

	return {**get_reservation(reservation), "cancellation": result}


@frappe.whitelist(methods=["POST"])
def mark_no_show(reservation: str, reason: str | None = None) -> dict:
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.mark_no_show(reservation, reason=reason)

	return {**get_reservation(reservation), "no_show": result}


@frappe.whitelist(methods=["POST"])
def assign_room(reservation: str, room_line: str, room: str, allow_unready: int = 0) -> dict:
	"""Assign a specific room to a reservation line."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	service.assign_room(reservation, room_line, room, allow_unready=bool(int(allow_unready or 0)))

	return get_reservation(reservation)


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------
#
# Every one of these is a POST, so CSRF applies, and every one calls
# `authorise_document` on the reservation the client named rather than
# `require_permission` on the DocType: the second answers "may this user ever
# edit a booking", which every reservation agent may, for every property in the
# estate (P1-15, N2).
#
# `authorise_document` is called here and not inside the services on purpose -
# see its docstring. The same service functions are called by the channel
# importer and the night audit, which act for no session user; the services
# assert `check_permission("write")` on the locked document instead.
#
# Nothing here decides anything. The services own the state guards, the lock
# chain, the availability re-check, the allow list and the audit record.


@frappe.whitelist(methods=["POST"])
def update_reservation_details(reservation: str, changes: dict | str) -> dict:
	"""Correct the non-inventory details of a booking.

	`changes` is a field map, not a document: the service refuses any key outside
	`DETAIL_WRITABLE_FIELDS`, so this is not a generic saver with a reservation
	name attached to it.
	"""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.update_reservation_details(
		reservation, frappe.parse_json(changes) if isinstance(changes, str) else changes
	)

	return {**get_reservation(reservation), "update": result}


@frappe.whitelist(methods=["POST"])
def change_line_interval(
	reservation: str,
	room_line: str,
	arrival: str | None = None,
	departure: str | None = None,
	allow_overbooking: int = 0,
	reason: str | None = None,
) -> dict:
	"""Move one room line's dates, re-checking inventory under lock."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.change_line_interval(
		reservation,
		room_line,
		arrival=clean_str(arrival),
		departure=clean_str(departure),
		allow_overbooking=clean_bool(allow_overbooking),
		reason=clean_str(reason),
	)

	return {**get_reservation(reservation), "interval": result}


@frappe.whitelist(methods=["POST"])
def add_room_line(
	reservation: str,
	room_type: str,
	arrival: str,
	departure: str,
	adults: int = 1,
	children: int = 0,
	rate_plan: str | None = None,
) -> dict:
	"""Add one more room to a booking that is still editable."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.add_room_line(
		reservation,
		room_type,
		arrival,
		departure,
		adults=clean_int(adults, 1) or 1,
		children=clean_int(children, 0) or 0,
		rate_plan=clean_str(rate_plan),
	)

	return {**get_reservation(reservation), "room_line": result}


@frappe.whitelist(methods=["POST"])
def remove_room_line(reservation: str, room_line: str) -> dict:
	"""Drop one room from a booking that is still editable."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.remove_room_line(reservation, room_line)

	return {**get_reservation(reservation), "room_line": result}


@frappe.whitelist(methods=["POST"])
def change_line_room_type(reservation: str, room_line: str, room_type: str) -> dict:
	"""Move one room line onto a different room type."""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.change_line_room_type(reservation, room_line, room_type)

	return {**get_reservation(reservation), "room_line": result}


@frappe.whitelist(methods=["POST"])
def set_line_rate_plan(reservation: str, room_line: str, rate_plan: str) -> dict:
	"""Put one room line on a different rate plan.

	There is no rate parameter, here or in the service. The plan is the choice;
	the rate is the rate service's answer to it.
	"""
	authorise_document(RESERVATION_DOCTYPE, reservation, "write")

	result = service.set_line_rate_plan(reservation, room_line, rate_plan)

	return {**get_reservation(reservation), "room_line": result}


@frappe.whitelist(methods=["GET"])
def arrivals(property: str | None = None, on_date: str | None = None) -> list[dict]:
	require_permission(RESERVATION_DOCTYPE, "read")

	return service.get_arrivals(resolve_property(property), on_date)


@frappe.whitelist(methods=["GET"])
def departures(property: str | None = None, on_date: str | None = None) -> list[dict]:
	require_permission(RESERVATION_DOCTYPE, "read")

	return service.get_departures(resolve_property(property), on_date)


@frappe.whitelist(methods=["GET"])
def calendar(property: str | None = None, from_date: str | None = None, to_date: str | None = None) -> dict:
	"""Reservation counts per day, for the calendar view.

	Aggregated on the server so the calendar is one request rather than one per
	day (Frontend Standards section 6).
	"""
	require_permission(RESERVATION_DOCTYPE, "read")
	property_name = resolve_property(property)

	# The operating day, not the calendar's - the same rule the front office
	# boards use, so the two public endpoint families cannot drift apart again.
	from_date = resolve_operational_date(property_name, from_date)
	to_date = getdate(to_date or frappe.utils.add_days(from_date, 30))

	records = frappe.get_list(
		RESERVATION_DOCTYPE,
		filters={
			"property": property_name,
			"arrival_date": ("<=", to_date),
			"departure_date": (">=", from_date),
			"reservation_status": ("in", (*service.HOLDING_STATES, service.TENTATIVE)),
		},
		fields=[
			"name",
			"guest_name",
			"arrival_date",
			"departure_date",
			"reservation_status",
			"total_rooms",
		],
		order_by="arrival_date asc",
		limit_page_length=0,
	)

	arrivals_by_day: dict[str, int] = {}
	departures_by_day: dict[str, int] = {}

	for record in records:
		arrival = str(getdate(record["arrival_date"]))
		departure = str(getdate(record["departure_date"]))
		rooms = int(record["total_rooms"] or 1)

		arrivals_by_day[arrival] = arrivals_by_day.get(arrival, 0) + rooms
		departures_by_day[departure] = departures_by_day.get(departure, 0) + rooms

	return {
		"property": property_name,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"reservations": records,
		"arrivals_by_day": arrivals_by_day,
		"departures_by_day": departures_by_day,
	}
