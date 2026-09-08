"""
Unit tests for `optimizer/diagnostics.py`.

The scenarios are the ones role binding actually produces: a settled rota that works two
half-days on one date, one that runs fuller than its contract, one that collides with
approved leave. Each must be named by the solver-free scan *and* found by the elastic
analysis, because the two are independent instruments and the report leans on both.

No Frappe context needed. Run with: uv run pytest tests/test_diagnostics.py
"""

from __future__ import annotations

import datetime
from typing import Any

import pulp
import pytest

from autoshift.optimizer import diagnostics
from autoshift.optimizer.model_builder import build
from autoshift.optimizer.types import DataPackage

MON = datetime.date(day=1, month=6, year=2026)  # a known Monday
TUE = MON + datetime.timedelta(days=1)


def pkg(**overrides) -> DataPackage:
	"""Minimal valid DataPackage: 1 employee, AM/PM shifts, 2 days, 1 branch, 1 room."""
	disc, b = "D1", "B1"
	base: dict[str, Any] = {
		"flags": set(),
		"employees": ["E1"],
		"shift_types": ["AM", "PM"],
		"working_days": [MON, TUE],
		"branches": [b],
		"roles": ["R1"],
		"role_discipline": {"R1": disc},
		"employee_roles": {"E1": ("R1",)},
		"target_shifts": {"E1": 2},
		"role_target_shifts": {},
		"max_rpe": {("E1", "R1"): 1},
		"rooms": {(disc, b): 1},
		"disciplines": [disc],
		"leave_blocked": set(),
		"forced": set(),
	}
	base.update(overrides)
	if "shift_preferences" not in base:
		n = len(base["shift_types"])
		base["shift_preferences"] = {e: {s: 1 / n for s in base["shift_types"]} for e in base["employees"]}
	return DataPackage(**base)


def specs(*keys: str) -> tuple[tuple[str, str, str, float], ...]:
	return tuple((k, k, "", 1.0) for k in keys)


def bound(**overrides) -> DataPackage:
	"""A package whose single employee's schedule is settled (bound) by `forced`."""
	overrides.setdefault("binding_pairs", frozenset({("E1", "R1")}))
	overrides.setdefault("rules", specs("warm_start", "bind_role_assignments", "one_shift_per_day"))
	return pkg(**overrides)


def solve(data: DataPackage) -> str:
	prob, *_ = build(data)
	prob.solve(pulp.COIN_CMD(msg=False))
	return pulp.LpStatus[prob.status]


# ── grouping ──────────────────────────────────────────────────────────────────


def test_group_of_reads_both_naming_conventions():
	# `_cname`/`_vname` prefix with "<rule>:"; PuLP's add_variable_dict only underscores.
	assert diagnostics.group_of("one_shift:one_shift_E1_2026_06_01") == "one_shift"
	assert diagnostics.group_of("x_E1_R1_AM_2026_06_01_B1") == "x"
	assert diagnostics.group_of("ar_D1_AM_2026_06_01_B1") == "ar"


# ── selected_builtins / pinned_assignments ────────────────────────────────────


def test_selected_builtins_falls_back_to_the_standard_set():
	# An empty selection means "every standard built-in" to apply_rules, so the scan has
	# to read it the same way or it reports conflicts the model does not have.
	from autoshift.optimizer.rules import STANDARD_RULES

	assert diagnostics.selected_builtins(pkg()) == STANDARD_RULES
	assert diagnostics.selected_builtins(pkg(rules=specs("one_shift_per_day"))) == {"one_shift_per_day"}


def test_pinned_assignments_only_covers_bound_pairs_under_binding():
	comb_bound = ("E1", "R1", "AM", MON, "B1")
	comb_free = ("E2", "R1", "AM", MON, "B1")
	data = bound(
		employees=["E1", "E2"],
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 2, "E2": 2},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		forced={comb_bound, comb_free},
	)
	pinned = diagnostics.pinned_assignments(data)
	assert set(pinned) == {comb_bound}

	# use_existing_assignments pins everybody's, bound or not
	everyone = diagnostics.pinned_assignments(
		pkg(forced={comb_bound, comb_free}, rules=specs("warm_start", "use_existing_assignments"))
	)
	assert set(everyone) == {comb_bound, comb_free}


