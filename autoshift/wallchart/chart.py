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

Two things the paper sheet never had either, and this chart only has where the
run measured them (gh#9, `room_coverage_matched_rooms`):

  **room identity** — where `Slot.room_index` is empty (no run, a pooled-coverage
  run, a non-gating role, a book slot), a band's rows are still numbered only
  because chairs are: each lane fills independently, top to bottom, sorted by
  label or by `Slot.sort_value` — see `_order_lane`. Row 2 is the second person
  on that half-day, not room 2. Where a slot carries a solved room index, that
  index **is** the row: room 2 is drawn on line 2 whatever else is on the chart,
  so two lanes' slots sharing an index share a row.
  **A room nobody was matched into therefore stays an empty line.** Nothing in
  the model prefers a low room number — every room of a band is interchangeable,
  so leaving room 1 shut and opening rooms 2 and 3 is one of many co-optimal
  answers — and closing the gap up would draw that as rooms 1 and 2, i.e. as a
  schedule the solver did not produce. A reader chasing "why is room 1 empty"
  should land on the right question (there is no such preference) rather than on
  a chart that quietly renumbered the answer.

  **pairing** — a genuine cross-lane match (see above) is the only thing that
  puts two lanes' slots on the same row on purpose; anything else landing
  together is `_order_lane`'s incidental fill order, same as it always was.
  A multi-room slot whose solved indices are not contiguous (nothing in the model
  requires a holder's rooms to be adjacent) falls back to the incidental
  placement, rather than draw a chip with a gap in it.

Both stay stable across rebuilds of the same data — real room identity because
`room_index` is itself stable across rebuilds of one solved run, and incidental
placement because `_order_lane`'s ordering is deterministic — which is what
matters for a chart someone reads every week.
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
	#: Rooms this person covers in this half-day. The chip is drawn that many rows
	#: tall, because a practitioner covering two rooms occupies two of the band's
	#: lines and the paper sheet has always drawn them that way. From a run whose
	#: ruleset measured it (`room_load_objective`), the rooms actually taken;
	#: otherwise `Scheduling Role.max_rooms`, or the holder's own override.
	rooms: int = 1
	#: The holder's max-rooms figure, where `rooms` is the measured load and may be
	#: less; 0 where `rooms` already is that figure. Only ever read for the tooltip.
	max_rooms: int = 0
	kind: str = KIND_EXISTING
	#: The run pinned this rather than choosing it (warm start / role binding).
	forced: bool = False
	#: False when the role was inferred from the employee's held roles rather
	#: than read from the source. A Shift Assignment records no role.
	role_certain: bool = True
	#: Set on a `kept` slot the run moved: what it says here differs from what is
	#: on the books. Human-readable, e.g. "was at Blandonnet".
	changed: str | None = None
	#: This employee's value of `Scheduling Role.chip_sort_field`, resolved by
	#: `source.py` — `None` when the role names no field, the field does not
	#: exist, or the employee has no value for it. Drives row order within a
	#: lane's day in place of the alphabetical default; see `_order_lane`.
	sort_value: object | None = None
	#: Not a record: a bound employee's rota day that no Shift Assignment covers yet,
	#: standing in for the one it would materialise as. The optimizer reads it as on
	#: the books, so it compares like one; the chart only draws it differently.
	virtual: bool = False
	#: `Optimizer Run Slot.room_index`, parsed — the specific room number(s) this
	#: assignment was matched into under `room_coverage_matched_rooms`. Empty unless
	#: that rule measured it: a book slot, a non-gating role, or a run that used the
	#: pooled `room_coverage` all leave this empty, and `_fill` places them exactly as
	#: it always has. Sparse by construction, the same convention as everywhere else
	#: a figure is "real where measured, absent otherwise".
	room_index: tuple[int, ...] = ()

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
	#: `Scheduling Role.chip_sort_descending` for this lane's role. No-op unless
	#: the role also names a `chip_sort_field` — see `_order_lane`.
	sort_descending: bool = False
	#: `Scheduling Role.gates_rooms`. A room is open only where *every* gating
	#: lane of its band is filled on that line, which is what `_coverage` counts
	#: and `room_coverage` enforces. A non-gating lane (a lead duty, a floater)
	#: is drawn like any other and simply does not decide whether a room counts.
	gates_rooms: bool = True


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
	#: Rows this placement occupies, starting at `row`. `slot.rooms`, except where
	#: it would run past what the band can draw.
	span: int = 1


@dataclass
class Chart:
	"""A layout, a week, and everything that landed in it."""

	layout: Layout
	monday: datetime.date
	placements: list[Placement] = field(default_factory=list)
	#: Rows actually drawn, per (shift_type, band key).
	heights: dict[tuple[str, str], int] = field(default_factory=dict)
	#: Rooms genuinely open, per (shift_type, band key, day index): **which rows**
	#: every gating lane staffs, ascending. Any other row is either empty or
	#: staffed by somebody but not by everybody the room needs, and the chart greys
	#: it — a half-staffed room is not an open room, and drawing it like one is how
	#: a chart lies. Rows, not a count, because a solved room index can leave a
	#: genuine hole: rooms 2 and 3 open with room 1 shut is not "two rooms from the
	#: top", and the hatching has to follow the real lines.
	covered: dict[tuple[str, str, int], tuple[int, ...]] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)
	#: The lanes the overflow band needed, if any.
	overflow_lanes: tuple[Lane, ...] = ()

	@property
	def dates(self) -> list[datetime.date]:
		return week_dates(self.monday)

	def height(self, shift_type: str, band_key: str) -> int:
		return self.heights.get((shift_type, band_key), 0)

	def covered_rooms(self, shift_type: str, band_key: str, day_index: int) -> int:
		"""How many rooms are open. The headline figure; `covered_rows` places them."""
		return len(self.covered.get((shift_type, band_key, day_index), ()))

	def covered_rows(self, shift_type: str, band_key: str, day_index: int) -> tuple[int, ...]:
		return self.covered.get((shift_type, band_key, day_index), ())

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
		merged.append(_recast(slot, KIND_KEPT, changed=_describe_move(slot, match), virtual=match.virtual))
	# Whatever is left was on the books and did not survive the solve.
	merged.extend(_recast(slot, KIND_DROPPED) for slot in by_key.values())
	return merged


def _recast(slot: Slot, kind: str, changed: str | None = None, virtual: bool | None = None) -> Slot:
	"""`slot` with a comparison verdict attached. Frozen dataclass, so a copy.

	`virtual` defaults to the slot's own; a `kept` slot takes its book side's, since
	whether the half-day is recorded yet is a fact about the books, not the run.
	"""
	return Slot(
		date=slot.date,
		shift_type=slot.shift_type,
		employee=slot.employee,
		employee_name=slot.employee_name,
		label=slot.label,
		branch=slot.branch,
		scheduling_role=slot.scheduling_role,
		rooms=slot.rooms,
		max_rooms=slot.max_rooms,
		kind=kind,
		forced=slot.forced,
		role_certain=slot.role_certain,
		changed=changed,
		sort_value=slot.sort_value,
		virtual=slot.virtual if virtual is None else virtual,
		room_index=slot.room_index,
	)


def _sort_key(slot: Slot) -> tuple[str, str]:
	return (slot.label.upper(), slot.employee)


def _order_lane(slots: list[Slot], descending: bool) -> list[Slot]:
	"""Row order within one lane's day.

	Alphabetical by label first, so ties break the same way whether or not a
	sort field is configured. Slots that carry a `sort_value` are then
	stable-sorted to the front by it (reversed when `descending`); slots
	without one — the role names no `chip_sort_field`, the field does not
	exist, or this employee has no value for it — stay in alphabetical order
	after every ranked one, rather than raising or guessing.

	`dropped` slots sink to the bottom of the lane whatever their label says.
	They are on the books and the run does not schedule them, so they are not
	part of the proposal filling the rooms: keeping them above it would leave a
	room covered on paper by somebody the run has sent home, and would break the
	run of covered lines that `_coverage` counts and the chart greys below.
	"""
	tied = sorted(slots, key=_sort_key)
	ranked = [s for s in tied if s.sort_value is not None]
	unranked = [s for s in tied if s.sort_value is None]
	ranked.sort(key=lambda s: s.sort_value, reverse=descending)
	ordered = ranked + unranked
	return [s for s in ordered if s.kind != KIND_DROPPED] + [s for s in ordered if s.kind == KIND_DROPPED]


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
					f"{section.title}: {band.discipline_label} at {band.branch_label} covers {used} "
					f"rooms on one half-day but only {band.rooms} are configured"
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
	"""Stack one band's claimed slots into rows. Returns the rows used.

	A slot covering more than one room occupies that many consecutive lines, so
	the next person in the lane starts below it rather than beside it. The rows a
	lane reaches on a day are therefore its slots' rooms added up, which is the
	same sum `room_coverage` puts on the left of its inequality — so the coverage
	`_coverage` reads back out of this is the chart's own arithmetic, not a second
	opinion about it. Where the run measured each holder's load
	(`room_load_objective`) that sum is the rooms actually open, not merely an
	upper bound on them.

	Slots carrying a `room_index` (`room_coverage_matched_rooms` measured it) are
	placed first, at exactly that room's row rather than by fill order, so two
	lanes' slots sharing an index land on the same row (a real pairing) and a room
	nobody was matched into stays an empty line — see the module docstring for why
	that hole is the truth and not a gap to close. Everything else fills the
	remaining rows exactly as it always has.
	"""
	used = 0
	#: (day index, lane key) -> the rows this lane *staffs* that day
	reach: dict[tuple[int, str], frozenset[int]] = {}
	for index in range(days):
		for lane in lanes:
			here = _order_lane(pool.get((band_key, lane.key, index, shift_type), []), lane.sort_descending)
			pinned: list[tuple[int, Slot]] = []
			unpinned: list[Slot] = []
			for slot in here:
				rows = sorted(slot.room_index)
				if rows and rows[-1] - rows[0] + 1 == len(rows):
					pinned.append((rows[0], slot))
				else:
					unpinned.append(slot)

			occupied: set[int] = set()
			#: `occupied` minus what the run sent home — see `_order_lane`. A dropped
			#: chip holds its line on the chart and staffs nothing, which is why the
			#: two sets are tracked apart rather than one being derived from the other.
			staffed: set[int] = set()
			for row, slot in pinned:
				span = max(int(slot.rooms or 1), 1)
				chart.placements.append(Placement(shift_type, band_key, row, lane.key, index, slot, span))
				occupied.update(range(row, row + span))
				if slot.kind != KIND_DROPPED:
					staffed.update(range(row, row + span))

			row = 1
			for slot in unpinned:
				span = max(int(slot.rooms or 1), 1)
				while occupied.intersection(range(row, row + span)):
					row += 1
				chart.placements.append(Placement(shift_type, band_key, row, lane.key, index, slot, span))
				occupied.update(range(row, row + span))
				if slot.kind != KIND_DROPPED:
					staffed.update(range(row, row + span))
				row += span

			reach[(index, lane.key)] = frozenset(staffed)
			used = max(used, max(occupied, default=0))

	gating = [lane.key for lane in lanes if lane.gates_rooms]
	for index in range(days):
		chart.covered[(shift_type, band_key, index)] = _coverage(reach, index, gating)
	return used


def _coverage(
	reach: dict[tuple[int, str], frozenset[int]], day_index: int, gating: list[str]
) -> tuple[int, ...]:
	"""Rooms open on one day of one band: the rows *every* gating lane staffs.

	The intersection, which is how `rules.room_coverage`'s minimum reads once rows
	are rooms — a room needs each of the roles it is defined by, so a line with a
	practitioner and no assistant is half-staffed rather than open, and the count
	of open rooms is how many lines survive the intersection. Where no run measured
	room identity every lane fills from the top, the intersection is a prefix, and
	this is the same figure the minimum always gave. A band with no gating lane at
	all covers nothing: there is no role to say a room is open, which is the same
	answer the solver gives.
	"""
	if not gating:
		return ()
	rows = set.intersection(*(set(reach.get((day_index, key), frozenset())) for key in gating))
	return tuple(sorted(rows))
