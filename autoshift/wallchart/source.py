# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""A week of scheduling data, read out of Frappe as `Slot` records.

Two sources, because the chart has to draw a week whether or not anything has
been solved for it — the point of an always-on view is that it is up before the
first run and still up after a failed one:

    Shift Assignment    what is actually in Frappe HR. Records **no role**, so
                        the role is inferred from what the employee holds and
                        every inferred slot is flagged rather than hidden.
    Optimizer Run       the run's `solution_table`. Authoritative: every slot
                        names the Scheduling Role it was assigned in.

`chart.merge` puts the two side by side, so a solved run reads as a diff against
the books — kept, added, dropped — rather than as a second unrelated picture.
"""

from __future__ import annotations

import datetime
from collections import defaultdict

import frappe

from . import layout
from .chart import KIND_ADDED, KIND_EXISTING, Slot, week_dates

#: `Employee.custom_initials` belongs to zawin2frappe (see CLAUDE.md, "Custom
#: fields"), so autoshift may read it but must never ship it. Sites without that
#: app get initials derived from the name instead.
INITIALS_FIELD = "custom_initials"


def short_label(employee: str, employee_name: str, initials: str | None) -> str:
	"""What a cell prints: a couple of characters, unambiguous in context.

	A wall chart has room for initials and nothing else. Where the site records
	them they are used as given; otherwise they are built from the name, which is
	what makes this work on a bench that has never seen the import.
	"""
	if initials:
		return initials
	if employee_name:
		parts = [p for p in employee_name.replace("-", " ").split() if p]
		if len(parts) >= 2:
			return (parts[0][0] + parts[-1][0]).upper()
		if parts:
			return parts[0][:2].upper()
	return employee


def _has_initials() -> bool:
	return bool(frappe.db.has_column("Employee", INITIALS_FIELD))


def _employees(names: set[str]) -> dict[str, dict]:
	if not names:
		return {}
	fields = ["name", "employee_name"]
	if _has_initials():
		fields.append(INITIALS_FIELD)
	rows = frappe.get_all(
		"Employee",
		filters={"name": ["in", list(names)]},
		fields=fields,
	)
	return {row["name"]: row for row in rows}


def _location_branches() -> dict[str, str | None]:
	"""Shift Location -> branch. The source of truth for a Shift Assignment's
	branch is `Shift Location.custom_branch` (CLAUDE.md, "Custom fields")."""
	return {
		row.name: row.custom_branch
		for row in frappe.get_all("Shift Location", fields=["name", "custom_branch"])
	}


def _location_disciplines() -> dict[str, str | None]:
	return {
		row.name: row.custom_discipline
		for row in frappe.get_all("Shift Location", fields=["name", "custom_discipline"])
	}


def _role_disciplines() -> dict[str, str | None]:
	return {
		row.name: row.discipline for row in frappe.get_all("Scheduling Role", fields=["name", "discipline"])
	}


def _held_roles(names: set[str]) -> dict[str, list[dict]]:
	"""Active Employee Scheduling Role rows per employee, validity window kept."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Employee Scheduling Role",
		filters={"employee": ["in", list(names)], "active": 1},
		fields=["employee", "scheduling_role", "max_rooms", "valid_from", "valid_to"],
	)
	held: dict[str, list[dict]] = defaultdict(list)
	for row in rows:
		held[row["employee"]].append(row)
	return held


def _in_window(row: dict, day: datetime.date) -> bool:
	if row.get("valid_from") and frappe.utils.getdate(row["valid_from"]) > day:
		return False
	to = row.get("valid_to")
	return not (to and frappe.utils.getdate(to) < day)


#: Sort key for a role that is no longer active, so it ranks after every role
#: the chart actually draws a lane for.
_UNRANKED = (2**31, 0, "")


