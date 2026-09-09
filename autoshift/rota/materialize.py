# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Turning a `Shift Schedule` into the `Shift Assignment` records it implies.

The DB half of this package; see `autoshift.rota` for why any of it exists and
`autoshift.rota.cycle` for the expansion rule itself. Everything here is scoped
to employees whose schedule is **binding** — a Scheduling Role marked
`assignments_binding`, minus anyone whose `Employee Scheduling Role` overrides it
off. Nobody else's Shift Schedule is materialised: the optimizer is supposed to
decide their week, and standing up records ahead of it would prejudge that.

Idempotency is by comparison, not by bookkeeping: a day a submitted
`Shift Assignment` already covers is left alone (see `_covered` for what "covers"
means on a given site). That holds however the covering record got there —
imported, hand-entered, committed from a previous run, or created here — and it
needs no high-water mark, which matters because the field HRMS uses as one
(`create_shifts_after`) is also the phase anchor and must not move.
"""

from __future__ import annotations

import datetime

import frappe

from .cycle import FREQUENCY_WEEKS, WEEKDAY_INDEX, Rota, occurrences

#: Per-row savepoint name, so one refused assignment does not lose the rest.
SAVEPOINT = "autoshift_rota_row"


def _getdate(value) -> datetime.date | None:
	return frappe.utils.getdate(value) if value else None


def binding_employees() -> set[str]:
	"""Employees holding at least one binding (employee, role) pair."""
	from autoshift.optimizer import data_loader

	return {employee for employee, _role in data_loader.configured_binding_pairs()}


def load_rotas(employees: set[str] | None = None) -> list[Rota]:
	"""Every Shift Schedule Assignment for `employees`, joined to its schedule.

	`enabled` and `shift_status` are deliberately not filtered on — see the
	package docstring. A schedule that never got submitted is skipped, because an
	unsubmitted rule is one nobody has agreed to yet.
	"""
	filters: dict = {}
	if employees is not None:
		if not employees:
			return []
		filters["employee"] = ["in", sorted(employees)]

	rows = frappe.get_all(
		"Shift Schedule Assignment",
		filters=filters,
		fields=["name", "employee", "company", "shift_schedule", "shift_location", "create_shifts_after"],
	)
	if not rows:
		return []

	schedules = {
		s.name: s
		for s in frappe.get_all(
			"Shift Schedule",
			filters={
				"name": ["in", list({r.shift_schedule for r in rows if r.shift_schedule})],
				"docstatus": 1,
			},
			fields=["name", "shift_type", "frequency"],
		)
	}
	days_by_schedule: dict[str, set[int]] = {}
	for row in frappe.get_all(
		"Assignment Rule Day",
		filters={"parent": ["in", list(schedules)], "parenttype": "Shift Schedule"},
		fields=["parent", "day"],
	):
		index = WEEKDAY_INDEX.get(row.day)
		if index is not None:
			days_by_schedule.setdefault(row.parent, set()).add(index)

	rotas = []
	for row in rows:
		schedule = schedules.get(row.shift_schedule)
		if not schedule:
			continue
		cycle = FREQUENCY_WEEKS.get(schedule.frequency)
		if cycle is None:
			# A frequency this app has never heard of: refuse to guess a cycle
			# length rather than silently materialise the wrong weeks.
			frappe.log_error(
				title="autoshift: unknown Shift Schedule frequency",
				message=f"Shift Schedule {schedule.name} has frequency {schedule.frequency!r}; skipped.",
			)
			continue
		rotas.append(
			Rota(
				assignment=row.name,
				employee=row.employee,
				company=row.company,
				shift_type=schedule.shift_type,
				shift_location=row.shift_location,
				weekdays=frozenset(days_by_schedule.get(schedule.name, ())),
				cycle_weeks=cycle,
				anchor=_getdate(row.create_shifts_after),
			)
		)
	# Deterministic, because under the one-a-day rule two of an employee's own
	# schedules landing on the same day are settled by whichever comes first.
	rotas.sort(key=lambda r: (r.employee, r.shift_type, r.assignment))
	return rotas


def one_shift_per_day() -> bool:
	"""Does this site hold an employee to a single Shift Assignment per day?

	`HR Settings.allow_multiple_shift_assignments` is HRMS's own switch for it, and
	`Shift Assignment.validate_same_date_multiple_shifts` refuses the second same-day
	record while it is off — so it decides what "already covered" can mean here, and
	the answer is read off the site rather than assumed.
	"""
	return not frappe.utils.cint(
		frappe.db.get_single_value("HR Settings", "allow_multiple_shift_assignments")
	)


def _covered(employees: set[str], first: datetime.date, last: datetime.date) -> set[tuple]:
	"""What the books already record over the window, as coverage keys.

	Keyed on (employee, date) where the site allows one shift a day and on
	(employee, date, shift_type) where it allows several — matching exactly what
	HRMS would accept, so nothing is offered that cannot be created.

	Under the one-a-day rule a day already carrying the *other* half is coverage, not
	a conflict to report: the schedule's AM/PM label is fitted from history rather
	than recorded, so the record on the books is the better evidence of which half
	was worked. It is also the only reading that does not offer a create HRMS would
	then refuse.

	Shift Assignments are stored as ranges, so each is expanded across the days of
	the window it actually covers. A blank `end_date` is an open-ended assignment.
	"""
	if not employees:
		return set()
	per_day = one_shift_per_day()
	rows = frappe.get_all(
		"Shift Assignment",
		filters={"employee": ["in", sorted(employees)], "docstatus": 1, "start_date": ["<=", last]},
		or_filters=[["end_date", ">=", first], ["end_date", "is", "not set"]],
		fields=["employee", "shift_type", "start_date", "end_date"],
	)
	covered: set[tuple] = set()
	for row in rows:
		start = max(frappe.utils.getdate(row.start_date), first)
		end = min(frappe.utils.getdate(row.end_date), last) if row.end_date else last
		day = start
		while day <= end:
			covered.add((row.employee, day) if per_day else (row.employee, day, row.shift_type))
			day += datetime.timedelta(days=1)
	return covered


def pending(first, last) -> dict:
	"""What a bound employee's Shift Schedule says they work but nothing records.

	Cheap enough to call on every wall-chart week change; reads configuration and
	two indexed tables, builds no DataPackage and solves nothing.
	"""
	first, last = frappe.utils.getdate(first), frappe.utils.getdate(last)
	employees = binding_employees()
	empty = {
		"first_day": first.isoformat(),
		"last_day": last.isoformat(),
		"count": 0,
		"employees": 0,
		"employee_names": [],
		"rows": [],
	}
	if not employees:
		return empty

	rotas = load_rotas(employees)
	if not rotas:
		return empty

	covered = _covered(employees, first, last)
	per_day = one_shift_per_day()
	rows = []
	for rota in rotas:
		for day in occurrences(rota, first, last):
			key = (rota.employee, day) if per_day else (rota.employee, day, rota.shift_type)
			if key in covered:
				continue
			# Two of this employee's own schedules landing on one day under the
			# one-a-day rule: the first wins, and the second would be refused.
			covered.add(key)
			rows.append(
				{
					"assignment": rota.assignment,
					"employee": rota.employee,
					"company": rota.company,
					"shift_type": rota.shift_type,
					"shift_location": rota.shift_location,
					"date": day.isoformat(),
					"cycle_weeks": rota.cycle_weeks,
				}
			)
	rows.sort(key=lambda r: (r["date"], r["employee"], r["shift_type"]))

	names: dict[str, str] = {}
	if rows:
		names = {
			person.name: person.employee_name
			for person in frappe.get_all(
				"Employee",
				filters={"name": ["in", sorted({r["employee"] for r in rows})]},
				fields=["name", "employee_name"],
			)
		}
	for row in rows:
		row["employee_name"] = names.get(row["employee"]) or row["employee"]

	return {
		**empty,
		"count": len(rows),
		"employees": len({r["employee"] for r in rows}),
		"employee_names": sorted({r["employee_name"] for r in rows}),
		"rows": rows,
	}


def materialize(first, last) -> dict:
	"""Create the `Shift Assignment` records :func:`pending` reports missing.

	One record per day rather than one per run of consecutive days: it is what the
	import writes, it keeps the coverage comparison above a set membership test,
	and it means a single day that cannot be created costs only that day.

	A row that HRMS refuses (an overlapping shift, most likely — somebody was
	given a conflicting assignment by hand) is collected and reported rather than
	aborting the rest, so one bad record cannot block a whole week.
	"""
	from hrms.hr.doctype.shift_assignment_tool.shift_assignment_tool import create_shift_assignment

	frappe.has_permission("Shift Assignment", "create", throw=True)

	found = pending(first, last)
	created, failed = [], []
	for row in found["rows"]:
		# A savepoint per row, so a refusal costs that row and not the whole span.
		frappe.db.savepoint(SAVEPOINT)
		try:
			# Always Active: `Shift Schedule Assignment.shift_status` is HRMS's
			# switch for HRMS's generator, and a rota is marked Inactive there
			# precisely because that generator would run it wrongly. These are
			# shifts the person works.
			doc = create_shift_assignment(
				row["employee"],
				row["company"],
				row["shift_type"],
				row["date"],
				row["date"],
				"Active",
				row["shift_location"],
				row["assignment"],
			)
			created.append(getattr(doc, "name", str(doc)))
		except Exception as error:
			frappe.db.rollback(save_point=SAVEPOINT)
			failed.append({**row, "reason": str(error)})
			frappe.log_error(
				title="autoshift: could not materialise a settled shift",
				message=f"{row['employee']} {row['date']} {row['shift_type']}: {error}",
			)

	return {
		"first_day": found["first_day"],
		"last_day": found["last_day"],
		"created": len(created),
		"failed": failed,
		"employees": found["employees"],
	}


@frappe.whitelist()
def materialize_between(first: str, last: str) -> dict:
	""":func:`materialize` over an arbitrary span — the wall chart's "Create them".

	The reading half needs no endpoint of its own: every surface that asks already has
	one (`get_week_chart` carries `pending_bound`, and both solve paths wrap
	:func:`pending` behind their own horizon).
	"""
	return materialize(first, last)
