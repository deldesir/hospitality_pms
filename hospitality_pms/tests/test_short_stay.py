"""Day use and hourly stays: rooms sold for part of a day, next to the nights.

What is defended here: a same-day booking exists only on a day-use or hourly plan and is
priced by the day or by blocks; it holds a room for its hours, so it is refused when the
overnight guests fill the type through those hours and allowed when the hours fit around
them; hourly stays share one room when their hours do not overlap; the charge is posted at
check-in and never by the night audit; room assignment obeys the same hours; a walk-in can
be a day use; a day use is settled at checkout like a night.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate

from hospitality_pms.services import checkout as checkout_service
from hospitality_pms.services import folio as folio_service
from hospitality_pms.services import reservations as reservation_service
from hospitality_pms.services import short_stay
from hospitality_pms.services import stays as stay_service
from hospitality_pms.services import walk_in as walk_in_service
from hospitality_pms.services.availability import get_availability
from hospitality_pms.services.exceptions import AvailabilityError, HospitalityPMSError
from hospitality_pms.services.rates import get_rate_breakdown
from hospitality_pms.tests.posting_world import PostingWorld


class TestShortStays(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = PostingWorld("SHRT", "SS")
		fx = cls.world.fixtures
		prop = cls.world.property
		cls.prop = prop
		cls.today = getdate(frappe.db.get_value("Property", prop, "business_date"))
		# two room types: a pair of rooms, and a single room where every clash shows
		cls.pair = fx.room_type(prop, "PAIR", base_rate=100)
		cls.pair_rooms = fx.rooms(prop, cls.pair, count=2)
		cls.single = fx.room_type(prop, "ONE", base_rate=100)
		cls.single_rooms = fx.rooms(prop, cls.single, count=1)
		cls.night_pair = fx.rate_plan(prop, cls.pair, base_rate=100, code="NP")
		cls.night_single = fx.rate_plan(prop, cls.single, base_rate=100, code="NS")
		cls.day_pair = fx.rate_plan(prop, cls.pair, base_rate=60, code="DP", stay_kind="Day Use", day_use_from="09:00:00", day_use_until="18:00:00")
		cls.day_single = fx.rate_plan(prop, cls.single, base_rate=60, code="DS", stay_kind="Day Use", day_use_from="09:00:00", day_use_until="18:00:00")
		cls.hourly_single = fx.rate_plan(prop, cls.single, base_rate=25, code="HS", stay_kind="Hourly", block_hours=3)
		# the checkout invoice needs an item behind the new charge types
		profile = frappe.get_doc("Posting Profile", cls.world.profile)
		for charge_type in ("Day Use", "Hourly"):
			if not any(row.charge_type == charge_type for row in profile.charge_items):
				profile.append("charge_items", {"charge_type": charge_type, "item": cls.world.room_item, "is_active": 1})
		profile.save(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls.world.teardown()
		frappe.db.commit()
		super().tearDownClass()

	# -- helpers -------------------------------------------------------------

	def reserve(self, room_type, rate_plan, *, arrival=None, departure=None, arrival_time=None, departure_time=None, blocks=1, rooms=1, guest=None):
		arrival = arrival or self.today
		departure = departure or arrival
		doc = frappe.get_doc(
			{
				"doctype": "Reservation",
				"property": self.prop,
				"reservation_status": "Draft",
				"reservation_type": "Individual",
				"guest": guest or self.world.fixtures.guest("Short"),
				"arrival_date": arrival,
				"departure_date": departure,
				"arrival_time": arrival_time,
				"departure_time": departure_time,
				"rate_plan": rate_plan,
				"rooms": [
					{
						"room_type": room_type,
						"rooms": rooms,
						"adults": 1,
						"arrival_date": arrival,
						"departure_date": departure,
						"arrival_time": arrival_time,
						"departure_time": departure_time,
						"blocks": blocks,
						"rate_plan": rate_plan,
					}
				],
			}
		).insert(ignore_permissions=True)
		self.world.fixtures.track_fresh("Reservation", doc.name, {"Reservation Log": "reservation"})
		return doc.name

	def confirmed(self, *args, **kwargs):
		name = self.reserve(*args, **kwargs)
		reservation_service.confirm(name)
		frappe.db.commit()
		return name

	def line_of(self, reservation):
		return frappe.get_all("Reservation Room", filters={"parent": reservation}, fields=["name", "arrival_time", "departure_time", "nights", "total_amount", "room_rate"])[0]

	def available_night(self, room_type, date):
		figures = get_availability(self.prop, date, add_days(date, 1), room_type)["room_types"][room_type]["by_night"][str(date)]
		return figures["available"]

	# -- pricing ---------------------------------------------------------------

	def test_day_use_is_priced_once_for_the_day(self):
		q = get_rate_breakdown(self.prop, self.pair, self.today, self.today, rate_plan=self.day_pair)
		self.assertEqual(q["nights"], 0)
		self.assertEqual(q["stay_kind"], "Day Use")
		self.assertEqual((q["arrival_time"], q["departure_time"]), ("09:00:00", "18:00:00"))
		self.assertAlmostEqual(q["total_amount"], 60.0)

	def test_hourly_is_priced_per_block(self):
		q = get_rate_breakdown(self.prop, self.single, self.today, self.today, rate_plan=self.hourly_single, arrival_time="10:00:00", blocks=2)
		self.assertEqual(q["stay_kind"], "Hourly")
		self.assertEqual((q["arrival_time"], q["departure_time"]), ("10:00:00", "16:00:00"))
		self.assertAlmostEqual(q["total_amount"], 50.0)

	def test_a_same_day_booking_needs_a_short_plan(self):
		with self.assertRaises(HospitalityPMSError):
			self.reserve(self.pair, self.night_pair)
		frappe.db.rollback()

	def test_an_hourly_booking_needs_an_arrival_time(self):
		with self.assertRaises(HospitalityPMSError):
			self.reserve(self.single, self.hourly_single)
		frappe.db.rollback()

	# -- inventory -------------------------------------------------------------

	def test_day_use_holds_a_room_only_when_its_hours_reach_check_in(self):
		day = add_days(self.today, 10)
		before = self.available_night(self.pair, day)
		self.confirmed(self.pair, self.day_pair, arrival=day)                      # 09:00-18:00, past 14:00 check-in
		self.assertEqual(self.available_night(self.pair, day), before - 1)
		self.confirmed(self.pair, self.day_pair, arrival=day, arrival_time="08:00:00", departure_time="11:00:00")
		self.assertEqual(self.available_night(self.pair, day), before - 1)        # the morning one holds nothing overnight

	def test_hourly_stays_share_a_room_when_their_hours_do_not_overlap(self):
		day = add_days(self.today, 20)
		self.confirmed(self.single, self.hourly_single, arrival=day, arrival_time="09:00:00")   # 09-12
		self.confirmed(self.single, self.hourly_single, arrival=day, arrival_time="12:00:00")   # 12-15
		with self.assertRaises(AvailabilityError):
			self.confirmed(self.single, self.hourly_single, arrival=day, arrival_time="11:00:00")   # 11-14 overlaps both
		frappe.db.rollback()

	def test_overnight_arrivals_fill_the_type_for_the_afternoon(self):
		day = add_days(self.today, 30)
		self.confirmed(self.single, self.night_single, arrival=day, departure=add_days(day, 1))
		with self.assertRaises(AvailabilityError):
			self.confirmed(self.single, self.day_single, arrival=day)                           # 09-18 runs into the arrival
		frappe.db.rollback()
		self.confirmed(self.single, self.day_single, arrival=day, arrival_time="08:00:00", departure_time="11:00:00")   # done before check-in

	def test_a_departing_guest_holds_the_room_until_check_out(self):
		day = add_days(self.today, 40)
		self.confirmed(self.single, self.night_single, arrival=add_days(day, -1), departure=day)
		with self.assertRaises(AvailabilityError):
			self.confirmed(self.single, self.day_single, arrival=day, arrival_time="10:00:00", departure_time="18:00:00")
		frappe.db.rollback()
		self.confirmed(self.single, self.day_single, arrival=day, arrival_time="12:00:00", departure_time="18:00:00")

	def test_room_assignment_obeys_the_hours(self):
		day = add_days(self.today, 50)
		room = self.single_rooms[0]
		first = self.confirmed(self.single, self.hourly_single, arrival=day, arrival_time="09:00:00")    # 09-12
		reservation_service.assign_room(first, self.line_of(first)["name"], room)
		second = self.confirmed(self.single, self.hourly_single, arrival=day, arrival_time="12:00:00")   # 12-15
		reservation_service.assign_room(second, self.line_of(second)["name"], room)                      # fits after the first
		frappe.db.commit()
		night = self.reserve(self.single, self.night_single, arrival=day, departure=add_days(day, 1))
		with self.assertRaises(HospitalityPMSError):
			reservation_service.assign_room(night, self.line_of(night)["name"], room)                   # 12-15 runs past 14:00
		frappe.db.rollback()

	# -- the stay ----------------------------------------------------------------

	def test_check_in_charges_the_day_and_the_audit_leaves_it_alone(self):
		res = self.confirmed(self.pair, self.day_pair)
		line = self.line_of(res)
		result = stay_service.check_in(res, line["name"], self.pair_rooms[0], skip_id_check=True)
		frappe.db.commit()
		stay = frappe.get_doc("Stay", result["stay"])
		self.assertEqual((stay.stay_kind, stay.nights, stay.stay_status), ("Day Use", 0, "In House"))
		charges = frappe.get_all("Folio Charge", filters={"parent": stay.folio, "charge_type": "Day Use"}, fields=["amount", "idempotency_key"])
		self.assertEqual(len(charges), 1)
		self.assertAlmostEqual(flt(charges[0]["amount"]), 60.0)
		self.assertEqual(charges[0]["idempotency_key"], f"short-stay:{stay.name}")
		owed = [s["name"] for s in stay_service.get_stays_for_room_charge(self.prop, self.today)]
		self.assertNotIn(stay.name, owed)
		self.checkout_settles(stay)

	def checkout_settles(self, stay):
		folio_service.post_payment(stay.folio, 60, "Cash", idempotency_key=f"test:pay:{stay.name}")
		frappe.db.commit()
		checkout_service.check_out(stay.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Stay", stay.name, "stay_status"), "Checked Out")
		self.assertIn(frappe.db.get_value("Guest Folio", stay.folio, "folio_status"), ("Settled", "Closed"))
		self.assertAlmostEqual(flt(frappe.db.get_value("Guest Folio", stay.folio, "balance")), 0.0)

	def test_a_walk_in_can_be_a_day_use(self):
		guest = self.world.fixtures.guest("DayWalk")
		result = walk_in_service.create_walk_in(
			property_name=self.prop,
			guest=guest,
			departure_date=self.today,
			room_type=self.pair,
			room=self.pair_rooms[1],
			rate_plan=self.day_pair,
			arrival_time="10:00:00",
			departure_time="16:00:00",
		)
		frappe.db.commit()
		stay = frappe.get_doc("Stay", result["stay"])
		self.assertEqual(stay.stay_kind, "Day Use")
		self.assertEqual((str(stay.arrival_time)[:5], str(stay.departure_time)[:5]), ("10:00", "16:00"))
		self.assertEqual(frappe.db.count("Folio Charge", {"parent": stay.folio, "charge_type": "Day Use"}), 1)
