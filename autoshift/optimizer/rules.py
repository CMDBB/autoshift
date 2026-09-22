"""
Optimization rule registry: the MILP constraint groups and objective terms,
as selectable named rules.

Pure Python (no Frappe imports) — safe to use in tests.

Each built-in rule is a function taking a :class:`RuleContext` and either adding
constraints (or fixing variables) on the PuLP problem, or contributing objective
terms via :meth:`RuleContext.add_objective`. Rules are registered in
``BUILTIN_RULES`` under a stable key; an ``Optimization Rule`` document with
``implementation_type = "Built-in"`` points at one of these keys.

Custom rules live as Python source on an ``Optimization Rule`` document
(``implementation_type = "Custom Code"``). The source must define a function
``apply(ctx)`` and only runs once a developer has checked ``validated`` on the
document. It is compiled here at solve time via :func:`compile_custom_rule`.

Which rules apply to a given solve comes from ``DataPackage.rules`` — the
``(rule_document_name, builtin_key, custom_code, weight)`` tuples loaded from
the run's ``Optimization Ruleset``. ``weight`` scales the rule's objective
contribution (it has no effect on constraint rules). An empty ``rules`` tuple
means "apply every built-in rule at weight 1.0" (the pre-ruleset behaviour,
still used by the unit tests).
"""

from __future__ import annotations

import contextlib
import datetime
import io
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FunctionType
from typing import TYPE_CHECKING

import pulp

from .types import MODE_COLLATERAL, MODE_EXCLUSIVE, DataPackage

KIND_CONSTRAINT = "Constraint"
KIND_OBJECTIVE = "Objective"
KIND_MIXED = "Mixed"
KIND_OTHER = "Other"

# Topics: an optional heading a rule files itself under, purely to give Optimizer Studio's
# toggle panel collapsible sections. Orthogonal to `BuiltinRule.group` — a group is a
# mutual-exclusion choice set the solver enforces, a topic constrains nothing. Seeded as
# `Scheduling Rule Topic` documents (is_system) so Custom Code rules can file themselves
# under one too; TOPIC_ORDER is their display order.
TOPIC_EXISTING_ASSIGNMENTS = "Existing Assignments"
TOPIC_AVAILABILITY = "Availability & Workload"
TOPIC_COVERAGE = "Room Coverage"
TOPIC_PREFERENCES = "Preferences"

TOPIC_ORDER: tuple[str, ...] = (
	TOPIC_COVERAGE,
	TOPIC_AVAILABILITY,
	TOPIC_EXISTING_ASSIGNMENTS,
	TOPIC_PREFERENCES,
)

TOPIC_DESCRIPTIONS: dict[str, str] = {
	TOPIC_COVERAGE: "How many rooms open, and how hard the schedule tries to fill them.",
	TOPIC_AVAILABILITY: "When somebody may work at all: leave, one-shift-a-day, and FTE limits.",
	TOPIC_EXISTING_ASSIGNMENTS: "What the optimizer does with Shift Assignments already on the books.",
	TOPIC_PREFERENCES: "Whose stated shift and branch preferences the schedule tries to honor.",
}


# ── Objective breakdown: where a rule's share of the objective was earned ─────
#
# An objective rule contributes its value in many small pieces — a room staffed here,
# a preference charged there — and "this rule scored 412" is rarely the number a planner
# wants. `path()` labels each piece with the place in the schedule it came from, so the
# run's breakdown can be drilled into down to the individual half-day and the rooms it
# staffs. It is reporting only and never reaches the model.

# One (level name, label) step of a breakdown path, coarse first: the levels are the
# columns of the tree a reader expands, so `Discipline -> Branch -> Day -> Shift` reads
# down the page while `Shift -> Discipline` does not.
PATH = tuple[tuple[str, str], ...]

# Level names shared across rules. Two rules that decompose by employee must spell the
# level the same way, or the reporting layer (which relabels employee ids with their
# names, say) cannot recognize it.
LEVEL_EMPLOYEE = "Employee"
LEVEL_ROLE = "Role"
LEVEL_DISCIPLINE = "Discipline"
LEVEL_BRANCH = "Branch"
LEVEL_DAY = "Day"
LEVEL_SHIFT = "Shift"


def path(**levels: object) -> PATH:
	"""Build a breakdown path for :meth:`RuleContext.add_objective`.

	Keyword order is the tree's nesting order, coarse level first::

	    ctx.add_objective(rooms, path(Discipline=k, Branch=b, Day=d, Shift=s))

	An underscore in a level name reads as a space (``Shift_type`` -> ``"Shift type"``),
	and every label is stringified, so dates and docnames can be passed as they are.
	"""
	return tuple((level.replace("_", " "), str(value)) for level, value in levels.items())


@dataclass
class RuleContext:
	"""Everything a rule may act on: the problem, its variables, and the input data."""

	prob: pulp.LpProblem
	x: dict[tuple, pulp.LpVariable]  # x[employee, role, shift, day, branch] binary
	active_rooms: dict[tuple, pulp.LpVariable]  # ar[discipline, shift, day, branch] integer
	data: DataPackage
	# p[employee, shift, day, branch] binary: is this person *in* for that shift, whatever
	# they end up doing. `model_builder` ties it to `x` — at most one working role per
	# presence, collateral duties beside it, never a presence spent on nothing. Rules that
	# count somebody's workload or freeze their week work on presence, not on `x`: which
	# role a settled half-day is worked in is exactly what the optimizer may still decide.
	presence: dict[tuple, pulp.LpVariable] = field(default_factory=dict)
	# x[employee, role, shift, day, branch] -> that assignment's room tranches beyond its
	# first, filled by `room_load_objective` for every gating assignment (an empty list for a
	# one-room holder). An assignment's rooms taken are `x + Σ tranches`; the solver persists
	# them so the wall chart draws a chip as tall as the rooms it takes, not the most it could.
	# Empty when that rule is not selected: then nothing says who takes which room.
	room_load: dict[tuple, list[pulp.LpVariable]] = field(default_factory=dict)
	# y[employee, role, shift, day, branch, room_index] binary: filled by
	# `room_coverage_matched_rooms` for every (employee, gating role) pair that can hold a
	# specific numbered room (1..Discipline Branch Config.rooms_num). Empty unless that rule
	# is selected — the legacy pooled `room_coverage` never creates one.
	room_occupancy: dict[tuple, pulp.LpVariable] = field(default_factory=dict)
	# room_active[discipline, shift, day, branch, room_index] binary: an AND, across every
	# gating role in the discipline, of "is this specific room's slot for that role filled" —
	# the per-room counterpart to the pooled `active_rooms`, which stays tied to
	# `Σ_n room_active[..., n]` so every rule reading `active_rooms` keeps working unchanged
	# whichever room-coverage rule is selected. Empty unless `room_coverage_matched_rooms` is.
	room_active: dict[tuple, pulp.LpVariable] = field(default_factory=dict)

	# objective terms accumulated by the applied rules; model_builder sums these
	# into the problem's (maximized) objective after all rules have run
	objective_terms: list = field(default_factory=list)
	# the same terms, keyed by the contributing rule's document name and then by the
	# `path()` the rule filed each one under, so a solved problem can report not only
	# each rule's share of the objective but where inside the schedule that share was
	# earned (see `objective_tree` and solver.run_solve). A rule that passes no path
	# files everything under the empty path: one undecomposed total.
	objective_contributions: dict[str, dict[PATH, list]] = field(default_factory=dict)
	# ruleset row weight / document name of the rule currently being applied
	# (both set by apply_rules)
	_current_weight: float = 1.0
	_current_rule: str = ""

	def add_objective(self, term, path: PATH = ()) -> None:
		"""Contribute a term to the maximized objective, scaled by the rule's ruleset weight.

		``path`` is where this term sits in the run's objective breakdown, built by
		:func:`path` — ``path(Discipline=k, Branch=b, Day=d, Shift=s)``. It is reporting
		only: the model is the same whether a rule contributes one summed term or the
		same value split over a thousand labelled ones, and a rule that wants no
		breakdown just leaves it out. Terms filed under the same path are added
		together, so a rule may file two different sums (a per-presence value and a
		per-assignment surcharge, say) under one label without inventing a level to
		tell them apart.
		"""
		weighted = self._current_weight * term
		self.objective_terms.append(weighted)
		rule = self.objective_contributions.setdefault(self._current_rule or "(unattributed)", {})
		rule.setdefault(path, []).append(weighted)


