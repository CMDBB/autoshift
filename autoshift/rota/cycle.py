# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Which days a `Shift Schedule` actually falls on. Pure Python, no Frappe.

This is a **re-implementation of HRMS's own**
`ShiftScheduleAssignment.create_shifts`, and it exists only because that one is
unsound for a cycle longer than a week. See `autoshift.rota` for why, and delete
this the day upstream fixes it.

The rule, stated once:

    a Shift Schedule covers the weekdays in `repeat_on_days`, in one week out of
    every `cycle_weeks`, counting weeks from the one after `create_shifts_after`,
    and stops after `custom_create_shifts_until`.

`create_shifts_after` is both the **handover boundary** — everything up to and
including it belongs to whoever wrote the records already on the books, and
nothing is generated on or before it — and the **phase anchor** for a rota.
HRMS's generator moves it forward as it goes, by less than a cycle, and that is
precisely the bug. **This app never moves it at all.** A pattern that changes is
ended (`custom_create_shifts_until`) and succeeded by a new row starting the next
day, which is the only way a date field doing double duty as a phase carrier can
also be a truthful record of when something took effect: move it and you either
lose the phase or rewrite history. See `autoshift.rota.edit` for the succession
rule and the design notes for what this replaced.

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
	#: `custom_create_shifts_until`: the last day the pattern is in force, None meaning
	#: open-ended. Together with :attr:`anchor` this makes a rota an **interval**,
	#: `(anchor, until]`, which is what lets a superseded pattern be ended rather than
	#: rewritten or deleted — see `autoshift.rota.edit` for the succession rule.
	until: datetime.date | None = None
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

	Empty when the window falls entirely outside the rota's own interval — on or before
	the handover boundary, where the records are somebody else's to write, or after
	:attr:`Rota.until`, where a successor pattern has taken over.
	"""
	if not rota.weekdays or last < first:
		return []

	cycle = max(int(rota.cycle_weeks or 1), 1)
	start = first
	if rota.anchor is not None:
		start = max(start, rota.anchor + datetime.timedelta(days=1))
	if rota.until is not None:
		last = min(last, rota.until)
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


#: How far before today a rota's anchor was once forced to lie, so a pattern always
#: covered the weeks immediately around now.
#:
#: **Historical.** Nothing calls this on new data any more: an anchor is a record of
#: when a pattern started, and pulling it back to make the pattern *visible* was a
#: bookkeeping lie that also back-filled weeks the person worked differently. The
#: Rota Editor now anchors a created pattern at the view it was laid out in and ends
#: its predecessor there; a pattern starting after the window on screen is surfaced as
#: a prompt instead of being silently dragged backwards. Retained only because
#: `patches.backdate_rota_anchors` already ran with it on every existing site.
ANCHOR_LEAD_WEEKS = 4


def backdated_anchor(
	anchor: datetime.date | None, cycle_weeks: int, not_after: datetime.date
) -> datetime.date | None:
	"""`anchor` moved back by whole cycles until it lies on or before `not_after`.

	A whole number of cycles keeps the phase exactly: the Monday `first_covered_week`
	counts from moves by a multiple of `cycle_weeks` weeks, so every later week lands
	on the same phase it did. Only the handover boundary moves, and only earlier.
	An anchor already early enough, or none at all, is returned unchanged.

	**Historical** — see :data:`ANCHOR_LEAD_WEEKS`. Live only through the patch that
	already ran; no current write path calls it.
	"""
	if anchor is None or anchor <= not_after:
		return anchor
	step = max(int(cycle_weeks or 1), 1) * 7
	cycles = -(-(anchor - not_after).days // step)  # ceiling division
	return anchor - datetime.timedelta(days=cycles * step)


def anchor_cutoff(today: datetime.date) -> datetime.date:
	"""The latest anchor :func:`backdated_anchor` lets stand, as of `today`. Historical."""
	return today - datetime.timedelta(weeks=ANCHOR_LEAD_WEEKS)
