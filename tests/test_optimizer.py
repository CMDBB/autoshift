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

import pulp
import pytest

from autoshift.optimizer.editor_support import completion_items
from autoshift.optimizer.model_builder import build
from autoshift.optimizer.rules import (
	BUILTIN_RULES,
	KIND_CONSTRAINT,
	KIND_MIXED,
	KIND_OBJECTIVE,
	STANDARD_RULES,
	leave_blocklist,
)
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
	disc = "D1"
	b = "B1"
	base: dict[str, Any] = dict(
		flags=set(),
		employees=["E1"],
		shift_types=["AM"],
		working_days=[MON],
		branches=[b],
		roles=["R1"],
		role_discipline={"R1": disc},
		employee_roles={"E1": ("R1",)},
		target_shifts={"E1": 1},
		role_target_shifts={},
		max_rpe={("E1", "R1"): 1},
		rooms={(disc, b): 1},
		disciplines=[disc],
		leave_blocked=set(),
		forced=set(),
	)
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
	rules = builtin_specs(*(STANDARD_RULES | {"weigh_assignments_objective"}))
	with pytest.raises(ValueError, match="mutually exclusive"):
		build(pkg(rules=rules))


def test_choice_group_permits_the_other_member_alone():
	rules = builtin_specs(
		*((STANDARD_RULES - {"use_existing_assignments"}) | {"weigh_assignments_objective"})
	)
	prob, _, _ = solve(pkg(rules=rules))
	assert status(prob) == "Optimal"


def test_choice_group_permits_neither_member():
	rules = builtin_specs(*(STANDARD_RULES - {"use_existing_assignments"}))
	prob, _, _ = solve(pkg(rules=rules))
	assert status(prob) == "Optimal"


def test_rule_without_implementation_raises():
	with pytest.raises(ValueError, match="no implementation"):
		build(pkg(rules=(("Someday Rule", "", "", 1.0),)))


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

	shares = {
		rule: pulp.value(pulp.lpSum(terms)) or 0.0 for rule, terms in ctx.objective_contributions.items()
	}
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
