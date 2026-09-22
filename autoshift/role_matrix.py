"""The Role Matrix page: Employee Scheduling Role drawn as the employee-by-role matrix it
is a sparse representation of, plus a one-chip summary of each row's Employee Settings.

A filled cell is one Employee Scheduling Role and shows its `suitability`: 1 for a
regular holder, higher for a less suitable substitute (read by
`rules.suitability_preference_objective`). An empty cell is no row at all — the employee
is not scheduled in that role. Edits stage in the browser and land here as one batch
(`apply_changes`), the same stage-then-apply shape as the Rota Editor, but without a
server-side draft: a batch of numbers is cheap to lose.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from autoshift.wallchart.source import INITIALS_FIELD, short_label

ESR = "Employee Scheduling Role"


@frappe.whitelist()
def list_disciplines() -> list[str]:
	"""Disciplines with at least one active Scheduling Role — each one a possible column set."""
	return sorted(
		set(frappe.get_all("Scheduling Role", filters={"active": 1}, pluck="discipline", distinct=True))
	)


def _roles(disciplines: list[str] | None) -> list[dict]:
	"""Active roles, ordered as the wall chart orders its lanes: discipline, then
	`display_order_key`, then most rooms per holder, then name."""
	filters: dict = {"active": 1}
	if disciplines:
		filters["discipline"] = ["in", disciplines]
	rows = frappe.get_all(
		"Scheduling Role",
		filters=filters,
		fields=["name", "discipline", "max_rooms", "display_order_key", "assignments_binding"],
	)
	rows.sort(key=lambda r: (r.discipline, r.display_order_key or 0, -(r.max_rooms or 1), r.name))
	return [
		{"role": r.name, "discipline": r.discipline, "binding": bool(r.assignments_binding)} for r in rows
	]


def _settings_summary(employees: list[str]) -> dict[str, dict]:
	"""What the settings chip needs: the document, and enough of its tables to say
	in a word or two what it changes."""
	if not employees:
		return {}
	settings = frappe.get_all(
		"Employee Settings",
		filters={"employee": ["in", employees]},
		fields=["name", "employee", "active", "favourite_shift"],
	)
	names = [s.name for s in settings]
	shift_prefs: dict[str, list[dict]] = {}
	branch_prefs: dict[str, list[dict]] = {}
	if names:
		for row in frappe.get_all(
			"Employee Shift Preference",
			filters={"parent": ["in", names], "parenttype": "Employee Settings"},
			fields=["parent", "shift_type", "weight"],
			order_by="idx asc",
		):
			shift_prefs.setdefault(row.parent, []).append(
				{"shift_type": row.shift_type, "weight": row.weight}
			)
		for row in frappe.get_all(
			"Employee Branch Preference",
			filters={"parent": ["in", names], "parenttype": "Employee Settings"},
			fields=["parent", "branch", "weight"],
			order_by="idx asc",
		):
			branch_prefs.setdefault(row.parent, []).append({"branch": row.branch, "weight": row.weight})
	return {
		s.employee: {
			"name": s.name,
			"active": bool(s.active),
			"favourite_shift": s.favourite_shift,
			"shift_preferences": shift_prefs.get(s.name, []),
			"branch_preferences": branch_prefs.get(s.name, []),
		}
		for s in settings
	}


@frappe.whitelist()
def get_matrix(disciplines: str | list | None = None, show_all: int | str = 0) -> dict:
	"""Rows, columns and filled cells for a set of disciplines (or every discipline when
	blank) — the same set filters both: a role is a column when its discipline is in the
	set, an employee a row when they hold one of those columns.

	Rows are active employees holding any Employee Scheduling Role among the shown
	columns — active or not, in window or not, since the matrix is where you would go to
	fix exactly those — or every active employee with `show_all`.
	"""
	frappe.has_permission(ESR, throw=True)
	if isinstance(disciplines, str):
		disciplines = json.loads(disciplines) if disciplines else []
	roles = _roles(disciplines or None)
	role_names = [r["role"] for r in roles]

	esr_rows = (
		frappe.get_all(
			ESR,
			filters={"scheduling_role": ["in", role_names]},
			fields=[
				"name",
				"employee",
				"scheduling_role",
				"suitability",
				"role_fte",
				"max_rooms",
				"binding_override",
				"active",
				"valid_from",
				"valid_to",
			],
		)
		if role_names
		else []
	)

	employee_fields = ["name", "employee_name"]
	if frappe.db.has_column("Employee", INITIALS_FIELD):
		employee_fields.append(INITIALS_FIELD)
	employee_filters: dict = {"status": "Active"}
	if not int(show_all or 0):
		employee_filters["name"] = ["in", sorted({r.employee for r in esr_rows})]
	employees = (
		frappe.get_all(
			"Employee", filters=employee_filters, fields=employee_fields, order_by="employee_name asc"
		)
		if int(show_all or 0) or esr_rows
		else []
	)
	shown = {e.name for e in employees}
	settings = _settings_summary([e.name for e in employees])

	return {
		"roles": roles,
		"employees": [
			{
				"employee": e.name,
				"employee_name": e.employee_name,
				"initials": short_label(e.name, e.employee_name, e.get(INITIALS_FIELD)),
				"settings": settings.get(e.name),
			}
			for e in employees
		],
		"cells": {
			f"{r.employee}|{r.scheduling_role}": {
				"name": r.name,
				# blank predates the field, and the loader reads it as a holder
				"suitability": float(r.suitability or 1),
				"role_fte": r.role_fte,
				"max_rooms": r.max_rooms,
				"binding_override": r.binding_override,
				"active": bool(r.active),
				"valid_from": r.valid_from,
				"valid_to": r.valid_to,
			}
			for r in esr_rows
			if r.employee in shown
		},
		"today": frappe.utils.today(),
		"can_create": bool(frappe.has_permission(ESR, "create")),
		"can_delete": bool(frappe.has_permission(ESR, "delete")),
	}


@frappe.whitelist(methods=["POST"])
def apply_changes(changes: str | list) -> dict:
	"""Write a staged batch: `[{employee, role, suitability}]`, where a `suitability` of
	null removes the Employee Scheduling Role and anything else creates or updates it.

	One request, so one transaction: a row that fails validation rolls the whole batch
	back rather than leaving half of it applied. Permissions are the doctype's own —
	`save`/`insert`/`delete_doc` check them.
	"""
	if isinstance(changes, str):
		changes = json.loads(changes)

	created = updated = deleted = 0
	for change in changes or []:
		employee, role = change.get("employee"), change.get("role")
		if not employee or not role:
			frappe.throw(_("Every change needs an employee and a role."))
		existing = frappe.db.get_value(ESR, {"employee": employee, "scheduling_role": role}, "name")
		value = change.get("suitability")

		if value is None or value == "":
			if existing:
				frappe.delete_doc(ESR, existing)
				deleted += 1
			continue

		value = float(value)
		if existing:
			doc = frappe.get_doc(ESR, existing)
			if float(doc.suitability or 1) == value:
				continue
			doc.suitability = value
			doc.save()
			updated += 1
		else:
			frappe.get_doc(
				{
					"doctype": ESR,
					"employee": employee,
					"scheduling_role": role,
					"suitability": value,
					"active": 1,
				}
			).insert()
			created += 1

	return {"created": created, "updated": updated, "deleted": deleted}
