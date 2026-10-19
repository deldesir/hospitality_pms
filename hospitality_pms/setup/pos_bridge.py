"""The fields the point-of-sale bridge needs on ERPNext documents.

Custom Fields rather than DocType changes: ERPNext stays untouched, and a site that
removes this app keeps its invoices readable. Created on install and on every migrate;
`create_custom_fields` is idempotent.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"POS Profile": [
		{
			"fieldname": "hpms_charge_to_room",
			"label": "Charge credit sales to the guest's room",
			"fieldtype": "Check",
			"insert_after": "warehouse",
			"description": (
				"A sale put on the customer's account (Pay on Account) is charged to that "
				"customer's stay in house and settled at checkout with the room (Hospitality PMS)."
			),
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "hpms_stay",
			"label": "Charged to stay",
			"fieldtype": "Link",
			"options": "Stay",
			"insert_after": "pos_profile",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "hpms_folio",
			"label": "Charged to folio",
			"fieldtype": "Link",
			"options": "Guest Folio",
			"insert_after": "hpms_stay",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
	],
}


def ensure_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
