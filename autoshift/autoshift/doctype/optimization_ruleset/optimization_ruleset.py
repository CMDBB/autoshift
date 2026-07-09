import frappe
from frappe.model.document import Document


class OptimizationRuleset(Document):
	def validate(self):
		self._validate_no_duplicate_rules()
		self._warn_about_unimplemented_rules()
		self._warn_about_objective_composition()

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

	def _warn_about_objective_composition(self):
		"""Objective terms come from rules too: warn on likely mistakes, but allow saving."""
		rule_names = [row.rule for row in (self.rules or []) if row.rule]
		if not rule_names:
			return
		kind_by_rule = {
			rule.name: rule.rule_kind
			for rule in frappe.get_all(
				"Optimization Rule",
				filters={"name": ["in", rule_names]},
				fields=["name", "rule_kind"],
			)
		}

		if not any(kind_by_rule.get(name) in ("Objective", "Mixed") for name in rule_names):
			frappe.msgprint(
				frappe._(
					"This ruleset contains no objective rule: the solver has nothing to maximize "
					"and will return an arbitrary feasible schedule (typically nobody assigned). "
					"Fine for pure feasibility checks; otherwise add an Objective rule."
				),
				indicator="orange",
			)

		weighted_constraints = [
			row.rule
			for row in (self.rules or [])
			if row.rule
			and kind_by_rule.get(row.rule) == "Constraint"
			and row.weight is not None
			and float(row.weight) != 1.0
		]
		if weighted_constraints:
			frappe.msgprint(
				frappe._("Weight has no effect on constraint rules: {0}").format(
					", ".join(frappe.bold(name) for name in weighted_constraints)
				),
				indicator="orange",
			)