@dataclass(frozen=True)
class BuiltinRule:
	key: str
	title: str
	description: str
	apply: Callable[[RuleContext], None]
	excludes: dict[str, str]
	requires: dict[str, str]
	kind: str = KIND_CONSTRAINT
	# Weight a freshly-seeded Optimization Ruleset row gets for this rule. Only meaningful
	# on Objective/Mixed rules (a constraint rule's weight is a no-op), and only a *default*:
	# the seeding never overwrites a weight already on a row, so a hand-tuned ruleset keeps
	# its own figure. Objective units are loosely calibrated against each other, so a rule
	# whose natural scale differs from the others declares it here rather than relying on
	# every ruleset author to discover it.
	default_weight: float = 1.0
	# Choice group: at most one rule sharing a given group may appear in a ruleset. Unlike
	# `excludes` (pairwise, hand-authored per rule) a group names the whole mutually-exclusive
	# set in one place, and "none of them" is always a legal choice — there is no rule that
	# means "ignore". Built-in only; Custom Code rules have no group and are never checked
	# against one (`check_ruleset` only looks rules up by builtin key).
	group: str | None = None
	# Optional presentation-only heading (one of TOPIC_*); see the topic constants above.
	topic: str | None = None

	@staticmethod
	def check_ruleset(ruleset: set[str]) -> None:
		for rule in ruleset:
			if rule not in BUILTIN_RULES:
				continue
			for ex, reason in BUILTIN_RULES[rule].excludes.items():
				if ex in ruleset:
					raise ValueError((reason or "{r} is incompatible with {ex}").format(r=rule, ex=ex))
			for req, reason in BUILTIN_RULES[rule].requires.items():
				if req not in ruleset:
					raise ValueError((reason or "{r} requires {req}").format(r=rule, req=req))

		groups: dict[str, list[str]] = {}
		for rule in ruleset:
			group = BUILTIN_RULES[rule].group if rule in BUILTIN_RULES else None
			if group:
				groups.setdefault(group, []).append(rule)
		for group, members in groups.items():
			if len(members) > 1:
				raise ValueError(
					f"{' and '.join(sorted(members))} are mutually exclusive choices in the "
					f"{group!r} group; a ruleset may include at most one."
				)


# Choice groups: see `BuiltinRule.group`. Named here because more than one place has to
# reason about the set as a whole (`data_loader.binding_rule_gap`, the pre-solve confirms).
GROUP_EXISTING_ASSIGNMENTS = "existing_assignments"
GROUP_ROLE_BINDING = "role_binding"
GROUP_WORKLOAD_CEILING = "workload_ceiling"
GROUP_SHIFT_PREFERENCE = "shift_preference"
GROUP_COLLATERAL_VALUE = "collateral_value"
GROUP_ROOM_COVERAGE = "room_coverage"
GROUP_ROOM_VALUE = "room_value_choice"


BUILTIN_RULES: dict[str, BuiltinRule] = {}  # empty -> filled by the builtin_rule decorator
STANDARD_RULES: set[str] = set()  # idem


def builtin_rule(
	title: str,
	description: str,
	kind: str = KIND_CONSTRAINT,
	standard: bool = False,
	requires: dict[FunctionType, str] | None = None,
	excludes: dict[FunctionType, str] | None = None,
	group: str | None = None,
	default_weight: float = 1.0,
	topic: str | None = None,
):
	"""Register a function as a built-in optimization rule."""

	def register(fn: FunctionType[[RuleContext], None]):
		BUILTIN_RULES[fn.__name__] = BuiltinRule(
			key=fn.__name__,
			title=title,
			description=description,
			apply=fn,
			kind=kind,
			requires={k.__name__: v for k, v in (requires or {}).items()},
			excludes={k.__name__: v for k, v in (excludes or {}).items()},
			group=group,
			default_weight=default_weight,
			topic=topic,
		)
		if standard:
			STANDARD_RULES.add(fn.__name__)
		return fn

	return register


def _cname(*parts) -> str:
	"""Sanitize parts into a PuLP-safe constraint name."""
	return (
		":".join(
			[
				str(parts[0]),
				"_".join(str(p) for p in parts),
			]
		)
		.replace("-", "_")
		.replace(" ", "_")
	)


def _vname(*parts) -> str:
	"""Sanitize parts into a PuLP-safe variable name.

	The counterpart to `_cname`, for rules that introduce auxiliary variables — a
	linearized absolute value, say. Same prefix convention, so sandbox tooling can group
	a rule's variables the way `constraint_frame` groups its constraints.
	"""
	return _cname(*parts)


# ── Built-in rules (formerly hardcoded in model_builder.build) ────────────────


@builtin_rule(
	"One shift per employee per day",
	"An employee is present for at most one shift per day, across all shift types and "
	"branches. Holding a second role widens where somebody can be scheduled, never how much "
	"they can work, and a collateral duty worked alongside their shift is not a second shift. "
	"Two half-days on one date are refused even where their times do not overlap: shift "
	"loadouts commonly do overlap, and a genuine double shift is better described by a Shift "
	"Type of its own.",
	standard=True,
	topic=TOPIC_AVAILABILITY,
)
def one_shift_per_day(ctx: RuleContext) -> None:
	data = ctx.data
	for e, d in itertools.product(data.employees, data.working_days):
		ctx.prob += (
			pulp.lpSum(ctx.presence[(e, s, d, b)] for s in data.shift_types for b in data.branches) <= 1,
			_cname("one_shift", e, d),
		)


@builtin_rule(
	"Use existing Shift Assignments as a baseline",
	"This provides a soft tie-breaking towards existing assignments, "
	"and is used as a baseline for other rules.",
	standard=True,
	kind=KIND_OTHER,
	topic=TOPIC_EXISTING_ASSIGNMENTS,
)
def warm_start(ctx: RuleContext) -> None:
	data = ctx.data
	for comb, var in ctx.x.items():
		e, _r, _s, d, _b = comb
		if comb in data.forced and (e, d) in data.leave_blocked:
			raise ValueError(f"Employee {e} cant both be on leave and scheduled for a shift on {d}")
		var.setInitialValue(1 if comb in data.forced else 0)
	# Presence follows the books the same way, so the rules that freeze a settled week can
	# `fixValue()` a presence variable exactly as they do an assignment variable.
	forced_presence = data.forced_presence()
	for slot, var in ctx.presence.items():
		var.setInitialValue(1 if slot in forced_presence else 0)


@builtin_rule(
	"Respect approved leaves",
	"An employee on approved leave (or a leave this run speculates as approved) is never "
	"assigned a shift on the leave days.",
	standard=True,
	requires={warm_start: "{r} doesn't set its values, include {req}"},
	topic=TOPIC_AVAILABILITY,
)
def leave_blocklist(ctx: RuleContext) -> None:
	data = ctx.data
	day_set = set(data.working_days)
	for e, d in data.leave_blocked:
		if e not in data.employees or d not in day_set:
			continue
		for s in data.shift_types:
			for b in data.branches:
				for r in data.employee_roles.get(e, ()):
					ctx.x[(e, r, s, d, b)].fixValue()
				# a collateral duty is still work, so leave blocks the whole presence
				ctx.presence[(e, s, d, b)].fixValue()


@builtin_rule(
	"Honor existing Shift Assignments",
	"Every Shift Assignment already on the books is pinned on, for every employee. Off by "
	"default: a practice's own history is rarely feasible under the rest of the ruleset — "
	"weeks that were worked short-handed, double-booked or off-config pin the model into "
	"infeasibility — so the books are a soft tie-break unless you say otherwise. To freeze "
	"only the people whose schedule is genuinely not the planner's to set, use "
	"<b>Bind settled schedules</b> instead.",
	standard=False,
	requires={warm_start: "{r} doesn't set its values, include {req}"},
	group=GROUP_EXISTING_ASSIGNMENTS,
	topic=TOPIC_EXISTING_ASSIGNMENTS,
)
def use_existing_assignments(ctx: RuleContext) -> None:
	for comb in ctx.data.forced:
		match ctx.x.get(comb):
			case None:
				pass
			case x:
				x.fixValue()


