"""
Optimization rule registry: the MILP constraint groups, as selectable named rules.

Pure Python (no Frappe imports) — safe to use in tests.

Each built-in rule is a function taking a :class:`RuleContext` and adding
constraints (or fixing variables) on the PuLP problem. Rules are registered in
``BUILTIN_RULES`` under a stable key; an ``Optimization Rule`` document with
``implementation_type = "Built-in"`` points at one of these keys.

Custom rules live as Python source on an ``Optimization Rule`` document
(``implementation_type = "Custom Code"``). The source must define a function
``apply(ctx)`` and only runs once a developer has checked ``validated`` on the
document. It is compiled here at solve time via :func:`compile_custom_rule`.

Which rules apply to a given solve comes from ``DataPackage.rules`` — the
``(rule_document_name, builtin_key, custom_code)`` triples loaded from the
run's ``Optimization Ruleset``. An empty ``rules`` tuple means "apply every
built-in rule" (the pre-ruleset behaviour, still used by the unit tests).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pulp

if TYPE_CHECKING:
	from .types import DataPackage


@dataclass
class RuleContext:
	"""Everything a rule may constrain: the problem, its variables, and the input data."""

	prob: pulp.LpProblem
	x: dict[tuple, pulp.LpVariable]  # x[employee, shift, day, branch] binary
	active_rooms: dict[tuple, pulp.LpVariable]  # ar[discipline, shift, day, branch] integer
	data: DataPackage


@dataclass(frozen=True)
class BuiltinRule:
	key: str
	title: str
	description: str
	apply: Callable[[RuleContext], None]


BUILTIN_RULES: dict[str, BuiltinRule] = {}


def builtin_rule(key: str, title: str, description: str):
	"""Register a function as a built-in optimization rule."""

	def register(fn: Callable[[RuleContext], None]):
		BUILTIN_RULES[key] = BuiltinRule(key=key, title=title, description=description, apply=fn)
		return fn

	return register


def _cname(*parts) -> str:
	"""Sanitize parts into a PuLP-safe constraint name."""
	return "_".join(str(p) for p in parts).replace("-", "_").replace(" ", "_")


# ── Built-in rules (formerly hardcoded in model_builder.build) ────────────────


@builtin_rule(
	"one_shift_per_day",
	"One shift per employee per day",
	"An employee works at most one shift per day, across all shift types and branches.",
)
def one_shift_per_day(ctx: RuleContext) -> None:
	data = ctx.data
	for e, d in itertools.product(data.employees, data.working_days):
		ctx.prob += (
			pulp.lpSum(ctx.x[(e, s, d, b)] for s in data.shift_types for b in data.branches) <= 1,
			_cname("one_shift", e, d),
		)


@builtin_rule(
	"leave_blocklist",
	"Respect approved leaves",
	"An employee on approved leave (or a leave this run speculates as approved) is never "
	"assigned a shift on the leave days.",
)
def leave_blocklist(ctx: RuleContext) -> None:
	data = ctx.data
	day_set = set(data.working_days)
	for e, d in data.leave_blocked:
		if e not in data.employees or d not in day_set:
			continue
		for s in data.shift_types:
			for b in data.branches:
				ctx.x[(e, s, d, b)].setInitialValue(0)
				ctx.x[(e, s, d, b)].fixValue()


@builtin_rule(
	"existing_assignments",
	"Honor existing Shift Assignments",
	"Shift Assignments already on the books are honored per the run's 'Existing Shift "
	"Assignments' mode: fixed as hard constraints in 'Use' mode, used as a soft warm-start "
	"the solver may override in 'Weigh' mode (in 'Ignore' mode the set is empty).",
)
def existing_assignments(ctx: RuleContext) -> None:
	data = ctx.data
	all_combs = itertools.product(data.employees, data.shift_types, data.working_days, data.branches)
	for comb in all_combs:
		ctx.x[comb].setInitialValue(1 if comb in data.forced else 0)
	if data.WEIGH_ASSIGNMENTS not in data.flags:
		# this is the 'Use'/'Ignore' mode
		for comb in data.forced:
			if comb in ctx.x:
				ctx.x[comb].fixValue()


@builtin_rule(
	"max_rooms_per_slot",
	"Max rooms per employee per slot",
	"In any single slot (shift + day), an employee covers at most their configured maximum "
	"number of rooms (from Discipline Designation Branch Config), which also limits working "
	"multiple branches in the same slot.",
)
def max_rooms_per_slot(ctx: RuleContext) -> None:
	data = ctx.data
	for e, s, d in itertools.product(data.employees, data.shift_types, data.working_days):
		limit = data.max_rpe.get(e, 1)
		ctx.prob += (
			pulp.lpSum(ctx.x[(e, s, d, b)] for b in data.branches) <= limit,
			_cname("max_rpe", e, s, d),
		)


@builtin_rule(
	"room_coverage",
	"Room coverage per discipline",
	"The rooms staffed in a discipline for a given shift, day and branch equal the room-slots "
	"contributed by the discipline's assigned employees (each contributes their max-rooms "
	"figure), capped at the branch's configured room count.",
)
def room_coverage(ctx: RuleContext) -> None:
	data = ctx.data
	for k, s, d, b in itertools.product(data.disciplines, data.shift_types, data.working_days, data.branches):
		employees_in_discipline = [e for e in data.employees if data.department.get(e) == k]
		if not employees_in_discipline:
			ctx.prob += (
				ctx.active_rooms[(k, s, d, b)] == 0,
				_cname("no_employees", k, s, d, b),
			)
		else:
			ctx.prob += (
				pulp.lpSum(data.max_rpe.get(e, 1) * ctx.x[(e, s, d, b)] for e in employees_in_discipline)
				== ctx.active_rooms[(k, s, d, b)],
				_cname("room_coverage", k, s, d, b),
			)


@builtin_rule(
	"fte_ceiling",
	"FTE ceiling",
	"An employee's total assigned shifts over the horizon stay at or below "
	"(1 + tolerance) x their FTE-derived target; utilization pressure toward the target "
	"comes from the objective, not a lower bound.",
)
def fte_ceiling(ctx: RuleContext) -> None:
	data = ctx.data
	tol = data.fte_tolerance
	for e in data.employees:
		target = data.target_shifts.get(e, 0)
		total_assigned = pulp.lpSum(
			ctx.x[(e, s, d, b)] for s in data.shift_types for d in data.working_days for b in data.branches
		)
		if target > 0:
			# upper bound only: employee utilization will come from objective function
			ctx.prob += (
				total_assigned <= (1 + tol) * target,
				_cname("fte_max", e),
			)


# ── Custom rules ──────────────────────────────────────────────────────────────


def compile_custom_rule(rule_name: str, code: str) -> Callable[[RuleContext], None]:
	"""
	Compile the Python source of a Custom Code rule and return its ``apply`` function.

	The source runs with ``pulp`` and ``itertools`` pre-imported and must define
	``apply(ctx)``. Only developer-validated code reaches this point (enforced by
	the data loader), so it executes with normal Python semantics — an Optimization
	Rule document is as trusted as app code.
	"""
	namespace: dict = {"pulp": pulp, "itertools": itertools}
	try:
		exec(compile(code, f"<Optimization Rule: {rule_name}>", "exec"), namespace)
	except Exception as exc:
		raise ValueError(f"Optimization Rule {rule_name!r}: implementation failed to load: {exc}") from exc
	apply_fn = namespace.get("apply")
	if not callable(apply_fn):
		raise ValueError(f"Optimization Rule {rule_name!r}: implementation must define apply(ctx).")
	return apply_fn


def apply_rules(ctx: RuleContext) -> None:
	"""
	Apply the rules selected in ``ctx.data.rules`` to the problem.

	An empty selection applies every built-in rule (pre-ruleset behaviour).
	"""
	specs = ctx.data.rules or tuple((rule.title, key, "") for key, rule in BUILTIN_RULES.items())
	for name, builtin_key, code in specs:
		if builtin_key:
			rule = BUILTIN_RULES.get(builtin_key)
			if rule is None:
				raise ValueError(
					f"Optimization Rule {name!r}: unknown built-in key {builtin_key!r}. "
					f"Registered keys: {sorted(BUILTIN_RULES)}"
				)
			rule.apply(ctx)
		elif code:
			compile_custom_rule(name, code)(ctx)
		else:
			raise ValueError(f"Optimization Rule {name!r} has no implementation and cannot be used.")
