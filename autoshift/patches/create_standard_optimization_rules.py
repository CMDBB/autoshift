"""
Seed one Optimization Rule document per built-in rule registered in
autoshift.optimizer.rules, bundle them into the "Standard Ruleset" (the default
of Optimizer Run.ruleset), and backfill the ruleset onto existing runs — those
were solved with exactly these hardcoded rules.

Runs as a patch on migrate (sites installed before this existed) AND from the
after_install hook (fresh installs mark patches as completed without running
them) — hence everything here must stay idempotent.
"""

import frappe

STANDARD_RULESET = "Standard Ruleset"


def execute():
	from autoshift.optimizer.rules import BUILTIN_RULES

	rule_doc_names = []
	for key, rule in BUILTIN_RULES.items():
		existing = frappe.db.get_value("Optimization Rule", {"builtin_key": key})
		if existing:
			rule_doc_names.append(existing)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Optimization Rule",
				"rule_name": rule.title,
				"description": rule.description,
				"implementation_type": "Built-in",
				"builtin_key": key,
			}
		)
		doc.insert(ignore_permissions=True)
		rule_doc_names.append(doc.name)

	if not frappe.db.exists("Optimization Ruleset", STANDARD_RULESET):
		frappe.get_doc(
			{
				"doctype": "Optimization Ruleset",
				"ruleset_name": STANDARD_RULESET,
				"description": "All built-in rules — the constraints every run applied "
				"before rulesets existed.",
				"rules": [{"rule": name} for name in rule_doc_names],
			}
		).insert(ignore_permissions=True)

	# Runs from before the ruleset field existed were solved with exactly the
	# built-in rules; record that so they remain loadable.
	frappe.db.set_value(
		"Optimizer Run",
		{"ruleset": ("is", "not set")},
		"ruleset",
		STANDARD_RULESET,
		update_modified=False,
	)
