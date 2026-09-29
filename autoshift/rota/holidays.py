# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""A bound employee's settled rota, projected into the per-employee `Holiday List`
Frappe HR needs to count their leave correctly.

## Why

Frappe HR charges leave by subtracting holidays from the span applied for:
`get_number_of_leave_days` takes `date_diff(to, from) + 1` and, unless the Leave Type
sets `include_holiday`, subtracts `get_holidays(employee, from, to)`. That resolves
through `hrms.utils.holiday_list.get_holiday_list_for_employee`, which reads submitted
**`Holiday List Assignment`** documents for the employee and falls back to their
company's. So an employee whose week is four days, holding only the company calendar,
is charged five days for a week off. The fix HR expects is a holiday list per employee,
with their own non-working days in it.

This package already knows those days: a bound employee's week is a `Shift Schedule`,
and `cycle.occurrences` expands it. So the list is derived, not entered — the union of
the company's own calendar with every day in the window the employee does not work.

## What this is and is not

**Mostly an export.** The optimizer reads these lists, but only their **dated holidays** —
`data_loader._availability` takes the non-weekly-off rows per employee, and the weekly offs
from the *company's* list rather than anyone's own. The rota-derived days off written here
are deliberately not read back: they are regenerated on demand and would lag, and which days
a bound employee works is the `role_binding` rules' decision at whatever strength the ruleset
chose. Deleting their variables would make that binding unconditional.

**Whole days only.** `Holiday` carries an `is_half_day` flag but `get_holidays` counts
dates, not days — so a half-day holiday still deducts a full one, and an employee who
works five mornings has no derivable holidays at all. Half-day patterns are outside what
this mechanism can express; say so rather than encoding something HR will miscount.

**Bound employees only.** An unbound employee's week is the optimizer's to decide and
differs from one week to the next, so there is no pattern to derive a list from. They
keep the company calendar, and a part-timer among them is charged as if they worked
every weekday. That is a real gap, and the honest one: inventing a nominal week for them
would put a false statement in HR's records rather than an incomplete one.

## Succession

Generation **mints, it never edits**. A rota that changes produces a new `Holiday List`
and a new `Holiday List Assignment` starting at the first date the two derivations
disagree; the old pair stays submitted. Editing a list in place would retroactively
change what every past leave application was counted against.

`Holiday List Assignment` needs no end date of its own — resolution is `from_date <=
as_on` ordered descending, limit 1, so a successor implicitly ends its predecessor. That
is the asymmetry with `Shift Schedule Assignment`, which needed
`custom_create_shifts_until`: an employee has one applicable holiday list at a time and
many concurrent rotas. See the design notes.

Already-approved leave keeps its numbers regardless: `Leave Application.total_leave_days`
is computed on validate and stored, so a later list only affects applications made after
it.

## Shape

