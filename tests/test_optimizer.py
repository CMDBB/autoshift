"""
Unit tests for the optimizer modules (types, model_builder).

No Frappe context needed — imports only pure-Python code.
Run with:  pytest autoshift/tests/test_optimizer.py
"""

from __future__ import annotations

import datetime
from typing import Any

import pulp
import pytest

from autoshift.optimizer.model_builder import build
from autoshift.optimizer.types import DataPackage, planning_days

# ── constants & helpers ───────────────────────────────────────────────────────

MON = datetime.date(day=1, month=6, year=2026)  # a known Monday


def days_from(n: int, start: datetime.date = MON) -> list[datetime.date]:
	return [start + datetime.timedelta(days=i) for i in range(n)]


def pkg(**overrides) -> DataPackage:
	"""
	Minimal valid DataPackage: 1 salaried employee, 1 AM shift, 1 day, 1 branch.
	Override any field to build specific scenarios.
	"""
	disc = "Omni"
	b = "B1"
	base: dict[str, Any] = dict(
		employees=["E1"],
		shift_types=["AM"],
		working_days=[MON],
		branches=[b],
		designation={"E1": "Doctor"},
		department={"E1": disc},
		is_salaried={"E1": True},
		target_shifts={"E1": 1},
		max_rpe={"E1": 1},
		rooms={(disc, b): 1},
		disciplines=[disc],
		leave_blocked=set(),
		forced=set(),
		fte_tolerance=0.0,
		turnover_weight=1.0,
	)
	base.update(overrides)
	if "shift_preferences" not in base:
		n_shifts: int = len(base["shift_types"])
		base["shift_preferences"] = {
			e: {s: 1 / n_shifts for s in base["shift_types"]} for e in base["employees"]
		}
	return DataPackage(**base)


def solve(data: DataPackage):
	prob, x, ar = build(data)
	prob.solve(pulp.COIN_CMD(msg=False))
	return prob, x, ar


def status(prob) -> str:
	return pulp.LpStatus[prob.status]


def assigned(x, employee=None, shift=None) -> int:
	"""Count binary variables set to 1, optionally filtered by employee and/or shift."""
	total = 0
	for (e, s, _d, _b), var in x.items():
		if employee is not None and e != employee:
			continue
		if shift is not None and s != shift:
			continue
		if (pulp.value(var) or 0) > 0.5:
			total += 1
	return total


# ── planning_days ─────────────────────────────────────────────────────────────


def test_planning_days():
	d = planning_days(MON, "1-week")
	assert len(d) == 7
	assert d[0] == MON
	assert d[-1] == MON + datetime.timedelta(days=6)
	assert len(planning_days(MON, "2-week")) == 14
	assert len(planning_days(MON, "4-week")) == 28


def test_unbounded_nyi():
	with pytest.raises(NotImplementedError):
		_ = planning_days(MON, "unbounded")


# ── build() guards ────────────────────────────────────────────────────────────


def test_raises_on_empty():
	with pytest.raises(ValueError):
		build(pkg(employees=[]))
	with pytest.raises(ValueError):
		build(pkg(shift_types=[]))
	with pytest.raises(ValueError):
		build(pkg(working_days=[]))


# ── infeasibility ─────────────────────────────────────────────────────────────


def test_infeasible_when_fte_impossible():
	"""
	Salaried employee requires exactly 2 shifts but only 1 slot exists.
	With zero tolerance the FTE lower bound can't be met.
	"""
	prob, _x, _ = solve(pkg(target_shifts={"E1": 2}, fte_tolerance=0.0))
	assert status(prob) == "Infeasible"


# ── single-employee optimal cases ─────────────────────────────────────────────


def test_single_employee_is_assigned():
	prob, x, _ = solve(pkg())
	assert status(prob) == "Optimal"
	assert assigned(x) == 1


def test_employee_on_leave_is_not_assigned():
	prob, x, _ = solve(
		pkg(
			leave_blocked={("E1", MON)},
			is_salaried={"E1": False},
			target_shifts={"E1": 0},
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_forced_assignment_is_honored():
	prob, x, _ = solve(pkg(forced={("E1", "AM", MON, "B1")}))
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "AM", MON, "B1")]) or 0) > 0.5


# ── one-shift-per-day constraint ──────────────────────────────────────────────


