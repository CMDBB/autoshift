"""The Synergy Matrix page: the employee-by-employee grid Employee Role Synergy is a sparse
representation of, plus each employee's own value multiplier alongside it.

Pairing is between employees holding *different* gating Scheduling Roles within a
discipline — same-role pairing isn't a considered feature, so the matrix is genuinely
rectangular rather than square: each discipline's gating roles split into two sides (row
side / column side), alternating by role name so a role's holders always land wholly on one
side and can never pair with each other. Which specific role lands on which side is
arbitrary — Employee Role Synergy carries no role of its own, only the two employees — so a
simple deterministic split (rather than anything more elaborate) is all this needs.

A filled pair cell is one Employee Role Synergy and shows its `synergy_multiplier`: 1 is no
bonus, above 1 a bonus, below 1 a penalty, applied by `rules.synergy_value_objective` when
the pair is matched into the same room together. An empty cell is no row at all.

A dedicated "Self" row and column carry a different field entirely — Employee Scheduling
Role.value_multiplier, the per-holder bonus `rules.employee_value_objective` reads — shown
here for convenience since it feeds the same room-value mechanism synergy does. Editing it
never creates or deletes the Employee Scheduling Role; the row must already exist (that is
how the employee is on this matrix at all), and a cleared cell just resets it to 1.

Edits stage in the browser and land here as one batch (`apply_changes`), the same
stage-then-apply shape as the Role Matrix.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from autoshift.wallchart.source import INITIALS_FIELD, short_label

ERS = "Employee Role Synergy"
ESR = "Employee Scheduling Role"


def _gating_roles(disciplines: list[str] | None) -> list[dict]:
	filters: dict = {"active": 1, "gates_rooms": 1}
	if disciplines:
		filters["discipline"] = ["in", disciplines]
	return frappe.get_all("Scheduling Role", filters=filters, fields=["name", "discipline"])


def _split_by_side(roles: list[dict]) -> tuple[set[str], set[str]]:
	"""Every discipline's gating roles, alternated by name (sorted, for determinism) into a
	row side and a column side — a role's holders land wholly on one side, so a pairing
	between two holders of the same role can never be represented, which is the point.
	"""
	by_discipline: dict[str, list[str]] = {}
	for r in roles:
		by_discipline.setdefault(r.discipline, []).append(r.name)
	row_side: set[str] = set()
	col_side: set[str] = set()
	for discipline in sorted(by_discipline):
		for i, role in enumerate(sorted(by_discipline[discipline])):
			(row_side if i % 2 == 0 else col_side).add(role)
	return row_side, col_side


def _employees_for_roles(role_names: set[str], show_all: int | str) -> list[dict]:
	holder_employees = (
		sorted(
			set(
				frappe.get_all(
					ESR, filters={"active": 1, "scheduling_role": ["in", list(role_names)]}, pluck="employee"
				)
			)
		)
		if role_names
		else []
	)
	employee_fields = ["name", "employee_name"]
	if frappe.db.has_column("Employee", INITIALS_FIELD):
		employee_fields.append(INITIALS_FIELD)
	employee_filters: dict = {"status": "Active"}
	if not int(show_all or 0):
		employee_filters["name"] = ["in", holder_employees]
	return (
		frappe.get_all(
			"Employee", filters=employee_filters, fields=employee_fields, order_by="employee_name asc"
		)
		if int(show_all or 0) or holder_employees
		else []
	)


def _employee_dicts(employees: list[dict]) -> list[dict]:
	return [
		{
			"employee": e.name,
			"employee_name": e.employee_name,
			"initials": short_label(e.name, e.employee_name, e.get(INITIALS_FIELD)),
		}
		for e in employees
	]


@frappe.whitelist()
def list_disciplines() -> list[str]:
	"""Disciplines with at least one active, room-gating Scheduling Role — each one a
	possible scope for the matrix."""
	return sorted(
		set(
			frappe.get_all(
				"Scheduling Role", filters={"active": 1, "gates_rooms": 1}, pluck="discipline", distinct=True
			)
		)
	)


@frappe.whitelist()
def get_matrix(disciplines: str | list | None = None, show_all: int | str = 0) -> dict:
	"""Row employees, column employees, filled pair cells and each shown employee's own
	"self" value for a set of disciplines (or every discipline when blank).

	Row and column are two different role-based groups (see module docstring) — an employee
	is on one side or the other depending on which side their gating role landed on, active
	or not, in window or not, since the matrix is where you would go to fix exactly those —
	or every active employee with `show_all`, on both sides independently.
	"""
	frappe.has_permission(ERS, throw=True)
	if isinstance(disciplines, str):
		disciplines = json.loads(disciplines) if disciplines else []
	roles = _gating_roles(disciplines or None)
	row_role_names, col_role_names = _split_by_side(roles)

	row_employees = _employees_for_roles(row_role_names, show_all)
	col_employees = _employees_for_roles(col_role_names, show_all)
	row_names = [e.name for e in row_employees]
	col_names = [e.name for e in col_employees]
	all_names = sorted(set(row_names) | set(col_names))

	held_rows = (
		frappe.get_all(
			ESR,
			filters={"active": 1, "scheduling_role": ["in", list(row_role_names | col_role_names)]},
			fields=["name", "employee", "scheduling_role", "value_multiplier"],
			order_by="scheduling_role asc",
		)
		if row_role_names or col_role_names
		else []
	)
	# One gating role per employee is the practice this page assumes — where an employee
	# somehow holds more than one, the first by role name wins, deterministically, for the
	# "self" value.
	self_by_employee: dict[str, dict] = {}
	for row in held_rows:
		self_by_employee.setdefault(
			row.employee,
			{
				"name": row.name,
				"scheduling_role": row.scheduling_role,
				"value_multiplier": float(row.value_multiplier or 1),
			},
		)

	synergy_rows = (
		frappe.get_all(
			ERS,
			filters={"employee_a": ["in", all_names], "employee_b": ["in", all_names]},
			fields=["name", "employee_a", "employee_b", "synergy_multiplier", "active"],
		)
		if all_names
		else []
	)
	row_set, col_set = set(row_names), set(col_names)
	# Only cross-side pairs are drawn: a stray same-side row (from before this split existed,
	# or an employee who happens to hold a role on both sides) has no cell to land in.
	cells = {
		f"{r.employee_a}|{r.employee_b}": {
			"name": r.name,
			"synergy_multiplier": float(r.synergy_multiplier or 1),
			"active": bool(r.active),
		}
		for r in synergy_rows
		if (r.employee_a in row_set and r.employee_b in col_set)
		or (r.employee_a in col_set and r.employee_b in row_set)
	}

	return {
		"row_employees": _employee_dicts(row_employees),
		"col_employees": _employee_dicts(col_employees),
		"cells": cells,
		"self": {name: self_by_employee[name] for name in all_names if name in self_by_employee},
		"can_create": bool(frappe.has_permission(ERS, "create")),
		"can_delete": bool(frappe.has_permission(ERS, "delete")),
		"can_write_esr": bool(frappe.has_permission(ESR, "write")),
	}


@frappe.whitelist(methods=["POST"])
def apply_changes(changes: str | list) -> dict:
	"""Write a staged batch of two kinds, distinguished by `kind`:

	- `{kind: "pair", employee_a, employee_b, synergy_multiplier}` — an Employee Role
	  Synergy; `synergy_multiplier` of null removes it, anything else creates or updates it.
	  Each pair is canonicalized (sorted) before lookup, matching the doctype's own
	  `before_insert` — (A, B) and (B, A) always resolve to one document.
	- `{kind: "self", employee, scheduling_role, value_multiplier}` — an existing Employee
	  Scheduling Role's own value multiplier; a null/blank value resets it to 1 rather than
	  deleting the row, since removing it would revoke the role that put the employee on
	  this matrix at all.

	One request, so one transaction: a row that fails validation rolls the whole batch
	back rather than leaving half of it applied. Permissions are each doctype's own —
	`save`/`insert`/`delete_doc` check them.
	"""
	if isinstance(changes, str):
		changes = json.loads(changes)

	created = updated = deleted = 0
	for change in changes or []:
		if change.get("kind") == "self":
			if _apply_self_value_change(change):
				updated += 1
			continue

		a, b = change.get("employee_a"), change.get("employee_b")
		if not a or not b or a == b:
			frappe.throw(_("Every change needs two distinct employees."))
		employee_a, employee_b = sorted((a, b))
		existing = frappe.db.get_value(ERS, {"employee_a": employee_a, "employee_b": employee_b}, "name")
		value = change.get("synergy_multiplier")

		if value is None or value == "":
			if existing:
				frappe.delete_doc(ERS, existing)
				deleted += 1
			continue

		value = float(value)
		if existing:
			doc = frappe.get_doc(ERS, existing)
			if float(doc.synergy_multiplier or 1) == value:
				continue
			doc.synergy_multiplier = value
			doc.save()
			updated += 1
		else:
			frappe.get_doc(
				{
					"doctype": ERS,
					"employee_a": employee_a,
					"employee_b": employee_b,
					"synergy_multiplier": value,
					"active": 1,
				}
			).insert()
			created += 1

	return {"created": created, "updated": updated, "deleted": deleted}


def _apply_self_value_change(change: dict) -> bool:
	"""Write Employee Scheduling Role.value_multiplier. Always an update — the row already
	exists, or the employee would not be on this matrix — and never a create or delete."""
	employee, role = change.get("employee"), change.get("scheduling_role")
	existing = frappe.db.get_value(ESR, {"employee": employee, "scheduling_role": role}, "name")
	if not existing:
		frappe.throw(_("{0} does not hold {1}.").format(employee, role))
	value = change.get("value_multiplier")
	new_value = 1.0 if value in (None, "") else float(value)
	doc = frappe.get_doc(ESR, existing)
	if float(doc.value_multiplier or 1) == new_value:
		return False
	doc.value_multiplier = new_value
	doc.save()
	return True