@builtin_rule(
	"Bind settled schedules (strictly)",
	"Somebody holding a Scheduling Role whose assignments are binding is present for exactly "
	"the half-days already on their books — the optimizer may not add one or drop one. Which "
	"<i>role</i> they work during a settled half-day is still the optimizer's to choose among "
	"the roles they hold, because a rota settles when a person is in, not what the demand that "
	"day turns out to be; an Exclusive role is the exception and keeps its shift exactly as "
	"booked, as does a collateral duty. For a role whose week is settled by its holders rather "
	"than by the planner. Individual holders opt out with a Binding Override on their Employee "
	"Scheduling Role — somebody whose schedule has not settled yet is scheduled normally. "
	"Approved leave still wins over a settled assignment. Inert until some Scheduling Role is "
	"marked binding.",
	standard=False,
	requires={warm_start: "{r} fixes variables at their warm-start values, include {req}"},
	group=GROUP_ROLE_BINDING,
	topic=TOPIC_EXISTING_ASSIGNMENTS,
)
def bind_role_assignments(ctx: RuleContext) -> None:
	"""Presence equals the books, day for day; only the role worked stays open.

	Bound employees, not bound pairs: presence is personal (see `DataPackage.bound_employees`).
	A day with nothing on the books stays empty, which is the half of "settled" that stops a
	holder quietly growing a schedule that was never the planner's to set — filling those gaps
	is the free-seat question, deliberately out of scope.
	"""
	data = ctx.data
	bound = data.bound_employees()
	if not bound:
		return
	forced_presence = data.forced_presence()
	booked_slots = {(e, s, d) for e, s, d, _b in forced_presence}

	branches_of_slot: dict[tuple, list] = {}
	for (e, s, d, _b), var in ctx.presence.items():
		if e not in bound:
			continue
		if (e, s, d) in booked_slots:
			branches_of_slot.setdefault((e, s, d), []).append(var)
		else:
			var.fixValue()  # warm_start put it at 0: a day off stays a day off
	for (e, s, d), variables in branches_of_slot.items():
		# exactly one presence, at a branch of the optimizer's choosing
		ctx.prob += (pulp.lpSum(variables) == 1, _cname("bind_presence", e, s, d))

	# A role that cannot be swapped out of, and a duty that is not a way of spending a shift,
	# are both settled as booked rather than merely implied by the presence.
	for comb in data.forced:
		e, r, *_ = comb
		if e in bound and data.mode(e, r) in (MODE_EXCLUSIVE, MODE_COLLATERAL):
			ctx.x[comb].fixValue()
	for comb, var in ctx.x.items():
		e, r, *_ = comb
		if e in bound and data.mode(e, r) == MODE_COLLATERAL and comb not in data.forced:
			var.fixValue()  # no collateral duty the books do not already record


@builtin_rule(
	"Bind settled schedules",
	"The same settled-schedule scope as the strict rule, one step weaker: a bound holder is "
	"never in on a half-day their books do not already have, but the half-days they do have "
	"are proposed rather than nailed down. The optimizer keeps them where they pay for "
	"themselves and may drop one where keeping it would make the whole schedule infeasible — "
	"a week that was worked double-booked or off-config comes back as a schedule instead of "
	"failing outright. The role worked during a settled half-day is the optimizer's to choose, "
	"an Exclusive one excepted: there the shift is either worked as booked or not at all. "
	"Collateral duties stay free, since demand decides those. On by default; choose the strict "
	"rule when a settled schedule may not be touched at all, and add <b>Conserve Existing "
	"Assignments</b> for a tie-break toward keeping them. Inert until some Scheduling Role is "
	"marked binding.",
	standard=True,
	requires={warm_start: "{r} supplies the values this rule fixes and suggests, include {req}"},
	group=GROUP_ROLE_BINDING,
	topic=TOPIC_EXISTING_ASSIGNMENTS,
)
def soft_bind_role_assignments(ctx: RuleContext) -> None:
	"""Half of `bind_role_assignments`: the ceiling on a settled week, without the floor.

	A half-day the books do not have is fixed to 0 (warm_start already put those presence
	variables at 0, so `fixValue()` nails them there); a half-day they do have is left free at
	its warm-start value of 1, so the optimizer may drop it rather than fail the whole solve.
	Note that CBC is not currently invoked with `warmStart=True`, so that initial value is a
	proposal on paper only — what actually keeps a settled shift is the objective. Pair with
	`weigh_assignments_objective` when the books should win a tie outright.
	"""
	data = ctx.data
	bound = data.bound_employees()
	if not bound:
		return
	booked_slots = {(e, s, d) for e, s, d, _b in data.forced_presence()}
	for (e, s, d, _b), var in ctx.presence.items():
		if e in bound and (e, s, d) not in booked_slots:
			var.fixValue()

	# An exclusive booking is worked as booked or not at all: being in for that half-day
	# implies that very role at that very branch, but nothing forces them to be in.
	for comb in data.forced:
		e, r, s, d, b = comb
		if e not in bound or data.mode(e, r) != MODE_EXCLUSIVE:
			continue
		ctx.prob += (
			pulp.lpSum(ctx.presence[(e, s, d, branch)] for branch in data.branches) <= ctx.x[comb],
			_cname("soft_bind_exclusive", e, r, s, d, b),
		)


@builtin_rule(
	"One Branch per Shift",
	"Employees can't cover more than one branch during a single shift. Redundant while "
	"<b>One shift per employee per day</b> is in the ruleset, which already allows a single "
	"presence a day; on its own it lets somebody work both half-days of a date, but never in "
	"two places at once.",
	standard=False,
	topic=TOPIC_COVERAGE,
)
def one_branch_per_shift(ctx: RuleContext) -> None:
	data = ctx.data
	for e, s, d in itertools.product(data.employees, data.shift_types, data.working_days):
		ctx.prob += (
			pulp.lpSum(ctx.presence[(e, s, d, b)] for b in data.branches) <= 1,
			_cname("one_branch", e, s, d),
		)


def _gating_holders(data: DataPackage) -> dict[str, dict[str, list[str]]]:
	"""Discipline -> gating role -> the employees holding it.

	Only gating roles. `room_coverage` takes the *minimum* over a discipline's roles, so a
	non-gating role counted there would hold every room in the discipline shut whenever
	nobody is working it — which is exactly what a lead duty must not do.
	"""
	holders: dict[str, dict[str, list[str]]] = {}
	for e in data.employees:
		for r in data.gating_roles(e):
			holders.setdefault(data.role_discipline.get(r, ""), {}).setdefault(r, []).append(e)
	return holders


@builtin_rule(
	"Room coverage per discipline",
	"The rooms staffed in a discipline for a given shift, day and branch equal the room-slots "
	"contributed by the Scheduling Roles assigned in that discipline (each contributes its "
	"max-rooms figure), capped at the branch's configured room count. Only the roles marked "
	"<b>Required To Staff A Room</b> count: a room opens where every gating role in its "
	"discipline is staffed, and a role that gates nothing (a lead duty, a floater) neither "
	"opens rooms nor holds them shut. What a lead duty is worth is decided by <b>Objective: "
	"Collateral duties</b> instead.",
	standard=True,
	group=GROUP_ROOM_COVERAGE,
	topic=TOPIC_COVERAGE,
)
def room_coverage(ctx: RuleContext) -> None:
	"""Note that the branch room cap is modeled by the variable bound."""
	data = ctx.data
	k_r_es = _gating_holders(data)

	for k, s, d, b in ctx.active_rooms:
		# lpSum([])=0 (no need for a condition)
		r_es = k_r_es.get(k, {})
		for r, es in r_es.items():
			# active_rooms[k] is the minimum staffing of all the roles that work in k at that time
			ctx.prob += (
				pulp.lpSum(data.max_rpe.get((e, r), 1) * ctx.x[(e, r, s, d, b)] for e in es)
				>= ctx.active_rooms[(k, s, d, b)],
				_cname("room_coverage", k, s, d, b, r),
			)


