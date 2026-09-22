# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The week chart as a payload the browser can draw without deciding anything.

Everything needing a judgement — which band, which row, which lane, whether a run
kept or dropped a half-day — is settled here, so `wall_chart.js` is a renderer
and nothing more. The shape is nested to match the drawing order:

    {
      "week": "2026-08-31",  "prev_week": …,  "next_week": …,
      "days":     [{date, weekday, holiday, working, in_window}, …7],
      "sections": [{shift_type, title,
                    bands: [{key, discipline, branch, numbered, rooms, height,
                             covered: [rooms open, …7],
                             lanes: [{key, label, gates_rooms}],
                             rows: [ [ [cell|null|SPANNED, …7], …lanes ], …height ] }]}],
      "leaves":   {"YYYY-MM-DD": [{employee, label, leave_type, speculative}]},
      "warnings": [str],
      "totals":   {staffed, capacity, kept, added, dropped},
      "pending_bound": {first_day, last_day, count, employees, employee_names},
      "run":      {name, status, mode, date, first_day, last_day, compared} | null,
    }

`pending_bound` is what a settled schedule says this week holds but no
`Shift Assignment` records — see `autoshift.rota`. Those days are drawn anyway, as
`virtual` cells, because the optimizer reads them as on the books; the summary
backs an on-demand "Create them" for a planner who wants them recorded.

`rows[row][lane][day]` is a cell, null, or `SPANNED`. A cell carries `span`: the
rooms that person covers, drawn as `<td rowspan>`, so the lines it swallows are
`SPANNED` and the renderer emits no cell for them at all. Null is a room nobody
is in, which is the thing the chart exists to show.

