# Copyright (c) 2026, Globcom Qatar and Contributors
# See license.txt

"""16.7.2 — changing a booking after it has been made.

`Reservation._guard_holding_immutability` closed the easy way round every
inventory check: a `frappe.get_doc(...).save()` that moved a departure date out
by three nights, added a room line, or re-typed one, and oversold the house
without a single availability check having run. Its docstring named what was
missing rather than pretending the guard was the answer — "a service operation
('move/rebook') that does not exist yet. Until it does, the only correct path is
cancel and rebook."

This suite is that operation's regression cover, plus the four editable-state
line operations beside it. Four properties are asserted throughout, because each
one is a defect the guard alone does not prevent:

* **Only the added nights are re-checked.** The nights a line already holds are
  the booking's by right, and it is sitting in the very figures a whole-interval
  re-check would read — so extending the last room in the house by one night
  would refuse itself.
* **A confirmed booking is never re-priced.** The guest has been quoted these
  amounts. A date change is not a re-quote, and the deposit split that hangs off
  those line values must come through unmoved.
* **One line's edit touches no other line.** A three-room booking is three rows
  now (P1-6); an edit that reached sideways would silently move a room somebody
  else's guest is sleeping in.
* **Room-type capacity and one specific room are different questions,** and an
  overbooking override answers only the first.

Real data throughout: real properties, real rooms, real rate plans, real roles
expressed as Frappe User Permissions, real check-ins.
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate

from hospitality_pms.api import reservations as reservations_api
from hospitality_pms.hospitality_reservations.doctype.reservation.reservation import TERMINAL_STATES
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services.exceptions import (
	AvailabilityError,
	HospitalityPMSError,
	InvalidStateTransitionError,
	PermissionDeniedError,
)
from hospitality_pms.tests.inventory_world import InventoryWorld

#: The world's own rate, and two more so a plan or type change is visible in the
#: numbers rather than merely asserted to have happened.
ROOM_RATE = 100.0
DELUXE_RATE = 150.0
PREMIUM_RATE = 250.0

PRECISION = 2

#: Operational residue to clear between tests. Confirmations and check-ins
#: commit, so a booking left behind by the previous test would satisfy — or
#: destroy — the next test's premise before it started.
RESET = (
	"Folio Log",
	"Guest Folio",
	"Room Status Log",
	"Stay",
	"Reservation Log",
	"Reservation",
)

#: Both shapes a cross-property refusal legitimately takes: Frappe's own
#: document permission error, and the app's property-access error. Which fires
#: depends on whether the User Permission or the property resolution catches it
#: first, and the caller is refused either way.
REFUSALS = (frappe.PermissionError, PermissionDeniedError)


def _make_rate_plan(fixtures, property_name: str, room_type: str, code: str, base_rate: float) -> str:
	"""A second rate plan for the same room type, at a different price."""
	name = f"{property_name}-{code}"

	if not frappe.db.exists("Rate Plan", name):
		business_date = getdate(frappe.db.get_value("Property", property_name, "business_date"))

		frappe.get_doc(
			{
				"doctype": "Rate Plan",
				"rate_plan_code": name,
				"rate_plan_name": f"Modification {code}",
				"property": property_name,
				"rate_type": "Standard",
				"valid_from": add_days(business_date, -365),
				"is_active": 1,
				"room_types": [{"room_type": room_type, "base_rate": base_rate, "is_active": 1}],
			}
		).insert(ignore_permissions=True)

	return fixtures.track("Rate Plan", name)


class ModificationTestCase(IntegrationTestCase):
	"""A property with rooms to sell and a booking to change."""

	ROOMS = 4

	#: Doctypes a particular suite leaves behind that the shared list does not
	#: cover. A rate restriction is committed and is *not* an operational record,
	#: so without this one test's stop-sell night silently breaks the next test's
	#: fixture - a draft reservation is priced with restriction checks on.
	EXTRA_RESET: tuple[str, ...] = ()

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = InventoryWorld(cls.WORLD_TAG, cls.WORLD_CODE, rooms=cls.ROOMS)

	@classmethod
	def tearDownClass(cls):
		cls.world.teardown()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self.world.fixtures.reset_property_records(self.world.property, RESET + self.EXTRA_RESET)

		for room in self.world.rooms:
			frappe.db.set_value(
				"Hotel Room",
				room,
				{
					"occupancy_status": "Vacant",
					"housekeeping_status": "Clean",
					"maintenance_status": "Operational",
					"inventory_status": "Available",
				},
				update_modified=False,
			)

		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# -- reading the world back ------------------------------------------

	def line(self, reservation: str, index: int = 0) -> dict:
		return self.world.lines(reservation)[index]

	def header(self, reservation: str) -> dict:
		return frappe.db.get_value(
			"Reservation",
			reservation,
			[
				"reservation_status",
				"arrival_date",
				"departure_date",
				"nights",
				"total_amount",
				"room_charges_total",
				"total_rooms",
				"total_adults",
			],
			as_dict=True,
		)

	def rate_snapshot(self, reservation: str) -> list[dict]:
		return frappe.get_all(
			"Reservation Rate Line",
			filters={"parent": reservation},
			fields=["rate_date", "rate", "net_rate", "room_line"],
			order_by="rate_date asc, room_line asc",
		)

	def logs(self, reservation: str) -> list[dict]:
		return frappe.get_all(
			"Reservation Log",
			filters={"reservation": reservation},
			fields=["from_status", "to_status", "reason", "details", "changed_by"],
			order_by="creation asc",
		)

	def modifications(self, reservation: str) -> list[dict]:
		"""Log entries that recorded an edit rather than a transition."""
		return [row for row in self.logs(reservation) if row["from_status"] == row["to_status"]]

	def assertMoney(self, actual, expected, msg=None):
		self.assertAlmostEqual(flt(actual), flt(expected), places=PRECISION, msg=msg)


# ---------------------------------------------------------------------------
# 1. The details
# ---------------------------------------------------------------------------


class TestReservationDetails(ModificationTestCase):
	"""An allow list, not a generic saver with a reservation name attached."""

	ROOMS = 2
	WORLD_TAG = "RMDET"
	WORLD_CODE = "MD"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures = cls.world.fixtures

		# A second property with a booking worth reaching for, so a refusal can
		# be told from an outage.
		cls.other_property = fixtures.property("MZ", require_id_at_check_in=0)
		other_type = fixtures.room_type(cls.other_property, base_rate=ROOM_RATE)
		fixtures.rooms(cls.other_property, other_type, count=1)
		other_plan = fixtures.rate_plan(cls.other_property, other_type, base_rate=ROOM_RATE)
		cls.other_reservation = fixtures.reservation(
			cls.other_property,
			other_type,
			fixtures.guest("Bravo"),
			rate_plan=other_plan,
			nights=2,
		)

		# A real Frappe User Permission on Property, which is the mechanism the
		# product relies on (HPMS-DEC-052) — not a test-only convention.
		cls.agent = fixtures.user("mdagent", ["Reservation Agent"], properties=[cls.world.property])

		frappe.db.commit()

	def _reservation(self) -> str:
		name = self.world.reservation(nights=2)
		frappe.db.commit()

		return name

	def test_update_details_authorized(self):
		"""Everything on the allow list, through the endpoint, as a real agent."""
		reservation = self._reservation()
		frappe.set_user(self.agent)

		reservations_api.update_reservation_details(
			reservation,
			{
				"booking_source": "Direct",
				"market_segment": "Corporate",
				"arrival_time": "15:30:00",
				"special_requests": "High floor, away from the lift",
				"internal_notes": "Repeat guest; waive the early check-in fee",
			},
		)

		frappe.set_user("Administrator")
		stored = frappe.db.get_value(
			"Reservation",
			reservation,
			["booking_source", "market_segment", "arrival_time", "special_requests", "internal_notes"],
			as_dict=True,
		)

		self.assertEqual(stored["booking_source"], "Direct")
		self.assertEqual(stored["market_segment"], "Corporate")
		self.assertEqual(str(stored["arrival_time"]), "15:30:00")
		self.assertEqual(stored["special_requests"], "High floor, away from the lift")
		self.assertEqual(stored["internal_notes"], "Repeat guest; waive the early check-in fee")

	def test_update_details_cross_property_refused(self):
		"""P1-15: the endpoint must ask about the document it was handed."""
		frappe.set_user(self.agent)

		with self.assertRaises(REFUSALS):
			reservations_api.update_reservation_details(
				self.other_reservation, {"special_requests": "leaked"}
			)

		frappe.set_user("Administrator")
		self.assertFalse(
			frappe.db.get_value("Reservation", self.other_reservation, "special_requests"),
			msg="a cross-property details update was written",
		)

	def test_update_details_rejects_a_field_outside_the_allow_list(self):
		"""A field nobody remembered to exclude is still not writable."""
		reservation = self._reservation()
		before = self.header(reservation)

		for changes in (
			{"total_amount": 1.0},
			{"room_charges_total": 1.0},
			{"reservation_status": reservation_service.CONFIRMED},
			{"arrival_date": str(self.world.day(9))},
			{"departure_date": str(self.world.day(9))},
			{"property": self.other_property},
			{"deposit_received": 500.0},
			{"confirmed_by": "Administrator"},
			# A permitted field travelling with a refused one must not land either.
			{"special_requests": "should not survive", "total_amount": 1.0},
		):
			with self.assertRaises(PermissionDeniedError, msg=f"{changes} was accepted"):
				reservation_service.update_reservation_details(reservation, changes)

		self.assertEqual(
			self.header(reservation), before, msg="a refused update still wrote something"
		)
		self.assertFalse(frappe.db.get_value("Reservation", reservation, "special_requests"))

	def test_update_details_refused_in_a_terminal_state(self):
		reservation = self._reservation()
		reservation_service.confirm(reservation)
		reservation_service.cancel(reservation, "guest cancelled the whole booking")
		frappe.db.commit()

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.update_reservation_details(
				reservation, {"special_requests": "too late"}
			)

		self.assertFalse(frappe.db.get_value("Reservation", reservation, "special_requests"))

	def test_details_may_still_be_corrected_while_a_guest_is_in_house(self):
		"""The paired positive for the state guard: live is not the same as editable."""
		reservation = self._reservation()
		reservation_service.confirm(reservation)
		self.world.check_in(reservation)

		self.assertEqual(
			self.header(reservation)["reservation_status"], reservation_service.CHECKED_IN
		)

		reservation_service.update_reservation_details(
			reservation, {"internal_notes": "Asked for a late checkout"}
		)

		self.assertEqual(
			frappe.db.get_value("Reservation", reservation, "internal_notes"),
			"Asked for a late checkout",
		)

	def test_a_guarantee_change_is_delegated_to_its_own_service(self):
		"""`guarantee_type` is a state change wearing a form field's clothes."""
		reservation = self._reservation()
		reservation_service.confirm(reservation)
		frappe.db.commit()

		result = reservation_service.update_reservation_details(
			reservation, {"guarantee_type": "Credit Card", "special_requests": "Late arrival"}
		)

		stored = frappe.db.get_value(
			"Reservation",
			reservation,
			["reservation_status", "guarantee_type", "guaranteed_on", "special_requests"],
			as_dict=True,
		)

		self.assertEqual(result["status"], reservation_service.GUARANTEED)
		self.assertEqual(stored["reservation_status"], reservation_service.GUARANTEED)
		self.assertEqual(stored["guarantee_type"], "Credit Card")
		self.assertTrue(
			stored["guaranteed_on"],
			msg="the guarantee was set without the transition that makes it one",
		)
		self.assertEqual(stored["special_requests"], "Late arrival")

		self.assertIn(
			(reservation_service.CONFIRMED, reservation_service.GUARANTEED),
			[(row["from_status"], row["to_status"]) for row in self.logs(reservation)],
			msg="the guarantee was recorded without a logged transition",
		)

	def test_a_guarantee_is_refused_where_that_transition_is_not_available(self):
		"""A Draft cannot reach Guaranteed, so it cannot record a guarantee."""
		reservation = self._reservation()
		before = frappe.db.get_value("Reservation", reservation, "guarantee_type")

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.update_reservation_details(
				reservation, {"guarantee_type": "Credit Card"}
			)

		self.assertEqual(
			frappe.db.get_value("Reservation", reservation, "guarantee_type"),
			before,
			msg="a guarantee was recorded without the transition that makes it one",
		)

	def test_an_unknown_select_value_is_refused_rather_than_stored(self):
		"""`guarantee()` writes with `db.set_value`, which validates nothing."""
		reservation = self._reservation()
		reservation_service.confirm(reservation)
		frappe.db.commit()

		before = frappe.db.get_value("Reservation", reservation, "guarantee_type")

		with self.assertRaises(HospitalityPMSError):
			reservation_service.update_reservation_details(
				reservation, {"guarantee_type": "Cash Under The Mat"}
			)

		self.assertEqual(
			frappe.db.get_value("Reservation", reservation, "guarantee_type"),
			before,
			msg="an unknown Select value was written straight past field validation",
		)
		self.assertEqual(
			frappe.db.get_value("Reservation", reservation, "reservation_status"),
			reservation_service.CONFIRMED,
		)

	def test_a_details_update_is_audited_with_before_and_after(self):
		reservation = self._reservation()
		frappe.set_user(self.agent)

		reservation_service.update_reservation_details(reservation, {"booking_source": "Walk In"})

		frappe.set_user("Administrator")
		entries = self.modifications(reservation)

		self.assertEqual(len(entries), 1, msg=f"expected one audit row, got {entries}")
		self.assertEqual(entries[0]["changed_by"], self.agent)

		changed = frappe.parse_json(entries[0]["details"])["changed"]
		self.assertEqual(changed["booking_source"]["to"], "Walk In")
		self.assertFalse(changed["booking_source"]["from"])

	def test_an_empty_change_set_is_refused(self):
		with self.assertRaises(HospitalityPMSError):
			reservation_service.update_reservation_details(self._reservation(), {})


