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

	# objective terms accumulated by the applied rules; model_builder sums these
	# into the problem's (maximized) objective after all rules have run
	objective_terms: list = field(default_factory=list)
	# the same terms keyed by the contributing rule's document name, so a solved
	# problem can report each rule's share of the objective (see solver.run_solve)
	objective_contributions: dict[str, list] = field(default_factory=dict)
	# ruleset row weight / document name of the rule currently being applied
	# (both set by apply_rules)
	_current_weight: float = 1.0
	_current_rule: str = ""

	def add_objective(self, term) -> None:
		"""Contribute a term to the maximized objective, scaled by the rule's ruleset weight."""
		weighted = self._current_weight * term
		self.objective_terms.append(weighted)
		self.objective_contributions.setdefault(self._current_rule or "(unattributed)", []).append(weighted)


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
	topic=TOPIC_COVERAGE,
)
def room_coverage(ctx: RuleContext) -> None:
	"""Note that the branch room cap is modeled by the variable bound."""
	data = ctx.data
	ROLE = str
	EMPLOYEE = str
	DISCIPLINE = str
	k_r_es: dict[DISCIPLINE, dict[ROLE, list[EMPLOYEE]]] = {}
	for e in data.employees:
		# Only gating roles. The constraint takes the *minimum* over a discipline's roles, so
		# a non-gating role counted here would hold every room in the discipline shut whenever
		# nobody is working it — which is exactly what a lead duty must not do.
		for r in data.gating_roles(e):
			k = data.role_discipline.get(r, "")
			k_r_es.setdefault(k, {}).setdefault(r, []).append(e)

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
	topic=TOPIC_COVERAGE,
)
def room_utilization_objective(ctx: RuleContext) -> None:
	ctx.add_objective(pulp.lpSum(ctx.active_rooms.values()))


@builtin_rule(
	"Objective: Collateral duties",
	"Value a collateral duty — a lead, say — by the rooms it oversees, never as a room of its "
	"own. A collateral role scheduled in a discipline at some branch, shift and day earns the "
	"rooms actually staffed there, up to the max-rooms figure its holders carry. Rooms still "
	"open without it: nothing here makes a lead a condition of staffing a room, and where the "
	"rooms are already covered without one the duty simply earns less than it costs.",
	kind=KIND_OBJECTIVE,
	standard=True,
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
	holders: dict[str, list[tuple[str, str]]] = {}
	for e in data.employees:
		for r in data.collateral_roles(e):
			holders.setdefault(data.role_discipline.get(r, ""), []).append((e, r))

	values = []
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
		values.append(value)

	ctx.add_objective(pulp.lpSum(values))


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
	penalties = []
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
		penalties.append(over)

	ctx.add_objective(-pulp.lpSum(penalties))


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
	penalties = []
	for (e, r), target in data.role_target_shifts.items():
		assigned = pulp.lpSum(
			ctx.x[(e, r, s, d, b)] for s in data.shift_types for d in data.working_days for b in data.branches
		)
		over = ctx.prob.add_variable(_vname("role_dev_over", e, r), lowBound=0)
		under = ctx.prob.add_variable(_vname("role_dev_under", e, r), lowBound=0)
		ctx.prob += (assigned - target == over - under, _cname("role_dev", e, r))
		penalties.append(over + under)

	ctx.add_objective(-pulp.lpSum(penalties))


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
	ctx.add_objective(
		pulp.lpSum(
			(-1 + data.shift_preferences.get(e, {}).get(s, 0.0)) * var
			for (e, s, _d, _b), var in ctx.presence.items()
		)
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
	ctx.add_objective(
		pulp.lpSum((-1 + pref.get(e, {}).get(s, 0.0)) * var for (e, s, _d, _b), var in ctx.presence.items())
		+ pulp.lpSum(
			(-1 + pref.get(e, {}).get(s, 0.0)) * (data.suitability(e, r) - 1) * var
			for (e, r, s, _d, _b), var in ctx.x.items()
		)
	)


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
	# Always contributes, even when every role is at 0 — an objective rule that sometimes
	# records no term at all would leave a hole in the run's per-rule objective breakdown,
	# whose shares are asserted to add back up to the objective.
	ctx.add_objective(
		pulp.lpSum(data.value_of(r) * var for (_e, r, _s, _d, _b), var in ctx.x.items() if data.value_of(r))
	)


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
	ctx.add_objective(
		pulp.lpSum((0 if comb in data.forced else -epsilon) * var for comb, var in ctx.x.items())
	)


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
	objective terms via ``ctx.add_objective(expr)``. Only developer-validated code
	reaches this point (enforced by the data loader), so it executes with normal
	Python semantics — an Optimization Rule document is as trusted as app code.
	"""
	namespace: dict = {"pulp": pulp, "itertools": itertools, "cname": _cname, "vname": _vname}
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
