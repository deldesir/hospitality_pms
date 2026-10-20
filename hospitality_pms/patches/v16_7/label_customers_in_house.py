"""Write the room onto the Customer of every guest in house when the field arrives.

The label is kept at check-in, check-out and room move from now on; guests who were already
in house got none. The Custom Field is normally made by `after_migrate`, which runs after
the patches, so it is made here first (idempotent), then every in-house guest is labelled.
"""

import frappe


def execute():
	from hospitality_pms.services.guests import sync_customer_room
	from hospitality_pms.setup.pos_bridge import ensure_custom_fields

	ensure_custom_fields()
	guests = frappe.get_all(
		"Stay", filters={"stay_status": ("in", ("In House", "Due Out"))}, pluck="guest", distinct=True
	)
	for guest in guests:
		try:
			sync_customer_room(guest)
		except Exception:
			frappe.log_error(title="Guest Customer room label (patch)", message=frappe.get_traceback())