# ---------------------------------------------------------------------------
# 2. The move/rebook operation — which nights get re-checked
# ---------------------------------------------------------------------------


class TestAddedNights(ModificationTestCase):
	"""One room in the house, so every availability assertion is unambiguous."""

	ROOMS = 1
	WORLD_TAG = "RMADD"
	WORLD_CODE = "MA"
	EXTRA_RESET = ("Room Inventory Restriction",)

	def _confirmed(self, *, arrival_offset: int = 0, nights: int = 2) -> str:
		name = self.world.reservation(nights=nights, arrival=self.world.day(arrival_offset))
		reservation_service.confirm(name)
		frappe.db.commit()

		return name

	def _draft(self, *, arrival_offset: int = 0, nights: int = 2) -> str:
		name = self.world.reservation(nights=nights, arrival=self.world.day(arrival_offset))
		frappe.db.commit()

		return name

	def _restrict(self, on_date, **restrictions) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Room Inventory Restriction",
				"property": self.world.property,
				"room_type": self.world.room_type,
				"restriction_date": getdate(on_date),
				**restrictions,
			}
		).insert(ignore_permissions=True)

		frappe.db.commit()

		return self.world.fixtures.track("Room Inventory Restriction", doc.name)

	def test_date_change_checks_inventory_for_only_the_added_nights(self):
		"""The house is full on the nights this booking is already occupying.

		A whole-interval re-check reads this very reservation in the sold figures
		and refuses a shift that keeps one of its own nights. Only the night
		actually being acquired is a real question.
		"""
		reservation = self._confirmed(nights=2)

		self.assertEqual(
			self.world.available(0, 2), 0, msg="the premise is a house with nothing left"
		)

		result = reservation_service.change_line_interval(
			reservation,
			self.line(reservation)["name"],
			arrival=self.world.day(1),
			departure=self.world.day(3),
		)

		# Night 1 is kept and never re-asked; only night 2 is new.
		self.assertEqual(
			result["added_nights"], [[str(self.world.day(2)), str(self.world.day(3))]]
		)

		line = self.line(reservation)
		self.assertEqual(getdate(line["arrival_date"]), getdate(self.world.day(1)))
		self.assertEqual(getdate(line["departure_date"]), getdate(self.world.day(3)))

	def test_date_change_refused_when_the_added_nights_are_sold_out(self):
		reservation = self._confirmed(nights=2)
		self._confirmed(arrival_offset=2, nights=2)

		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				reservation,
				self.line(reservation)["name"],
				arrival=self.world.day(1),
				departure=self.world.day(3),
			)

	def test_date_change_is_atomic_on_failure(self):
		reservation = self._confirmed(nights=2)
		self._confirmed(arrival_offset=2, nights=2)

		line_before = self.line(reservation)
		header_before = self.header(reservation)
		snapshot_before = self.rate_snapshot(reservation)

		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				reservation,
				line_before["name"],
				arrival=self.world.day(1),
				departure=self.world.day(3),
			)

		self.assertEqual(
			self.line(reservation),
			line_before,
			msg="the room line was written before availability refused",
		)
		self.assertEqual(
			self.header(reservation),
			header_before,
			msg="the reservation header was written before availability refused",
		)
		self.assertEqual(
			self.rate_snapshot(reservation),
			snapshot_before,
			msg="the rate snapshot was re-dated before availability refused",
		)
		self.assertEqual(
			self.modifications(reservation), [], msg="a refused move left an audit record"
		)

	def test_a_move_that_adds_nights_at_both_ends_checks_both(self):
		"""The difference of two intervals is two intervals, and both are real.

		Reachable only while the booking is still editable: growing at both ends
		makes the stay longer, and a held booking may be shifted but not
		re-priced. A draft holds no inventory of its own, so what refuses it here
		is other people's.
		"""
		reservation = self._draft(arrival_offset=2, nights=2)
		behind = self._confirmed(arrival_offset=5, nights=1)

		line = self.line(reservation)["name"]

		# The right-hand segment runs into the night `behind` holds.
		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(1), departure=self.world.day(6)
			)

		reservation_service.cancel(behind, "clear the right-hand end")
		ahead = self._confirmed(arrival_offset=1, nights=1)

		# And now the left-hand segment runs into the night `ahead` holds.
		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(1), departure=self.world.day(6)
			)

		reservation_service.cancel(ahead, "clear the left-hand end")

		result = reservation_service.change_line_interval(
			reservation, line, arrival=self.world.day(1), departure=self.world.day(6)
		)

		self.assertEqual(
			result["added_nights"],
			[
				[str(self.world.day(1)), str(self.world.day(2))],
				[str(self.world.day(4)), str(self.world.day(6))],
			],
		)

	def test_an_entirely_shifted_interval_checks_every_new_night(self):
		"""No overlap means the whole new interval is added, not nothing."""
		reservation = self._confirmed(nights=2)
		self._confirmed(arrival_offset=5, nights=2)

		line = self.line(reservation)["name"]

		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(5), departure=self.world.day(7)
			)

		result = reservation_service.change_line_interval(
			reservation, line, arrival=self.world.day(8), departure=self.world.day(10)
		)

		self.assertEqual(
			result["added_nights"], [[str(self.world.day(8)), str(self.world.day(10))]]
		)
		self.assertEqual(
			self.world.sold(0, 2), 0, msg="the nights the booking left behind are still sold"
		)
		self.assertEqual(self.world.sold(8, 10), 1)

	def test_a_shift_releases_the_nights_it_leaves_behind(self):
		reservation = self._confirmed(nights=2)

		self.assertEqual(self.world.sold(0, 2), 1)

		reservation_service.change_line_interval(
			reservation,
			self.line(reservation)["name"],
			arrival=self.world.day(2),
			departure=self.world.day(4),
		)

		self.assertEqual(self.world.sold(0, 2), 0, msg="the released nights are still unsellable")
		self.assertEqual(self.world.available(0, 2), 1)
		self.assertEqual(self.world.sold(2, 4), 1)

	def test_a_length_change_is_refused_once_holding(self):
		"""Preserving a two-night price across three nights is not bookkeeping.

		Nothing re-prices a held booking, so a changed night count would leave the
		line total, the booking's totals and the rate snapshot describing an
		interval the guest does not have.
		"""
		reservation = self._confirmed(nights=2)
		line = self.line(reservation)
		header_before = self.header(reservation)
		snapshot_before = self.rate_snapshot(reservation)

		for kwargs in (
			{"departure": self.world.day(5)},
			{"departure": self.world.day(1)},
			{"arrival": self.world.day(-1)},
			{"arrival": self.world.day(1)},
			{"arrival": self.world.day(1), "departure": self.world.day(5)},
		):
			with self.assertRaises(InvalidStateTransitionError, msg=f"{kwargs} was accepted"):
				reservation_service.change_line_interval(reservation, line["name"], **kwargs)

		self.assertEqual(self.line(reservation), line)
		self.assertEqual(self.header(reservation), header_before)
		self.assertEqual(self.rate_snapshot(reservation), snapshot_before)

	def test_a_draft_may_still_be_lengthened_and_shortened(self):
		"""The paired positive: an editable booking keeps full freedom.

		It is safe precisely because the save that follows re-prices it, which is
		asserted here rather than assumed.
		"""
		draft = self._draft(nights=2)
		line = self.line(draft)["name"]

		reservation_service.change_line_interval(draft, line, departure=self.world.day(5))
		self.assertEqual(self.header(draft)["nights"], 5)
		self.assertMoney(self.header(draft)["total_amount"], ROOM_RATE * 5)

		reservation_service.change_line_interval(draft, line, departure=self.world.day(1))
		self.assertEqual(self.header(draft)["nights"], 1)
		self.assertMoney(self.header(draft)["total_amount"], ROOM_RATE)
		self.assertEqual(len(self.rate_snapshot(draft)), 1)

	def test_added_nights_before_the_business_date_are_refused(self):
		"""A night whose audit has run can never be charged for."""
		reservation = self._confirmed(arrival_offset=1, nights=2)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				reservation,
				self.line(reservation)["name"],
				arrival=self.world.day(-1),
				departure=self.world.day(1),
			)

		self.assertEqual(
			getdate(self.line(reservation)["arrival_date"]), getdate(self.world.day(1))
		)

	def test_a_move_into_a_stopped_night_is_refused(self):
		"""`validate_restrictions` was reachable only through pricing before this.

		Availability is untouched by a Room Inventory Restriction, so nothing but
		the restriction check can refuse these moves.
		"""
		reservation = self._confirmed(nights=2)
		line = self.line(reservation)["name"]

		self._restrict(self.world.day(3), stop_sell=1)
		self._restrict(self.world.day(5), closed_to_arrival=1)

		with self.assertRaises(HospitalityPMSError, msg="a move into a stop-sell night was written"):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(2), departure=self.world.day(4)
			)

		with self.assertRaises(HospitalityPMSError, msg="a move onto a closed arrival was written"):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(5), departure=self.world.day(7)
			)

		self.assertEqual(
			getdate(self.line(reservation)["arrival_date"]), getdate(self.world.day(0))
		)

		# A night with no restriction on it still moves.
		reservation_service.change_line_interval(
			reservation, line, arrival=self.world.day(6), departure=self.world.day(8)
		)
		self.assertEqual(
			getdate(self.line(reservation)["arrival_date"]), getdate(self.world.day(6))
		)

	def test_a_move_into_a_minimum_stay_night_is_refused(self):
		reservation = self._confirmed(nights=2)

		self._restrict(self.world.day(4), min_length_of_stay=5)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				reservation,
				self.line(reservation)["name"],
				arrival=self.world.day(4),
				departure=self.world.day(6),
			)

	def test_an_interval_that_does_not_move_is_refused(self):
		reservation = self._confirmed(nights=2)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				reservation, self.line(reservation)["name"], departure=self.world.day(2)
			)

	def test_a_departure_on_or_before_the_arrival_is_refused(self):
		reservation = self._confirmed(nights=2)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				reservation, self.line(reservation)["name"], departure=self.world.day(0)
			)