`covered` is how many of a band's rooms are genuinely open on each weekday — the
lines every gating lane reaches. Rows past it are staffed by somebody but not by
everybody the room needs, and the chart greys them: a half-staffed room is not an
open room. It is the chart's own arithmetic, from the same minimum-over-roles the
solver's `room_coverage` applies, so the picture and the numbers cannot drift.
"""

from __future__ import annotations

import datetime

import frappe

from . import layout as layout_mod
from . import source
from .chart import KIND_ADDED, KIND_DROPPED, KIND_KEPT, OVERFLOW, build, merge, monday_of, week_dates

#: Weekday names are the browser's job (it has the user's locale); the payload
#: only says which weekday a column is, and whether it is a working day.
WEEKENDS = (5, 6)


def _resolve_week(week: str | None, run_doc=None) -> datetime.date:
	if week:
		return monday_of(frappe.utils.getdate(week))
	if run_doc and run_doc.get("date"):
		return monday_of(frappe.utils.getdate(run_doc.date))
	return monday_of(frappe.utils.getdate(frappe.utils.today()))


#: Marks a line swallowed by the `rowspan` of a cell above it. Distinct from
#: null, which is an empty room the chart must still draw.
SPANNED = "spanned"


def _cell(slot, span: int) -> dict:
	return {
		"span": span,
		"employee": slot.employee,
		"employee_name": slot.employee_name,
		"label": slot.label,
		"kind": slot.kind,
		"role": slot.scheduling_role,
		"branch": slot.branch,
		"forced": slot.forced,
		"uncertain": not slot.role_certain,
		"changed": slot.changed,
		"virtual": slot.virtual,
		# only where the run measured a load below the holder's ceiling
		"max_rooms": slot.max_rooms if slot.max_rooms > slot.rooms else 0,
	}


def _index(chart) -> dict[tuple, dict | str]:
	"""(section, band, row, lane, day) -> cell, or SPANNED. One pass, not a scan per cell."""
	cells: dict[tuple, dict | str] = {}
	for p in chart.placements:
		cells[(p.section, p.band, p.row, p.lane, p.day_index)] = _cell(p.slot, p.span)
		for offset in range(1, p.span):
			cells[(p.section, p.band, p.row + offset, p.lane, p.day_index)] = SPANNED
	return cells


def _band_payload(chart, cells, band_key, shift_type, lanes, rooms) -> dict:
	height = chart.height(shift_type, band_key)
	rows = [
		[[cells.get((shift_type, band_key, row, lane.key, day)) for day in range(7)] for lane in lanes]
		for row in range(1, height + 1)
	]
	return {
		"key": band_key,
		"rooms": rooms,
		"height": height,
		"covered": [chart.covered_rooms(shift_type, band_key, day) for day in range(7)],
		"lanes": [{"key": lane.key, "label": lane.label, "gates_rooms": lane.gates_rooms} for lane in lanes],
		"rows": rows,
	}


def _planning_window(run_doc) -> tuple[str | None, str | None]:
	"""The run's own horizon, so a day it never considered reads as out of scope
	rather than as a day it declined to staff."""
	if not run_doc or not run_doc.get("date"):
		return None, None
	from autoshift.optimizer import types

	try:
		window = types.planning_days(frappe.utils.getdate(run_doc.date), run_doc.mode)
	except (NotImplementedError, IndexError):
		return None, None
	return (window[0].isoformat(), window[-1].isoformat()) if window else (None, None)


@frappe.whitelist()
def get_week_chart(week: str | None = None, run: str | None = None, mode: str = "Bounded") -> dict:
	"""The wall chart for one week, optionally diffed against an Optimizer Run.

	`run` may name a run in any state. An unsolved or failed one contributes no
	slots and the chart falls back to the Shift Assignments on the books — which
	is the whole reason this view is always on: the week a solve failed for is
	exactly the week somebody needs to look at.
	"""
	frappe.has_permission("Shift Assignment", throw=True)

	run_doc = None
	if run:
		run_doc = frappe.get_doc("Optimizer Run", run)
		run_doc.check_permission("read")
		mode = run_doc.mode or mode

	monday = _resolve_week(week, run_doc)
	week_days = week_dates(monday)
	speculated = (
		{r.leave_application for r in (run_doc.get("leaves_speculations") or [])} if run_doc else set()
	)
	week_leaves = source.leaves(monday, speculated)
	pending = _pending_bound(week_days[0], week_days[-1])
	existing = source.from_shift_assignments(monday) + source.from_settled_rotas(
		monday, _off_leave(pending.pop("rows"), week_leaves)
	)
	proposed = source.from_optimizer_run(run, monday) if run_doc else []

	structure = layout_mod.derive()
	chart = build(structure, merge(existing, proposed), monday)
	cells = _index(chart)

	holidays = source.holidays(monday, mode)
	first_day, last_day = _planning_window(run_doc)

	days = []
	for day in week_days:
		iso = day.isoformat()
		days.append(
			{
				"date": iso,
				"weekday": day.weekday(),
				"holiday": holidays.get(iso),
				"working": day.weekday() not in WEEKENDS and iso not in holidays,
				"in_window": bool(first_day and last_day and first_day <= iso <= last_day),
			}
		)

	sections = []
	for section in structure.sections:
		bands = []
		for band in structure.bands:
			if section.shift_type not in band.shift_types:
				continue
			bands.append(
				_band_payload(chart, cells, band.key, section.shift_type, band.lanes, band.rooms)
				| {
					"discipline": band.discipline_label,
					"branch": band.branch_label,
					"numbered": True,
					"overflow": False,
				}
			)
		if chart.height(section.shift_type, OVERFLOW):
			bands.append(
				_band_payload(chart, cells, OVERFLOW, section.shift_type, chart.overflow_lanes, 0)
				| {
					"discipline": structure.overflow_label,
					"branch": "",
					"numbered": False,
					"overflow": True,
				}
			)
		sections.append({"shift_type": section.shift_type, "title": section.title, "bands": bands})

	return {
		"week": monday.isoformat(),
		"prev_week": (monday - datetime.timedelta(days=7)).isoformat(),
		"next_week": (monday + datetime.timedelta(days=7)).isoformat(),
		"days": days,
		"sections": sections,
		"leaves": week_leaves,
		"warnings": _warnings(chart, structure),
		"totals": _totals(chart, structure, days),
		"pending_bound": pending,
		"run": (
			{
				"name": run_doc.name,
				"status": run_doc.status,
				"mode": run_doc.mode,
				"date": str(run_doc.date) if run_doc.date else None,
				"first_day": first_day,
				"last_day": last_day,
				# False when the run has no solution to compare against, so the
				# chart can say it is showing the books rather than a proposal.
				"compared": bool(proposed),
			}
			if run_doc
			else None
		),
	}


def _pending_bound(first, last) -> dict:
	"""Settled schedules this week needs that nothing on the books records.

	The rows are drawn as virtual chips (and popped off before the payload leaves);
	the summary backs the on-demand "Create them". See `autoshift.rota` for why HRMS
	is not doing this itself.
	"""
	from autoshift.rota import materialize as rota

	return rota.pending(first, last)


def _off_leave(rows: list[dict], week_leaves: dict[str, list[dict]]) -> list[dict]:
	"""Drop rota days the employee is on leave for — leave wins, as it does in the loader."""
	away = {(entry["employee"], day) for day, entries in week_leaves.items() for entry in entries}
	return [row for row in rows if (row["employee"], row["date"]) not in away]


def _warnings(chart, structure) -> list[str]:
	warnings = list(chart.warnings)
	if not structure.bands:
		warnings.append(
			"no Discipline Branch Config exists, so the chart has no bands to draw — configure "
			"rooms and Shift Types per (discipline, branch) first"
		)
	unconfigured = layout_mod.unconfigured_disciplines()
	if unconfigured:
		warnings.append(
			"a Scheduling Role names these disciplines but no Discipline Branch Config covers "
			"them, so their holders can never be placed: " + ", ".join(unconfigured)
		)
	return warnings


def _totals(chart, structure, days: list[dict]) -> dict:
	"""Rooms open against rooms configured, for the week actually drawn.

	Counted off the same coverage the chart greys by rather than off `Optimizer
	Run Coverage`, because a headline that disagreed with the picture under it
	would be worse than no headline. A half-staffed room counts for nothing here,
	exactly as it counts for nothing in the solver's `active_rooms`. Capacity
	counts working days only: nothing in the configuration claims a Sunday room,
	so counting one would make every full week look two-sevenths empty.
	"""
	kinds = {KIND_KEPT: 0, KIND_ADDED: 0, KIND_DROPPED: 0}
	working = {index for index, day in enumerate(days) if day["working"]}
	for placement in chart.placements:
		kinds[placement.slot.kind] = kinds.get(placement.slot.kind, 0) + 1
	staffed = sum(
		chart.covered_rooms(section.shift_type, band.key, day)
		for section in structure.sections
		for band in structure.bands
		if section.shift_type in band.shift_types
		for day in working
	)
	capacity = sum(
		band.rooms * len(working)
		for section in structure.sections
		for band in structure.bands
		if section.shift_type in band.shift_types
	)
	return {"staffed": staffed, "capacity": capacity, **kinds}
