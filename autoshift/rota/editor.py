# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The DB half of the Rota Editor: turning `edit.py`'s pure `Change`/`EditPlan` into a
grid a browser can draw, a transcript it can read, and — on Apply — real documents.

Mirrors `materialize.py`'s split: the pure rule lives in `edit.py`, this module only
loads, calls it, and writes back. One `Rota Edit Draft` per (user, discipline) buffers a
session's edits (`get_or_create_draft`); every read here folds that buffer's `Change`s
onto the current `Shift Schedule Assignment`s before drawing anything, so the grid always
shows what Apply would produce, not just what is already on the books.

A drafted **add** needs a company nothing else here supplies — see `edit.Change` — so
`_change_from_row` fetches it from `Employee.company` at the point a row is turned into a
`Change`, once per read.
"""

from __future__ import annotations

import datetime

import frappe

from . import edit
from .cycle import FREQUENCY_LABEL, WEEKDAY_INDEX, WEEKDAY_LABEL, Rota, monday_of, occurrences
from .materialize import load_rotas


def _company_of(employee: str) -> str | None:
	return frappe.db.get_value("Employee", employee, "company")


def _role_discipline() -> dict[str, str]:
	return {
		r.name: r.discipline
		for r in frappe.get_all("Scheduling Role", filters={"active": 1}, fields=["name", "discipline"])
	}


def _employees_of(discipline: str) -> list[str]:
	"""Every employee holding a binding Scheduling Role in `discipline` — the rows the
	Rota Editor's grid has to show. Not scoped to `assignments_binding` at the role level:
	`configured_binding_pairs` already resolves the per-holder override."""
	from autoshift.optimizer import data_loader

	role_discipline = _role_discipline()
	return sorted(
		{
			employee
			for employee, role in data_loader.configured_binding_pairs()
			if role_discipline.get(role) == discipline
		}
	)


@frappe.whitelist()
def list_disciplines() -> list[str]:
	"""Disciplines with at least one binding pair — the tool's whole scope, since a
	schedule the optimizer is free to plan is not this tool's business."""
	from autoshift.optimizer import data_loader

	role_discipline = _role_discipline()
	return sorted(
		{
			role_discipline[role]
			for _employee, role in data_loader.configured_binding_pairs()
			if role in role_discipline
		}
	)


def _config_rows(discipline: str) -> list[dict]:
	return frappe.get_all(
		"Discipline Branch Config", filters={"discipline": discipline}, fields=["name", "branch"]
	)


def shift_types_of(discipline: str) -> list[dict]:
	"""Shift Types in scope anywhere in this discipline, ordered like the wall chart's
	own sections (`Shift Type.start_time`)."""
	configs = _config_rows(discipline)
	if not configs:
		return []
	names = {
		row.shift_type
		for row in frappe.get_all(
			"Discipline Branch Config Shift Type",
			filters={"parenttype": "Discipline Branch Config", "parent": ["in", [c.name for c in configs]]},
			fields=["shift_type"],
		)
	}
	if not names:
		return []
	return frappe.get_all(
		"Shift Type",
		filters={"name": ["in", list(names)]},
		fields=["name", "start_time"],
		order_by="start_time asc",
	)


def branches_of(discipline: str) -> dict[str, list[str]]:
	"""shift_type -> branches valid for it in this discipline, i.e. legal drop targets."""
	configs = _config_rows(discipline)
	if not configs:
		return {}
	branch_of = {c.name: c.branch for c in configs}
	rows = frappe.get_all(
		"Discipline Branch Config Shift Type",
		filters={"parenttype": "Discipline Branch Config", "parent": ["in", list(branch_of)]},
		fields=["parent", "shift_type"],
	)
	result: dict[str, set[str]] = {}
	for row in rows:
		result.setdefault(row.shift_type, set()).add(branch_of[row.parent])
	return {shift_type: sorted(branches) for shift_type, branches in result.items()}