def infer_role(
	candidates: list[str],
	discipline: str | None,
	role_disciplines: dict[str, str | None],
	role_order: dict[str, tuple],
) -> tuple[str | None, bool]:
	"""Pick the role an employee most likely worked. Returns (role, certain).

	A Shift Assignment names a Shift Location and a location names exactly one
	discipline, so the discipline narrows the field first — which settles most
	people outright. What survives is genuinely ambiguous (an assistant who also
	holds Sterilization) and is broken by `layout.lane_sort_key`, the same order
	the chart's lanes use, so the guess lands in the leftmost lane the employee
	could plausibly have worked rather than an arbitrary one. It is flagged
	uncertain either way and the chart draws it differently rather than
	pretending.
	"""
	if not candidates:
		return None, False
	narrowed = [r for r in candidates if role_disciplines.get(r) == discipline] if discipline else []
	pool = narrowed or candidates
	if len(pool) == 1:
		return pool[0], bool(narrowed) or len(candidates) == 1
	return sorted(pool, key=lambda r: (role_order.get(r, _UNRANKED), r))[0], False


def from_shift_assignments(monday: datetime.date, employees: list[str] | None = None) -> list[Slot]:
	"""Submitted Shift Assignments overlapping the week starting `monday`.

	Assignments are stored as date ranges. The ZaWin import writes one row per
	day, but hrms allows a span and a hand-entered one may well use it, so each
	assignment is expanded across the days of the week it actually covers.
	"""
	days = week_dates(monday)
	first, last = days[0], days[-1]
	filters: dict = {"docstatus": 1, "start_date": ["<=", last]}
	if employees is not None:
		if not employees:
			return []
		filters["employee"] = ["in", employees]
	rows = frappe.get_all(
		"Shift Assignment",
		filters=filters,
		or_filters=[["end_date", ">=", first], ["end_date", "is", "not set"]],
		fields=["employee", "shift_type", "start_date", "end_date", "shift_location"],
	)
	if not rows:
		return []

	names = {row["employee"] for row in rows}
	people = _employees(names)
	branches = _location_branches()
	disciplines = _location_disciplines()
	role_disciplines = _role_disciplines()
	role_order = layout.role_order()
	held = _held_roles(names)

	slots: list[Slot] = []
	for row in rows:
		location = row["shift_location"] or ""
		person = people.get(row["employee"], {})
		start = frappe.utils.getdate(row["start_date"])
		end = frappe.utils.getdate(row["end_date"]) if row["end_date"] else start
		for day in days:
			if not (start <= day <= end):
				continue
			candidates = [
				held_row["scheduling_role"]
				for held_row in held.get(row["employee"], [])
				if _in_window(held_row, day)
			]
			role, certain = infer_role(candidates, disciplines.get(location), role_disciplines, role_order)
			slots.append(
				Slot(
					date=day,
					shift_type=row["shift_type"],
					employee=row["employee"],
					employee_name=person.get("employee_name") or "",
					label=short_label(
						row["employee"],
						person.get("employee_name") or "",
						person.get(INITIALS_FIELD),
					),
					branch=branches.get(location),
					scheduling_role=role,
					kind=KIND_EXISTING,
					role_certain=certain,
				)
			)
	return slots


