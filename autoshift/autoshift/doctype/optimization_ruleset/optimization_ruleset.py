import frappe
from frappe.model.document import Document


class OptimizationRuleset(Document):
	def validate(self):
		self._validate_no_duplicate_rules()
		self._warn_about_unimplemented_rules()

	def _validate_no_duplicate_rules(self):
		seen = set()
		for row in self.rules or []:
			if row.rule in seen:
				frappe.throw(frappe._("Rule {0} appears more than once.").format(frappe.bold(row.rule)))
			seen.add(row.rule)

	def _warn_about_unimplemented_rules(self):
		"""Unimplemented rules may be drafted into a ruleset, but block solving."""
		rule_names = [row.rule for row in (self.rules or []) if row.rule]
		if not rule_names:
			return
		unimplemented = [
			rule.name
			for rule in frappe.get_all(
				"Optimization Rule",
				filters={"name": ["in", rule_names], "implemented": 0},
				fields=["name"],
			)
		]
		if unimplemented:
			frappe.msgprint(
				frappe._(
					"The following rules are not implemented yet; an Optimizer Run using this "
					"ruleset will refuse to solve until they are: {0}"
				).format(", ".join(frappe.bold(name) for name in unimplemented)),
				indicator="orange",
			)
