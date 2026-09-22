"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, r, s, d, b]           Binary  - employee e works shift s on day d at branch b in role r
p[e, s, d, b]              Binary  - e is *present* for shift s on day d at branch b
active_rooms[k, s, d, b]   Integer - rooms staffed in discipline k, shift s, day d, branch b

Presence is the settled half of somebody's week: a rota says which half-days they are in,
while the role they work during one may be the optimizer's to choose (see
`DataPackage.mode`). The constraints tying `p` to `x` are built here rather than in a rule
because they are what `p` *means*, not a policy about it — the same place the room cap lives
in `active_rooms`'s bound. Policy is still rules: how many presences a day
(`one_shift_per_day`), whose presence is settled (the binding rules), and what a presence
costs (the preference objectives).

Constraints and objective
-------------------------
Both come from named rules (see rules.py): the DataPackage's ``rules`` selection
— loaded from the run's Optimization Ruleset — decides which apply. Constraint
rules add constraints; objective rules contribute terms via
``ctx.add_objective``, each scaled by its ruleset row weight, and the maximized
objective is their sum. An empty selection applies every built-in rule at
weight 1.0. No objective rules selected = constant-zero objective (a pure
feasibility problem).
"""

from __future__ import annotations

import itertools

import pulp

from .rules import RuleContext, _cname, apply_rules
from .types import DataPackage


def presence_variables(data: DataPackage, prob: pulp.LpProblem) -> dict[tuple, pulp.LpVariable]:
	"""The `p[e, s, d, b]` variables, without the constraints that tie them to `x`.

	Split out so a throwaway problem can recreate them under the same names — that is how
	`sandbox.helpers.objective_breakdown` re-runs an objective rule against an already-solved
	model.
	"""
	return {
		key: variable
		for e in data.employees
		for key, variable in prob.add_variable_dict(
			"p", ([e], data.shift_types, data.working_days, data.branches), cat=pulp.LpBinary
		).items()
	}


def _link_presence(prob: pulp.LpProblem, data: DataPackage, x: dict, presence: dict) -> None:
	"""What presence means: one working role per presence, collateral beside it, none idle.

	`Σ working ≤ p` allows a presence spent entirely on a collateral duty; `p ≤ Σ working +
	Σ collateral` refuses a presence spent on nothing at all, which would otherwise let a
	rule that counts presence (an FTE ceiling, a binding equality) be satisfied by thin air.
	Both are per branch, so a collateral duty lands at its host's branch without any rule
	saying so.
	"""
	for (e, s, d, b), p in presence.items():
		working = [x[(e, r, s, d, b)] for r in data.working_roles(e)]
		collateral = [x[(e, r, s, d, b)] for r in data.collateral_roles(e)]
		if working:
			prob += (pulp.lpSum(working) <= p, _cname("presence_role", e, s, d, b))
		for r in data.collateral_roles(e):
			prob += (x[(e, r, s, d, b)] <= p, _cname("presence_collateral", e, r, s, d, b))
		prob += (p <= pulp.lpSum(working + collateral), _cname("presence_idle", e, s, d, b))


def build(data: DataPackage) -> tuple[pulp.LpProblem, dict, dict, str, RuleContext]:
	prob = pulp.LpProblem("shift_optimizer", pulp.LpMaximize)

	E = data.employees
	S = data.shift_types
	D = data.working_days
	B = data.branches

	if not E:
		raise ValueError("No eligible employees found.")
	if not S:
		raise ValueError("No shift types found.")
	if not D:
		raise ValueError("No working days in planning horizon.")

	# ── Decision variables ────────────────────────────────────────────────────
	# x is indexed by the (employee, role) pairs each employee actually holds, not by the
	# full employee x role product. Role eligibility is therefore structural: a variable
	# for a role somebody cannot work simply does not exist, so no rule has to forbid it
	# and the model is no larger than it was before roles. This mirrors active_rooms
	# below, whose branch room cap likewise lives in the variable bound rather than in a
	# constraint (see rules.room_coverage).
	x: dict[tuple, pulp.LpVariable] = {}
	for e in E:
		for r in data.employee_roles.get(e, ()):
			x |= prob.add_variable_dict("x", ([e], [r], S, D, B), cat=pulp.LpBinary)

	if not x:
		raise ValueError(
			"No employee holds a Scheduling Role over this horizon, so there is nothing to "
			"schedule. Give the employees you want scheduled an Employee Scheduling Role."
		)

	presence: dict[tuple, pulp.LpVariable] = presence_variables(data, prob)
	_link_presence(prob, data, x, presence)

	active_rooms: dict[tuple, pulp.LpVariable] = {
		key: variable
		for k, b in itertools.product(data.disciplines, B)
		for key, variable in prob.add_variable_dict(
			"ar",
			([k], S, D, [b]),
			lowBound=0,
			upBound=data.rooms.get((k, b), 0),
			cat=pulp.LpInteger,
		).items()
	}

	# ── Constraints and objective terms (selected rules) ─────────────────────
	ctx = RuleContext(prob=prob, x=x, active_rooms=active_rooms, data=data, presence=presence)
	logs = apply_rules(ctx)

	prob += pulp.lpSum(ctx.objective_terms)

	# The ctx rides along for diagnostics: its objective_contributions map each rule's
	# document name to the terms it contributed, which the solver evaluates against the
	# solved variables into the run's per-rule objective breakdown.
	return prob, x, active_rooms, logs, ctx
