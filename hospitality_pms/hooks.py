app_name = "hospitality_pms"
app_title = "Hospitality PMS"
app_publisher = "Globcom Qatar"
app_description = "Property Management System for Frappe/ERPNext v16"
app_email = "waheed@globcomqatar.com"
app_license = "mit"

# ERPNext is the financial and enterprise system of record (HPMS-DEC-002).
required_apps = ["frappe/erpnext"]

app_logo_url = "/assets/hospitality_pms/images/hospitality-pms-logo.svg"

# ------------------------------------------------------------------------------
# Desk apps screen
# ------------------------------------------------------------------------------
# Without this the app is installed but invisible: Desk lists only ERPNext, and
# a member of staff has no way to reach /pms except by typing the URL. The tile
# points at the operational frontend rather than the Desk workspace because that
# is the app's front door -- the workspace stays reachable from Desk itself for
# the configuration and administration work that belongs there.
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": "/pms",
		"has_permission": "hospitality_pms.check_app_permission",
	}
]

# ------------------------------------------------------------------------------
# Installation
# ------------------------------------------------------------------------------

after_install = "hospitality_pms.install.after_install"
after_migrate = "hospitality_pms.install.after_migrate"
before_uninstall = "hospitality_pms.install.before_uninstall"

# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------
# Fixtures must stay reproducible across benches. Only export records the app
# owns; never export site-specific or ERPNext master data.
#
# Workspaces ship as JSON under each module's workspace/ folder (developer
# mode export), so they reach every bench automatically with the app source.
# Number Card is not a "Document Export" doctype -- developer mode does not
# write it to disk -- so the eight cards the "Hospitality PMS" workspace
# displays would otherwise exist only in the database they were created in.
# Listing them here makes `bench --site x export-fixtures` / `import-fixtures`
# carry them to every other bench the same way the workspace JSON already does.
fixtures = [
	{
		"dt": "Number Card",
		"filters": [
			[
				"name",
				"in",
				[
					"Arrivals Today",
					"In House",
					"Rooms Out of Order",
					"Housekeeping Tasks Pending",
					"Open Maintenance Tickets",
					"Failed Postings",
					"Integration Failures Pending",
					"Overdue Guest Requests",
				],
			]
		],
	},
	{
		"dt": "Dashboard Chart",
		"filters": [
			[
				"name",
				"in",
				[
					"Occupancy Percentage",
					"Room Revenue",
					"Rooms by Occupancy Status",
					"Rooms by Housekeeping Status",
					"Reservations by Type",
					"Reservation Pickup",
				],
			]
		],
	},
]

# ------------------------------------------------------------------------------
# Permissions
# ------------------------------------------------------------------------------
# Property-aware access is enforced server side (HPMS-DEC-052). Query conditions
# are registered per DocType as the domain builds land.

permission_query_conditions = {}
has_permission = {}

# ------------------------------------------------------------------------------
# Document Events
# ------------------------------------------------------------------------------

doc_events = {
	# A till's "Pay on Account" sale lands on the guest's folio (services/pos_bridge.py).
	"Sales Invoice": {
		"on_submit": "hospitality_pms.services.pos_bridge.on_submit",
		"before_cancel": "hospitality_pms.services.pos_bridge.before_cancel",
	},
	# A receipt taken at the till against such a sale is mirrored onto the folio.
	"Payment Entry": {
		"on_submit": "hospitality_pms.services.pos_bridge.on_payment_submit",
		"before_cancel": "hospitality_pms.services.pos_bridge.before_payment_cancel",
	},
}

# POS Next extension point: the till's customer picker shows the room of a guest in house.
pos_next_customer_annotate = ["hospitality_pms.services.pos_bridge.annotate_till_customers"]

# ------------------------------------------------------------------------------
# Scheduled Tasks
# ------------------------------------------------------------------------------

# Nothing in this app schedules itself. Without these entries the SLA sweep
# never escalates an overdue guest request, channels sell stale inventory, and
# failed integration work sits in the queue unretried -- all silently, which is
# the worst way for them to fail.
scheduler_events = {
	"cron": {
		# Every 15 minutes: escalate guest requests past their due time.
		"*/15 * * * *": [
			"hospitality_pms.tasks.sweep_guest_service_sla",
		],
		# Every 30 minutes: push availability to active channels and work the
		# integration failure queue.
		"*/30 * * * *": [
			"hospitality_pms.tasks.sync_channels",
			"hospitality_pms.tasks.retry_failed_integrations",
		],
		# Hourly: ask providers what became of operations whose reply was lost.
		# Rarer than the retry sweep on purpose - an ambiguous outcome is
		# resolved by a lookup, not by pressure, and every lookup is a request
		# to a provider that has already shown signs of being unwell.
		"7 * * * *": [
			"hospitality_pms.tasks.reconcile_ambiguous_operations",
		],
	},
}

# ------------------------------------------------------------------------------
# Desk
# ------------------------------------------------------------------------------

# Shared query report filters. A plain asset path rather than a bundle: it is one
# small file with no imports, and `bundled_asset` only consults the build
# manifest for names carrying `.bundle.`.
app_include_js = ["/assets/hospitality_pms/js/reports.js"]
app_include_css = []

# Puts the user's property on `frappe.boot` so a report filter can default to it
# without waiting on a request.
boot_session = "hospitality_pms.boot.boot_session"

# ------------------------------------------------------------------------------
# Website / Operational Frontend
# ------------------------------------------------------------------------------
# The Vue operational frontend is served from the Frappe site at /pms
# (HPMS-DEC-043, HPMS-DEC-050). No standalone Node runtime in production.

# Deep links such as /pms/reservations/RES-0001 are resolved by the Vue router,
# so every path under /pms is served by the same page.
website_route_rules = [
	{"from_route": "/pms/<path:app_path>", "to_route": "pms"},
]

# ------------------------------------------------------------------------------
# Jinja
# ------------------------------------------------------------------------------

jinja = {"methods": [], "filters": []}