def from_optimizer_run(run_name: str, monday: datetime.date) -> list[Slot]:
	"""One Optimizer Run's proposed slots, clipped to the week."""
	rows = frappe.get_all(
		"Optimizer Run Slot",
		filters={"parent": run_name, "parenttype": "Optimizer Run"},
		fields=["employee", "scheduling_role", "shift_type", "date", "shift_location", "branch", "forced"],
	)
	if not rows:
		return []
	days = set(week_dates(monday))
	names = {row["employee"] for row in rows}
	people = _employees(names)
	branches = _location_branches()

	# Roles became a slot field partway through; a run solved before that has none
	# on any of its slots, and without this every one of them would land under
	# "Unplaced" — the chart would look broken rather than old. Inferred exactly as
	# a Shift Assignment's is, and flagged the same way.
	needs_inference = any(not row["scheduling_role"] for row in rows)
	disciplines = _location_disciplines() if needs_inference else {}
	role_disciplines = _role_disciplines() if needs_inference else {}
	role_order = layout.role_order() if needs_inference else {}
	held = _held_roles(names) if needs_inference else {}

	slots: list[Slot] = []
	for row in rows:
		day = frappe.utils.getdate(row["date"])
		if day not in days:
			continue
		person = people.get(row["employee"], {})
		role, certain = row["scheduling_role"], True
		if not role:
			candidates = [
				held_row["scheduling_role"]
				for held_row in held.get(row["employee"], [])
				if _in_window(held_row, day)
			]
			role, certain = infer_role(
				candidates,
				disciplines.get(row["shift_location"] or ""),
				role_disciplines,
				role_order,
			)
		slots.append(
			Slot(
				date=day,
				shift_type=row["shift_type"],
				employee=row["employee"],
				employee_name=person.get("employee_name") or "",
				label=short_label(
					row["employee"], person.get("employee_name") or "", person.get(INITIALS_FIELD)
				),
				# The run slot carries its own branch; the location is a fallback
				# for runs written before that field existed.
				branch=row["branch"] or branches.get(row["shift_location"] or ""),
				scheduling_role=role,
				kind=KIND_ADDED,
				forced=bool(row["forced"]),
				role_certain=certain,
			)
		)
	return slots


def leaves(monday: datetime.date, speculated: set[str] | None = None) -> dict[str, list[dict]]:
	"""Approved (plus optionally speculated) leave in the week, per ISO date.

	Not a `Slot`: somebody on leave is not in a chair, so they have no cell. They
	are the answer to "why is this chair empty", though, which is the whole point
	of the chart, so they are reported alongside it.
	"""
	days = week_dates(monday)
	first, last = days[0], days[-1]
	rows = frappe.get_all(
		"Leave Application",
		filters={"status": "Approved", "from_date": ["<=", last], "to_date": [">=", first]},
		fields=["name", "employee", "leave_type", "from_date", "to_date"],
	)
	if speculated:
		rows += frappe.get_all(
			"Leave Application",
			filters={"name": ["in", list(speculated)]},
			fields=["name", "employee", "leave_type", "from_date", "to_date"],
		)

	people = _employees({row["employee"] for row in rows})
	out: dict[str, list[dict]] = {}
	seen: set[str] = set()
	for row in rows:
		if row["name"] in seen:
			continue
		seen.add(row["name"])
		person = people.get(row["employee"], {})
		entry = {
			"employee": row["employee"],
			"employee_name": person.get("employee_name") or "",
			"label": short_label(
				row["employee"], person.get("employee_name") or "", person.get(INITIALS_FIELD)
			),
			"leave_type": row["leave_type"],
			"speculative": bool(speculated and row["name"] in speculated),
		}
		day = frappe.utils.getdate(row["from_date"])
		end = frappe.utils.getdate(row["to_date"])
		while day <= end:
			if day in days:
				out.setdefault(day.isoformat(), []).append(entry)
			day += datetime.timedelta(days=1)
	for entries in out.values():
		entries.sort(key=lambda e: (e["label"].upper(), e["employee"]))
	return out


def holidays(monday: datetime.date, mode: str = "Bounded") -> dict[str, str]:
	"""ISO date -> holiday description, for the week, from Optimizer Settings.

	The chart dims a non-working day rather than dropping the column, so an
	assignment that landed on one is still visible — which is exactly the kind of
	thing worth seeing.
	"""
	settings = frappe.get_single("Optimizer Settings")
	list_name = settings.get("unbounded_holiday_list" if mode == "Unbounded" else "bounded_holiday_list")
	if not list_name:
		return {}
	days = {day.isoformat() for day in week_dates(monday)}
	out: dict[str, str] = {}
	for row in frappe.get_all(
		"Holiday",
		filters={"parent": list_name, "parenttype": "Holiday List"},
		fields=["holiday_date", "description"],
	):
		iso = frappe.utils.getdate(row["holiday_date"]).isoformat()
		if iso in days:
			out[iso] = row["description"] or ""
	return out
