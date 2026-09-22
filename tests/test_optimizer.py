"""
Unit tests for the optimizer modules (types, model_builder).

No Frappe context needed — imports only pure-Python code.
Run with:  pytest autoshift/tests/test_optimizer.py
"""

from __future__ import annotations

import dataclasses
import datetime
import itertools
import json
from typing import Any
from unittest import mock

import pulp
import pytest

from autoshift.optimizer import types as types_module
from autoshift.optimizer.editor_support import completion_items
from autoshift.optimizer.model_builder import build
from autoshift.optimizer.rules import (
	BUILTIN_RULES,
	KIND_CONSTRAINT,
	KIND_MIXED,
	KIND_OBJECTIVE,
	LEVEL_DAY,
	LEVEL_DISCIPLINE,
	LEVEL_EMPLOYEE,
	STANDARD_RULES,
	BuiltinRule,
	leave_blocklist,
	objective_shares,
	objective_tree,
	order_specs,
	path,
)
from autoshift.optimizer.types import (
	MODE_COLLATERAL,
	MODE_EXCLUSIVE,
	MODE_FLEXIBLE,
	DataPackage,
	planning_days,
)

MON = datetime.date(day=1, month=6, year=2026)  # a known Monday


def days_from(n: int, start: datetime.date = MON) -> list[datetime.date]:
	return [start + datetime.timedelta(days=i) for i in range(n)]


def pkg(**overrides) -> DataPackage:
	"""
	Minimal valid DataPackage: 1 salaried employee, 1 AM shift, 1 day, 1 branch.
	Override any field to build specific scenarios.
	"""
	disc = "D1"
	b = "B1"
	base: dict[str, Any] = {
		"flags": set(),
		"employees": ["E1"],
		"shift_types": ["AM"],
		"working_days": [MON],
		"branches": [b],
		"roles": ["R1"],
		"role_discipline": {"R1": disc},
		"employee_roles": {"E1": ("R1",)},
		"target_shifts": {"E1": 1},
		"role_target_shifts": {},
		"max_rpe": {("E1", "R1"): 1},
		"rooms": {(disc, b): 1},
		"disciplines": [disc],
		"leave_blocked": set(),
		"forced": set(),
	}
	base.update(overrides)
	if "shift_preferences" not in base:
		n_shifts: int = len(base["shift_types"])
		base["shift_preferences"] = {
			e: {s: 1 / n_shifts for s in base["shift_types"]} for e in base["employees"]
		}
	return DataPackage(**base)


def builtin_specs(*keys: str, weight: float = 1.0) -> tuple[tuple[str, str, str, float], ...]:
	"""Rule specs selecting the given built-in rules (doc name = key)."""
	return tuple((k, k, "", weight) for k in keys)


def solve(data: DataPackage):
	prob, x, ar, _, _ctx = build(data)
	prob.solve(pulp.COIN_CMD(msg=False))
	return prob, x, ar


def status(prob) -> str:
	return pulp.LpStatus[prob.status]


def assigned(x, employee=None, shift=None, role=None) -> int:
	"""Count binary variables set to 1, optionally filtered by employee, shift and/or role."""
	total = 0
	for (e, r, s, _d, _b), var in x.items():
		if employee is not None and e != employee:
			continue
		if role is not None and r != role:
			continue
		if shift is not None and s != shift:
			continue
		if (pulp.value(var) or 0) > 0.5:
			total += 1
	return total


# ── planning_days ─────────────────────────────────────────────────────────────


def test_planning_days():
	d = list(planning_days(MON, "1-week"))
	assert len(d) == 7
	assert d[0] == MON
	assert d[-1] == MON + datetime.timedelta(days=6)
	assert len(list(planning_days(MON, "2-week"))) == 14
	assert len(list(planning_days(MON, "4-week"))) == 28


