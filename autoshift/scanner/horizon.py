# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""What a scanned window says about each of its days, and where those days sit on a calendar.

Frappe-free, like `optimizer/types.py` and `wallchart/chart.py`: everything here takes a
solved `DataPackage` plus the two tables a run persists and decides nothing else, so the
whole verdict is unit-tested (`tests/test_scanner.py`). `scanner/scan.py` is the half that
touches the database.

A scan is a sequence of **windows** — one Optimizer Run each, stepping by the window's own
length — and the thing a reader looks at is a *day*. So the verdict is computed per day,
out of four independent kinds of finding:

  - **coverage** — one `Cell` per (discipline, branch, shift type) the configuration says
	should be staffed, with what the run actually staffed in it;
  - **under-FTE** — who ended the window below their FTE target, filed under the days they
	were free and nowhere;
  - **conflicts** — the loader's `binding_conflicts` / `unresolved_assignments`, which
	carry a date of their own;
  - **failure** — the window never solved. That belongs to the window, not the day, so it
	is applied by :func:`window_days` rather than stored per day.

The coverage finding has **two readings**, and both are stored, because which one is a
problem depends on the practice: `capacity` is what the configuration asks for, while
`exclusive_capacity` is what the exclusive gating roles actually scheduled that slot could
ever open. A practice with three dentists and four rooms is not short a room on the day
only three dentists are in — it is short a dentist, which is a hiring question, not a
scheduling one. Scaling to the exclusive holders present therefore flags **only a lack of
flexible staff** around the holders who are there. The reader picks (`scaled`), and nothing
has to be re-solved to change their mind.
"""

from __future__ import annotations

import calendar as _calendar
import dataclasses
import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from autoshift.optimizer import types

#: Bumped when the shape of a window's stored findings changes. A row written by an older
#: version is read back as far as it goes and never guessed at — see :func:`loads`.
FINDINGS_VERSION = 1

SEV_OK = "ok"
SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"
SEV_FAILED = "failed"

#: Worst wins, everywhere a severity is aggregated — over the cells of a day, the days of a
#: window, and the windows of a scan.
SEVERITY_RANK = {SEV_OK: 0, SEV_INFO: 1, SEV_WARNING: 2, SEV_CRITICAL: 3, SEV_FAILED: 4}

#: Window statuses that mean "there is no verdict for these days yet".
UNSCANNED = ("Pending", "Solving")

#: …and the one that means the verdict is "no schedule exists".
FAILED_STATUSES = ("Failed", "Timed Out")


def worst(severities: Iterable[str]) -> str:
	"""The highest-ranking severity, `ok` over an empty sequence."""
	return max(severities, key=lambda s: SEVERITY_RANK.get(s, 0), default=SEV_OK)


@dataclass(frozen=True)
class Cell:
	"""One (discipline, branch, shift type) room-coverage slot of one day.

	`capacity` is `Discipline Branch Config.rooms_num`; `staffed` the run's own
	`active_rooms` for the slot. `exclusive_capacity` is `capacity` capped by what the
	exclusive gating roles scheduled here could open — equal to `capacity` where the
	discipline has no exclusive gating role, so a site not using Exclusive mode reads
	identically under either lens.
	"""

	discipline: str
	branch: str
	shift_type: str
	staffed: int
	capacity: int
	exclusive_capacity: int

	def effective(self, scaled: bool) -> int:
		return self.exclusive_capacity if scaled else self.capacity

	def shortfall(self, scaled: bool) -> int:
		return max(0, self.effective(scaled) - self.staffed)

	def severity(self, scaled: bool) -> str:
		"""`ok` where the slot is as full as it could be, `critical` where it is shut.

		A slot nobody is in at all is a different thing from a half-staffed one: the
		rooms are closed, not crowded, and no amount of reading the number tells a
		planner that as fast as the colour does.
		"""
		goal = self.effective(scaled)
		if goal <= 0 or self.staffed >= goal:
			return SEV_OK
		return SEV_CRITICAL if self.staffed <= 0 else SEV_WARNING


@dataclass(frozen=True)
class Day:
	"""Every finding of one day, with no severity of its own — see :func:`day_summary`.

	`worked` is false for a day the horizon does not contain (a company weekly off, a
	public holiday): it carries no cells, and an empty calendar square that nobody works
	is not a problem to report.
	"""

	date: datetime.date
	worked: bool
	cells: tuple[Cell, ...] = ()
	#: (employee, shifts short of their FTE target over the window, their disciplines)
	under_fte: tuple[tuple[str, int, tuple[str, ...]], ...] = ()
	#: (kind, employee, detail, discipline) — `binding` or `unresolved`. The discipline is
	#: "" where the finding names none, which is why a filter cannot drop one: an
	#: assignment nothing could place is exactly the kind of thing a narrowed view must
	#: not swallow.
	conflicts: tuple[tuple[str, str, str, str], ...] = ()


# ── the verdict ─────────────────────────────────────────────────────────────────


def day_severity(day: Day, scaled: bool) -> str:
	"""Worst cell wins, and the non-coverage findings raise a floor under it.

	A conflict between the books and a leave is a `warning` however full the rooms are —
	it is a record that contradicts another record, and the schedule being fine around it
	is no answer. Ending the window under FTE is only `info`: nobody is missing from a
	room over it, and whose contract is short of hours is a different conversation.
	"""
	severity = worst(cell.severity(scaled) for cell in day.cells)
	if day.conflicts:
		return worst((severity, SEV_WARNING))
	if day.under_fte:
		return worst((severity, SEV_INFO))
	return severity


def day_summary(day: Day, scaled: bool, severity: str | None = None) -> dict:
	"""One calendar square: its severity, the numbers behind it, and why in words.

	`severity` overrides the day's own, which is how a window that never solved paints
	its days — the failure is the window's, not the day's (see :func:`window_days`).
	"""
	staffed = sum(cell.staffed for cell in day.cells)
	capacity = sum(cell.effective(scaled) for cell in day.cells)
	configured = sum(cell.capacity for cell in day.cells)
	shortfall = sum(cell.shortfall(scaled) for cell in day.cells)
	return {
		"date": day.date.isoformat(),
		"severity": severity or day_severity(day, scaled),
		"worked": day.worked,
		"staffed": staffed,
		"capacity": capacity,
		"configured": configured,
		"shortfall": shortfall,
		"short_cells": sum(1 for cell in day.cells if cell.shortfall(scaled)),
		"shut_cells": sum(1 for cell in day.cells if cell.severity(scaled) == SEV_CRITICAL),
		"under_fte": len(day.under_fte),
		"conflicts": len(day.conflicts),
	}


def day_detail(day: Day, scaled: bool) -> dict:
	"""Everything :func:`day_summary` counts, itemised, for the panel behind a click."""
	return {
		**day_summary(day, scaled),
		"cells": [
			{
				**dataclasses.asdict(cell),
				"effective": cell.effective(scaled),
				"shortfall": cell.shortfall(scaled),
				"severity": cell.severity(scaled),
			}
			for cell in sorted(
				day.cells,
				key=lambda c: (-SEVERITY_RANK[c.severity(scaled)], c.discipline, c.branch, c.shift_type),
			)
		],
		"under_fte_rows": [
			{"employee": employee, "deficit": deficit, "disciplines": list(disciplines)}
			for employee, deficit, disciplines in day.under_fte
		],
		"conflict_rows": [
			{"kind": kind, "employee": employee, "detail": detail, "discipline": discipline}
			for kind, employee, detail, discipline in day.conflicts
		],
	}


def narrow(day: Day, disciplines: Sequence[str], branches: Sequence[str]) -> Day:
	"""The day as a reader narrowed to these disciplines and branches sees it.

	Narrowed here rather than in the browser, for the wall chart's reason: the severity,
	the shortfall and the counts all have to be derived from the same cells the reader is
	looking at, or the headline describes a calendar nobody is being shown. **An empty
	selection is everything**, never nothing.

	A finding naming no discipline survives every filter — an assignment nothing could
	place is exactly what a narrowed view must not swallow.
	"""
	if not disciplines and not branches:
		return day
	keep_discipline = set(disciplines)
	keep_branch = set(branches)

	def in_scope(discipline: str, branch: str | None = None) -> bool:
		if keep_discipline and discipline and discipline not in keep_discipline:
			return False
		return not (keep_branch and branch and branch not in keep_branch)

	return dataclasses.replace(
		day,
		cells=tuple(cell for cell in day.cells if in_scope(cell.discipline, cell.branch)),
		under_fte=tuple(
			row
			for row in day.under_fte
			if not keep_discipline or not row[2] or any(d in keep_discipline for d in row[2])
		),
		conflicts=tuple(row for row in day.conflicts if not row[3] or in_scope(row[3])),
	)


def window_days(days: Sequence[Day], status: str, scaled: bool) -> list[dict]:
	"""The window's days as calendar squares, with its status folded in.

	A window that failed has no coverage numbers to show — nothing was persisted — so
	every day it covers is painted `failed`, worked or not. A window still Pending paints
	nothing at all: an unscanned day is not a clean day, and saying so in the same colour
	would be the one lie this calendar cannot afford.
	"""
	if status in FAILED_STATUSES:
		return [day_summary(day, scaled, severity=SEV_FAILED) for day in days]
	return [day_summary(day, scaled) for day in days]


def window_severity(days: Sequence[Day], status: str, scaled: bool) -> str:
	if status in FAILED_STATUSES:
		return SEV_FAILED
	return worst(day_severity(day, scaled) for day in days)


# ── building the findings ───────────────────────────────────────────────────────


def exclusive_capacity(
	data: types.DataPackage, discipline: str, branch: str, present: Sequence[tuple]
) -> int:
	"""How many rooms the exclusive gating roles scheduled in this slot could open.

	A room waits on every gating role (`room_coverage`), so the exclusive ones bound it
	from above: each holder scheduled in such a role contributes their `max_rpe`, and the
	scarcest of those roles is the cap. Roles worked Flexible are deliberately *not*
	counted — they are the staff a scan is looking for a shortage of, so capping by them
	would hide exactly the finding this figure exists to isolate.

	`present` is this slot's solved assignments as (employee, role) pairs. A discipline
	with no exclusive gating role returns the configured capacity untouched.
	"""
	capacity = data.rooms.get((discipline, branch), 0)
	supply: dict[str, int] = {}
	for role, role_discipline in data.role_discipline.items():
		if role_discipline != discipline or not data.gates_rooms(role):
			continue
		holders = [employee for employee, held in present if held == role]
		if data.role_mode.get(role, types.MODE_FLEXIBLE) != types.MODE_EXCLUSIVE:
			# A Flexible role caps nothing: somebody else could have been put in it, which
			# is the shortage a scan is for. The exception is a role whose every holder
			# *here* overrides it to Exclusive (`role_mode_overrides`) — then nobody else
			# could have been, and it caps like one.
			if not holders or not all(
				data.mode(employee, role) == types.MODE_EXCLUSIVE for employee in holders
			):
				continue
		# Nobody in an exclusive gating role means no room in this slot can open at all,
		# which is a supply of zero rather than a role to skip.
		supply[role] = sum(data.max_rpe.get((employee, role), 1) for employee in holders)
	if not supply:
		return capacity
	return min(capacity, min(supply.values()))


def window_findings(
	data: types.DataPackage,
	slots: Iterable[dict],
	coverage: Iterable[dict],
	days: Sequence[datetime.date],
) -> tuple[Day, ...]:
	"""Build one `Day` per day of the window out of a solved run's own two tables.

	`slots` is `Optimizer Run Slot` and `coverage` is `Optimizer Run Coverage`, as plain
	dicts. Both are read rather than re-derived: they are what the solver actually
	decided, and `Optimizer Run Coverage` keeps its zero-staffed rows precisely so a shut
	slot can be reported.

	`days` is the window's whole span, not `DataPackage.working_days`: a day nobody works
	is not in the horizon the solver saw, but it is still a square on the calendar and the
	reason it is empty is worth drawing.
	"""
	slots = [dict(row) for row in slots]
	coverage = [dict(row) for row in coverage]
	worked_days = set(data.working_days)

	#: (date, shift type, branch, discipline) -> the (employee, role) pairs solved into it
	present: dict[tuple, list[tuple[str, str]]] = {}
	#: (employee, date, shift type) — presence, so a collateral duty does not count twice
	presences: set[tuple[str, datetime.date, str]] = set()
	for slot in slots:
		date = _as_date(slot["date"])
		role = slot.get("scheduling_role") or ""
		discipline = data.role_discipline.get(role, "")
		present.setdefault((date, slot["shift_type"], slot["branch"], discipline), []).append(
			(slot["employee"], role)
		)
		presences.add((slot["employee"], date, slot["shift_type"]))

	cells: dict[datetime.date, list[Cell]] = {}
	for row in coverage:
		date = _as_date(row["date"])
		discipline, branch, shift_type = row["discipline"], row["branch"], row["shift_type"]
		here = present.get((date, shift_type, branch, discipline), [])
		cells.setdefault(date, []).append(
			Cell(
				discipline=discipline,
				branch=branch,
				shift_type=shift_type,
				staffed=int(row.get("staffed_rooms") or 0),
				capacity=int(row.get("capacity") or 0),
				exclusive_capacity=exclusive_capacity(data, discipline, branch, here),
			)
		)

	under_fte = _under_fte(data, presences)
	conflicts = _conflicts(data)

	return tuple(
		Day(
			date=day,
			worked=day in worked_days,
			cells=tuple(sorted(cells.get(day, ()), key=lambda c: (c.discipline, c.branch, c.shift_type))),
			under_fte=tuple(under_fte.get(day, ())),
			conflicts=tuple(conflicts.get(day, ())),
		)
		for day in days
	)


def _under_fte(
	data: types.DataPackage, presences: set[tuple[str, datetime.date, str]]
) -> dict[datetime.date, list[tuple[str, int, tuple[str, ...]]]]:
	"""Who ended the window short of their FTE target, filed under their empty days.

	Counted in presences, exactly as `rules.fte_ceiling` and the statistics panel count
	them: a collateral duty rides on a half-day already worked and costs its holder no
	extra time. The deficit is a property of the whole window, so it is filed under every
	day the employee was free and scheduled nowhere — those are the days that could have
	absorbed it. A day they were not available on is not one of them.
	"""
	assigned: dict[str, int] = {}
	busy: set[tuple[str, datetime.date]] = set()
	for employee, day, _shift_type in presences:
		assigned[employee] = assigned.get(employee, 0) + 1
		busy.add((employee, day))

	out: dict[datetime.date, list[tuple[str, int, tuple[str, ...]]]] = {}
	for employee in data.employees:
		deficit = int(data.target_shifts.get(employee, 0)) - assigned.get(employee, 0)
		if deficit <= 0:
			continue
		# Their own disciplines, so a narrowed calendar does not report somebody nothing
		# in view could have scheduled.
		disciplines = tuple(
			sorted(
				{
					data.role_discipline[role]
					for role in data.employee_roles.get(employee, ())
					if role in data.role_discipline
				}
			)
		)
		for day in data.days_of(employee):
			if (employee, day) not in busy:
				out.setdefault(day, []).append((employee, deficit, disciplines))
	return out


def _conflicts(data: types.DataPackage) -> dict[datetime.date, list[tuple[str, str, str, str]]]:
	"""The loader's own two complaints, each of which already names a date."""
	out: dict[datetime.date, list[tuple[str, str, str, str]]] = {}
	for employee, role, shift_type, date, branch in data.binding_conflicts:
		out.setdefault(_as_date(date), []).append(
			(
				"binding",
				employee,
				f"{role or '?'} · {shift_type} · {branch} dropped for leave",
				data.role_discipline.get(role, ""),
			)
		)
	for employee, date, reason in data.unresolved_assignments:
		# Nothing could place it, so nothing names its discipline either.
		out.setdefault(_as_date(date), []).append(("unresolved", employee, reason, ""))
	return out


