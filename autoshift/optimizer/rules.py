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

from .types import DataPackage

KIND_CONSTRAINT = "Constraint"
KIND_OBJECTIVE = "Objective"
KIND_MIXED = "Mixed"
KIND_OTHER = "Other"


@dataclass
class RuleContext:
	"""Everything a rule may act on: the problem, its variables, and the input data."""

	prob: pulp.LpProblem
	x: dict[tuple, pulp.LpVariable]  # x[employee, shift, day, branch] binary
	active_rooms: dict[tuple, pulp.LpVariable]  # ar[discipline, shift, day, branch] integer
	data: DataPackage

	# objective terms accumulated by the applied rules; model_builder sums these
	# into the problem's (maximized) objective after all rules have run
	objective_terms: list = field(default_factory=list)
	# ruleset row weight of the rule currently being applied (set by apply_rules)
	_current_weight: float = 1.0

	def add_objective(self, term) -> None:
		"""Contribute a term to the maximized objective, scaled by the rule's ruleset weight."""
		self.objective_terms.append(self._current_weight * term)


@dataclass(frozen=True)
class BuiltinRule:
	key: str
	title: str
	description: str
	apply: Callable[[RuleContext], None]
	excludes: dict[str, str]
	requires: dict[str, str]
	kind: str = KIND_CONSTRAINT

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


BUILTIN_RULES: dict[str, BuiltinRule] = {}  # empty -> filled by the builtin_rule decorator
STANDARD_RULES: set[str] = set()  # idem


