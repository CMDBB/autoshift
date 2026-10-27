# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""What a scanned window says about its days, and where those days land on a calendar.

Pure Python, no Frappe and no site, the same bargain the other suites make:
`scanner/horizon.py` takes a `DataPackage` plus the two tables a run persists and decides
nothing else, so the whole verdict is assertable here. Neutral placeholders throughout
(`E1`, `B1`, `D1`) — see CLAUDE.md, "App boundary".
"""

from __future__ import annotations

import datetime
import itertools
from typing import Any

import pytest

from autoshift.optimizer.types import MODE_EXCLUSIVE, MODE_FLEXIBLE, DataPackage
from autoshift.scanner.horizon import (
	SEV_CRITICAL,
	SEV_FAILED,
	SEV_INFO,
	SEV_OK,
	SEV_WARNING,
	Cell,
	Day,
	day_detail,
	day_severity,
	day_summary,
	dumps,
	loads,
	monday_of,
	month_grids,
	narrow,
	plan_windows,
	window_days,
	window_findings,
	window_severity,
	worst,
)

MON = datetime.date(2026, 6, 1)  # a known Monday
DISC = "D1"
BRANCH = "B1"


def days_from(n: int, start: datetime.date = MON) -> list[datetime.date]:
	return [start + datetime.timedelta(days=i) for i in range(n)]


def pkg(**overrides) -> DataPackage:
	"""A minimal package: one employee holding one gating role in one discipline."""
	base: dict[str, Any] = {
		"flags": set(),
		"employees": ["E1"],
		"shift_types": ["AM"],
		"working_days": [MON],
		"branches": [BRANCH],
		"roles": ["R1"],
		"role_discipline": {"R1": DISC},
		"employee_roles": {"E1": ("R1",)},
		"target_shifts": {"E1": 1},
		"role_target_shifts": {},
		"max_rpe": {("E1", "R1"): 1},
		"rooms": {(DISC, BRANCH): 1},
		"disciplines": [DISC],
		"leave_blocked": set(),
		"forced": set(),
		"shift_preferences": {"E1": {"AM": 1.0}},
	}
	base.update(overrides)
	return DataPackage(**base)


def slot(employee="E1", role="R1", shift_type="AM", date=MON, branch=BRANCH) -> dict:
	return {
		"employee": employee,
		"scheduling_role": role,
		"shift_type": shift_type,
		"date": date,
		"branch": branch,
	}


def coverage(staffed, capacity, date=MON, shift_type="AM", discipline=DISC, branch=BRANCH) -> dict:
	return {
		"discipline": discipline,
		"branch": branch,
		"date": date,
		"shift_type": shift_type,
		"staffed_rooms": staffed,
		"capacity": capacity,
	}


def cell(staffed, capacity, exclusive=None, **kwargs) -> Cell:
	return Cell(
		discipline=kwargs.get("discipline", DISC),
		branch=kwargs.get("branch", BRANCH),
		shift_type=kwargs.get("shift_type", "AM"),
		staffed=staffed,
		capacity=capacity,
		exclusive_capacity=capacity if exclusive is None else exclusive,
	)


# ── the horizon ───────────────────────────────────────────────────────────────


def test_plan_windows_starts_on_the_monday_of_the_start_date():
	windows = plan_windows(MON + datetime.timedelta(days=2), MON + datetime.timedelta(days=13), "1-week")
	assert windows[0] == (MON, MON + datetime.timedelta(days=6))


def test_plan_windows_do_not_overlap_and_cover_every_day_once():
	windows = plan_windows(MON, MON + datetime.timedelta(days=27), "1-week")
	assert len(windows) == 4
	for (_, last), (next_first, _) in itertools.pairwise(windows):
		assert next_first == last + datetime.timedelta(days=1)


def test_plan_windows_keeps_the_last_window_whole():
	"""Truncating it would solve a short week and then report the hole as a finding."""
	windows = plan_windows(MON, MON + datetime.timedelta(days=8), "1-week")
	assert windows[-1] == (MON + datetime.timedelta(days=7), MON + datetime.timedelta(days=13))


def test_plan_windows_steps_by_the_window_length():
	windows = plan_windows(MON, MON + datetime.timedelta(days=27), "4-week")
	assert windows == [(MON, MON + datetime.timedelta(days=27))]


def test_plan_windows_refuses_an_unbounded_horizon():
	with pytest.raises(NotImplementedError):
		plan_windows(MON, MON + datetime.timedelta(days=7), "Unbounded")


def test_monday_of_is_idempotent_on_a_monday():
	assert monday_of(MON) == MON


# ── severity ──────────────────────────────────────────────────────────────────


def test_a_full_slot_is_ok_and_an_empty_one_is_critical():
	assert cell(2, 2).severity(scaled=False) == SEV_OK
	assert cell(1, 2).severity(scaled=False) == SEV_WARNING
	assert cell(0, 2).severity(scaled=False) == SEV_CRITICAL


def test_a_slot_with_no_configured_rooms_is_never_a_problem():
	assert cell(0, 0).severity(scaled=False) == SEV_OK


def test_worst_cell_wins_over_the_day():
	day = Day(MON, worked=True, cells=(cell(2, 2), cell(0, 2), cell(1, 2)))
	assert day_severity(day, scaled=False) == SEV_CRITICAL


def test_worst_over_nothing_is_ok():
	assert worst(()) == SEV_OK
	assert day_severity(Day(MON, worked=True), scaled=False) == SEV_OK


def test_under_fte_alone_is_information_not_a_problem():
	"""Nobody is missing from a room over it — it is a fact about somebody's contract."""
	day = Day(MON, worked=True, cells=(cell(2, 2),), under_fte=(("E1", 2, (DISC,)),))
	assert day_severity(day, scaled=False) == SEV_INFO


