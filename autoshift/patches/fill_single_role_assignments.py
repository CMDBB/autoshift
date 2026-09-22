"""Attribute a Scheduling Role to every record whose holder only has one.

`backfill_shift_assignment_roles` recovered a blank `custom_scheduling_role` from the
record's Shift Location: the discipline filed on the location, then a role the employee
holds there. Two things fall outside that, and both are common in imported data — a
record with no Shift Location at all, and one whose location names no discipline. Neither
needs one: somebody who holds exactly one non-collateral Scheduling Role worked that role,
and there is no second answer for a location to choose between.

This is the same rung `optimizer.types.resolve_assignment_role` now applies at read time
(`rota.materialize.RoleContext`, `optimizer.data_loader`), written down so the field on
disk agrees with what everything already reads — and so the Rota Editor can tell which
discipline a pattern belongs to, which is what stops one discipline's view editing
another's settled week.

Only blank fields are filled, so a role a planner has since corrected is never touched,
and re-running it is a no-op. Collateral roles are never inferred: a duty is an explicit
editorial act, and nothing in the old data records one.
"""

import frappe

BATCH = 500


def execute():
	roles = {
		row.name: row
		for row in frappe.get_all(
			"Scheduling Role",
			filters={"active": 1},
			fields=["name", "assignment_mode"],
			ignore_permissions=True,
		)
	}
	if not roles:
		return

	# employee -> the non-collateral roles they hold; only a single one settles anything.
	working: dict[str, set[str]] = {}
	for row in frappe.get_all(
		"Employee Scheduling Role",
		filters={"active": 1},
		fields=["employee", "scheduling_role", "assignment_mode_override"],
		ignore_permissions=True,
	):
		role = roles.get(row.scheduling_role)
		if not role:
			continue
		if (row.assignment_mode_override or role.assignment_mode) == "Collateral":
			continue
		working.setdefault(row.employee, set()).add(row.scheduling_role)

	resolved = {employee: held.pop() for employee, held in working.items() if len(held) == 1}
	if not resolved:
		return

	for doctype in ("Shift Assignment", "Shift Schedule Assignment"):
		_fill(doctype, resolved)


def _fill(doctype: str, resolved: dict[str, str]) -> None:
	rows = frappe.get_all(
		doctype,
		filters={
			"employee": ["in", sorted(resolved)],
			"custom_scheduling_role": ["in", ["", None]],
		},
		fields=["name", "employee"],
		ignore_permissions=True,
	)
	for index, row in enumerate(rows, start=1):
		# db_set on the docname: a Shift Assignment is submitted, and the field is a
		# description of a record that is not otherwise changing.
		frappe.db.set_value(
			doctype, row.name, "custom_scheduling_role", resolved[row.employee], update_modified=False
		)
		if index % BATCH == 0:
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	print(f"attributed {len(rows)} {doctype} rows to their holder's only Scheduling Role")