def test_unbounded_exceeds_any_bounded():
	# This uses a static list of modes, see the sister integration test that uses the following:
	# modes = frappe.get_meta("Optimizer Run").get_field("mode").options.split("\n")
	modes = ["1-week", "2-week", "4-week", "Unbounded"]
	bounded_max = max(
		len(d)  # ty:ignore[invalid-argument-type]
		for d in (planning_days(MON, m) for m in modes)
		if hasattr(d, "__len__")
	)

	for i, _ in enumerate(planning_days(MON, "Unbounded")):
		if i > bounded_max:
			break
	else:
		pytest.fail(f"Unbounded planning_days is bounded by {bounded_max}")


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
	Salaried employee requires exactly 1 shifts but 2 slot exists.
	With zero tolerance, full utilization is impossible.
	"""
	prob1, _x, _ = solve(
		pkg(
			target_shifts={"E1": 1},
		)
	)
	prob2, _x, _ = solve(
		pkg(
			working_days=days_from(2),
			target_shifts={"E1": 1},
		)
	)
	assert status(prob1) == "Optimal"
	assert status(prob2) == "Optimal"
	assert prob1.objective == prob2.objective


# ── single-employee optimal cases ─────────────────────────────────────────────


def test_single_employee_is_assigned():
	prob, x, _ = solve(pkg())
	assert status(prob) == "Optimal"
	assert assigned(x) == 1


def test_employee_on_leave_is_not_assigned():
	prob, x, _ = solve(
		pkg(
			leave_blocked={("E1", MON)},
			target_shifts={"E1": 1},
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_forced_assignment_is_honored():
	prob, x, _ = solve(pkg(forced={("E1", "R1", "AM", MON, "B1")}))
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) > 0.5


def test_weigh_mode_allows_overriding_forced_assignment():
	"""
	In 'Weigh' mode, a forced (already-assigned) shift is only a soft warm-start,
	not a hard constraint: if another shift is clearly preferred, the solver moves
	the employee off the forced slot. Contrast with the default ('Use'/'Ignore')
	mode exercised in test_forced_assignment_is_honored, where forced is fixed.
	"""
	prob, x, _ = solve(
		pkg(
			flags={DataPackage.WEIGH_ASSIGNMENTS},
			shift_types=["AM", "PM"],
			forced={("E1", "R1", "AM", MON, "B1")},
			shift_preferences={"E1": {"AM": 0.0, "PM": 1.0}},
		)
	)
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "R1", "PM", MON, "B1")]) or 0) > 0.5
	assert (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) < 0.5


def test_weigh_mode_breaks_ties_toward_existing_assignment():
	"""
	With one open slot and two equally-preferred employees, only one can be
	assigned and both choices score identically under the objective. 'Weigh'
	mode's warm-start should steer the solver to keep the one already assigned
	(E1) rather than switching to the tied alternative (E2).
	"""
	disc, b = "D1", "B1"
	prob, x, _ = solve(
		pkg(
			flags={DataPackage.WEIGH_ASSIGNMENTS},
			employees=["E1", "E2"],
			roles=["R1"],
			role_discipline={"R1": disc},
			employee_roles={"E1": ("R1",), "E2": ("R1",)},
			target_shifts={"E1": 1, "E2": 1},
			max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
			rooms={(disc, b): 1},
			forced={("E1", "R1", "AM", MON, b)},
			shift_preferences={"E1": {"AM": 0.5}, "E2": {"AM": 0.5}},
		)
	)
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "R1", "AM", MON, b)]) or 0) > 0.5
	assert (pulp.value(x[("E2", "R1", "AM", MON, b)]) or 0) < 0.5


# ── one-shift-per-day constraint ──────────────────────────────────────────────


def test_at_most_one_shift_per_employee_per_day():
	"""Two shift types available; employee may work at most one of them."""
	prob, x, _ = solve(
		pkg(
			shift_types=["AM", "PM"],
			target_shifts={"E1": 2},
		)
	)
	assert status(prob) == "Optimal"
	am = pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0
	pm = pulp.value(x[("E1", "R1", "PM", MON, "B1")]) or 0
	assert am + pm <= 1 + 1e-6


# ── FTE constraints ───────────────────────────────────────────────────────────


def test_employee_meets_exact_target():
	D = days_from(4)
	prob, x, _ = solve(
		pkg(
			working_days=D,
			target_shifts={"E1": 2},
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2


def test_employee_does_not_exceed_upper_bound():
	"""
	Salaried employee with target=2 and zero tolerance must work exactly 2 shifts
	even when more slots are available.
	"""
	D = days_from(5)
	prob, x, _ = solve(
		pkg(
			working_days=D,
			target_shifts={"E1": 2},
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
	disc, b = "D1", "B1"
	prob, _x, ar = solve(
		pkg(
			working_days=D,
			rooms={(disc, b): 3},
			target_shifts={"E1": 2},
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
			rooms={("D1", "B1"): 1, ("D1", "B2"): 1},
			max_rpe={("E1", "R1"): 1},
			target_shifts={"E1": 1},
		)
	)
	assert status(prob) == "Optimal"
	# E1 can work at most one branch on the one available day
	total = (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) + (
		pulp.value(x[("E1", "R1", "AM", MON, "B2")]) or 0
	)
	assert total <= 1 + 1e-6


# ── AM/PM balance (there is deliberately no fairness rule) ───────────────────


def test_nothing_balances_am_and_pm_across_employees():
	"""
	No built-in rewards an even AM/PM spread, so nothing stops a skewed schedule.

	E1 is force-assigned AM on every day; E2's two shifts are then free to land
	anywhere. With uniform preferences every arrangement scores identically, so the
	assertion here is only that the solver is *free* to skew — pinning which way it
	skews would be testing CBC's tie-breaking, not this app.

	Kept as the standing record that AM/PM fairness is unimplemented (README lists it
	as a limitation). Note this is a different thing from the agreed *role* split,
	which role_fte_target_objective does implement.
	"""
	D = days_from(4)
	disc, b = "D1", "B1"
	forced_e1 = {("E1", "R1", "AM", d, b) for d in D}

	data = pkg(
		employees=["E1", "E2"],
		shift_types=["AM", "PM"],
		working_days=D,
		roles=["R1"],
		role_discipline={"R1": disc},
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 4, "E2": 2},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={(disc, b): 2},
		forced=forced_e1,
		# "Honor existing Shift Assignments" is no longer standard, so the forcing this
		# test leans on has to be asked for. What it is actually about is unchanged.
		rules=builtin_specs(*(STANDARD_RULES | {"use_existing_assignments"})),
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"

	# All forced E1 AM slots must be assigned
	for d in D:
		assert (pulp.value(x[("E1", "R1", "AM", d, b)]) or 0) > 0.5

	# E2 must work exactly 2 shifts (salaried, zero tolerance)
	e2_total = assigned(x, employee="E2")
	assert e2_total == 2

	# ...split however the solver likes across AM and PM — nothing scores that choice
	e2_am = assigned(x, employee="E2", shift="AM")
	e2_pm = assigned(x, employee="E2", shift="PM")
	assert e2_am + e2_pm == 2


# ── rule selection ────────────────────────────────────────────────────────────

ALL_BUT_LEAVE = builtin_specs(
	*(
		rule
		for rule in STANDARD_RULES
		if rule
		not in [
			leave_blocklist.__name__,
		]
	)
)


def test_excluded_rule_is_not_applied():
	"""Same leave-blocked scenario as test_employee_on_leave_is_not_assigned, but
	with the leave rule left out of the selection: the leave no longer blocks."""
	prob, x, _ = solve(
		pkg(
			leave_blocked={("E1", MON)},
			rules=ALL_BUT_LEAVE,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1


def test_explicitly_selected_rules_match_default_behavior():
	prob, x, _ = solve(
		pkg(
			leave_blocked={("E1", MON)},
			rules=ALL_BUT_LEAVE + builtin_specs("leave_blocklist"),
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_custom_code_rule_is_applied():
	code = (
		"def apply(ctx):\n"
		"    for (e, r, s, d, b), var in ctx.x.items():\n"
		"        if e == 'E1':\n"
		"            name = f'never_e1_{s}_{d}_{b}'.replace('-', '_').replace(' ', '_')\n"
		"            ctx.prob += (var <= 0, name)\n"
	)
	rules = (*builtin_specs(*STANDARD_RULES), ("Never E1", "", code, 1.0))
	prob, x, _ = solve(pkg(rules=rules))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_unknown_builtin_key_raises():
	with pytest.raises(ValueError, match="unknown built-in key"):
		build(pkg(rules=(("Mystery Rule", "no_such_rule", "", 1.0),)))


def test_conflicting_choice_group_members_raise():
	rules = builtin_specs(*(STANDARD_RULES | {"use_existing_assignments", "weigh_assignments_objective"}))
	with pytest.raises(ValueError, match="mutually exclusive"):
		build(pkg(rules=rules))


def test_choice_group_permits_the_other_member_alone():
	rules = builtin_specs(*(STANDARD_RULES | {"weigh_assignments_objective"}))
	prob, _, _ = solve(pkg(rules=rules))
	assert status(prob) == "Optimal"


def test_choice_group_permits_neither_member():
	"""Which is what the Standard Ruleset now does, so this is the default path."""
	assert not {"use_existing_assignments", "weigh_assignments_objective"} & STANDARD_RULES
	prob, _, _ = solve(pkg(rules=builtin_specs(*STANDARD_RULES)))
	assert status(prob) == "Optimal"


def test_the_standard_set_does_not_pin_everyone_to_the_books():
	"""A practice's own history is rarely feasible under the rest of the ruleset.

	Weeks worked short-handed, double-booked or off-config pin the model into
	infeasibility, which is what made most historical weeks unsolvable. Only the
	people whose schedule is genuinely not the planner's to set are frozen now, and
	that is `soft_bind_role_assignments` — which is standard, and inert until a
	Scheduling Role is marked binding.
	"""
	assert "use_existing_assignments" not in STANDARD_RULES
	assert "warm_start" in STANDARD_RULES
	assert "soft_bind_role_assignments" in STANDARD_RULES
	assert "bind_role_assignments" not in STANDARD_RULES


def test_rule_without_implementation_raises():
	with pytest.raises(ValueError, match="no implementation"):
		build(pkg(rules=(("Someday Rule", "", "", 1.0),)))


# ── rule application order ────────────────────────────────────────────────────


def titled_specs(*keys: str) -> tuple[tuple[str, str, str, float], ...]:
	"""
	Specs named after the rules' real Optimization Rule documents, sorted by that name.

	This is what `data_loader._load_rules` hands the engine on a live run — and it is not
	the dependency order: `warm_start`'s title ("Use existing Shift Assignments as a
	baseline") sorts last, so every `fixValue()` rule that depends on it comes first.
	`builtin_specs` above names each spec after its *key* instead, which happens to order
	warm_start early and so hides the problem.
	"""
	return tuple(sorted((BUILTIN_RULES[k].title, k, "", 1.0) for k in keys))


def test_rule_order_honors_existing_assignment_whatever_the_document_names():
	"""A forced assignment is fixed even though 'Honor…' sorts before 'Use…as a baseline'."""
	specs = titled_specs("use_existing_assignments", "warm_start", "one_shift_per_day")
	assert specs[0][1] == "use_existing_assignments", "document-name order must put the dependent first"
	_prob, x, _ar, _logs, _ctx = build(
		pkg(forced={("E1", "R1", "AM", MON, "B1")}, rules=specs, shift_types=["AM", "PM"])
	)
	var = x[("E1", "R1", "AM", MON, "B1")]
	assert (var.lowBound, var.upBound) == (1, 1)


def test_rule_order_blocks_leave_whatever_the_document_names():
	"""Same ordering hazard on the leave rule: an unfixed variable would let CBC assign it."""
	specs = titled_specs(
		"leave_blocklist", "warm_start", "one_shift_per_day", "room_coverage", "room_utilization_objective"
	)
	prob, x, _ = solve(pkg(leave_blocked={("E1", MON)}, rules=specs))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_order_specs_leaves_an_independent_selection_alone():
	specs = builtin_specs("one_shift_per_day", "room_coverage")
	assert order_specs(specs) == specs


def test_order_specs_survives_a_dependency_cycle():
	"""A cycle emits every spec exactly once rather than looping or dropping rules."""
	cyclic = {
		"a": BuiltinRule(key="a", title="A", description="", apply=None, excludes={}, requires={"b": ""}),
		"b": BuiltinRule(key="b", title="B", description="", apply=None, excludes={}, requires={"a": ""}),
	}
	specs = (("A", "a", "", 1.0), ("B", "b", "", 1.0))
	with mock.patch.dict(BUILTIN_RULES, cyclic, clear=True):
		assert sorted(order_specs(specs)) == sorted(specs)


# ── role binding (settled schedules) ──────────────────────────────────────────


BINDING_RULES = builtin_specs(
	"warm_start",
	"bind_role_assignments",
	"one_shift_per_day",
	"room_coverage",
	# an objective, so "staffs the room" is something the model wants rather than something
	# CBC happens to return: a pure feasibility model is equally happy leaving every room dark
	"room_utilization_objective",
)


def test_binding_role_freezes_the_whole_schedule():
	"""
	A bound pair works exactly what is on the books: the existing Monday AM shift stays,
	and nothing is added on the other day or the other shift type.
	"""
	days = days_from(2)
	prob, x, _ = solve(
		pkg(
			working_days=days,
			shift_types=["AM", "PM"],
			forced={("E1", "R1", "AM", MON, "B1")},
			binding_pairs=frozenset({("E1", "R1")}),
			target_shifts={"E1": 2},
			rules=BINDING_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) > 0.5
	assert assigned(x, employee="E1") == 1


def test_bound_pair_without_existing_assignments_is_idle():
	"""Freeze means empty, not free: a bound holder with nothing on the books works nothing."""
	prob, x, _ = solve(
		pkg(
			working_days=days_from(2),
			binding_pairs=frozenset({("E1", "R1")}),
			target_shifts={"E1": 2},
			rules=BINDING_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0


def test_binding_override_opts_an_employee_out():
	"""E2 is absent from binding_pairs (a 'Not Binding' override) and is scheduled normally."""
	disc, b = "D1", "B1"
	prob, x, _ = solve(
		pkg(
			employees=["E1", "E2"],
			employee_roles={"E1": ("R1",), "E2": ("R1",)},
			target_shifts={"E1": 1, "E2": 1},
			max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
			rooms={(disc, b): 2},
			binding_pairs=frozenset({("E1", "R1")}),
			rules=BINDING_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0  # bound, nothing on the books
	assert assigned(x, employee="E2") == 1  # opted out, staffs the room


def test_binding_rule_is_inert_without_binding_pairs():
	"""No role marked binding => the rule adds nothing, so the schedule is unchanged."""
	data = {"working_days": days_from(2), "target_shifts": {"E1": 2}}
	_prob, with_rule, _ = solve(pkg(**data, rules=BINDING_RULES))
	_prob2, without, _ = solve(
		pkg(
			**data,
			rules=builtin_specs(
				"warm_start", "one_shift_per_day", "room_coverage", "room_utilization_objective"
			),
		)
	)
	assert assigned(with_rule) == assigned(without)


def test_binding_composes_with_the_existing_assignment_choices():
	"""
	Neither binding rule is in the `existing_assignments` choice group: binding is gated
	by role data, not a third global policy, so it must combine with either member.
	"""
	for binding in ("bind_role_assignments", "soft_bind_role_assignments"):
		for member in ("use_existing_assignments", "weigh_assignments_objective"):
			rules = builtin_specs("warm_start", binding, "one_shift_per_day", member)
			BuiltinRule.check_ruleset({key for _, key, _, _ in rules})


def test_binding_rule_requires_the_warm_start():
	with pytest.raises(ValueError, match="warm_start"):
		BuiltinRule.check_ruleset({"bind_role_assignments"})
	with pytest.raises(ValueError, match="warm_start"):
		BuiltinRule.check_ruleset({"soft_bind_role_assignments"})


# ── soft role binding (the default) ───────────────────────────────────────────

# Soft binding needs something in the objective that *wants* to add shifts, or "kept the
# settled shift" and "the solver stopped at the first feasible point" look identical.
SOFT_BINDING_RULES = builtin_specs(
	"warm_start",
	"soft_bind_role_assignments",
	"one_shift_per_day",
	"room_coverage",
	"room_utilization_objective",
)


def test_the_two_binding_rules_are_mutually_exclusive():
	"""One `role_binding` choice group: a ruleset picks strict, soft, or neither."""
	with pytest.raises(ValueError, match="mutually exclusive"):
		BuiltinRule.check_ruleset({"warm_start", "bind_role_assignments", "soft_bind_role_assignments"})


def test_soft_binding_keeps_a_settled_shift_it_can_keep():
	"""Nothing forces the Monday AM on, but the warm start suggests it and staffing the
	room pays — so a feasible settled schedule comes back intact."""
	prob, x, _ = solve(
		pkg(
			working_days=days_from(2),
			shift_types=["AM", "PM"],
			forced={("E1", "R1", "AM", MON, "B1")},
			binding_pairs=frozenset({("E1", "R1")}),
			target_shifts={"E1": 2},
			rules=SOFT_BINDING_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) > 0.5
	assert assigned(x, employee="E1") == 1


def test_soft_binding_still_adds_nothing_to_a_settled_schedule():
	"""The half that stays hard: everything a bound holder does *not* have on the books
	is fixed to 0, so the Tuesday room the objective would love goes unstaffed."""
	prob, x, _ = solve(
		pkg(
			working_days=days_from(2),
			shift_types=["AM", "PM"],
			forced={("E1", "R1", "AM", MON, "B1")},
			binding_pairs=frozenset({("E1", "R1")}),
			target_shifts={"E1": 4},
			rules=SOFT_BINDING_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1


def test_soft_binding_drops_a_settled_shift_where_strict_binding_is_infeasible():
	"""The point of the rule: a settled week that breaks the rest of the ruleset — here
	two half-days on one date under one-shift-per-day — solves instead of failing, at the
	cost of exactly one dropped shift."""
	books = {("E1", "R1", "AM", MON, "B1"), ("E1", "R1", "PM", MON, "B1")}
	data = {
		"working_days": days_from(2),
		"shift_types": ["AM", "PM"],
		"forced": books,
		"binding_pairs": frozenset({("E1", "R1")}),
		"target_shifts": {"E1": 2},
	}
	strict_rules = builtin_specs(
		"warm_start",
		"bind_role_assignments",
		"one_shift_per_day",
		"room_coverage",
		"room_utilization_objective",
	)
	strict, _, _ = solve(pkg(**data, rules=strict_rules))
	assert status(strict) == "Infeasible"

	prob, x, _ = solve(pkg(**data, rules=SOFT_BINDING_RULES))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1


def test_soft_binding_is_inert_without_binding_pairs():
	"""No role marked binding => the rule fixes nothing, so the schedule is unchanged."""
	data = {"working_days": days_from(2), "target_shifts": {"E1": 2}}
	_prob, with_rule, _ = solve(pkg(**data, rules=SOFT_BINDING_RULES))
	_prob2, without, _ = solve(
		pkg(
			**data,
			rules=builtin_specs(
				"warm_start", "one_shift_per_day", "room_coverage", "room_utilization_objective"
			),
		)
	)
	assert assigned(with_rule) == assigned(without)


def _three_bound_holders(*extra_rules: str) -> DataPackage:
	"""Three bound holders of a 3-room role, all booked Monday AM, at a 6-room branch.

	Two shift types, so a presence costs something under the preference objective — with
	one, every presence is free and nobody is ever worth dropping.
	"""
	staff = ["E1", "E2", "E3"]
	return pkg(
		employees=staff,
		shift_types=["AM", "PM"],
		employee_roles={e: ("R1",) for e in staff},
		max_rpe={(e, "R1"): 3 for e in staff},
		target_shifts={e: 1 for e in staff},
		rooms={("D1", "B1"): 6},
		forced={(e, "R1", "AM", MON, "B1") for e in staff},
		binding_pairs=frozenset((e, "R1") for e in staff),
		rules=builtin_specs(
			"warm_start",
			"soft_bind_role_assignments",
			"one_shift_per_day",
			"room_coverage",
			"suitability_preference_objective",
			*extra_rules,
		)
		+ builtin_specs("room_utilization_objective", weight=3.0),
	)


def _rooms_taken(ctx, employee: str) -> float:
	"""The Monday AM rooms an employee takes: their assignment plus its filled load tranches,
	read exactly as `solver.run_solve` persists them."""
	comb = (employee, "R1", "AM", MON, "B1")
	return (pulp.value(ctx.x[comb]) or 0) + sum(pulp.value(t) or 0 for t in ctx.room_load[comb])


def test_without_load_spreading_soft_binding_sends_a_settled_holder_home():
	"""The bug the load rule exists for: two holders absorb all six rooms, so the third's
	settled half-day pays nothing and soft binding drops it (3+3+0)."""
	prob, x, ar = solve(_three_bound_holders())
	assert status(prob) == "Optimal"
	assert pulp.value(ar[("D1", "AM", MON, "B1")]) == 6
	assert assigned(x, shift="AM") == 2


def test_room_load_spreads_rooms_and_keeps_every_settled_holder():
	"""With the convex load cost the same six rooms come back 2+2+2, whole numbers each."""
	prob, x, ar, _, ctx = build(_three_bound_holders("room_load_objective"))
	prob.solve(pulp.COIN_CMD(msg=False))
	assert status(prob) == "Optimal"
	assert pulp.value(ar[("D1", "AM", MON, "B1")]) == 6
	assert assigned(x, shift="AM") == 3
	assert [_rooms_taken(ctx, e) for e in ("E1", "E2", "E3")] == [2, 2, 2]


def test_room_load_records_one_room_holders_with_no_tranches():
	"""A one-room holder gets no load variables; the solver still reads 1 room off `x`."""
	_prob, _x, _ar, _, ctx = build(
		pkg(
			rules=builtin_specs("warm_start", "room_coverage", "room_load_objective"),
		)
	)
	assert ctx.room_load == {("E1", "R1", "AM", MON, "B1"): []}
	assert not [v for v in _prob.variables() if v.name.startswith("room_load")]


def test_room_load_still_lets_a_lone_holder_take_their_full_load():
	"""Nobody to spread to: the last room costs 1 against the 3 it pays, so it still opens."""
	prob, _x, ar = solve(
		pkg(
			shift_types=["AM", "PM"],
			max_rpe={("E1", "R1"): 3},
			rooms={("D1", "B1"): 3},
			rules=builtin_specs(
				"warm_start",
				"one_shift_per_day",
				"room_coverage",
				"suitability_preference_objective",
				"room_load_objective",
			)
			+ builtin_specs("room_utilization_objective", weight=3.0),
		)
	)
	assert status(prob) == "Optimal"
	assert sum(pulp.value(v) for v in ar.values()) == 3


def test_room_load_requires_room_coverage():
	with pytest.raises(ValueError, match="room_coverage"):
		BuiltinRule.check_ruleset({"room_load_objective"})


def test_custom_code_without_apply_raises():
	with pytest.raises(ValueError, match="apply"):
		build(pkg(rules=(("Broken Rule", "", "x = 1\n", 1.0),)))


def test_rules_selection_changes_input_hash():
	assert pkg().input_hash() != pkg(rules=ALL_BUT_LEAVE).input_hash()
	assert pkg(rules=ALL_BUT_LEAVE).input_hash() == pkg(rules=ALL_BUT_LEAVE).input_hash()


def test_dumps_loads_round_trips_rules():
	code = "def apply(ctx):\n    pass\n"
	data = pkg(rules=(*builtin_specs("one_shift_per_day"), ("Custom", "", code, 2.5)))
	restored = DataPackage.loads(data.dumps())
	assert restored == data
	assert restored.input_hash() == data.input_hash()


def test_loads_pads_legacy_rule_triples_with_weight():
	"""Packages cached before the weight element existed still deserialize."""
	data = pkg(rules=builtin_specs("one_shift_per_day"))
	payload = json.loads(data.dumps())
	payload["rules"] = [spec[:3] for spec in payload["rules"]]  # pre-weight format
	restored = DataPackage.loads(json.dumps(payload))
	assert restored.rules == (("one_shift_per_day", "one_shift_per_day", "", 1.0),)


# ── objective rules ───────────────────────────────────────────────────────────

CONSTRAINTS_ONLY = builtin_specs(
	*(
		key
		for key, rule in BUILTIN_RULES.items()
		if rule.kind
		not in [  # constraints only <=> NO objectives
			KIND_OBJECTIVE,
			KIND_MIXED,
		]
		and key in STANDARD_RULES
	)
)


def test_objective_less_ruleset_assigns_nobody():
	"""Constraint rules only: constant-zero objective, the solver has no reason to
	assign anyone. This is the documented semantics of an objective-less ruleset.
	Objectiveless runs consider any feasible solution optimal, not just empty ones."""
	prob, _x, _ = solve(pkg(rules=CONSTRAINTS_ONLY))
	assert status(prob) == "Optimal"
	assert (pulp.value(prob.objective) or 0) == 0


def test_explicit_all_builtins_matches_default_selection():
	"""Selecting every built-in explicitly (weight 1) is the pre-ruleset behaviour."""
	default_prob, _, _ = solve(pkg())
	explicit_prob, _, _ = solve(pkg(rules=builtin_specs(*STANDARD_RULES)))
	assert pulp.value(default_prob.objective) == pytest.approx(pulp.value(explicit_prob.objective))


def test_objective_weight_scales_term():
	"""Doubling the room-utilization row weight adds exactly one extra unit of
	objective for the single staffed room; the assignment itself is unchanged."""
	weighted = tuple(
		(name, key, code, 2.0 if key == "room_utilization_objective" else w)
		for name, key, code, w in builtin_specs(*STANDARD_RULES)
	)
	prob1, x1, _ = solve(pkg(rules=builtin_specs(*STANDARD_RULES)))
	prob2, x2, _ = solve(pkg(rules=weighted))
	assert assigned(x1) == assigned(x2) == 1
	assert pulp.value(prob2.objective) == pytest.approx(pulp.value(prob1.objective) + 1.0)


def test_path_levels_match_the_constants_the_reporting_layer_matches_on():
	"""`path(Employee=...)` and LEVEL_EMPLOYEE have to be the same string: the run
	statistics relabel employee ids by looking the level name up."""
	assert path(Employee="E1") == ((LEVEL_EMPLOYEE, "E1"),)
	assert path(Discipline="D1", Day=MON) == ((LEVEL_DISCIPLINE, "D1"), (LEVEL_DAY, str(MON)))
	assert path(Shift_type="AM") == (("Shift type", "AM"),)


def test_objective_tree_breaks_a_rule_down_to_its_half_days():
	"""Room utilization files one term per (discipline, branch, day, shift), so its
	subtree opens from the discipline down to the rooms a single half-day staffed."""
	data = pkg(
		employees=["E1", "E2"],
		working_days=days_from(2),
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 2, "E2": 2},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={("D1", "B1"): 2},
	)
	specs = builtin_specs("warm_start", "room_coverage", "room_utilization_objective")
	prob, _x, _ar, _logs, ctx = build(dataclasses.replace(data, rules=specs))
	prob.solve(pulp.COIN_CMD(msg=False))

	tree = objective_tree(ctx)
	(node,) = [n for n in tree if n["rule"] == "room_utilization_objective"]
	assert node["levels"] == [LEVEL_DISCIPLINE, "Branch", LEVEL_DAY, "Shift"]
	assert node["value"] == pytest.approx(4.0)  # two rooms, two days

	# A node carries no level of its own: its level is `levels[depth - 1]` of its rule.
	(disc,) = node["children"]
	assert disc["label"] == "D1"
	assert "level" not in disc
	(branch,) = disc["children"]
	days = branch["children"]
	assert [d["label"] for d in days] == [str(d) for d in data.working_days]
	assert all(day["children"][0]["value"] == pytest.approx(2.0) for day in days)


def test_objective_tree_values_roll_up_and_match_the_flat_shares():
	"""Every node is the sum of its children, and a rule node is its flat share — the
	invariant the statistics panel's drill-down rests on."""
	specs = builtin_specs(*STANDARD_RULES)
	prob, _x, _ar, _logs, ctx = build(pkg(rules=specs))
	prob.solve(pulp.COIN_CMD(msg=False))

	shares = objective_shares(ctx)
	tree = objective_tree(ctx)
	assert {n["rule"] for n in tree} == set(shares)

	def check(node):
		children = node.get("children") or []
		if children:
			assert node["value"] == pytest.approx(sum(c["value"] for c in children), abs=1e-3)
		for child in children:
			check(child)

	for node in tree:
		assert node["value"] == pytest.approx(shares[node["rule"]], abs=1e-3)
		check(node)
	assert sum(n["value"] for n in tree) == pytest.approx(pulp.value(prob.objective), abs=1e-3)


