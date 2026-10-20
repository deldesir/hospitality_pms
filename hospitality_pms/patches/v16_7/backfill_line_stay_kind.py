"""Write `stay_kind` onto the reservation lines that predate the field.

A day-use or hourly line used to be recognised by its dates alone (departure on the arrival
date); the line now carries its kind, copied from its rate plan when the reservation is
validated, and check-in reads it. Lines saved before the field existed are same-day lines on
a short plan with the default `Night`: this writes the plan's kind on them, once. Night
lines keep the default. Nothing to do on a site that never had short stays.
"""

import frappe


def execute():
	if not frappe.db.has_column("Reservation Room", "stay_kind") or not frappe.db.has_column("Rate Plan", "stay_kind"):
		return
	frappe.db.sql(
		"""
		update `tabReservation Room` line
		join `tabReservation` res on res.name = line.parent
		join `tabRate Plan` plan on plan.name = coalesce(line.rate_plan, res.rate_plan)
		set line.stay_kind = plan.stay_kind
		where line.arrival_date = line.departure_date
		  and plan.stay_kind in ('Day Use', 'Hourly')
		  and coalesce(line.stay_kind, 'Night') = 'Night'
		"""
	)
