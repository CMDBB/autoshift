"""
Optimizer Studio: a workspace-level abstraction over Optimizer Run + Optimization
Ruleset, backing the "Optimizer Studio" page. Lets a user pick Planning Mode / Start
Date / a human-readable set of rule toggles without hand-editing a ruleset, preview the
resulting schedule, and hand off to a real Optimizer Run (created with
``type="Automatic"``) to Approve/Commit through the existing flow — this module doesn't
reimplement any of that, it only assembles the inputs and calls into it.

Every preview writes into one ruleset per user (``Studio Draft — <user>``) rather than
minting a new Ruleset document on every click. Toggling never edits an ``is_system``
(app-curated) ruleset in place: the draft *is* the duplicate, created lazily on first
preview. ``Optimization Ruleset.validate()`` already enforces built-in rule compatibility
(choice groups, requires/excludes — see ``rules.BuiltinRule.check_ruleset``) and warns
about unimplemented rules or a missing objective, so ``_save_draft_ruleset`` doesn't
duplicate any of that; a bad toggle combination surfaces as the same error a hand-edited
ruleset would raise on save.
"""

import json

import frappe

from autoshift.optimizer.rules import BUILTIN_RULES

DRAFT_RULESET_PREFIX = "Studio Draft"


def _draft_ruleset_name(user: str | None = None) -> str:
	return f"{DRAFT_RULESET_PREFIX} — {user or frappe.session.user}"


@frappe.whitelist()
def get_rule_catalog() -> list[dict]:
	"""Every usable Optimization Rule, annotated with the built-in dependency-graph
	metadata (choice group, requires, excludes) needed to render Studio's toggle panel,
	plus the topic each rule is filed under so the panel can be sectioned.
	Custom Code rules carry none of the dependency metadata and are listed as plain
	standalone toggles; they may still name a topic.
	"""
	rules = frappe.get_all(
		"Optimization Rule",
		filters={"implemented": 1},
		fields=[
			"name",
			"rule_name",
			"description",
			"rule_kind",
			"implementation_type",
			"builtin_key",
			"topic",
		],
		order_by="rule_name asc",
	)
	# Topic display order, so Studio's sections read coverage-first rather than
	# alphabetically. Hand-authored topics default to 0 and sort by name among themselves.
	topic_order = {
		t.name: (int(t.display_order or 0), t.name)
		for t in frappe.get_all("Scheduling Rule Topic", fields=["name", "display_order"])
	}
	# builtin_key (rules.py identity) -> Optimization Rule doc name (ruleset row identity),
	# so a built-in's `requires`/`excludes` (keyed by builtin_key) can be reported in terms
	# a ruleset row actually uses.
	key_to_name = {r.builtin_key: r.name for r in rules if r.implementation_type == "Built-in"}

	catalog = []
	for r in rules:
		builtin = BUILTIN_RULES.get(r.builtin_key) if r.implementation_type == "Built-in" else None
		catalog.append(
			{
				"name": r.name,
				"title": r.rule_name,
				"description": r.description,
				"kind": r.rule_kind,
				"group": builtin.group if builtin else None,
				"topic": r.topic or None,
				"topic_order": topic_order.get(r.topic, (0, r.topic or ""))[0],
				"requires": [key_to_name.get(k, k) for k in builtin.requires] if builtin else [],
				"excludes": [key_to_name.get(k, k) for k in builtin.excludes] if builtin else [],
			}
		)
	return catalog


@frappe.whitelist()
def check_binding_rule_gap(rows: str | dict) -> dict:
	"""Would the panel's current selection ignore the site's settled schedules?

	Studio's counterpart to ``OptimizerRun.check_binding_rule_gap``: the selection is not
	a saved ruleset yet, so the rule documents come straight from the toggle panel.
	"""
	from autoshift.optimizer import data_loader

	rows = frappe.parse_json(rows) if isinstance(rows, str) else (rows or {})
	return data_loader.binding_rule_gap(data_loader.builtin_keys_of(list(rows)))


@frappe.whitelist()
def get_ruleset_selection(ruleset: str) -> dict:
	"""Rows of `ruleset` as {rule_name: weight}, plus whether it's app-curated."""
	if not ruleset or not frappe.db.exists("Optimization Ruleset", ruleset):
		return {"rows": {}, "is_system": False}
	rows = frappe.get_all(
		"Optimization Ruleset Rule",
		filters={"parent": ruleset, "parenttype": "Optimization Ruleset"},
		fields=["rule", "weight"],
	)
	is_system = bool(frappe.db.get_value("Optimization Ruleset", ruleset, "is_system"))
	return {
		"rows": {row.rule: (row.weight if row.weight is not None else 1.0) for row in rows},
		"is_system": is_system,
	}