def test_a_conflict_is_a_warning_even_where_every_room_is_staffed():
	day = Day(MON, worked=True, cells=(cell(2, 2),), conflicts=(("binding", "E1", "x", DISC),))
	assert day_severity(day, scaled=False) == SEV_WARNING


def test_a_conflict_never_lowers_a_worse_coverage_verdict():
	day = Day(MON, worked=True, cells=(cell(0, 2),), conflicts=(("binding", "E1", "x", DISC),))
	assert day_severity(day, scaled=False) == SEV_CRITICAL


# ── scaling to the exclusive holders who are actually in ──────────────────────


def test_scaling_forgives_rooms_no_exclusive_holder_could_have_opened():
	"""Two rooms, one dentist in: short a dentist, not short a schedule."""
	day = Day(MON, worked=True, cells=(cell(1, 2, exclusive=1),))
	assert day_severity(day, scaled=False) == SEV_WARNING
	assert day_severity(day, scaled=True) == SEV_OK


def test_scaling_still_flags_a_room_the_flexible_staff_left_shut():
	"""Both dentists in, only one room staffed — that is the finding scaling exists for."""
	day = Day(MON, worked=True, cells=(cell(1, 2, exclusive=2),))
	assert day_severity(day, scaled=True) == SEV_WARNING
	assert day_summary(day, scaled=True)["shortfall"] == 1


def test_the_scaled_reading_reports_its_own_capacity_and_the_configured_one():
	summary = day_summary(Day(MON, worked=True, cells=(cell(1, 4, exclusive=2),)), scaled=True)
	assert (summary["capacity"], summary["configured"], summary["shortfall"]) == (2, 4, 1)


def test_exclusive_capacity_is_the_scarcest_exclusive_gating_role():
	"""A room waits on every gating role, so the scarcest exclusive one caps it."""
	data = pkg(
		employees=["E1", "E2", "E3"],
		roles=["R1", "R2"],
		role_discipline={"R1": DISC, "R2": DISC},
		employee_roles={"E1": ("R1",), "E2": ("R1",), "E3": ("R2",)},
		role_mode={"R1": MODE_EXCLUSIVE, "R2": MODE_EXCLUSIVE},
		max_rpe={("E1", "R1"): 1, ("E2", "R1"): 1, ("E3", "R2"): 1},
		target_shifts={"E1": 1, "E2": 1, "E3": 1},
		rooms={(DISC, BRANCH): 4},
		shift_preferences={e: {"AM": 1.0} for e in ("E1", "E2", "E3")},
	)
	days = window_findings(
		data,
		[slot("E1", "R1"), slot("E2", "R1"), slot("E3", "R2")],
		[coverage(1, 4)],
		[MON],
	)
	# R1 supplies two rooms, R2 only one: one room is all the pair can open.
	assert days[0].cells[0].exclusive_capacity == 1
	assert days[0].cells[0].capacity == 4


def test_a_flexible_role_never_caps_the_scaled_capacity():
	"""Flexible staff are what a scan is looking for a shortage of."""
	data = pkg(
		role_mode={"R1": MODE_FLEXIBLE},
		rooms={(DISC, BRANCH): 3},
	)
	days = window_findings(data, [slot()], [coverage(1, 3)], [MON])
	assert days[0].cells[0].exclusive_capacity == 3
	assert days[0].cells[0].severity(scaled=True) == SEV_WARNING


def test_a_discipline_with_no_exclusive_role_reads_the_same_under_both_lenses():
	data = pkg(rooms={(DISC, BRANCH): 2})
	days = window_findings(data, [slot()], [coverage(1, 2)], [MON])
	assert days[0].cells[0].severity(scaled=True) == days[0].cells[0].severity(scaled=False)


