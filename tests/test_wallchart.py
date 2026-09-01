# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Placement tests for the week wall chart.

Pure Python, no Frappe and no site, the same bargain `test_optimizer.py` makes:
`wallchart/chart.py` takes a layout and a list of slots and decides nothing else,
so everything worth asserting about the chart can be asserted here. Neutral
placeholders throughout (`E1`, `B1`, `D1`) — see CLAUDE.md, "App boundary".
"""

import datetime

import pytest

from autoshift.wallchart.chart import (
	KIND_ADDED,
	KIND_DROPPED,
	KIND_EXISTING,
	KIND_KEPT,
	OVERFLOW,
	Band,
	Lane,
	Layout,
	Section,
	Slot,
	build,
	merge,
	monday_of,
	week_dates,
)

MONDAY = datetime.date(2026, 8, 31)
TUESDAY = datetime.date(2026, 9, 1)
SATURDAY = datetime.date(2026, 9, 5)

AM = "AM"
PM = "PM"

PRACTITIONER = Lane("R1", "Practitioner")
ASSISTANT = Lane("R2", "Assistant")


def band(key="C1", branch="B1", discipline="D1", rooms=2, lanes=(PRACTITIONER, ASSISTANT), shifts=(AM, PM)):
	return Band(
		key=key,
		branch=branch,
		discipline=discipline,
		branch_label=branch,
		discipline_label=discipline,
		rooms=rooms,
		lanes=tuple(lanes),
		shift_types=frozenset(shifts),
	)


def layout(*bands, sections=(AM, PM)):
	return Layout(
		sections=tuple(Section(s, s) for s in sections),
		bands=tuple(bands) or (band(),),
	)


def slot(employee="E1", day=MONDAY, shift=AM, role="R1", branch="B1", **kwargs):
	return Slot(
		date=day,
		shift_type=shift,
		employee=employee,
		employee_name=kwargs.pop("employee_name", employee),
		label=kwargs.pop("label", employee),
		branch=branch,
		scheduling_role=role,
		**kwargs,
	)


# ── week arithmetic ──────────────────────────────────────────────────────────


def test_monday_of_is_idempotent_on_a_monday():
	assert monday_of(MONDAY) == MONDAY
	assert monday_of(SATURDAY) == MONDAY


def test_a_week_is_always_seven_days():
	"""The chart's width never changes, so a weekend is dimmed rather than dropped."""
	assert len(week_dates(MONDAY)) == 7
	assert week_dates(MONDAY)[-1] == MONDAY + datetime.timedelta(days=6)


# ── placement ────────────────────────────────────────────────────────────────


def test_a_slot_lands_in_its_branch_and_role():
	chart = build(layout(), [slot()], MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E1"]
	assert chart.cell(AM, "C1", 1, "R2", 0) == []
	assert chart.cell(PM, "C1", 1, "R1", 0) == []


def test_a_band_is_at_least_as_tall_as_its_room_count():
	"""Empty rows are the point: an unstaffed room has to be visible as a gap."""
	chart = build(layout(band(rooms=4)), [slot()], MONDAY)
	assert chart.height(AM, "C1") == 4


def test_a_band_grows_past_its_rooms_rather_than_hiding_anybody():
	slots = [slot(employee=f"E{n}") for n in range(1, 4)]
	chart = build(layout(band(rooms=1)), slots, MONDAY)
	assert chart.height(AM, "C1") == 3
	assert len(chart.placements) == 3
	assert any("only 1 rooms configured" in w for w in chart.warnings)


def test_rows_stack_per_lane_independently():
	"""Nothing pairs the lanes, so row 1 of each fills from its own people."""
	slots = [slot(employee="E1", role="R1"), slot(employee="E2", role="R2")]
	chart = build(layout(), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E1"]
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R2", 0)] == ["E2"]


def test_people_in_one_cell_are_ordered_by_label_not_by_input():
	slots = [slot(employee="E2", label="ZZ"), slot(employee="E1", label="AA")]
	chart = build(layout(), slots, MONDAY)
	assert [s.label for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["AA"]
	assert [s.label for s in chart.cell(AM, "C1", 2, "R1", 0)] == ["ZZ"]


def test_days_are_indexed_from_monday():
	chart = build(layout(), [slot(day=TUESDAY)], MONDAY)
	assert chart.cell(AM, "C1", 1, "R1", 0) == []
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 1)] == ["E1"]


def test_a_weekend_slot_still_gets_a_column():
	chart = build(layout(), [slot(day=SATURDAY)], MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 5)] == ["E1"]
	assert not chart.warnings