@frappe.whitelist()
def prefill_from_run(run: str) -> dict:
	"""Mode / date / leave speculations / rule selection to seed the panel from an
	existing Optimizer Run — the "populate from an existing run" link-picker.
	"""
	doc = frappe.get_doc("Optimizer Run", run)
	selection = get_ruleset_selection(doc.ruleset)
	return {
		"mode": doc.mode,
		"date": str(doc.date) if doc.date else None,
		"leaves_speculations": [row.leave_application for row in (doc.leaves_speculations or [])],
		"ruleset": doc.ruleset,
		"rows": selection["rows"],
	}


def _save_draft_ruleset(rows: dict) -> str:
	"""Create-or-overwrite the calling user's draft ruleset with exactly `rows`
	({rule_name: weight}) and return its name.
	"""
	name = _draft_ruleset_name()
	if frappe.db.exists("Optimization Ruleset", name):
		doc = frappe.get_doc("Optimization Ruleset", name)
		doc.rules = []
	else:
		doc = frappe.new_doc("Optimization Ruleset")
		doc.ruleset_name = name
		doc.description = (
			"Auto-managed scratch ruleset for Optimizer Studio; overwritten on every preview. "
			"Use 'Save Ruleset As' to keep a configuration under a permanent name."
		)
	doc.is_system = 0
	for rule_name, weight in rows.items():
		doc.append("rules", {"rule": rule_name, "weight": weight})
	doc.save(ignore_permissions=True)
	return doc.name


@frappe.whitelist()
def preview(mode: str, date: str, rows: str | dict, leaves_speculations: str | list | None = None) -> dict:
	"""Materialize the toggle selection into the user's draft ruleset, create a fresh
	Optimizer Run (``type="Automatic"``) with it, and solve synchronously.

	`rows` is a {rule_name: weight} mapping (a JSON string over the wire, like
	`leaves_speculations`). Mirrors what the Optimizer Run form's own "Solve" button
	does: a large problem that doesn't finish quickly escalates to a background job, in
	which case the caller should poll `get_run_status`.
	"""
	if isinstance(rows, str):
		rows = json.loads(rows)
	if isinstance(leaves_speculations, str):
		leaves_speculations = json.loads(leaves_speculations)
	if not rows:
		frappe.throw(frappe._("Select at least one rule before previewing."))

	ruleset_name = _save_draft_ruleset(rows)

	run = frappe.new_doc("Optimizer Run")
	run.mode = mode
	run.date = date
	run.ruleset = ruleset_name
	run.type = "Automatic"
	for leave_name in leaves_speculations or []:
		run.append("leaves_speculations", {"leave_application": leave_name})
	run.insert(ignore_permissions=True)

	status = run.solve()
	result = {"run": run.name, "status": status, "ruleset": ruleset_name}
	if status == "Solved":
		result["schedule"] = run.get_schedule_events()
		result["objective_value"] = run.objective_value
		result["statistics"] = run.get_run_statistics()
	else:
		result["solver_log"] = run.solver_log or ""
	return result


@frappe.whitelist()
def get_run_status(run: str) -> dict:
	"""Poll a previewed run that escalated to the background solver."""
	doc = frappe.get_doc("Optimizer Run", run)
	result = {"status": doc.status}
	if doc.status == "Solved":
		result["schedule"] = doc.get_schedule_events()
		result["objective_value"] = doc.objective_value
		result["statistics"] = doc.get_run_statistics()
	else:
		result["solver_log"] = doc.solver_log or ""
	return result


@frappe.whitelist()
def save_ruleset_as(ruleset: str, new_name: str) -> str:
	"""Promote a ruleset (typically the studio draft) to a permanent, real name."""
	new_name = (new_name or "").strip()
	if not new_name:
		frappe.throw(frappe._("Name required."))
	if frappe.db.exists("Optimization Ruleset", new_name):
		frappe.throw(frappe._("Optimization Ruleset {0} already exists.").format(new_name))
	doc = frappe.copy_doc(frappe.get_doc("Optimization Ruleset", ruleset))
	doc.ruleset_name = new_name
	doc.description = f"Saved from Optimizer Studio ({ruleset})."
	doc.is_system = 0
	doc.insert(ignore_permissions=True)
	return doc.name
