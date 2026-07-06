"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, s, d, b]              Binary  - employee e works shift s on day d at branch b
active_rooms[k, s, d, b]   Integer - rooms staffed in discipline k, shift s, day d, branch b

Room coverage
-------------
Every employee in a discipline contributes max_rpe[e] room-slots per assigned shift.
The weighted sum must be >= active_rooms for that discipline/slot/branch.

Objective (maximize)
--------------------
1. Room utilization:  turnover_weight * Σ active_rooms[k,s,d,b]
2. Shift preferences: Σ_{e,s,d,b} pref[e,s] * x[e,s,d,b]
	 pref[e,s] comes from Employee Settings → shift_preferences child table.
	 Missing entries are 0.0 (neutral).
"""

from __future__ import annotations

import itertools
from typing import Any

import pulp

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

	# ── Constraints ───────────────────────────────────────────────────────────

	# 1. One shift per employee per day (across all shifts and branches)
	for e, d in itertools.product(E, D):
		prob += (
			pulp.lpSum(x[(e, s, d, b)] for s in S for b in B) <= 1,
			f"one_shift_{e}_{d}".replace("-", "_").replace(" ", "_"),
		)

	# 2. Leave blocklist
	for e, d in data.leave_blocked:
		if e not in E or d not in set(D):
			continue
		for s in S:
			for b in B:
				x[(e, s, d, b)].setInitialValue(0)
				x[(e, s, d, b)].fixValue()

	# 3. Forced assignments
	for comb in itertools.product(E, S, D, B):
		x[comb].setInitialValue(1 if comb in data.forced else 0)
	if data.WEIGH_ASSIGNMENTS not in data.flags:
		# this is the 'Use'/'Ignore' mode
		for comb in itertools.product(E, S, D, B):
			if comb in data.forced:
				x[comb].fixValue()

	# 4. Max rooms per employee per slot (each shift+day combination)
	for e, s, d in itertools.product(E, S, D):
		limit = data.max_rpe.get(e, 1)
		prob += (
			pulp.lpSum(x[(e, s, d, b)] for b in B) <= limit,
			f"max_rpe_{e}_{s}_{d}".replace("-", "_").replace(" ", "_"),
		)

	# 5. Room coverage: weighted sum of all employees in discipline ≥ active_rooms
	#    Each employee contributes max_rpe[e] rooms per slot assigned.
	for k, s, d, b in itertools.product(data.disciplines, S, D, B):
		employees_in_discipline = [e for e in E if data.department.get(e) == k]
		if not employees_in_discipline:
			prob += (
				active_rooms[(k, s, d, b)] == 0,
				f"no_employees_{k}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			)
		else:
			prob += (
				pulp.lpSum(data.max_rpe.get(e, 1) * x[(e, s, d, b)] for e in employees_in_discipline)
				== active_rooms[(k, s, d, b)],
				f"room_coverage_{k}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			)

	# 6 & 7. FTE targets
	tol = data.fte_tolerance
	for e in E:
		target = data.target_shifts.get(e, 0)
		total_assigned = pulp.lpSum(x[(e, s, d, b)] for s in S for d in D for b in B)
		if target > 0:
			# upper bound only: employee utilization will come from objective function
			prob += (
				total_assigned <= (1 + tol) * target,
				f"fte_max_{e}".replace("-", "_").replace(" ", "_"),
			)

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
