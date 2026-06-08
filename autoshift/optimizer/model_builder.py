"""
Constructs the PuLP MILP model from a DataPackage.

Decision variables
------------------
x[e, s, d, b]              Binary  – employee e works shift s on day d at branch b
active_rooms[k, s, d, b]   Integer – rooms staffed in discipline k, shift s, day d, branch b

Objective (maximise)
--------------------
1. Room utilisation:   Σ active_rooms[k,s,d,b]
2. Shift fairness:    -Σ_{e1,e2} |unfairness(e1) − unfairness(e2)|
   where unfairness(e) = Σ_{s1<s2} |total_shifts_in_s1 − total_shifts_in_s2| for employee e
   Both absolute values are linearised with auxiliary non-negative variables.
"""

from __future__ import annotations

import itertools

import pulp

from .data_loader import DataPackage


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
		x[(e, s, d, b)] = pulp.LpVariable(
			f"x_{e}_{s}_{d}_{b}".replace("-", "_").replace(" ", "_"),
			cat="Binary",
		)

	active_rooms: dict[tuple, pulp.LpVariable] = {}
	for k, s, d, b in itertools.product(data.disciplines, S, D, B):
		cap = data.rooms.get((k, b), 0)
		active_rooms[(k, s, d, b)] = pulp.LpVariable(
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

	# Term 1: room utilisation
	room_util = pulp.lpSum(
		active_rooms[(k, s, d, b)]
		for k, s, d, b in itertools.product(data.disciplines, S, D, B)
	)

	# Term 2: shift fairness (linearised absolute values)
	# per-employee, per-shift-pair absolute difference in total shifts worked
	shift_pairs = [(s1, s2) for i, s1 in enumerate(S) for s2 in S[i + 1 :]]

	# diff[e, s1, s2] ≥ |Σ_{d,b} x[e,s1,d,b] − Σ_{d,b} x[e,s2,d,b]|
	diff_vars: dict[tuple, pulp.LpVariable] = {}
	for e, (s1, s2) in itertools.product(E, shift_pairs):
		key = (e, s1, s2)
		v = pulp.LpVariable(
			f"diff_{e}_{s1}_{s2}".replace("-", "_").replace(" ", "_"),
			lowBound=0,
		)
		diff_vars[key] = v
		lhs = pulp.lpSum(x[(e, s1, d, b)] - x[(e, s2, d, b)] for d in D for b in B)
		prob += (v >= lhs, f"diff_pos_{e}_{s1}_{s2}".replace("-", "_").replace(" ", "_"))
		prob += (v >= -lhs, f"diff_neg_{e}_{s1}_{s2}".replace("-", "_").replace(" ", "_"))

	# unfairness(e) expressed as a linear combination (not a standalone variable)
	def unfairness_expr(e):
		return pulp.lpSum(diff_vars[(e, s1, s2)] for (s1, s2) in shift_pairs)

	# pair_diff[e1, e2] ≥ |unfairness(e1) − unfairness(e2)|  for e1 < e2
	pair_diff_vars: dict[tuple, pulp.LpVariable] = {}
	employee_pairs = [(e1, e2) for i, e1 in enumerate(E) for e2 in E[i + 1 :]]
	for e1, e2 in employee_pairs:
		key = (e1, e2)
		v = pulp.LpVariable(
			f"pdiff_{e1}_{e2}".replace("-", "_").replace(" ", "_"),
			lowBound=0,
		)
		pair_diff_vars[key] = v
		delta = unfairness_expr(e1) - unfairness_expr(e2)
		prob += (v >= delta, f"pdiff_pos_{e1}_{e2}".replace("-", "_").replace(" ", "_"))
		prob += (v >= -delta, f"pdiff_neg_{e1}_{e2}".replace("-", "_").replace(" ", "_"))

	total_unfairness = pulp.lpSum(pair_diff_vars.values())

	prob += data.turnover_weight * room_util - total_unfairness

	return prob, x, active_rooms
