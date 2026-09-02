# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Staging and applying hand-edits to a bound employee's rota. Pure Python, no Frappe.

Pairs with `autoshift.rota.cycle` (the expansion rule) and `autoshift.rota.materialize`
(the read/create-Shift-Assignment half). This module is the logic behind the Rota Editor
page: given the `Rota` rows `materialize.load_rotas` already reads, and a list of staged
`Change`s, compute what `Shift Schedule Assignment` documents need to exist afterwards —
never touching Frappe itself, so the one invariant that matters (a batch of edits produces
a coherent new set of assignments, not a half-applied mess) is testable without a database.

A hand edit never rewrites a `Rota` in place: the assignment it touches is always replaced
wholesale by a fresh one, tagged `custom_manually_edited` — see `autoshift.rota` for why
(a hand-edited rota is gold standard, and zawin2frappe's import must never overwrite one;
the tag is how it recognises which ones are its own). A `Shift Schedule` an edit no longer
needs is only ever deleted if it was itself tagged, i.e. this app created it — a shared,
zawin2frappe-owned schedule is never touched, only unlinked (the one `Shift Schedule
Assignment` row pointing at it for this employee is what changes).

**Periodicity is derived, not identity.** A group of edits is keyed on
`(employee, shift_type, branch)` alone — cadence and anchor are *outcomes* of folding the
changes, not inputs to the grouping. This is what lets a genuinely varying multi-week
pattern (CLAUDE.md's "Multi-week rotas": several same-cadence assignments at different
phases) be edited at all: every one of a group's member rows, whatever their own cadence,
is resampled into a per-view-week `phases` map (`_Group.phases`, `phase -> weekdays`) before
any `Change` is folded in, and `minimal_cycle` reads the *smallest* cadence that map still
needs once every change has landed. Editing a single occurrence inside a view wider than
the pattern's current cadence is therefore exactly how a 1-week rota becomes a 2-week one —
no separate "make this a rota" action exists, or is needed: `apply_changes` notices the
phases stopped agreeing and promotes it, `minimal_cycle` recomputing on every fold means it
demotes back to weekly just as readily if a later edit makes the phases agree again. Emitted
as `EditPlan.cadence_changes`, one line per touched pattern whose cadence actually moved —
the Rota Editor renders these in its transcript.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass

from .cycle import FREQUENCY_LABEL, WEEKDAY_LABEL, Rota, occurrences


def weekday_range_label(weekdays: Iterable[int]) -> str:
	"""Compress a weekday set into a compact label: `{1, 2, 4}` -> `"Tue-Wed, Fri"`.

	Used both for the transcript ("moved from AM Tue-Wed to AM Tue-Fri") and to describe
	a pattern the grid is about to show, so a run of consecutive days reads as a range
	rather than a list.
	"""
	days = sorted(set(weekdays))
	if not days:
		return "none"
	short = {i: WEEKDAY_LABEL[i][:3] for i in range(7)}
	runs: list[tuple[int, int]] = []
	start = prev = days[0]
	for day in days[1:]:
		if day == prev + 1:
			prev = day
			continue
		runs.append((start, prev))
		start = prev = day
	runs.append((start, prev))
	return ", ".join(short[a] if a == b else f"{short[a]}-{short[b]}" for a, b in runs)


def _cadence_label(cycle_weeks: int) -> str:
	return FREQUENCY_LABEL.get(cycle_weeks, f"Every {cycle_weeks} Weeks")


@dataclass(frozen=True)
class Change:
	"""One staged edit, at single-occurrence granularity.

	`op` is one of "add", "move", "remove". A "move" or "remove" identifies the
	occurrence it touches via `from_assignment` (a `Shift Schedule Assignment` docname)
	+ `from_weekday`; an "add" has neither, and needs `company` since there is no source
	`Rota` to take it from. `to_shift_type`/`to_branch` left `None` on a "move" mean
	"unchanged" — most drags only move a weekday within the same pattern, which is
	deliberately the cheap path.

	`from_phase`/`to_phase` say *which* week of the editor's current view the touched
	occurrence lives in (0-indexed from the view's first day) — the whole mechanism
	behind auto-detecting a cadence change, see the module docstring. A view only one
	week wide leaves both at their default 0, which is exactly the pre-multi-week
	behaviour: every occurrence is "the" occurrence.
	"""

	op: str
	employee: str
	company: str | None = None
	to_shift_type: str | None = None
	to_weekday: int | None = None
	to_branch: str | None = None
	to_phase: int = 0
	from_assignment: str | None = None
	from_weekday: int | None = None
	from_phase: int = 0


