# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Tests for `rota/calendar.py` — an employee's own holiday calendar, derived from the
days their rota puts them on.

Pure Python, no Frappe, same bargain as `test_rota.py`. Neutral placeholders throughout
— see CLAUDE.md, "App boundary".
"""

import datetime

from autoshift.rota.calendar import NON_WORKING, Row, derive, digest, first_difference

#: A Monday, and a fortnight to look at.
WEEK_1 = datetime.date(2026, 8, 31)
LAST = WEEK_1 + datetime.timedelta(days=13)

XMAS = datetime.date(2026, 9, 2)  # not really, but a Wednesday inside the window


def days(*offsets) -> set[datetime.date]:
	return {WEEK_1 + datetime.timedelta(days=o) for o in offsets}


def base(*rows) -> dict:
	return {r.date: r for r in rows}


def dates_of(rows) -> list[datetime.date]:
	return [r.date for r in rows]


def test_every_unworked_day_becomes_a_holiday():
	"""The whole point: a four-day week must show its fifth day as a holiday, or HR
	charges a full week of leave for it."""
	worked = days(0, 1, 2, 3, 7, 8, 9, 10)  # Mon-Thu, both weeks

	rows = derive(WEEK_1, LAST, worked, base())

	assert dates_of(rows) == sorted(days(4, 5, 6, 11, 12, 13))
	assert all(r.description == NON_WORKING and r.weekly_off for r in rows)


def test_weekends_need_no_special_case():
	"""A rota never covers Saturday or Sunday, so they fall out of the same subtraction
	as any other unworked day — there is no weekend concept here at all."""
	worked = days(*range(5)) | days(*range(7, 12))

	rows = derive(WEEK_1, LAST, worked, base())

	assert dates_of(rows) == sorted(days(5, 6, 12, 13))


def test_a_company_holiday_is_carried_through_verbatim():
	"""Description and weekly_off come from the company calendar, not from us."""
	public = Row(date=XMAS, description="Public holiday", weekly_off=False)
	worked = days(*range(5)) | days(*range(7, 12))

	rows = derive(WEEK_1, LAST, worked, base(public))

	assert public in rows
	assert [r for r in rows if r.date == XMAS] == [public]


def test_a_company_holiday_survives_a_rota_that_covers_it():
	"""The pattern knows nothing about public holidays, so a rota landing on one is the
	pattern being wrong about that day — the calendar wins."""
	public = Row(date=XMAS, description="Public holiday", weekly_off=False)
	worked = days(*range(14))  # works every single day, weekends included

	rows = derive(WEEK_1, LAST, worked, base(public))

	assert rows == [public]


def test_an_employee_working_every_weekday_gets_only_the_weekends():
	rows = derive(WEEK_1, LAST, days(*range(5)) | days(*range(7, 12)), base())

	assert len(rows) == 4


def test_the_window_bounds_the_result():
	rows = derive(WEEK_1, WEEK_1 + datetime.timedelta(days=6), set(), base())

	assert dates_of(rows) == sorted(days(*range(7)))


# ── digest ───────────────────────────────────────────────────────────────────


def test_two_employees_on_the_same_week_share_a_digest():
	"""What makes a practice of a hundred people produce a handful of Holiday Lists."""
	one = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())
	other = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())

	assert digest(one) == digest(other)


def test_a_different_week_digests_differently():
	one = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())
	other = derive(WEEK_1, LAST, days(0, 1, 3, 7, 8, 9), base())

	assert digest(one) != digest(other)


def test_the_description_is_part_of_the_identity():
	"""Two calendars with the same dates but a different reason for one of them are not
	the same calendar — reusing a document across them would relabel somebody's day off."""
	public = Row(date=XMAS, description="Public holiday", weekly_off=False)
	renamed = Row(date=XMAS, description="Company closure", weekly_off=False)
	worked = days(*range(5)) | days(*range(7, 12))

	assert digest(derive(WEEK_1, LAST, worked, base(public))) != digest(
		derive(WEEK_1, LAST, worked, base(renamed))
	)


# ── first_difference ─────────────────────────────────────────────────────────


def test_identical_calendars_have_no_difference():
	rows = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())

	assert first_difference(rows, rows) is None


def test_the_difference_is_the_day_the_week_changed():
	"""A Thursday added to the second week: nothing before it moved, so the succession
	starts there and not at the top of the window."""
	before = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())
	after = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9, 10), base())

	assert first_difference(before, after) == WEEK_1 + datetime.timedelta(days=10)


def test_a_day_only_one_calendar_covers_counts_as_a_difference():
	"""How an extended company calendar shows up as drift rather than as agreement."""
	short = derive(WEEK_1, WEEK_1 + datetime.timedelta(days=6), set(), base())
	long = derive(WEEK_1, LAST, set(), base())

	assert first_difference(short, long) == WEEK_1 + datetime.timedelta(days=7)


def test_an_employee_with_no_list_of_their_own_differs_from_the_first_day():
	derived = derive(WEEK_1, LAST, days(0, 1, 2, 7, 8, 9), base())

	assert first_difference([], derived) == derived[0].date
