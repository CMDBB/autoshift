# Copyright (c) 2026, CMDBB and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]

HR_ONLY_USER = "test-hr-rules@example.com"

CUSTOM_CODE = "def apply(ctx):\n\tpass\n"


class IntegrationTestOptimizationRule(IntegrationTestCase):
	"""
	Locks in the developer-only gate on rule implementation/validation.

	Permlevel 1 makes the implementation fields read-only in the UI for everyone
	but System Manager; the controller's validate_higher_perm_levels override is
	the authoritative server-side guard (it also covers API writes, and unlike the
	framework's silent reset it warns / refuses). These tests exercise the guard
	directly through Document.save() as an HR-Manager-only user.
	"""

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", HR_ONLY_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": HR_ONLY_USER,
					"first_name": "HR Rules Test",
					"send_welcome_email": 0,
					"roles": [{"role": "HR Manager"}],
				}
			).insert(ignore_permissions=True)
		self._created_rules = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self._created_rules:
			frappe.delete_doc("Optimization Rule", name, force=True, ignore_missing=True)
		super().tearDown()

	def _insert_rule(self, **fields):
		doc = frappe.get_doc({"doctype": "Optimization Rule", "description": "test rule", **fields})
		doc.insert()
		self._created_rules.append(doc.name)
		return doc

	def _make_builtin_rule(self):
		"""A developer-implemented rule, created as Administrator."""
		frappe.set_user("Administrator")
		return self._insert_rule(
			rule_name="TEST builtin rule",
			implementation_type="Built-in",
			builtin_key="fte_ceiling",
		)

	def _make_custom_rule(self, validated=0):
		frappe.set_user("Administrator")
		return self._insert_rule(
			rule_name="TEST custom rule",
			implementation_type="Custom Code",
			implementation_code=CUSTOM_CODE,
			validated=validated,
		)

	# ── HR Manager (non-developer) ────────────────────────────────────────────

	def test_hr_can_create_description_only_rule(self):
		frappe.set_user(HR_ONLY_USER)
		rule = self._insert_rule(rule_name="TEST hr nl rule")
		self.assertEqual(rule.implementation_type, "Not Implemented")
		self.assertEqual(rule.implemented, 0)

	def test_hr_implementation_attempt_is_cleaned_on_create(self):
		frappe.set_user(HR_ONLY_USER)
		frappe.clear_messages()
		rule = self._insert_rule(
			rule_name="TEST hr sneaky rule",
			implementation_type="Custom Code",
			implementation_code=CUSTOM_CODE,
		)
		self.assertEqual(rule.implementation_type, "Not Implemented")
		self.assertFalse(rule.implementation_code)
		self.assertEqual(rule.implemented, 0)
		self.assertTrue(
			any("Not Implemented" in str(m.get("message")) for m in frappe.get_message_log()),
			"expected a cleanup warning",
		)

	def test_hr_cannot_validate_on_create(self):
		frappe.set_user(HR_ONLY_USER)
		with self.assertRaises(frappe.PermissionError):
			self._insert_rule(
				rule_name="TEST hr validated rule",
				implementation_type="Custom Code",
				implementation_code=CUSTOM_CODE,
				validated=1,
			)

	def test_hr_cannot_flip_validated(self):
		rule = self._make_custom_rule()
		frappe.set_user(HR_ONLY_USER)
		rule = frappe.get_doc("Optimization Rule", rule.name)
		rule.validated = 1
		with self.assertRaises(frappe.PermissionError):
			rule.save()

	def test_hr_description_edit_preserves_implementation(self):
		rule = self._make_builtin_rule()
		frappe.set_user(HR_ONLY_USER)
		rule = frappe.get_doc("Optimization Rule", rule.name)
		rule.description = "clarified by HR"
		rule.save()
		self.assertEqual(rule.description, "clarified by HR")
		self.assertEqual(rule.implementation_type, "Built-in")
		self.assertEqual(rule.builtin_key, "fte_ceiling")
		self.assertEqual(rule.implemented, 1)

	def test_hr_rule_kind_change_is_reverted(self):
		frappe.set_user(HR_ONLY_USER)
		rule = self._insert_rule(rule_name="TEST hr kind rule")
		rule = frappe.get_doc("Optimization Rule", rule.name)
		rule.rule_kind = "Objective"
		rule.save()
		self.assertEqual(rule.rule_kind, "Constraint")

	def test_hr_implementation_change_is_reverted(self):
		rule = self._make_builtin_rule()
		frappe.set_user(HR_ONLY_USER)
		frappe.clear_messages()
		rule = frappe.get_doc("Optimization Rule", rule.name)
		rule.implementation_type = "Custom Code"
		rule.implementation_code = CUSTOM_CODE
		rule.save()
		self.assertEqual(rule.implementation_type, "Built-in")
		self.assertEqual(rule.builtin_key, "fte_ceiling")
		self.assertFalse(rule.implementation_code)
		self.assertTrue(
			any("reverted" in str(m.get("message")) for m in frappe.get_message_log()),
			"expected a revert warning",
		)

	# ── System Manager (developer) ────────────────────────────────────────────

	def test_developer_can_implement_and_validate(self):
		rule = self._make_custom_rule(validated=1)
		self.assertEqual(rule.implemented, 1)

	def test_rule_kind_synced_from_registry(self):
		constraint_rule = self._make_builtin_rule()  # fte_ceiling
		self.assertEqual(constraint_rule.rule_kind, "Constraint")
		frappe.set_user("Administrator")
		objective_rule = self._insert_rule(
			rule_name="TEST objective builtin",
			implementation_type="Built-in",
			builtin_key="room_utilization_objective",
		)
		self.assertEqual(objective_rule.rule_kind, "Objective")

	def test_editing_code_clears_validated(self):
		rule = self._make_custom_rule(validated=1)
		rule.implementation_code = CUSTOM_CODE + "\n# changed\n"
		rule.save()
		self.assertEqual(rule.validated, 0)
		self.assertEqual(rule.implemented, 0)

	def test_builtin_key_must_be_registered(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			self._insert_rule(
				rule_name="TEST bogus builtin",
				implementation_type="Built-in",
				builtin_key="no_such_rule",
			)
