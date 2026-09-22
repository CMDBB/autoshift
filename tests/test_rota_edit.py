# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Tests for `rota/edit.py` — staging and folding hand-edits into an `EditPlan`.

Pure Python, no Frappe, same bargain as `test_rota.py`. Neutral placeholders
throughout (`E1`, `B1`) — see CLAUDE.md, "App boundary".
"""

import datetime

from autoshift.rota.cycle import Rota, monday_of
from autoshift.rota.edit import (
	Change,
	apply_changes,
	describe_change,
	minimal_cycle,
	phase_fractions,
	rota_view_weeks,
	weekday_range_label,
)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ANCHOR = datetime.date(2026, 8, 30)  # a Sunday


def rota(
	name,
	weekdays,
	shift_type="AM",
	branch="B1",
	cycle=1,
	anchor=None,
	employee="E1",
	unconfirmed=False,
	role=None,
	collateral=(),
) -> Rota:
	return Rota(
		assignment=name,
		employee=employee,
		company="C1",
		shift_type=shift_type,
		shift_location=branch,
		weekdays=frozenset(weekdays),
		cycle_weeks=cycle,
		anchor=anchor,
		unconfirmed=unconfirmed,
		scheduling_role=role,
		collateral_roles=tuple(collateral),
	)


# ── weekday_range_label ──────────────────────────────────────────────────────


def test_weekday_range_label_compresses_consecutive_runs():
	assert weekday_range_label([TUE, WED]) == "Tue-Wed"
	assert weekday_range_label([TUE, WED, FRI]) == "Tue-Wed, Fri"
	assert weekday_range_label([MON]) == "Mon"
	assert weekday_range_label([]) == "none"


# ── apply_changes: move ──────────────────────────────────────────────────────


def test_move_within_the_same_pattern_replaces_one_assignment():
	r = rota("SSA1", [TUE, WED])
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=FRI)

	plan = apply_changes([r], [change])

	assert plan.delete == ("SSA1",)
	assert len(plan.create) == 1
	created = plan.create[0]
	assert created.weekdays == frozenset({TUE, FRI})
	assert created.shift_type == "AM" and created.branch == "B1"
	assert created.cycle_weeks == 1 and created.anchor is None


def test_move_across_shift_type_touches_both_patterns():
	am = rota("SSA-AM", [TUE, WED], shift_type="AM")
	pm = rota("SSA-PM", [THU], shift_type="PM")
	change = Change(
		op="move",
		employee="E1",
		from_assignment="SSA-AM",
		from_weekday=WED,
		to_shift_type="PM",
		to_weekday=WED,
	)

	plan = apply_changes([am, pm], [change])

	assert set(plan.delete) == {"SSA-AM", "SSA-PM"}
	by_shift_type = {c.shift_type: c for c in plan.create}
	assert by_shift_type["AM"].weekdays == frozenset({TUE})
	assert by_shift_type["PM"].weekdays == frozenset({THU, WED})


def test_move_across_branch_carries_the_new_branch():
	r = rota("SSA1", [TUE], branch="B1")
	change = Change(
		op="move", employee="E1", from_assignment="SSA1", from_weekday=TUE, to_branch="B2", to_weekday=TUE
	)

	plan = apply_changes([r], [change])

	assert plan.delete == ("SSA1",)
	assert plan.create[0].branch == "B2"
	assert plan.create[0].weekdays == frozenset({TUE})


def test_move_onto_an_existing_pattern_merges_into_it():
	source = rota("SSA-A", [MON])
	target = rota("SSA-B", [FRI])
	# moving Monday from A onto B's own (AM, B1) pattern, landing on Wednesday
	change = Change(
		op="move",
		employee="E1",
		from_assignment="SSA-A",
		from_weekday=MON,
		to_shift_type="AM",
		to_weekday=WED,
		to_branch="B1",
	)

	plan = apply_changes([source, target], [change])

	assert set(plan.delete) == {"SSA-A", "SSA-B"}
	assert len(plan.create) == 1
	assert plan.create[0].weekdays == frozenset({FRI, WED})


def test_move_preserves_multiweek_cadence_and_realigns_the_anchor():
	# ANCHOR's own phase-0 week is the Monday after it (see cycle.first_covered_week).
	view_start = monday_of(ANCHOR) + datetime.timedelta(weeks=1)
	r = rota("SSA1", [MON, WED], cycle=4, anchor=ANCHOR)
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=FRI)

	plan = apply_changes([r], [change], view_start=view_start, view_weeks=4)

	assert plan.create[0].cycle_weeks == 4
	# Recreated wholesale like every edited pattern (see the module docstring) — a
	# different anchor date is fine as long as it resolves to the same phase-0 week.
	assert plan.create[0].anchor == view_start - datetime.timedelta(weeks=1)


def test_move_that_nets_to_nothing_leaves_the_assignment_alone():
	r = rota("SSA1", [TUE, WED])
	# move Wed to Wed: no actual change
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=WED)

	plan = apply_changes([r], [change])

	assert plan.delete == ()
	assert plan.create == ()


def test_unrelated_assignment_is_never_touched():
	touched = rota("SSA1", [MON])
	untouched = rota("SSA2", [FRI], shift_type="PM")
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=MON, to_weekday=TUE)

	plan = apply_changes([touched, untouched], [change])

	assert plan.delete == ("SSA1",)
	assert len(plan.create) == 1
	assert plan.create[0].shift_type == "AM"


# ── apply_changes: remove ────────────────────────────────────────────────────


def test_remove_the_only_day_deletes_with_no_replacement():
	r = rota("SSA1", [TUE])
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=TUE)

	plan = apply_changes([r], [change])

	assert plan.delete == ("SSA1",)
	assert plan.create == ()


def test_remove_one_of_several_days_recreates_the_rest():
	r = rota("SSA1", [TUE, WED, FRI])
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=WED)

	plan = apply_changes([r], [change])

	assert plan.delete == ("SSA1",)
	assert plan.create[0].weekdays == frozenset({TUE, FRI})


# ── apply_changes: add ───────────────────────────────────────────────────────


def test_add_creates_a_fresh_weekly_assignment():
	change = Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_weekday=MON, to_branch="B1")

	plan = apply_changes([], [change])

	assert plan.delete == ()
	assert len(plan.create) == 1
	created = plan.create[0]
	assert created.employee == "E1" and created.company == "C1"
	assert created.weekdays == frozenset({MON})
	assert created.cycle_weeks == 1 and created.anchor is None


def test_add_onto_an_existing_pattern_extends_it_instead_of_duplicating():
	existing = rota("SSA1", [TUE, WED])
	change = Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_branch="B1", to_weekday=FRI)

	plan = apply_changes([existing], [change])

	assert plan.delete == ("SSA1",)
	assert plan.create[0].weekdays == frozenset({TUE, WED, FRI})


def test_add_without_a_company_raises():
	change = Change(op="add", employee="E1", to_shift_type="AM", to_weekday=MON, to_branch="B1")
	try:
		apply_changes([], [change])
	except ValueError:
		pass
	else:
		raise AssertionError("expected ValueError for a companyless add")


# ── apply_changes: a batch of changes folds sequentially ────────────────────


def test_a_batch_folds_changes_in_order():
	r = rota("SSA1", [MON])
	changes = [
		Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=MON, to_weekday=TUE),
		Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_branch="B1", to_weekday=THU),
	]

	plan = apply_changes([r], changes)

	assert plan.delete == ("SSA1",)
	assert plan.create[0].weekdays == frozenset({TUE, THU})


# ── apply_changes: chained edits on a not-yet-applied occurrence ────────────
#
# The editor lets a chip stay draggable before Apply — a follow-up edit on one has no
# real Shift Schedule Assignment to reference, so it identifies the touched pattern via
# from_shift_type/from_branch instead of from_assignment (see rota.editor._effective_rotas
# and the Change docstring).


def test_a_second_move_on_a_still_pending_chip_resolves_by_pattern_not_assignment():
	r = rota("SSA1", [MON, TUE])
	changes = [
		Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=MON, to_weekday=THU),
		# The chip just moved to THU has no real assignment yet — from_assignment is None,
		# exactly as `editor.stage_change` persists it — so this relies on from_shift_type/
		# from_branch to find the same in-flight group.
		Change(
			op="move",
			employee="E1",
			from_shift_type="AM",
			from_branch="B1",
			from_weekday=THU,
			to_weekday=FRI,
		),
	]

	plan = apply_changes([r], changes)

	assert plan.delete == ("SSA1",)
	assert len(plan.create) == 1
	assert plan.create[0].weekdays == frozenset({TUE, FRI})


def test_removing_a_still_pending_add_nets_to_nothing():
	changes = [
		Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_branch="B1", to_weekday=MON),
		Change(op="remove", employee="E1", from_shift_type="AM", from_branch="B1", from_weekday=MON),
	]

	plan = apply_changes([], changes)

	assert plan.delete == ()
	assert plan.create == ()


def test_a_still_pending_add_can_be_moved_to_a_different_pattern():
	# `editor.stage_change` always resupplies `company` fresh from Employee.company on
	# every Change, move included — a move onto a group with no real Rota of its own (as
	# here) has nowhere else to draw it from.
	changes = [
		Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_branch="B1", to_weekday=MON),
		Change(
			op="move",
			employee="E1",
			company="C1",
			from_shift_type="AM",
			from_branch="B1",
			from_weekday=MON,
			to_shift_type="PM",
			to_branch="B2",
			to_weekday=TUE,
		),
	]

	plan = apply_changes([], changes)

	assert plan.delete == ()
	assert len(plan.create) == 1
	created = plan.create[0]
	assert created.shift_type == "PM" and created.branch == "B2"
	assert created.weekdays == frozenset({TUE})
	assert created.company == "C1"  # carried across the chain, not lost with the source


# ── apply_changes: silver and gold ───────────────────────────────────────────


def test_an_untouched_batch_promotes_nothing():
	r = rota("SSA1", [MON], unconfirmed=True)
	plan = apply_changes(
		[r],
		[Change(op="add", employee="E1", company="C1", to_shift_type="PM", to_weekday=TUE, to_branch="B1")],
	)
	assert plan.promote == ()


def test_promote_confirms_every_silver_pattern_of_that_employee_in_place():
	rows = [
		rota("S1", [MON], unconfirmed=True),
		rota("S2", [TUE], shift_type="PM", unconfirmed=True),
		rota("G1", [WED], shift_type="PM", branch="B2"),
		rota("OTHER", [MON], employee="E2", unconfirmed=True),
	]

	plan = apply_changes(rows, [Change(op="promote", employee="E1")])

	assert plan.promote == ("S1", "S2")
	assert plan.delete == ()
	assert plan.create == ()


def test_promote_skips_a_pattern_the_same_batch_replaces():
	"""An edited pattern is recreated gold already; promoting the row it replaces would
	write to a document Apply is about to delete."""
	rows = [rota("S1", [MON, TUE], unconfirmed=True), rota("S2", [MON], shift_type="PM", unconfirmed=True)]
	changes = [
		Change(op="promote", employee="E1"),
		Change(op="move", employee="E1", from_assignment="S1", from_weekday=TUE, to_weekday=WED),
	]

	plan = apply_changes(rows, changes)

	assert plan.delete == ("S1",)
	assert plan.promote == ("S2",)


def test_describe_promote_counts_the_patterns_it_confirms():
	rows = [
		rota("S1", [MON], unconfirmed=True),
		rota("S2", [TUE], shift_type="PM", unconfirmed=True),
		rota("G1", [WED]),
	]
	assert (
		describe_change(Change(op="promote", employee="E1"), rows)
		== "E1: confirmed 2 unconfirmed pattern(s) as they stand"
	)


# ── describe_change ───────────────────────────────────────────────────────────


def test_describe_move_within_one_pattern_names_the_shape_before_and_after():
	r = rota("SSA1", [TUE, WED])
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=FRI)

	assert describe_change(change, [r]) == "E1: moved AM (B1) from Tue-Wed to Tue, Fri"


def test_describe_move_across_shift_type_names_both_sides():
	r = rota("SSA1", [TUE], shift_type="AM", branch="B1")
	change = Change(
		op="move",
		employee="E1",
		from_assignment="SSA1",
		from_weekday=TUE,
		to_shift_type="PM",
		to_branch="B2",
		to_weekday=WED,
	)

	assert describe_change(change, [r]) == "E1: moved Tuesday from AM (B1) to PM (B2) Wednesday"


def test_describe_add():
	change = Change(op="add", employee="E1", company="C1", to_shift_type="PM", to_weekday=MON, to_branch="B1")
	assert describe_change(change, []) == "E1: added PM Monday at B1"


def test_describe_remove():
	r = rota("SSA1", [TUE])
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=TUE)
	assert describe_change(change, [r]) == "E1: removed AM Tuesday at B1"


# ── rota_view_weeks ──────────────────────────────────────────────────────────


def test_a_weekly_rota_is_visible_and_tiles_at_every_width():
	assert rota_view_weeks(cycle_weeks=1, view_weeks=1)
	assert rota_view_weeks(cycle_weeks=1, view_weeks=4)


def test_a_wider_rota_is_hidden_from_a_narrower_view():
	assert not rota_view_weeks(cycle_weeks=4, view_weeks=1)
	assert not rota_view_weeks(cycle_weeks=4, view_weeks=2)


def test_a_rota_is_visible_at_its_own_width_and_any_clean_multiple():
	assert rota_view_weeks(cycle_weeks=2, view_weeks=2)
	assert rota_view_weeks(cycle_weeks=2, view_weeks=4)
	assert not rota_view_weeks(cycle_weeks=3, view_weeks=4)


# ── minimal_cycle ─────────────────────────────────────────────────────────────


def test_minimal_cycle_collapses_uniform_phases_to_weekly():
	assert minimal_cycle({0: frozenset({MON})}, 1) == 1
	assert minimal_cycle({0: frozenset({MON}), 1: frozenset({MON})}, 2) == 1


def test_minimal_cycle_finds_the_smallest_divisor_that_fits():
	assert minimal_cycle({0: frozenset({MON}), 1: frozenset()}, 2) == 2
	assert (
		minimal_cycle({0: frozenset({MON}), 1: frozenset({MON}), 2: frozenset({MON}), 3: frozenset()}, 4) == 4
	)


def test_minimal_cycle_finds_a_period_that_is_not_the_whole_view():
	# repeats every 2 weeks inside a 4-week view: 1 == 3-of-a-kind (phase 0, 2) and
	# phase 1, 3 agree too, so the true period is 2, not 4.
	phases = {0: frozenset({MON}), 1: frozenset({FRI}), 2: frozenset({MON}), 3: frozenset({FRI})}
	assert minimal_cycle(phases, 4) == 2


# ── apply_changes: auto-detecting a periodicity change ──────────────────────────


def test_removing_one_occurrence_in_a_wider_view_promotes_the_cadence():
	view_start = datetime.date(2026, 8, 31)  # a Monday
	r = rota("SSA1", [FRI])  # weekly: Friday every week
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=FRI, from_phase=1)

	plan = apply_changes([r], [change], view_start=view_start, view_weeks=2)

	assert plan.delete == ("SSA1",)
	assert len(plan.create) == 1
	created = plan.create[0]
	assert created.cycle_weeks == 2
	assert created.weekdays == frozenset({FRI})
	assert created.anchor == view_start - datetime.timedelta(weeks=1)
	assert plan.cadence_changes == ("E1: AM (B1) periodicity changed from Every Week to Every 2 Weeks",)


def test_converging_a_two_week_rota_back_to_uniform_demotes_the_cadence():
	view_start = datetime.date(2026, 8, 31)  # a Monday
	# a genuine 2-week rota: Friday in phase 0 only.
	week1_only = rota("SSA1", [FRI], cycle=2, anchor=view_start - datetime.timedelta(weeks=1))
	change = Change(
		op="add", employee="E1", company="C1", to_shift_type="AM", to_branch="B1", to_weekday=FRI, to_phase=1
	)

	plan = apply_changes([week1_only], [change], view_start=view_start, view_weeks=2)

	assert plan.delete == ("SSA1",)
	assert len(plan.create) == 1
	created = plan.create[0]
	assert created.cycle_weeks == 1 and created.anchor is None
	assert created.weekdays == frozenset({FRI})
	assert plan.cadence_changes == ("E1: AM (B1) periodicity changed from Every 2 Weeks to Every Week",)


def test_editing_in_a_one_week_view_never_promotes_the_cadence():
	# with only one phase to touch, every edit is by construction uniform.
	r = rota("SSA1", [TUE, WED])
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=FRI)

	plan = apply_changes([r], [change], view_start=datetime.date(2026, 8, 31), view_weeks=1)

	assert plan.cadence_changes == ()
	assert plan.create[0].cycle_weeks == 1


def test_editing_one_phase_of_a_wider_view_leaves_the_other_phase_as_it_was():
	# touching only phase 0's Wednesday, in a 2-week view, is exactly what makes the
	# pattern stop being uniform — phase 1 is untouched and keeps its old content.
	r = rota("SSA1", [TUE, WED])
	change = Change(op="move", employee="E1", from_assignment="SSA1", from_weekday=WED, to_weekday=FRI)

	plan = apply_changes([r], [change], view_start=datetime.date(2026, 8, 31), view_weeks=2)

	assert len(plan.create) == 2
	by_weekdays = {frozenset(c.weekdays) for c in plan.create}
	assert by_weekdays == {frozenset({TUE, FRI}), frozenset({TUE, WED})}
	assert all(c.cycle_weeks == 2 for c in plan.create)
	assert plan.cadence_changes == ("E1: AM (B1) periodicity changed from Every Week to Every 2 Weeks",)


def test_a_brand_new_pattern_is_an_addition_not_a_cadence_change():
	change = Change(op="add", employee="E1", company="C1", to_shift_type="AM", to_weekday=MON, to_branch="B1")

	plan = apply_changes([], [change], view_start=datetime.date(2026, 8, 31), view_weeks=2)

	assert plan.cadence_changes == ()


# ── describe_change: naming the touched week in a wider view ───────────────────


def test_describe_remove_names_the_touched_week_when_the_view_is_wider_than_one_week():
	r = rota("SSA1", [FRI])
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=FRI, from_phase=1)
	assert describe_change(change, [r], view_weeks=2) == "E1: removed AM Friday (week 2) at B1"


def test_describe_add_names_the_touched_week_when_the_view_is_wider_than_one_week():
	change = Change(
		op="add", employee="E1", company="C1", to_shift_type="PM", to_weekday=MON, to_branch="B1", to_phase=1
	)
	assert describe_change(change, [], view_weeks=2) == "E1: added PM Monday (week 2) at B1"


def test_describe_a_one_week_view_never_mentions_a_week_number():
	r = rota("SSA1", [TUE])
	change = Change(op="remove", employee="E1", from_assignment="SSA1", from_weekday=TUE)
	assert "week" not in describe_change(change, [r], view_weeks=1)


# ── phase_fractions ──────────────────────────────────────────────────────────


def test_phase_fractions_averages_occupancy_over_a_full_cycle():
	view_start = datetime.date(2026, 8, 31)  # a Monday
	rows = [
		rota("A0", [MON, WED], cycle=4, anchor=view_start - datetime.timedelta(weeks=1)),
		rota("A1", [MON], cycle=4, anchor=view_start),
		rota("A2", [MON, WED], cycle=4, anchor=view_start + datetime.timedelta(weeks=1)),
		rota("A3", [MON], cycle=4, anchor=view_start + datetime.timedelta(weeks=2)),
	]

	fractions = phase_fractions(rows, "AM", [MON, WED, FRI], view_start, cycle_weeks=4)

	assert fractions[MON] == (4, "B1")
	assert fractions[WED] == (2, "B1")
	assert fractions[FRI] == (0, None)


def test_phase_fractions_reports_no_branch_when_the_occupied_weeks_disagree():
	view_start = datetime.date(2026, 8, 31)
	rows = [
		rota("A0", [MON], branch="B1", cycle=2, anchor=view_start - datetime.timedelta(weeks=1)),
		rota("A1", [MON], branch="B2", cycle=2, anchor=view_start),
	]

	fractions = phase_fractions(rows, "AM", [MON], view_start, cycle_weeks=2)

	assert fractions[MON] == (2, None)


def test_phase_fractions_ignores_other_shift_types():
	r = rota("SSA1", [MON], shift_type="PM")
	fractions = phase_fractions([r], "AM", [MON], datetime.date(2026, 8, 31), cycle_weeks=1)
	assert fractions[MON] == (0, None)


# ── the role a pattern is worked in ──────────────────────────────────────────


def test_moving_a_day_keeps_the_role_the_pattern_is_worked_in():
	"""Editing *when* somebody works must never quietly change *what* they work."""
	rotas = [rota("A1", [MON, TUE], role="R1", collateral=("RC",))]
	plan = apply_changes(
		rotas, [Change(op="move", employee="E1", from_assignment="A1", from_weekday=TUE, to_weekday=WED)]
	)
	assert [n.scheduling_role for n in plan.create] == ["R1"]
	assert [n.collateral_roles for n in plan.create] == [("RC",)]


def test_moving_to_another_branch_carries_the_role_with_it():
	rotas = [rota("A1", [MON], role="R1")]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="move",
				employee="E1",
				from_assignment="A1",
				from_weekday=MON,
				to_weekday=MON,
				to_branch="B2",
			)
		],
	)
	assert [(n.branch, n.scheduling_role) for n in plan.create] == [("B2", "R1")]


def test_an_add_records_the_role_its_change_carries():
	"""An add has no pattern to inherit from, the same reason it must carry a company."""
	plan = apply_changes(
		[],
		[
			Change(
				op="add",
				employee="E1",
				company="C1",
				scheduling_role="R1",
				to_shift_type="AM",
				to_branch="B1",
				to_weekday=MON,
			)
		],
	)
	assert [n.scheduling_role for n in plan.create] == ["R1"]


def test_a_pattern_with_no_role_recorded_stays_that_way():
	"""Nothing is invented: a rota the import never gave a role to keeps saying nothing."""
	plan = apply_changes(
		[rota("A1", [MON, TUE])],
		[Change(op="move", employee="E1", from_assignment="A1", from_weekday=TUE, to_weekday=WED)],
	)
	assert [n.scheduling_role for n in plan.create] == [None]


def test_two_roles_at_one_shift_and_branch_are_two_patterns_not_one():
	"""The role is part of a pattern's identity (`group_key`).

	`Shift Schedule Assignment.custom_scheduling_role` is single-valued, so folding two
	differently-roled half-days into one document would silently re-role one of them.
	Editing the R1 pattern must therefore leave the R2 one completely alone.
	"""
	rotas = [rota("A1", [MON, TUE], role="R1"), rota("A2", [WED], role="R2")]
	plan = apply_changes(
		rotas, [Change(op="move", employee="E1", from_assignment="A1", from_weekday=TUE, to_weekday=THU)]
	)
	assert plan.delete == ("A1",)
	assert [(sorted(n.weekdays), n.scheduling_role) for n in plan.create] == [([MON, THU], "R1")]


def test_two_collateral_duty_sets_at_one_shift_and_branch_stay_apart():
	"""Same argument as the role: the duties ride on the document, so they are identity."""
	rotas = [rota("A1", [MON], role="R1", collateral=("RC",)), rota("A2", [TUE], role="R1")]
	plan = apply_changes(
		rotas, [Change(op="move", employee="E1", from_assignment="A2", from_weekday=TUE, to_weekday=WED)]
	)
	assert plan.delete == ("A2",)
	assert [(sorted(n.weekdays), n.collateral_roles) for n in plan.create] == [([WED], ())]


def test_moving_onto_a_day_another_role_already_works_does_not_merge_them():
	"""Two half-days at one shift type and branch, in two roles — the site may allow it
	(`allow_multiple_shift_assignments`), and it is certainly not one pattern."""
	rotas = [rota("A1", [MON], role="R1"), rota("A2", [TUE], role="R2")]
	plan = apply_changes(
		rotas, [Change(op="move", employee="E1", from_assignment="A1", from_weekday=MON, to_weekday=TUE)]
	)
	assert plan.delete == ("A1",)
	assert [(sorted(n.weekdays), n.scheduling_role) for n in plan.create] == [([TUE], "R1")]


def test_a_chained_edit_on_a_pending_chip_keeps_the_role_it_came_from():
	"""A still-pending chip has no Shift Schedule Assignment to look the role up on, so
	the caller supplies the source identity — see `Change.from_scheduling_role`."""
	plan = apply_changes(
		[rota("A1", [MON], role="R1", collateral=("RC",))],
		[
			Change(
				op="move",
				employee="E1",
				company="C1",
				from_assignment=None,
				from_shift_type="AM",
				from_branch="B1",
				from_scheduling_role="R1",
				from_collateral_roles=("RC",),
				from_weekday=MON,
				to_weekday=TUE,
			)
		],
	)
	assert [(sorted(n.weekdays), n.scheduling_role, n.collateral_roles) for n in plan.create] == [
		([TUE], "R1", ("RC",))
	]


# ── retag: changing what a half-day is worked as ─────────────────────────────


def test_retag_moves_one_occurrence_into_another_role():
	"""The day stays put; only the role changes — so the pattern splits in two."""
	rotas = [rota("A1", [MON, TUE], role="R1")]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="retag",
				employee="E1",
				from_assignment="A1",
				from_weekday=TUE,
				scheduling_role="R2",
			)
		],
	)
	assert plan.delete == ("A1",)
	assert sorted((sorted(n.weekdays), n.scheduling_role) for n in plan.create) == [
		([MON], "R1"),
		([TUE], "R2"),
	]


def test_retagging_a_single_day_pattern_replaces_it_wholesale():
	rotas = [rota("A1", [WED], role="R1")]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="retag",
				employee="E1",
				from_assignment="A1",
				from_weekday=WED,
				scheduling_role="R2",
			)
		],
	)
	assert plan.delete == ("A1",)
	assert [(sorted(n.weekdays), n.scheduling_role, n.branch) for n in plan.create] == [([WED], "R2", "B1")]


def test_retagging_into_a_role_that_already_has_a_pattern_merges_into_it():
	rotas = [rota("A1", [MON, TUE], role="R1"), rota("A2", [THU], role="R2")]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="retag",
				employee="E1",
				from_assignment="A1",
				from_weekday=TUE,
				scheduling_role="R2",
			)
		],
	)
	assert plan.delete == ("A1", "A2")
	assert sorted((sorted(n.weekdays), n.scheduling_role) for n in plan.create) == [
		([MON], "R1"),
		([TUE, THU], "R2"),
	]


def test_retag_keeps_the_collateral_duties_riding_on_the_half_day():
	rotas = [rota("A1", [MON], role="R1", collateral=("RC",))]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="retag",
				employee="E1",
				from_assignment="A1",
				from_weekday=MON,
				scheduling_role="R2",
			)
		],
	)
	assert [(n.scheduling_role, n.collateral_roles) for n in plan.create] == [("R2", ("RC",))]


def test_retag_that_nets_back_to_the_original_role_leaves_the_assignment_alone():
	rotas = [rota("A1", [MON], role="R1")]
	plan = apply_changes(
		rotas,
		[
			Change(op="retag", employee="E1", from_assignment="A1", from_weekday=MON, scheduling_role="R2"),
			Change(
				op="retag",
				employee="E1",
				from_assignment=None,
				from_shift_type="AM",
				from_branch="B1",
				from_scheduling_role="R2",
				from_weekday=MON,
				scheduling_role="R1",
			),
		],
	)
	assert plan == plan.__class__()


def test_describe_retag_names_both_roles():
	rotas = [rota("A1", [MON, TUE], role="R1")]
	line = describe_change(
		Change(op="retag", employee="E1", from_assignment="A1", from_weekday=TUE, scheduling_role="R2"),
		rotas,
	)
	assert line == "E1: AM Tuesday now worked as R2 (was R1)"


def test_describe_retag_of_an_unattributed_pattern_says_so():
	rotas = [rota("A1", [MON])]
	line = describe_change(
		Change(op="retag", employee="E1", from_assignment="A1", from_weekday=MON, scheduling_role="R1"),
		rotas,
	)
	assert line == "E1: AM Monday now worked as R1 (was no role)"


def test_a_retag_in_a_wider_view_names_the_touched_week():
	rotas = [rota("A1", [MON], role="R1")]
	line = describe_change(
		Change(
			op="retag",
			employee="E1",
			from_assignment="A1",
			from_weekday=MON,
			from_phase=1,
			scheduling_role="R2",
		),
		rotas,
		view_weeks=2,
	)
	assert line == "E1: AM Monday (week 2) now worked as R2 (was R1)"


def test_a_retag_in_a_wider_view_promotes_the_cadence_like_any_other_edit():
	"""Re-roling one week's Monday and not the other's is exactly how a weekly pattern
	becomes fortnightly — periodicity is still derived, see the module docstring."""
	view_start = monday_of(datetime.date(2026, 9, 7))
	rotas = [rota("A1", [MON], role="R1")]
	plan = apply_changes(
		rotas,
		[
			Change(
				op="retag",
				employee="E1",
				from_assignment="A1",
				from_weekday=MON,
				from_phase=1,
				scheduling_role="R2",
			)
		],
		view_start=view_start,
		view_weeks=2,
	)
	assert sorted((sorted(n.weekdays), n.scheduling_role, n.cycle_weeks) for n in plan.create) == [
		([MON], "R1", 2),
		([MON], "R2", 2),
	]
