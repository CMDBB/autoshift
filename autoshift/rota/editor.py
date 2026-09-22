# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The DB half of the Rota Editor: turning `edit.py`'s pure `Change`/`EditPlan` into a
grid a browser can draw, a transcript it can read, and — on Apply — real documents.

Mirrors `materialize.py`'s split: the pure rule lives in `edit.py`, this module only
loads, calls it, and writes back. One `Rota Edit Draft` per (user, discipline) buffers a
session's edits (`get_or_create_draft`); every read here folds that buffer's `Change`s
onto the current `Shift Schedule Assignment`s before drawing anything, so the grid always
shows what Apply would produce, not just what is already on the books.

A `Change`'s `company` — needed for a fresh "add", and to seed a group that has no real
`Shift Schedule Assignment` yet (see `edit.Change`) — is never stored on the draft row;
`_change_from_row` fetches it fresh from `Employee.company` each time a row is turned into
a `Change`, so it doesn't need to survive a chain of edits on its own.
"""

from __future__ import annotations

import dataclasses
import datetime

import frappe

from . import edit, materialize
from .cycle import (
	FREQUENCY_LABEL,
	WEEKDAY_INDEX,
	WEEKDAY_LABEL,
	Rota,
	anchor_cutoff,
	backdated_anchor,
	monday_of,
	occurrences,
)
from .materialize import load_rotas


def _company_of(employee: str) -> str | None:
	return frappe.db.get_value("Employee", employee, "company")


def _role_discipline() -> dict[str, str]:
	return {
		r.name: r.discipline
		for r in frappe.get_all("Scheduling Role", filters={"active": 1}, fields=["name", "discipline"])
	}


def _role_candidates(employee: str, discipline: str, roles: materialize.RoleContext) -> list[str]:
	"""The Scheduling Roles a pattern of `employee`'s in `discipline` may be worked in.

	Every non-collateral role they hold there, binding or not — a collateral duty is
	never one of them, because a duty is an explicit editorial act rather than a way of
	spending a half-day. This is what an "add" picks from and what a "retag" may move an
	occurrence into, so it is also exactly the set the page offers the planner.
	"""
	return sorted(
		role for role in roles.working.get(employee, ()) if roles.role_discipline.get(role) == discipline
	)


def _default_role(
	employee: str, discipline: str, roles: materialize.RoleContext, candidates: list[str] | None = None
) -> str | None:
	"""The role a fresh pattern takes without asking anybody — or `None`, meaning ask.

	The same ladder `materialize.RoleContext.resolve` applies to a pattern already on the
	books, minus the rungs that need a record: the single role they hold in this
	discipline, else — holding several — the single one of those that is *binding* for
	them, since the settled week this editor draws is the binding role's week. `None`
	where neither settles it, which is the editor's cue to prompt rather than to invent.
	"""
	if candidates is None:
		candidates = _role_candidates(employee, discipline, roles)
	if len(candidates) == 1:
		return candidates[0]
	binding = [role for role in candidates if role in roles.binding.get(employee, ())]
	return binding[0] if len(binding) == 1 else None


def _role_labels(names: set[str]) -> dict[str, str]:
	"""Short chip badges for Scheduling Roles: initials of a multi-word name, else the
	first three characters. Only ever decoration — the full name is in every tooltip."""
	labels = {}
	for name in names:
		words = [word for word in str(name).replace("-", " ").split() if word]
		labels[name] = "".join(word[0] for word in words[:3]).upper() if len(words) > 1 else str(name)[:3]
	return labels


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


def _rotas_by_branch(employees: list[str], roles: materialize.RoleContext | None = None) -> list[Rota]:
	"""`materialize.load_rotas`, with `shift_location` (a `Shift Location` docname — the
	raw field `Shift Schedule Assignment.shift_location` actually stores) resolved to the
	**Branch** it belongs to (`Shift Location.custom_branch`).

	Everything in this module and in `edit.py` reasons in Branches — the vocabulary
	`Discipline Branch Config` and `branches_of` already use, and the one a planner drags
	a chip between. A Shift Location with no `custom_branch` set falls back to its own
	name, so it still draws as *something* rather than disappearing.
	"""
	rotas = load_rotas(set(employees), roles) if employees else []
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
			unconfirmed=r.unconfirmed,
			scheduling_role=r.scheduling_role,
			collateral_roles=r.collateral_roles,
			discipline=r.discipline,
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


#: How a draft row stores a pattern's collateral duties — a `Shift Schedule Assignment`
#: keeps them in a child table, but a staged `Change` only needs enough of them to find
#: the group again (see `edit.group_key`), and a single Data field does that.
COLLATERAL_SEPARATOR = ", "


def _split_collateral(value: str | None) -> tuple[str, ...]:
	return tuple(sorted(part.strip() for part in (value or "").split(",") if part.strip()))


def _change_from_row(row) -> edit.Change:
	op = row.op.lower()
	return edit.Change(
		op=op,
		employee=row.employee,
		company=_company_of(row.employee),
		scheduling_role=row.scheduling_role or None,
		to_shift_type=row.to_shift_type or None,
		to_weekday=WEEKDAY_INDEX.get(row.to_weekday),
		to_branch=row.to_branch or None,
		to_phase=frappe.utils.cint(row.to_phase),
		from_assignment=row.from_assignment or None,
		from_shift_type=row.from_shift_type or None,
		from_branch=row.from_branch or None,
		from_scheduling_role=row.from_scheduling_role or None,
		from_collateral_roles=_split_collateral(row.from_collateral_roles),
		from_weekday=WEEKDAY_INDEX.get(row.from_weekday),
		from_phase=frappe.utils.cint(row.from_phase),
	)


def _view_start(start) -> datetime.date:
	return monday_of(frappe.utils.getdate(start)) + datetime.timedelta(days=7)


def editor_start_for(day) -> str:
	"""The toolbar `start` value whose view opens on `day`'s week — the inverse of
	`_view_start`, for a link into the editor from elsewhere."""
	return (monday_of(frappe.utils.getdate(day)) - datetime.timedelta(days=7)).isoformat()


def unconfirmed_rotas(first=None, last=None) -> dict:
	"""Bound employees who still have a silver (imported, unconfirmed) pattern, per
	discipline — what a pre-solve warning points the planner at.

	With `first`/`last`, only patterns that put someone on at least one day of that span
	count: a silver rota the horizon never reaches cannot affect the run. A pattern is
	filed under **its own** discipline (`Rota.discipline`), so somebody holding binding
	roles in two disciplines is listed under the one whose week is actually unconfirmed,
	and the link the warning offers opens the Rota Editor where the pattern can be edited.
	A pattern nothing attributes falls back to every discipline they are bound in — it
	could be any of them, and each of those views may claim it (`is_native`).

	Cheap, like `materialize.pending`: configuration plus one rota read, no DataPackage.
	"""
	from autoshift.optimizer import data_loader

	role_discipline = _role_discipline()
	disciplines_of: dict[str, set[str]] = {}
	for employee, role in data_loader.configured_binding_pairs():
		if role in role_discipline:
			disciplines_of.setdefault(employee, set()).add(role_discipline[role])

	first = frappe.utils.getdate(first) if first else None
	last = frappe.utils.getdate(last) if last else None
	silver: dict[str, int] = {}
	by_discipline: dict[str, set[str]] = {}
	for rota in load_rotas(set(disciplines_of)) if disciplines_of else []:
		if not rota.unconfirmed:
			continue
		if first and last and not occurrences(rota, first, last):
			continue
		silver[rota.employee] = silver.get(rota.employee, 0) + 1
		for discipline in [rota.discipline] if rota.discipline else sorted(disciplines_of[rota.employee]):
			by_discipline.setdefault(discipline, set()).add(rota.employee)

	return {
		"patterns": sum(silver.values()),
		"employees": len(silver),
		"disciplines": [
			{"discipline": discipline, "employees": len(employees)}
			for discipline, employees in sorted(by_discipline.items())
		],
		"editor_start": editor_start_for(first) if first else None,
	}


def _effective_rotas(rotas: list[Rota], plan: edit.EditPlan, discipline: str | None = None) -> list[Rota]:
	"""What the grid should draw: the current rotas with the draft's plan folded in,
	its creations given placeholder names so a chip can still be dragged again before
	Apply ever runs. A staged promotion already draws gold, and so does every creation."""
	promoted = set(plan.promote)
	kept = [
		dataclasses.replace(r, unconfirmed=False) if r.assignment in promoted else r
		for r in rotas
		if r.assignment not in plan.delete
	]
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
			scheduling_role=new.scheduling_role,
			collateral_roles=new.collateral_roles,
			# Staged edits are only ever made from the discipline's own view, and
			# `stage_change` refuses one that touches another discipline's pattern, so
			# anything this plan creates belongs here by construction.
			discipline=discipline,
		)
		for index, new in enumerate(plan.create)
	]
	return kept + created


def is_native(rota: Rota, discipline: str) -> bool:
	"""May a view of `discipline` edit this pattern?

	Only its own discipline's, and anything genuinely **unattributed** — a pattern whose
	role could not be resolved and whose Shift Location names no discipline belongs to
	nobody in particular, so the view looking at it is as entitled to claim it as any
	other, and claiming it is how it acquires a role (see `apply_draft`). Everything
	else is another discipline's settled week: drawn, so a clash is visible, and
	read-only, so it can only be changed where it is owned.
	"""
	return rota.discipline is None or rota.discipline == discipline


def split_by_discipline(rotas: list[Rota], discipline: str) -> tuple[list[Rota], list[Rota]]:
	"""`(editable here, another discipline's)` — see :func:`is_native`.

	Only the first list is ever handed to `edit.apply_changes`. That is what makes a
	foreign pattern structurally untouchable rather than merely unoffered: two patterns
	that happen to share a `group_key` — an unattributed native one and an unattributed
	foreign one at the same shift type and branch — would otherwise be folded together
	and one of them deleted, however carefully `stage_change` validated the request.
	"""
	native = [rota for rota in rotas if is_native(rota, discipline)]
	return native, [rota for rota in rotas if not is_native(rota, discipline)]


def _cell_key(shift_type: str, day) -> str:
	return f"{shift_type}|{day if isinstance(day, str) else day.isoformat()}"


def _hidden_cells(emp_rotas: list[Rota], days: list[datetime.date], view_start: datetime.date) -> dict:
	"""A period-incompatible employee's read-only summary row: for each `(shift_type,
	day)` the view actually draws, the fraction of that shift's own cadence that puts
	them there — see `edit.phase_fractions` for why this is an average over a full
	cycle rather than the (possibly misleading) single phase this view happens to show.
	Sparse like a normal row's `cells` — a cell with zero occupancy is simply absent.
	`unconfirmed` is set when any silver pattern contributes to the cell.
	"""
	shift_types = sorted({r.shift_type for r in emp_rotas})
	weekdays = sorted({d.weekday() for d in days})
	cells: dict[str, dict] = {}
	for shift_type in shift_types:
		members = [r for r in emp_rotas if r.shift_type == shift_type]
		cycle_weeks = max((r.cycle_weeks for r in members), default=1)
		fractions = edit.phase_fractions(emp_rotas, shift_type, weekdays, view_start, cycle_weeks)
		for day in days:
			occupied, branch = fractions.get(day.weekday(), (0, None))
			if not occupied:
				continue
			on_day = [r for r in members if day.weekday() in r.weekdays]
			roles = {r.scheduling_role for r in on_day if r.scheduling_role}
			cells[_cell_key(shift_type, day)] = {
				"occupied": occupied,
				"cycle_weeks": cycle_weeks,
				"branch": branch,
				"role": roles.pop() if len(roles) == 1 else None,
				"unconfirmed": any(r.unconfirmed for r in on_day),
				"clash": False,
			}
	return cells


def _foreign_cells(emp_rotas: list[Rota], first: datetime.date, last: datetime.date) -> dict[str, dict]:
	"""Another discipline's settled half-days, drawn read-only in this view.

	Dated exactly, by `occurrences`, whatever the pattern's cadence — unlike the native
	rows, where a cadence the view cannot tile has to be summarised (`_hidden_cells`)
	because *editing* a misaligned pattern would distort it. Nothing here is editable, so
	the honest thing is simply the days it actually falls on.
	"""
	cells: dict[str, dict] = {}
	for rota in emp_rotas:
		for day in occurrences(rota, first, last):
			cells[_cell_key(rota.shift_type, day)] = {
				"branch": rota.shift_location,
				"discipline": rota.discipline,
				"role": rota.scheduling_role,
				"cycle_weeks": rota.cycle_weeks,
				"unconfirmed": rota.unconfirmed,
				"clash": False,
			}
	return cells


def _mark_clashes(cells: dict[str, dict], foreign: dict[str, dict], per_day: bool) -> None:
	"""Flag every half-day this employee is booked for in two disciplines at once.

	Under `HR Settings.allow_multiple_shift_assignments` a clash is the same shift type on
	the same day; with the setting off — HRMS's default, and the one `materialize._covered`
	already reads — a person has *one* shift a day, so any two rotas landing on one date
	collide however their shift types are labelled. Both sides are marked: the foreign chip
	is what turns red, but a planner needs to find its counterpart in their own rows.
	"""
	if not cells or not foreign:
		return
	native_keys = set(cells)
	for key, cell in foreign.items():
		date = key.split("|", 1)[1]
		hits = (
			[other for other in native_keys if other.split("|", 1)[1] == date]
			if per_day
			else ([key] if key in native_keys else [])
		)
		if not hits:
			continue
		cell["clash"] = True
		for other in hits:
			cells[other]["clash"] = True


@frappe.whitelist()
def get_state(discipline: str, start: str, view_weeks: int | str) -> dict:
	"""Everything the Rota Editor page needs to draw one discipline at one width: the
	grid (current books + draft folded in) and the transcript of what is staged.

	An employee holding binding roles in two disciplines has one settled week, not two,
	so both are drawn — but only this discipline's is editable (`is_native`). The rest
	arrive as `foreign_cells`, read-only, on whatever Shift Type they are worked on;
	`extra_shift_types` names the Shift Types this discipline's own config does not cover,
	drawn as read-only sections below it, so nothing is silently invisible and a
	double-booked half-day shows up as a clash rather than as an absence.

	Cheap enough to call after every stage/discard: a few small config tables, one rota
	read, and `edit.apply_changes` over what is usually a handful of rows.
	"""
	frappe.has_permission("Shift Schedule Assignment", throw=True)
	view_weeks = int(view_weeks)

	start_date = _view_start(start)
	end_date = start_date + datetime.timedelta(days=view_weeks * 7 - 1)
	days = [start_date + datetime.timedelta(days=i) for i in range(view_weeks * 7)]

	employees = _employees_of(discipline)
	roles = materialize.RoleContext(set(employees))
	rotas = _rotas_by_branch(employees, roles)

	native_rotas, foreign_rotas = split_by_discipline(rotas, discipline)
	draft = _existing_draft(discipline)
	staged = [_change_from_row(row) for row in (draft.changes if draft else [])]
	plan = edit.apply_changes(native_rotas, staged, view_start=start_date, view_weeks=view_weeks)
	effective = _effective_rotas(native_rotas, plan, discipline) + foreign_rotas

	by_employee: dict[str, list[Rota]] = {}
	for rota in effective:
		by_employee.setdefault(rota.employee, []).append(rota)

	names = (
		{
			e.name: {
				"employee_label": f"{e.name}:{e.custom_initials}" if e.custom_initials else e.name,
				"employee_name": e.employee_name,
			}
			for e in frappe.get_all(
				"Employee",
				filters={"name": ["in", employees]},
				fields=["name", "custom_initials", "employee_name"],
			)
		}
		if employees
		else {}
	)

	per_day = materialize.one_shift_per_day()
	own_shift_types = shift_types_of(discipline)
	own_names = {row.name for row in own_shift_types}
	extra_names: set[str] = set()
	used_roles: set[str] = set()

	visible, hidden = [], []
	for employee in employees:
		emp_rotas = by_employee.get(employee, [])
		native, outside = split_by_discipline(emp_rotas, discipline)
		foreign = _foreign_cells(outside, start_date, end_date)
		# Any Shift Type this discipline's own config does not cover, whoever books it:
		# `branches_of` lists no drop target for one, so it could never be edited here
		# anyway, and drawing it read-only beats the old behaviour of not drawing it.
		extra_names |= {r.shift_type for r in emp_rotas} - own_names
		used_roles |= {r.scheduling_role for r in emp_rotas if r.scheduling_role}
		candidates = _role_candidates(employee, discipline, roles)
		used_roles |= set(candidates)
		# Only this discipline's own patterns decide whether the row is editable at this
		# width: a foreign one is read-only anyway, and a fortnightly rota somewhere else
		# is no reason to stop a planner touching the weekly one here.
		unconfirmed = sum(1 for r in native if r.unconfirmed)
		bad_cadences = sorted(
			{r.cycle_weeks for r in native if not edit.rota_view_weeks(r.cycle_weeks, view_weeks)}
		)
		row = {
			"employee": employee,
			"unconfirmed": unconfirmed,
			"foreign_cells": foreign,
			"roles": candidates,
			"default_role": _default_role(employee, discipline, roles, candidates),
			**names[employee],
		}
		if bad_cadences:
			cells = _hidden_cells(native, days, start_date)
			_mark_clashes(cells, foreign, per_day)
			hidden.append({**row, "cycle_weeks": bad_cadences, "cells": cells})
			continue
		cells = {}
		for rota in native:
			for day in occurrences(rota, start_date, end_date):
				cells[_cell_key(rota.shift_type, day)] = {
					"branch": rota.shift_location,
					"assignment": rota.assignment,
					"cycle_weeks": rota.cycle_weeks,
					"unconfirmed": rota.unconfirmed,
					"role": rota.scheduling_role,
					"collateral_roles": list(rota.collateral_roles),
					"clash": False,
				}
		_mark_clashes(cells, foreign, per_day)
		visible.append({**row, "cells": cells})
	visible = sorted(visible, key=lambda e: int(e.get("employee")))

	extra_shift_types = (
		frappe.get_all(
			"Shift Type",
			filters={"name": ["in", sorted(extra_names)]},
			fields=["name", "start_time"],
			order_by="start_time asc",
		)
		if extra_names
		else []
	)

	return {
		"discipline": discipline,
		"start": start_date.isoformat(),
		# The banner: how many people here still have an imported pattern nobody has
		# confirmed. Counted on the draft-folded rotas, so a staged promotion or edit
		# already takes someone off it.
		"unconfirmed_employees": sum(1 for emp in visible + hidden if emp["unconfirmed"]),
		"view_weeks": view_weeks,
		"days": [{"date": d.isoformat(), "weekday": WEEKDAY_LABEL[d.weekday()]} for d in days],
		"shift_types": own_shift_types,
		# Shift Types these people are booked on that this discipline's config does not
		# cover — another discipline's, usually. Drawn below the discipline's own
		# sections and read-only throughout (`branches_of` offers no drop target for one),
		# so a half-day worked on an unfamiliar shift is never simply invisible.
		"extra_shift_types": extra_shift_types,
		"branches": branches_of(discipline),
		"role_labels": _role_labels(used_roles),
		"one_shift_per_day": per_day,
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
	from_weekday, to_shift_type, to_weekday, to_branch, scheduling_role}` (a "promote"
	carries only `{op, employee}`), weekdays as `Assignment Rule
	Day` labels ("Monday" etc — the grid's own day columns already carry that label, so
	the browser never has to think in a different weekday numbering than the one it drew).

	Two things are refused here rather than merely not offered by the grid: an edit to a
	pattern belonging to **another discipline** (`is_native` — that week is settled where
	it is owned, and this view would re-role it by rewriting it), and a `scheduling_role`
	the employee does not hold in this discipline.
	"""
	change = frappe.parse_json(change) if isinstance(change, str) else dict(change)
	frappe.has_permission("Shift Schedule Assignment", "write", throw=True)
	view_weeks = int(view_weeks)

	employees = _employees_of(discipline)
	if change.get("employee") not in employees:
		frappe.throw(
			frappe._("{0} does not hold a binding role in {1}.").format(change.get("employee"), discipline)
		)
	employee = change["employee"]

	valid_branches = branches_of(discipline)
	to_shift_type = change.get("to_shift_type")
	to_branch = change.get("to_branch")
	if to_branch and to_shift_type and to_branch not in valid_branches.get(to_shift_type, []):
		frappe.throw(
			frappe._("{0} is not configured for {1} in {2}.").format(to_branch, to_shift_type, discipline)
		)

	# Scoped to the whole grid, not just this employee: the same context resolves every
	# rota `_rotas_by_branch` is about to read, and one built for a single person would
	# leave everybody else's patterns unattributed.
	roles = materialize.RoleContext(set(employees))
	candidates = _role_candidates(employee, discipline, roles)
	scheduling_role = change.get("scheduling_role") or None
	if scheduling_role and scheduling_role not in candidates:
		frappe.throw(
			frappe._("{0} does not hold Scheduling Role {1} in {2}.").format(
				employee, scheduling_role, discipline
			)
		)

	start_date = _view_start(start)
	native_rotas, foreign_rotas = split_by_discipline(_rotas_by_branch(employees, roles), discipline)
	draft = get_or_create_draft(discipline)
	prior = [_change_from_row(row) for row in draft.changes]
	effective_before = (
		_effective_rotas(
			native_rotas,
			edit.apply_changes(native_rotas, prior, view_start=start_date, view_weeks=view_weeks),
			discipline,
		)
		+ foreign_rotas
	)

	if change["op"] == "promote" and not any(
		r.unconfirmed for r in effective_before if r.employee == employee and is_native(r, discipline)
	):
		frappe.throw(frappe._("{0} has no unconfirmed pattern left to promote.").format(employee))

	from_assignment = change.get("from_assignment") or None
	source = None
	if change["op"] in ("move", "remove", "retag"):
		# `effective_before` includes not-yet-applied placeholders (see `_effective_rotas`),
		# so this also resolves a chip that is itself still pending — a chip stays
		# draggable throughout, not just once its own Shift Schedule Assignment exists.
		source = next((r for r in effective_before if r.assignment == from_assignment), None)
		if source is None:
			frappe.throw(frappe._("That pattern is no longer there — reload and try again."))
		if source.employee != employee:
			# The grid never offers this (a drag is scoped to its own row), so reaching
			# here means a stale or tampered request, not a legitimate reassignment.
			frappe.throw(frappe._("A drag may not move a shift to a different employee."))
		if not is_native(source, discipline):
			frappe.throw(
				frappe._("{0} is worked in {1}, not {2}. Open the Rota Editor on {1} to change it.").format(
					source.shift_type, source.discipline, discipline
				)
			)

	if change["op"] == "retag":
		if not scheduling_role:
			frappe.throw(frappe._("Changing the role of a shift needs a Scheduling Role to change it to."))
		if source is not None and source.scheduling_role == scheduling_role:
			frappe.throw(frappe._("That shift is already worked as {0}.").format(scheduling_role))
	elif change["op"] == "add" and not scheduling_role:
		# Nothing was chosen (the page only prompts when the ladder cannot settle it, and
		# an older page never sends one), so settle it the way everything else does.
		scheduling_role = _default_role(employee, discipline, roles, candidates)

	new_change = edit.Change(
		op=change["op"],
		employee=employee,
		company=_company_of(employee),
		scheduling_role=scheduling_role if change["op"] in ("add", "retag") else None,
		to_shift_type=to_shift_type or None,
		to_weekday=WEEKDAY_INDEX.get(change.get("to_weekday")),
		to_branch=to_branch or None,
		to_phase=frappe.utils.cint(change.get("to_phase")),
		from_assignment=from_assignment,
		from_shift_type=source.shift_type if source else None,
		from_branch=source.shift_location if source else None,
		from_scheduling_role=source.scheduling_role if source else None,
		from_collateral_roles=tuple(source.collateral_roles) if source else (),
		from_weekday=WEEKDAY_INDEX.get(change.get("from_weekday")),
		from_phase=frappe.utils.cint(change.get("from_phase")),
	)
	description = edit.describe_change(new_change, effective_before, view_weeks=view_weeks)

	# A placeholder's `from_assignment` ("NEW-…", see `_effective_rotas`) names no real
	# Shift Schedule Assignment — a Link field can't store it. `from_shift_type`/
	# `from_branch`/`from_scheduling_role`/`from_collateral_roles` above are what a chained
	# edit resolves the pattern through instead (edit.apply_changes), so the row still
	# folds correctly without it.
	persisted_from_assignment = (
		None if from_assignment and from_assignment.startswith("NEW-") else from_assignment
	)

	draft.append(
		"changes",
		{
			"op": new_change.op.capitalize(),
			"employee": new_change.employee,
			"from_assignment": persisted_from_assignment,
			"from_shift_type": new_change.from_shift_type,
			"from_branch": new_change.from_branch,
			"from_scheduling_role": new_change.from_scheduling_role,
			"from_collateral_roles": COLLATERAL_SEPARATOR.join(new_change.from_collateral_roles),
			"from_weekday": change.get("from_weekday") or None,
			"from_phase": new_change.from_phase,
			"to_shift_type": new_change.to_shift_type,
			"to_weekday": change.get("to_weekday") or None,
			"to_phase": new_change.to_phase,
			"to_branch": new_change.to_branch,
			"scheduling_role": new_change.scheduling_role,
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
	replacements (gold, i.e. without `custom_unconfirmed`), clear the flag on every
	assignment a "promote" confirms, and clean up any private Shift Schedule an edit
	emptied out.

	`start`/`view_weeks` are the editor's current toolbar state — the same view every
	staged `Change`'s `from_phase`/`to_phase` was recorded against (see `edit.apply_changes`
	and `Change`), so Apply folds the batch exactly as the grid and transcript the planner
	is looking at right now already show it.

	A replacement that still names no Scheduling Role — an unattributed pattern this view
	has just claimed (`is_native`) — takes the discipline's own `_default_role` here, which
	is how a rota imported before the field existed acquires one simply by being edited.

	Created assignments are `enabled = 0` / `shift_status = "Inactive"` like every rota
	this app materialises by hand — HRMS's own generator staying off them is exactly the
	point, see `autoshift.rota`. `create_shifts_after` is only ever a phase anchor here,
	and is set at least `cycle.ANCHOR_LEAD_WEEKS` before today (moved back by whole
	cycles, so the phase is unchanged): a boundary in the future would leave the pattern
	missing from exactly the weeks a planner and a solve look at first.
	"""
	frappe.has_permission("Shift Schedule Assignment", "write", throw=True)
	frappe.has_permission("Shift Schedule", "create", throw=True)

	draft = _existing_draft(discipline)
	if not draft or not draft.changes:
		return {"created": 0, "deleted": 0, "confirmed": 0}

	employees = _employees_of(discipline)
	roles = materialize.RoleContext(set(employees))
	native_rotas, _foreign = split_by_discipline(_rotas_by_branch(employees, roles), discipline)
	changes = [_change_from_row(row) for row in draft.changes]
	plan = edit.apply_changes(
		native_rotas, changes, view_start=_view_start(start), view_weeks=int(view_weeks)
	)

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
		# The role the pattern is worked in, carried over from whatever this replaces
		# (`edit.apply_changes`) so an edit to somebody's days never silently changes
		# what they do on them. `materialize` copies it onto every Shift Assignment.
		#
		# Still blank only for a pattern that had no role to carry and no choice to
		# record — an unattributed import this view has just claimed. Writing the
		# discipline's own answer here is the point at which "even retroactively" becomes
		# true: touching a pattern is what settles what it is worked as.
		assignment.custom_scheduling_role = new.scheduling_role or _default_role(
			new.employee, discipline, roles
		)
		for role in new.collateral_roles:
			assignment.append("custom_collateral_roles", {"scheduling_role": role})
		if new.anchor:
			# `edit` anchors on the view the planner happens to be looking at, which may be
			# this week or later; pulled back by whole cycles so the pattern also covers the
			# weeks around now, in the phase the planner laid it out in.
			assignment.create_shifts_after = backdated_anchor(
				new.anchor, new.cycle_weeks, anchor_cutoff(frappe.utils.getdate(frappe.utils.today()))
			)
		assignment.insert(ignore_permissions=True)
		created.append(assignment.name)

	for name in plan.promote:
		frappe.db.set_value("Shift Schedule Assignment", name, "custom_unconfirmed", 0)

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

	return {"created": len(created), "deleted": len(plan.delete), "confirmed": len(plan.promote)}