@builtin_rule(
	"Room coverage per discipline (matched pairing)",
	"The pooled alternative to <b>Room coverage per discipline</b>: rooms are numbered "
	"1..the branch's configured count, and a specific room opens only where every gating "
	"role in its discipline has a specific holder matched into that exact room number — not "
	"merely enough headcount to cover it. This is what lets a staffed room, and the people "
	"in it, be priced and paired individually (<b>Objective: Room value</b>, <b>Employee "
	"value</b>, <b>Synergy value</b>); the pooled rule has no notion of which holder of one "
	"gating role is paired with which holder of another. Off by default: real per-room "
	"matching is a materially larger model than a headcount minimum, worth its cost only "
	"where a room-level value mechanism is actually in use.",
	standard=False,
	group=GROUP_ROOM_COVERAGE,
	topic=TOPIC_COVERAGE,
)
def room_coverage_matched_rooms(ctx: RuleContext) -> None:
	"""Per-room AND across gating roles, tied back to the pooled `active_rooms`.

	Rooms are bare ordinals 1..`data.rooms[(k, b)]` — no identity beyond the index, so the
	solver is free to relabel which physical room a matched pair lands at (see
	`synergy_value_objective`'s docstring for why that is not a restriction). For each room
	index `n`: `room_occ_unique` caps each gating role to at most one occupant at `n`;
	`room_occ_cap` caps how many rooms one holder occupies at their own `max_rpe`, gated on
	their being assigned to the role at all; `room_active_le`/`room_active_ge` linearize
	`room_active[n]` as the AND, across every gating role, of "somebody occupies `n` in that
	role" — the per-room counterpart to the pooled minimum `room_coverage` takes. Finally
	`active_rooms_eq_matched` ties the pooled `active_rooms[k,s,d,b]` to `Σ_n room_active`, so
	every existing rule that reads `active_rooms` (`room_utilization_objective`, both
	collateral value rules) keeps working unchanged under either coverage rule.
	"""
	data = ctx.data
	k_r_es = _gating_holders(data)

	for k, s, d, b in ctx.active_rooms:
		r_es = k_r_es.get(k, {})
		n_rooms = data.rooms.get((k, b), 0)
		if not r_es or not n_rooms:
			continue

		occ_by_role_room: dict[tuple[str, int], object] = {}
		for r, es in r_es.items():
			for n in range(1, n_rooms + 1):
				occ_vars = []
				for e in es:
					y = ctx.prob.add_variable(_vname("room_occ", e, r, s, d, b, n), cat=pulp.LpBinary)
					ctx.room_occupancy[(e, r, s, d, b, n)] = y
					occ_vars.append(y)
				occ = pulp.lpSum(occ_vars)
				ctx.prob += (occ <= 1, _cname("room_occ_unique", k, r, s, d, b, n))
				occ_by_role_room[(r, n)] = occ
			for e in es:
				# capped by their own max-rooms figure, and only while actually assigned to
				# the role — an assigned-but-unmatched gating holder is legal, exactly as
				# under the pooled rule; it simply opens no room.
				room_vars = [ctx.room_occupancy[(e, r, s, d, b, n)] for n in range(1, n_rooms + 1)]
				ctx.prob += (
					pulp.lpSum(room_vars) <= data.max_rpe.get((e, r), 1) * ctx.x[(e, r, s, d, b)],
					_cname("room_occ_cap", e, r, s, d, b),
				)

		roles = sorted(r_es)
		for n in range(1, n_rooms + 1):
			active = ctx.prob.add_variable(_vname("room_active", k, s, d, b, n), cat=pulp.LpBinary)
			ctx.room_active[(k, s, d, b, n)] = active
			for r in roles:
				ctx.prob += (
					active <= occ_by_role_room[(r, n)],
					_cname("room_active_le", k, r, s, d, b, n),
				)
			ctx.prob += (
				active >= pulp.lpSum(occ_by_role_room[(r, n)] for r in roles) - (len(roles) - 1),
				_cname("room_active_ge", k, s, d, b, n),
			)

		ctx.prob += (
			ctx.active_rooms[(k, s, d, b)]
			== pulp.lpSum(ctx.room_active[(k, s, d, b, n)] for n in range(1, n_rooms + 1)),
			_cname("active_rooms_eq_matched", k, s, d, b),
		)


@builtin_rule(
	"FTE ceiling",
	"The half-days an employee is present over the horizon, whatever roles they work during "
	"them, stay at or below "
	"105% x their FTE-derived target; utilization pressure toward the target "
	"comes from the objective. A hard limit: a horizon that cannot be covered without "
	"somebody going over comes back infeasible. Where the agreed workload is a courtesy "
	"rather than a legal cap, pick <b>FTE soft ceiling</b> instead.",
	standard=True,
	group=GROUP_WORKLOAD_CEILING,
	topic=TOPIC_AVAILABILITY,
)
def fte_ceiling(ctx: RuleContext) -> None:
	data = ctx.data
	tol = 0.05
	for e in data.employees:
		target = data.target_shifts.get(e, 0)
		# presence, not assignments: a collateral duty worked alongside a shift is the same
		# half-day of somebody's life, and must not count against their agreed workload twice
		total_assigned = pulp.lpSum(
			ctx.presence[(e, s, d, b)]
			for s in data.shift_types
			for d in data.working_days
			for b in data.branches
		)
		if target > 0:
			# upper bound only: employee utilization will come from objective function
			ctx.prob += (
				total_assigned <= (1 + tol) * target,
				_cname("fte_max", e),
			)


@builtin_rule(
	"Agreed role FTE ceiling",
	"The hard reading of an agreed role split: where an Employee Scheduling Role names an "
	"agreed FTE, that role's assigned shifts stay at or below 105% x the agreed figure. Off "
	"by default, because these splits are normally informal expectations rather than "
	"entitlements — the objective rule of the same name is the usual way to express them.",
	standard=False,
	topic=TOPIC_AVAILABILITY,
)
def role_fte_ceiling(ctx: RuleContext) -> None:
	data = ctx.data
	tol = 0.05
	for (e, r), target in data.role_target_shifts.items():
		if target <= 0:
			continue
		assigned = pulp.lpSum(
			ctx.x[(e, r, s, d, b)] for s in data.shift_types for d in data.working_days for b in data.branches
		)
		ctx.prob += (assigned <= (1 + tol) * target, _cname("role_fte_max", e, r))


@builtin_rule(
	"Exclusive roles admit no collateral duty",
	"A shift worked in a role whose Assignment Mode is <b>Exclusive</b> is worked in that role "
	"and nothing else: no lead duty or other collateral role may be scheduled alongside it. "
	"Inert until somebody holds both an exclusive role and a collateral one.",
	standard=True,
	topic=TOPIC_AVAILABILITY,
)
def exclusive_role_purity(ctx: RuleContext) -> None:
	"""`Σ collateral ≤ |collateral| · (1 - Σ exclusive)` per employee, shift, day and branch.

	One constraint per slot rather than one per (exclusive, collateral) pair: the right-hand
	side turns off every collateral duty at once as soon as an exclusive role is worked, and
	leaves them all free otherwise. `Σ exclusive ≤ 1` already holds, since `model_builder`
	allows one working role per presence.
	"""
	data = ctx.data
	for e in data.employees:
		exclusive = data.exclusive_roles(e)
		collateral = data.collateral_roles(e)
		if not exclusive or not collateral:
			continue
		for s, d, b in itertools.product(data.shift_types, data.working_days, data.branches):
			ctx.prob += (
				pulp.lpSum(ctx.x[(e, r, s, d, b)] for r in collateral)
				<= len(collateral) * (1 - pulp.lpSum(ctx.x[(e, r, s, d, b)] for r in exclusive)),
				_cname("exclusive_purity", e, s, d, b),
			)


# ── Built-in objective rules (formerly hardcoded in model_builder.build) ──────


@builtin_rule(
	"Objective: Room utilization",
	"Maximize the total number of staffed rooms across all disciplines, shifts, days and branches.",
	kind=KIND_OBJECTIVE,
	standard=True,
	requires={room_coverage: "{r} requires {req}, otherwise the solver will just staff rooms for free."},
	# One objective point is loosely ~100 CHF/h, which puts a staffed room at 3. At weight 1
	# this rule cannot outbid the per-assignment cost the preference objective charges:
	# `room_coverage` takes the *minimum* over a discipline's roles, so opening one room
	# costs two or more assignments, and the schedule collapses to near-empty.
	default_weight=3.0,
	group=GROUP_ROOM_VALUE,
	topic=TOPIC_COVERAGE,
)
def room_utilization_objective(ctx: RuleContext) -> None:
	# One term per slot rather than one sum, so the run's breakdown can be opened down to
	# the rooms a single half-day at a single branch staffed. The model sees the same sum.
	for (k, s, d, b), rooms in ctx.active_rooms.items():
		ctx.add_objective(rooms, path(Discipline=k, Branch=b, Day=d, Shift=s))


