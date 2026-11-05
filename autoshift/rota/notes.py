# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Planner-written footnotes on a rota pattern.

A `Rota Day Note` says something about one weekday of one `Shift Schedule
Assignment` — "every Wednesday Sarah covers for Jan" — without touching
anything the optimizer reads: no Shift Assignment, no Shift Schedule
Assignment of its own, nothing in `DataPackage`. It exists for the case the
rest of this app is not yet built for: one person's rota genuinely differs
from the pattern for a recurring day, and writing a sweeping rule just to
explain it would be overkill. The wall chart draws it as a footnote
(`autoshift/wallchart/CLAUDE.md`); the Rota Editor's right-click (via
`rota.editor.set_note`) is the only way to write one.

**Tied to the rota, not to the day it was written on.** A note is keyed by
(`shift_schedule_assignment`, `weekday`) rather than by a calendar date, so it
reads back on every future occurrence of that weekday — exactly the dates
`cycle.occurrences` already computes off the same pattern's own anchor, cycle
and `until`. Edit the pattern (`rota/edit.py`'s "superseded, not deleted") and
the note goes with it: the Shift Schedule Assignment it names stops covering
any days, so `for_window` stops expanding it, which is the right outcome for
a note describing a pattern that itself no longer runs.
"""

from __future__ import annotations

import datetime

import frappe

from .cycle import FREQUENCY_WEEKS, WEEKDAY_INDEX, Rota, occurrences


def _getdate(value) -> datetime.date | None:
	return frappe.utils.getdate(value) if value else None


def _assignment_info(names: set[str]) -> dict[str, dict]:
	"""`Shift Schedule Assignment` name -> just enough of its own schedule to
	replay which days it covers: its employee, Shift Type, and the
	(cycle_weeks, anchor, until) triple `cycle.occurrences` reads off a `Rota`.

	Mirrors `materialize.load_rotas` in miniature rather than calling it: a note
	needs none of that function's role resolution, and staying independent of
	`RoleContext` / `optimizer.data_loader` keeps this module as uninvolved with
	the optimizer as the note itself is.
	"""
	if not names:
		return {}
	print(names)
	rows = frappe.get_all(
		"Shift Schedule Assignment",
		filters={"name": ["in", sorted(names)]},
		fields=["name", "employee", "shift_schedule", "create_shifts_after", "custom_create_shifts_until"],
	)
	if not rows:
		return {}
	schedules = {
		s.name: s
		for s in frappe.get_all(
			"Shift Schedule",
			filters={"name": ["in", list({row.shift_schedule for row in rows if row.shift_schedule})]},
			fields=["name", "shift_type", "frequency"],
		)
	}
	out = {}
	for row in rows:
		schedule = schedules.get(row.shift_schedule)
		if not schedule:
			continue
		out[row.name] = {
			"employee": row.employee,
			"shift_type": schedule.shift_type,
			"cycle_weeks": FREQUENCY_WEEKS.get(schedule.frequency, 1),
			"anchor": _getdate(row.create_shifts_after),
			"until": _getdate(row.custom_create_shifts_until),
		}
	return out


def for_window(
	employees: set[str] | list[str], first: datetime.date, last: datetime.date
) -> dict[tuple[str, str, str], str]:
	"""(employee, date, shift_type) -> note text, for every day in `[first, last]`
	some `Rota Day Note` covers.

	Read by both the Rota Editor (to draw a marker on the chip it belongs to)
	and the wall chart (to draw the footnote) — the same lookup either way,
	since neither cares how the note got there. Returns the same shape a
	per-day note would have, so callers never need to know a note is actually
	stored per *weekday of a pattern* rather than per day.
	"""
	if not employees:
		return {}
	rows = frappe.get_all(
		"Rota Day Note",
		filters={"employee": ["in", sorted(employees)]},
		fields=["shift_schedule_assignment", "weekday", "note"],
	)
	if not rows:
		return {}
	info = _assignment_info({row.shift_schedule_assignment for row in rows})
	out: dict[tuple[str, str, str], str] = {}
	for row in rows:
		meta = info.get(row.shift_schedule_assignment)
		weekday = WEEKDAY_INDEX.get(row.weekday)
		if not meta or weekday is None:
			continue
		rota = Rota(
			assignment=row.shift_schedule_assignment,
			employee=meta["employee"],
			company="",
			shift_type=meta["shift_type"],
			shift_location=None,
			weekdays=frozenset({weekday}),
			cycle_weeks=meta["cycle_weeks"],
			anchor=meta["anchor"],
			until=meta["until"],
		)
		for day in occurrences(rota, first, last):
			out[(meta["employee"], day.isoformat(), meta["shift_type"])] = row.note
	return out


def set_note(shift_schedule_assignment: str, weekday: str, note: str) -> None:
	"""Write, change or clear the note on one weekday of `shift_schedule_assignment`.

	A blank `note` deletes the row. No permission check of its own —
	`rota.editor.set_note` gates on the same Shift Schedule Assignment write
	permission every other right-click edit uses, since this still only ever
	runs from behind that menu.
	"""
	name = frappe.db.get_value(
		"Rota Day Note", {"shift_schedule_assignment": shift_schedule_assignment, "weekday": weekday}
	)
	note = (note or "").strip()
	if not note:
		if name:
			frappe.delete_doc("Rota Day Note", name, ignore_permissions=True)
		return
	if name:
		frappe.db.set_value("Rota Day Note", name, "note", note)
		return
	doc = frappe.new_doc("Rota Day Note")
	doc.shift_schedule_assignment = shift_schedule_assignment
	doc.weekday = weekday
	doc.note = note
	doc.insert(ignore_permissions=True)