# ---------------------------------------------------------------------------
# 3. Multi-room integrity under a date change
# ---------------------------------------------------------------------------


class TestMultiRoomDateChange(ModificationTestCase):
	"""A booking is rows now, and an edit must reach exactly one of them."""

	ROOMS = 4
	WORLD_TAG = "RMMLT"
	WORLD_CODE = "MM"

	def _confirmed_pair(self) -> str:
		"""Two rooms of different lengths, so the header cannot copy either one."""
		reservation = self.world.reservation_with_lines([{"nights": 2}, {"nights": 3}])
		reservation_service.confirm(reservation)
		frappe.db.commit()

		return reservation

	def test_date_change_updates_the_header_from_the_room_lines(self):
		reservation = self._confirmed_pair()
		short, long_stay = self.world.lines(reservation)

		self.assertEqual(
			getdate(self.header(reservation)["departure_date"]), getdate(self.world.day(3))
		)

		# The shorter room shifts out past the longer one; the header follows it.
		reservation_service.change_line_interval(
			reservation, short["name"], arrival=self.world.day(3), departure=self.world.day(5)
		)
		header = self.header(reservation)
		self.assertEqual(getdate(header["departure_date"]), getdate(self.world.day(5)))
		self.assertEqual(getdate(header["arrival_date"]), getdate(self.world.day(0)))
		self.assertEqual(header["nights"], 5)

		# And back again. The header is the *span* of its rooms, never a copy of
		# whichever one just changed: the booking does not shrink to day 2 because
		# the room that moved happens to end there.
		reservation_service.change_line_interval(
			reservation, short["name"], arrival=self.world.day(0), departure=self.world.day(2)
		)
		header = self.header(reservation)
		self.assertEqual(
			getdate(header["departure_date"]),
			getdate(self.world.day(3)),
			msg="the booking shrank to the room that changed",
		)
		self.assertEqual(getdate(header["arrival_date"]), getdate(self.world.day(0)))

		# The arrival end behaves the same way round.
		reservation_service.change_line_interval(
			reservation, long_stay["name"], arrival=self.world.day(2), departure=self.world.day(5)
		)
		self.assertEqual(
			getdate(self.header(reservation)["arrival_date"]),
			getdate(self.world.day(0)),
			msg="the booking's arrival followed one room instead of summarising both",
		)

	def test_edit_one_room_line_in_multi_room_booking(self):
		reservation = self._confirmed_pair()
		target, untouched = self.world.lines(reservation)

		reservation_service.change_line_interval(
			reservation, target["name"], arrival=self.world.day(4), departure=self.world.day(6)
		)

		after = {row["name"]: row for row in self.world.lines(reservation)}

		self.assertEqual(
			after[untouched["name"]],
			untouched,
			msg="editing one room changed another room's dates, type, rate or assignment",
		)
		self.assertEqual(
			getdate(after[target["name"]]["departure_date"]), getdate(self.world.day(6))
		)

	def test_confirmed_reservation_is_not_repriced_by_an_edit(self):
		"""The financial guard. A date change is not a re-quote."""
		reservation = self.world.reservation(nights=2)
		reservation_service.confirm(reservation)
		frappe.db.commit()

		before = self.header(reservation)
		line_before = self.line(reservation)
		snapshot = self.rate_snapshot(reservation)

		self.assertMoney(
			before["total_amount"],
			ROOM_RATE * 2,
			msg="the fixture priced differently than this test expects",
		)

		reservation_service.change_line_interval(
			reservation, line_before["name"], arrival=self.world.day(3), departure=self.world.day(5)
		)

		after = self.header(reservation)
		line_after = self.line(reservation)

		self.assertMoney(
			after["total_amount"],
			before["total_amount"],
			msg="a confirmed booking was re-priced by a date change",
		)
		self.assertMoney(after["room_charges_total"], before["room_charges_total"])
		self.assertMoney(line_after["room_rate"], line_before["room_rate"])
		self.assertMoney(line_after["total_amount"], line_before["total_amount"])

		# The interval really did move, so nothing above is vacuously true.
		self.assertEqual(getdate(line_after["arrival_date"]), getdate(self.world.day(3)))
		self.assertEqual(getdate(line_after["departure_date"]), getdate(self.world.day(5)))

	def test_a_shift_redates_the_rate_snapshot_without_changing_it(self):
		"""Same nights, same money, corrected dates.

		`_first_night_amount` reads the *earliest* snapshot row, so a booking
		shifted forward with its old dates still in the snapshot would compute a
		First Night cancellation charge from a night it never held.
		"""
		reservation = self.world.reservation(nights=2)
		reservation_service.confirm(reservation)
		frappe.db.commit()

		before = self.rate_snapshot(reservation)
		total = flt(self.header(reservation)["total_amount"])

		self.assertEqual(
			[str(getdate(row["rate_date"])) for row in before],
			[str(self.world.day(0)), str(self.world.day(1))],
		)

		result = reservation_service.change_line_interval(
			reservation, self.line(reservation)["name"], arrival=self.world.day(3), departure=self.world.day(5)
		)

		after = self.rate_snapshot(reservation)

		self.assertEqual(result["rate_lines_redated"], 2)
		self.assertEqual(
			[str(getdate(row["rate_date"])) for row in after],
			[str(self.world.day(3)), str(self.world.day(4))],
			msg="the snapshot still describes the interval the guest no longer has",
		)
		self.assertEqual(
			[flt(row["net_rate"]) for row in after],
			[flt(row["net_rate"]) for row in before],
			msg="re-dating changed an amount",
		)
		self.assertEqual(len(after), 2, msg="a night gained or lost a snapshot row")
		self.assertMoney(
			sum(flt(row["net_rate"]) for row in after),
			total,
			msg="the snapshot no longer reconciles to the booking total",
		)

	def test_confirmed_rows_remain_rooms_one(self):
		"""A quantity is how three rooms are asked for, never how they are run."""
		reservation = self.world.reservation(nights=2, rooms=3)

		self.assertEqual(
			[row["rooms"] for row in self.world.lines(reservation)],
			[3],
			msg="a draft may still carry a quantity",
		)

		reservation_service.confirm(reservation)
		frappe.db.commit()

		lines = self.world.lines(reservation)
		self.assertEqual([row["rooms"] for row in lines], [1, 1, 1])

		reservation_service.change_line_interval(
			reservation, lines[0]["name"], arrival=self.world.day(2), departure=self.world.day(4)
		)

		after = self.world.lines(reservation)
		self.assertEqual(len(after), 3)
		self.assertEqual(
			[row["rooms"] for row in after],
			[1, 1, 1],
			msg="a confirmed row picked a quantity back up",
		)
		self.assertEqual(self.header(reservation)["total_rooms"], 3)

	def test_a_move_with_a_quantity_checks_the_summed_demand(self):
		"""A draft line may carry five rooms, and five is what must be free.

		Hardcoding one - which is safe in `extend_stay`, because a Stay is always
		exactly one room - would sell three rooms against one free room here.
		"""
		draft = self.world.reservation(nights=2, rooms=3)
		frappe.db.commit()

		self.assertEqual(self.line(draft)["rooms"], 3)

		# Two of the four rooms are taken on nights 4 and 5.
		filler = self.world.reservation(nights=2, rooms=2, arrival=self.world.day(4))
		reservation_service.confirm(filler)
		frappe.db.commit()
		self.assertEqual(self.world.available(4, 6), 2)

		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_interval(
				draft, self.line(draft)["name"], arrival=self.world.day(4), departure=self.world.day(6)
			)

		# Nights where all four are free take the whole quantity.
		reservation_service.change_line_interval(
			draft, self.line(draft)["name"], arrival=self.world.day(6), departure=self.world.day(8)
		)
		self.assertEqual(getdate(self.line(draft)["arrival_date"]), getdate(self.world.day(6)))

	def test_date_change_refused_for_a_checked_in_line(self):
		"""A room in use belongs to its Stay, and moves through `extend_stay`."""
		reservation = self._confirmed_pair()
		lines = self.world.lines(reservation)

		self.world.check_in(reservation, line=lines[0]["name"])

		self.assertEqual(
			self.header(reservation)["reservation_status"],
			reservation_service.CONFIRMED,
			msg="a two-room booking is not checked in until both rooms arrive",
		)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.change_line_interval(
				reservation, lines[0]["name"], arrival=self.world.day(1), departure=self.world.day(3)
			)

		# The room that has not arrived may still be moved — the rule is scoped
		# to the line, because that is the scope it actually has.
		reservation_service.change_line_interval(
			reservation, lines[1]["name"], arrival=self.world.day(1), departure=self.world.day(4)
		)
		self.assertEqual(
			getdate(self.line(reservation, 1)["arrival_date"]), getdate(self.world.day(1))
		)

		# Put it back on today so it can arrive, then the whole booking is in house.
		reservation_service.change_line_interval(
			reservation, lines[1]["name"], arrival=self.world.day(0), departure=self.world.day(3)
		)
		self.world.check_in(reservation, line=lines[1]["name"])
		self.assertEqual(
			self.header(reservation)["reservation_status"], reservation_service.CHECKED_IN
		)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.change_line_interval(
				reservation, lines[1]["name"], arrival=self.world.day(1), departure=self.world.day(4)
			)

	def test_a_date_change_does_not_re_split_the_deposit(self):
		"""The split is weighted by line value, and no line value moves here."""
		reservation = self.world.reservation_with_lines(
			[{"nights": 2}, {"nights": 4}], deposit=300.0
		)
		reservation_service.confirm(reservation)
		frappe.db.commit()

		before = reservation_service.deposit_allocation(frappe.get_doc("Reservation", reservation))
		self.assertMoney(sum(before.values()), 300.0)

		reservation_service.change_line_interval(
			reservation, self.line(reservation)["name"], arrival=self.world.day(4), departure=self.world.day(6)
		)

		after = reservation_service.deposit_allocation(frappe.get_doc("Reservation", reservation))

		self.assertEqual(
			after, before, msg="a date change re-weighted the deposit across the rooms"
		)
		self.assertMoney(sum(after.values()), 300.0)

	def test_a_move_is_logged_with_the_rate_it_preserved(self):
		"""A preserved rate is untraceable unless the move says so."""
		reservation = self.world.reservation(nights=2)
		reservation_service.confirm(reservation)
		frappe.db.commit()

		reservation_service.change_line_interval(
			reservation,
			self.line(reservation)["name"],
			arrival=self.world.day(3),
			departure=self.world.day(5),
			reason="Guest asked to travel later",
		)

		entries = self.modifications(reservation)
		self.assertEqual(len(entries), 1, msg=f"expected one audit row, got {entries}")

		entry = entries[0]
		self.assertEqual(entry["reason"], "Guest asked to travel later")
		self.assertEqual(entry["from_status"], reservation_service.CONFIRMED)
		self.assertEqual(entry["to_status"], reservation_service.CONFIRMED)

		details = frappe.parse_json(entry["details"])
		self.assertEqual(details["from"]["arrival"], str(self.world.day(0)))
		self.assertEqual(details["to"]["arrival"], str(self.world.day(3)))
		self.assertEqual(details["from"]["nights"], details["to"]["nights"])
		self.assertEqual(details["room_type"], self.world.room_type)
		self.assertMoney(details["preserved_rate"]["room_rate"], ROOM_RATE)
		self.assertMoney(details["preserved_rate"]["total_amount"], ROOM_RATE * 2)
		self.assertEqual(details["preserved_rate"]["rate_lines_redated"], 2)

	def test_add_and_remove_refused_once_holding(self):
		"""Cancel and rebook is the sanctioned path, and nothing here overrides it."""
		reservation = self._confirmed_pair()
		lines = self.world.lines(reservation)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.add_room_line(
				reservation, self.world.room_type, self.world.day(0), self.world.day(2)
			)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.remove_room_line(reservation, lines[0]["name"])

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.change_line_room_type(
				reservation, lines[0]["name"], self.world.room_type
			)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.set_line_rate_plan(
				reservation, lines[0]["name"], self.world.rate_plan
			)

		self.assertEqual(
			self.world.lines(reservation),
			lines,
			msg="a refused line operation still wrote something",
		)

	def test_a_line_of_another_reservation_is_refused(self):
		first = self.world.reservation(nights=2)
		second = self.world.reservation(nights=2)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				first, self.line(second)["name"], departure=self.world.day(4)
			)