@builtin_rule(
	"Objective: Room value",
	"The mutually exclusive alternative to <b>Room utilization</b>: instead of paying a flat "
	"3 points per staffed room, each room is worth its <b>Value Of A Staffed Room</b> "
	"(Discipline Branch Config), so different branches can price a staffed room differently. "
	"Requires <b>Room coverage per discipline (matched pairing)</b> — a room is only worth "
	"its value once a specific holder of every gating role is matched into it. A branch left "
	"at 0 (the default on an unconfigured row) contributes nothing.",
	kind=KIND_OBJECTIVE,
	standard=False,
	requires={
		room_coverage_matched_rooms: "{r} requires {req}: a room's value is only earned once "
		"it is genuinely matched, not merely headcounted."
	},
	group=GROUP_ROOM_VALUE,
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def room_value_objective(ctx: RuleContext) -> None:
	data = ctx.data
	for (k, s, d, b, n), active in ctx.room_active.items():
		value = data.room_value_of(k, b)
		if value:
			ctx.add_objective(value * active, path(Discipline=k, Branch=b, Day=d, Shift=s, Room=str(n)))


@builtin_rule(
	"Objective: Employee value",
	"Adds a flat bonus (or penalty) to a staffed room's value for the specific holder "
	"actually matched into it, scaled by their <b>Value Multiplier In A Staffed Room</b> "
	"(Employee Scheduling Role): 1 is no bonus, 1.25 a 25% premium, added as "
	"(-1 + multiplier) &times; the room's own value — flat and additive, never compounding "
	"the room's base value. A room worth 4 with a 1.25 multiplier scores 4 + 1 = 5. Inert "
	"for any pair left at the default multiplier of 1.",
	kind=KIND_OBJECTIVE,
	standard=False,
	requires={
		room_value_objective: "{r} scales its bonus off the same room value {req} prices, include {req}"
	},
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def employee_value_objective(ctx: RuleContext) -> None:
	"""AND(y, room_active), linearized as a continuous squeeze — no new binary needed.

	`z`'s only appearance is this one term, so the (signed) objective coefficient alone
	decides which bound the maximization drives it to: a positive bonus pushes `z` up to
	`min(y, room_active)`, a penalty pushes it down to `max(0, y + room_active - 1)` — both
	equal the AND of two binaries exactly, the same squeeze `collateral_room_value_objective`
	already relies on for a `min(...)`.
	"""
	data = ctx.data
	for (e, r, s, d, b, n), y in ctx.room_occupancy.items():
		multiplier = data.value_multiplier(e, r)
		if multiplier == 1.0:
			continue
		k = data.role_discipline.get(r, "")
		base = data.room_value_of(k, b)
		active = ctx.room_active.get((k, s, d, b, n))
		if not base or active is None:
			continue
		z = ctx.prob.add_variable(_vname("emp_value_and", e, r, s, d, b, n), lowBound=0)
		ctx.prob += (z <= y, _cname("emp_value_le_y", e, r, s, d, b, n))
		ctx.prob += (z <= active, _cname("emp_value_le_active", e, r, s, d, b, n))
		ctx.prob += (z >= y + active - 1, _cname("emp_value_ge", e, r, s, d, b, n))
		coefficient = (-1 + multiplier) * base
		ctx.add_objective(coefficient * z, path(Discipline=k, Branch=b, Day=d, Shift=s, Employee=e))


@builtin_rule(
	"Objective: Synergy value",
	"Adds a flat bonus (or penalty) when two specific employees are matched into the same "
	"room together, scaled by their <b>Synergy Multiplier</b> (Employee Role Synergy): 1 is "
	"no bonus, added as (-1 + multiplier) &times; the room's own value, on top of that room's "
	"base value and any employee-value bonus already earned there — never compounded with "
	"either. Symmetric: which employee is A and which is B does not matter. Inert while no "
	"Employee Role Synergy is configured.",
	kind=KIND_OBJECTIVE,
	standard=False,
	requires={
		room_value_objective: "{r} scales its bonus off the same room value {req} prices, include {req}"
	},
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def synergy_value_objective(ctx: RuleContext) -> None:
	"""AND(occupies-as-A, occupies-as-B, room_active), the 3-way analogue of the employee bonus.

	Gated on a shared *room*, not merely a shared session: room indices carry no meaning
	anywhere else in the model, so the solver is free to relabel which index a matched pair
	lands at — rewarding "share a room" and "share a session, wherever indices land" produce
	identical optimal schedules, and the room-scoped form is the cheaper one to linearize.

	`occ_e[(employee, discipline, ...)]` sums an employee's gating-role occupancy variables
	for one room; it stays 0/1 in the ordinary case because `model_builder._link_presence`
	already limits an employee to one *working* role per presence, and a gating role that is
	also Collateral (the one way to hold two at once) is an unusual configuration
	`Scheduling Role` itself warns against.
	"""
	data = ctx.data
	if not data.employee_synergy:
		return

	occ_e: dict[tuple, list[pulp.LpVariable]] = {}
	for (e, r, s, d, b, n), y in ctx.room_occupancy.items():
		k = data.role_discipline.get(r, "")
		occ_e.setdefault((e, k, s, d, b, n), []).append(y)

	by_room: dict[tuple, set[str]] = {}
	for e, k, s, d, b, n in occ_e:
		by_room.setdefault((k, s, d, b, n), set()).add(e)

	for (ea, eb), multiplier in data.employee_synergy.items():
		if multiplier == 1.0:
			continue
		for (k, s, d, b, n), here in by_room.items():
			if ea not in here or eb not in here:
				continue
			active = ctx.room_active.get((k, s, d, b, n))
			base = data.room_value_of(k, b)
			if not base or active is None:
				continue
			occ_a = pulp.lpSum(occ_e[(ea, k, s, d, b, n)])
			occ_b = pulp.lpSum(occ_e[(eb, k, s, d, b, n)])
			z = ctx.prob.add_variable(_vname("synergy_and", ea, eb, s, d, b, n), lowBound=0)
			ctx.prob += (z <= occ_a, _cname("synergy_le_a", ea, eb, s, d, b, n))
			ctx.prob += (z <= occ_b, _cname("synergy_le_b", ea, eb, s, d, b, n))
			ctx.prob += (z <= active, _cname("synergy_le_active", ea, eb, s, d, b, n))
			ctx.prob += (
				z >= occ_a + occ_b + active - 2,
				_cname("synergy_ge", ea, eb, s, d, b, n),
			)
			coefficient = (-1 + multiplier) * base
			ctx.add_objective(
				coefficient * z,
				path(Discipline=k, Branch=b, Day=d, Shift=s, Employees=f"{ea}+{eb}"),
			)


@builtin_rule(
	"Objective: Spread room load",
	"Where somebody can staff several rooms at once, make each further room they take on cost "
	"more than the one before: the first room is free, their last allowed room costs the full "
	"weight, and the rooms in between are priced on a straight line. Two people at two rooms "
	"each then beat one at three and one at one, and three at two beat two at three with the "
	"third sent home. That last case is why this is on by default: under <b>Bind settled "
	"schedules</b> a settled half-day is kept only where it pays for itself, and without this "
	"rule a colleague who can absorb the rooms makes it pay nothing. <b>Note what this "
	"costs</b>: it also pulls in somebody who is <i>not</i> bound to relieve a colleague's "
	"load, whenever the relief is worth more than their shift preference charge, and at the "
	"default weight it only goes so far: four settled holders at six rooms still come back "
	"2+2+2+0. Raise the weight to spread harder, lower it to spread less. Inert for roles "
	"whose holders staff one room.",
	kind=KIND_OBJECTIVE,
	standard=True,
	requires={room_coverage: "{r} prices the rooms {req} staffs, include {req}"},
	# The last allowed room costs the weight. Spreading 3+3+0 to 2+2+2 saves 2*w on the two
	# third rooms and pays w/2 for a second room plus the ~0.5-1 presence charge the
	# preference objective bills — so w = 1 spreads a settled holder's half-day back in.
	# Against the 3 a staffed room pays, a room nobody else can take still opens: its
	# costliest tranche is w per gating role.
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def room_load_objective(ctx: RuleContext) -> None:
	"""A convex cost on the rooms each holder staffs, linearized by tranches.

	`room_coverage` credits a holder with their whole max-rooms figure the moment they are
	assigned, so it has no notion of how many rooms they actually take: 3+3+0 and 2+2+2 are
	the same six rooms to it, and the first is one presence cheaper. This rule splits a
	holder's capacity into one-room tranches — the first is `x` itself, rooms 2..m are
	continuous `t_j ∈ [0, 1]` with `Σ t_j ≤ (m - 1)·x` — and re-states coverage over the
	tranches, which binds tighter than `room_coverage`'s `m·x` whenever the tranches are
	not full.

	No binaries and no SOS2, because the cost is convex: tranche j costs `(j-1)/(m-1)`, so a
	cheaper tranche always fills first and nothing has to force the order. Given the
	assignments, what remains is one coverage row of ones over bounded tranches — totally
	unimodular — so the rooms each holder takes come back whole. Who takes the odd room in a
	tie is arbitrary.
	"""
	data = ctx.data
	k_r_es = _gating_holders(data)
	for k, s, d, b in ctx.active_rooms:
		for r, es in k_r_es.get(k, {}).items():
			load = []
			for e in es:
				x = ctx.x[(e, r, s, d, b)]
				load.append(x)
				m = data.max_rpe.get((e, r), 1)
				ctx.room_load[(e, r, s, d, b)] = []
				if m <= 1:
					continue
				tranches = [
					ctx.prob.add_variable(_vname("room_load", e, r, s, d, b, j), lowBound=0, upBound=1)
					for j in range(2, m + 1)
				]
				ctx.prob += (pulp.lpSum(tranches) <= (m - 1) * x, _cname("room_load", e, r, s, d, b))
				load.extend(tranches)
				ctx.room_load[(e, r, s, d, b)] = tranches
				# Filed per holder and half-day: the breakdown then reads as who was charged
				# for a further room, when — the question this rule's weight is tuned against.
				ctx.add_objective(
					-pulp.lpSum((j - 1) / (m - 1) * t for j, t in enumerate(tranches, start=2)),
					path(Employee=e, Day=d, Shift=s),
				)
			ctx.prob += (
				pulp.lpSum(load) >= ctx.active_rooms[(k, s, d, b)],
				_cname("room_load_cover", k, s, d, b, r),
			)


def _collateral_holders(data: DataPackage) -> dict[str, list[tuple[str, str]]]:
	"""Discipline -> the `(employee, collateral role)` pairs that can be worked in it."""
	holders: dict[str, list[tuple[str, str]]] = {}
	for e in data.employees:
		for r in data.collateral_roles(e):
			holders.setdefault(data.role_discipline.get(r, ""), []).append((e, r))
	return holders


@builtin_rule(
	"Objective: Collateral duties",
	"Value a collateral duty — a lead, say — by the rooms <b>actually staffed</b> where it is "
	"worked, never as a room of its own: it earns the rooms open in its discipline at that "
	"branch, shift and day, up to the max-rooms figure its holders carry. Rooms still open "
	"without it, and where they are covered anyway the duty earns less than it costs. "
	"<b>Note what this rewards</b>: a lead is worth more where more rooms are running, so the "
	"optimizer will gather people into the branches that have one. That is a real effect and "
	"sometimes the wanted one — pick <b>by configured rooms</b> instead where a supervised "
	"post is worth the same wherever it is staffed.",
	kind=KIND_OBJECTIVE,
	standard=False,
	group=GROUP_COLLATERAL_VALUE,
	requires={room_coverage: "{r} values the rooms {req} staffs, include {req}"},
	# Loosely ~1 objective point per overseen room, against the 3 a staffed room itself pays
	# under `room_utilization_objective`: worth doing where the rooms are open anyway,
	# never worth opening a room for.
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def collateral_room_value_objective(ctx: RuleContext) -> None:
	"""`min(active_rooms, Σ max_rooms · c)`, linearized.

	`min` of two expressions is not linear, but the objective only ever pushes this value
	*up*, so a free variable under both ceilings is squeezed to exactly their minimum. Two
	inequalities and no binaries: the duty is worth the rooms it oversees, and the rooms are
	worth nothing extra without somebody overseeing them.
	"""
	data = ctx.data
	holders = _collateral_holders(data)

	for (k, s, d, b), rooms in ctx.active_rooms.items():
		pairs = holders.get(k)
		if not pairs:
			continue
		value = ctx.prob.add_variable(_vname("collateral_value", k, s, d, b), lowBound=0)
		ctx.prob += (value <= rooms, _cname("collateral_rooms", k, s, d, b))
		ctx.prob += (
			value <= pulp.lpSum(data.max_rpe.get((e, r), 1) * ctx.x[(e, r, s, d, b)] for e, r in pairs),
			_cname("collateral_span", k, s, d, b),
		)
		ctx.add_objective(value, path(Discipline=k, Branch=b, Day=d, Shift=s))


@builtin_rule(
	"Objective: FTE soft ceiling",
	"Penalize each half-day an employee is present beyond their FTE-derived target, instead of "
	"forbidding it. Statutory limits on working time are usually written against a full-time "
	"week, so a part-timer's agreed percentage is a courtesy to keep rather than a cap the "
	"law sets — and a schedule that would otherwise be infeasible is better than no schedule. "
	"At the default weight, exceeding somebody's agreed workload costs more than the staffed "
	"room it would buy, so the optimizer only does it when nothing else covers the horizon; "
	"lower the weight to let coverage outbid the courtesy, raise it to approach a hard cap. "
	"Working <i>under</i> target is not penalized here — that pressure comes from room "
	"utilization and the agreed role FTE split.",
	kind=KIND_OBJECTIVE,
	standard=False,
	group=GROUP_WORKLOAD_CEILING,
	# Calibrated against `room_utilization_objective`: one more assignment can open at most
	# one more room, worth 3 there, minus the ~1 the preference objective charges per
	# assignment. A default above that makes the courtesy hold wherever the schedule has any
	# other way to cover the room, while still yielding to infeasibility.
	default_weight=4.0,
	topic=TOPIC_AVAILABILITY,
)
def fte_soft_ceiling(ctx: RuleContext) -> None:
	"""One-sided deviation, linearized.

	`max(0, assigned - target)` needs a single non-negative variable bounded below by
	`assigned - target`; the negative objective coefficient squeezes it down to exactly
	that maximum, so no equality (and no second slack, as in `role_fte_target_objective`)
	is required. Employees with no FTE-derived target are left alone, exactly as
	`fte_ceiling` leaves them.
	"""
	data = ctx.data
	for e in data.employees:
		target = data.target_shifts.get(e, 0)
		if target <= 0:
			continue
		assigned = pulp.lpSum(
			ctx.presence[(e, s, d, b)]
			for s in data.shift_types
			for d in data.working_days
			for b in data.branches
		)
		over = ctx.prob.add_variable(_vname("fte_over", e), lowBound=0)
		ctx.prob += (over >= assigned - target, _cname("fte_over", e))
		# The penalty is one number per person by construction — there is no finer level
		# to file it under, and that is the level the courtesy is owed at anyway.
		ctx.add_objective(-over, path(Employee=e))


@builtin_rule(
	"Objective: Agreed role FTE split",
	"Where an Employee Scheduling Role names an agreed FTE for the role, penalize the "
	"absolute deviation of that role's assigned shifts from the agreed figure. Roles with no "
	"agreed figure are unpenalized, and bounded only by the employee's overall FTE ceiling.",
	kind=KIND_OBJECTIVE,
	standard=True,
	topic=TOPIC_AVAILABILITY,
)
def role_fte_target_objective(ctx: RuleContext) -> None:
	"""Absolute deviation, linearized.

	`|assigned - target|` is not linear, so split it into non-negative over/under slacks
	tied by `assigned - target == over - under` and penalize their sum. Both carry a
	negative coefficient in a maximization, so the solver squeezes them to the smallest
	pair the equality permits — exactly one of them ends up non-zero, and it equals the
	absolute deviation. No binaries and no big-M needed.
	"""
	data = ctx.data
	for (e, r), target in data.role_target_shifts.items():
		assigned = pulp.lpSum(
			ctx.x[(e, r, s, d, b)] for s in data.shift_types for d in data.working_days for b in data.branches
		)
		over = ctx.prob.add_variable(_vname("role_dev_over", e, r), lowBound=0)
		under = ctx.prob.add_variable(_vname("role_dev_under", e, r), lowBound=0)
		ctx.prob += (assigned - target == over - under, _cname("role_dev", e, r))
		ctx.add_objective(-(over + under), path(Employee=e, Role=r))


@builtin_rule(
	"Objective: Shift preferences",
	"Reward each half-day an employee is present according to their normalized shift "
	"preferences (from Employee Settings); a less-preferred shift scores lower. Charged per "
	"presence, so a collateral duty worked alongside a shift is not billed twice. Ignores role "
	"substitution suitability: every role an employee holds counts as fully suitable.",
	kind=KIND_OBJECTIVE,
	standard=False,
	group=GROUP_SHIFT_PREFERENCE,
	topic=TOPIC_PREFERENCES,
)
def shift_preference_objective(ctx: RuleContext) -> None:
	data = ctx.data
	for (e, s, d, _b), var in ctx.presence.items():
		ctx.add_objective(
			(-1 + data.shift_preferences.get(e, {}).get(s, 0.0)) * var,
			path(Employee=e, Day=d, Shift=s),
		)


@builtin_rule(
	"Objective: Shift preferences and role suitability",
	"Reward each half-day an employee is present according to their normalized shift "
	"preferences (from Employee Settings), and make working it in a role the employee only "
	"substitutes in cost more. The surcharge is scaled by the Suitability on its Employee "
	"Scheduling Role "
	"(maintained in the Role Matrix): 1 is a regular holder and costs what 'Shift preferences' "
	"charges, 1.2 a good backup, 3 a terrible but feasible one. With every suitability at 1 "
	"this is exactly 'Shift preferences'.",
	kind=KIND_OBJECTIVE,
	standard=True,
	group=GROUP_SHIFT_PREFERENCE,
	topic=TOPIC_PREFERENCES,
)
def suitability_preference_objective(ctx: RuleContext) -> None:
	"""`(-1 + pref)` per presence, plus `(-1 + pref) * (suitability - 1)` per assignment.

	The per-presence term is a (negative) net value, so "divide the desirability by the
	suitability" has to scale the cost *up*: dividing a negative value would make a poor
	substitute cheaper than the holder. At the default weights a staffed room pays 3, so
	a suitability-3 backup (about -1.5) still opens a room nobody else can staff.

	Split in two because the cost of being in on a Tuesday morning is charged once, on the
	presence, while the surcharge for spending it in a role somebody merely substitutes in
	belongs to the assignment. Their sum is `(-1 + pref) * suitability` whenever the presence
	is spent on exactly one role, which is every case without collateral duties — so with
	every suitability at 1 this is still exactly `shift_preference_objective`.
	"""
	data = ctx.data
	pref = data.shift_preferences
	# Both halves are filed under the same (employee, day, shift) label: they are one
	# charge for working that half-day, and splitting them in the report would only
	# expose the linearization.
	for (e, s, d, _b), var in ctx.presence.items():
		ctx.add_objective((-1 + pref.get(e, {}).get(s, 0.0)) * var, path(Employee=e, Day=d, Shift=s))
	for (e, r, s, d, _b), var in ctx.x.items():
		ctx.add_objective((1 - data.suitability(e, r)) * var, path(Employee=e, Day=d, Shift=s))


@builtin_rule(
	"Objective: Collateral duties (by configured rooms)",
	"Value a collateral duty — a lead, say — by the rooms its discipline has <b>configured</b> "
	"at that branch, rather than by how many happen to be staffed that half-day. A supervised "
	"post is then worth the same wherever it is worked, so pricing the duty does not quietly "
	"become a reason to gather people into the branches that have somebody supervising. Still "
	"capped by the branch's room count and by the max-rooms figure the duty's holders carry, "
	"so a second lead at a two-room branch earns nothing further. On by default; pick "
	"<b>Objective: Collateral duties</b> instead where supervising a busy half-day really is "
	"worth more than supervising a quiet one.",
	kind=KIND_OBJECTIVE,
	standard=True,
	group=GROUP_COLLATERAL_VALUE,
	# Same scale as the rule it replaces: loosely one point per overseen room, against the 3
	# a staffed room itself pays under `room_utilization_objective`.
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def collateral_capacity_value_objective(ctx: RuleContext) -> None:
	"""`min(configured rooms, Σ max_rooms · c)`, linearized.

	The capacity half is a constant, so it lives in the variable's own upper bound rather
	than in a constraint — the same place `active_rooms` keeps its room cap. What is left is
	one inequality per slot, and the objective pushing the value up squeezes it to exactly
	the minimum.

	Reads no `active_rooms` at all, which is the whole point: the value of the post does not
	move with how well the practice managed to staff that half-day, so it cannot be earned by
	concentrating people around it.
	"""
	data = ctx.data
	holders = _collateral_holders(data)

	for k, s, d, b in itertools.product(data.disciplines, data.shift_types, data.working_days, data.branches):
		pairs = holders.get(k)
		capacity = data.rooms.get((k, b), 0)
		if not pairs or not capacity:
			continue
		value = ctx.prob.add_variable(
			_vname("collateral_capacity_value", k, s, d, b), lowBound=0, upBound=capacity
		)
		ctx.prob += (
			value <= pulp.lpSum(data.max_rpe.get((e, r), 1) * ctx.x[(e, r, s, d, b)] for e, r in pairs),
			_cname("collateral_capacity", k, s, d, b),
		)
		ctx.add_objective(value, path(Discipline=k, Branch=b, Day=d, Shift=s))


@builtin_rule(
	"Objective: Value of working a role",
	"Add each role's own <b>Value Of A Shift In This Role</b> to the objective, once per "
	"assignment. It is what answers the question room coverage cannot: whether somebody the "
	"schedule has no room for is better on a standby or float role than left at home. A role "
	"at the default of 0 contributes nothing, so this rule is inert until somebody prices a "
	"role — and a negative figure makes a role a last resort rather than a filler. Priced in "
	"the same points as everything else: a staffed room pays 3 and an assignment costs about "
	"1, so a standby role has to be worth more than about 1 before the optimizer will place "
	"anybody on it.",
	kind=KIND_OBJECTIVE,
	standard=True,
	# The figure on the role is already in objective points — that is the whole contract of
	# the field — so the ruleset weight is a scale factor on a decision the site has already
	# made, and starts at 1.
	default_weight=1.0,
	topic=TOPIC_COVERAGE,
)
def role_value_objective(ctx: RuleContext) -> None:
	"""`value(role)` per assignment.

	Per assignment rather than per presence, because the question is what *this role* is
	worth rather than what being in is worth: somebody working a gating role with a priced
	collateral duty beside it earns both, which is the point of pricing the duty.
	"""
	data = ctx.data
	for (e, r, s, d, _b), var in ctx.x.items():
		value = data.value_of(r)
		if value:
			ctx.add_objective(value * var, path(Role=r, Employee=e, Day=d, Shift=s))


@builtin_rule(
	"Objective: Conserve Existing Assignments",
	"Promote tie-breaking towards existing assignments with small reward. Mutually exclusive "
	"with 'Honor existing Shift Assignments': that rule fixes them as hard constraints, which "
	"would make this soft reward a no-op. Neither rule present means existing assignments are "
	"disregarded entirely.",
	kind=KIND_OBJECTIVE,
	standard=False,
	group=GROUP_EXISTING_ASSIGNMENTS,
	topic=TOPIC_EXISTING_ASSIGNMENTS,
)
def weigh_assignments_objective(ctx: RuleContext) -> None:
	data = ctx.data
	epsilon = 2**-10
	for comb, var in ctx.x.items():
		if comb in data.forced:
			continue
		e, _r, s, d, _b = comb
		ctx.add_objective(-epsilon * var, path(Employee=e, Day=d, Shift=s))


def rules_in_group(group: str) -> set[str]:
	"""The built-in keys belonging to a choice group, derived from the registry.

	So callers that reason about a whole choice set — "is *some* binding rule selected?" —
	cannot drift from the rules' own declarations.
	"""
	return {key for key, rule in BUILTIN_RULES.items() if rule.group == group}


# ── Custom rules ──────────────────────────────────────────────────────────────


def compile_custom_rule(rule_name: str, code: str) -> Callable[[RuleContext], None]:
	"""
	Compile the Python source of a Custom Code rule and return its ``apply`` function.

	The source runs with ``pulp`` and ``itertools`` pre-imported and must define
	``apply(ctx)``; it may add constraints to ``ctx.prob`` and/or contribute
	objective terms via ``ctx.add_objective(expr)`` — optionally labelled with
	``path(...)``, also pre-imported, so the rule's share of the objective can be
	drilled into on the run's statistics panel. Only developer-validated code
	reaches this point (enforced by the data loader), so it executes with normal
	Python semantics — an Optimization Rule document is as trusted as app code.
	"""
	namespace: dict = {
		"pulp": pulp,
		"itertools": itertools,
		"cname": _cname,
		"vname": _vname,
		"path": path,
	}
	try:
		# source: ../autoshift/doctype/optimization_rule/optimization_rule.json
		# developer must ensure that no unauthorized user can add/edit/validate rules
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-codeinjection-eval
		exec(compile(code, f"<Optimization Rule: {rule_name}>", "exec"), namespace)
	except Exception as exc:
		raise ValueError(f"Optimization Rule {rule_name!r}: implementation failed to load: {exc}") from exc
	apply_fn = namespace.get("apply")
	if not callable(apply_fn):
		raise ValueError(f"Optimization Rule {rule_name!r}: implementation must define apply(ctx).")
	return apply_fn


def legacy_ruleset(data: DataPackage) -> tuple[DataPackage.RULE, ...]:
	"""The implicit selection an empty ``DataPackage.rules`` means: every standard built-in."""
	if DataPackage.WEIGH_ASSIGNMENTS in data.flags:
		return tuple(
			(rule.title, key, "", 1.0)
			for key, rule in BUILTIN_RULES.items()
			if (
				key in STANDARD_RULES
				and key
				not in [
					use_existing_assignments.__name__,
				]
			)
			or key
			in [
				weigh_assignments_objective.__name__,
			]
		)
	return tuple((rule.title, key, "", 1.0) for key, rule in BUILTIN_RULES.items() if key in STANDARD_RULES)


def order_specs(specs: tuple[DataPackage.RULE, ...]) -> tuple[DataPackage.RULE, ...]:
	"""
	Order rule specs so a rule runs after every built-in it ``requires``.

	``DataPackage.rules`` arrives sorted by *document name* (``data_loader._load_rules``
	sorts that way to keep ``DataPackage.input_hash`` stable), which has nothing to do
	with the dependency graph: ``warm_start``'s title sorts last among the standard
	rules, so every ``fixValue()`` rule that depends on it used to run first and do
	nothing at all — ``pulp.LpVariable.fixValue`` is a silent no-op while ``varValue``
	is ``None``. ``requires`` declared the dependency but nothing enforced it as an
	order.

	Stable topological sort: a spec is emitted once every built-in it requires (and
	that is actually part of this selection — ``check_ruleset`` has already rejected
	missing ones) has been emitted, preserving the incoming order as the tiebreak so
	the result stays deterministic. Custom Code rules declare no metadata, so they
	simply keep their position. A dependency cycle falls back to the incoming order
	rather than dropping rules or looping forever.
	"""
	selected = {key for _, key, _, _ in specs if key}
	emitted: set[str] = set()

	def is_ready(spec: DataPackage.RULE) -> bool:
		rule = BUILTIN_RULES.get(spec[1])
		if rule is None:  # Custom Code (or an unknown key apply_rules will reject)
			return True
		# only wait on requirements that are part of this selection; check_ruleset has
		# already rejected a selection missing one
		return not (rule.requires.keys() & selected) - emitted

	pending = list(specs)
	ordered: list[DataPackage.RULE] = []
	while pending:
		# partitioning `pending` in place keeps the incoming order as the tiebreak
		ready = [spec for spec in pending if is_ready(spec)]
		blocked = [spec for spec in pending if not is_ready(spec)]
		if not ready:  # cycle among `requires` — emit the rest as they came
			ordered.extend(pending)
			break
		ordered.extend(ready)
		emitted.update(spec[1] for spec in ready if spec[1])
		pending = blocked
	return tuple(ordered)


def apply_rules(ctx: RuleContext) -> str:
	"""
	Apply the rules selected in ``ctx.data.rules`` to the problem.

	Each spec's weight scales the objective terms the rule contributes (via
	``ctx.add_objective``); constraint rules are unaffected by it. An empty
	selection applies every built-in rule at weight 1.0 (pre-ruleset behaviour).

	Rules are applied in dependency order (see :func:`order_specs`), not in the
	order the specs arrive.
	"""
	specs = ctx.data.rules or legacy_ruleset(ctx.data)
	BuiltinRule.check_ruleset({key for _, key, _, _ in specs})
	specs = order_specs(specs)
	logs = io.StringIO()
	for name, builtin_key, code, weight in specs:
		ctx._current_weight = weight
		ctx._current_rule = name
		if builtin_key in BUILTIN_RULES and BUILTIN_RULES[builtin_key].kind == KIND_OBJECTIVE:
			# Every objective rule that ran gets a (possibly empty) entry, so a rule that
			# happens to contribute nothing this run — nobody staffs a second room, no role
			# is priced — is reported as the 0 it scored rather than vanishing from the
			# breakdown. Rules used to contribute a constant-0 term by hand to achieve this.
			ctx.objective_contributions.setdefault(name, {})
		try:
			if builtin_key:
				rule = BUILTIN_RULES.get(builtin_key)
				if rule is None:
					raise ValueError(
						f"Optimization Rule {name!r}: unknown built-in key {builtin_key!r}. "
						f"Registered keys: {sorted(BUILTIN_RULES)}"
					)
				with contextlib.redirect_stdout(logs):
					rule.apply(ctx)
			elif code:
				with contextlib.redirect_stdout(logs):
					compile_custom_rule(name, code)(ctx)
			else:
				raise ValueError(f"Optimization Rule {name!r} has no implementation and cannot be used.")
		finally:
			ctx._current_weight = 1.0
			ctx._current_rule = ""
	return logs.getvalue()


# ── Reporting the objective ───────────────────────────────────────────────────

# Schema version of the JSON a run persists in `objective_breakdown`. 1 was the flat
# `{rule: value}` map this tree replaced; `optimizer_run.get_run_statistics` still reads
# those, since a solved run is never re-solved.
BREAKDOWN_VERSION = 2

# A leaf whose solved value is smaller than this is dropped from the breakdown: a rule
# files a term per candidate slot, and the ones the solver left at 0 are the majority.
OBJECTIVE_EPSILON = 1e-9
# Children shown under one node before the tail is folded into a single "… and N more"
# row. Breadth past this is a list, not a breakdown.
MAX_CHILDREN = 40
# Nodes one rule's subtree may hold. A rule decomposed to (employee, day, shift) over a
# four-week horizon is thousands of leaves, which is a payload nobody reads; when a rule
# overruns, its *deepest* level is dropped and the leaves re-folded, repeatedly, until it
# fits. Depth goes first because the coarse levels are the ones that answer "where did
# this rule's score come from" — and the dropped levels are named on the node, so the
# reader is told what they are not seeing.
MAX_NODES_PER_RULE = 2000


def objective_shares(ctx: RuleContext) -> dict[str, float]:
	"""Each rule's share of the solved objective, keyed by rule document name.

	Only meaningful after ``prob.solve()`` — an unsolved variable evaluates to ``None``,
	which is reported here as 0.0.
	"""
	return {
		rule: sum(pulp.value(pulp.lpSum(terms)) or 0.0 for terms in by_path.values())
		for rule, by_path in ctx.objective_contributions.items()
	}


def objective_tree(
	ctx: RuleContext,
	*,
	max_children: int = MAX_CHILDREN,
	max_nodes: int = MAX_NODES_PER_RULE,
) -> list[dict]:
	"""The solved objective as a drill-down tree, one subtree per contributing rule.

	Each rule node is ``{"rule", "value", "levels", "trimmed", "children"}`` and each
	inner node ``{"label", "value", "children"}``; ``children`` is absent on a leaf, and a
	node carries no level of its own — a node's level is ``levels[depth - 1]`` of the rule
	it sits under, and repeating it on every one of a few thousand nodes only inflates
	what the run stores. Siblings are ordered by the size of their contribution, whatever
	its sign — the question a breakdown answers is "what moved the objective", and a cost
	that moved it by -40 outranks a reward that moved it by 2. ``levels`` names the path
	levels the subtree actually has and ``trimmed`` the deeper ones dropped to fit
	``max_nodes``.

	Rule values are the *unpruned* totals, so they still add up to the solved objective
	even where a subtree drops near-zero leaves.
	"""
	nodes = [
		_rule_node(rule, by_path, max_children, max_nodes)
		for rule, by_path in ctx.objective_contributions.items()
	]
	nodes.sort(key=lambda n: (-abs(n["value"]), n["rule"]))
	return nodes


def _rule_node(rule: str, by_path: dict[PATH, list], max_children: int, max_nodes: int) -> dict:
	leaves = {p: (pulp.value(pulp.lpSum(terms)) or 0.0) for p, terms in by_path.items()}
	node: dict = {"rule": rule, "value": round(sum(leaves.values()), 4)}

	kept = {p: v for p, v in leaves.items() if abs(v) > OBJECTIVE_EPSILON}
	# level names by depth, taken from the paths themselves: a rule files one shape of
	# path, so the first one seen at a given depth names that level for all of them.
	levels: list[str] = []
	for p in kept:
		for depth, (level, _label) in enumerate(p):
			if depth == len(levels):
				levels.append(level)
	depth = len(levels)
	while depth and _prefix_count(kept, depth) > max_nodes:
		depth -= 1
	if depth < len(levels):
		node["trimmed"] = levels[depth:]
		folded: dict[PATH, float] = {}
		for p, v in kept.items():
			folded[p[:depth]] = folded.get(p[:depth], 0.0) + v
		kept = folded
	node["levels"] = levels[:depth]

	children = _breakdown_children(kept, max_children)
	if children:
		node["children"] = children
	return node


def _prefix_count(leaves: dict[PATH, float], depth: int) -> int:
	"""How many nodes a tree over these leaves holds if cut off at ``depth``."""
	return len({p[:i] for p in leaves for i in range(1, min(len(p), depth) + 1)})


def _breakdown_children(leaves: dict[PATH, float], max_children: int) -> list[dict]:
	groups: dict[tuple[str, str], dict[PATH, float]] = {}
	own = 0.0
	for p, value in leaves.items():
		if not p:
			own += value  # a term this rule filed shallower than its siblings
			continue
		groups.setdefault(p[0], {})[p[1:]] = value

	children = []
	for (_level, label), sub in groups.items():
		child: dict = {"label": label, "value": round(sum(sub.values()), 4)}
		grandchildren = _breakdown_children(sub, max_children)
		if grandchildren:
			child["children"] = grandchildren
		children.append(child)
	children.sort(key=lambda c: (-abs(c["value"]), c["label"]))

	if len(children) > max_children:
		rest = children[max_children:]
		children = children[:max_children]
		children.append(
			{
				"label": f"… and {len(rest)} more",
				"value": round(sum(c["value"] for c in rest), 4),
				"rest": len(rest),
			}
		)
	if children and abs(own) > OBJECTIVE_EPSILON:
		children.append({"label": "(unbroken)", "value": round(own, 4)})
	return children
