# Copyright (c) 2026, Globcom Qatar and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from hospitality_pms.services.availability import nights_between, stay_nights
from hospitality_pms.services.exceptions import (
	ConfigurationError,
	HospitalityPMSError,
	InvalidStateTransitionError,
)
from hospitality_pms.services.guests import assert_not_blacklisted
from hospitality_pms.services.reservations import (
	CANCELLED,
	DRAFT,
	HOLDING_STATES,
	TENTATIVE,
	find_by_external_reference,
	price_reservation,
	sync_room_lines,
)

ROOM_TYPE_DOCTYPE = "Room Type"
GUEST_DOCTYPE = "Guest"

#: Once a reservation has left inventory-holding behind it is done: nothing
#: further changes it except a new transition service call. Kept alongside
#: HOLDING_STATES (imported above) rather than merged into it, because the
#: two lists mean different things - one still consumes inventory, the other
#: never will again.
TERMINAL_STATES = ("Checked Out", "Closed", CANCELLED, "No Show")

#: Statuses a brand-new reservation may be saved as directly from the form.
#:
#: Confirmed is deliberately NOT here. Confirming is the moment inventory is
#: committed, and `ReservationService.confirm` is the only path that locks the
#: room types and re-checks availability first. Allowing an insert straight
#: into Confirmed would let a Desk user oversell the house by typing a status.
CREATABLE_STATUSES = (DRAFT, TENTATIVE)


#: Room line fields a document save may not touch once the line holds
#: inventory. Each has a service that owns it and re-checks under lock.
#:
#: `room_rate` is here because nothing else was holding it. It is not derived on
#: a holding line - `price_reservation` does not run past a holding status and
#: `sync_room_lines` recomputes neither the rate nor the line total - so a save
#: could put 500 down to 50 on a confirmed line while `total_amount`, the
#: booking's totals and every `Reservation Rate Line` kept the figure the guest
#: was actually quoted, and nothing recorded that anyone had done it.
LOCKED_ROOM_LINE_FIELDS = (
	"arrival_date",
	"departure_date",
	"assigned_room",
	"rooms",
	"room_type",
	"room_rate",
)

#: Header fields a document save may not touch once the booking is holding.
#:
#: The first four are what a fresh availability check depends on, and are the
#: original four. The rest decide what happens when the booking *ends*, and were
#: open to a plain form save:
#:
#: * `cancellation_policy` and `no_show_policy` decide the charge. `cancel()`
#:   requires `CANCEL_OVERRIDE_ROLES` only when there is a charge to waive, so
#:   editing the policy to one that charges nothing waived the fee without ever
#:   meeting the role check that exists to authorise waiving it.
#: * `corporate_account` decides whose credit comes back. Credit is consumed at
#:   confirmation against the account named then; editing the field before
#:   cancelling released credit to an account that never consumed any and left
#:   the original consuming it forever.
#: * `deposit_required` is what `stays._assert_deposit_satisfied` gates check-in
#:   on, so lowering it walked a guest in without the deposit the booking was
#:   sold on.
#:
#: Each has a service that owns it, or none at all - in which case the answer is
#: cancel and rebook, not a silent edit.
GUARDED_HEADER_FIELDS = (
	"arrival_date",
	"departure_date",
	"property",
	"guest",
	"cancellation_policy",
	"no_show_policy",
	"corporate_account",
	"deposit_required",
)

#: Fieldtypes whose values must be compared as numbers rather than as text.
NUMERIC_FIELDTYPES = ("Currency", "Float", "Percent")


def _values_agree(field, before, after) -> bool:
	"""Whether a guarded field still holds the value it held before this save.

	Text comparison is the default and is what the room line guard has always
	used: it makes a date that arrived as a string equal to the same date, and an
	empty link equal to `None`.

	Numbers are compared as numbers at the column's own precision instead. A
	Currency is stored rounded to two places and read back as a float, so a
	value recomputed in memory to full precision differs from the stored one in
	the ninth decimal - and as text that reads as a change, which would refuse a
	save that altered nothing. This became load-bearing when `room_rate` and
	`deposit_required` joined the guarded sets.
	"""
	if field is not None and field.fieldtype in NUMERIC_FIELDTYPES:
		precision = frappe.get_precision(field.parent, field.fieldname) or 2

		return flt(before, precision) == flt(after, precision)

	return str(before or "") == str(after or "")