def _rotas_by_branch(employees: list[str]) -> list[Rota]:
	"""`materialize.load_rotas`, with `shift_location` (a `Shift Location` docname — the
	raw field `Shift Schedule Assignment.shift_location` actually stores) resolved to the
	**Branch** it belongs to (`Shift Location.custom_branch`).

	Everything in this module and in `edit.py` reasons in Branches — the vocabulary
	`Discipline Branch Config` and `branches_of` already use, and the one a planner drags
	a chip between. A Shift Location with no `custom_branch` set falls back to its own
	name, so it still draws as *something* rather than disappearing.
	"""
	rotas = load_rotas(set(employees)) if employees else []
	locations = {r.shift_location for r in rotas if r.shift_location}
	branch_of = {}
	if locations:
		branch_of = {
			row.name: row.custom_branch or row.name
			for row in frappe.get_all(
				"Shift Location", filters={"name": ["in", list(locations)]}, fields=["name", "custom_branch"]
			)
		}
	return [
		Rota(
			assignment=r.assignment,
			employee=r.employee,
			company=r.company,
			shift_type=r.shift_type,
			shift_location=branch_of.get(r.shift_location, r.shift_location) if r.shift_location else None,
			weekdays=r.weekdays,
			cycle_weeks=r.cycle_weeks,
			anchor=r.anchor,
		)
		for r in rotas
	]


def _shift_location_for(discipline: str, branch: str) -> str:
	"""The `Shift Location` a fresh `Shift Schedule Assignment` should point at for
	`branch` — the inverse of `_rotas_by_branch`.

	Room-level assignment doesn't exist yet (see CLAUDE.md, "To be implemented") — the
	optimizer tracks an aggregate room count per (discipline, branch), never a specific
	room — so picking the alphabetically-first matching Shift Location is as good a
	choice as any other; nothing downstream distinguishes between them.
	"""
	matches = frappe.get_all(
		"Shift Location",
		filters={"custom_branch": branch, "custom_discipline": discipline},
		pluck="name",
		order_by="name asc",
	)
	if not matches:
		matches = frappe.get_all(
			"Shift Location", filters={"custom_branch": branch}, pluck="name", order_by="name asc"
		)
	if not matches:
		frappe.throw(
			frappe._(
				"No Shift Location is configured for branch {0}. Add one (with Discipline and "
				"Branch set) before assigning a shift there."
			).format(branch)
		)
	return matches[0]


# ── the draft ─────────────────────────────────────────────────────────────────


def _draft_name(discipline: str, user: str | None = None) -> str:
	return f"Rota Draft — {user or frappe.session.user} — {discipline}"


def _existing_draft(discipline: str, user: str | None = None):
	name = _draft_name(discipline, user)
	return frappe.get_doc("Rota Edit Draft", name) if frappe.db.exists("Rota Edit Draft", name) else None


def get_or_create_draft(discipline: str, user: str | None = None):
	"""One draft per (user, discipline) by construction — `autoname` derives the name
	from both, so this always finds the same document rather than piling up stray ones."""
	name = _draft_name(discipline, user)
	if frappe.db.exists("Rota Edit Draft", name):
		return frappe.get_doc("Rota Edit Draft", name)
	doc = frappe.new_doc("Rota Edit Draft")
	doc.user = user or frappe.session.user
	doc.discipline = discipline
	doc.insert(ignore_permissions=True)
	return doc


def _change_from_row(row) -> edit.Change:
	op = row.op.lower()
	return edit.Change(
		op=op,
		employee=row.employee,
		company=_company_of(row.employee) if op == "add" else None,
		to_shift_type=row.to_shift_type or None,
		to_weekday=WEEKDAY_INDEX.get(row.to_weekday),
		to_branch=row.to_branch or None,
		to_phase=frappe.utils.cint(row.to_phase),
		from_assignment=row.from_assignment or None,
		from_weekday=WEEKDAY_INDEX.get(row.from_weekday),
		from_phase=frappe.utils.cint(row.from_phase),
	)


def _view_start(start) -> datetime.date:
	return monday_of(frappe.utils.getdate(start)) + datetime.timedelta(days=7)


def _effective_rotas(rotas: list[Rota], plan: edit.EditPlan) -> list[Rota]:
	"""What the grid should draw: the current rotas with the draft's plan folded in,
	its creations given placeholder names so a chip can still be dragged again before
	Apply ever runs."""
	kept = [r for r in rotas if r.assignment not in plan.delete]
	created = [
		Rota(
			assignment=f"NEW-{index}",
			employee=new.employee,
			company=new.company,
			shift_type=new.shift_type,
			shift_location=new.branch,
			weekdays=new.weekdays,
			cycle_weeks=new.cycle_weeks,
			anchor=new.anchor,
		)
		for index, new in enumerate(plan.create)
	]
	return kept + created