def test_a_slot_outside_the_week_is_counted_not_dropped_silently():
	chart = build(layout(), [slot(day=MONDAY + datetime.timedelta(days=8))], MONDAY)
	assert not chart.placements
	assert any("outside this week" in w for w in chart.warnings)


# ── which sections a band appears in ─────────────────────────────────────────


def test_a_band_is_only_drawn_in_the_shift_types_its_config_lists():
	chart = build(layout(band(shifts=(AM,))), [slot(shift=AM)], MONDAY)
	assert chart.height(AM, "C1") == 2
	assert chart.height(PM, "C1") == 0


def test_a_slot_in_a_shift_the_band_does_not_list_goes_to_overflow():
	chart = build(layout(band(shifts=(AM,))), [slot(shift=PM)], MONDAY)
	assert chart.height(PM, OVERFLOW) == 1
	assert any("does not list PM as a Shift Type" in w for w in chart.warnings)


def test_a_shift_type_no_section_covers_is_reported():
	chart = build(layout(sections=(AM,)), [slot(shift=PM)], MONDAY)
	assert not chart.placements
	assert any("no table for them" in w for w in chart.warnings)


# ── overflow ─────────────────────────────────────────────────────────────────


def test_a_role_no_band_lists_lands_in_overflow_with_a_reason():
	chart = build(layout(), [slot(role="R9")], MONDAY)
	assert [s.employee for s in chart.cell(AM, OVERFLOW, 1, "R9", 0)] == ["E1"]
	assert any("R9 at B1 matches no Discipline Branch Config" in w for w in chart.warnings)


def test_a_branch_no_band_covers_lands_in_overflow():
	chart = build(layout(), [slot(branch="B9")], MONDAY)
	assert chart.height(AM, OVERFLOW) == 1
	assert any("at B9" in w for w in chart.warnings)


def test_overflow_lanes_are_the_roles_that_could_not_be_placed():
	chart = build(layout(), [slot(role="R9"), slot(employee="E2", role=None)], MONDAY)
	assert [lane.key for lane in chart.overflow_lanes] == ["(no role)", "R9"]


def test_overflow_is_absent_when_everything_placed():
	chart = build(layout(), [slot()], MONDAY)
	assert chart.overflow_lanes == ()
	assert chart.height(AM, OVERFLOW) == 0


# ── two bands ────────────────────────────────────────────────────────────────