# ---------------------------------------------------------------------------
# 4. The editable-state line operations
# ---------------------------------------------------------------------------


class TestEditableLineOperations(ModificationTestCase):
	"""Adding, removing, re-typing and re-planning, while that is still safe."""

	ROOMS = 4
	WORLD_TAG = "RMEDT"
	WORLD_CODE = "ME"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures = cls.world.fixtures

		cls.deluxe = fixtures.room_type(cls.world.property, "DLX", base_rate=DELUXE_RATE)
		fixtures.rooms(cls.world.property, cls.deluxe, count=2)

		# The world's plan must be able to price both types, or a room-type
		# change would be a rate-plan puzzle rather than an inventory question.
		plan = frappe.get_doc("Rate Plan", cls.world.rate_plan)
		if not any(row.room_type == cls.deluxe for row in plan.room_types):
			plan.append(
				"room_types", {"room_type": cls.deluxe, "base_rate": DELUXE_RATE, "is_active": 1}
			)
			plan.save(ignore_permissions=True)

		cls.premium_plan = _make_rate_plan(
			fixtures, cls.world.property, cls.world.room_type, "RP2", PREMIUM_RATE
		)

		frappe.db.commit()

	def _draft(self, specs: list[dict] | None = None) -> str:
		return self.world.reservation_with_lines(specs or [{"nights": 2}, {"nights": 3}])

	# -- adding ----------------------------------------------------------

	def test_adding_room_checks_incremental_demand(self):
		draft = self._draft([{"nights": 2}])

		filler = self.world.reservation(nights=2, rooms=self.ROOMS)
		reservation_service.confirm(filler)
		frappe.db.commit()

		self.assertEqual(self.world.available(0, 2), 0, msg="the premise is a full house")

		with self.assertRaises(AvailabilityError):
			reservation_service.add_room_line(
				draft, self.world.room_type, self.world.day(0), self.world.day(2)
			)

		self.assertEqual(
			len(self.world.lines(draft)), 1, msg="a refused addition still added a row"
		)

		# Nights the house can take are added.
		result = reservation_service.add_room_line(
			draft, self.world.room_type, self.world.day(3), self.world.day(5)
		)

		lines = self.world.lines(draft)
		self.assertEqual(len(lines), 2)
		self.assertEqual(lines[-1]["name"], result["room_line"])
		self.assertEqual(
			lines[-1]["rooms"], 1, msg="an added room must be one operational row"
		)
		self.assertEqual(
			getdate(self.header(draft)["departure_date"]),
			getdate(self.world.day(5)),
			msg="the header did not widen to span the added room",
		)

	# -- removing --------------------------------------------------------

	def test_removing_room_releases_only_that_room_inventory(self):
		draft = self._draft([{"nights": 2}, {"nights": 4}, {"nights": 2}])
		lines = self.world.lines(draft)
		keep = [lines[0], lines[2]]

		reservation_service.remove_room_line(draft, lines[1]["name"])

		remaining = self.world.lines(draft)
		self.assertEqual([row["name"] for row in remaining], [row["name"] for row in keep])

		for before, after in zip(keep, remaining, strict=True):
			self.assertEqual(after, before, msg="removing one room changed another")

		header = self.header(draft)
		self.assertEqual(
			getdate(header["departure_date"]),
			getdate(self.world.day(2)),
			msg="the header kept the removed room's departure",
		)
		self.assertEqual(header["total_rooms"], 2)

		reservation_service.confirm(draft)
		frappe.db.commit()

		self.assertEqual(self.world.sold(0, 2), 2)
		self.assertEqual(
			self.world.sold(2, 4), 0, msg="the removed room is still holding its nights"
		)

	def test_removing_the_last_room_line_is_refused(self):
		draft = self._draft([{"nights": 2}])

		with self.assertRaises(HospitalityPMSError):
			reservation_service.remove_room_line(draft, self.line(draft)["name"])

		self.assertEqual(len(self.world.lines(draft)), 1)

	# -- re-typing -------------------------------------------------------

	def test_changing_one_room_type_does_not_change_other_lines(self):
		draft = self._draft([{"nights": 2}, {"nights": 3}])
		target, untouched = self.world.lines(draft)

		reservation_service.change_line_room_type(draft, target["name"], self.deluxe)

		after = {row["name"]: row for row in self.world.lines(draft)}

		self.assertEqual(
			after[untouched["name"]],
			untouched,
			msg="changing one room's type changed another line",
		)
		self.assertEqual(after[target["name"]]["room_type"], self.deluxe)
		self.assertMoney(
			after[target["name"]]["room_rate"],
			DELUXE_RATE,
			msg="the new type was not priced by the rate service",
		)

	def test_changing_room_type_releases_a_room_of_the_old_type(self):
		draft = self._draft([{"nights": 2}])
		line = self.line(draft)
		reservation_service.assign_room(draft, line["name"], self.world.rooms[0])

		result = reservation_service.change_line_room_type(draft, line["name"], self.deluxe)

		self.assertEqual(result["assignment_released"], self.world.rooms[0])
		self.assertFalse(
			self.line(draft)["assigned_room"],
			msg="a room of the old type is still promised to this line",
		)

	def test_changing_room_type_is_availability_checked_on_the_new_type(self):
		draft = self._draft([{"nights": 2}])

		filler = self.world.reservation_with_lines(
			[
				{"nights": 2, "room_type": self.deluxe},
				{"nights": 2, "room_type": self.deluxe},
			]
		)
		reservation_service.confirm(filler)
		frappe.db.commit()

		with self.assertRaises(AvailabilityError):
			reservation_service.change_line_room_type(
				draft, self.line(draft)["name"], self.deluxe
			)

		self.assertEqual(self.line(draft)["room_type"], self.world.room_type)

	# -- re-planning -----------------------------------------------------

	def test_rate_plan_change_uses_the_rate_service(self):
		"""The caller chooses a plan. The rate is the rate service's answer."""
		draft = self._draft([{"nights": 2}])
		line = self.line(draft)

		# A rate the caller would like to be true. There is no parameter to pass
		# it through, so the only way to plant it is to write it onto the row -
		# and the reprice must overwrite it.
		frappe.db.set_value(
			"Reservation Room", line["name"], "room_rate", 999.0, update_modified=False
		)

		reservation_service.set_line_rate_plan(draft, line["name"], self.premium_plan)

		stored = frappe.db.get_value(
			"Reservation Room",
			line["name"],
			["rate_plan", "room_rate", "total_amount"],
			as_dict=True,
		)

		self.assertEqual(stored["rate_plan"], self.premium_plan)
		self.assertMoney(
			stored["room_rate"],
			PREMIUM_RATE,
			msg="the nightly rate did not come from the rate service",
		)
		self.assertMoney(stored["total_amount"], PREMIUM_RATE * 2)
		self.assertMoney(self.header(draft)["total_amount"], PREMIUM_RATE * 2)

		# And there is no way for a caller to name a rate at all, at either layer.
		for function in (
			reservation_service.set_line_rate_plan,
			reservations_api.set_line_rate_plan,
		):
			parameters = inspect.signature(function).parameters
			self.assertNotIn("rate", parameters)
			self.assertNotIn("room_rate", parameters)
			self.assertNotIn("total_amount", parameters)

	def test_an_empty_rate_plan_is_refused(self):
		draft = self._draft([{"nights": 2}])

		with self.assertRaises(HospitalityPMSError):
			reservation_service.set_line_rate_plan(draft, self.line(draft)["name"], "   ")

	# -- the arithmetic that has to survive all of it --------------------

	def test_total_remains_correct_after_multi_room_edit(self):
		draft = self._draft([{"nights": 2}, {"nights": 3}])

		def assert_consistent(where: str):
			header = self.header(draft)
			lines = self.world.lines(draft)

			self.assertMoney(
				header["total_amount"],
				sum(flt(row["total_amount"]) for row in lines),
				msg=f"the booking total is not the sum of its rooms {where}",
			)
			self.assertMoney(header["room_charges_total"], header["total_amount"], msg=where)
			self.assertEqual(
				header["total_rooms"],
				sum(int(row["rooms"]) for row in lines),
				msg=f"the room count is wrong {where}",
			)
			self.assertEqual(
				header["total_adults"],
				sum(int(row["adults"]) * int(row["rooms"]) for row in lines),
				msg=f"the occupancy total is wrong {where}",
			)

		assert_consistent("as booked")

		added = reservation_service.add_room_line(
			draft, self.world.room_type, self.world.day(0), self.world.day(4), adults=2
		)
		assert_consistent("after adding a room")

		reservation_service.set_line_rate_plan(draft, added["room_line"], self.premium_plan)
		assert_consistent("after a rate plan change")

		reservation_service.change_line_room_type(draft, self.line(draft)["name"], self.deluxe)
		assert_consistent("after a room type change")

		reservation_service.remove_room_line(draft, added["room_line"])
		assert_consistent("after removing a room")

		reservation_service.confirm(draft)
		frappe.db.commit()
		assert_consistent("after confirmation")

		# A held booking shifts rather than lengthens, so the totals must still be
		# the ones the guest was quoted after it moves.
		reservation_service.change_line_interval(
			draft, self.line(draft)["name"], arrival=self.world.day(4), departure=self.world.day(6)
		)
		assert_consistent("after a confirmed date change")

	def test_line_operations_are_audited(self):
		draft = self._draft([{"nights": 2}])

		added = reservation_service.add_room_line(
			draft, self.world.room_type, self.world.day(2), self.world.day(4)
		)
		reservation_service.set_line_rate_plan(draft, added["room_line"], self.premium_plan)
		reservation_service.change_line_room_type(draft, added["room_line"], self.deluxe)
		reservation_service.remove_room_line(draft, added["room_line"])

		reasons = [row["reason"] for row in self.modifications(draft)]

		self.assertEqual(len(reasons), 4, msg=f"one audit row per edit expected; got {reasons}")