def test_an_exclusive_holder_nobody_scheduled_supplies_nothing():
	"""The cap follows who is *in*, not who exists — a day off closes their room."""
	data = pkg(
		role_mode={"R1": MODE_EXCLUSIVE},
		rooms={(DISC, BRANCH): 2},
	)
	days = window_findings(data, [], [coverage(0, 2)], [MON])
	assert days[0].cells[0].exclusive_capacity == 0
	assert days[0].cells[0].severity(scaled=True) == SEV_OK
	assert days[0].cells[0].severity(scaled=False) == SEV_CRITICAL


def test_a_holders_own_mode_override_makes_the_role_exclusive_here():
	data = pkg(
		role_mode_overrides={("E1", "R1"): MODE_EXCLUSIVE},
		rooms={(DISC, BRANCH): 2},
	)
	days = window_findings(data, [slot()], [coverage(1, 2)], [MON])
	assert days[0].cells[0].exclusive_capacity == 1


def test_a_multi_room_exclusive_holder_supplies_their_max_rooms():
	data = pkg(
		role_mode={"R1": MODE_EXCLUSIVE},
		max_rpe={("E1", "R1"): 2},
		rooms={(DISC, BRANCH): 3},
	)
	days = window_findings(data, [slot()], [coverage(2, 3)], [MON])
	assert days[0].cells[0].exclusive_capacity == 2


def test_a_non_gating_exclusive_role_does_not_cap_anything():
	"""An administrative post is worked exclusively and opens no room."""
	data = pkg(
		role_mode={"R1": MODE_EXCLUSIVE},
		role_gates_rooms={"R1": False},
		rooms={(DISC, BRANCH): 2},
	)
	days = window_findings(data, [slot()], [coverage(0, 2)], [MON])
	assert days[0].cells[0].exclusive_capacity == 2


# ── building a window's findings ──────────────────────────────────────────────


def test_every_day_of_the_window_gets_a_day_even_outside_the_horizon():
	"""A day nobody works is still a square on the calendar."""
	data = pkg(working_days=[MON])
	days = window_findings(data, [slot()], [coverage(1, 1)], days_from(7))
	assert [day.date for day in days] == days_from(7)
	assert [day.worked for day in days] == [True] + [False] * 6


def test_a_zero_staffed_coverage_row_is_reported_not_dropped():
	data = pkg(working_days=[MON], rooms={(DISC, BRANCH): 2})
	days = window_findings(data, [], [coverage(0, 2)], [MON])
	assert day_severity(days[0], scaled=False) == SEV_CRITICAL


def test_under_fte_is_filed_under_the_days_the_employee_was_free_and_nowhere():
	data = pkg(working_days=days_from(2), target_shifts={"E1": 2})
	days = window_findings(
		data, [slot(date=MON)], [coverage(1, 1), coverage(0, 1, date=days_from(2)[1])], days_from(2)
	)
	assert days[0].under_fte == ()  # they worked that day
	assert days[1].under_fte == (("E1", 1, (DISC,)),)


def test_under_fte_is_counted_in_presences_so_a_collateral_duty_does_not_pay_twice():
	data = pkg(
		roles=["R1", "C1"],
		role_discipline={"R1": DISC, "C1": DISC},
		employee_roles={"E1": ("R1", "C1")},
		max_rpe={("E1", "R1"): 1, ("E1", "C1"): 1},
		target_shifts={"E1": 2},
		working_days=days_from(2),
	)
	# Two rows, one half-day: the lead duty rides on the shift beside it.
	days = window_findings(
		data,
		[slot(role="R1", date=MON), slot(role="C1", date=MON)],
		[coverage(1, 1)],
		days_from(2),
	)
	assert days[1].under_fte == (("E1", 1, (DISC,)),)


def test_a_day_the_employee_is_unavailable_on_carries_none_of_their_deficit():
	tuesday = days_from(2)[1]
	data = pkg(
		working_days=days_from(2),
		target_shifts={"E1": 2},
		employee_days={"E1": frozenset({MON})},
	)
	days = window_findings(data, [], [], days_from(2))
	assert days[0].under_fte == (("E1", 2, (DISC,)),)
	assert days[1].under_fte == ()
	assert days[1].date == tuesday


def test_binding_conflicts_and_unresolved_assignments_land_on_their_own_day():
	tuesday = days_from(2)[1]
	data = pkg(
		working_days=days_from(2),
		binding_conflicts=(("E1", "R1", "AM", MON, BRANCH),),
		unresolved_assignments=(("E1", tuesday, "no discipline on the location"),),
	)
	days = window_findings(data, [], [], days_from(2))
	assert days[0].conflicts[0][0] == "binding"
	assert days[0].conflicts[0][3] == DISC
	assert days[1].conflicts[0][0] == "unresolved"
	assert days[1].conflicts[0][3] == ""  # nothing placed it, so nothing names its discipline


