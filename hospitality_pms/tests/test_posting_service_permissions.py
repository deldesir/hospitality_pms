"""The posting-service setup revoked ERPNext's own permissions (16.7.5-R1C).

`setup/posting_service.py` grants the posting identity read on four ERPNext
DocTypes - Sales Invoice, Account, Item and Customer - by inserting a bare
`Custom DocPerm` row for each. Its docstring says this was chosen "so nothing in
ERPNext core is modified and `bench migrate` cannot overwrite it". The first half
is true. The second is the opposite of what happens.

Frappe replaces a DocType's **entire** permission list the moment any Custom
DocPerm exists for it - `frappe/model/meta.py::Meta.set_custom_permissions`:

    def set_custom_permissions(self):
        \"\"\"Reset `permissions` with Custom DocPerm if exists\"\"\"
        ...
        custom_perms = frappe.get_all("Custom DocPerm", ..., filters=dict(parent=self.name))
        if custom_perms:
            self.permissions = [Document(d) for d in custom_perms]

and `frappe/permissions.py::get_all_perms` reads exactly that list. So one
one-permission row does not *add* a grant; it becomes the whole grant set, and
every standard DocPerm on that DocType stops applying to every role.

Measured on this bench before the fix: **122** individual
`(role, permlevel, ptype)` grants destroyed across the four DocTypes. Accounts
Manager and Accounts User lost Sales Invoice and Account outright; Item Manager,
Stock Manager/User, Sales User, Purchase User, Maintenance User and Manufacturing
User lost Item; Sales Master Manager, Sales Manager/User and the Stock roles lost
Customer. Even `All`'s permlevel-1 read on Sales Invoice went.

`install.py` calls `sync_roles()` from **both** `after_install` and
`after_migrate`, so the damage is re-applied by every `bench migrate`.

Frappe's own API does the safe thing, and always did:
`frappe.permissions.add_permission` calls `setup_custom_perms(doctype)` first,
which runs `copy_perms(parent)` to carry the standard rows into Custom DocPerm
**before** the new row joins them.

Two suites here, because they answer different questions:

* `TestCustomDocPermMechanism` proves the mechanism on a purpose-built DocType.
  Deterministic, site-state independent, and it can establish its own premise -
  which a test against Sales Invoice cannot do on a bench where the grants are
  already gone.
* `TestErpPermissionBaseline` asserts the real four DocTypes still carry the
  grants ERPNext shipped, read from the installed `DocPerm` metadata rather than
  from anybody's memory. That is the one that is red on an already-damaged site
  and green once the setup is corrected and the site normalised.
"""

import frappe
from frappe.tests import IntegrationTestCase

from hospitality_pms.setup import posting_service

def ptypes() -> list[str]:
	"""Every permission type a DocPerm row can carry, from the DocType's own meta.

	Read rather than listed. A hand-written tuple is a blind spot by construction:
	`import` and `mask` exist today and were both missing from the first draft of
	this file, so a grant made through either would have been invisible to every
	assertion below - including the one that claims the posting identity holds
	nothing but read.
	"""
	# Only the types both permission tables carry: Custom DocPerm has grown flags of
	# its own (`is_app_disabled` since Frappe's app-disabling work) that `tabDocPerm`
	# never had, and `granted()` reads the same column list from both tables.
	standard = {field.fieldname for field in frappe.get_meta("DocPerm").fields}
	return [
		field.fieldname
		for field in frappe.get_meta("Custom DocPerm").fields
		if field.fieldtype == "Check" and field.fieldname != "if_owner" and field.fieldname in standard
	]