@dataclass(frozen=True)
class NewAssignment:
	"""A `Shift Schedule Assignment` (+ backing `Shift Schedule`) `apply_changes` says
	must exist afterwards. Always fresh — see the module docstring."""

	employee: str
	company: str
	shift_type: str
	branch: str | None
	weekdays: frozenset[int]
	cycle_weeks: int
	anchor: datetime.date | None


@dataclass(frozen=True)
class EditPlan:
	"""What a batch of `Change`s resolves to: assignments to delete, and their
	replacements to create. An assignment untouched by any change, and one whose net
	effect is a no-op (e.g. a move immediately undone), appears in neither list.

	`cadence_changes` is separate from both: informational lines for a pattern whose
	*cadence* moved as a side effect of folding the batch (a 1-week rota that just
	picked up a second, differing phase; or one that converged back to weekly) — see the
	module docstring. Never itself a reason to delete or create anything beyond what the
	`delete`/`create` lists already say.
	"""

	delete: tuple[str, ...] = ()
	create: tuple[NewAssignment, ...] = ()
	cadence_changes: tuple[str, ...] = ()


def _divisors(n: int) -> list[int]:
	"""Ascending divisors of `n`, `n` itself last — `minimal_cycle` always finds one."""
	return [d for d in range(1, n + 1) if n % d == 0]


def minimal_cycle(phases: dict[int, frozenset[int]], view_weeks: int) -> int:
	"""The smallest cadence a `phase -> weekdays` map over `view_weeks` actually needs:
	the least divisor `d` of `view_weeks` such that phase `p`'s weekdays equal phase
	`p % d`'s, for every `p` in `range(view_weeks)`. `d = view_weeks` always qualifies
	(every phase trivially equals itself), so this never fails to return.
	"""
	empty: frozenset[int] = frozenset()
	for d in _divisors(view_weeks):
		if all(phases.get(p, empty) == phases.get(p % d, empty) for p in range(view_weeks)):
			return d
	return view_weeks  # unreachable — d = view_weeks above always satisfies the check


def _phase_anchor(view_start: datetime.date, phase: int) -> datetime.date:
	"""`create_shifts_after` for a freshly created phase-`phase` row, anchored against
	the editor's current view: the Monday one week before `view_start`'s own phase-`phase`
	week, so `cycle.first_covered_week` resolves back to exactly that week. Only called
	for `cycle_weeks > 1` — a weekly pattern needs no anchor, see `minimal_cycle`.
	"""
	return view_start + datetime.timedelta(weeks=phase - 1)


class _Group:
	"""One `(employee, shift_type, branch)` pattern being folded, as a per-view-week
	`phases` map rather than a flat weekday set — see the module docstring for why."""

	__slots__ = ("branch", "company", "employee", "original_phases", "phases", "shift_type", "sources")

	def __init__(self, employee, shift_type, branch):
		self.employee = employee
		self.shift_type = shift_type
		self.branch = branch
		self.company: str | None = None
		self.phases: dict[int, set[int]] = {}
		self.original_phases: dict[int, frozenset[int]] = {}
		self.sources: set[str] = set()


