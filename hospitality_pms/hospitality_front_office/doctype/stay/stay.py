# Copyright (c) 2026, Globcom Qatar and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime

from hospitality_pms.services.availability import nights_between, stay_nights
from hospitality_pms.services.base import (
	STAY_ORCHESTRATION,
	assert_service_context,
	in_service_context,
)
from hospitality_pms.services.exceptions import HospitalityPMSError, InvalidStateTransitionError
from hospitality_pms.services.stays import EXPECTED

#: The only status a brand-new stay may be created with.
#:
#: `StayService.check_in` inserts at Expected and promotes to In House with
#: `frappe.db.set_value()`, which does not run `validate()` - so In House never
#: needed to be creatable, and allowing it meant a document save could put a
#: guest straight into a room with no reservation, no folio and no room status
#: behind it.
#:
#: This is a narrowing, not the fix. The fix is `_guard_created_by_service`
#: below: a status list cannot tell an orchestrated check-in from a hand-built
#: document, because both would name a legal status.
CREATABLE_STATUSES = (EXPECTED,)


class Stay(Document):
	def before_insert(self):
		self._guard_created_by_service()

	def validate(self):
		# The two guards run first so an edit that is simply not allowed is
		# reported as such, rather than as some other field's derived rule
		# failing downstream.
		self._guard_status_change()
		self._guard_locked_fields()

		self._validate_dates()
		self._validate_room()
		self._validate_guest()
		self._validate_companions()
		self._stamp_notes()

	def on_trash(self):
		# Stay history is retained for ten years (SAS section 8). A stay that
		# never left Expected never became a real physical stay, so it alone
		# may be deleted; the state machine never transitions back to
		# Expected, so "currently Expected" and "never left Expected" are the
		# same test.
		if self.stay_status != EXPECTED:
			frappe.throw(
				_("Stay {0} has left Expected and must be retained; it cannot be deleted.").format(self.name),
				exc=HospitalityPMSError,
			)

	# ------------------------------------------------------------------
	# A stay is created by orchestration, never by a document save
	# ------------------------------------------------------------------

	def _guard_created_by_service(self):
		"""Refuse a Stay that no check-in orchestration created.

		A Stay is the record that a real person is in a real room. Creating one
		is not a data-entry act; it is the outcome of a sequence that has to
		happen first, all of which lives in `StayService.check_in`: the
		reservation is confirmed and due, the room line is not already checked
		in, the guest is not blacklisted, identification is captured, the
		deposit is satisfied, the room is assignable, a Folio is opened, the
		room is marked Occupied and the reservation follows.

		A `frappe.get_doc({...}).insert()` performs none of that, and the sweep
		showed the result: a guest In House with no folio to charge, a room
		still reading Vacant, no Room Status Log entry, availability unaware -
		and the same thing succeeding for a blacklisted guest.

		No arrangement of field values can distinguish that from a real
		check-in, because the difference is not in the values. It is in which
		code produced them, which is what the orchestration context records.

		Deliberately `before_insert`: an existing Stay is still editable
		through the paths that own it (`change_room`, `extend_stay`, notes and
		companions), each of which has its own guards.
		"""
		assert_service_context(
			STAY_ORCHESTRATION,
			_(
				"A stay is created by checking a guest in, which reserves the room, opens the "
				"folio and records the arrival. It cannot be created by saving a Stay directly."
			),
		)

	# ------------------------------------------------------------------
	# Dates
	# ------------------------------------------------------------------

	def _validate_dates(self):
		same_day = getdate(self.arrival_date) == getdate(self.departure_date)
		if getdate(self.arrival_date) > getdate(self.departure_date) or (
			same_day and (self.stay_kind or "Night") == "Night"
		):   # a day-use or hourly stay lives on its one date
			frappe.throw(
				_("Arrival date {0} must be before departure date {1}.").format(
					self.arrival_date, self.departure_date
				),
				exc=HospitalityPMSError,
			)

		self.nights = len(stay_nights(self.arrival_date, self.departure_date))

	# ------------------------------------------------------------------
	# Room
	# ------------------------------------------------------------------

	def _validate_room(self):
		if not self.room:
			return

		room_property, room_type = frappe.db.get_value("Hotel Room", self.room, ["property", "room_type"])

		if room_property != self.property:
			frappe.throw(
				_("Room {0} belongs to another property.").format(self.room),
				exc=HospitalityPMSError,
			)

		if self.room_type and room_type != self.room_type:
			frappe.throw(
				_("Room {0} is a {1}, not the stay's room type {2}.").format(
					self.room, room_type, self.room_type
				),
				exc=HospitalityPMSError,
			)

	# ------------------------------------------------------------------
	# Guest
	# ------------------------------------------------------------------

	def _validate_guest(self):
		if not self.guest:
			frappe.throw(_("A guest is required for a stay."), exc=HospitalityPMSError)

	# ------------------------------------------------------------------
	# Companions
	# ------------------------------------------------------------------

	def _validate_companions(self):
		if len(self.companions) == 1:
			# A single companion is unambiguously the primary guest - there is
			# nothing to choose between, so mark it rather than making the
			# front desk tick a box that has only one possible answer.
			self.companions[0].is_primary = 1
			return

		if len([row for row in self.companions if row.is_primary]) > 1:
			frappe.throw(
				_("Only one companion can be marked as the primary guest."),
				exc=HospitalityPMSError,
			)

	# ------------------------------------------------------------------
	# Notes
	# ------------------------------------------------------------------

	def _stamp_notes(self):
		for row in self.stay_notes:
			# `row.is_new()` is the child-row equivalent of `is_new()`: it is
			# true for a row appended in this save and false for one already
			# persisted, which is what tells a freshly logged note apart from
			# one being re-saved along with an unrelated change elsewhere on
			# the stay.
			if not row.is_new():
				continue

			row.noted_by = row.noted_by or frappe.session.user
			row.noted_on = row.noted_on or now_datetime()

	# ------------------------------------------------------------------
	# Status is service-owned
	# ------------------------------------------------------------------

	def _guard_status_change(self):
		"""Refuse a direct edit of `stay_status`.

		Every real transition goes through StayService, which writes the field
		with `frappe.db.set_value()` - that call does not run `validate()`, so
		this guard never fights the service; it only catches a form save or
		API call that tried to set the field itself.
		"""
		if self.is_new():
			# has_value_changed() has no prior value to compare against on
			# insert and unconditionally reports True, so a new document is
			# checked against the fixed list of legal starting statuses
			# instead.
			if self.stay_status not in CREATABLE_STATUSES:
				frappe.throw(
					_("A new stay can only be created with status {0}.").format(
						" or ".join(CREATABLE_STATUSES)
					),
					exc=InvalidStateTransitionError,
				)
			return

		if self.has_value_changed("stay_status"):
			frappe.throw(
				_("Stay status is changed only through a stay transition, not by editing the document directly."),
				exc=InvalidStateTransitionError,
			)

	# ------------------------------------------------------------------
	# Immutability once the guest has actually arrived
	# ------------------------------------------------------------------

	def _guard_locked_fields(self):
		"""Refuse edits to who and where a stay belongs to once it is real.

		`property`, `guest` and `reservation` identify the stay; once it has
		left Expected the guest is, or has been, physically in the property
		and these can no longer be silently repointed. `room` is deliberately
		excluded: StayService.change_room is the supported way to move a
		guest mid-stay and legitimately rewrites `room`/`room_type` on this
		same document while it is In House or Due Out.
		"""
		if self.is_new():
			return

		# The dates are inventory, not description. `StayService.extend_stay`
		# re-checks availability and moves the Reservation Room interval with
		# them; `shorten_stay` releases the nights. A save that moved these on
		# its own would leave the Stay and the interval availability counts
		# telling different stories - which is exactly P1-5 and P2-5.
		if not in_service_context(STAY_ORCHESTRATION):
			for fieldname in ("arrival_date", "departure_date"):
				if self.has_value_changed(fieldname):
					frappe.throw(
						_(
							"{0} cannot be changed by editing the stay; use extend or shorten instead."
						).format(_(self.meta.get_label(fieldname))),
						exc=InvalidStateTransitionError,
					)

		if self.stay_status == EXPECTED:
			return

		for fieldname in ("property", "guest", "reservation"):
			if self.has_value_changed(fieldname):
				frappe.throw(
					_("{0} cannot be changed once a stay is {1}.").format(
						_(self.meta.get_label(fieldname)), _(self.stay_status)
					),
					exc=InvalidStateTransitionError,
				)
