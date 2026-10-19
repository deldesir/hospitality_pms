"""Install and migrate hooks for Hospitality PMS.

Everything performed here must be idempotent: `install-app`, `migrate` and
re-installation on an existing site must all converge to the same state.
"""

import frappe


def after_install():
	"""Run once when the app is installed on a site."""
	sync_module_defs()
	sync_roles()
	sync_desktop_presence()
	sync_schema_constraints()
	sync_custom_fields()


def after_migrate():
	"""Run after every `bench migrate`.

	Module Defs are only created by the framework at install time, so a module
	added to `modules.txt` in a later build would never appear on a site that is
	upgraded rather than freshly installed. Syncing here keeps every bench
	reproducible from the repository alone.
	"""
	sync_module_defs()
	sync_roles()
	sync_desktop_presence()
	sync_schema_constraints()
	sync_custom_fields()


def sync_custom_fields():
	"""The point-of-sale bridge's fields on POS Profile and Sales Invoice (setup/pos_bridge.py)."""
	from hospitality_pms.setup.pos_bridge import ensure_custom_fields

	ensure_custom_fields()


def sync_schema_constraints():
	"""Apply the database constraints the DocType JSON cannot express.

	Runs from both hooks on purpose. The `patches.txt` entry is what upgrades a
	site from 16.5.3, but Frappe marks every patch as already executed when an
	app is installed fresh rather than running it - so a brand-new site would
	otherwise never get the index. Calling the same idempotent function here
	makes fresh install, upgrade and repeated migrate converge on the same
	schema.
	"""
	from hospitality_pms.setup.schema import apply_all_constraints

	apply_all_constraints()
	frappe.db.commit()


def sync_desktop_presence():
	"""Make the Desk apps screen show the Hospitality PMS tile.

	A site whose users arranged their Desk before this app existed keeps a
	frozen copy of that arrangement, and the tile cannot reach them through the
	Desktop Icon alone. See `setup.desktop` for the full reasoning.
	"""
	from hospitality_pms.setup.desktop import sync_desktop_presence as _sync

	_sync()


def sync_roles():
	"""Create the approved PMS roles and correct their desk access.

	Frappe auto-creates any role a DocType JSON references, always with desk
	access enabled, so this has to run after the DocType sync to put the
	operational roles back to frontend-only.
	"""
	from hospitality_pms.setup.posting_service import ensure_posting_service
	from hospitality_pms.setup.roles import create_roles

	create_roles()

	# The identity Hospitality posts to ERPNext under. Kept here so a fresh
	# install, an upgrade and a repeated migrate all converge on the same
	# narrowly-scoped role, permission and user (UAT-004).
	#
	# Called **without** `repair`: this path grants, it never restores. The
	# restoration of what the pre-16.7.5-R1C implementation revoked is a one-shot
	# patch (`patches/v16_7/repair_erp_permission_clobber.py`), because the
	# fingerprint it recognises - every custom row on the parent belongs to the
	# posting service - is also what a deliberately locked-down site looks like,
	# and repairing on every migrate would overrule that operator for ever.
	ensure_posting_service()

	# Reported, never repaired. The patch above cannot reach every damaged site -
	# `bench install-app --force` over an already-installed site marks patches
	# complete without running them - and a site whose ERPNext permissions are
	# missing should say so on every migrate rather than only in a transcript
	# nobody kept. Read-only, four cheap queries, and it warns rather than throws:
	# an accounting permission model is not this function's to decide, and a
	# migrate must not fail because of one.
	_warn_on_erp_permission_drift()

	frappe.db.commit()


def _warn_on_erp_permission_drift():
	"""Log a warning if ERPNext's own grants are not in effect on our four DocTypes.

	The drift detector `setup/posting_service.py`'s docstring refers to, in the one
	place it can run on a production site - the test suite that also detects it
	never runs there.
	"""
	from hospitality_pms.patches.v16_7.repair_erp_permission_clobber import (
		_missing_standard_grants,
	)
	from hospitality_pms.setup.posting_service import SERVICE_PERMISSIONS

	for declared in SERVICE_PERMISSIONS:
		doctype = declared["doctype"]

		try:
			missing = _missing_standard_grants(doctype)
		except Exception:
			# A DocType that is not installed, or metadata that will not load, is
			# not a reason to fail a migrate.
			continue

		if not missing:
			continue

		message = (
			f"Hospitality PMS: {len(missing)} standard ERPNext permission grant(s) on "
			f"{doctype} are not in effect. Run "
			f"`bench --site <site> execute "
			f"hospitality_pms.patches.v16_7.repair_erp_permission_clobber.execute` "
			f"to restore them, or review Custom DocPerm for this DocType if the "
			f"narrowing was deliberate."
		)

		print(f"  {message}")
		frappe.logger("hospitality_pms").warning(message)


def before_uninstall():
	"""Run before the app is removed from a site.

	Operational and financial records are retained per the approved data
	retention policy (SAS section 8); nothing is destroyed here.
	"""
	pass


def sync_module_defs():
	"""Create Module Def records for every module declared in `modules.txt`.

	Modules that the app no longer declares are removed only when nothing
	references them, so a stale Module Def can never take DocTypes with it.
	"""
	declared = set(frappe.get_module_list("hospitality_pms"))

	existing = set(
		frappe.get_all(
			"Module Def",
			filters={"app_name": "hospitality_pms"},
			pluck="name",
		)
	)

	for module in sorted(declared - existing):
		doc = frappe.new_doc("Module Def")
		doc.app_name = "hospitality_pms"
		doc.module_name = module
		doc.insert(ignore_permissions=True, ignore_if_duplicate=True)

	for module in sorted(existing - declared):
		if frappe.db.exists("DocType", {"module": module}):
			continue

		frappe.delete_doc("Module Def", module, ignore_permissions=True, force=True)

	frappe.db.commit()