def apply_changes(
	rotas: Iterable[Rota],
	changes: Iterable[Change],
	view_start: datetime.date | None = None,
	view_weeks: int = 1,
) -> EditPlan:
	"""Fold a batch of `Change`s onto the current `Rota`s, and say what must change in
	the DB to match.

	`view_start`/`view_weeks` are the editor's current view — the window every `Change`'s
	`from_phase`/`to_phase` is relative to, and the span `minimal_cycle` resamples a
	group's actual weekday-per-week content over. `view_start` may be omitted (any Monday
	will do) when every touched pattern is, and stays, weekly: a cadence that never
	exceeds one week is invariant to which week you sample it from.

	A "group" is the unit a single `Shift Schedule Assignment` can represent — nominally
	one `(employee, shift_type, branch)` combination, though a cadence wider than one week
	needs one row per phase. Groups are seeded from whatever `Rota`s already share that
	key, resampled into `phases` (`phase index -> weekdays occupied that week`) so a
	pre-existing multi-phase pattern is read back exactly as it already behaves — a
	weekly member contributes to every phase (cycle 1 ignores phase alignment, see
	`cycle.occurrences`), a wider one only to the phase(s) its own anchor actually reaches.
	"""
	if view_start is None:
		view_start = datetime.date(2000, 1, 3)  # an arbitrary Monday; irrelevant unless
		# some touched pattern's cadence exceeds one week, which needs an explicit
		# view_start to resample correctly (see the module and _phase_anchor docstrings).
	rotas = list(rotas)
	by_name = {r.assignment: r for r in rotas}
	by_key: dict[tuple, list[Rota]] = {}
	for r in rotas:
		by_key.setdefault((r.employee, r.shift_type, r.shift_location), []).append(r)

	def week_bounds(phase: int) -> tuple[datetime.date, datetime.date]:
		start = view_start + datetime.timedelta(weeks=phase)
		return start, start + datetime.timedelta(days=6)

	groups: dict[tuple, _Group] = {}

	def group_for(key) -> _Group:
		if key not in groups:
			employee, shift_type, branch = key
			group = _Group(employee, shift_type, branch)
			members = by_key.get(key, ())
			for r in members:
				group.sources.add(r.assignment)
				group.company = group.company or r.company
			for phase in range(view_weeks):
				start, end = week_bounds(phase)
				group.phases[phase] = {day.weekday() for m in members for day in occurrences(m, start, end)}
			group.original_phases = {p: frozenset(s) for p, s in group.phases.items()}
			groups[key] = group
		return groups[key]

	for change in changes:
		src_rota = by_name.get(change.from_assignment) if change.from_assignment else None

		if change.op in ("move", "remove"):
			if src_rota is None:
				continue  # already gone from a prior change in this batch, or unknown
			src_key = (src_rota.employee, src_rota.shift_type, src_rota.shift_location)
			group_for(src_key).phases[change.from_phase].discard(change.from_weekday)

		if change.op == "move":
			if src_rota is None:
				continue
			shift_type = change.to_shift_type or src_rota.shift_type
			branch = change.to_branch if change.to_branch is not None else src_rota.shift_location
			dst = group_for((change.employee, shift_type, branch))
			dst.phases[change.to_phase].add(change.to_weekday)
			dst.company = dst.company or src_rota.company
		elif change.op == "add":
			dst = group_for((change.employee, change.to_shift_type, change.to_branch))
			dst.phases[change.to_phase].add(change.to_weekday)
			dst.company = dst.company or change.company

	deletes: list[str] = []
	creates: list[NewAssignment] = []
	cadence_changes: list[str] = []

	for group in groups.values():
		final_phases = {p: frozenset(s) for p, s in group.phases.items()}
		if final_phases == group.original_phases:
			continue  # net no-op — e.g. a move immediately undone

		deletes.extend(group.sources)

		had_content = any(group.original_phases.values())
		has_content = any(final_phases.values())
		new_cycle = minimal_cycle(final_phases, view_weeks) if has_content else None

		if has_content:
			if not group.company:
				raise ValueError(
					f"no company known for {group.employee}/{group.shift_type}: "
					"an add's Change must carry one"
				)
			for phase in range(new_cycle):
				weekdays = final_phases[phase]
				if not weekdays:
					continue
				creates.append(
					NewAssignment(
						employee=group.employee,
						company=group.company,
						shift_type=group.shift_type,
						branch=group.branch,
						weekdays=weekdays,
						cycle_weeks=new_cycle,
						anchor=_phase_anchor(view_start, phase) if new_cycle > 1 else None,
					)
				)

		if had_content and has_content:
			old_cycle = minimal_cycle(group.original_phases, view_weeks)
			if old_cycle != new_cycle:
				branch = f" ({group.branch})" if group.branch else ""
				cadence_changes.append(
					f"{group.employee}: {group.shift_type}{branch} periodicity changed from "
					f"{_cadence_label(old_cycle)} to {_cadence_label(new_cycle)}"
				)

	return EditPlan(
		delete=tuple(sorted(deletes)), create=tuple(creates), cadence_changes=tuple(cadence_changes)
	)


def _phase_suffix(phase: int, view_weeks: int) -> str:
	return f" (week {phase + 1})" if view_weeks > 1 else ""