# ---------------------------------------------------------------------------
# 5. The physical room is a different question from the room type
# ---------------------------------------------------------------------------


class TestRoomAlreadyGivenAway(ModificationTestCase):
	ROOMS = 2
	WORLD_TAG = "RMROM"
	WORLD_CODE = "MR"

	def test_date_change_refuses_a_room_already_given_away(self):
		first = self.world.confirmed(nights=2)
		reservation_service.assign_room(first, self.line(first)["name"], self.world.rooms[0])

		second = self.world.confirmed(nights=2, arrival=self.world.day(2))
		reservation_service.assign_room(second, self.line(second)["name"], self.world.rooms[0])
		frappe.db.commit()

		# The room *type* has capacity on the night being added - two rooms, one
		# sold - so only the room-level check can refuse this.
		self.assertEqual(self.world.available(2, 3), 1)

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				first, self.line(first)["name"], arrival=self.world.day(1), departure=self.world.day(3)
			)

		self.assertEqual(getdate(self.line(first)["departure_date"]), getdate(self.world.day(2)))
		self.assertEqual(
			self.line(first)["assigned_room"],
			self.world.rooms[0],
			msg="the refused move dropped the pre-assignment on its way out",
		)

	def test_a_move_into_free_nights_keeps_its_own_room(self):
		"""The paired positive: a line must not be treated as its own rival."""
		reservation = self.world.confirmed(nights=2)
		line = self.line(reservation)["name"]
		reservation_service.assign_room(reservation, line, self.world.rooms[0])
		frappe.db.commit()

		reservation_service.change_line_interval(
			reservation, line, arrival=self.world.day(2), departure=self.world.day(4)
		)

		after = self.line(reservation)
		self.assertEqual(after["assigned_room"], self.world.rooms[0])
		self.assertEqual(getdate(after["departure_date"]), getdate(self.world.day(4)))

	def test_a_move_onto_an_unsellable_room_is_refused(self):
		"""`assert_assignable` at the new dates, not merely at the old ones."""
		reservation = self.world.confirmed(nights=2)
		line = self.line(reservation)["name"]
		reservation_service.assign_room(reservation, line, self.world.rooms[0])

		frappe.db.set_value(
			"Hotel Room",
			self.world.rooms[0],
			"maintenance_status",
			"Out of Order",
			update_modified=False,
		)
		frappe.db.commit()

		with self.assertRaises(HospitalityPMSError):
			reservation_service.change_line_interval(
				reservation, line, arrival=self.world.day(2), departure=self.world.day(4)
			)

		self.assertEqual(getdate(self.line(reservation)["departure_date"]), getdate(self.world.day(2)))