def test_at_most_one_shift_per_employee_per_day():
	"""Two shift types available; employee may work at most one of them."""
	prob, x, _ = solve(
		pkg(
			shift_types=["AM", "PM"],
			target_shifts={"E1": 1},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	am = pulp.value(x[("E1", "AM", MON, "B1")]) or 0
	pm = pulp.value(x[("E1", "PM", MON, "B1")]) or 0
	assert am + pm <= 1 + 1e-6


# ── FTE constraints ───────────────────────────────────────────────────────────


def test_salaried_meets_exact_target():
	D = days_from(4)
	prob, x, _ = solve(
		pkg(
			working_days=D,
			target_shifts={"E1": 2},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2


def test_turnover_meets_minimum_but_may_exceed():
	"""
	Turnover employee has only a lower bound. With room-utilization driving
	assignments and sufficient capacity, they should work at least target shifts.
	"""
	D = days_from(4)
	prob, x, _ = solve(
		pkg(
			working_days=D,
			is_salaried={"E1": False},
			target_shifts={"E1": 2},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") >= 2


def test_salaried_does_not_exceed_upper_bound():
	"""
	Salaried employee with target=2 and zero tolerance must work exactly 2 shifts
	even when more slots are available.
	"""
	D = days_from(5)
	prob, x, _ = solve(
		pkg(
			working_days=D,
			target_shifts={"E1": 2},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2


# ── room constraints ──────────────────────────────────────────────────────────


def test_active_rooms_cannot_exceed_assistants_in_slot():
	"""
	With room capacity=3 but only 1 assistant in any slot,
	active_rooms must stay ≤ 1 for every slot.
	"""
	D = days_from(2)
	disc, b = "Omni", "B1"
	prob, _x, ar = solve(
		pkg(
			working_days=D,
			rooms={(disc, b): 3},
			target_shifts={"E1": 2},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	for var in ar.values():
		assert (pulp.value(var) or 0) <= 1 + 1e-6


def test_max_rooms_per_employee_limits_cross_branch_assignment():
	"""
	max_rpe=1: an employee can only be in one branch per shift-slot, even
	when two branches exist.
	"""
	prob, x, _ = solve(
		pkg(
			branches=["B1", "B2"],
			rooms={("Omni", "B1"): 1, ("Omni", "B2"): 1},
			max_rpe={"E1": 1},
			target_shifts={"E1": 1},
			fte_tolerance=0.0,
		)
	)
	assert status(prob) == "Optimal"
	# E1 can work at most one branch on the one available day
	total = (pulp.value(x[("E1", "AM", MON, "B1")]) or 0) + (pulp.value(x[("E1", "AM", MON, "B2")]) or 0)
	assert total <= 1 + 1e-6


# ── fairness objective ────────────────────────────────────────────────────────


def test_fairness_equalizes_unfairness_not_individual_balance():
	"""
	E1 is force-assigned to AM on every day (unfairness=4). The fairness term
	minimizes |unfairness(E1) - unfairness(E2)|, so it steers E2 toward an
	equally skewed schedule (2 AM *or* 2 PM, unfairness=2, |4-2|=2) rather than
	a personally balanced one (1 AM + 1 PM, unfairness=0, |4-0|=4).
	"""
	D = days_from(4)
	disc, b = "Omni", "B1"
	forced_e1 = {("E1", "AM", d, b) for d in D}

	data = pkg(
		employees=["E1", "E2"],
		shift_types=["AM", "PM"],
		working_days=D,
		designation={"E1": "Nurse", "E2": "Nurse"},
		department={"E1": disc, "E2": disc},
		is_salaried={"E1": False, "E2": True},
		target_shifts={"E1": 4, "E2": 2},
		max_rpe={"E1": 1, "E2": 1},
		rooms={(disc, b): 2},
		forced=forced_e1,
		fte_tolerance=0.0,
		turnover_weight=1.0,
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"

	# All forced E1 AM slots must be assigned
	for d in D:
		assert (pulp.value(x[("E1", "AM", d, b)]) or 0) > 0.5

	# E2 must work exactly 2 shifts (salaried, zero tolerance)
	e2_total = assigned(x, employee="E2")
	assert e2_total == 2

	# E2 should be skewed (2 AM or 2 PM), not balanced (1+1)
	e2_am = assigned(x, employee="E2", shift="AM")
	e2_pm = assigned(x, employee="E2", shift="PM")
	assert max(e2_am, e2_pm) == 2
	assert min(e2_am, e2_pm) == 0


# ── multi-employee integration ────────────────────────────────────────────────


def test_two_employees_both_meet_fte_targets():
	D = days_from(4)
	disc, b = "Omni", "B1"
	data = pkg(
		employees=["E1", "E2"],
		working_days=D,
		designation={"E1": "Doctor", "E2": "Doctor"},
		department={"E1": disc, "E2": disc},
		is_salaried={"E1": True, "E2": True},
		target_shifts={"E1": 2, "E2": 3},
		max_rpe={"E1": 1, "E2": 1},
		rooms={(disc, b): 2},
		fte_tolerance=0.0,
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2
	assert assigned(x, employee="E2") == 3


def test_leave_does_not_block_other_employees():
	"""E1 on leave; E2 should still be assigned normally."""
	disc, b = "Omni", "B1"
	data = pkg(
		employees=["E1", "E2"],
		designation={"E1": "Doctor", "E2": "Doctor"},
		department={"E1": disc, "E2": disc},
		is_salaried={"E1": False, "E2": True},
		target_shifts={"E1": 0, "E2": 1},
		max_rpe={"E1": 1, "E2": 1},
		rooms={(disc, b): 1},
		leave_blocked={("E1", MON)},
		fte_tolerance=0.0,
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0
	assert assigned(x, employee="E2") == 1