class Reservation(Document):
	def before_insert(self):
		self.booked_on = now_datetime()

	def validate(self):
		# The two guards run first so an edit that is simply not allowed is
		# reported as such. Left until last, a forbidden change to the arrival
		# date would instead trip the room-line date check and tell the user
		# their room lines are wrong, which sends them to fix the wrong thing.
		self._guard_status_change()
		self._guard_holding_immutability()
		self._guard_deposit_received()

		self._validate_dates()
		self._validate_room_lines()
		self._validate_guest()
		self._validate_blacklist()
		self._validate_duplicate_external_reference()

		# Re-pricing is only safe while the reservation is still editable (see
		# _is_editable). Sync must run after pricing so the denormalised room
		# line fields reflect whatever price_reservation just wrote.
		if self._is_editable():
			price_reservation(self, check_restrictions=self._check_restrictions_on_price())

		sync_room_lines(self)

	def on_trash(self):
		# Reservation history is retained for ten years (SAS section 8). A
		# reservation that never left Draft never became real booking history,
		# so it alone may be deleted; the state machine never transitions back
		# to Draft, so "currently Draft" and "never left Draft" are the same
		# test.
		if self.reservation_status != DRAFT:
			frappe.throw(
				_(
					"Reservation {0} has left Draft and must be retained; it cannot be deleted."
				).format(self.name),
				exc=HospitalityPMSError,
			)

	# ------------------------------------------------------------------
	# Dates
	# ------------------------------------------------------------------

	def _validate_dates(self):
		"""SAS 3.4 mandatory control: arrival < departure, at both levels."""
		from hospitality_pms.services import short_stay

		self._derive_dates_from_room_lines()

		if getdate(self.arrival_date) > getdate(self.departure_date):
			self._throw_dates()

		for line in self.rooms:
			line.arrival_date = line.arrival_date or self.arrival_date
			line.departure_date = line.departure_date or self.departure_date

			if getdate(line.arrival_date) < getdate(self.arrival_date) or getdate(
				line.departure_date
			) > getdate(self.departure_date):
				frappe.throw(
					_(
						"Room line {0} ({1} to {2}) must fall within the reservation's stay ({3} to {4})."
					).format(
						line.idx, line.arrival_date, line.departure_date, self.arrival_date, self.departure_date
					),
					exc=HospitalityPMSError,
				)

			# The line's kind is a stored fact, written from its plan here and read by check-in
			# and the stay: a short plan on a line with nights, or a night plan on a same-day
			# line, is refused in `line_kind` rather than inferred again downstream.
			plan = line.rate_plan or self.rate_plan
			line.stay_kind = short_stay.line_kind(plan, line.arrival_date, line.departure_date) if plan else short_stay.NIGHT

		same_day = getdate(self.arrival_date) == getdate(self.departure_date)
		if same_day and not self._is_short_stay():
			self._throw_dates()

		self.nights = len(stay_nights(self.arrival_date, self.departure_date))

	def _throw_dates(self):
		frappe.throw(
			_("Arrival date {0} must be before departure date {1}.").format(self.arrival_date, self.departure_date),
			exc=HospitalityPMSError,
		)

	def _is_short_stay(self) -> bool:
		"""A same-day booking is a day-use or hourly stay when every room line sells on such a
		plan (its `stay_kind`, just written from the plan); the lines then carry the hours the
		room is held (resolved from the plan here, so pricing and availability see them)."""
		from hospitality_pms.services import short_stay

		if not self.rooms:
			return bool(self.rate_plan) and short_stay.is_short_plan(self.rate_plan)
		if not all((line.stay_kind or short_stay.NIGHT) in short_stay.SHORT_KINDS for line in self.rooms):
			return False
		for line in self.rooms:
			short_stay.apply_window(line, line.rate_plan or self.rate_plan)
		if self.rooms:
			self.arrival_time = self.arrival_time or self.rooms[0].arrival_time
			self.departure_time = self.departure_time or self.rooms[0].departure_time
		return True

	def _derive_dates_from_room_lines(self):
		"""The header summarises its rooms; it does not constrain them.

		A booking's dates are the span of the rooms it holds - the earliest
		arrival and the latest departure. Treating the header as the authority
		instead made it lie as soon as the rooms diverged: one room extended to
		the 15th and the header still read the 12th, and a room shortened by a
		guest leaving early would have dragged the whole booking's dates back
		with it (Wave 5, Part 10).

		Room lines that carry no dates of their own inherit the header's, so
		the ordinary single-room booking is unaffected: it derives back exactly
		what was typed in.
		"""
		if not self.rooms:
			return

		for line in self.rooms:
			line.arrival_date = line.arrival_date or self.arrival_date
			line.departure_date = line.departure_date or self.departure_date

		if not all(line.arrival_date and line.departure_date for line in self.rooms):
			return

		self.arrival_date = min(getdate(line.arrival_date) for line in self.rooms)
		self.departure_date = max(getdate(line.departure_date) for line in self.rooms)

	# ------------------------------------------------------------------
	# Room lines
	# ------------------------------------------------------------------

	def _validate_room_lines(self):
		if self.reservation_status != DRAFT and not self.rooms:
			frappe.throw(
				_("At least one room line is required once a reservation leaves Draft."),
				exc=HospitalityPMSError,
			)

		for line in self.rooms:
			if int(line.rooms or 0) < 1:
				frappe.throw(
					_("Room line {0} must book at least one room.").format(line.idx),
					exc=HospitalityPMSError,
				)

			if not line.room_type:
				continue

			room_type_property = frappe.db.get_value(ROOM_TYPE_DOCTYPE, line.room_type, "property")

			if room_type_property != self.property:
				frappe.throw(
					_("Room type {0} on line {1} does not belong to property {2}.").format(
						line.room_type, line.idx, self.property
					),
					exc=ConfigurationError,
				)

	# ------------------------------------------------------------------
	# Guest
	# ------------------------------------------------------------------

	def _validate_guest(self):
		if self.reservation_status != DRAFT and not self.guest:
			frappe.throw(
				_("A guest is required once a reservation leaves Draft."),
				exc=HospitalityPMSError,
			)

		if not self.guest or (self.guest_email and self.guest_mobile):
			return

		# Channel imports and quick walk-ins often only supply the guest link;
		# fill the contact fields from the guest record rather than leaving
		# them blank, but never overwrite a value already entered on this
		# reservation (it may be a stay-specific contact, not the guest's own).
		email, mobile = frappe.db.get_value(GUEST_DOCTYPE, self.guest, ["email_id", "mobile_no"]) or (
			None,
			None,
		)
		self.guest_email = self.guest_email or email
		self.guest_mobile = self.guest_mobile or mobile

	def _validate_blacklist(self):
		"""A blacklisted guest must not get a live booking (Draft/Cancelled are not live)."""
		if self.guest and self.reservation_status not in (DRAFT, CANCELLED):
			assert_not_blacklisted(self.guest)

	# ------------------------------------------------------------------
	# Duplicate channel booking protection
	# ------------------------------------------------------------------

	def _validate_duplicate_external_reference(self):
		"""SAS 3.4 duplicate external booking protection for channel imports."""
		if not self.external_reference or self.reservation_status == CANCELLED:
			return

		existing = find_by_external_reference(self.property, self.external_reference)

		if existing and existing != self.name:
			frappe.throw(
				_("External reference {0} is already used by reservation {1}.").format(
					self.external_reference, existing
				),
				exc=HospitalityPMSError,
			)

	# ------------------------------------------------------------------
	# Pricing
	# ------------------------------------------------------------------

	def _is_editable(self) -> bool:
		"""Whether the rate snapshot may still be recomputed.

		Re-pricing is safe exactly while the reservation has not yet committed
		to inventory or an outcome: once it reaches a HOLDING_STATES status
		(Confirmed, Guaranteed, Checked In) the guest has been quoted these
		amounts and re-running today's rate grid would silently change what
		they owe; once it reaches a terminal status (Checked Out, Closed,
		Cancelled, No Show) the stay is over or never happened and there is
		nothing left to price. Draft, Tentative and Waitlisted are the only
		statuses where the snapshot is still a working draft.
		"""
		return self.reservation_status not in HOLDING_STATES and self.reservation_status not in TERMINAL_STATES

	def _check_restrictions_on_price(self) -> bool:
		"""Whether restriction checks (stop-sell, min-LOS, CTA...) run while pricing.

		Draft and Tentative reservations - including a brand-new one, whose
		status defaults to Draft - are still being put together, so a
		restriction violation should be surfaced immediately. Waitlisted is
		different: a reservation lands there precisely because a restriction
		or availability check failed elsewhere, so re-validating restrictions
		on every subsequent save of a Waitlisted row would make it impossible
		to save the very state that records that failure. Its snapshot is
		still refreshed for informational totals, just without re-throwing on
		the restriction that put it there.
		"""
		return self.is_new() or self.reservation_status in (DRAFT, TENTATIVE)

	# ------------------------------------------------------------------
	# Status is service-owned
	# ------------------------------------------------------------------

	def _guard_status_change(self):
		"""Refuse a direct edit of `reservation_status`.

		Every real transition goes through `ReservationService._transition`,
		which writes the field with `frappe.db.set_value()` - that call does
		not run `validate()`, so this guard never fights the service; it only
		catches a form save or API call that tried to set the field itself.
		"""
		if self.is_new():
			# has_value_changed() has no prior value to compare against on
			# insert and unconditionally reports True, so a new document is
			# checked against the fixed list of legal starting statuses
			# instead.
			if self.reservation_status not in CREATABLE_STATUSES:
				frappe.throw(
					_("A new reservation can only be created with status {0}.").format(
						", ".join(CREATABLE_STATUSES)
					),
					exc=InvalidStateTransitionError,
				)
			return

		if self.has_value_changed("reservation_status"):
			frappe.throw(
				_(
					"Reservation status is changed only through a reservation transition, "
					"not by editing the document directly."
				),
				exc=InvalidStateTransitionError,
			)

	# ------------------------------------------------------------------
	# Immutability once holding
	# ------------------------------------------------------------------

	def _guard_holding_immutability(self):
		"""Refuse edits a document save cannot make safely.

		Two kinds of field. The dates, the property and the guest are what a
		fresh availability check depends on, and changing them once the booking
		holds inventory is only safe if that check is re-run under lock - which
		is `reservations.change_line_interval`, the move/rebook service, and not
		something a `doc.save()` does. The policies, the corporate account and
		the required deposit are what the *ending* of the booking depends on, and
		each of them routed around a control when edited here (see
		`GUARDED_HEADER_FIELDS`).

		For every one of them the supported answer is a service operation or
		cancel and rebook. This guard is what makes that true rather than merely
		documented.
		"""
		if self.is_new() or self._is_editable():
			return

		for fieldname in GUARDED_HEADER_FIELDS:
			if self._guarded_value_changed(fieldname):
				frappe.throw(
					_(
						"{0} cannot be changed once a reservation is {1}; cancel and rebook instead."
					).format(_(self.meta.get_label(fieldname)), _(self.reservation_status)),
					exc=InvalidStateTransitionError,
				)

		self._guard_room_line_immutability()

	def _guard_deposit_received(self):
		"""`deposit_received` is money the hotel actually holds; it is never set
		through the document API.

		The field is read at check-in and converted, pound for pound, into a folio
		Deposit payment (stays._create_folio_for_stay). If a booking payload or a
		`doc.save()` could set it, any reservation-write role could mint a payment
		the drawer never took - the guest then settles that much short at checkout.
		It carries `read_only` (a UI hint only, no server force) and permlevel 0,
		so nothing else stops it. Its label says "Maintained by the payment and
		folio services": those write it with a direct `db.set_value` against real
		money received, which does not pass through here - so refusing every ORM
		change closes the fabrication without blocking the sanctioned writer.
		"""
		field = self.meta.get_field("deposit_received")

		if self.is_new():
			if flt(self.deposit_received):
				frappe.throw(
					_(
						"Deposit received is recorded by the payments service when money is "
						"taken; it cannot be set when creating a reservation."
					),
					exc=InvalidStateTransitionError,
				)
			return

		before = self.get_doc_before_save()

		if before and not _values_agree(field, before.get("deposit_received"), self.deposit_received):
			frappe.throw(
				_(
					"Deposit received is maintained by the payments service and cannot be "
					"changed by editing the reservation."
				),
				exc=InvalidStateTransitionError,
			)

	def _guarded_value_changed(self, fieldname: str) -> bool:
		"""`has_value_changed`, but comparing a Currency as a number.

		Frappe's own `has_value_changed` compares with `!=` and answers True when
		it cannot see the document's previous state at all. Both behaviours are
		kept: the conservative answer to "we do not know" is still "refuse", and
		only the comparison itself is corrected (see `_values_agree`).
		"""
		before = self.get_doc_before_save()

		if not before:
			return True

		return not _values_agree(
			self.meta.get_field(fieldname), before.get(fieldname), self.get(fieldname)
		)

	def _guard_room_line_immutability(self):
		"""The same protection, one level down, where the inventory actually is.

		`Reservation Room` is what availability counts and what a room is
		promised on, so every service that touches it re-checks under lock:
		`extend_stay` asks availability again, `shorten_stay` releases the
		nights, `assign_room` locks the room and refuses one already given
		away. A `frappe.get_doc(...).save()` does none of that, and until this
		guard existed it was the easy way round all of them - move a departure
		out by three nights and the hotel oversells without a single check
		having run.

		Those services write with `frappe.db.set_value`, which does not run
		validation, so the supported paths are unaffected by design rather
		than by exemption.
		"""
		before = self.get_doc_before_save()
		if not before:
			return

		previous = {row.name: row for row in before.rooms}

		if {row.name for row in self.rooms} != set(previous):
			frappe.throw(
				_(
					"Room lines cannot be added or removed once a reservation is {0}; cancel and rebook instead."
				).format(_(self.reservation_status)),
				exc=InvalidStateTransitionError,
			)

		for row in self.rooms:
			old = previous[row.name]

			for fieldname in LOCKED_ROOM_LINE_FIELDS:
				if _values_agree(row.meta.get_field(fieldname), old.get(fieldname), row.get(fieldname)):
					continue

				frappe.throw(
					_(
						"{0} on room line {1} cannot be changed by editing the reservation; use the front office operation instead."
					).format(_(row.meta.get_label(fieldname)), row.idx),
					exc=InvalidStateTransitionError,
				)