def granted(doctype: str, table: str) -> set[tuple[str, int, int, str]]:
	"""Every `(role, permlevel, if_owner, ptype)` a permission table grants.

	`if_owner` is part of the key, not dropped. Frappe treats
	`(role, permlevel, if_owner)` as the identity of a permission rule, and an
	owner-scoped grant is a different grant from an unrestricted one - so a
	comparison that ignored it could call a standard `if_owner=0` read "still
	present" when it had been replaced by an `if_owner=1` one, which is a
	narrowing, or miss an `if_owner=1` write held by the service identity, which
	is a widening.
	"""
	fields = ptypes()
	rows = frappe.get_all(
		table,
		filters={"parent": doctype},
		fields=["role", "permlevel", "if_owner", *fields],
	)

	return {
		(row["role"], row["permlevel"], int(row.get("if_owner") or 0), ptype)
		for row in rows
		for ptype in fields
		if row.get(ptype)
	}


def effective(doctype: str) -> set[tuple[str, int, str]]:
	"""What the framework will actually enforce, Custom-DocPerm override included.

	Read the same way `Meta.set_custom_permissions` decides it: the custom rows if
	any exist, the standard rows otherwise. Deliberately not via
	`frappe.get_meta`, whose cache would answer from before the test's own writes.
	"""
	if frappe.db.exists("Custom DocPerm", {"parent": doctype}):
		return granted(doctype, "Custom DocPerm")

	return granted(doctype, "DocPerm")


