"""
Seed one Optimization Rule document per built-in rule registered in
autoshift.optimizer.rules (constraints and objective terms alike), bundle them
into the "Standard Ruleset" (the default of Optimizer Run.ruleset), and backfill
the ruleset onto existing runs — those were solved with exactly these hardcoded
rules.

Runs as a patch on migrate (sites installed before this existed) AND from the
after_install hook (fresh installs mark patches as completed without running
them) — hence everything here must stay idempotent. The add_objective_rules
patch re-invokes execute() on already-migrated sites so the objective rules
introduced later get seeded and appended to the Standard Ruleset too.
"""

import frappe

STANDARD_RULESET = "Standard Ruleset"


def _legacy_turnover_weight() -> float:
	"""Old Optimizer Settings.turnover_weight (field removed; the tabSingles row survives)."""
	rows = frappe.db.sql(
		"select `value` from `tabSingles` where `doctype`=%s and `field`=%s",
		("Optimizer Settings", "turnover_weight"),
	)
	try:
		return float(rows[0][0]) if rows and rows[0][0] is not None else 1.0
	except (TypeError, ValueError):
		return 1.0


def execute():
	from autoshift.optimizer.rules import BUILTIN_RULES

	rule_doc_names = {}
	for key, rule in BUILTIN_RULES.items():
		existing = frappe.db.get_value("Optimization Rule", {"builtin_key": key})
		if existing:
			rule_doc_names[key] = existing
			# sync kind for docs created before rule_kind existed
			frappe.db.set_value(
				"Optimization Rule", existing, "rule_kind", rule.kind.title(), update_modified=False
			)
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
		rule_doc_names[key] = doc.name

	# The room-utilization row's weight replaces the removed global turnover_weight
	# (BREAKING: other rulesets keep their rows' weights; only Standard is managed here).
	row_weight = {"room_utilization_objective": _legacy_turnover_weight()}

	if not frappe.db.exists("Optimization Ruleset", STANDARD_RULESET):
		frappe.get_doc(
			{
				"doctype": "Optimization Ruleset",
				"ruleset_name": STANDARD_RULESET,
				"description": "All built-in rules — the constraints and objective every run "
				"applied before rulesets existed.",
				"rules": [
					{"rule": name, "weight": row_weight.get(key, 1.0)} for key, name in rule_doc_names.items()
				],
			}
		).insert(ignore_permissions=True)
	else:
		# append built-ins introduced after the ruleset was created (e.g. the
		# objective rules) so its behaviour keeps matching the pre-refactor solver
		standard = frappe.get_doc("Optimization Ruleset", STANDARD_RULESET)
		present = {row.rule for row in standard.rules}
		changed = False
		for key, name in rule_doc_names.items():
			if name not in present:
				standard.append("rules", {"rule": name, "weight": row_weight.get(key, 1.0)})
				changed = True
		if changed:
			standard.save(ignore_permissions=True)

	# Runs from before the ruleset field existed were solved with exactly the
	# built-in rules; record that so they remain loadable.
	frappe.db.set_value(
		"Optimizer Run",
		{"ruleset": ("is", "not set")},
		"ruleset",
		STANDARD_RULESET,
		update_modified=False,
	)