def _hidden_cells(emp_rotas: list[Rota], days: list[datetime.date], view_start: datetime.date) -> dict:
	"""A period-incompatible employee's read-only summary row: for each `(shift_type,
	day)` the view actually draws, the fraction of that shift's own cadence that puts
	them there — see `edit.phase_fractions` for why this is an average over a full
	cycle rather than the (possibly misleading) single phase this view happens to show.
	Sparse like a normal row's `cells` — a cell with zero occupancy is simply absent.
	"""
	shift_types = sorted({r.shift_type for r in emp_rotas})
	weekdays = sorted({d.weekday() for d in days})
	cells: dict[str, dict] = {}
	for shift_type in shift_types:
		cycle_weeks = max((r.cycle_weeks for r in emp_rotas if r.shift_type == shift_type), default=1)
		fractions = edit.phase_fractions(emp_rotas, shift_type, weekdays, view_start, cycle_weeks)
		for day in days:
			occupied, branch = fractions.get(day.weekday(), (0, None))
			if not occupied:
				continue
			cells[f"{shift_type}|{day.isoformat()}"] = {
				"occupied": occupied,
				"cycle_weeks": cycle_weeks,
				"branch": branch,
			}
	return cells


@frappe.whitelist()
def get_state(discipline: str, start: str, view_weeks: int | str) -> dict:
	"""Everything the Rota Editor page needs to draw one discipline at one width: the
	grid (current books + draft folded in) and the transcript of what is staged.

	Cheap enough to call after every stage/discard: two small config tables, one rota
	read, and `edit.apply_changes` over what is usually a handful of rows.
	"""
	frappe.has_permission("Shift Schedule Assignment", throw=True)
	view_weeks = int(view_weeks)

	start_date = _view_start(start)
	end_date = start_date + datetime.timedelta(days=view_weeks * 7 - 1)
	days = [start_date + datetime.timedelta(days=i) for i in range(view_weeks * 7)]

	employees = _employees_of(discipline)
	rotas = _rotas_by_branch(employees)

	draft = _existing_draft(discipline)
	staged = [_change_from_row(row) for row in (draft.changes if draft else [])]
	plan = edit.apply_changes(rotas, staged, view_start=start_date, view_weeks=view_weeks)
	effective = _effective_rotas(rotas, plan)

	by_employee: dict[str, list[Rota]] = {}
	for rota in effective:
		by_employee.setdefault(rota.employee, []).append(rota)

	names = (
		{
			e.name: f"{e.name}:{e.custom_initials or 'n/a'}"
			# e.name: e.employee_name
			for e in frappe.get_all(
				"Employee", filters={"name": ["in", employees]}, fields=["name", "custom_initials"]
			)
		}
		if employees
		else {}
	)

	visible, hidden = [], []
	for employee in employees:
		emp_rotas = by_employee.get(employee, [])
		bad_cadences = sorted(
			{r.cycle_weeks for r in emp_rotas if not edit.rota_view_weeks(r.cycle_weeks, view_weeks)}
		)
		if bad_cadences:
			hidden.append(
				{
					"employee": employee,
					"employee_name": names.get(employee, employee),
					"cycle_weeks": bad_cadences,
					"cells": _hidden_cells(emp_rotas, days, start_date),
				}
			)
			continue
		cells = {}
		for rota in emp_rotas:
			for day in occurrences(rota, start_date, end_date):
				cells[f"{rota.shift_type}|{day.isoformat()}"] = {
					"branch": rota.shift_location,
					"assignment": rota.assignment,
					"cycle_weeks": rota.cycle_weeks,
				}
		visible.append({"employee": employee, "employee_name": names.get(employee, employee), "cells": cells})
	visible = sorted(visible, key=lambda e: int(e.get("employee")))

	return {
		"discipline": discipline,
		"start": start_date.isoformat(),
		"view_weeks": view_weeks,
		"days": [{"date": d.isoformat(), "weekday": WEEKDAY_LABEL[d.weekday()]} for d in days],
		"shift_types": shift_types_of(discipline),
		"branches": branches_of(discipline),
		"employees": visible,
		"hidden_employees": hidden,
		"pending_changes": [
			{"description": row.description, "op": row.op} for row in (draft.changes if draft else [])
		],
		# Auto-detected cadence promotions/demotions the staged batch causes — see
		# edit.EditPlan.cadence_changes. Recomputed fresh every call, never stored:
		# it is a derived fact about the current draft + view, not itself an edit.
		"periodicity_notes": list(plan.cadence_changes),
	}