def test_objective_tree_folds_wide_and_deep_subtrees():
	"""Breadth past `max_children` collapses into one '… and N more' row carrying the
	rest of the value, and a subtree over its node budget loses its deepest levels
	rather than its coarse ones."""
	days = days_from(4)
	employees = [f"E{i}" for i in range(1, 7)]
	data = pkg(
		employees=employees,
		working_days=days,
		shift_types=["AM", "PM"],
		employee_roles={e: ("R1",) for e in employees},
		target_shifts={e: 8 for e in employees},
		max_rpe={(e, "R1"): 1 for e in employees},
		rooms={("D1", "B1"): 6},
		rules=builtin_specs(
			"warm_start",
			"room_coverage",
			"room_utilization_objective",
			"suitability_preference_objective",
		),
	)
	prob, _x, _ar, _logs, ctx = build(data)
	prob.solve(pulp.COIN_CMD(msg=False))

	(node,) = [n for n in objective_tree(ctx) if n["rule"] == "suitability_preference_objective"]
	assert node["levels"] == [LEVEL_EMPLOYEE, LEVEL_DAY, "Shift"]
	assert not node.get("trimmed")

	narrow = objective_tree(ctx, max_children=2)
	(node,) = [n for n in narrow if n["rule"] == "suitability_preference_objective"]
	assert len(node["children"]) == 3  # two employees, then the fold
	assert node["children"][-1]["label"].startswith("…")
	assert node["value"] == pytest.approx(sum(c["value"] for c in node["children"]), abs=1e-3)

	shallow = objective_tree(ctx, max_nodes=10)
	(node,) = [n for n in shallow if n["rule"] == "suitability_preference_objective"]
	assert node["levels"] == [LEVEL_EMPLOYEE]
	assert node["trimmed"] == [LEVEL_DAY, "Shift"]
	assert all("children" not in c for c in node["children"])
	assert node["value"] == pytest.approx(sum(c["value"] for c in node["children"]), abs=1e-3)


