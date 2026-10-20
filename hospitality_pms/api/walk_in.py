"""Walk-in endpoints for the front desk.

Two endpoints only. The wizard's room and rate lookups reuse the endpoints that
already own them - `availability.search`, `availability.assignable_rooms` and
`reservations.quote` - because a second search that drifts from the first is how
a frontend ends up offering a room the booking path will refuse.

Nothing here decides anything: WalkInService coordinates the reservation and
stay services, and those own the locks, the availability re-check, the pricing
and every arrival guard. Domain errors are deliberately not caught or reshaped -
"a deposit of 500 is required" is the message the agent has to act on, and
flattening it into "walk-in failed" would send them to the wrong screen.
"""

import frappe

from hospitality_pms.services import walk_in as service
from hospitality_pms.services.base import require_permission
from hospitality_pms.services.property import resolve_property
from hospitality_pms.utils.params import clean_bool, clean_int, clean_str

RESERVATION_DOCTYPE = "Reservation"
STAY_DOCTYPE = "Stay"


@frappe.whitelist(methods=["GET"])
def get_walk_in_context(property: str | None = None) -> dict:
	"""The property context a walk-in starts from.

	Exists so the frontend never derives the business date itself. A walk-in
	arrives on the property's operating day, which is not necessarily today: a
	property that has not run Night Audit is still working yesterday, and a
	browser clock has no way to know that.
	"""
	require_permission(RESERVATION_DOCTYPE, "read")

	return service.get_context(resolve_property(clean_str(property)))


@frappe.whitelist(methods=["POST"])
def create_walk_in(
	guest: str,
	departure_date: str,
	room_type: str,
	room: str,
	property: str | None = None,
	adults: int = 1,
	children: int = 0,
	rate_plan: str | None = None,
	billing_instructions: str | None = None,
	allow_unready_room: int = 0,
	readiness_reason: str | None = None,
	special_requests: str | None = None,
	arrival_time: str | None = None,
	departure_time: str | None = None,
	blocks: int = 1,
) -> dict:
	"""Book and check in a guest standing at the desk, in one operation.

	Both permissions are asserted up front: a walk-in creates a Reservation and
	a Stay, and an agent who may do only one of those must be told so before any
	inventory is touched.
	"""
	require_permission(RESERVATION_DOCTYPE, "create")
	require_permission(STAY_DOCTYPE, "create")

	return service.create_walk_in(
		property_name=resolve_property(clean_str(property)),
		guest=clean_str(guest),
		departure_date=clean_str(departure_date),
		room_type=clean_str(room_type),
		room=clean_str(room),
		adults=clean_int(adults, 1),
		children=clean_int(children, 0),
		rate_plan=clean_str(rate_plan),
		billing_instructions=clean_str(billing_instructions),
		allow_unready_room=clean_bool(allow_unready_room),
		readiness_reason=clean_str(readiness_reason),
		special_requests=clean_str(special_requests),
		arrival_time=clean_str(arrival_time),
		departure_time=clean_str(departure_time),
		blocks=clean_int(blocks, 1),
	)
