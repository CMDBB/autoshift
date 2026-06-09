"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, s, d, b]              Binary  - employee e works shift s on day d at branch b
active_rooms[k, s, d, b]   Integer - rooms staffed in discipline k, shift s, day d, branch b

Objective (maximize)
--------------------
1. Room utilization:      turnover_weight x Σ active_rooms[k,s,d,b]
2. Shift preferences:     Σ_{e,s,d,b} pref[e,s] x x[e,s,d,b]
   pref[e,s] comes from Employee Settings → shift_preferences child table.
   Missing entries are 0.0 (neutral). No auxiliary variables required.
"""

from __future__ import annotations

import itertools

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
	x: dict[tuple, pulp.LpVariable] = {}
	for e, s, d, b in itertools.product(E, S, D, B):
		x[(e, s, d, b)] = prob.add_variable(
			f"x_{e}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			cat="Binary",
		)

	active_rooms: dict[tuple, pulp.LpVariable] = {}
	for k, s, d, b in itertools.product(data.disciplines, S, D, B):
		cap = data.rooms.get((k, b), 0)
		active_rooms[(k, s, d, b)] = prob.add_variable(
			f"ar_{k}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			lowBound=0,
			upBound=cap,
			cat="Integer",
		)

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
		prob += (
			pulp.lpSum(x[(e, s, d, b)] for s in S for b in B) == 0,
			f"leave_{e}_{d}".replace("-", "_").replace(" ", "_"),
		)

	# 3. Forced assignments
	for e, s, d, b in data.forced:
		if e not in E or s not in S or d not in set(D) or b not in B:
			continue
		prob += (
			x[(e, s, d, b)] == 1,
			f"forced_{e}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
		)

	# 4. Max rooms per employee per slot (each shift+day combination)
	for e, s, d in itertools.product(E, S, D):
		limit = data.max_rpe.get(e, 1)
		prob += (
			pulp.lpSum(x[(e, s, d, b)] for b in B) <= limit,
			f"max_rpe_{e}_{s}_{d}".replace("-", "_").replace(" ", "_"),
		)

	# 5. Assistant coverage: assistants assigned ≥ active_rooms in that discipline/slot
	for k, s, d, b in itertools.product(data.disciplines, S, D, B):
		assistants_in_discipline = [
			e for e in E
			if data.designation.get(e) in data.assistant_designations.get(k, [])
			and data.department.get(e) == k
		]
		if not assistants_in_discipline:
			# If no assistants configured for this discipline, force active_rooms to 0
			prob += (
				active_rooms[(k, s, d, b)] == 0,
				f"no_assistants_{k}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			)
		else:
			prob += (
				pulp.lpSum(x[(e, s, d, b)] for e in assistants_in_discipline)
				>= active_rooms[(k, s, d, b)],
				f"asst_coverage_{k}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			)

	# 6 & 7. FTE targets
	tol = data.fte_tolerance
	for e in E:
		target = data.target_shifts.get(e, 0)
		total_assigned = pulp.lpSum(x[(e, s, d, b)] for s in S for d in D for b in B)
		if data.is_salaried.get(e, True):
			# Salaried: two-sided bound
			if target > 0:
				prob += (
					total_assigned >= (1 - tol) * target,
					f"fte_min_{e}".replace("-", "_").replace(" ", "_"),
				)
				prob += (
					total_assigned <= (1 + tol) * target,
					f"fte_max_{e}".replace("-", "_").replace(" ", "_"),
				)
		else:
			# Turnover-paid: minimum only
			if target > 0:
				prob += (
					total_assigned >= (1 - tol) * target,
					f"fte_min_{e}".replace("-", "_").replace(" ", "_"),
				)

	# ── Objective ─────────────────────────────────────────────────────────────

	# Term 1: room utilization (weighted by turnover_weight from Optimizer Settings)
	room_util = pulp.lpSum(
		active_rooms[(k, s, d, b)]
		for k, s, d, b in itertools.product(data.disciplines, S, D, B)
	)

	# Term 2: employee shift preferences (simple dot product)
  # Complexity is handled on the user side
	pref_sum = pulp.lpSum(
		data.shift_preferences.get(e, {}).get(s, 0.0) * x[(e, s, d, b)]
		for e, s, d, b in itertools.product(E, S, D, B)
	)

	prob += data.turnover_weight * room_util + pref_sum

	return prob, x, active_rooms
