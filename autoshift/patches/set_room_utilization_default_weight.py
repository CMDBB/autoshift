"""
Raise the Standard Ruleset's room-utilization weight from 1.0 to the rule's declared
default of 3.0.

At weight 1 that rule cannot outbid the per-assignment cost the shift-preference objective
charges: `room_coverage` takes the *minimum* over a discipline's roles, so opening a single
room costs two or more assignments at roughly -0.5 each against a gain of +1, and the
solver mostly declines to schedule anyone (a real 51-employee week solved to 6 of 140
room-slots). Weight 3 also matches the working calibration that one objective point is
loosely ~100 CHF/h.

Only the app-curated Standard Ruleset (`is_system`) is touched, and only where the weight
is still the old default — a hand-tuned figure, there or in any other ruleset, is left
alone. The shared seeding in create_standard_optimization_rules preserves existing weights,
so it would never have applied this on its own.
"""

import frappe

from autoshift.patches.create_standard_optimization_rules import STANDARD_RULESET_NAME

OLD_DEFAULT_WEIGHT = 1.0


def execute():
	from autoshift.optimizer.rules import BUILTIN_RULES

	if not frappe.db.exists("Optimization Ruleset", STANDARD_RULESET_NAME):
		return

	key = "room_utilization_objective"
	rule_name = frappe.db.get_value(
		"Optimization Rule", {"implementation_type": "Built-in", "builtin_key": key}, "name"
	)
	if not rule_name:
		return

	ruleset = frappe.get_doc("Optimization Ruleset", STANDARD_RULESET_NAME)
	if not ruleset.is_system:
		return

	changed = False
	for row in ruleset.rules:
		if row.rule == rule_name and float(row.weight or 0) == OLD_DEFAULT_WEIGHT:
			row.weight = BUILTIN_RULES[key].default_weight
			changed = True

	if changed:
		ruleset.save(ignore_permissions=True)