def builtin_rule(
	title: str,
	description: str,
	kind: str = KIND_CONSTRAINT,
	standard: bool = False,
	requires: dict[FunctionType, str] | None = None,
	excludes: dict[FunctionType, str] | None = None,
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


# ── Built-in rules (formerly hardcoded in model_builder.build) ────────────────


@builtin_rule(
	"One shift per employee per day",
	"An employee works at most one shift per day, across all shift types and branches.",
	standard=True,
)
def one_shift_per_day(ctx: RuleContext) -> None:
	data = ctx.data
	for e, d in itertools.product(data.employees, data.working_days):
		ctx.prob += (
			pulp.lpSum(ctx.x[(e, s, d, b)] for s in data.shift_types for b in data.branches) <= 1,
			_cname("one_shift", e, d),
		)


@builtin_rule(
	"Use existing Shift Assignments as a baseline",
	"This provides a soft tie-breaking towards existing assignments, "
	"and is used as a baseline for other rules.",
	standard=True,
	kind=KIND_OTHER,
)
def warm_start(ctx: RuleContext) -> None:
	data = ctx.data
	for (e, s, d, b), var in ctx.x.items():
		if (e, s, d, b) in data.forced and (e, s, d, b) in data.leave_blocked:
			raise ValueError(f"Employee {e} cant both be on leave and scheduled for a shift on {d}")
		var.setInitialValue(1 if (e, s, d, b) in data.forced else 0)


@builtin_rule(
	"Respect approved leaves",
	"An employee on approved leave (or a leave this run speculates as approved) is never "
	"assigned a shift on the leave days.",
	standard=True,
	requires={warm_start: "{r} doesn't set its values, include {req}"},
)
def leave_blocklist(ctx: RuleContext) -> None:
	data = ctx.data
	day_set = set(data.working_days)
	for e, d in data.leave_blocked:
		if e not in data.employees or d not in day_set:
			continue
		for s in data.shift_types:
			for b in data.branches:
				ctx.x[(e, s, d, b)].fixValue()


@builtin_rule(
	"Honor existing Shift Assignments",
	"Shift Assignments already on the books are honored.",
	standard=True,
	requires={warm_start: "{r} doesn't set its values, include {req}"},
)
def use_existing_assignments(ctx: RuleContext) -> None:
	for comb in ctx.data.forced:
		ctx.x[comb].fixValue()


@builtin_rule(
	"One Branch per Shift",
	"Employees can't cover more than one branch during a single shift.",
	standard=False,
)
def one_branch_per_shift(ctx: RuleContext) -> None:
	data = ctx.data
	for e, s, d in itertools.product(data.employees, data.shift_types, data.working_days):
		ctx.prob += (
			pulp.lpSum(ctx.x[(e, s, d, b)] for b in data.branches) <= 1,
			_cname("one_branch", e, s, d),
		)


@builtin_rule(
	"Room coverage per discipline",
	"The rooms staffed in a discipline for a given shift, day and branch equal the room-slots "
	"contributed by the discipline's assigned employees (each contributes their max-rooms "
	"figure), capped at the branch's configured room count.",
	standard=True,
)
def room_coverage(ctx: RuleContext) -> None:
	"""Note that the branch room cap is modeled by the variable bound."""
	data = ctx.data
	for k, s, d, b in ctx.active_rooms:
		employees_in_discipline = [e for e in data.employees if data.department.get(e) == k]
		# lpSum([])=0 (no need for a condition)
		ctx.prob += (
			pulp.lpSum(data.max_rpe.get(e, 1) * ctx.x[(e, s, d, b)] for e in employees_in_discipline)
			== ctx.active_rooms[(k, s, d, b)],
			_cname("room_coverage", k, s, d, b),
		)


@builtin_rule(
	"FTE ceiling",
	"An employee's total assigned shifts over the horizon stay at or below "
	"105% x their FTE-derived target; utilization pressure toward the target "
	"comes from the objective.",
	standard=True,
)
def fte_ceiling(ctx: RuleContext) -> None:
	data = ctx.data
	tol = 0.05
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


# ── Built-in objective rules (formerly hardcoded in model_builder.build) ──────


@builtin_rule(
	"Objective: Room utilization",
	"Maximize the total number of staffed rooms across all disciplines, shifts, days and branches.",
	kind=KIND_OBJECTIVE,
	standard=True,
	requires={room_coverage: "{r} requires {req}, otherwise the solver will just staff rooms for free."},
)
def room_utilization_objective(ctx: RuleContext) -> None:
	ctx.add_objective(pulp.lpSum(ctx.active_rooms.values()))


@builtin_rule(
	"Objective: Shift preferences",
	"Reward assignments matching each employee's normalized shift preferences (from Employee "
	"Settings); assignments to less-preferred shifts score lower.",
	kind=KIND_OBJECTIVE,
	standard=True,
)
def shift_preference_objective(ctx: RuleContext) -> None:
	data = ctx.data
	ctx.add_objective(
		pulp.lpSum(
			(-1 + data.shift_preferences.get(e, {}).get(s, 0.0)) * var
			for (e, s, _d, _b), var in ctx.x.items()
		)
	)


@builtin_rule(
	"Objective: Conserve Existing Assignments",
	"Promote tie-breaking towards existing assignments with small reward.",
	kind=KIND_OBJECTIVE,
	standard=False,
)
def weigh_assignments_objective(ctx: RuleContext) -> None:
	data = ctx.data
	epsilon = 2**-10
	ctx.add_objective(
		pulp.lpSum((0 if comb in data.forced else -epsilon) * var for comb, var in ctx.x.items())
	)


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
	namespace: dict = {"pulp": pulp, "itertools": itertools, "cname": _cname}
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


def _get_legacy_ruleset(ctx: RuleContext) -> tuple[DataPackage.RULE, ...]:
	if DataPackage.WEIGH_ASSIGNMENTS in ctx.data.flags:
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


def apply_rules(ctx: RuleContext) -> str:
	"""
	Apply the rules selected in ``ctx.data.rules`` to the problem.

	Each spec's weight scales the objective terms the rule contributes (via
	``ctx.add_objective``); constraint rules are unaffected by it. An empty
	selection applies every built-in rule at weight 1.0 (pre-ruleset behaviour).
	"""
	specs = ctx.data.rules or _get_legacy_ruleset(ctx)
	BuiltinRule.check_ruleset({key for _, key, _, _ in specs})
	logs = io.StringIO()
	for name, builtin_key, code, weight in specs:
		ctx._current_weight = weight
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
	return logs.getvalue()