class TestCustomDocPermMechanism(IntegrationTestCase):
	"""A bare Custom DocPerm insert replaces the permission set; `add_permission` does not.

	Run against a DocType created for the purpose. Two reasons that is the right
	call rather than a compromise: the suite can assert its own premise (a real
	DocType on this bench has already lost the grants, so "the standard role has
	the permission" is no longer true there), and nothing it does can touch
	ERPNext's accounting permissions even if it fails half way.
	"""

	DOCTYPE = "HPMS R1C Permission Probe"
	STANDARD_ROLE = "Accounts User"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		if frappe.db.exists("DocType", cls.DOCTYPE):
			frappe.delete_doc("DocType", cls.DOCTYPE, force=True, ignore_permissions=True)

		# A DocType whose standard permissions look like an ERPNext one: a
		# read/write/create grant for an ordinary role, at permlevel 0 and 1.
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": cls.DOCTYPE,
				"module": "Hospitality Setup",
				"custom": 1,
				"is_submittable": 0,
				"fields": [
					{"fieldname": "title", "fieldtype": "Data", "label": "Title"},
					{"fieldname": "secret", "fieldtype": "Data", "label": "Secret", "permlevel": 1},
				],
				"permissions": [
					{
						"role": cls.STANDARD_ROLE,
						"permlevel": 0,
						"read": 1,
						"write": 1,
						"create": 1,
						"report": 1,
					},
					{"role": cls.STANDARD_ROLE, "permlevel": 1, "read": 1},
					{"role": "Accounts Manager", "permlevel": 0, "read": 1, "write": 1},
				],
			}
		).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")

		for name in frappe.get_all(
			"Custom DocPerm", filters={"parent": cls.DOCTYPE}, pluck="name"
		):
			frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)

		if frappe.db.exists("DocType", cls.DOCTYPE):
			frappe.delete_doc("DocType", cls.DOCTYPE, force=True, ignore_permissions=True)

		# `delete_doc` on a DocType does not always drop its table (16.7.5-R1D).
		# Without this the probe leaves a `tabHPMS ...` orphan on every site the
		# suite runs against. Only ever this table: the name is a class constant.
		frappe.db.sql_ddl(f"drop table if exists `tab{cls.DOCTYPE}`")

		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self._clear_custom()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clear_custom()

	def _clear_custom(self):
		for name in frappe.get_all(
			"Custom DocPerm", filters={"parent": self.DOCTYPE}, pluck="name"
		):
			frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)

		frappe.clear_cache(doctype=self.DOCTYPE)
		frappe.db.commit()

	def _insert_bare_custom_docperm(self, role: str):
		"""Exactly what `_ensure_permissions` used to do, verbatim in shape."""
		perm = frappe.new_doc("Custom DocPerm")
		perm.parent = self.DOCTYPE
		perm.parenttype = "DocType"
		perm.parentfield = "permissions"
		perm.role = role
		perm.permlevel = 0
		perm.read = 1

		for off in ("write", "create", "delete", "submit", "cancel", "amend", "report", "export", "share", "print", "email"):
			setattr(perm, off, 0)

		perm.insert(ignore_permissions=True)
		frappe.clear_cache(doctype=self.DOCTYPE)
		frappe.db.commit()

	# -- the premise -------------------------------------------------------

	def test_the_probe_doctype_starts_with_standard_grants(self):
		"""Without this the two tests below could pass by measuring nothing."""
		standard = granted(self.DOCTYPE, "DocPerm")

		self.assertIn((self.STANDARD_ROLE, 0, 0, "read"), standard)
		self.assertIn((self.STANDARD_ROLE, 0, 0, "write"), standard)
		self.assertIn((self.STANDARD_ROLE, 1, 0, "read"), standard)
		self.assertIn(("Accounts Manager", 0, 0, "write"), standard)

		self.assertFalse(
			frappe.db.exists("Custom DocPerm", {"parent": self.DOCTYPE}),
			msg="the probe already has custom permissions, so it proves nothing",
		)

		# And with no custom rows, the effective set *is* the standard set.
		self.assertEqual(effective(self.DOCTYPE), standard)

	# -- the defect --------------------------------------------------------

	def test_a_bare_custom_docperm_insert_destroys_the_standard_grants(self):
		"""The reproduction. One added row, every other grant gone."""
		standard = granted(self.DOCTYPE, "DocPerm")

		self._insert_bare_custom_docperm(posting_service.POSTING_SERVICE_ROLE)

		now = effective(self.DOCTYPE)

		# The service role got what it asked for.
		self.assertIn((posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"), now)

		# And took everything else with it.
		lost = standard - now

		self.assertTrue(
			lost,
			msg="the bare insert did not clobber anything - has Frappe's override changed?",
		)
		self.assertNotIn((self.STANDARD_ROLE, 0, 0, "read"), now)
		self.assertNotIn((self.STANDARD_ROLE, 0, 0, "write"), now)
		self.assertNotIn((self.STANDARD_ROLE, 1, 0, "read"), now)
		self.assertNotIn(("Accounts Manager", 0, 0, "write"), now)

		# Every single standard grant, not merely some.
		self.assertEqual(lost, standard)

	# -- the fix -----------------------------------------------------------

	def test_add_permission_preserves_the_standard_grants(self):
		"""Frappe's own API copies the standard rows forward first.

		`add_permission` -> `setup_custom_perms` -> `copy_perms`, which inserts a
		Custom DocPerm for every existing DocPerm before the new row is added. This
		is the behaviour the fix relies on, so it is pinned rather than assumed.
		"""
		from frappe.permissions import add_permission

		standard = granted(self.DOCTYPE, "DocPerm")

		add_permission(self.DOCTYPE, posting_service.POSTING_SERVICE_ROLE, 0, "read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		now = effective(self.DOCTYPE)

		self.assertIn((posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"), now)

		# Nothing lost, at any permlevel, for any role.
		self.assertEqual(
			standard - now,
			set(),
			msg=f"add_permission lost grants: {sorted(standard - now)}",
		)

	def test_the_corrected_setup_preserves_the_standard_grants(self):
		"""The same assertion, driven through our own function.

		`test_add_permission_preserves...` pins the framework; this pins *us*. A
		future edit that goes back to a bare insert fails here.
		"""
		standard = granted(self.DOCTYPE, "DocPerm")

		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		now = effective(self.DOCTYPE)

		self.assertIn((posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"), now)
		self.assertEqual(standard - now, set())

	def test_the_corrected_setup_repairs_an_already_clobbered_doctype(self):
		"""The state this bench is already in has to be recoverable.

		A site that has migrated with the old code has lost the standard rows
		*before* the fix ever runs, so a fix that only stops causing new damage
		would leave every existing bench broken. The sole Custom DocPerm on the
		parent is ours, which is what makes restoring safe - see
		`_restore_standard_permissions`.
		"""
		standard = granted(self.DOCTYPE, "DocPerm")

		# Break it exactly as the old code did.
		self._insert_bare_custom_docperm(posting_service.POSTING_SERVICE_ROLE)
		self.assertEqual(effective(self.DOCTYPE) & standard, set())

		# Then run the corrected setup over the damage, in repair mode - which is
		# what the one-shot patch does, and deliberately not what `after_migrate`
		# does. See `grant_service_permission`.
		posting_service.grant_service_permission(
			self.DOCTYPE, permlevel=0, ptype="read", repair=True
		)
		frappe.clear_cache(doctype=self.DOCTYPE)

		now = effective(self.DOCTYPE)

		self.assertEqual(
			standard - now,
			set(),
			msg=f"repair left grants missing: {sorted(standard - now)}",
		)
		self.assertIn((posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"), now)

	def test_repair_leaves_another_roles_custom_permission_alone(self):
		"""Operator customisation is not ours to normalise away.

		If a Custom DocPerm exists for a role that is *not* the posting service,
		the row set is somebody's deliberate configuration - class B or C in the
		provenance split - and the repair must not touch it or invent standard rows
		beside it. Our own grant is still ensured.
		"""
		self._insert_bare_custom_docperm("Accounts Manager")

		before = granted(self.DOCTYPE, "Custom DocPerm")

		posting_service.grant_service_permission(
			self.DOCTYPE, permlevel=0, ptype="read", repair=True
		)
		frappe.clear_cache(doctype=self.DOCTYPE)

		after = granted(self.DOCTYPE, "Custom DocPerm")

		# The operator's row survives untouched.
		self.assertTrue(before.issubset(after))

		# Ours is present.
		self.assertIn((posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"), after)

		# And nothing was resurrected around it: the standard rows stay out,
		# because this configuration was chosen rather than damaged.
		self.assertNotIn((self.STANDARD_ROLE, 0, 0, "write"), after)

	def test_an_owner_scoped_row_for_the_service_role_is_narrowed_too(self):
		"""`if_owner=1` would be write on everything the identity creates.

		`(role, permlevel, if_owner)` is the identity of a permission rule, so a row
		with `if_owner=1` sits *beside* the ordinary one rather than replacing it.
		The first version of the narrowing filtered on `if_owner=0` and could not
		see such a row at all: `add_permission` would create the ordinary row next to
		it and report least privilege while the identity held write on every document
		it owns - which, since it is the identity that posts them, is all of them.
		"""
		perm = frappe.new_doc("Custom DocPerm")
		perm.parent = self.DOCTYPE
		perm.parenttype = "DocType"
		perm.parentfield = "permissions"
		perm.role = posting_service.POSTING_SERVICE_ROLE
		perm.permlevel = 0
		perm.if_owner = 1
		perm.read = 1
		perm.write = 1
		perm.insert(ignore_permissions=True)
		frappe.db.commit()

		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		held = {
			(permlevel, if_owner, ptype)
			for (role, permlevel, if_owner, ptype) in granted(self.DOCTYPE, "Custom DocPerm")
			if role == posting_service.POSTING_SERVICE_ROLE
		}

		self.assertEqual(
			held,
			{(0, 0, "read")},
			msg=f"the service identity holds more than one unrestricted read: {sorted(held)}",
		)

	def test_a_row_at_an_undeclared_permlevel_is_stripped(self):
		"""Least privilege covers permlevels this app never asked for."""
		perm = frappe.new_doc("Custom DocPerm")
		perm.parent = self.DOCTYPE
		perm.parenttype = "DocType"
		perm.parentfield = "permissions"
		perm.role = posting_service.POSTING_SERVICE_ROLE
		perm.permlevel = 1
		perm.read = 1
		perm.insert(ignore_permissions=True)
		frappe.db.commit()

		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		held = {
			(permlevel, if_owner, ptype)
			for (role, permlevel, if_owner, ptype) in granted(self.DOCTYPE, "Custom DocPerm")
			if role == posting_service.POSTING_SERVICE_ROLE
		}

		self.assertEqual(held, {(0, 0, "read")})

	def test_a_row_whose_read_bit_was_cleared_is_restored(self):
		"""The narrowing asserts the grant, it does not merely remove surplus.

		A hand edit or an aborted older build could leave the identity holding a row
		that grants nothing, and UAT-004's `PermissionError` would come back with no
		trace of why. The earlier version returned early in exactly that case,
		because it only looked for surplus to remove.
		"""
		from frappe.permissions import add_permission

		add_permission(self.DOCTYPE, posting_service.POSTING_SERVICE_ROLE, 0, "read")
		name = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": self.DOCTYPE, "role": posting_service.POSTING_SERVICE_ROLE},
			"name",
		)
		frappe.db.set_value("Custom DocPerm", name, "read", 0, update_modified=False)
		frappe.clear_cache(doctype=self.DOCTYPE)
		frappe.db.commit()

		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		self.assertIn(
			(posting_service.POSTING_SERVICE_ROLE, 0, 0, "read"),
			granted(self.DOCTYPE, "Custom DocPerm"),
			msg="the identity was left holding a row that grants nothing",
		)

	def test_duplicate_standard_rows_are_copied_across_only_once(self):
		"""The repair must not compound a duplicate it finds in `DocPerm`.

		`DocPerm` is only checked for duplicate `(role, permlevel, if_owner)` triples
		when a DocType is *saved*, so a bad fixture or import can leave two identical
		rows behind. Copying both into `Custom DocPerm` would then make the next
		`add_permission` on that parent throw and abort a migrate.

		Asserted against `_restore_standard_permissions` alone, deliberately. Driving
		the whole of `grant_service_permission` here proves nothing about this guard:
		a duplicated `DocPerm` makes the *DocType* invalid, so
		`validate_permissions_for_doctype` inside `add_permission` throws on the
		duplicate itself whatever the repair did with it. That refusal is Frappe's and
		it is correct; what is ours to get right is not making a second copy.
		"""
		twin = frappe.new_doc("DocPerm")
		twin.update(
			frappe.get_all(
				"DocPerm",
				filters={"parent": self.DOCTYPE, "role": self.STANDARD_ROLE, "permlevel": 0},
				fields="*",
			)[0]
		)
		twin.name = None
		twin.insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			# Our own damage, so the repair is willing to act.
			self._insert_bare_custom_docperm(posting_service.POSTING_SERVICE_ROLE)

			restored = posting_service._restore_standard_permissions(self.DOCTYPE)
			frappe.db.commit()

			self.assertTrue(restored, msg="the repair declined a DocType it should have fixed")

			self.assertEqual(
				frappe.db.count(
					"Custom DocPerm",
					{
						"parent": self.DOCTYPE,
						"role": self.STANDARD_ROLE,
						"permlevel": 0,
						"if_owner": 0,
					},
				),
				1,
				msg="the duplicated standard row was copied across twice",
			)
		finally:
			# Removed here rather than at the end of the body: if an assertion above
			# fails, a duplicated DocPerm left on the probe makes every later test in
			# this class fail as collateral.
			frappe.delete_doc("DocPerm", twin.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_the_repair_does_not_run_unless_it_is_asked_for(self):
		"""`after_migrate` must not re-widen a deliberately narrowed DocType.

		The repair recognises our damage by its fingerprint - every custom row
		belongs to the posting service. An operator who narrows one of these
		DocTypes down to only the posting service produces the same fingerprint, so
		a repair on every migrate would undo their decision for ever. It runs from a
		one-shot patch instead, and `repair` defaults to off.
		"""
		standard = granted(self.DOCTYPE, "DocPerm")

		self._insert_bare_custom_docperm(posting_service.POSTING_SERVICE_ROLE)

		# The ordinary migrate path: grant, and leave the permission set alone.
		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)

		self.assertEqual(
			granted(self.DOCTYPE, "Custom DocPerm") & standard,
			set(),
			msg="the migrate path restored standard grants it was not asked to restore",
		)

		# And the patch path repairs it.
		posting_service.grant_service_permission(
			self.DOCTYPE, permlevel=0, ptype="read", repair=True
		)
		frappe.clear_cache(doctype=self.DOCTYPE)

		self.assertEqual(standard - granted(self.DOCTYPE, "Custom DocPerm"), set())

	# -- idempotency -------------------------------------------------------

	def test_running_the_setup_three_times_changes_nothing_after_the_first(self):
		"""`after_migrate` runs this on every migrate."""
		standard = granted(self.DOCTYPE, "DocPerm")

		posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
		frappe.clear_cache(doctype=self.DOCTYPE)
		first = granted(self.DOCTYPE, "Custom DocPerm")
		first_count = frappe.db.count("Custom DocPerm", {"parent": self.DOCTYPE})

		for _ in range(2):
			posting_service.grant_service_permission(self.DOCTYPE, permlevel=0, ptype="read")
			frappe.clear_cache(doctype=self.DOCTYPE)

		self.assertEqual(granted(self.DOCTYPE, "Custom DocPerm"), first)
		self.assertEqual(
			frappe.db.count("Custom DocPerm", {"parent": self.DOCTYPE}),
			first_count,
			msg="repeated runs duplicated Custom DocPerm rows",
		)
		self.assertEqual(standard - effective(self.DOCTYPE), set())


class TestErpPermissionBaseline(IntegrationTestCase):
	"""The four real DocTypes still grant what ERPNext shipped.

	Expected values are read from the installed `DocPerm` metadata, never written
	down here: this bench's ERPNext is the authority on its own permissions, and a
	hand-copied list would be wrong the first time ERPNext changed.

	Red on a site that has already migrated with the old code. That is the point -
	it is the check that says whether *this* bench still needs normalising.
	"""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_the_service_role_holds_exactly_its_declared_grants(self):
		"""Least privilege, asserted as an equality rather than a subset."""
		for declared in posting_service.SERVICE_PERMISSIONS:
			doctype = declared["doctype"]

			held = {
				(permlevel, if_owner, ptype)
				for (role, permlevel, if_owner, ptype) in effective(doctype)
				if role == posting_service.POSTING_SERVICE_ROLE
			}

			self.assertEqual(
				held,
				{(declared["permlevel"], 0, "read")},
				msg=f"the posting service holds more than read on {doctype}: {sorted(held)}",
			)

	def test_no_standard_erpnext_grant_was_lost_on_any_affected_doctype(self):
		"""The 122-grant regression, as one assertion per DocType."""
		for declared in posting_service.SERVICE_PERMISSIONS:
			doctype = declared["doctype"]

			with self.subTest(doctype=doctype):
				standard = granted(doctype, "DocPerm")
				now = effective(doctype)
				lost = standard - now

				self.assertEqual(
					lost,
					set(),
					msg=(
						f"{doctype}: {len(lost)} standard ERPNext grants are not in "
						f"effect. Sample: {sorted(lost)[:6]}"
					),
				)

	def test_the_accounting_roles_still_read_their_own_doctypes(self):
		"""Named explicitly, because these are the roles a hotel's finance team uses.

		Skipped per role that this bench does not have, so the test reports a
		missing role rather than passing over it silently.
		"""
		expected = [
			("Accounts Manager", "Sales Invoice"),
			("Accounts User", "Sales Invoice"),
			("Accounts Manager", "Account"),
			("Accounts User", "Account"),
			("Accounts Manager", "Customer"),
			("Accounts User", "Item"),
		]

		for role, doctype in expected:
			with self.subTest(role=role, doctype=doctype):
				if not frappe.db.exists("Role", role):
					self.skipTest(f"{role} is not installed on this bench")

				# Only assert what ERPNext itself grants: if a future ERPNext drops
				# the grant, this test should follow it rather than fail.
				if (role, 0, 0, "read") not in granted(doctype, "DocPerm"):
					self.skipTest(f"ERPNext does not grant {role} read on {doctype} here")

				self.assertIn(
					(role, 0, 0, "read"),
					effective(doctype),
					msg=f"{role} lost read on {doctype}",
				)

	def test_the_stock_and_sales_roles_still_read_item_and_customer(self):
		expected = [
			("Stock User", "Item"),
			("Stock Manager", "Item"),
			("Item Manager", "Item"),
			("Sales User", "Item"),
			("Purchase User", "Item"),
			("Manufacturing User", "Item"),
			("Sales User", "Customer"),
			("Sales Manager", "Customer"),
			("Sales Master Manager", "Customer"),
			("Stock User", "Customer"),
			("Stock Manager", "Customer"),
		]

		for role, doctype in expected:
			with self.subTest(role=role, doctype=doctype):
				if not frappe.db.exists("Role", role):
					self.skipTest(f"{role} is not installed on this bench")

				if (role, 0, 0, "read") not in granted(doctype, "DocPerm"):
					self.skipTest(f"ERPNext does not grant {role} read on {doctype} here")

				self.assertIn(
					(role, 0, 0, "read"),
					effective(doctype),
					msg=f"{role} lost read on {doctype}",
				)


class TestPostingServiceIdentity(IntegrationTestCase):
	"""The identity stays dedicated, non-interactive and least privilege."""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_the_role_carries_no_desk_access(self):
		self.assertTrue(frappe.db.exists("Role", posting_service.POSTING_SERVICE_ROLE))
		self.assertEqual(
			int(frappe.db.get_value("Role", posting_service.POSTING_SERVICE_ROLE, "desk_access") or 0),
			0,
		)

	def test_the_user_holds_that_role_and_nothing_else(self):
		if not frappe.db.exists("User", posting_service.POSTING_SERVICE_USER):
			self.skipTest("the posting service user is not set up on this bench")

		roles = set(
			frappe.get_all(
				"Has Role",
				filters={"parent": posting_service.POSTING_SERVICE_USER, "parenttype": "User"},
				pluck="role",
			)
		)

		self.assertEqual(roles, {posting_service.POSTING_SERVICE_ROLE})

	def test_the_identity_is_not_a_system_manager_or_administrator(self):
		if not frappe.db.exists("User", posting_service.POSTING_SERVICE_USER):
			self.skipTest("the posting service user is not set up on this bench")

		frappe.set_user(posting_service.POSTING_SERVICE_USER)

		try:
			held = set(frappe.get_roles())

			for forbidden in ("System Manager", "Administrator", "Accounts Manager"):
				self.assertNotIn(forbidden, held)

			# And no operational hospitality role.
			self.assertFalse(
				{role for role in held if role.startswith(("Front Office", "Housekeeping", "Hospitality Admin"))},
				msg=f"the posting identity holds an operational role: {sorted(held)}",
			)
		finally:
			frappe.set_user("Administrator")

	def test_the_identity_cannot_write_the_doctypes_it_reads(self):
		"""Read-only is the whole grant. No create, write, submit or delete."""
		for declared in posting_service.SERVICE_PERMISSIONS:
			doctype = declared["doctype"]

			with self.subTest(doctype=doctype):
				held = {
					ptype
					for (role, _lvl, _owner, ptype) in effective(doctype)
					if role == posting_service.POSTING_SERVICE_ROLE
				}

				for forbidden in ("write", "create", "delete", "submit", "cancel", "amend"):
					self.assertNotIn(forbidden, held, msg=f"{doctype}: holds {forbidden}")

	def test_the_identity_reads_nothing_beyond_the_declared_four(self):
		"""No grant accumulates on any other DocType."""
		granted_parents = set(
			frappe.get_all(
				"Custom DocPerm",
				filters={"role": posting_service.POSTING_SERVICE_ROLE},
				pluck="parent",
			)
		) | set(
			frappe.get_all(
				"DocPerm",
				filters={"role": posting_service.POSTING_SERVICE_ROLE},
				pluck="parent",
			)
		)

		declared = {row["doctype"] for row in posting_service.SERVICE_PERMISSIONS}

		self.assertEqual(
			granted_parents - declared,
			set(),
			msg=f"the posting service gained permissions on {sorted(granted_parents - declared)}",
		)