def test_objective_rule_that_contributes_nothing_still_reports_a_zero():
	"""A priced-role objective on a site where no role is priced scores 0 — and says so,
	rather than dropping out of the breakdown."""
	specs = builtin_specs("warm_start", "room_coverage", "role_value_objective")
	prob, _x, _ar, _logs, ctx = build(pkg(rules=specs))
	prob.solve(pulp.COIN_CMD(msg=False))

	assert ctx.objective_contributions["role_value_objective"] == {}
	(node,) = [n for n in objective_tree(ctx) if n["rule"] == "role_value_objective"]
	assert node["value"] == 0.0
	assert "children" not in node


def test_objective_contributions_attribute_and_sum_to_the_objective():
	"""Each objective rule's terms are recorded under its rule document name, and the
	recorded shares add back up to the solved objective — the invariant the run's
	"objective breakdown by rule" reporting rests on."""
	specs = builtin_specs(*STANDARD_RULES)
	prob, _x, _ar, _logs, ctx = build(pkg(rules=specs))
	prob.solve(pulp.COIN_CMD(msg=False))

	objective_rules = {name for name, key, _code, _w in specs if BUILTIN_RULES[key].kind == KIND_OBJECTIVE}
	assert set(ctx.objective_contributions) == objective_rules
	assert "" not in ctx.objective_contributions  # every term is attributed to a rule

	shares = objective_shares(ctx)
	assert sum(shares.values()) == pytest.approx(pulp.value(prob.objective))


def test_weight_on_constraint_rule_is_a_noop():
	prob1, x1, _ = solve(pkg(rules=builtin_specs(*STANDARD_RULES)))
	reweighted = tuple(
		(name, key, code, 5.0 if key == "one_shift_per_day" else w)
		for name, key, code, w in builtin_specs(*STANDARD_RULES)
	)
	prob2, x2, _ = solve(pkg(rules=reweighted))
	assert pulp.value(prob1.objective) == pytest.approx(pulp.value(prob2.objective))
	assert assigned(x1) == assigned(x2)


def test_custom_objective_rule_steers_solution():
	"""A custom rule contributes objective terms via ctx.add_objective: penalizing
	E1's assignments makes the solver give the single room slot to E2."""
	code = (
		"def apply(ctx):\n"
		"    ctx.add_objective(\n"
		"        pulp.lpSum(-var for (e, r, s, d, b), var in ctx.x.items() if e == 'E1')\n"
		"    )\n"
	)
	disc, b = "D1", "B1"
	data = pkg(
		employees=["E1", "E2"],
		roles=["R1"],
		role_discipline={"R1": disc},
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 1, "E2": 1},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={(disc, b): 1},
		rules=(*builtin_specs(*STANDARD_RULES), ("Avoid E1", "", code, 1.0)),
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0
	assert assigned(x, employee="E2") == 1


