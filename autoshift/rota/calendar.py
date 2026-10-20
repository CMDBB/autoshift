# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""What one employee's own holiday calendar contains. Pure Python, no Frappe.

The arithmetic half of `autoshift.rota.holidays`, which is where the *why* is written
and which does the reading and writing. This module only answers three questions, so
they can be answered without a site:

- given the days somebody works, which days of a window are holidays for them
  (:func:`derive`),
- do two such calendars differ, and from when (:func:`first_difference`),
- are two of them the same calendar (:func:`digest`, which is how employees working the
  same week come to share one `Holiday List` document rather than each getting a copy).
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass

#: What a generated row says about a day the employee's pattern simply does not cover.
NON_WORKING = "Non-working day"


@dataclass(frozen=True, order=True)
class Row:
	"""One `Holiday` child row: the day, what it is, and whether it is a weekly off."""

	date: datetime.date
	description: str
	weekly_off: bool


def derive(
	first: datetime.date,
	last: datetime.date,
	worked: set[datetime.date],
	base: dict[datetime.date, Row],
) -> list[Row]:
	"""The employee's own holiday rows over `[first, last]`, in date order.

	`base` is the company calendar's rows by date, carried through **verbatim** — a public
	holiday stays a public holiday, with its own description and `weekly_off` flag, even on
	a day the rota nominally covers. A pattern knows nothing about public holidays, so a
	rota landing on one is the rota being wrong about that day, not the calendar.

	Every other day of the window the employee does not work is added as a weekly off. Not
	always literally weekly — a four-week cycle's off-days are not — but the flag is what
	separates "not a working day for this person" from "a holiday the company observes",
	and that is the distinction `only_non_weekly` callers are asking about.
	"""
	rows = dict(base)
	day = first
	while day <= last:
		if day not in rows and day not in worked:
			rows[day] = Row(date=day, description=NON_WORKING, weekly_off=True)
		day += datetime.timedelta(days=1)
	return [rows[key] for key in sorted(rows)]


def digest(rows: list[Row]) -> str:
	"""A content hash over the rows: the identity of a calendar, rather than of whoever
	happens to hold it."""
	payload = "\n".join(f"{r.date.isoformat()}|{r.description}|{int(r.weekly_off)}" for r in rows)
	return hashlib.sha256(payload.encode()).hexdigest()[:12]


def first_difference(current: list[Row], derived: list[Row]) -> datetime.date | None:
	"""The earliest date the two calendars disagree about, or None if they agree
	everywhere.

	What a new assignment starts from: the day the employee's week actually changed,
	rather than "today" or the start of the window, so the succession records when it
	happened. A date one calendar covers and the other does not counts as a disagreement,
	which is what makes an extended company calendar show up as drift.
	"""
	by_current = {r.date: r for r in current}
	by_derived = {r.date: r for r in derived}
	differing = [
		date for date in set(by_current) | set(by_derived) if by_current.get(date) != by_derived.get(date)
	]
	return min(differing) if differing else None
