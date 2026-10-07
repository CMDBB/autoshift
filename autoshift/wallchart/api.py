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
                             open_rows: [[rows open, …], …7],
                             lanes: [{key, label, gates_rooms}],
                             rows: [ [ [cell|null, …7], …lanes ], …height ] }],
                    leaves: [[{employee, label, leave_type, …}, …], …7]}],
      "leaves_unknown": [[leave entry, …], …7],
      "filters":  {disciplines: {options: [{value, label}], selected: [str]},
                   branches:    {options: […],             selected: [str]},
                   weekdays:    {selected: [0‥6]},
                   hidden: int},
      "warnings": [str],
      "totals":   {staffed, capacity, kept, added, dropped},
      "pending_bound": {first_day, last_day, count, employees, employee_names},
      "run":      {name, status, mode, date, first_day, last_day, compared} | null,
    }

`pending_bound` is what a settled schedule says this week holds but no
`Shift Assignment` records — see `autoshift.rota`. Those days are drawn anyway, as
`virtual` cells, because the optimizer reads them as on the books; the summary
backs an on-demand "Create them" for a planner who wants them recorded.

`rows[row][lane][day]` is a cell or null. One cell is one chip on one line, so
somebody covering two rooms appears as two cells — see `chart._fill`; `rooms` on the
cell is how many they cover in all, for the tooltip. Null is a room nobody is in,
which is the thing the chart exists to show.

A leave entry is filed under the Shift Type of the half it empties, so a section's
`leaves` sit under that section's own table — morning leave below the morning
chart — and `leaves_unknown` collects the ones nothing could attribute to a half
(see `source._expected_shifts`). The same entry appears under both halves of a
day somebody works twice: they are away for both.

`filters` is the reader's own narrowing, the one view preference in the payload.
`weekdays` is which weekdays the practice works — the reader's answer where they gave
one, the calendar's where it has one, and Mon-Fri where neither does; it decides which
columns read as working, what the headline counts against, and which days a leave can
have emptied (see `_default_worked`).
`options` are every discipline and branch there is a band for, *unfiltered* — the
picker has to keep offering what the current selection hid — and `hidden` counts
the half-days set aside, because a filter may be explicit but a quietly dropped
person never is.