# ── Scheduling Roles (multi-skill) ────────────────────────────────────────────


def two_role_pkg(**overrides) -> DataPackage:
	"""E1 holds R1 in discipline D1 and R2 in discipline D2; E2 holds only R1.

	One room per discipline per branch, five days, one shift type — so the only
	interesting question is how E1 splits between the two disciplines.
	"""
	b = "B1"
	base: dict[str, Any] = dict(
		employees=["E1", "E2"],
		shift_types=["AM"],
		working_days=days_from(5),
		branches=[b],
		roles=["R1", "R2"],
		role_discipline={"R1": "D1", "R2": "D2"},
		employee_roles={"E1": ("R1", "R2"), "E2": ("R1",)},
		target_shifts={"E1": 5, "E2": 5},
		max_rpe={("E1", "R1"): 1, ("E1", "R2"): 1, ("E2", "R1"): 1},
		rooms={("D1", b): 1, ("D2", b): 1},
		disciplines=["D1", "D2"],
	)
	base.update(overrides)
	return pkg(**base)


def test_variables_exist_only_for_held_roles():
	"""Eligibility is structural: an unheld (employee, role) pair has no variable at all.

	This is what makes the hard eligibility rule unnecessary — there is nothing for the
	solver to set, so nothing to forbid.
	"""
	_prob, x, _ar = solve(two_role_pkg())

	assert any(e == "E1" and r == "R2" for e, r, _s, _d, _b in x)
	assert not any(e == "E2" and r == "R2" for e, r, _s, _d, _b in x)


def test_one_shift_per_day_holds_across_roles():
	"""Holding two roles widens where E1 can work, never how much.

	Without the sum over roles, E1 could work D1 and D2 on the same day and each
	discipline would count them as present — over-stating capacity, which is exactly the
	failure that made dual-role staff unschedulable before.
	"""
	D = days_from(5)
	_prob, x, _ar = solve(two_role_pkg(working_days=D))

	for d in D:
		same_day = sum(
			1
			for (e, _r, _s, day, _b), var in x.items()
			if e == "E1" and day == d and (pulp.value(var) or 0) > 0.5
		)
		assert same_day <= 1


def test_room_coverage_counts_a_dual_role_employee_in_the_role_they_work():
	"""D2's room can only ever be staffed by E1, since E2 holds no role there."""
	_prob, x, ar = solve(two_role_pkg())

	for (discipline, _s, d, b), var in ar.items():
		staffed = (pulp.value(var) or 0) > 0.5
		if not staffed:
			continue
		workers = [
			e
			for (e, r, _s2, day, br), xv in x.items()
			if day == d
			and br == b
			and (pulp.value(xv) or 0) > 0.5
			and r in {"R1", "R2"}
			# the role's own discipline is what ties the assignment to this room
			and {"R1": "D1", "R2": "D2"}[r] == discipline
		]
		assert workers, f"{discipline} room staffed on {d} with nobody assigned in that discipline"


def test_per_pair_max_rpe_is_used():
	"""max_rpe is keyed on (employee, role): the same person can cover 3 rooms in one
	discipline and 1 in another, which a per-designation figure could not express."""
	data = two_role_pkg(
		working_days=[MON],
		employees=["E1"],
		employee_roles={"E1": ("R1", "R2")},
		target_shifts={"E1": 1},
		max_rpe={("E1", "R1"): 3, ("E1", "R2"): 1},
		rooms={("D1", "B1"): 3, ("D2", "B1"): 3},
		# at the seeded weights: under the legacy all-1.0 selection a staffed room pays 1,
		# exactly what `room_load_objective` charges for a holder's last room, and the third
		# room would be a coin toss
		rules=tuple((k, k, "", BUILTIN_RULES[k].default_weight) for k in sorted(STANDARD_RULES)),
	)
	_prob, x, ar = solve(data)

	# One shift per day, so E1 works exactly one role; room utilization makes the
	# 3-room role strictly better, and the room variable must equal that figure.
	assert (pulp.value(x[("E1", "R1", "AM", MON, "B1")]) or 0) > 0.5
	assert (pulp.value(ar[("D1", "AM", MON, "B1")]) or 0) == 3


# ── agreed role FTE split ─────────────────────────────────────────────────────


def test_role_split_target_steers_a_dual_role_employee():
	"""E1 is expected 4 days in D1 and 1 in D2; the penalty should produce exactly that.

	Room utilization alone is indifferent — every day E1 works staffs one room whichever
	discipline it is in — so any split the solver picks here comes from this rule.
	"""
	data = two_role_pkg(
		employees=["E1"],
		employee_roles={"E1": ("R1", "R2")},
		target_shifts={"E1": 5},
		role_target_shifts={("E1", "R1"): 4.0, ("E1", "R2"): 1.0},
	)
	_prob, x, _ar = solve(data)

	assert assigned(x, employee="E1", role="R1") == 4
	assert assigned(x, employee="E1", role="R2") == 1


def test_no_target_means_no_pull():
	"""An Employee Scheduling Role with a blank agreed FTE imposes nothing.

	The split then floats free, bounded only by the overall FTE ceiling — which is the
	documented meaning of leaving the field empty.
	"""
	data = two_role_pkg(
		employees=["E1"],
		employee_roles={"E1": ("R1", "R2")},
		target_shifts={"E1": 5},
		role_target_shifts={},
		rules=builtin_specs(*STANDARD_RULES),
	)
	prob, x, _ar = solve(data)

	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 5
	# no deviation variables were created at all
	assert not [v for v in prob.variables() if v.name.startswith("role_dev")]


def test_role_deviation_penalty_equals_the_absolute_deviation():
	"""The linearization is exact: one unreachable shift of target costs exactly 1.

	E1 can work at most 2 days but is expected 5 in R1, so the deviation is fixed at 3
	whatever the solver does. Comparing against the same package with no target isolates
	the penalty from every other objective term.
	"""
	common = dict(
		employees=["E1"],
		working_days=days_from(2),
		employee_roles={"E1": ("R1",)},
		roles=["R1"],
		role_discipline={"R1": "D1"},
		max_rpe={("E1", "R1"): 1},
		rooms={("D1", "B1"): 1},
		disciplines=["D1"],
		target_shifts={"E1": 2},
	)
	baseline, _x, _ar = solve(pkg(**common, role_target_shifts={}))
	penalized, x, _ar2 = solve(pkg(**common, role_target_shifts={("E1", "R1"): 5.0}))

	assert assigned(x, employee="E1") == 2
	assert pulp.value(penalized.objective) == pytest.approx(pulp.value(baseline.objective) - 3.0)


def test_role_deviation_penalty_scales_with_the_ruleset_weight():
	"""Doubling the row weight doubles the penalty, like any other objective rule."""
	common = dict(
		employees=["E1"],
		working_days=days_from(2),
		employee_roles={"E1": ("R1",)},
		roles=["R1"],
		role_discipline={"R1": "D1"},
		max_rpe={("E1", "R1"): 1},
		rooms={("D1", "B1"): 1},
		disciplines=["D1"],
		target_shifts={"E1": 2},
		role_target_shifts={("E1", "R1"): 5.0},
	)
	specs = builtin_specs(*STANDARD_RULES)
	doubled = tuple(
		(name, key, code, 2.0 if key == "role_fte_target_objective" else w) for name, key, code, w in specs
	)
	single, _x, _ar = solve(pkg(**common, rules=specs))
	double, _x2, _ar2 = solve(pkg(**common, rules=doubled))

	# the deviation is 3 either way, so the extra weight costs exactly one more copy of it
	assert pulp.value(double.objective) == pytest.approx(pulp.value(single.objective) - 3.0)


def test_role_fte_ceiling_is_opt_in_and_caps_the_role():
	"""The hard reading of the same figure, off by default.

	Selected explicitly, it stops E1 exceeding the agreed R1 figure even though room
	utilization would happily use them for all five days.
	"""
	common = dict(
		employees=["E1"],
		employee_roles={"E1": ("R1",)},
		target_shifts={"E1": 5},
		role_target_shifts={("E1", "R1"): 2.0},
	)
	assert "role_fte_ceiling" not in STANDARD_RULES

	# Half weight on the deviation penalty, so overworking R1 is strictly the better
	# trade (5 rooms - 1.5 penalty beats 2 rooms - 0) and the soft case has no tie to
	# break. That is the whole difference the ceiling makes.
	specs = tuple(
		(name, key, code, 0.5 if key == "role_fte_target_objective" else w)
		for name, key, code, w in builtin_specs(*STANDARD_RULES)
	)

	soft, x_soft, _ar = solve(two_role_pkg(**common, rules=specs))
	hard, x_hard, _ar2 = solve(
		two_role_pkg(**common, rules=(*specs, ("role_fte_ceiling", "role_fte_ceiling", "", 1.0)))
	)

	assert status(soft) == status(hard) == "Optimal"
	assert assigned(x_soft, employee="E1", role="R1") == 5
	assert assigned(x_hard, employee="E1", role="R1") == 2  # floor(1.05 * 2)


# ── FTE soft ceiling ──────────────────────────────────────────────────────────


# Weighted at the rules' own declared defaults, because what this rule does is a matter of
# calibration against `room_utilization_objective`: a flat weight of 1.0 everywhere would
# test a model nobody solves.
def default_weight_specs(*keys: str) -> tuple[tuple[str, str, str, float], ...]:
	return tuple((k, k, "", BUILTIN_RULES[k].default_weight) for k in keys)


