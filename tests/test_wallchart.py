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
	assert any("only 1 are configured" in w for w in chart.warnings)


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


# ── chip sort (`Scheduling Role.chip_sort_field`) ────────────────────────────


def test_chip_order_follows_sort_value_over_label_when_configured():
	slots = [slot(employee="E1", label="ZZ", sort_value=2), slot(employee="E2", label="AA", sort_value=1)]
	chart = build(layout(), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E2"]
	assert [s.employee for s in chart.cell(AM, "C1", 2, "R1", 0)] == ["E1"]


def test_chip_order_reverses_when_the_lane_is_descending():
	descending = Lane("R1", "Practitioner", sort_descending=True)
	slots = [slot(employee="E1", label="ZZ", sort_value=2), slot(employee="E2", label="AA", sort_value=1)]
	chart = build(layout(band(lanes=(descending, ASSISTANT))), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E1"]
	assert [s.employee for s in chart.cell(AM, "C1", 2, "R1", 0)] == ["E2"]


def test_chips_with_no_sort_value_fall_after_ranked_ones_alphabetically():
	"""A role with no `chip_sort_field`, or an employee missing the value, still
	prints a stable row rather than raising or being dropped."""
	slots = [slot(employee="E1", label="AA"), slot(employee="E2", label="ZZ", sort_value=5)]
	chart = build(layout(), slots, MONDAY)
	assert [s.employee for s in chart.cell(AM, "C1", 1, "R1", 0)] == ["E2"]
	assert [s.employee for s in chart.cell(AM, "C1", 2, "R1", 0)] == ["E1"]


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


def test_a_kept_slot_is_virtual_exactly_when_its_book_side_is():
	"""Whether a half-day is recorded yet is a fact about the books, not the run: a run
	reproducing an unrecorded rota day is still showing a day nobody has written down."""
	proposed = slot(kind=KIND_ADDED)
	assert [s.virtual for s in merge([slot(virtual=True)], [proposed])] == [True]
	assert [s.virtual for s in merge([slot()], [proposed])] == [False]


def test_a_dropped_rota_day_stays_virtual_and_an_added_one_is_not():
	merged = merge([slot(virtual=True)], [slot(employee="E2", kind=KIND_ADDED)])
	assert sorted((s.employee, s.kind, s.virtual) for s in merged) == [
		("E1", KIND_DROPPED, True),
		("E2", KIND_ADDED, False),
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


# ── rooms covered: chip height and what counts as an open room ────────────────


LEAD = Lane("RC", "Lead", gates_rooms=False)


def placement(chart, row, lane, day=0, shift=AM, band_key="C1"):
	return next(
		(
			p
			for p in chart.placements
			if p.section == shift
			and p.band == band_key
			and p.row == row
			and p.lane == lane
			and p.day_index == day
		),
		None,
	)


def test_a_chip_is_as_tall_as_the_rooms_it_covers():
	chart = build(layout(band(rooms=3)), [slot(rooms=2)], MONDAY)
	assert placement(chart, 1, "R1").span == 2


def test_the_next_person_in_a_lane_starts_below_the_chip_above():
	slots = [slot(employee="E1", rooms=2), slot(employee="E2", rooms=1)]
	chart = build(layout(band(rooms=4)), slots, MONDAY)
	assert placement(chart, 1, "R1").slot.employee == "E1"
	assert placement(chart, 2, "R1") is None  # covered by the chip above it
	assert placement(chart, 3, "R1").slot.employee == "E2"


def test_a_kept_chip_is_as_tall_as_the_rooms_the_run_measured():
	"""The books only know a holder's ceiling; a run under `room_load_objective` knows the
	rooms they take, and a kept half-day is drawn at the run's figure, ceiling kept aside."""
	existing = slot(rooms=3)
	proposed = slot(kind=KIND_ADDED, rooms=2, max_rooms=3)
	(kept,) = merge([existing], [proposed])
	chart = build(layout(band(rooms=3)), [kept], MONDAY)
	assert placement(chart, 1, "R1").span == 2
	assert placement(chart, 1, "R1").slot.max_rooms == 3


def test_a_band_grows_for_spans_as_well_as_for_heads():
	"""Two people covering two rooms each need four lines, however few heads that is."""
	slots = [slot(employee="E1", rooms=2), slot(employee="E2", rooms=2)]
	chart = build(layout(band(rooms=2)), slots, MONDAY)
	assert chart.height(AM, "C1") == 4
	assert any("covers 4 rooms" in w for w in chart.warnings)


def test_a_room_is_covered_only_where_every_gating_lane_reaches_it():
	"""One practitioner over two rooms, one assistant: one room is open, one is half-staffed."""
	slots = [slot(employee="E1", role="R1", rooms=2), slot(employee="E2", role="R2", rooms=1)]
	chart = build(layout(band(rooms=2)), slots, MONDAY)
	assert chart.covered_rooms(AM, "C1", 0) == 1


def test_a_lane_nobody_fills_leaves_every_room_uncovered():
	chart = build(layout(band(rooms=2)), [slot(employee="E1", role="R1", rooms=2)], MONDAY)
	assert chart.covered_rooms(AM, "C1", 0) == 0


def test_a_non_gating_lane_neither_opens_a_room_nor_holds_one_shut():
	"""A lead duty is drawn like anything else; the rooms are decided without it."""
	lanes = (PRACTITIONER, ASSISTANT, LEAD)
	slots = [
		slot(employee="E1", role="R1"),
		slot(employee="E2", role="R2"),
		slot(employee="E3", role="RC", rooms=3),
	]
	chart = build(layout(band(rooms=2, lanes=lanes)), slots, MONDAY)
	assert chart.covered_rooms(AM, "C1", 0) == 1  # the practitioner and the assistant, not the lead
	assert placement(chart, 1, "RC").span == 3  # still drawn over the rooms it oversees


def test_a_band_with_no_gating_lane_covers_nothing():
	chart = build(layout(band(rooms=2, lanes=(LEAD,))), [slot(employee="E1", role="RC")], MONDAY)
	assert chart.covered_rooms(AM, "C1", 0) == 0


def test_coverage_is_counted_per_day():
	slots = [
		slot(employee="E1", role="R1", day=MONDAY),
		slot(employee="E2", role="R2", day=MONDAY),
		slot(employee="E3", role="R1", day=TUESDAY),
	]
	chart = build(layout(band(rooms=2)), slots, MONDAY)
	assert chart.covered_rooms(AM, "C1", 0) == 1
	assert chart.covered_rooms(AM, "C1", 1) == 0  # Tuesday has no assistant


def test_a_merged_slot_keeps_the_rooms_it_covers():
	proposed = [slot(employee="E1", rooms=2)]
	merged = merge([slot(employee="E1", rooms=2)], proposed)
	assert [s.rooms for s in merged] == [2]


def test_a_dropped_slot_sinks_below_the_proposal_and_covers_nothing():
	"""What the run sent home does not staff a room, and must not sit above what does."""
	existing = [slot(employee="E1", role="R1"), slot(employee="E2", role="R2")]
	proposed = [slot(employee="E9", role="R1", kind=KIND_ADDED), slot(employee="E2", role="R2")]
	chart = build(layout(band(rooms=2)), merge(existing, proposed), MONDAY)

	assert placement(chart, 1, "R1").slot.employee == "E9"  # the proposal, alphabetically later
	assert placement(chart, 2, "R1").slot.kind == KIND_DROPPED
	assert chart.covered_rooms(AM, "C1", 0) == 1


# ── solved room_index: real pairing and numbering ──────────────────────────────


def test_slots_sharing_a_solved_room_index_land_on_the_same_row():
	"""The pairing itself: two lanes matched into the same room by the solver."""
	slots = [
		slot(employee="E1", role="R1", room_index=(3,)),
		slot(employee="E2", role="R2", room_index=(3,)),
	]
	chart = build(layout(band(rooms=3)), slots, MONDAY)
	assert placement(chart, 3, "R1").slot.employee == "E1"
	assert placement(chart, 3, "R2").slot.employee == "E2"


def test_a_solved_room_index_is_the_row_it_draws_at():
	"""Room 3 is line 3, not "the second one that happened to be used"."""
	slots = [
		slot(employee="E1", role="R1", room_index=(3,)),
		slot(employee="E2", role="R1", room_index=(1,)),
	]
	chart = build(layout(band(rooms=3)), slots, MONDAY)
	assert placement(chart, 1, "R1").slot.employee == "E2"
	assert placement(chart, 3, "R1").slot.employee == "E1"


def test_a_room_nobody_was_matched_into_stays_an_empty_line():
	"""Every room of a band is interchangeable, so a solve that leaves room 1 shut and
	works rooms 2 and 3 is co-optimal with any other pick — and closing the gap up
	would draw a schedule the solver did not produce. The hole is the answer."""
	slots = [
		slot(employee="E1", role="R1", room_index=(2,)),
		slot(employee="E2", role="R2", room_index=(2,)),
		slot(employee="E3", role="R1", room_index=(3,)),
		slot(employee="E4", role="R2", room_index=(3,)),
	]
	chart = build(layout(band(rooms=3)), slots, MONDAY)
	assert placement(chart, 1, "R1") is None
	assert placement(chart, 1, "R2") is None
	assert chart.covered_rows(AM, "C1", 0) == (2, 3)  # open rooms, not "the top two"
	assert chart.covered_rooms(AM, "C1", 0) == 2


def test_a_noncontiguous_multi_room_slot_falls_back_to_incidental_placement():
	"""Nothing ties a multi-room holder's rooms together, so a chip that would have to
	be drawn with a gap in it (rooms 1 and 3) is not pinned at all — the slot is
	placed exactly as it would be with no room_index."""
	slots = [
		slot(employee="E1", role="R1", rooms=2, room_index=(1, 3)),
		slot(employee="E2", role="R2", room_index=(2,)),
	]
	chart = build(layout(band(rooms=3)), slots, MONDAY)
	placed = placement(chart, 1, "R1")
	assert placed.slot.employee == "E1"
	assert placed.span == 2


def test_lanes_matched_into_different_rooms_open_neither_of_them():
	"""The practitioner is in room 1 and the assistant in room 3: two half-staffed
	lines, not one open room. Coverage is per row, so a count taken from the top
	would have called this a room."""
	slots = [
		slot(employee="E1", role="R1", room_index=(1,)),
		slot(employee="E2", role="R2", room_index=(3,)),
	]
	chart = build(layout(band(rooms=3)), slots, MONDAY)
	assert placement(chart, 1, "R1").slot.employee == "E1"
	assert placement(chart, 3, "R2").slot.employee == "E2"
	assert chart.covered_rows(AM, "C1", 0) == ()


def test_an_unpinned_slot_fills_around_a_pinned_row():
	"""A solved pairing at room 2 does not stop a second, unmeasured assignment in the
	same lane from taking the line above it."""
	slots = [
		slot(employee="E1", role="R1", room_index=(2,)),
		slot(employee="E2", role="R1"),
	]
	chart = build(layout(band(rooms=2)), slots, MONDAY)
	assert placement(chart, 2, "R1").slot.employee == "E1"
	assert placement(chart, 1, "R1").slot.employee == "E2"


def test_merge_keeps_the_room_index_on_a_kept_slot():
	proposed = [slot(employee="E1", room_index=(3,))]
	merged = merge([slot(employee="E1")], proposed)
	assert merged[0].room_index == (3,)
