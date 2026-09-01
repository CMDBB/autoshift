# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The week wall chart: what goes in which cell, as pure data.

The optimizer's roster grid answers "what did *this person* get". It cannot
answer the question a planner actually asks — "is the practice covered on
Tuesday morning" — because that is a fact about *rooms*, and rooms are not one of
its axes. This module is the other view: a stack of labelled bands, one per
configured (branch, discipline), each as tall as that config's room count, split
into one lane per Scheduling Role of the discipline, with a column per weekday.
An unstaffed room is a blank line and an uncovered role is a blank column, which
is what makes a half-empty schedule diagnosable at a glance.

Frappe-free on purpose, exactly like `optimizer/rules.py`: the layout arrives as
dataclasses and the slots as records, so placement can be reasoned about and
tested without a site. `layout.py` derives the layout from the configuration and
`source.py` reads the slots; only those two touch the database.

Two things this deliberately does *not* invent, both inherited from the paper
sheet it generalizes:

  **room identity** — a band's rows are numbered because chairs are, but
  autoshift tracks how many rooms are in use, never which (gh#9). Row 2 is the
  second person on that half-day, not room 2.

  **pairing** — lanes read as a tandem (practitioner beside assistant) but
  nothing pairs them: each lane is filled independently and sorted by label. A
  row is two people working the same half-day at the same place, not a stated
  partnership.

Both are stable across rebuilds of the same data, which is what matters for a
chart someone reads every week.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

#: Band key used for slots no configured band claimed. They are always emitted —
#: a chart that quietly drops a scheduled person is worse than no chart at all.
OVERFLOW = "__overflow__"

#: On the books, with no run to compare against.
KIND_EXISTING = "existing"
#: The run reproduced a Shift Assignment that is already on the books.
KIND_KEPT = "kept"
#: The run proposes this and nothing is on the books for it.
KIND_ADDED = "added"
#: On the books, and the run did **not** reproduce it. The interesting one: a
#: settled schedule the ruleset re-planned away.
KIND_DROPPED = "dropped"


@dataclass(frozen=True)
class Slot:
	"""One person, working one half-day, in one role, at one branch."""

	date: datetime.date
	shift_type: str
	employee: str
	employee_name: str
	#: What the cell prints. Initials where the site records them, else a short
	#: form of the name — see `source.short_label`.
	label: str
	branch: str | None
	scheduling_role: str | None
	kind: str = KIND_EXISTING
	#: The run pinned this rather than choosing it (warm start / role binding).
	forced: bool = False
	#: False when the role was inferred from the employee's held roles rather
	#: than read from the source. A Shift Assignment records no role.
	role_certain: bool = True
	#: Set on a `kept` slot the run moved: what it says here differs from what is
	#: on the books. Human-readable, e.g. "was at Blandonnet".
	changed: str | None = None

	@property
	def match_key(self) -> tuple[str, datetime.date, str]:
		"""What makes two slots from different sources the same working half-day.

		Branch and role are deliberately *not* in it. A Shift Assignment records
		no role, so `source` infers one; matching on the inference would report a
		re-plan every time it guessed differently from the solver. One person can
		hold at most one shift on a day anyway (`one_shift_per_day`), so this is
		already unique — and a genuine move of branch or role is reported through
		`changed` instead of as a drop plus an add.
		"""
		return (self.employee, self.date, self.shift_type)


@dataclass(frozen=True)
class Lane:
	"""One column within a band's day. One per Scheduling Role."""

	key: str
	label: str


@dataclass(frozen=True)
class Band:
	"""A labelled stack of rows: one configured (branch, discipline)."""

	key: str
	branch: str | None
	discipline: str | None
	#: Display names, company suffix stripped — see `layout.derive`.
	branch_label: str
	discipline_label: str
	#: `Discipline Branch Config.rooms_num`. A minimum, never a cap: drawing
	#: fewer rows than there are people would hide somebody.
	rooms: int
	lanes: tuple[Lane, ...]
	#: The Shift Types this config puts in scope. A band is only drawn in the
	#: sections it actually covers, so a discipline that runs mornings only does
	#: not print an empty afternoon block.
	shift_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Section:
	"""One stacked table. One per Shift Type: morning, afternoon, …"""

	shift_type: str
	title: str


@dataclass(frozen=True)
class Layout:
	"""The shape of the chart, derived from configuration."""

	sections: tuple[Section, ...]
	bands: tuple[Band, ...]
	overflow_label: str = "Unplaced"

	def band(self, key: str) -> Band | None:
		return next((b for b in self.bands if b.key == key), None)


@dataclass(frozen=True)
class Placement:
	"""One filled cell."""

	section: str
	band: str
	row: int
	lane: str
	day_index: int
	slot: Slot


@dataclass
class Chart:
	"""A layout, a week, and everything that landed in it."""

	layout: Layout
	monday: datetime.date
	placements: list[Placement] = field(default_factory=list)
	#: Rows actually drawn, per (shift_type, band key).
	heights: dict[tuple[str, str], int] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)
	#: The lanes the overflow band needed, if any.
	overflow_lanes: tuple[Lane, ...] = ()

	@property
	def dates(self) -> list[datetime.date]:
		return week_dates(self.monday)

	def height(self, shift_type: str, band_key: str) -> int:
		return self.heights.get((shift_type, band_key), 0)

	def cell(self, shift_type: str, band_key: str, row: int, lane: str, day_index: int) -> list[Slot]:
		return [
			p.slot
			for p in self.placements
			if p.section == shift_type
			and p.band == band_key
			and p.row == row
			and p.lane == lane
			and p.day_index == day_index
		]