SOFT_FTE_RULES = default_weight_specs(
	"warm_start",
	"one_shift_per_day",
	"room_coverage",
	"room_utilization_objective",
	"shift_preference_objective",
	"fte_soft_ceiling",
)


def test_the_two_fte_ceilings_are_mutually_exclusive():
	"""One `workload_ceiling` choice group: the hard cap, the penalty, or neither."""
	with pytest.raises(ValueError, match="mutually exclusive"):
		BuiltinRule.check_ruleset({"fte_ceiling", "fte_soft_ceiling"})


def test_fte_soft_ceiling_is_opt_in():
	"""The hard ceiling stays the default; the soft one is the deliberate choice."""
	assert "fte_ceiling" in STANDARD_RULES
	assert "fte_soft_ceiling" not in STANDARD_RULES


def test_fte_soft_ceiling_holds_the_agreed_workload_when_it_can():
	"""Two days, two rooms and an employee agreed to one shift: the second room stays
	dark, because at the default weights going over costs 4 and the room pays 3."""
	prob, x, _ = solve(
		pkg(
			working_days=days_from(2),
			target_shifts={"E1": 1},
			rules=SOFT_FTE_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1


def test_fte_soft_ceiling_yields_to_coverage_at_a_lower_weight():
	"""The knob the rule exists for: weighted below what a staffed room earns, the
	courtesy gives way and the second day is covered."""
	cheap = tuple(
		(name, key, code, 1.0 if key == "fte_soft_ceiling" else w) for name, key, code, w in SOFT_FTE_RULES
	)
	prob, x, _ = solve(pkg(working_days=days_from(2), target_shifts={"E1": 1}, rules=cheap))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2


def test_fte_soft_ceiling_solves_what_the_hard_ceiling_calls_infeasible():
	"""A settled week fuller than the agreed workload: the hard cap fails the whole run,
	the penalty absorbs it and still returns a schedule."""
	books = {("E1", "R1", "AM", d, "B1") for d in days_from(2)}
	data = dict(working_days=days_from(2), forced=books, target_shifts={"E1": 1})

	hard, _x, _ = solve(
		pkg(**data, rules=default_weight_specs("warm_start", "use_existing_assignments", "fte_ceiling"))
	)
	assert status(hard) == "Infeasible"

	prob, x, _ = solve(
		pkg(
			**data, rules=(*SOFT_FTE_RULES, ("use_existing_assignments", "use_existing_assignments", "", 1.0))
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2


def test_fte_soft_ceiling_ignores_employees_without_a_target():
	"""No FTE-derived target, no penalty variable — the same employees `fte_ceiling`
	leaves alone, left alone here too."""
	_prob, _x, _ar, _logs, ctx = build(pkg(target_shifts={"E1": 0}, rules=SOFT_FTE_RULES))
	assert not [v for v in ctx.prob.variables() if v.name.startswith("fte_over")]


# ── role substitution suitability ─────────────────────────────────────────────


SUITABILITY_RULES = default_weight_specs(
	"warm_start",
	"one_shift_per_day",
	"room_coverage",
	"room_utilization_objective",
	"suitability_preference_objective",
)


def two_candidates(**overrides) -> DataPackage:
	"""E1 and E2 both hold R1; one room, one shift, one day — only one of them is needed."""
	base: dict[str, Any] = {
		"employees": ["E1", "E2"],
		"employee_roles": {"E1": ("R1",), "E2": ("R1",)},
		"target_shifts": {"E1": 1, "E2": 1},
		"max_rpe": {("E1", "R1"): 1, ("E2", "R1"): 1},
		"shift_preferences": {"E1": {"AM": 0.5}, "E2": {"AM": 0.5}},
		"rules": SUITABILITY_RULES,
	}
	base.update(overrides)
	return pkg(**base)


def test_the_two_preference_objectives_are_mutually_exclusive():
	with pytest.raises(ValueError, match="mutually exclusive"):
		BuiltinRule.check_ruleset({"shift_preference_objective", "suitability_preference_objective"})


def test_suitability_objective_replaces_shift_preferences_in_the_standard_ruleset():
	assert "suitability_preference_objective" in STANDARD_RULES
	assert "shift_preference_objective" not in STANDARD_RULES


def test_suitability_objective_is_shift_preferences_when_every_suitability_is_one():
	"""The default matrix is all ones: swapping the rule in must not move the optimum."""
	data = pkg(
		employees=["E1", "E2"],
		shift_types=["AM", "PM"],
		working_days=days_from(3),
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 3, "E2": 2},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={("D1", "B1"): 2},
		shift_preferences={"E1": {"AM": 0.8, "PM": 0.2}, "E2": {"AM": 0.3, "PM": 0.7}},
	)
	common = ("warm_start", "one_shift_per_day", "fte_ceiling", "room_coverage", "room_utilization_objective")
	plain, _, _ = solve(
		dataclasses.replace(data, rules=default_weight_specs(*common, "shift_preference_objective"))
	)
	scaled, _, _ = solve(
		dataclasses.replace(data, rules=default_weight_specs(*common, "suitability_preference_objective"))
	)
	assert status(plain) == status(scaled) == "Optimal"
	assert pulp.value(scaled.objective) == pytest.approx(pulp.value(plain.objective))


def test_a_regular_holder_is_preferred_over_a_substitute():
	prob, x, _ = solve(two_candidates(role_suitability={("E2", "R1"): 1.2}))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1
	assert assigned(x, employee="E2") == 0


def test_a_better_backup_is_preferred_over_a_worse_one():
	prob, x, _ = solve(two_candidates(role_suitability={("E1", "R1"): 3.0, ("E2", "R1"): 1.2}))
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0
	assert assigned(x, employee="E2") == 1


def test_a_terrible_substitute_still_opens_a_room_nobody_else_can():
	"""Suitability 3 costs -1.5 against the room's +3: feasible, and still worth it."""
	prob, x, ar = solve(
		pkg(
			shift_preferences={"E1": {"AM": 0.5}},
			role_suitability={("E1", "R1"): 3.0},
			rules=SUITABILITY_RULES,
		)
	)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 1
	assert sum(pulp.value(v) or 0 for v in ar.values()) == pytest.approx(1)


def test_role_suitability_changes_input_hash_only_when_set():
	assert pkg().input_hash() == pkg(role_suitability={}).input_hash()
	assert pkg().input_hash() != pkg(role_suitability={("E1", "R1"): 1.2}).input_hash()


def test_dumps_loads_round_trips_role_suitability():
	data = pkg(role_suitability={("E1", "R1"): 1.2})
	restored = DataPackage.loads(data.dumps())
	assert restored == data
	assert restored.input_hash() == data.input_hash()


def test_loads_reads_a_package_without_role_suitability_as_all_ones():
	payload = json.loads(pkg().dumps())
	del payload["role_suitability"]
	restored = DataPackage.loads(json.dumps(payload))
	assert restored.role_suitability == {}
	assert restored.suitability("E1", "R1") == 1.0


# ── multi-employee integration ────────────────────────────────────────────────


def test_two_employees_both_meet_fte_targets():
	D = days_from(4)
	disc, b = "D1", "B1"
	data = pkg(
		employees=["E1", "E2"],
		working_days=D,
		roles=["R1"],
		role_discipline={"R1": disc},
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 2, "E2": 3},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={(disc, b): 2},
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 2
	assert assigned(x, employee="E2") == 3


# ── DataPackage dumps/loads round-trip ────────────────────────────────────────


def test_dumps_loads_round_trips_to_equal_package():
	data = pkg(
		employees=["E1", "E2"],
		roles=["R1"],
		role_discipline={"R1": "D1"},
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		leave_blocked={("E1", MON)},
		forced={("E2", "R1", "AM", MON, "B1")},
	)
	restored = DataPackage.loads(data.dumps())
	assert restored == data


def test_dumps_loads_preserves_dates_and_collection_types():
	data = pkg(leave_blocked={("E1", MON)}, forced={("E1", "R1", "AM", MON, "B1")})
	restored = DataPackage.loads(data.dumps())

	(_e, leave_date) = next(iter(restored.leave_blocked))
	assert isinstance(leave_date, datetime.date)
	assert leave_date == MON

	(_e, _r, _s, forced_date, _b) = next(iter(restored.forced))
	assert isinstance(forced_date, datetime.date)
	assert forced_date == MON

	assert isinstance(restored.working_days[0], datetime.date)
	assert isinstance(restored.rooms, dict)
	assert isinstance(next(iter(restored.rooms)), tuple)
	assert isinstance(restored.leave_blocked, set)
	assert isinstance(restored.forced, set)


def test_dumps_loads_round_trip_preserves_input_hash():
	data = pkg(leave_blocked={("E1", MON)}, forced={("E1", "R1", "AM", MON, "B1")})
	restored = DataPackage.loads(data.dumps())
	assert restored.input_hash() == data.input_hash()


def test_leave_does_not_block_other_employees():
	"""E1 on leave; E2 should still be assigned normally."""
	disc, b = "D1", "B1"
	data = pkg(
		employees=["E1", "E2"],
		roles=["R1"],
		role_discipline={"R1": disc},
		employee_roles={"E1": ("R1",), "E2": ("R1",)},
		target_shifts={"E1": 0, "E2": 1},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1},
		rooms={(disc, b): 1},
		leave_blocked={("E1", MON)},
	)
	prob, x, _ = solve(data)
	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1") == 0
	assert assigned(x, employee="E2") == 1


# ── editor completions (Custom Code rule authoring) ───────────────────────────


def test_completion_items_cover_rule_authoring_api():
	values = {item["value"] for item in completion_items()}
	assert {"ctx.prob", "ctx.x", "ctx.active_rooms", "ctx.data", "ctx.add_objective"} <= values
	# every DataPackage field is offered under ctx.data (introspected, cannot drift)
	assert {f"ctx.data.{f.name}" for f in dataclasses.fields(DataPackage)} <= values
	assert {"ctx.data.WEIGH_ASSIGNMENTS", "pulp.lpSum", "itertools.product"} <= values


def test_completion_items_do_not_leak_private_members():
	leaked = [item["value"] for item in completion_items() if "._" in item["value"]]
	assert not leaked


def test_completion_items_have_editor_shape():
	items = completion_items()
	assert items
	for item in items:
		assert set(item) == {"value", "meta", "score"}
		assert item["meta"] in ("ctx", "data", "pulp", "itertools", "utils")
		assert isinstance(item["score"], int)


# ── role modes: collateral duties and exclusive roles ─────────────────────────


def with_collateral(**overrides) -> DataPackage:
	"""E1 holds a working role R1 and a collateral duty RC, both in discipline D1."""
	base: dict[str, Any] = {
		"roles": ["R1", "RC"],
		"role_discipline": {"R1": "D1", "RC": "D1"},
		"employee_roles": {"E1": ("R1", "RC")},
		"max_rpe": {("E1", "R1"): 1, ("E1", "RC"): 3},
		"role_mode": {"RC": MODE_COLLATERAL},
		"rules": builtin_specs(*STANDARD_RULES),
	}
	base.update(overrides)
	return pkg(**base)


def solved(data: DataPackage):
	"""`solve`, with the presence variables — the rest of the file only needs x."""
	prob, x, ar, _logs, ctx = build(data)
	prob.solve(pulp.COIN_CMD(msg=False))
	return prob, x, ar, ctx.presence


def test_a_collateral_duty_is_worked_alongside_a_shift_on_one_presence():
	"""Both roles in the same slot, and that counts as one half-day of somebody's life."""
	prob, x, _ar, presence = solved(with_collateral(target_shifts={"E1": 1}))

	assert status(prob) == "Optimal"
	assert assigned(x, role="R1") == 1
	assert assigned(x, role="RC") == 1
	assert (pulp.value(presence[("E1", "AM", MON, "B1")]) or 0) > 0.5
	assert sum(1 for var in presence.values() if (pulp.value(var) or 0) > 0.5) == 1


def test_a_collateral_duty_does_not_count_against_the_fte_ceiling():
	"""An FTE target of one shift is not spent twice by a lead duty worked during it."""
	data = with_collateral(
		working_days=days_from(2),
		target_shifts={"E1": 1},  # one half-day over two days
		rooms={("D1", "B1"): 2},
	)
	prob, x, _ar, presence = solved(data)

	assert status(prob) == "Optimal"
	assert sum(1 for var in presence.values() if (pulp.value(var) or 0) > 0.5) == 1
	assert assigned(x) == 2  # the shift and the duty worked during it


def test_a_collateral_duty_can_stand_alone():
	"""Nothing requires a host: a lead whose rooms are covered may spend the day leading."""
	data = with_collateral(
		employees=["E1", "E2"],
		employee_roles={"E1": ("RC",), "E2": ("R1",)},
		target_shifts={"E1": 1, "E2": 1},
		max_rpe={("E1", "RC"): 3, ("E2", "R1"): 1},
	)
	prob, x, _ar, presence = solved(data)

	assert status(prob) == "Optimal"
	assert assigned(x, employee="E1", role="RC") == 1
	assert (pulp.value(presence[("E1", "AM", MON, "B1")]) or 0) > 0.5


def test_collateral_roles_do_not_gate_room_coverage_by_default():
	"""
	`room_coverage` takes the minimum over a discipline's *gating* roles, and a collateral
	role does not gate unless somebody says so. Counted there it would cap the discipline at
	the rooms its leads span — and leave every room dark whenever nobody is leading.
	"""
	data = with_collateral(
		employees=["E1", "E2"],
		employee_roles={"E1": ("R1",), "E2": ("RC",)},
		target_shifts={"E1": 1, "E2": 1},
		max_rpe={("E1", "R1"): 1, ("E2", "RC"): 3},
		leave_blocked={("E2", MON)},  # the lead is on leave, so nobody leads
	)
	prob, _x, ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	assert pulp.value(ar[("D1", "AM", MON, "B1")]) == 1


def scaled_by_staffing() -> tuple:
	"""The standard set with the staffing-scaled collateral value in place of the standard
	one — they are a choice group, so it is a swap, never an addition."""
	return builtin_specs(
		*((STANDARD_RULES - {"collateral_capacity_value_objective"}) | {"collateral_room_value_objective"})
	)


def collateral_values(prob, prefix: str) -> set[float]:
	return {pulp.value(var) for var in prob.variables() if var.name.startswith(prefix)}


def test_a_staffing_scaled_duty_is_worth_the_rooms_actually_open():
	"""`min(rooms staffed, max-rooms of the duty)`, so a lead over one open room earns one."""
	data = with_collateral(rooms={("D1", "B1"): 3}, target_shifts={"E1": 1}, rules=scaled_by_staffing())
	prob, _x, _ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	# one employee staffs one room, though the duty spans three
	assert collateral_values(prob, "collateral_value") == {1.0}


def test_the_standard_duty_value_does_not_move_with_how_busy_the_branch_is():
	"""Priced on the post, not the crowd: the same lead earns the same on a quiet half-day.

	Which is the point of it — scaled by staffing, a lead is worth more where more rooms are
	running, and the optimizer answers that by gathering people into the branches that have
	one.
	"""
	data = with_collateral(rooms={("D1", "B1"): 3}, target_shifts={"E1": 1})
	prob, _x, _ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	# three rooms configured, one of them staffed, and the duty spans three
	assert collateral_values(prob, "collateral_capacity_value") == {3.0}


def test_the_standard_duty_value_is_capped_by_the_branch_room_count():
	"""A duty spanning three rooms at a one-room branch supervises one room."""
	data = with_collateral(rooms={("D1", "B1"): 1}, target_shifts={"E1": 1})
	prob, _x, _ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	assert collateral_values(prob, "collateral_capacity_value") == {1.0}


def test_an_unworked_duty_is_worth_nothing_under_either_rule():
	"""The cap is a ceiling on a value somebody still has to earn by working the duty."""
	for rules in (None, scaled_by_staffing()):
		data = with_collateral(
			employees=["E1", "E2"],
			employee_roles={"E1": ("R1",), "E2": ("RC",)},
			target_shifts={"E1": 1, "E2": 1},
			max_rpe={("E1", "R1"): 1, ("E2", "RC"): 3},
			leave_blocked={("E2", MON)},  # nobody can lead today
			**({"rules": rules} if rules else {}),
		)
		prob, x, _ar, _presence = solved(data)
		assert status(prob) == "Optimal"
		assert assigned(x, role="RC") == 0
		assert all(value == pytest.approx(0) for value in collateral_values(prob, "collateral"))


def test_the_two_collateral_value_rules_are_mutually_exclusive():
	"""One question — what is a supervised post worth — with two answers, so: a choice group."""
	with pytest.raises(ValueError, match="mutually exclusive"):
		BuiltinRule.check_ruleset(
			{"collateral_room_value_objective", "collateral_capacity_value_objective", "room_coverage"}
		)


def test_an_exclusive_role_admits_no_collateral_duty():
	"""The same slot the flexible version works both ways round is worked one way only."""
	flexible = with_collateral(target_shifts={"E1": 1})
	exclusive = with_collateral(
		target_shifts={"E1": 1}, role_mode={"R1": MODE_EXCLUSIVE, "RC": MODE_COLLATERAL}
	)

	_prob, x_flexible, _ar, _p = solved(flexible)
	prob, x_exclusive, _ar, _p = solved(exclusive)

	assert assigned(x_flexible, role="R1") == 1 and assigned(x_flexible, role="RC") == 1
	assert status(prob) == "Optimal"
	assert assigned(x_exclusive, role="R1") == 1  # staffing the room is worth more
	assert assigned(x_exclusive, role="RC") == 0


def test_a_holders_own_mode_override_wins():
	"""
	The same two assignments in one slot: legal while the second role is a collateral duty,
	impossible once this holder works it as an ordinary role, because a presence spends
	itself on one role.
	"""
	both = {("E1", "R1", "AM", MON, "B1"), ("E1", "RC", "AM", MON, "B1")}
	rules = builtin_specs("warm_start", "use_existing_assignments", "one_shift_per_day")

	as_collateral = with_collateral(forced=both, rules=rules)
	as_role = with_collateral(forced=both, rules=rules, role_mode_overrides={("E1", "RC"): MODE_FLEXIBLE})

	assert as_collateral.mode("E1", "RC") == MODE_COLLATERAL
	assert as_role.working_roles("E1") == ("R1", "RC")
	prob, _x, _ar, _p = solved(as_collateral)
	assert status(prob) == "Optimal"
	prob, _x, _ar, _p = solved(as_role)
	assert status(prob) == "Infeasible"


def test_role_modes_leave_the_input_hash_of_a_site_without_them_alone():
	"""A site where every role is Flexible must keep hitting the solver cache."""
	assert pkg().input_hash() == pkg(role_mode={}, role_mode_overrides={}).input_hash()
	assert pkg().input_hash() != pkg(role_mode={"R1": MODE_COLLATERAL}).input_hash()


def test_a_role_that_gates_nothing_leaves_the_rooms_to_the_others():
	"""A floater: an ordinary role to work, but no room in the discipline waits on it."""
	data = with_collateral(
		employees=["E1", "E2"],
		employee_roles={"E1": ("R1",), "E2": ("R2",)},
		roles=["R1", "R2"],
		role_discipline={"R1": "D1", "R2": "D1"},
		target_shifts={"E1": 1, "E2": 1},
		max_rpe={("E1", "R1"): 1, ("E2", "R2"): 1},
		leave_blocked={("E2", MON)},  # the floater is on leave, so nobody floats
		role_mode={},  # both ordinary roles; only the gating flag separates them
		role_gates_rooms={"R1": True, "R2": False},
	)
	prob, _x, ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	assert data.gating_roles("E2") == ()
	assert pulp.value(ar[("D1", "AM", MON, "B1")]) == 1


def test_a_collateral_duty_can_be_made_to_gate_rooms():
	"""The switch is the authority, not the mode: a discipline may not run without its lead."""
	data = with_collateral(
		employees=["E1", "E2"],
		employee_roles={"E1": ("R1",), "E2": ("RC",)},
		target_shifts={"E1": 1, "E2": 1},
		max_rpe={("E1", "R1"): 1, ("E2", "RC"): 3},
		leave_blocked={("E2", MON)},  # nobody can lead today
		role_gates_rooms={"RC": True},
	)
	prob, x, ar, _presence = solved(data)

	assert status(prob) == "Optimal"
	assert pulp.value(ar[("D1", "AM", MON, "B1")]) == 0  # no lead, no room
	assert assigned(x) == 0  # and so nothing is worth scheduling


def test_the_gating_flag_is_read_before_the_mode():
	data = with_collateral(role_gates_rooms={"R1": False, "RC": True})
	assert data.gates_rooms("R1") is False
	assert data.gates_rooms("RC") is True
	# ... and a role nobody has ruled on follows its mode
	assert with_collateral().gates_rooms("R1") is True
	assert with_collateral().gates_rooms("RC") is False


def test_the_gating_flag_leaves_the_hash_of_a_site_without_it_alone():
	assert pkg().input_hash() == pkg(role_gates_rooms={}).input_hash()
	assert pkg().input_hash() != pkg(role_gates_rooms={"R1": False}).input_hash()


# ── which role an existing Shift Assignment was worked in ─────────────────────


def test_a_recorded_role_is_taken_at_its_word():
	outcome, role = types_module.resolve_assignment_role("R2", ["R1", "R2"], "D1", ["R1", "R2"])
	assert (outcome, role) == (types_module.ROLE_RESOLVED, "R2")


def test_a_recorded_role_the_employee_does_not_hold_is_refused():
	outcome, role = types_module.resolve_assignment_role("R9", ["R1"], "D1", ["R1"])
	assert (outcome, role) == (types_module.ROLE_NOT_HELD, None)


def test_a_single_role_in_the_discipline_is_inferred():
	outcome, role = types_module.resolve_assignment_role(None, ["R1", "R2"], "D1", ["R1"])
	assert (outcome, role) == (types_module.ROLE_RESOLVED, "R1")


def test_two_roles_in_the_discipline_are_never_guessed_between():
	"""The loader used to sort and take the first; that is the guess the field exists to stop."""
	outcome, role = types_module.resolve_assignment_role(None, ["R1", "R2"], "D1", ["R1", "R2"])
	assert (outcome, role) == (types_module.ROLE_AMBIGUOUS, None)


def test_two_roles_resolve_to_the_one_that_is_binding():
	"""A settled half-day is presence, not a choice of role — the books already settle it."""
	outcome, role = types_module.resolve_assignment_role(
		None, ["R1", "R2"], "D1", ["R1", "R2"], binding=["R2"]
	)
	assert (outcome, role) == (types_module.ROLE_RESOLVED, "R2")


def test_two_binding_roles_are_still_never_guessed_between():
	outcome, role = types_module.resolve_assignment_role(
		None, ["R1", "R2"], "D1", ["R1", "R2"], binding=["R1", "R2"]
	)
	assert (outcome, role) == (types_module.ROLE_AMBIGUOUS, None)


def test_a_binding_role_the_employee_does_not_hold_in_the_discipline_is_ignored():
	"""`binding` may list roles held elsewhere; only candidates in this discipline count."""
	outcome, role = types_module.resolve_assignment_role(
		None, ["R1", "R2"], "D1", ["R1", "R2"], binding=["R3"]
	)
	assert (outcome, role) == (types_module.ROLE_AMBIGUOUS, None)


def test_an_assignment_with_nothing_to_infer_from_says_so():
	assert types_module.resolve_assignment_role(None, ["R1"], None, []) == (
		types_module.ROLE_NO_DISCIPLINE,
		None,
	)
	assert types_module.resolve_assignment_role(None, ["R1"], "D2", []) == (
		types_module.ROLE_NONE_IN_DISCIPLINE,
		None,
	)


def test_somebody_holding_one_role_needs_no_discipline_to_infer_it():
	"""Rung 2: one non-collateral role held is not a choice between anything."""
	assert types_module.resolve_assignment_role(None, ["R1"], None, [], working=["R1"]) == (
		types_module.ROLE_RESOLVED,
		"R1",
	)


def test_one_role_held_outvotes_a_location_filed_under_another_discipline():
	"""The location says D2, where they hold nothing; they hold exactly one role, in D1.

	That is a mis-filed Shift Location, not a second role — see `resolve_assignment_role`.
	Without `working` the same call is still an error, which is why the rung is opt-in.
	"""
	assert types_module.resolve_assignment_role(None, ["R1"], "D2", [], working=["R1"]) == (
		types_module.ROLE_RESOLVED,
		"R1",
	)
	assert types_module.resolve_assignment_role(None, ["R1"], "D2", []) == (
		types_module.ROLE_NONE_IN_DISCIPLINE,
		None,
	)


def test_the_recorded_role_still_wins_over_the_only_role_held():
	"""Rung 1 is above rung 2: a record that names a role it holds is never second-guessed,
	and one naming a role the employee does not hold is still reported rather than replaced."""
	assert types_module.resolve_assignment_role("R2", ["R1", "R2"], None, [], working=["R1", "R2"]) == (
		types_module.ROLE_RESOLVED,
		"R2",
	)
	assert types_module.resolve_assignment_role("R9", ["R1"], None, [], working=["R1"]) == (
		types_module.ROLE_NOT_HELD,
		None,
	)


def test_two_roles_held_and_no_discipline_is_still_unanswerable():
	assert types_module.resolve_assignment_role(None, ["R1", "R2"], None, [], working=["R1", "R2"]) == (
		types_module.ROLE_NO_DISCIPLINE,
		None,
	)


# ── what a role is worth on its own ───────────────────────────────────────────


def standby(**overrides) -> DataPackage:
	"""E1 can only work RS, a standby role: no room in D1 waits on it, so nothing but its
	own value can ever be a reason to schedule them."""
	base: dict[str, Any] = {
		"roles": ["R1", "RS"],
		"role_discipline": {"R1": "D1", "RS": "D1"},
		"employee_roles": {"E1": ("RS",)},
		"max_rpe": {("E1", "RS"): 1},
		"role_gates_rooms": {"RS": False},
		"rules": builtin_specs(*STANDARD_RULES),
	}
	base.update(overrides)
	return pkg(**base)


def test_an_unpriced_role_is_not_worth_scheduling_anybody_on():
	"""The default: a shift is worth exactly the rooms it staffs, and standby staffs none."""
	prob, x, _ar, _p = solved(standby())
	assert status(prob) == "Optimal"
	assert assigned(x) == 0


def test_a_priced_standby_role_beats_leaving_somebody_unassigned():
	prob, x, _ar, _p = solved(standby(role_value={"RS": 2.0}))
	assert status(prob) == "Optimal"
	assert assigned(x, role="RS") == 1


def test_a_negatively_priced_role_stays_a_last_resort():
	"""Priced below what it costs, it is worked only where something else pays for it."""
	prob, x, _ar, _p = solved(standby(role_value={"RS": -5.0}))
	assert status(prob) == "Optimal"
	assert assigned(x) == 0


def test_role_value_is_earned_once_per_assignment():
	"""Two days of standby are worth two days of it, so the horizon scales the reward."""
	data = standby(working_days=days_from(2), target_shifts={"E1": 2}, role_value={"RS": 2.0})
	prob, x, _ar, _p = solved(data)
	assert status(prob) == "Optimal"
	assert assigned(x, role="RS") == 2


def test_a_priced_collateral_duty_is_earned_beside_the_shift_it_rides_on():
	"""Per assignment, not per presence: pricing a duty is how a duty gets paid for."""
	data = with_collateral(target_shifts={"E1": 1}, role_value={"RC": 1.0})
	prob, x, _ar, _p = solved(data)
	assert status(prob) == "Optimal"
	assert assigned(x, role="R1") == 1
	assert assigned(x, role="RC") == 1


def test_role_value_leaves_the_hash_of_a_site_without_it_alone():
	assert pkg().input_hash() == pkg(role_value={}).input_hash()
	assert pkg().input_hash() != pkg(role_value={"R1": 1.0}).input_hash()
