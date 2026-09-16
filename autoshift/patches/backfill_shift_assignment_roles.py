"""Fill in `custom_scheduling_role` on the Shift Assignments and rotas already on the books.

Records created before the field existed name no role, so the data loader used to recover
one from the assignment's Shift Location: the discipline on the location, then whichever
Scheduling Role the employee holds there. That guess picks by sort order as soon as somebody
holds two roles in one discipline — the normal case now that a rota settles *presence* and
the role worked during it varies — so the loader no longer makes it, and the value is stored
on the record instead.

**A stopgap.** The right fix is for `zawin2frappe` to write the role at import time; this
patch exists because its re-imports currently overwrite rows, and it goes when they stop.
Only blank fields are filled, so a role a planner has since corrected is never touched, and
re-running it is a no-op.

Collateral roles are never inferred: a collateral duty is an explicit editorial act, and
nothing in the old data records one.
"""

import frappe

BATCH = 500


def execute():
	locations = {
		row.name: row.custom_discipline
		for row in frappe.get_all(
			"Shift Location", fields=["name", "custom_discipline"], ignore_permissions=True
		)
		if row.custom_discipline
	}
	if not locations:
		return

	# (employee, discipline) -> role, resolved exactly as the loader used to: a binding role
	# first (a settled schedule should be attributed to the role that is actually settled),
	# then by name. Collateral roles are not candidates.
	roles = {
		row.name: row
		for row in frappe.get_all(
			"Scheduling Role",
			fields=["name", "discipline", "assignments_binding", "assignment_mode"],
			ignore_permissions=True,
		)
	}
	held: dict[tuple[str, str], list[str]] = {}
	for row in frappe.get_all(
		"Employee Scheduling Role",
		filters={"active": 1},
		fields=["employee", "scheduling_role", "binding_override", "assignment_mode_override"],
		ignore_permissions=True,
	):
		role = roles.get(row.scheduling_role)
		if not role:
			continue
		mode = row.assignment_mode_override or role.assignment_mode
		if mode == "Collateral":
			continue
		binding = (
			row.binding_override == "Binding" if row.binding_override else bool(role.assignments_binding)
		)
		held.setdefault((row.employee, role.discipline), []).append((not binding, role.name))

	resolved = {key: sorted(names)[0][1] for key, names in held.items() if names}
	if not resolved:
		return

	for doctype in ("Shift Assignment", "Shift Schedule Assignment"):
		_backfill(doctype, locations, resolved)


def _backfill(doctype: str, locations: dict[str, str], resolved: dict[tuple[str, str], str]) -> None:
	rows = frappe.get_all(
		doctype,
		filters={
			"shift_location": ["in", list(locations)],
			"custom_scheduling_role": ["in", ["", None]],
		},
		fields=["name", "employee", "shift_location"],
		ignore_permissions=True,
	)
	updated = 0
	for index, row in enumerate(rows, start=1):
		role = resolved.get((row.employee, locations[row.shift_location]))
		if not role:
			continue
		# db_set on the docname: these are submitted documents, and the field is a
		# description of a record that is not otherwise changing.
		frappe.db.set_value(doctype, row.name, "custom_scheduling_role", role, update_modified=False)
		updated += 1
		if index % BATCH == 0:
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	print(f"backfilled {updated} of {len(rows)} {doctype} rows with a Scheduling Role")