def test_two_branches_of_one_discipline_are_separate_bands():
	first = band(key="C1", branch="B1")
	second = band(key="C2", branch="B2")
	slots = [slot(branch="B1"), slot(employee="E2", branch="B2")]
	chart = build(layout(first, second), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E1"]
	assert [s.employee for s in chart.cell(AM, "C2", 1, "R1", 0)] == ["E2"]


def test_two_disciplines_at_one_branch_are_separate_bands():
	first = band(key="C1", discipline="D1", lanes=(PRACTITIONER,))
	second = band(key="C2", discipline="D2", lanes=(Lane("R3", "Steriliser"),))
	slots = [slot(role="R1"), slot(employee="E2", role="R3")]
	chart = build(layout(first, second), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E1"]
	assert [s.employee for s in chart.cell(AM, "C2", 1, "R3", 0)] == ["E2"]


# ── merging a run against the books ──────────────────────────────────────────


def test_without_a_run_everything_reads_as_on_the_books():
	merged = merge([slot()], [])
	assert [s.kind for s in merged] == [KIND_EXISTING]


def test_a_reproduced_assignment_is_kept():
	existing = slot()
	proposed = slot(kind=KIND_ADDED)
	assert [s.kind for s in merge([existing], [proposed])] == [KIND_KEPT]


def test_a_proposal_with_nothing_on_the_books_is_added():
	assert [s.kind for s in merge([], [slot(kind=KIND_ADDED)])] == [KIND_ADDED]


def test_an_assignment_the_run_did_not_reproduce_is_dropped():
	"""The interesting verdict: a settled half-day the ruleset re-planned away."""
	merged = merge([slot()], [slot(employee="E2", kind=KIND_ADDED)])
	assert sorted((s.employee, s.kind) for s in merged) == [
		("E1", KIND_DROPPED),
		("E2", KIND_ADDED),
	]


def test_matching_ignores_the_role_so_an_inference_is_not_a_false_re_plan():
	"""A Shift Assignment records no role, so `source` guesses one. Matching on
	that guess would report a drop and an add every time it guessed differently
	from the solver, which is a claim about the inference, not the schedule."""
	existing = slot(role="R2", role_certain=False)
	merged = merge([existing], [slot(role="R1", kind=KIND_ADDED)])
	assert [s.kind for s in merged] == [KIND_KEPT]
	assert merged[0].scheduling_role == "R1"
	assert merged[0].changed is None


def test_a_move_is_reported_on_the_kept_slot_not_as_a_drop_plus_an_add():
	existing = slot(branch="B2")
	merged = merge([existing], [slot(branch="B1", kind=KIND_ADDED)])
	assert [s.kind for s in merged] == [KIND_KEPT]
	assert merged[0].changed == "was at B2"


def test_a_role_change_is_reported_when_the_books_were_sure_of_the_role():
	existing = slot(role="R2", role_certain=True)
	merged = merge([existing], [slot(role="R1", kind=KIND_ADDED)])
	assert merged[0].changed == "was as R2"


def test_a_dropped_slot_is_still_placed_so_it_can_be_seen():
	merged = merge([slot()], [slot(employee="E2", kind=KIND_ADDED)])
	chart = build(layout(), merged, MONDAY)
	kinds = {s.employee: s.kind for s in chart.cell(AM, "C1", 1, "R1", 0) + chart.cell(AM, "C1", 2, "R1", 0)}
	assert kinds == {"E1": KIND_DROPPED, "E2": KIND_ADDED}


# ── inferred roles ───────────────────────────────────────────────────────────


def test_inferred_placements_are_counted_out_loud():
	chart = build(layout(), [slot(role_certain=False)], MONDAY)
	assert any("inferred from the employee's held roles" in w for w in chart.warnings)


def test_a_certain_placement_raises_no_inference_warning():
	chart = build(layout(), [slot()], MONDAY)
	assert not chart.warnings


# ── stability ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_placement_does_not_depend_on_input_order(order):
	slots = [
		slot(employee="E1", label="AA"),
		slot(employee="E2", label="BB"),
		slot(employee="E3", label="CC"),
	]
	chart = build(layout(band(rooms=3)), [slots[i] for i in order], MONDAY)
	assert [s.label for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["AA"]
	assert [s.label for s in chart.cell(AM, "C1", 2, "R1", 0)] == ["BB"]
	assert [s.label for s in chart.cell(AM, "C1", 3, "R1", 0)] == ["CC"]


def test_an_empty_layout_places_everything_in_overflow():
	"""A site with no Discipline Branch Config still gets its people shown."""
	chart = build(Layout(sections=(Section(AM, AM),), bands=()), [slot()], MONDAY)
	assert chart.height(AM, OVERFLOW) == 1