def monday_of(day: datetime.date) -> datetime.date:
	return day - datetime.timedelta(days=day.weekday())


def week_dates(monday: datetime.date) -> list[datetime.date]:
	"""All seven days.

	The chart always draws a full week so its width never changes between weeks:
	a weekend column is dimmed, not dropped, and a stray Saturday assignment
	stays visible instead of having nowhere to go.
	"""
	return [monday + datetime.timedelta(days=offset) for offset in range(7)]


def _describe_move(proposed: Slot, existing: Slot) -> str | None:
	"""What the run changed about a half-day that was already on the books."""
	parts = []
	if proposed.branch != existing.branch:
		parts.append(f"was at {existing.branch or '(no branch)'}")
	# Only when the source was sure: an inferred role differing from the solved
	# one says something about the inference, not about the schedule.
	if existing.role_certain and proposed.scheduling_role != existing.scheduling_role:
		parts.append(f"was as {existing.scheduling_role or '(no role)'}")
	return ", ".join(parts) or None


def merge(existing: list[Slot], proposed: list[Slot]) -> list[Slot]:
	"""One slot list showing what a run changed about what is on the books.

	Called with an empty `proposed` — no run, or a run that never solved — this
	is just the existing assignments and every occupant reads as `existing`,
	which is what keeps the chart useful before anything has been solved.
	"""
	if not proposed:
		return list(existing)
	by_key = {slot.match_key: slot for slot in existing}
	merged: list[Slot] = []
	for slot in proposed:
		match = by_key.pop(slot.match_key, None)
		if match is None:
			merged.append(_recast(slot, KIND_ADDED))
			continue
		merged.append(_recast(slot, KIND_KEPT, changed=_describe_move(slot, match)))
	# Whatever is left was on the books and did not survive the solve.
	merged.extend(_recast(slot, KIND_DROPPED) for slot in by_key.values())
	return merged


def _recast(slot: Slot, kind: str, changed: str | None = None) -> Slot:
	"""`slot` with a comparison verdict attached. Frozen dataclass, so a copy."""
	return Slot(
		date=slot.date,
		shift_type=slot.shift_type,
		employee=slot.employee,
		employee_name=slot.employee_name,
		label=slot.label,
		branch=slot.branch,
		scheduling_role=slot.scheduling_role,
		kind=kind,
		forced=slot.forced,
		role_certain=slot.role_certain,
		changed=changed,
	)


def _sort_key(slot: Slot) -> tuple[str, str]:
	return (slot.label.upper(), slot.employee)