def _as_date(value) -> datetime.date:
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value)[:10])


# ── storage ────────────────────────────────────────────────────────────────────


def dumps(days: Sequence[Day]) -> dict:
	"""The window's findings as the JSON a `Horizon Scan Window` row stores.

	Compact on purpose — a year-long scan stores one of these per window and the page
	reads every one of them in a single request.
	"""
	return {
		"version": FINDINGS_VERSION,
		"days": [
			{
				"d": day.date.isoformat(),
				"w": 1 if day.worked else 0,
				"c": [
					[
						cell.discipline,
						cell.branch,
						cell.shift_type,
						cell.staffed,
						cell.capacity,
						cell.exclusive_capacity,
					]
					for cell in day.cells
				],
				"f": [
					[employee, deficit, list(disciplines)] for employee, deficit, disciplines in day.under_fte
				],
				"x": [list(conflict) for conflict in day.conflicts],
			}
			for day in days
		],
	}


def loads(payload: dict | None) -> tuple[Day, ...]:
	"""Read :func:`dumps` back. An unreadable or future payload is no findings at all."""
	if not payload or payload.get("version") != FINDINGS_VERSION:
		return ()
	return tuple(
		Day(
			date=_as_date(row["d"]),
			worked=bool(row.get("w")),
			cells=tuple(Cell(*cell) for cell in row.get("c", ())),
			under_fte=tuple(
				(employee, deficit, tuple(disciplines)) for employee, deficit, disciplines in row.get("f", ())
			),
			conflicts=tuple(tuple(conflict) for conflict in row.get("x", ())),  # ty:ignore[invalid-argument-type]
		)
		for row in payload.get("days", ())
	)