The arithmetic is `autoshift.rota.calendar` (Frappe-free, `tests/test_holidays.py`);
this module reads and writes, the same split as `cycle.py`/`materialize.py`.
Idempotency is by comparison, like `materialize`'s: what the employee's currently
assigned list says against what their rota now implies. No high-water mark, no state.
"""

from __future__ import annotations

import datetime

import frappe

from . import materialize
from .calendar import Row, derive, digest, first_difference
from .cycle import occurrences

#: Prefix for every `Holiday List` this module creates, so a hand-made one is never
#: mistaken for a generated one (and never reused, or deleted, by it).
LIST_PREFIX = "Autoshift"


def _rows_of(holiday_list: str) -> list[Row]:
	return rows_between([holiday_list])


def rows_between(
	holiday_lists: list[str],
	first: datetime.date | None = None,
	last: datetime.date | None = None,
) -> list[Row]:
	"""The `Holiday` rows of these lists, in date order, each with its `weekly_off` flag.

	Several lists at once because an assignee's list can change part-way through a window
	(:func:`applicable_lists`); where two of them name the same date, the first list wins,
	which is the order `applicable_lists` returns them in.
	"""
	if not holiday_lists:
		return []
	filters: dict = {"parent": ["in", holiday_lists], "parenttype": "Holiday List"}
	if first is not None and last is not None:
		filters["holiday_date"] = ["between", [first, last]]
	order = {name: index for index, name in enumerate(holiday_lists)}
	by_date: dict[datetime.date, tuple[int, Row]] = {}
	for row in frappe.get_all(
		"Holiday", filters=filters, fields=["parent", "holiday_date", "description", "weekly_off"]
	):
		date = frappe.utils.getdate(row.holiday_date)
		rank = order.get(row.parent, len(order))
		if date in by_date and by_date[date][0] <= rank:
			continue
		by_date[date] = (
			rank,
			Row(
				date=date,
				description=(row.description or "").strip(),
				weekly_off=bool(row.weekly_off),
			),
		)
	return [by_date[date][1] for date in sorted(by_date)]


def applicable_lists(
	assigned_to: str, company: str | None, first: datetime.date, last: datetime.date
) -> list[str]:
	"""The `Holiday List`s in force for `assigned_to` over `[first, last]`, earliest first.

	The resolution HRMS itself does — `Holiday List Assignment` for the assignee, then for
	their company — **plus `Company.default_holiday_list`**, which HRMS's own
	`get_holiday_list_for_employee` dropped when it took the `employee_holiday_list` hook
	over from erpnext. A site that never created an assignment and simply set the company
	default therefore reads as having no calendar at all through the HRMS helpers, which is
	not what anybody configuring it meant.

	Two probes rather than one because an assignment can start part-way through the window;
	that is also why this returns a list.
	"""
	from hrms.utils.holiday_list import get_assigned_holiday_list

	names: list[str] = []
	for as_on in (first, last):
		name = get_assigned_holiday_list(assigned_to, as_on)
		if not name and company:
			name = base_list_for(company, as_on)
		if name and name not in names:
			names.append(name)
	return names


def base_list_for(company: str, as_on: datetime.date) -> str | None:
	"""The company's own holiday list — the calendar every generated list is built on.

	Read through `Holiday List Assignment`, which is where this HRMS keeps the answer;
	`Company.default_holiday_list` is the fallback for a site not using assignments yet.
	Deliberately **not** `Optimizer Settings`: those two lists are the solver's planning
	calendar (one weekends-only, one weekends plus closures), not a statement about what
	the company observes.
	"""
	from hrms.utils.holiday_list import get_assigned_holiday_list

	return get_assigned_holiday_list(company, as_on) or frappe.db.get_value(
		"Company", company, "default_holiday_list"
	)


def _worked_days(
	employees: set[str], first: datetime.date, last: datetime.date
) -> dict[str, set[datetime.date]]:
	"""Every day in the window each employee works: their rotas expanded, plus whatever
	the books already record.

	Both, because neither alone is the whole answer. Rotas reach into the future, where no
	record exists; records cover a half-day somebody was given by hand or by a committed
	run, which no pattern mentions.
	"""
	worked: dict[str, set[datetime.date]] = {employee: set() for employee in employees}

	for rota in materialize.load_rotas(employees, materialize.RoleContext(employees, first, last)):
		worked.setdefault(rota.employee, set()).update(occurrences(rota, first, last))

	rows = frappe.get_all(
		"Shift Assignment",
		filters={
			"employee": ["in", sorted(employees)],
			"docstatus": 1,
			"status": "Active",
			"start_date": ["<=", last],
		},
		or_filters=[["end_date", ">=", first], ["end_date", "is", "not set"]],
		fields=["employee", "start_date", "end_date"],
	)
	for row in rows:
		day = max(frappe.utils.getdate(row.start_date), first)
		end = min(frappe.utils.getdate(row.end_date), last) if row.end_date else last
		while day <= end:
			worked.setdefault(row.employee, set()).add(day)
			day += datetime.timedelta(days=1)

	return worked


def pending(employees: set[str] | None = None) -> dict:
	"""Which bound employees' holiday lists no longer match their rota.

	The read half, and what a prompt is built from — nothing here writes. `employees`
	omitted means every bound employee.

	The window is the **base list's own**: a generated list cannot reach past the dates
	the company has published holidays for, and "the company calendar was extended into
	next year" is a drift worth surfacing rather than a range to invent.
	"""
	employees = materialize.binding_employees() if employees is None else set(employees)
	if not employees:
		return {"window": None, "employees": [], "unconfigured": []}

	today = frappe.utils.getdate(frappe.utils.today())
	companies = {
		row.name: row.company
		for row in frappe.get_all(
			"Employee", filters={"name": ["in", sorted(employees)]}, fields=["name", "company"]
		)
	}

	base_cache: dict[str, tuple[str, datetime.date, datetime.date, dict] | None] = {}

	def base_for(company: str):
		if company not in base_cache:
			name = base_list_for(company, today) if company else None
			if not name:
				base_cache[company] = None
			else:
				window = frappe.db.get_value("Holiday List", name, ["from_date", "to_date"], as_dict=True)
				base_cache[company] = (
					name,
					frappe.utils.getdate(window.from_date),
					frappe.utils.getdate(window.to_date),
					{row.date: row for row in _rows_of(name)},
				)
		return base_cache[company]

	spans = [base_for(c) for c in set(companies.values()) if base_for(c)]
	if not spans:
		return {"window": None, "employees": [], "unconfigured": sorted(employees)}
	first, last = min(s[1] for s in spans), max(s[2] for s in spans)
	worked = _worked_days(employees, first, last)

	out, unconfigured = [], []
	for employee in sorted(employees):
		base = base_for(companies.get(employee))
		if not base:
			unconfigured.append(employee)
			continue
		base_name, base_first, base_last, base_rows = base
		rows = derive(base_first, base_last, worked.get(employee, set()), base_rows)
		current_name = _assigned_list(employee)
		current = _rows_of(current_name) if current_name else []
		changed_from = first_difference(current, rows)
		if changed_from is None:
			continue
		# `HolidayListAssignment.validate_assignment_start_date` refuses a `from_date`
		# outside its list's own window, and the difference can legitimately fall before
		# it — the list being replaced may cover an earlier year than the one being
		# published. The new list cannot apply before it begins, so that is where it
		# starts.
		changed_from = max(changed_from, base_first)
		out.append(
			{
				"employee": employee,
				"company": companies.get(employee),
				"base_list": base_name,
				"current_list": current_name,
				"from_date": changed_from,
				"digest": digest(rows),
				"rows": rows,
				"window": (base_first, base_last),
				"retroactive": changed_from < today,
			}
		)
	return {
		"window": (first, last),
		"employees": out,
		# Nobody can derive a list for these: their company publishes no calendar to
		# build one on. Reported rather than skipped silently.
		"unconfigured": unconfigured,
	}


def _assigned_list(employee: str) -> str | None:
	"""The employee's *own* latest assigned list, ignoring the company fallback — what a
	generated one would be replacing.

	Latest by `from_date` whether or not that date has arrived, rather than whichever is in
	force today. A run that already wrote a future-dated succession must compare against
	it, or the next run sees the same drift again and mints a duplicate. Generated lists
	always span the whole base window, so comparing content against the newest one loses
	nothing about the days in between.
	"""
	rows = frappe.get_all(
		"Holiday List Assignment",
		filters={"assigned_to": employee, "applicable_for": "Employee", "docstatus": 1},
		fields=["holiday_list"],
		order_by="from_date desc",
		limit=1,
	)
	return rows[0].holiday_list if rows else None


def _holiday_list_for(rows: list[Row], window: tuple[datetime.date, datetime.date]) -> str:
	"""A submitted `Holiday List` with exactly these rows, reused if one already exists.

	Named by content digest, so every employee working the same week shares one document
	— a practice of a hundred people on a handful of patterns produces a handful of lists.
	"""
	first, last = window
	name = f"{LIST_PREFIX} {first.year} {digest(rows)}"
	if frappe.db.exists("Holiday List", name):
		return name

	doc = frappe.new_doc("Holiday List")
	doc.holiday_list_name = name
	doc.from_date = first
	doc.to_date = last
	for row in rows:
		doc.append(
			"holidays",
			{
				"holiday_date": row.date,
				"description": row.description,
				"weekly_off": int(row.weekly_off),
			},
		)
	doc.insert(ignore_permissions=True)
	return doc.name


def apply(employees: set[str] | None = None) -> dict:
	"""Write what :func:`pending` found: a `Holiday List` per distinct pattern and a
	`Holiday List Assignment` per employee, starting from the day their week changed.

	Nothing is edited and nothing is cancelled — a superseded assignment stays submitted
	and is simply outranked by the newer `from_date`. One savepoint per employee, as in
	`materialize`, so one refused row does not lose the rest.

	The one refusal worth expecting is HRMS's own duplicate check: an employee who already
	has an assignment starting on the very day their calendar changed. That means the
	existing assignment was wrong from its first day, which is a judgement call about
	somebody's leave entitlement rather than a conflict to resolve automatically — so it
	is reported and left for a human, not cancelled and replaced.
	"""
	frappe.has_permission("Holiday List Assignment", "create", throw=True)
	frappe.has_permission("Holiday List", "create", throw=True)

	found = pending(employees)
	created_lists: set[str] = set()
	assigned: list[str] = []
	failures: list[dict] = []

	for entry in found["employees"]:
		frappe.db.savepoint(materialize.SAVEPOINT)
		try:
			list_name = _holiday_list_for(entry["rows"], entry["window"])
			created_lists.add(list_name)
			assignment = frappe.new_doc("Holiday List Assignment")
			assignment.applicable_for = "Employee"
			assignment.assigned_to = entry["employee"]
			assignment.holiday_list = list_name
			assignment.from_date = entry["from_date"]
			assignment.insert(ignore_permissions=True)
			assignment.submit()
			assigned.append(assignment.name)
		except Exception as exc:  # one employee's refusal is not the batch's problem
			frappe.db.rollback(save_point=materialize.SAVEPOINT)
			failures.append({"employee": entry["employee"], "error": str(exc)})
			frappe.log_error(
				title="autoshift: could not assign a holiday list",
				message=f"{entry['employee']} from {entry['from_date']}: {exc}",
			)

	return {
		"assigned": len(assigned),
		"lists": sorted(created_lists),
		"failures": failures,
		"unconfigured": found["unconfigured"],
	}


@frappe.whitelist()
def get_pending() -> dict:
	"""Whitelisted, JSON-safe `pending` — the derived rows themselves are left out, being
	a few thousand dates nobody is going to read in a dialog."""
	frappe.has_permission("Holiday List Assignment", throw=True)
	found = pending()
	return {
		"window": [d.isoformat() for d in found["window"]] if found["window"] else None,
		"unconfigured": found["unconfigured"],
		"employees": [
			{
				"employee": e["employee"],
				"from_date": e["from_date"].isoformat(),
				"current_list": e["current_list"],
				"retroactive": e["retroactive"],
			}
			for e in found["employees"]
		],
	}


@frappe.whitelist()
def apply_pending() -> dict:
	return apply()
