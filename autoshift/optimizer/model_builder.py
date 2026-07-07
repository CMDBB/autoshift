"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, s, d, b]              Binary  - employee e works shift s on day d at branch b
active_rooms[k, s, d, b]   Integer - rooms staffed in discipline k, shift s, day d, branch b

Constraints
-----------
Constraints come from named rules (see rules.py): the DataPackage's ``rules``
selection — loaded from the run's Optimization Ruleset — decides which apply.
An empty selection applies every built-in rule.

Objective (maximize)
--------------------
1. Room utilization:  turnover_weight * Σ active_rooms[k,s,d,b]
2. Shift preferences: Σ_{e,s,d,b} pref[e,s] * x[e,s,d,b]
	 pref[e,s] comes from Employee Settings → shift_preferences child table.
	 Missing entries are 0.0 (neutral).
"""

from __future__ import annotations

import itertools

import pulp

from .rules import RuleContext, apply_rules
from .types import DataPackage


def build(data: DataPackage) -> tuple[pulp.LpProblem, dict, dict]:
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

	# ── Constraints (selected rules) ──────────────────────────────────────────
	apply_rules(RuleContext(prob=prob, x=x, active_rooms=active_rooms, data=data))

	# ── Objective ─────────────────────────────────────────────────────────────

	# Term 1: room utilization (weighted by turnover_weight from Optimizer Settings)
	room_util = pulp.lpSum(
		active_rooms[(k, s, d, b)] for k, s, d, b in itertools.product(data.disciplines, S, D, B)
	)

	# Term 2: employee shift preferences (simple dot product)
	# Complexity is handled on the user side
	pref_sum = pulp.lpSum(
		(-1 + data.shift_preferences.get(e, {}).get(s, 0.0)) * x[(e, s, d, b)]
		for e, s, d, b in itertools.product(E, S, D, B)
	)

	prob += data.turnover_weight * room_util + pref_sum

	return prob, x, active_rooms