def describe_change(change: Change, rotas: Iterable[Rota], view_weeks: int = 1) -> str:
	"""One transcript line for a staged change, in terms of the pattern it touches
	rather than the single day — "moved from AM Tue-Wed to AM Tue-Fri" reads a lot more
	like what happened than "moved Wed to Fri" does. `view_weeks > 1` additionally names
	the touched week ("Friday (week 2)") wherever a bare weekday is mentioned, since a
	weekday alone stops identifying one occurrence once more than one week is in view.

	Takes the `Rota`s as they stood *before* this change (the caller folds changes in one
	at a time when building a transcript, so each line is frozen against the state it was
	actually staged against, not recomputed later against a batch that has since moved on).
	"""
	by_name = {r.assignment: r for r in rotas}
	weekday = WEEKDAY_LABEL.get(change.to_weekday) if change.to_weekday is not None else None
	from_weekday = WEEKDAY_LABEL.get(change.from_weekday) if change.from_weekday is not None else None
	to_suffix = _phase_suffix(change.to_phase, view_weeks)
	from_suffix = _phase_suffix(change.from_phase, view_weeks)

	if change.op == "remove":
		src = by_name.get(change.from_assignment)
		shift_type = src.shift_type if src else "?"
		branch = f" at {src.shift_location}" if src and src.shift_location else ""
		return f"{change.employee}: removed {shift_type} {from_weekday}{from_suffix}{branch}"

	if change.op == "add":
		branch = f" at {change.to_branch}" if change.to_branch else ""
		return f"{change.employee}: added {change.to_shift_type} {weekday}{to_suffix}{branch}"

	# move
	src = by_name.get(change.from_assignment)
	if src is None:
		return f"{change.employee}: moved {from_weekday}{from_suffix} to {change.to_shift_type} {weekday}{to_suffix}"
	same_pattern = (change.to_shift_type or src.shift_type) == src.shift_type and (
		change.to_branch if change.to_branch is not None else src.shift_location
	) == src.shift_location
	before = weekday_range_label(src.weekdays)
	if same_pattern and change.from_phase == change.to_phase:
		after = weekday_range_label((src.weekdays - {change.from_weekday}) | {change.to_weekday})
		branch = f" ({src.shift_location})" if src.shift_location else ""
		week = _phase_suffix(change.to_phase, view_weeks)
		return f"{change.employee}: moved {src.shift_type}{branch}{week} from {before} to {after}"
	from_branch = f" ({src.shift_location})" if src.shift_location else ""
	to_branch = f" ({change.to_branch})" if change.to_branch else ""
	return (
		f"{change.employee}: moved {from_weekday}{from_suffix} from {src.shift_type}{from_branch} "
		f"to {change.to_shift_type}{to_branch} {weekday}{to_suffix}"
	)


def rota_view_weeks(cycle_weeks: int, view_weeks: int) -> bool:
	"""Can a rota of this cadence be drawn, undistorted, in a view this wide?

	True exactly when the view tiles evenly over the cadence: a weekly rota repeats to
	fill a wider view (shown identically in every week of it), and a rota wider than the
	view cannot be represented at all in a narrower one — only one phase of it would be
	visible, which is not its actual pattern and would misreport as a half-empty week.
	Such an employee is filtered out of that view rather than shown wrong — see
	`phase_fractions` for what it is shown instead.
	"""
	return cycle_weeks > 0 and view_weeks % cycle_weeks == 0


def phase_fractions(
	rotas: Iterable[Rota],
	shift_type: str,
	weekdays: Iterable[int],
	view_start: datetime.date,
	cycle_weeks: int,
) -> dict[int, tuple[int, str | None]]:
	"""What a period-incompatible employee's row shows instead of one misleading
	single-phase snapshot: for each of `weekdays`, how many of `shift_type`'s own
	`cycle_weeks` actually put them on that weekday, averaged over one full cycle rather
	than read off whichever phase the current view happens to land on (see
	`rota_view_weeks`) — the fraction is invariant to which `cycle_weeks`-long span of
	weeks you sample, since it is one full cycle either way.

	Returns `{weekday: (occupied, branch)}`, `occupied` in `[0, cycle_weeks]`; `branch`
	is the one location every occupied week agrees on, or `None` if none occupy it or
	they disagree. `occupied == cycle_weeks` is not fractional at all — a weekday that
	simple deserves a real chip, which the caller decides, not this function.
	"""
	members = [r for r in rotas if r.shift_type == shift_type]
	weeks = [
		(view_start + datetime.timedelta(weeks=i), view_start + datetime.timedelta(weeks=i, days=6))
		for i in range(max(cycle_weeks, 1))
	]
	result: dict[int, tuple[int, str | None]] = {}
	for weekday in weekdays:
		occupied = 0
		branches: set[str | None] = set()
		for start, end in weeks:
			hit_branches = {
				m.shift_location
				for m in members
				for day in occurrences(m, start, end)
				if day.weekday() == weekday
			}
			if hit_branches:
				occupied += 1
				branches |= hit_branches
		result[weekday] = (occupied, next(iter(branches)) if len(branches) == 1 else None)
	return result