# ── a window that never solved ────────────────────────────────────────────────


def test_a_failed_window_paints_every_one_of_its_days():
	days = tuple(Day(day, worked=True) for day in days_from(7))
	assert {square["severity"] for square in window_days(days, "Failed", scaled=False)} == {SEV_FAILED}
	assert window_severity(days, "Timed Out", scaled=False) == SEV_FAILED


def test_a_solved_window_reports_its_worst_day():
	days = (
		Day(MON, worked=True, cells=(cell(2, 2),)),
		Day(days_from(2)[1], worked=True, cells=(cell(0, 2),)),
	)
	assert window_severity(days, "Solved", scaled=False) == SEV_CRITICAL


# ── narrowing ─────────────────────────────────────────────────────────────────


def test_an_empty_selection_is_every_band():
	day = Day(MON, worked=True, cells=(cell(1, 2), cell(1, 2, discipline="D2")))
	assert narrow(day, (), ()) is day


def test_narrowing_drops_the_other_bands_cells_and_with_them_its_verdict():
	day = Day(MON, worked=True, cells=(cell(2, 2), cell(0, 2, discipline="D2")))
	assert day_severity(day, scaled=False) == SEV_CRITICAL
	assert day_severity(narrow(day, [DISC], []), scaled=False) == SEV_OK


def test_narrowing_by_branch_keeps_the_same_disciplines_other_branch_out():
	day = Day(MON, worked=True, cells=(cell(2, 2), cell(0, 2, branch="B2")))
	assert day_severity(narrow(day, [], [BRANCH]), scaled=False) == SEV_OK


def test_narrowing_keeps_an_employee_finding_naming_one_of_the_shown_disciplines():
	day = Day(MON, worked=True, under_fte=(("E1", 1, (DISC, "D2")), ("E2", 1, ("D2",))))
	assert narrow(day, [DISC], []).under_fte == (("E1", 1, (DISC, "D2")),)


def test_narrowing_never_swallows_a_finding_that_names_no_discipline():
	day = Day(MON, worked=True, conflicts=(("unresolved", "E1", "nothing placed it", ""),))
	assert narrow(day, ["D2"], []).conflicts == day.conflicts


# ── storage ───────────────────────────────────────────────────────────────────


def test_findings_survive_a_round_trip():
	days = (
		Day(
			MON,
			worked=True,
			cells=(cell(1, 2, exclusive=1),),
			under_fte=(("E1", 2, (DISC,)),),
			conflicts=(("binding", "E1", "dropped for leave", DISC),),
		),
		Day(days_from(2)[1], worked=False),
	)
	assert loads(dumps(days)) == days


def test_a_payload_from_another_version_is_read_as_no_findings_rather_than_guessed_at():
	payload = dumps(())
	payload["version"] = 99
	assert loads(payload) == ()
	assert loads(None) == ()


def test_day_detail_itemises_what_the_summary_counts():
	day = Day(
		MON,
		worked=True,
		cells=(cell(2, 2), cell(0, 2, shift_type="PM")),
		under_fte=(("E1", 1, (DISC,)),),
		conflicts=(("binding", "E1", "x", DISC),),
	)
	detail = day_detail(day, scaled=False)
	assert detail["under_fte"] == len(detail["under_fte_rows"]) == 1
	assert detail["conflicts"] == len(detail["conflict_rows"]) == 1
	# Worst first, so the panel opens on the thing worth reading.
	assert detail["cells"][0]["severity"] == SEV_CRITICAL


# ── the calendar the days are drawn on ────────────────────────────────────────


def test_month_grids_cover_every_month_the_horizon_touches():
	grids = month_grids(datetime.date(2026, 11, 20), datetime.date(2027, 1, 4))
	assert [(grid["year"], grid["month"]) for grid in grids] == [(2026, 11), (2026, 12), (2027, 1)]


def test_a_month_grid_holds_each_of_its_days_exactly_once_and_nobody_elses():
	grid = month_grids(datetime.date(2026, 2, 1), datetime.date(2026, 2, 28))[0]
	dates = [day for week in grid["weeks"] for day in week if day]
	assert len(dates) == len(set(dates)) == 28
	assert all(date.startswith("2026-02") for date in dates)


def test_a_month_grids_weeks_start_on_monday():
	grid = month_grids(datetime.date(2026, 6, 1), datetime.date(2026, 6, 30))[0]
	assert grid["weeks"][0][0] == "2026-06-01"  # 1 June 2026 is a Monday
	for week in grid["weeks"]:
		for index, date in enumerate(week):
			if date:
				assert datetime.date.fromisoformat(date).weekday() == index