# ── the horizon, and the calendar it is drawn on ───────────────────────────────


def monday_of(day: datetime.date) -> datetime.date:
	return day - datetime.timedelta(days=day.weekday())


def plan_windows(
	start: datetime.date, end: datetime.date, mode: str
) -> list[tuple[datetime.date, datetime.date]]:
	"""The (first day, last day) of every window a scan of [start, end] solves.

	Windows are the planning mode's own length and do not overlap, so each day of the
	horizon is judged exactly once and by exactly one run. The first starts on the Monday
	of `start` — an `Optimizer Run` takes a Monday, and a scan that began mid-week would
	draw a short first week nobody asked for. The last window is kept whole even where it
	runs past `end`: truncating it would solve a week under a horizon the practice does
	not have, and report the resulting hole as a finding.
	"""
	if mode == "Unbounded":
		# `planning_days` hands back an infinite generator for it, and a scan is a finite
		# sweep of finite windows by definition. See issue #8.
		raise NotImplementedError("A horizon scan needs a bounded window length")
	length = len(list(types.planning_days(monday_of(start), mode)))
	windows = []
	first = monday_of(start)
	while first <= end:
		windows.append((first, first + datetime.timedelta(days=length - 1)))
		first += datetime.timedelta(days=length)
	return windows


def month_grids(first: datetime.date, last: datetime.date) -> list[dict]:
	"""Every month [first, last] touches, as Monday-first week rows of ISO dates.

	Days outside the month are `None` rather than the neighbouring month's, so a square
	belongs to exactly one grid and the scan's own range can be shaded without a day
	appearing twice. Days inside the month but outside the scan are present and simply
	carry no verdict — "not scanned" is something the calendar has to be able to say.
	"""
	grids = []
	year, month = first.year, first.month
	while (year, month) <= (last.year, last.month):
		days_in_month = _calendar.monthrange(year, month)[1]
		weeks: list[list[str | None]] = []
		row: list[str | None] = [None] * 7
		for number in range(1, days_in_month + 1):
			day = datetime.date(year, month, number)
			row[day.weekday()] = day.isoformat()
			if day.weekday() == 6 or number == days_in_month:
				weeks.append(row)
				row = [None] * 7
		grids.append(
			{
				"year": year,
				"month": month,
				"label": f"{_calendar.month_name[month]} {year}",
				"weeks": weeks,
			}
		)
		year, month = (year + 1, 1) if month == 12 else (year, month + 1)
	return grids