# ---------------------------------------------------------------------------
# 6. Overselling on a move is the same decision it is on a confirmation
# ---------------------------------------------------------------------------


class TestOverbookingOnDateChange(ModificationTestCase):
	"""One room, one night of overbooking allowed, and a full night to sell."""

	ROOMS = 1
	WORLD_TAG = "RMOVB"
	WORLD_CODE = "MO"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures = cls.world.fixtures

		# The world builds a plain property; overselling needs a limit set.
		frappe.db.set_value(
			"Property", cls.world.property, "overbooking_limit", 1, update_modified=False
		)
		frappe.clear_document_cache("Property", cls.world.property)

		cls.agent = fixtures.user("ovbagent", ["Reservation Agent"], properties=[cls.world.property])
		cls.manager = fixtures.user(
			"ovbmgr", ["Reservation Manager"], properties=[cls.world.property]
		)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.world.fixtures.set_setting("enable_overbooking", 1)

		self.reservation = self.world.confirmed(nights=2)
		self.world.confirmed(nights=2, arrival=self.world.day(2))
		frappe.db.commit()

		self.room_line = self.line(self.reservation)["name"]

	def _move(self, **kwargs):
		"""Shift the booking one night forward, onto the night the other one holds.

		A shift rather than an extension, because a held booking may be moved but
		not lengthened - so the only thing standing between this move and the
		sold-out night is the availability check itself.
		"""
		return reservation_service.change_line_interval(
			self.reservation,
			self.room_line,
			arrival=self.world.day(1),
			departure=self.world.day(3),
			**kwargs,
		)

	def test_the_move_is_refused_outright_without_an_override(self):
		frappe.set_user(self.manager)

		with self.assertRaises(AvailabilityError):
			self._move()

	def test_overbooking_override_requires_authorization(self):
		"""An ordinary Reservation Agent may not oversell the house."""
		frappe.set_user(self.agent)

		with self.assertRaises(PermissionDeniedError):
			self._move(allow_overbooking=True, reason="Airline crew, contracted")

		frappe.set_user("Administrator")
		self.assertEqual(
			getdate(self.line(self.reservation)["departure_date"]), getdate(self.world.day(2))
		)

	def test_overbooking_override_requires_a_reason(self):
		frappe.set_user(self.manager)

		with self.assertRaises(HospitalityPMSError):
			self._move(allow_overbooking=True)

		with self.assertRaises(HospitalityPMSError):
			self._move(allow_overbooking=True, reason="   ")

	def test_overbooking_override_respects_the_system_switch(self):
		self.world.fixtures.set_setting("enable_overbooking", 0)
		frappe.set_user(self.manager)

		with self.assertRaises(HospitalityPMSError):
			self._move(allow_overbooking=True, reason="Airline crew, contracted")

	def test_an_authorized_override_moves_the_dates_and_is_audited(self):
		frappe.set_user(self.manager)

		self._move(allow_overbooking=True, reason="Airline crew, contracted")

		frappe.set_user("Administrator")
		self.assertEqual(
			getdate(self.line(self.reservation)["departure_date"]), getdate(self.world.day(3))
		)

		entries = self.modifications(self.reservation)
		self.assertEqual(len(entries), 1, msg=f"expected one audit row, got {entries}")

		self.assertEqual(entries[0]["changed_by"], self.manager)
		details = frappe.parse_json(entries[0]["details"])

		self.assertTrue(details.get("overbooking_override"))
		self.assertEqual(details.get("overbooking_reason"), "Airline crew, contracted")
		self.assertTrue(details.get("business_date"))

	def test_an_ordinary_move_records_no_override(self):
		"""A move that fitted must not look like an oversell in the audit."""
		frappe.set_user(self.manager)

		reservation_service.change_line_interval(
			self.reservation,
			self.room_line,
			arrival=self.world.day(4),
			departure=self.world.day(6),
		)

		frappe.set_user("Administrator")
		details = frappe.parse_json(self.modifications(self.reservation)[0]["details"]) or {}

		self.assertFalse(details.get("overbooking_override"))
		self.assertNotIn("overbooking_reason", details)