@frappe.whitelist()
def stage_change(discipline: str, change: str | dict, start: str, view_weeks: int | str) -> dict:
	"""Append one edit to this user's draft for `discipline`, and return the refreshed
	`get_state` in the same round trip.

	`change` mirrors a `Rota Edit Draft Change` row: `{op, employee, from_assignment,
	from_weekday, to_shift_type, to_weekday, to_branch}`, weekdays as `Assignment Rule
	Day` labels ("Monday" etc — the grid's own day columns already carry that label, so
	the browser never has to think in a different weekday numbering than the one it drew).
	"""
	change = frappe.parse_json(change) if isinstance(change, str) else dict(change)
	frappe.has_permission("Shift Schedule Assignment", "write", throw=True)
	view_weeks = int(view_weeks)

	employees = _employees_of(discipline)
	if change.get("employee") not in employees:
		frappe.throw(
			frappe._("{0} does not hold a binding role in {1}.").format(change.get("employee"), discipline)
		)

	valid_branches = branches_of(discipline)
	to_shift_type = change.get("to_shift_type")
	to_branch = change.get("to_branch")
	if to_branch and to_shift_type and to_branch not in valid_branches.get(to_shift_type, []):
		frappe.throw(
			frappe._("{0} is not configured for {1} in {2}.").format(to_branch, to_shift_type, discipline)
		)

	start_date = _view_start(start)
	rotas = _rotas_by_branch(employees)
	draft = get_or_create_draft(discipline)
	prior = [_change_from_row(row) for row in draft.changes]
	effective_before = _effective_rotas(
		rotas, edit.apply_changes(rotas, prior, view_start=start_date, view_weeks=view_weeks)
	)

	from_assignment = change.get("from_assignment") or None
	if change["op"] in ("move", "remove"):
		if from_assignment and from_assignment.startswith("NEW-"):
			# A placeholder standing in for a not-yet-applied "add" (see _effective_rotas):
			# it has no real Shift Schedule Assignment for a chained edit to reference yet.
			frappe.throw(frappe._("Apply the draft before moving a shift you just added."))
		source = next((r for r in effective_before if r.assignment == from_assignment), None)
		if source is None:
			frappe.throw(frappe._("That pattern is no longer there — reload and try again."))
		if source.employee != change["employee"]:
			# The grid never offers this (a drag is scoped to its own row), so reaching
			# here means a stale or tampered request, not a legitimate reassignment.
			frappe.throw(frappe._("A drag may not move a shift to a different employee."))

	new_change = edit.Change(
		op=change["op"],
		employee=change["employee"],
		company=_company_of(change["employee"]) if change["op"] == "add" else None,
		to_shift_type=to_shift_type or None,
		to_weekday=WEEKDAY_INDEX.get(change.get("to_weekday")),
		to_branch=to_branch or None,
		to_phase=frappe.utils.cint(change.get("to_phase")),
		from_assignment=from_assignment,
		from_weekday=WEEKDAY_INDEX.get(change.get("from_weekday")),
		from_phase=frappe.utils.cint(change.get("from_phase")),
	)
	description = edit.describe_change(new_change, effective_before, view_weeks=view_weeks)

	draft.append(
		"changes",
		{
			"op": new_change.op.capitalize(),
			"employee": new_change.employee,
			"from_assignment": new_change.from_assignment,
			"from_weekday": change.get("from_weekday") or None,
			"from_phase": new_change.from_phase,
			"to_shift_type": new_change.to_shift_type,
			"to_weekday": change.get("to_weekday") or None,
			"to_phase": new_change.to_phase,
			"to_branch": new_change.to_branch,
			"description": description,
		},
	)
	draft.save(ignore_permissions=True)

	return get_state(discipline, start, view_weeks)


