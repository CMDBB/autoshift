# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Expansion tests for a Shift Schedule's cycle.

Pure Python, no Frappe and no site, the same bargain `test_optimizer.py` and
`test_wallchart.py` make. `rota/cycle.py` re-implements HRMS's own
`create_shifts` because that one collapses any cycle longer than a week (see
`autoshift.rota`), so the thing worth pinning here is the phase: a fortnightly
schedule must fire on alternate weeks and keep firing on the *same* alternate
weeks however far out you ask. Neutral placeholders throughout (`E1`, `B1`) —
see CLAUDE.md, "App boundary".
"""

import datetime

import pytest

from autoshift.rota.cycle import FREQUENCY_WEEKS, WEEKDAY_INDEX, Rota, monday_of, occurrences

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

#: A Monday, so the anchor below is the Sunday before it.
WEEK_1 = datetime.date(2026, 8, 31)
WEEK_2 = WEEK_1 + datetime.timedelta(weeks=1)
WEEK_3 = WEEK_1 + datetime.timedelta(weeks=2)
WEEK_5 = WEEK_1 + datetime.timedelta(weeks=4)

#: zawin2frappe anchors a schedule on the Sunday before the first week it covers.
ANCHOR = WEEK_1 - datetime.timedelta(days=1)


def rota(weekdays, cycle=1, anchor=ANCHOR, shift_type="AM") -> Rota:
	return Rota(
		assignment="SSA1",
		employee="E1",
		company="C1",
		shift_type=shift_type,
		shift_location="L1",
		weekdays=frozenset(weekdays),
		cycle_weeks=cycle,
		anchor=anchor,
	)


def weeks_of(days) -> list[datetime.date]:
	return sorted({monday_of(day) for day in days})


def test_weekly_schedule_fires_every_week():
	days = occurrences(
		rota([MON, WED]), WEEK_1, WEEK_1 + datetime.timedelta(weeks=4) - datetime.timedelta(days=1)
	)
	assert weeks_of(days) == [WEEK_1, WEEK_2, WEEK_3, WEEK_1 + datetime.timedelta(weeks=3)]
	assert {day.weekday() for day in days} == {MON, WED}


def test_fortnightly_schedule_fires_on_alternate_weeks():
	days = occurrences(rota([TUE], cycle=2), WEEK_1, WEEK_5 + datetime.timedelta(days=6))
	assert weeks_of(days) == [WEEK_1, WEEK_3, WEEK_5]


def test_phase_holds_however_far_out_you_ask():
	"""The bug this replaces: HRMS re-anchors mid-pattern and drifts toward weekly.

	Asking for the whole span at once and asking week by week must agree, because
	nothing here carries state between calls.
	"""
	settled = rota([THU], cycle=4)
	whole = occurrences(settled, WEEK_1, WEEK_1 + datetime.timedelta(weeks=13) - datetime.timedelta(days=1))
	piecewise = [
		day
		for index in range(13)
		for day in occurrences(
			settled,
			WEEK_1 + datetime.timedelta(weeks=index),
			WEEK_1 + datetime.timedelta(weeks=index, days=6),
		)
	]
	assert whole == piecewise
	assert weeks_of(whole) == [
		WEEK_1,
		WEEK_5,
		WEEK_1 + datetime.timedelta(weeks=8),
		WEEK_1 + datetime.timedelta(weeks=12),
	]


def test_the_anchor_is_a_handover_boundary():
	"""Nothing is generated on or before `create_shifts_after`.

	Everything up to it belongs to whoever wrote the records already on the books
	— the import, usually — and generating over them would double-book the week.
	"""
	late = ANCHOR + datetime.timedelta(days=10)
	days = occurrences(rota(range(7), anchor=late), WEEK_1, WEEK_3 + datetime.timedelta(days=6))
	assert days and min(days) == late + datetime.timedelta(days=1)


def test_a_window_entirely_before_the_boundary_yields_nothing():
	assert occurrences(rota([MON], anchor=WEEK_3), WEEK_1, WEEK_2) == []


def test_a_schedule_with_no_weekdays_yields_nothing():
	assert occurrences(rota([]), WEEK_1, WEEK_5) == []


def test_an_empty_window_yields_nothing():
	assert occurrences(rota([MON]), WEEK_2, WEEK_1) == []


def test_without_an_anchor_the_window_supplies_the_phase():
	"""A hand-made schedule with no `create_shifts_after` still has to pick a phase.

	The window's own first week is the only defensible one: there is nothing else
	to count from, and it makes the first week asked for a week that fires.
	"""
	days = occurrences(rota([WED], cycle=2, anchor=None), WEEK_1, WEEK_5 + datetime.timedelta(days=6))
	assert weeks_of(days) == [WEEK_1, WEEK_3, WEEK_5]


def test_a_window_starting_mid_cycle_keeps_the_anchor_s_phase():
	"""Asking about week 2 of a fortnightly rota answers "nothing", not "everything".

	The phase is counted off the anchor, so a window that opens on an off week has
	to stay off — this is precisely where re-anchoring goes wrong.
	"""
	settled = rota([TUE], cycle=2)
	assert occurrences(settled, WEEK_2, WEEK_2 + datetime.timedelta(days=6)) == []
	assert occurrences(settled, WEEK_3, WEEK_3 + datetime.timedelta(days=6)) == [
		WEEK_3 + datetime.timedelta(days=1)
	]


@pytest.mark.parametrize("cycle", [0, None])
def test_a_missing_cycle_length_reads_as_weekly(cycle):
	days = occurrences(rota([FRI], cycle=cycle), WEEK_1, WEEK_3 + datetime.timedelta(days=6))
	assert weeks_of(days) == [WEEK_1, WEEK_2, WEEK_3]


def test_frequency_and_weekday_tables_match_hrms():
	"""These two are read straight off HRMS's own field options.

	`Shift Schedule.frequency` is a Select and `Assignment Rule Day` a Link table;
	a value drifting out of step here would silently mis-date a whole rota.
	"""
	assert FREQUENCY_WEEKS == {
		"Every Week": 1,
		"Every 2 Weeks": 2,
		"Every 3 Weeks": 3,
		"Every 4 Weeks": 4,
	}
	assert [name for name, _ in sorted(WEEKDAY_INDEX.items(), key=lambda kv: kv[1])] == [
		"Monday",
		"Tuesday",
		"Wednesday",
		"Thursday",
		"Friday",
		"Saturday",
		"Sunday",
	]


def test_is_rota_marks_what_hrms_cannot_run():
	assert not rota([MON]).is_rota
	assert rota([MON], cycle=2).is_rota