def test_nothing_pinned_when_neither_honoring_rule_is_selected():
	data = pkg(forced={("E1", "R1", "AM", MON, "B1")}, rules=specs("warm_start", "one_shift_per_day"))
	assert diagnostics.pinned_assignments(data) == {}
	assert diagnostics.conflict_scan(data) == []


# ── conflict_scan ─────────────────────────────────────────────────────────────


def test_a_settled_full_day_breaks_one_shift_per_day():
	"""The half-day rota case: a morning and an afternoon on one date, both frozen on."""
	data = bound(forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "PM", MON, "B1")})
	assert solve(data) == "Infeasible"

	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["one_shift_per_day"]
	assert "E1 on 2026-06-01" == conflicts[0].subject
	assert "2 shifts" in conflicts[0].detail


def test_a_settled_rota_over_contract_breaks_the_fte_ceiling():
	data = bound(
		target_shifts={"E1": 1},  # 105% of 1 is 1.05, so two pinned shifts cannot fit
		rules=specs("warm_start", "bind_role_assignments", "fte_ceiling"),
		forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "AM", TUE, "B1")},
	)
	assert solve(data) == "Infeasible"

	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["fte_ceiling"]
	assert "pinned to 2 shifts" in conflicts[0].detail


def test_an_agreed_role_split_ceiling_is_scanned_too():
	data = bound(
		role_target_shifts={("E1", "R1"): 1.0},
		rules=specs("warm_start", "bind_role_assignments", "role_fte_ceiling"),
		forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "AM", TUE, "B1")},
	)
	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["role_fte_ceiling"]


def test_two_branches_in_one_shift_breaks_one_branch_per_shift():
	data = bound(
		branches=["B1", "B2"],
		rooms={("D1", "B1"): 1, ("D1", "B2"): 1},
		rules=specs("warm_start", "bind_role_assignments", "one_branch_per_shift"),
		forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "AM", MON, "B2")},
	)
	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["one_branch_per_shift"]
	assert "2 branches" in conflicts[0].detail


def test_leave_over_a_settled_shift_is_reported():
	data = bound(
		leave_blocked={("E1", MON)},
		rules=specs("warm_start", "bind_role_assignments", "leave_blocklist"),
		forced={("E1", "R1", "AM", MON, "B1")},
	)
	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["leave_blocklist"]
	assert "on leave" in conflicts[0].detail


def test_a_pinned_combination_with_no_variable_is_structural():
	# The loader throws on this for a bound employee; a hand-built package can still hold it.
	data = bound(forced={("E1", "R_GONE", "AM", MON, "B1")}, binding_pairs=frozenset({("E1", "R_GONE")}))
	conflicts = diagnostics.conflict_scan(data)
	assert [c.rule for c in conflicts] == ["(structural)"]


def test_a_feasible_settled_rota_scans_clean():
	data = bound(forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "PM", TUE, "B1")})
	assert solve(data) == "Optimal"
	assert diagnostics.conflict_scan(data) == []


# ── model_dump ────────────────────────────────────────────────────────────────


def test_model_dump_groups_variables_and_constraints_by_rule():
	data = bound(forced={("E1", "R1", "AM", MON, "B1")})
	prob, *_ = build(data)
	text = diagnostics.model_dump(prob)

	assert "x" in {group.name for group in diagnostics.variable_summary(prob)}
	assert "one_shift" in {group.name for group in diagnostics.constraint_summary(prob)}
	# binding fixes every one of the pair's variables; exactly one of them is fixed *on*
	x_group = next(g for g in diagnostics.variable_summary(prob) if g.name == "x")
	assert x_group.fixed == x_group.count
	assert x_group.fixed_on == 1
	assert "Constraints by rule:" in text


def test_write_lp_writes_the_whole_model(tmp_path):
	data = pkg(rules=specs("one_shift_per_day"))
	prob, *_ = build(data)
	path = diagnostics.write_lp(prob, str(tmp_path / "model.lp"))
	written = (tmp_path / "model.lp").read_text()
	assert path == str(tmp_path / "model.lp")
	assert "one_shift" in written  # constraints, by the name the rule gave them
	assert "x_E1_R1_AM_2026_06_01_B1" in written  # and every variable