# ---------------------------------------------------------------------------
# 7. Guards a workspace that can edit a booking makes reachable
# ---------------------------------------------------------------------------


class TestGuardHoles(ModificationTestCase):
	"""Five controls that were open, and are reachable from a moving booking.

	Each was harmless only while nothing edited a held reservation. Every test
	here is paired with the supported path still working, because a guard that
	refuses everything is not a fix.
	"""

	ROOMS = 4
	WORLD_TAG = "RMGRD"
	WORLD_CODE = "MG"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		cls.policy = f"{cls.world.property}-NOCHG"
		if not frappe.db.exists("Rate Policy", cls.policy):
			frappe.get_doc(
				{
					"doctype": "Rate Policy",
					"policy_code": cls.policy,
					"policy_name": "Modification No Charge",
					"property": cls.world.property,
					"policy_type": "Cancellation",
					"is_active": 1,
					"charge_basis": "No Charge",
				}
			).insert(ignore_permissions=True)

		cls.world.fixtures.track("Rate Policy", cls.policy)
		frappe.db.commit()

	# -- the room rate ---------------------------------------------------

	def test_a_confirmed_lines_rate_cannot_be_edited_by_a_document_save(self):
		"""Nothing derives `room_rate` on a held line, so nothing corrected it."""
		reservation = self.world.confirmed(nights=2)
		line = self.line(reservation)["name"]

		doc = frappe.get_doc("Reservation", reservation)
		doc.rooms[0].room_rate = 50.0

		with self.assertRaises(InvalidStateTransitionError):
			doc.save(ignore_permissions=True)

		self.assertMoney(
			frappe.db.get_value("Reservation Room", line, "room_rate"),
			ROOM_RATE,
			msg="a confirmed line's rate was edited straight past the guard",
		)

	def test_a_save_that_changes_nothing_is_still_allowed_when_holding(self):
		"""The guard must compare a Currency as a number, not as text.

		Compared as text, a stored `100.0` and a recomputed `99.999999999` read as
		a change and every save of a held booking would be refused a field nobody
		touched.
		"""
		reservation = self.world.confirmed(nights=2)

		doc = frappe.get_doc("Reservation", reservation)
		doc.internal_notes = "Late arrival, holding the room"
		doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("Reservation", reservation, "internal_notes"),
			"Late arrival, holding the room",
		)

	# -- the fields that decide how a booking ends -----------------------

	def test_policies_account_and_required_deposit_are_locked_once_holding(self):
		reservation = self.world.confirmed(nights=2)

		for fieldname, value in (
			("cancellation_policy", self.policy),
			("no_show_policy", self.policy),
			("corporate_account", "SOME-OTHER-ACCOUNT"),
			("deposit_required", 250.0),
		):
			doc = frappe.get_doc("Reservation", reservation)
			doc.set(fieldname, value)

			with self.assertRaises(
				InvalidStateTransitionError, msg=f"{fieldname} was editable on a held booking"
			):
				doc.save(ignore_permissions=True)

			self.assertFalse(
				frappe.db.get_value("Reservation", reservation, fieldname),
				msg=f"{fieldname} was written despite the guard",
			)

	def test_those_fields_are_still_editable_while_the_booking_is_a_draft(self):
		"""The paired positive: these are ordinary fields until inventory is held."""
		draft = self.world.reservation(nights=2)

		doc = frappe.get_doc("Reservation", draft)
		doc.cancellation_policy = self.policy
		doc.deposit_required = 250.0
		doc.save(ignore_permissions=True)

		stored = frappe.db.get_value(
			"Reservation", draft, ["cancellation_policy", "deposit_required"], as_dict=True
		)
		self.assertEqual(stored["cancellation_policy"], self.policy)
		self.assertMoney(stored["deposit_required"], 250.0)

	# -- assignment after arrival ----------------------------------------

	def test_assigning_a_room_to_a_checked_in_line_is_refused(self):
		"""`Reservation Room.assigned_room` and `Stay.room` are one fact.

		This writes with `db.set_value`, so the controller guard never sees it - it
		would move the guest on paper and leave the room's occupancy, its
		housekeeping state and the folio pointing at the room they are in.
		"""
		reservation = self.world.confirmed(nights=2, rooms=2)
		lines = self.world.lines(reservation)

		result = self.world.check_in(reservation, line=lines[0]["name"])
		occupied = result["room"]
		spare = next(room for room in self.world.rooms if room != occupied)

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.assign_room(reservation, lines[0]["name"], spare)

		self.assertEqual(
			frappe.db.get_value("Reservation Room", lines[0]["name"], "assigned_room"),
			occupied,
			msg="a checked-in line was re-assigned behind its stay's back",
		)

		# The room that has not arrived may still be assigned.
		reservation_service.assign_room(reservation, lines[1]["name"], spare)
		self.assertEqual(
			frappe.db.get_value("Reservation Room", lines[1]["name"], "assigned_room"), spare
		)

	def test_assigning_a_room_once_the_booking_has_ended_is_refused(self):
		reservation = self.world.confirmed(nights=2)
		line = self.line(reservation)["name"]

		reservation_service.cancel(reservation, "guest cancelled the whole booking")
		frappe.db.commit()

		with self.assertRaises(InvalidStateTransitionError):
			reservation_service.assign_room(reservation, line, self.world.rooms[0])

		self.assertFalse(frappe.db.get_value("Reservation Room", line, "assigned_room"))

	# -- check-in reads the line, not the header -------------------------

	def test_check_in_reads_the_lines_arrival_not_the_headers(self):
		"""The hole the move service opens, closed with it.

		The header is the earliest arrival across the rooms, so a room moved a week
		out sits behind a header that still says today - and would get a Stay dated
		in the future with its room marked Occupied now.
		"""
		reservation = self.world.reservation_with_lines([{"nights": 2}, {"nights": 2}])
		reservation_service.confirm(reservation)
		frappe.db.commit()

		lines = self.world.lines(reservation)

		reservation_service.change_line_interval(
			reservation, lines[1]["name"], arrival=self.world.day(7), departure=self.world.day(9)
		)

		self.assertEqual(
			getdate(self.header(reservation)["arrival_date"]),
			getdate(self.world.day(0)),
			msg="the premise needs a header that still reads today",
		)

		with self.assertRaises(InvalidStateTransitionError):
			self.world.check_in(reservation, line=lines[1]["name"])

		self.assertEqual(
			frappe.db.count("Stay", {"reservation_room_line": lines[1]["name"]}),
			0,
			msg="a stay was created for a room that arrives next week",
		)

		# The room that is genuinely due still arrives.
		self.world.check_in(reservation, line=lines[0]["name"])
		self.assertEqual(
			frappe.db.count("Stay", {"reservation_room_line": lines[0]["name"]}), 1
		)

	# -- a reversed deposit is not a credited one ------------------------

	def test_a_reversed_deposit_is_not_counted_as_credited(self):
		"""`is_reversed` is the state a reversal leaves behind, and it was ignored.

		Counting a reversed credit made the remaining budget in `deposit_share`
		too small, so the room the deposit should have gone to was capped out of
		its share and the guest was asked for it twice.
		"""
		reservation = self.world.reservation_with_lines(
			[{"nights": 2}, {"nights": 2}], deposit=200.0
		)
		reservation_service.confirm(reservation)
		frappe.db.commit()

		lines = self.world.lines(reservation)
		self.world.check_in(reservation, line=lines[0]["name"])

		credited = reservation_service.deposit_credited(reservation)
		self.assertMoney(credited, 100.0, msg="the first room should have taken its share")

		payment = frappe.get_all(
			"Folio Payment",
			filters={"payment_type": "Deposit", "reference": reservation},
			pluck="name",
		)
		self.assertEqual(len(payment), 1)

		frappe.db.set_value("Folio Payment", payment[0], "is_reversed", 1, update_modified=False)

		self.assertMoney(
			reservation_service.deposit_credited(reservation),
			0.0,
			msg="a reversed deposit is still counted as credited",
		)

		# And the share is available again, rather than capped out by a credit
		# that was undone.
		doc = frappe.get_doc("Reservation", reservation)
		self.assertMoney(reservation_service.deposit_share(doc, lines[1]["name"]), 100.0)


