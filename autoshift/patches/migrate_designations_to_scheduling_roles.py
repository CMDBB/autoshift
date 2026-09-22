"""
Migrate the designation-keyed room config onto Scheduling Roles.

Before this, an employee was one discipline (`Employee.department`) and one capability
(`Employee.designation`), and `Discipline Designation Branch Config` carried both plus a
branch. That could not express somebody who works two disciplines, so this converts:

  * every distinct (discipline, designation) in the old config -> one `Scheduling Role`,
    carrying that pair's `max_rooms_for_employee_type`;
  * every Active employee whose (department, designation) matches such a pair -> one
    `Employee Scheduling Role`, with no agreed FTE (the split is opt-in);
  * the old config rows -> `Discipline Branch Config`, one per (discipline, branch), which
    is the shape `data_loader` always collapsed them to anyway.

The old doctypes are dropped at the end. Seeded roles reproduce the previous behaviour
exactly: everybody holds precisely one role, so the model has the same variables it did.

Fresh installs never see any of this — there is no old doctype to read, and the patch
returns immediately.
"""

import frappe

OLD_CONFIG = "Discipline Designation Branch Config"
OLD_CHILD = "Discipline Designation Branch Config Shift Type"


def execute():
	if not frappe.db.exists("DocType", OLD_CONFIG):
		return

	old_rows = frappe.get_all(
		OLD_CONFIG,
		fields=[
			"name",
			"discipline",
			"employee_type",
			"branch",
			"max_rooms_for_employee_type",
			"rooms_num",
		],
	)
	if not old_rows:
		_drop_old_doctypes()
		return

	shift_types_by_row = _old_shift_types(old_rows)
	role_by_pair = _create_scheduling_roles(old_rows)
	_create_employee_roles(role_by_pair)
	_create_branch_configs(old_rows, shift_types_by_row)
	_drop_old_doctypes()


def _old_shift_types(old_rows) -> dict[str, set[str]]:
	by_row: dict[str, set[str]] = {}
	for row in frappe.get_all(
		OLD_CHILD,
		filters={"parent": ["in", [r.name for r in old_rows]]},
		fields=["parent", "shift_type"],
	):
		by_row.setdefault(row.parent, set()).add(row.shift_type)
	return by_row


def _create_scheduling_roles(old_rows) -> dict[tuple[str, str], str]:
	"""One role per (discipline, designation), keeping that pair's max-rooms figure."""
	max_rooms: dict[tuple[str, str], int] = {}
	for row in old_rows:
		if not (row.discipline and row.employee_type):
			continue
		pair = (row.discipline, row.employee_type)
		# max across branches, matching what data_loader used to do when it collapsed the key
		max_rooms[pair] = max(max_rooms.get(pair, 0), int(row.max_rooms_for_employee_type or 1))

	# A designation that only ever appears in one discipline gets the plain name; only
	# genuinely ambiguous ones need the discipline spelled out.
	disciplines_per_designation: dict[str, set[str]] = {}
	for discipline, designation in max_rooms:
		disciplines_per_designation.setdefault(designation, set()).add(discipline)

	role_by_pair: dict[tuple[str, str], str] = {}
	for (discipline, designation), rooms in sorted(max_rooms.items()):
		ambiguous = len(disciplines_per_designation[designation]) > 1
		role_name = f"{designation} ({discipline})" if ambiguous else designation

		if frappe.db.exists("Scheduling Role", role_name):
			role_by_pair[(discipline, designation)] = role_name
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Scheduling Role",
				"role_name": role_name,
				"discipline": discipline,
				"max_rooms": max(rooms, 1),
				"active": 1,
				"description": (
					f"Migrated from the {designation} rows of the old "
					f"Discipline Designation Branch Config for {discipline}."
				),
			}
		).insert(ignore_permissions=True)
		role_by_pair[(discipline, designation)] = doc.name

	return role_by_pair


def _create_employee_roles(role_by_pair: dict[tuple[str, str], str]) -> None:
	"""Give every previously-schedulable employee the one role they implicitly had."""
	unplaced: list[str] = []
	designations = {designation for _discipline, designation in role_by_pair}

	for emp in frappe.get_all(
		"Employee",
		filters={"status": "Active", "designation": ["in", list(designations)]},
		fields=["name", "department", "designation"],
	):
		role = role_by_pair.get((emp.department, emp.designation))
		if not role:
			# Previously loaded (the old filter looked at designation alone) but matched no
			# room-coverage constraint, because their department was not a configured
			# discipline. They were dead weight in the model; now they are simply out of scope.
			unplaced.append(f"{emp.name} ({emp.designation} in {emp.department or 'no department'})")
			continue

		if frappe.db.exists("Employee Scheduling Role", {"employee": emp.name, "scheduling_role": role}):
			continue

		frappe.get_doc(
			{
				"doctype": "Employee Scheduling Role",
				"employee": emp.name,
				"scheduling_role": role,
				"active": 1,
			}
		).insert(ignore_permissions=True)

	if unplaced:
		frappe.log_error(
			title="Employees left without a Scheduling Role",
			message=(
				"These Active employees held a configured designation but their department was "
				"not a configured discipline, so the migration could not infer a Scheduling "
				"Role. The optimizer never staffed a room with them, so this changes no "
				"schedule — but give them a role if they should be scheduled:\n\n"
				+ "\n".join(sorted(unplaced))
			),
		)


def _create_branch_configs(old_rows, shift_types_by_row: dict[str, set[str]]) -> None:
	"""Collapse the designation rows into one config per (discipline, branch)."""
	rooms: dict[tuple[str, str], int] = {}
	shift_types: dict[tuple[str, str], set[str]] = {}
	for row in old_rows:
		if not (row.discipline and row.branch):
			continue
		key = (row.discipline, row.branch)
		# The old rows duplicated the room count per designation; they should agree, and
		# where they don't, the larger figure is the safe reading of "rooms that exist".
		rooms[key] = max(rooms.get(key, 0), int(row.rooms_num or 0))
		shift_types.setdefault(key, set()).update(shift_types_by_row.get(row.name, set()))

	for (discipline, branch), rooms_num in sorted(rooms.items()):
		if frappe.db.exists("Discipline Branch Config", {"discipline": discipline, "branch": branch}):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Discipline Branch Config",
				"discipline": discipline,
				"branch": branch,
				"rooms_num": rooms_num,
			}
		)
		for shift_type in sorted(shift_types.get((discipline, branch), set())):
			doc.append("shift_types", {"shift_type": shift_type})
		doc.insert(ignore_permissions=True)


def _drop_old_doctypes() -> None:
	"""The JSON is gone from the app, but migrate leaves the DB definition behind."""
	for doctype in (OLD_CONFIG, OLD_CHILD):
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