def build(layout: Layout, slots: list[Slot], monday: datetime.date) -> Chart:
	"""Place a week of slots into the layout."""
	chart = Chart(layout=layout, monday=monday)
	dates = week_dates(monday)
	day_index = {date: index for index, date in enumerate(dates)}
	sections = {section.shift_type for section in layout.sections}

	# (branch, role) -> band. Bands are one per configured (branch, discipline)
	# and lanes are that discipline's roles, so placement is a lookup rather than
	# the first-match scan a hand-authored layout needs.
	by_branch_role: dict[tuple[str | None, str | None], Band] = {}
	for band in layout.bands:
		for lane in band.lanes:
			by_branch_role[(band.branch, lane.key)] = band

	outside = 0
	unsectioned: set[str] = set()
	#: (band key, lane key, day index, shift type) -> slots
	pool: dict[tuple[str, str, int, str], list[Slot]] = defaultdict(list)
	overflow_lane_keys: set[str] = set()
	overflow_reasons: set[str] = set()

	for slot in slots:
		if slot.date not in day_index:
			outside += 1
			continue
		if slot.shift_type not in sections:
			unsectioned.add(slot.shift_type)
			continue
		role = slot.scheduling_role or "(no role)"
		band = by_branch_role.get((slot.branch, slot.scheduling_role))
		reason = None
		if band is None:
			reason = f"{role} at {slot.branch or '(no branch)'} matches no Discipline Branch Config"
		elif slot.shift_type not in band.shift_types:
			# Configured, but not for this half-day. Saying so is the point: it is
			# almost always a Discipline Branch Config missing a Shift Type.
			reason = (
				f"{band.discipline_label} at {band.branch_label} does not list "
				f"{slot.shift_type} as a Shift Type"
			)
			band = None
		if band is None:
			overflow_reasons.add(reason)
			overflow_lane_keys.add(role)
			pool[(OVERFLOW, role, day_index[slot.date], slot.shift_type)].append(slot)
			continue
		pool[(band.key, role, day_index[slot.date], slot.shift_type)].append(slot)

	if outside:
		chart.warnings.append(f"{outside} slots fall outside this week and are not shown")
	if unsectioned:
		chart.warnings.append(
			"no Discipline Branch Config puts these Shift Types in scope, so the chart has no "
			"table for them: " + ", ".join(sorted(unsectioned))
		)
	if overflow_reasons:
		chart.warnings.append(
			f"shown under {layout.overflow_label!r} — " + "; ".join(sorted(overflow_reasons))
		)

	chart.overflow_lanes = tuple(Lane(key, key) for key in sorted(overflow_lane_keys))

	for section in layout.sections:
		for band in layout.bands:
			if section.shift_type not in band.shift_types:
				continue
			used = _fill(chart, pool, section.shift_type, band.key, band.lanes, len(dates))
			chart.heights[(section.shift_type, band.key)] = max(band.rooms, used)
			if used > band.rooms:
				chart.warnings.append(
					f"{section.title}: {band.discipline_label} at {band.branch_label} has {used} "
					f"people on one half-day but only {band.rooms} rooms configured"
				)
		used = _fill(chart, pool, section.shift_type, OVERFLOW, chart.overflow_lanes, len(dates))
		if used:
			chart.heights[(section.shift_type, OVERFLOW)] = used

	uncertain = sum(1 for p in chart.placements if not p.slot.role_certain)
	if uncertain:
		chart.warnings.append(
			f"{uncertain} placements use a role the source did not record, inferred from the "
			"employee's held roles"
		)
	return chart


def _fill(
	chart: Chart,
	pool: dict[tuple[str, str, int, str], list[Slot]],
	shift_type: str,
	band_key: str,
	lanes: tuple[Lane, ...],
	days: int,
) -> int:
	"""Stack one band's claimed slots into rows. Returns the rows used."""
	used = 0
	for lane in lanes:
		for index in range(days):
			here = sorted(pool.get((band_key, lane.key, index, shift_type), []), key=_sort_key)
			used = max(used, len(here))
			for row, slot in enumerate(here, start=1):
				chart.placements.append(Placement(shift_type, band_key, row, lane.key, index, slot))
	return used
