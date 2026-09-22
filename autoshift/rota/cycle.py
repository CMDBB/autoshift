# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Which days a `Shift Schedule` actually falls on. Pure Python, no Frappe.

This is a **re-implementation of HRMS's own**
`ShiftScheduleAssignment.create_shifts`, and it exists only because that one is
unsound for a cycle longer than a week. See `autoshift.rota` for why, and delete
this the day upstream fixes it.

The rule, stated once:

    a Shift Schedule covers the weekdays in `repeat_on_days`, in one week out of
    every `cycle_weeks`, counting weeks from the one after `create_shifts_after`.

`create_shifts_after` is both the **handover boundary** — everything up to and
including it belongs to whoever wrote the records already on the books, and
nothing is generated on or before it — and the **phase anchor** for a rota.
HRMS's generator moves it forward as it goes, by less than a cycle, and that is
precisely the bug. This app only ever moves it *back*, and only by whole cycles
(:func:`backdated_anchor`), which leaves the phase untouched: a rota whose
boundary sits in the future is otherwise simply absent from the weeks before it,
which the optimizer and the wall chart both read.

One deliberate divergence: weeks here are ISO weeks (Monday-based), where
`create_shifts` chops arbitrary seven-day blocks off whatever date it was handed.
The two agree whenever `create_shifts_after` is a Sunday, which is what
zawin2frappe's own phase anchoring produces; where they disagree, a Monday-based
week is the reading the practice's wall chart and zawin2frappe's cycle fitting
both use.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

#: `Shift Schedule.frequency` -> cycle length in weeks.
FREQUENCY_WEEKS: dict[str, int] = {
	"Every Week": 1,
	"Every 2 Weeks": 2,
	"Every 3 Weeks": 3,
	"Every 4 Weeks": 4,
}

#: `Assignment Rule Day` value -> `date.weekday()`.
WEEKDAY_INDEX: dict[str, int] = {
	"Monday": 0,
	"Tuesday": 1,
	"Wednesday": 2,
	"Thursday": 3,
	"Friday": 4,
	"Saturday": 5,
	"Sunday": 6,
}

#: The inverses, for building an `Assignment Rule Day` row / a `Shift Schedule.frequency`
#: from a `Rota` — the direction `rota.edit`'s apply half needs, `WEEKDAY_INDEX` and
#: `FREQUENCY_WEEKS` being the direction `materialize.load_rotas` needs.
WEEKDAY_LABEL: dict[int, str] = {index: label for label, index in WEEKDAY_INDEX.items()}
FREQUENCY_LABEL: dict[int, str] = {weeks: label for label, weeks in FREQUENCY_WEEKS.items()}


def monday_of(day: datetime.date) -> datetime.date:
	return day - datetime.timedelta(days=day.weekday())


@dataclass(frozen=True)
class Rota:
	"""One `Shift Schedule Assignment` joined to its `Shift Schedule`.

	Everything needed to say which days the person works, and nothing else — so
	the expansion below can be tested without a site.
	"""

	#: Shift Schedule Assignment docname, so a generated Shift Assignment can link back.
	assignment: str
	employee: str
	company: str
	shift_type: str
	shift_location: str | None
	weekdays: frozenset[int]
	cycle_weeks: int = 1
	#: `create_shifts_after`: handover boundary and phase anchor. None means the
	#: schedule has no boundary — every week in the window is fair game and a
	#: rota's phase falls back to the window's own first week.
	anchor: datetime.date | None = None
	#: `custom_unconfirmed`: an importer inferred this pattern and nobody has confirmed
	#: it yet (silver standard). Never changes which days it covers.
	unconfirmed: bool = False
	#: `custom_scheduling_role`: the Scheduling Role the shifts this rota generates are
	#: worked in. Carried, never interpreted — which days it covers is the same either
	#: way — so that a materialised `Shift Assignment` can record its own role instead of
	#: leaving the optimizer to infer one (see `optimizer.types.resolve_assignment_role`).
	scheduling_role: str | None = None
	#: `custom_collateral_roles`: duties worked on top of every shift it generates.
	collateral_roles: tuple[str, ...] = ()
	#: Which discipline this pattern belongs to: `Scheduling Role.discipline` of
	#: :attr:`scheduling_role`, falling back to the Shift Location's own
	#: `custom_discipline` where no role could be resolved. Carried, never interpreted
	#: here — it exists so the Rota Editor can tell one discipline's settled week from
	#: another's without re-reading the DB, and refuse to let a view of one edit the
	#: other. `None` means genuinely unattributed, which is the only case a view is
	#: allowed to claim for itself.
	discipline: str | None = None

	@property
	def is_rota(self) -> bool:
		"""Longer than a week, i.e. the shape HRMS cannot run."""
		return self.cycle_weeks > 1


def first_covered_week(rota: Rota, window_start: datetime.date) -> datetime.date:
	"""Monday of the first week the schedule covers — the phase anchor.

	`create_shifts` emits its weekday set in the week *following*
	`create_shifts_after`, which is what makes this the week to count phases from.
	"""
	if rota.anchor is None:
		return monday_of(window_start)
	return monday_of(rota.anchor) + datetime.timedelta(weeks=1)


def occurrences(rota: Rota, first: datetime.date, last: datetime.date) -> list[datetime.date]:
	"""The days in `[first, last]` this rota puts the employee on `rota.shift_type`.

	Empty when the window falls entirely on or before the handover boundary: the
	records covering those days are somebody else's to write.
	"""
	if not rota.weekdays or last < first:
		return []

	cycle = max(int(rota.cycle_weeks or 1), 1)
	start = first
	if rota.anchor is not None:
		start = max(start, rota.anchor + datetime.timedelta(days=1))
	if start > last:
		return []

	anchor_week = first_covered_week(rota, start)
	days: list[datetime.date] = []
	day = start
	while day <= last:
		if day.weekday() in rota.weekdays:
			# Python's floor division keeps this correct for a week before the
			# anchor too, and `%` on a negative quotient still lands in [0, cycle).
			if cycle == 1 or ((monday_of(day) - anchor_week).days // 7) % cycle == 0:
				days.append(day)
		day += datetime.timedelta(days=1)
	return days


#: How far before today a rota's anchor must lie, so a pattern always covers the
#: weeks immediately around now — the ones the wall chart and a solve look at first.
ANCHOR_LEAD_WEEKS = 4


def backdated_anchor(
	anchor: datetime.date | None, cycle_weeks: int, not_after: datetime.date
) -> datetime.date | None:
	"""`anchor` moved back by whole cycles until it lies on or before `not_after`.

	A whole number of cycles keeps the phase exactly: the Monday `first_covered_week`
	counts from moves by a multiple of `cycle_weeks` weeks, so every later week lands
	on the same phase it did. Only the handover boundary moves, and only earlier.
	An anchor already early enough, or none at all, is returned unchanged.
	"""
	if anchor is None or anchor <= not_after:
		return anchor
	step = max(int(cycle_weeks or 1), 1) * 7
	cycles = -(-(anchor - not_after).days // step)  # ceiling division
	return anchor - datetime.timedelta(days=cycles * step)


def anchor_cutoff(today: datetime.date) -> datetime.date:
	"""The latest anchor :func:`backdated_anchor` lets stand, as of `today`."""
	return today - datetime.timedelta(weeks=ANCHOR_LEAD_WEEKS)
