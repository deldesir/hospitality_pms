"""Session context for the operational frontend.

The Vue shell asks for this once on boot. It is the only place the frontend
learns who it is talking to, and it is advisory only: every endpoint re-checks
permissions server side regardless of what the shell was told.
"""

import frappe
from frappe import _
from frappe.permissions import AUTOMATIC_ROLES

RTL_LANGUAGES = {"ar", "he", "fa", "ur"}

#: Languages the operational frontend ships message catalogues for
#: (frontend/src/locales/<code>.json), and the server a PO file for (locale/<code>.po).
SUPPORTED_LANGUAGES = ("en", "ar", "fr")


@frappe.whitelist(methods=["GET"])
def get_session_context() -> dict:
	"""Return the authenticated user's operational context."""
	if frappe.session.user == "Guest":
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	user = frappe.get_cached_doc("User", frappe.session.user)
	language = normalise_language(user.language or frappe.local.lang or "en")
	roles = frappe.get_roles()

	return {
		"user": {
			"name": user.name,
			"full_name": user.full_name,
			"user_image": user.user_image,
			"language": language,
			"time_zone": user.time_zone or frappe.db.get_single_value("System Settings", "time_zone"),
		},
		"roles": roles,
		"has_desk_access": has_desk_access(roles),
		"language": language,
		"direction": "rtl" if language in RTL_LANGUAGES else "ltr",
		"supported_languages": list(SUPPORTED_LANGUAGES),
		"system": {
			"app_version": frappe.get_attr("hospitality_pms.__version__"),
			"date_format": frappe.db.get_single_value("System Settings", "date_format"),
			"time_format": frappe.db.get_single_value("System Settings", "time_format"),
			"float_precision": frappe.db.get_single_value("System Settings", "float_precision"),
		},
	}


@frappe.whitelist(methods=["POST"])
def set_language(language: str) -> dict:
	"""Persist the user's operational language.

	Written against the User record so Desk, server-generated messages and the
	operational frontend all agree on one language for the user.
	"""
	language = normalise_language(language)

	if language not in SUPPORTED_LANGUAGES:
		frappe.throw(_("Language {0} is not supported.").format(language))

	frappe.db.set_value("User", frappe.session.user, "language", language, update_modified=False)
	frappe.local.lang = language

	return {"language": language, "direction": "rtl" if language in RTL_LANGUAGES else "ltr"}


def has_desk_access(roles: list[str]) -> bool:
	"""Whether any of the user's roles opens the Frappe Desk.

	Operational roles such as Room Attendant deliberately have desk access
	switched off, so the frontend should not offer them a link into /app that
	would only bounce them back.
	"""
	if frappe.session.user == "Administrator":
		return True

	# `frappe.get_roles()` includes the automatic roles (All, Guest, Desk User),
	# and "All" carries desk access, so every user would look like a Desk user.
	# Only roles explicitly granted to the user count.
	assigned = [role for role in roles if role not in AUTOMATIC_ROLES]

	if not assigned:
		return False

	return bool(
		frappe.db.exists(
			"Role",
			{"name": ("in", assigned), "desk_access": 1, "disabled": 0},
		)
	)


def normalise_language(language: str) -> str:
	"""Reduce a locale such as `ar-QA` to the catalogue key `ar`."""
	return (language or "en").replace("_", "-").split("-")[0].lower()