`open_rows` is *which* of a band's rows are genuinely open on each weekday — the
lines every gating lane staffs. Any other row is empty, or staffed by somebody but
not by everybody the room needs, and the chart greys it: a half-staffed room is not
an open room. Rows rather than a count, because under `room_coverage_matched_rooms`
a room nobody was matched into stays a hole (see `chart.py`) and the open ones need
not start at the top. It is the chart's own arithmetic, from the same
minimum-over-roles the solver's `room_coverage` applies, so the picture and the
numbers cannot drift.
"""

from __future__ import annotations

import datetime

import frappe

from . import layout as layout_mod
from . import source
from .chart import (
	KIND_ADDED,
	KIND_DROPPED,
	KIND_KEPT,
	OVERFLOW,
	bucket_leaves,
	build,
	merge,
	monday_of,
	week_dates,
)

#: Weekday names are the browser's job (it has the user's locale); the payload
#: only says which weekday a column is, and whether it is a working day.
#:
#: Saturday and Sunday, as the **default of a visible control only** — see
#: :func:`_default_worked`. Which days are worked is a fact the company's Holiday List
#: carries, not one this file gets to assert, and a practice that starts opening on a
#: Saturday must not have to come here to say so.
WEEKENDS = (5, 6)


def _default_worked(weekly_offs: set[int], calendar: bool) -> set[int]:
	"""Which weekdays the practice works, before the reader says otherwise.

	The calendar's answer wherever it has one: an ERPNext `Holiday List` carries its
	weekly offs as dated `Holiday` rows, which is the same reading
	`data_loader._availability` takes. Generate the list without Saturday and the chart
	opens on Saturdays, with no code change anywhere.

	`WEEKENDS` is what a calendar that *names no weekly off at all* falls back to — a
	list nobody has generated them for, or a genuinely seven-day practice, and nothing
	distinguishes the two. It is the old hardcoded rule, and it is kept **only as the
	default of a control the reader can see and change** (`filters.weekdays`), rather
	than as a rule buried here: a site that works every day says so with one click, and a
	site still staging towards weekend work gets the week it actually runs in the
	meantime. The honest answer is still to put the weekly offs in the Holiday List,
	which is the only one a solve will ever read.
	"""
	if calendar and weekly_offs:
		return {weekday for weekday in range(7) if weekday not in weekly_offs}
	return {weekday for weekday in range(7) if weekday not in WEEKENDS}


def _weekdays(raw) -> set[int]:
	"""The weekday control as it arrives from the browser.

	Parsed apart from `_selection` because **Monday is 0**, and 0 is the one value a
	"drop anything falsy" filter would quietly eat.
	"""
	if not raw:
		return set()
	values = frappe.parse_json(raw) if isinstance(raw, str) else raw
	return {int(value) for value in values if 0 <= int(value) <= 6}


def _resolve_week(week: str | None, run_doc=None) -> datetime.date:
	if week:
		return monday_of(frappe.utils.getdate(week))
	if run_doc and run_doc.get("date"):
		return monday_of(frappe.utils.getdate(run_doc.date))
	return monday_of(frappe.utils.getdate(frappe.utils.today()))


def _cell(slot, room: int) -> dict:
	return {
		# rooms this half-day covers in all, however many lines it is drawn on
		"rooms": slot.rooms,
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
		# this chip's solved room number, only where room_coverage_matched_rooms
		# measured it — the row it is drawn at is already placed from this, so it is
		# reporting-only here, for a tooltip to say "Room 3" instead of just showing it.
		"room": room or None,
	}


def _index(chart) -> dict[tuple, dict]:
	"""(section, band, row, lane, day) -> cell. One pass, not a scan per cell.

	One placement, one cell: `chart._fill` already gave a multi-room holder a
	placement per room, so nothing here has to reserve the lines below a chip.
	"""
	return {(p.section, p.band, p.row, p.lane, p.day_index): _cell(p.slot, p.room) for p in chart.placements}


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
		"open_rows": [list(chart.covered_rows(shift_type, band_key, day)) for day in range(7)],
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
	except NotImplementedError, IndexError:
		return None, None
	return (window[0].isoformat(), window[-1].isoformat()) if window else (None, None)


def _selection(raw) -> list[str]:
	"""A filter argument as it arrives from the browser: JSON, a list, or nothing.

	An empty selection is "no filter", never "no bands" — see `layout.derive`.
	"""
	if not raw:
		return []
	values = frappe.parse_json(raw) if isinstance(raw, str) else raw
	return [value for value in values if value]


@frappe.whitelist()
def get_week_chart(
	week: str | None = None,
	run: str | None = None,
	mode: str = "Bounded",
	disciplines=None,
	branches=None,
	weekdays=None,
) -> dict:
	"""The wall chart for one week, optionally diffed against an Optimizer Run.

	`run` may name a run in any state. An unsolved or failed one contributes no
	slots and the chart falls back to the Shift Assignments on the books — which
	is the whole reason this view is always on: the week a solve failed for is
	exactly the week somebody needs to look at.

	`disciplines` and `branches` narrow the chart to those bands. Narrowed here
	rather than in the browser so that everything derived from the band set — the
	sections, the coverage headline, the leave list — is derived from the same set
	the reader is looking at. A chart whose headline counted rooms it was not
	drawing would be worse than no headline, which is the same reason `_totals`
	counts off the chart rather than off `Optimizer Run Coverage`.

	`weekdays` is which weekdays the practice works, for a site whose Holiday List cannot
	say yet (see :func:`_default_worked`). It narrows and never widens: it decides which
	columns read as working, what the coverage headline counts against, and — the reason
	it is here rather than in the browser — which days a leave can have emptied.
	"""
	frappe.has_permission("Shift Assignment", throw=True)

	run_doc = None
	if run:
		run_doc = frappe.get_doc("Optimizer Run", run)
		run_doc.check_permission("read")
		mode = run_doc.mode or mode

	disciplines, branches = _selection(disciplines), _selection(branches)
	structure = layout_mod.derive(disciplines, branches)

	monday = _resolve_week(week, run_doc)
	week_days = week_dates(monday)
	# Resolved before the leave list, because which days are worked is what decides
	# whether a leave day emptied anything.
	holidays, weekly_offs, calendar = source.holidays(monday, mode)
	worked = _weekdays(weekdays) or _default_worked(weekly_offs, calendar)
	speculated = (
		{r.leave_application for r in (run_doc.get("leaves_speculations") or [])} if run_doc else set()
	)
	# Leave follows the bands actually drawn. A site with no band at all is the
	# exception: there is nothing to narrow against, and reporting the week's leave
	# is the only thing such a chart can still do.
	drawn = {band.discipline for band in structure.bands} or None
	week_leaves = source.leaves(monday, speculated, drawn, worked)
	pending = _pending_bound(week_days[0], week_days[-1])
	existing = source.from_shift_assignments(monday) + source.from_settled_rotas(
		monday, _off_leave(pending.pop("rows"), week_leaves)
	)
	proposed = source.from_optimizer_run(run, monday) if run_doc else []

	existing, hidden_existing = source.in_scope(existing, disciplines, branches)
	proposed, hidden_proposed = source.in_scope(proposed, disciplines, branches)
	# Half-days, not chips, and not twice for one the run kept — the same counting
	# `_totals` uses.
	hidden = len({slot.match_key for slot in hidden_existing + hidden_proposed})

	chart = build(structure, merge(existing, proposed), monday)
	cells = _index(chart)

	first_day, last_day = _planning_window(run_doc)

	days = []
	for day in week_days:
		iso = day.isoformat()
		days.append(
			{
				"date": iso,
				"weekday": day.weekday(),
				"holiday": holidays.get(iso),
				"working": day.weekday() in worked and iso not in holidays,
				"in_window": bool(first_day and last_day and first_day <= iso <= last_day),
			}
		)

	by_shift, unknown, _not_due = bucket_leaves(
		week_leaves, [day["date"] for day in days], [s.shift_type for s in structure.sections]
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
		sections.append(
			{
				"shift_type": section.shift_type,
				"title": section.title,
				"bands": bands,
				"leaves": by_shift[section.shift_type],
			}
		)

	return {
		"week": monday.isoformat(),
		"prev_week": (monday - datetime.timedelta(days=7)).isoformat(),
		"next_week": (monday + datetime.timedelta(days=7)).isoformat(),
		"days": days,
		"sections": sections,
		"leaves_unknown": unknown,
		"leaves_not_due": _not_due,
		"filters": _filters(disciplines, branches, sorted(worked), hidden),
		"warnings": _warnings(chart, structure, calendar),
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


def _filters(disciplines: list[str], branches: list[str], weekdays: list[int], hidden: int) -> dict:
	"""The reader's narrowing, and what there is to narrow to.

	`weekdays.selected` is the *effective* set — the reader's own answer where they gave
	one, the calendar's default otherwise — so the control always shows what the chart
	is actually doing, and a reader who has never touched it still sees which days it
	believes are worked. Weekday *names* are left to the browser, which has the locale.
	"""
	options = layout_mod.filter_options()
	return {
		"disciplines": {"options": options["disciplines"], "selected": disciplines},
		"branches": {"options": options["branches"], "selected": branches},
		# Strings, because the browser control keys its options by value and "0" is
		# Monday; `_weekdays` parses them back.
		"weekdays": {"selected": [str(weekday) for weekday in weekdays]},
		"hidden": hidden,
	}


def _pending_bound(first, last) -> dict:
	"""Settled schedules this week needs that nothing on the books records.

	The rows are drawn as virtual chips (and popped off before the payload leaves);
	the summary backs the on-demand "Create them". See `autoshift.rota` for why HRMS
	is not doing this itself.
	"""
	from autoshift.rota import materialize as rota

	return rota.pending(first, last)


def _off_leave(rows: list[dict], week_leaves: list[dict]) -> list[dict]:
	"""Drop rota days the employee is on leave for — leave wins, as it does in the loader."""
	away = {(entry["employee"], entry["date"]) for entry in week_leaves}
	return [row for row in rows if (row["employee"], row["date"]) not in away]


def _warnings(chart, structure, calendar: bool) -> list[str]:
	warnings = list(chart.warnings)
	if not calendar:
		# A solve refuses to run at all on this (`data_loader._availability` throws), so
		# the chart saying it out loud is the kinder half of the same message.
		warnings.append(
			"no Holiday List resolves for any company, so the chart cannot tell which days "
			"are worked: it is falling back to Saturday and Sunday as the weekend, and every "
			"day of a leave reads as a day it emptied. Assign one through Holiday List "
			"Assignment, or set the company's Default Holiday List"
		)
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
	# Counted over half-days, not chips: a two-room holder is drawn twice (see
	# `chart._fill`) and is still one kept — or dropped — assignment.
	for _half_day, kind in {(p.slot.match_key, p.slot.kind) for p in chart.placements}:
		kinds[kind] = kinds.get(kind, 0) + 1
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