# ---------------------------------------------------------------------------
# 8. The gates themselves
# ---------------------------------------------------------------------------


class TestModificationGates(IntegrationTestCase):
	"""The status lists these services gate on must stay tied to the machine.

	`TERMINAL_STATES` lives on the Reservation controller, which imports the
	service, so the service cannot import it back and states the complement
	positively instead. That is only safe if something checks - a status added to
	the workflow and to neither list would silently become editable in every
	state.
	"""

	def test_live_states_are_exactly_the_non_terminal_states(self):
		self.assertEqual(
			set(reservation_service.LIVE_STATES) | set(TERMINAL_STATES),
			set(reservation_service.TRANSITIONS),
			msg="a reservation status exists that is neither live nor terminal",
		)
		self.assertFalse(
			set(reservation_service.LIVE_STATES) & set(TERMINAL_STATES),
			msg="a status is both live and terminal",
		)

	def test_line_editable_states_are_exactly_the_controllers_own_predicate(self):
		"""`_is_editable()` decides whether the snapshot may move; so must these."""
		for status in reservation_service.TRANSITIONS:
			doc = frappe.get_doc({"doctype": "Reservation", "reservation_status": status})

			self.assertEqual(
				doc._is_editable(),
				status in reservation_service.LINE_EDITABLE_STATES,
				msg=f"{status} disagrees with Reservation._is_editable()",
			)

	def test_interval_editable_states_exclude_checked_in_and_terminal(self):
		self.assertNotIn(
			reservation_service.CHECKED_IN, reservation_service.INTERVAL_EDITABLE_STATES
		)
		self.assertFalse(
			set(reservation_service.INTERVAL_EDITABLE_STATES) & set(TERMINAL_STATES)
		)
		self.assertTrue(
			set(reservation_service.LINE_EDITABLE_STATES).issubset(
				reservation_service.INTERVAL_EDITABLE_STATES
			),
			msg="a line operation is allowed in a state a date change is not",
		)

	def test_the_detail_allow_list_names_only_real_unguarded_fields(self):
		meta = frappe.get_meta("Reservation")
		guarded = ("arrival_date", "departure_date", "property", "guest", "reservation_status")

		for fieldname in reservation_service.DETAIL_WRITABLE_FIELDS:
			field = meta.get_field(fieldname)

			self.assertIsNotNone(field, msg=f"{fieldname} is not a Reservation field")
			self.assertNotIn(
				fieldname, guarded, msg=f"{fieldname} is guarded and has its own service"
			)
			self.assertFalse(
				field.read_only,
				msg=f"{fieldname} is derived and must not be writable from a request",
			)

	def test_the_guarded_sets_name_real_fields_and_keep_their_additions(self):
		"""A guarded set that loses a field loses it silently."""
		from hospitality_pms.hospitality_reservations.doctype.reservation.reservation import (
			GUARDED_HEADER_FIELDS,
			LOCKED_ROOM_LINE_FIELDS,
		)

		for doctype, fields, required in (
			("Reservation Room", LOCKED_ROOM_LINE_FIELDS, ("room_rate",)),
			(
				"Reservation",
				GUARDED_HEADER_FIELDS,
				(
					"arrival_date",
					"departure_date",
					"property",
					"guest",
					"cancellation_policy",
					"no_show_policy",
					"corporate_account",
					"deposit_required",
				),
			),
		):
			meta = frappe.get_meta(doctype)

			for fieldname in fields:
				self.assertIsNotNone(
					meta.get_field(fieldname), msg=f"{doctype}.{fieldname} does not exist"
				)

			for fieldname in required:
				self.assertIn(
					fieldname, fields, msg=f"{doctype}.{fieldname} is no longer guarded"
				)

	def test_every_modification_endpoint_is_a_post_and_authorises_the_document(self):
		endpoints = (
			"update_reservation_details",
			"change_line_interval",
			"add_room_line",
			"remove_room_line",
			"change_line_room_type",
			"set_line_rate_plan",
		)

		for name in endpoints:
			function = getattr(reservations_api, name)

			self.assertIn(function, frappe.whitelisted, msg=f"{name} is not a whitelisted method")
			# Frappe stores the allowed methods as a list or, since mid-2026, a tuple.
			self.assertEqual(
				list(frappe.allowed_http_methods_for_whitelisted_func.get(function) or []),
				["POST"],
				msg=f"{name} must be POST so CSRF applies",
			)
			self.assertIn(
				"authorise_document",
				inspect.getsource(function),
				msg=f"{name} does not authorise the document it was handed",
			)
