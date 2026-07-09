"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, s, d, b]              Binary  - employee e works shift s on day d at branch b
active_rooms[k, s, d, b]   Integer - rooms staffed in discipline k, shift s, day d, branch b

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

from .rules import RuleContext, apply_rules
from .types import DataPackage


def build(data: DataPackage) -> tuple[pulp.LpProblem, dict, dict, str]:
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
	x: dict[tuple, pulp.LpVariable] = prob.add_variable_dict(
		"x",
		(E, S, D, B),
		cat=pulp.LpBinary,
	)

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
	ctx = RuleContext(prob=prob, x=x, active_rooms=active_rooms, data=data)
	logs = apply_rules(ctx)

	prob += pulp.lpSum(ctx.objective_terms)

	return prob, x, active_rooms, logs