@frappe.whitelist()
def discard_draft(discipline: str, start: str, view_weeks: int | str) -> dict:
	"""Drop every staged change for `discipline` without touching the books."""
	draft = _existing_draft(discipline)
	if draft and draft.changes:
		draft.changes = []
		draft.save(ignore_permissions=True)
	return get_state(discipline, start, view_weeks)


@frappe.whitelist()
def apply_draft(discipline: str, start: str, view_weeks: int | str) -> dict:
	"""Commit the draft's `EditPlan`: delete the assignments it supersedes, create its
	replacements tagged `custom_manually_edited`, and clean up any private Shift Schedule
	an edit emptied out.

	`start`/`view_weeks` are the editor's current toolbar state — the same view every
	staged `Change`'s `from_phase`/`to_phase` was recorded against (see `edit.apply_changes`
	and `Change`), so Apply folds the batch exactly as the grid and transcript the planner
	is looking at right now already show it.

	Created assignments are `enabled = 0` / `shift_status = "Inactive"` like every rota
	this app materialises by hand — HRMS's own generator staying off them is exactly the
	point, see `autoshift.rota`. `create_shifts_after` is only ever a phase anchor here,
	never a boundary this app itself will later cross: nothing generated by this app ever
	reads it back except `cycle.occurrences`.
	"""
	frappe.has_permission("Shift Schedule Assignment", "write", throw=True)
	frappe.has_permission("Shift Schedule", "create", throw=True)

	draft = _existing_draft(discipline)
	if not draft or not draft.changes:
		return {"created": 0, "deleted": 0}

	employees = _employees_of(discipline)
	rotas = _rotas_by_branch(employees)
	changes = [_change_from_row(row) for row in draft.changes]
	plan = edit.apply_changes(rotas, changes, view_start=_view_start(start), view_weeks=int(view_weeks))

	# Clear the draft's rows before deleting anything they reference: each row's
	# `from_assignment` is a live Link, and Frappe refuses to delete a document another
	# one still points at.
	draft.changes = []
	draft.save(ignore_permissions=True)

	orphaned_schedules: set[str] = set()
	for name in plan.delete:
		schedule = frappe.db.get_value("Shift Schedule Assignment", name, "shift_schedule")
		if schedule:
			orphaned_schedules.add(schedule)
		frappe.delete_doc("Shift Schedule Assignment", name, ignore_permissions=True)

	created = []
	for new in plan.create:
		schedule = frappe.new_doc("Shift Schedule")
		schedule.name = f"Autoshift Manual {frappe.generate_hash(length=8)}"
		schedule.shift_type = new.shift_type
		schedule.frequency = FREQUENCY_LABEL[new.cycle_weeks]
		for weekday in sorted(new.weekdays):
			schedule.append("repeat_on_days", {"day": WEEKDAY_LABEL[weekday]})
		schedule.custom_manually_edited = 1
		schedule.insert(ignore_permissions=True)
		schedule.submit()

		assignment = frappe.new_doc("Shift Schedule Assignment")
		assignment.employee = new.employee
		assignment.company = new.company
		assignment.shift_schedule = schedule.name
		assignment.shift_location = _shift_location_for(discipline, new.branch) if new.branch else None
		assignment.enabled = 0
		assignment.shift_status = "Inactive"
		if new.anchor:
			assignment.create_shifts_after = new.anchor
		assignment.custom_manually_edited = 1
		assignment.insert(ignore_permissions=True)
		created.append(assignment.name)

	# A private (manually_edited) Shift Schedule an edit fully emptied is dead weight.
	# A shared, zawin2frappe-owned one is never touched here — only unlinked, by the
	# delete of this employee's own Shift Schedule Assignment row above.
	for schedule_name in orphaned_schedules:
		if frappe.db.exists("Shift Schedule Assignment", {"shift_schedule": schedule_name}):
			continue
		if not frappe.db.get_value("Shift Schedule", schedule_name, "custom_manually_edited"):
			continue
		doc = frappe.get_doc("Shift Schedule", schedule_name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Shift Schedule", schedule_name, ignore_permissions=True)

	return {"created": len(created), "deleted": len(plan.delete)}
