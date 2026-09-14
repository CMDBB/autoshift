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

STANDARD_RULESET_NAME = "Standard Ruleset"


def _seed_topics(topic_order, topic_descriptions) -> None:
	"""
	Upsert one Scheduling Rule Topic per topic the built-in registry declares.

	Only app-curated (``is_system``) topics are re-synced or removed; a topic somebody
	created by hand for their Custom Code rules is left entirely alone.
	"""
	curated = {
		doc["name"]: doc["name"]
		for doc in frappe.get_all("Scheduling Rule Topic", filters={"is_system": 1}, fields=["name"])
	}
	for name in set(curated) - set(topic_order):
		frappe.log(f"Deleting unsupported leftover rule topic {name}")
		frappe.delete_doc("Scheduling Rule Topic", name, force=True)
	for order, name in enumerate(topic_order):
		values = {
			"doctype": "Scheduling Rule Topic",
			"topic_name": name,
			"description": topic_descriptions.get(name, ""),
			"display_order": order,
			"is_system": 1,
		}
		if frappe.db.exists("Scheduling Rule Topic", name):
			frappe.get_doc("Scheduling Rule Topic", name).update(values).save(ignore_permissions=True)
		else:
			frappe.new_doc(**values).insert(ignore_permissions=True)


def execute():
	from autoshift.optimizer.rules import (
		BUILTIN_RULES,
		STANDARD_RULES,
		TOPIC_DESCRIPTIONS,
		TOPIC_ORDER,
	)

	# before the rules: Optimization Rule.topic links here, so the targets must exist
	_seed_topics(TOPIC_ORDER, TOPIC_DESCRIPTIONS)

	rule_doc_names = {}
	existing = {
		doc["builtin_key"]: doc["name"]
		for doc in frappe.get_all(
			"Optimization Rule", filters={"implementation_type": "Built-in"}, fields=["name", "builtin_key"]
		)
	}
	for key in set(existing) - set(BUILTIN_RULES):
		frappe.log(f"Deleting unsupported leftover rule {key}")
		frappe.delete_doc("Optimization Rule", existing[key], force=True)
	for key, rule in BUILTIN_RULES.items():
		rule = {
			"doctype": "Optimization Rule",
			"rule_name": rule.title,
			"description": rule.description,
			"implementation_type": "Built-in",
			"builtin_key": key,
			"rule_kind": rule.kind,
			"topic": rule.topic,
		}
		if key in existing:
			doc = (
				frappe.get_doc("Optimization Rule", existing[key]).update(rule).save(ignore_permissions=True)
			)
		else:
			doc = frappe.new_doc(**rule).insert(ignore_permissions=True)

		rule_doc_names[key] = doc.name

	if not frappe.db.exists("Optimization Ruleset", STANDARD_RULESET_NAME):
		frappe.get_doc(
			{
				"doctype": "Optimization Ruleset",
				"ruleset_name": STANDARD_RULESET_NAME,
				"description": "A minimal set of built-in rules that is guaranteed to be feasible for any problem.",
				"is_system": 1,
				"rules": [
					{"rule": name, "weight": BUILTIN_RULES[key].default_weight}
					for key, name in rule_doc_names.items()
					if key in STANDARD_RULES
				],
			}
		).insert(ignore_permissions=True)
	else:
		# Compare the rows' *rule* links against the rule documents the standard built-ins
		# resolved to. Comparing `row.name` (a child-row hash) against builtin keys, as this
		# once did, can never match — which rebuilt the ruleset on every migrate and silently
		# reset every hand-tuned weight to 1.0.
		standard = frappe.get_doc("Optimization Ruleset", STANDARD_RULESET_NAME)
		wanted = {name for key, name in rule_doc_names.items() if key in STANDARD_RULES and name}
		needs_save = {row.rule for row in standard.rules} != wanted
		if not standard.is_system:
			standard.is_system = 1
			needs_save = True
		if needs_save:
			# Preserve the weights of rows that survive; only genuinely new rules take the
			# rule's declared default.
			weights = {row.rule: row.weight for row in standard.rules}
			standard.rules = []
			for key, name in rule_doc_names.items():
				if key in STANDARD_RULES and name:
					standard.append(
						"rules",
						{"rule": name, "weight": weights.get(name, BUILTIN_RULES[key].default_weight)},
					)
			standard.save(ignore_permissions=True)

	# Runs from before the ruleset field existed were solved with exactly the
	# built-in rules; record that so they remain loadable.
	frappe.db.set_value(
		"Optimizer Run",
		{"ruleset": ("is", "not set")},
		"ruleset",
		STANDARD_RULESET_NAME,
		update_modified=False,
	)