# ── elastic analysis ──────────────────────────────────────────────────────────


def test_elastic_analysis_finds_nothing_to_relax_in_a_feasible_model():
	status, violations = diagnostics.elastic_analysis(pkg())
	assert status == "Optimal"
	assert violations == []


def test_elastic_analysis_names_the_constraint_a_settled_full_day_breaks():
	data = bound(forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "PM", MON, "B1")})
	status, violations = diagnostics.elastic_analysis(data)

	assert status == "Optimal"  # elasticized, so it always solves
	assert [v.rule for v in violations] == ["one_shift"]
	# both half-days are pinned on and only one may stand: the constraint is out by exactly 1
	assert violations[0].amount == pytest.approx(1.0)


def test_elastic_analysis_reports_the_size_of_an_fte_overrun():
	data = bound(
		target_shifts={"E1": 1},
		rules=specs("warm_start", "bind_role_assignments", "fte_ceiling"),
		forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "AM", TUE, "B1")},
	)
	status, violations = diagnostics.elastic_analysis(data)
	assert status == "Optimal"
	assert [v.rule for v in violations] == ["fte_max"]
	assert violations[0].amount == pytest.approx(0.95)  # 2 pinned against a ceiling of 1.05


def test_elasticize_leaves_variable_bounds_alone():
	"""The fixed bounds *are* the binding; relaxing them would hide what we are looking for."""
	data = bound(forced={("E1", "R1", "AM", MON, "B1")})
	prob, *_ = build(data)
	before = {var.name: (var.lowBound, var.upBound) for var in prob.variables()}
	diagnostics.elasticize(prob)
	after = {var.name: (var.lowBound, var.upBound) for var in prob.variables()}
	assert all(after[name] == bounds for name, bounds in before.items())
	assert prob.sense == pulp.LpMinimize


# ── LP relaxation / shadow prices ─────────────────────────────────────────────


def test_relax_integrality_makes_every_variable_continuous():
	prob, *_ = build(pkg())
	assert any(var.cat != pulp.LpContinuous for var in prob.variables())
	diagnostics.relax_integrality(prob)
	assert all(var.cat == pulp.LpContinuous for var in prob.variables())


def test_the_relaxation_prices_the_binding_constraint():
	# One employee, one room, two days: the FTE ceiling is what stops more room-hours.
	data = pkg(
		target_shifts={"E1": 1},
		rules=(
			*specs("warm_start", "one_shift_per_day", "fte_ceiling", "room_coverage"),
			("room_utilization_objective", "room_utilization_objective", "", 3.0),
		),
	)
	prob, _x, _ar, _ctx = diagnostics.lp_relaxation(data)
	prob.solve(pulp.COIN_CMD(msg=False))
	assert pulp.LpStatus[prob.status] == "Optimal"

	priced = dict(diagnostics.shadow_prices(prob))
	assert any(name.startswith("fte_max") for name in priced), priced


# ── report ────────────────────────────────────────────────────────────────────


def test_report_covers_input_conflicts_model_and_elastic_analysis():
	data = bound(forced={("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "PM", MON, "B1")})
	text = diagnostics.report(data)

	assert "AUTOSHIFT MODEL DIAGNOSTICS" in text
	assert data.input_hash() in text
	assert "one_shift_per_day" in text  # the scan named the rule
	assert "Constraints by rule:" in text  # the model was dumped
	assert "must give" in text  # the elastic analysis found the violation


def test_report_survives_a_model_that_will_not_build():
	# warm_start throws on a leave/assignment collision, so there is no model to dump —
	# the report still has to come back with the scan's finding rather than propagate.
	data = bound(
		leave_blocked={("E1", MON)},
		rules=specs("warm_start", "bind_role_assignments", "leave_blocklist"),
		forced={("E1", "R1", "AM", MON, "B1")},
	)
	with pytest.raises(ValueError):
		build(data)

	text = diagnostics.report(data)
	assert "build() raised" in text
	assert "on leave" in text
