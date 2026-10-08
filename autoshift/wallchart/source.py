# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""A week of scheduling data, read out of Frappe as `Slot` records.

Three sources, because the chart has to draw a week whether or not anything has
been solved for it — the point of an always-on view is that it is up before the
first run and still up after a failed one:

    Shift Assignment    Frappe HR's own record. `custom_scheduling_role` names
                        the role where it is set; where it is not, the role is
                        inferred from what the employee holds and every inferred
                        slot is flagged rather than hidden.
    Shift Schedule      a bound employee's rota, for the days no Shift Assignment
                        records yet. Exactly what the optimizer reads as being on
                        the books (`rota.materialize.settled_rows`), drawn as
                        `virtual` chips so the week can be seen before anybody
                        creates the records.
    Optimizer Run       the run's `solution_table`. Authoritative: every slot
                        names the Scheduling Role it was assigned in.

`chart.merge` puts the two side by side, so a solved run reads as a diff against
the books — kept, added, dropped — rather than as a second unrelated picture.

:func:`leaves` reads the fourth thing on the chart, and reads two of the sources
above a second time to do it: a leave day is a day with no slot, so the only
evidence of *which half* it empties is the person's rota and whatever the books
already record (:func:`_expected_shifts`).
"""

from __future__ import annotations

import datetime
from collections import defaultdict

import frappe

from . import layout
from .chart import KIND_ADDED, KIND_EXISTING, Slot, week_dates

#: `Employee.custom_initials` belongs to zawin2frappe (see CLAUDE.md, "Custom
#: fields"), so autoshift may read it but must never ship it. Sites without that
#: app get initials derived from the name instead.
INITIALS_FIELD = "custom_initials"


def short_label(employee: str, employee_name: str, initials: str | None) -> str:
	"""What a cell prints: a couple of characters, unambiguous in context.

	A wall chart has room for initials and nothing else. Where the site records
	them they are used as given; otherwise they are built from the name, which is
	what makes this work on a bench that has never seen the import.
	"""
	if initials:
		return initials
	if employee_name:
		parts = [p for p in employee_name.replace("-", " ").split() if p]
		if len(parts) >= 2:
			return (parts[0][0] + parts[-1][0]).upper()
		if parts:
			return parts[0][:2].upper()
	return employee


def _has_initials() -> bool:
	return bool(frappe.db.has_column("Employee", INITIALS_FIELD))


def _employees(names: set[str], extra_fields: tuple[str, ...] = ()) -> dict[str, dict]:
	if not names:
		return {}
	fields = list(dict.fromkeys(["name", "employee_name", *extra_fields]))
	if _has_initials():
		fields.append(INITIALS_FIELD)
	rows = frappe.get_all(
		"Employee",
		filters={"name": ["in", list(names)]},
		fields=fields,
	)
	return {row["name"]: row for row in rows}


def _chip_sort_config() -> dict[str, tuple[str, bool]]:
	"""Active Scheduling Role -> (Employee fieldname, descending), for roles
	that configure a `chip_sort_field`.

	Guarded against `frappe.db.has_column` rather than trusting the doctype:
	the field the role names may have been removed from Employee since it was
	configured, and a chart that throws over that is worse than one that falls
	back to alphabetical (`chart._order_lane`'s `sort_value is None` branch).
	`scheduling_role.py` also checks this at save time, so in practice this is
	a runtime safety net, not the primary guard.
	"""
	rows = frappe.get_all(
		"Scheduling Role",
		filters={"active": 1},
		fields=["name", "chip_sort_field", "chip_sort_descending"],
	)
	return {
		row["name"]: (row["chip_sort_field"], bool(row["chip_sort_descending"]))
		for row in rows
		if row["chip_sort_field"] and frappe.db.has_column("Employee", row["chip_sort_field"])
	}


def _sort_value(role: str | None, person: dict, chip_sort: dict[str, tuple[str, bool]]) -> object | None:
	if not role or role not in chip_sort:
		return None
	field, _descending = chip_sort[role]
	return person.get(field)


def _location_branches() -> dict[str, str | None]:
	"""Shift Location -> branch. The source of truth for a Shift Assignment's
	branch is `Shift Location.custom_branch` (CLAUDE.md, "Custom fields")."""
	return {
		row.name: row.custom_branch
		for row in frappe.get_all("Shift Location", fields=["name", "custom_branch"])
	}


def _location_disciplines() -> dict[str, str | None]:
	return {
		row.name: row.custom_discipline
		for row in frappe.get_all("Shift Location", fields=["name", "custom_discipline"])
	}


def _role_disciplines() -> dict[str, str | None]:
	return {
		row.name: row.discipline for row in frappe.get_all("Scheduling Role", fields=["name", "discipline"])
	}


def _role_max_rooms() -> dict[str, int]:
	"""Scheduling Role -> rooms one holder covers, before any per-holder override."""
	return {
		row.name: max(int(row.max_rooms or 1), 1)
		for row in frappe.get_all("Scheduling Role", fields=["name", "max_rooms"])
	}


def rooms_covered(
	employee: str,
	role: str | None,
	day: datetime.date,
	held: dict[str, list[dict]],
	role_max_rooms: dict[str, int],
) -> int:
	"""How many rooms this person covers in this role — the chip's height.

	`Employee Scheduling Role.max_rooms` where the holder has one, else the role's
	own figure: the same resolution `data_loader` does into `DataPackage.max_rpe`,
	so a chip is exactly as tall as the room-slots the optimizer counted it for.
	"""
	if not role:
		return 1
	for row in held.get(employee, []):
		if row["scheduling_role"] == role and _in_window(row, day):
			if row.get("max_rooms"):
				return max(int(row["max_rooms"]), 1)
			break
	return role_max_rooms.get(role, 1)


def _held_roles(names: set[str]) -> dict[str, list[dict]]:
	"""Active Employee Scheduling Role rows per employee, validity window kept."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Employee Scheduling Role",
		filters={"employee": ["in", list(names)], "active": 1},
		fields=["employee", "scheduling_role", "max_rooms", "valid_from", "valid_to"],
	)
	held: dict[str, list[dict]] = defaultdict(list)
	for row in rows:
		held[row["employee"]].append(row)
	return held


def _in_window(row: dict, day: datetime.date) -> bool:
	if row.get("valid_from") and frappe.utils.getdate(row["valid_from"]) > day:
		return False
	to = row.get("valid_to")
	return not (to and frappe.utils.getdate(to) < day)


#: Sort key for a role that is no longer active, so it ranks after every role
#: the chart actually draws a lane for.
_UNRANKED = (2**31, 0, "")


def infer_role(
	candidates: list[str],
	discipline: str | None,
	role_disciplines: dict[str, str | None],
	role_order: dict[str, tuple],
) -> tuple[str | None, bool]:
	"""Pick the role an employee most likely worked. Returns (role, certain).

	A Shift Assignment names a Shift Location and a location names exactly one
	discipline, so the discipline narrows the field first — which settles most
	people outright. What survives is genuinely ambiguous (an assistant who also
	holds Sterilization) and is broken by `layout.lane_sort_key`, the same order
	the chart's lanes use, so the guess lands in the leftmost lane the employee
	could plausibly have worked rather than an arbitrary one. It is flagged
	uncertain either way and the chart draws it differently rather than
	pretending.
	"""
	if not candidates:
		return None, False
	narrowed = [r for r in candidates if role_disciplines.get(r) == discipline] if discipline else []
	pool = narrowed or candidates
	if len(pool) == 1:
		return pool[0], bool(narrowed) or len(candidates) == 1
	return sorted(pool, key=lambda r: (role_order.get(r, _UNRANKED), r))[0], False


def collateral_roles(assignments: list[str]) -> dict[str, list[str]]:
	"""Shift Assignment -> the collateral roles worked on top of it.

	A collateral duty rides on its host's record rather than having one of its
	own, because HRMS refuses two Shift Assignments whose times overlap.
	"""
	if not assignments:
		return {}
	out: dict[str, list[str]] = defaultdict(list)
	for row in frappe.get_all(
		"Collateral Scheduling Role",
		filters={
			"parenttype": "Shift Assignment",
			"parentfield": "custom_collateral_roles",
			"parent": ["in", assignments],
		},
		fields=["parent", "scheduling_role"],
	):
		if row["scheduling_role"]:
			out[row["parent"]].append(row["scheduling_role"])
	return out


def from_shift_assignments(monday: datetime.date, employees: list[str] | None = None) -> list[Slot]:
	"""Submitted Shift Assignments overlapping the week starting `monday`.

	Assignments are stored as date ranges. The ZaWin import writes one row per
	day, but hrms allows a span and a hand-entered one may well use it, so each
	assignment is expanded across the days of the week it actually covers.
	"""
	days = week_dates(monday)
	first, last = days[0], days[-1]
	filters: dict = {"docstatus": 1, "start_date": ["<=", last]}
	if employees is not None:
		if not employees:
			return []
		filters["employee"] = ["in", employees]
	rows = frappe.get_all(
		"Shift Assignment",
		filters=filters,
		or_filters=[["end_date", ">=", first], ["end_date", "is", "not set"]],
		fields=[
			"name",
			"employee",
			"shift_type",
			"start_date",
			"end_date",
			"shift_location",
			"custom_scheduling_role",
		],
	)
	if not rows:
		return []
	collateral = collateral_roles([row["name"] for row in rows])
	return _book_slots(
		[
			{
				"employee": row["employee"],
				"shift_type": row["shift_type"],
				"shift_location": row["shift_location"],
				"role": row.get("custom_scheduling_role"),
				"collateral": collateral.get(row["name"], ()),
				"start": frappe.utils.getdate(row["start_date"]),
				"end": frappe.utils.getdate(row["end_date"]) if row["end_date"] else None,
			}
			for row in rows
		],
		days,
		virtual=False,
	)


def from_settled_rotas(monday: datetime.date, rows: list[dict]) -> list[Slot]:
	"""Bound employees' rota days in the week that nothing on the books records yet.

	`rows` are `rota.materialize.settled_rows` — exactly what the optimizer reads as
	though it were on the books, so the chart shows the same week the solver sees.
	Drawn as `virtual` chips: a planner sees what "Create them" would write before
	anything is written.
	"""
	return _book_slots(
		[
			{
				"employee": row["employee"],
				"shift_type": row["shift_type"],
				"shift_location": row["shift_location"],
				"role": row.get("scheduling_role"),
				"collateral": row.get("collateral_roles") or (),
				"start": frappe.utils.getdate(row["date"]),
				"end": None,
			}
			for row in rows
		],
		week_dates(monday),
		virtual=True,
	)


def _book_slots(records: list[dict], days: list[datetime.date], virtual: bool) -> list[Slot]:
	"""Slots for records on (or standing in for) the books, one per day each covers.

	`end` None means a single day. The role is the record's own where it has one, else
	inferred and flagged; collateral duties get chips of their own.
	"""
	if not records:
		return []
	names = {record["employee"] for record in records}
	chip_sort = _chip_sort_config()
	people = _employees(names, tuple({field for field, _ in chip_sort.values()}))
	branches = _location_branches()
	disciplines = _location_disciplines()
	role_disciplines = _role_disciplines()
	role_order = layout.role_order()
	held = _held_roles(names)
	role_max_rooms = _role_max_rooms()

	slots: list[Slot] = []
	for record in records:
		location = record["shift_location"] or ""
		person = people.get(record["employee"], {})
		start = record["start"]
		end = record["end"] or start
		for day in days:
			if not (start <= day <= end):
				continue
			if record["role"]:
				role, certain = record["role"], True
			else:
				candidates = [
					held_row["scheduling_role"]
					for held_row in held.get(record["employee"], [])
					if _in_window(held_row, day)
				]
				role, certain = infer_role(
					candidates, disciplines.get(location), role_disciplines, role_order
				)
			# The duties worked on top of this shift get a chip of their own, in
			# their own lane: HRMS cannot hold them as records of their own (see
			# `data_loader`), but a chart that hid them would say the lead was
			# somewhere else.
			for role_of_chip, is_certain in [(role, certain)] + [(r, True) for r in record["collateral"]]:
				slots.append(
					Slot(
						date=day,
						shift_type=record["shift_type"],
						employee=record["employee"],
						employee_name=person.get("employee_name") or "",
						label=short_label(
							record["employee"],
							person.get("employee_name") or "",
							person.get(INITIALS_FIELD),
						),
						branch=branches.get(location),
						scheduling_role=role_of_chip,
						rooms=rooms_covered(record["employee"], role_of_chip, day, held, role_max_rooms),
						kind=KIND_EXISTING,
						role_certain=is_certain,
						sort_value=_sort_value(role_of_chip, person, chip_sort),
						virtual=virtual,
					)
				)
	return slots


def _parse_room_index(raw: str | None) -> tuple[int, ...]:
	"""`Optimizer Run Slot.room_index` ("3,4" or blank) -> the room numbers, parsed.

	Blank on every run that never selected `room_coverage_matched_rooms`, or on an
	assignment that rule did not match into a room — the ordinary case, not an error.
	"""
	if not raw:
		return ()
	return tuple(int(part) for part in raw.split(",") if part.strip())


def from_optimizer_run(run_name: str, monday: datetime.date) -> list[Slot]:
	"""One Optimizer Run's proposed slots, clipped to the week."""
	rows = frappe.get_all(
		"Optimizer Run Slot",
		filters={"parent": run_name, "parenttype": "Optimizer Run"},
		fields=[
			"employee",
			"scheduling_role",
			"shift_type",
			"date",
			"shift_location",
			"branch",
			"forced",
			"rooms",
			"room_index",
		],
	)
	if not rows:
		return []
	days = set(week_dates(monday))
	names = {row["employee"] for row in rows}
	chip_sort = _chip_sort_config()
	people = _employees(names, tuple({field for field, _ in chip_sort.values()}))
	branches = _location_branches()

	# Roles became a slot field partway through; a run solved before that has none
	# on any of its slots, and without this every one of them would land under
	# "Unplaced" — the chart would look broken rather than old. Inferred exactly as
	# a Shift Assignment's is, and flagged the same way.
	needs_inference = any(not row["scheduling_role"] for row in rows)
	disciplines = _location_disciplines() if needs_inference else {}
	role_disciplines = _role_disciplines() if needs_inference else {}
	role_order = layout.role_order() if needs_inference else {}
	# `held` is needed either way now: it carries the per-holder max-rooms override
	# that decides how tall a chip is drawn.
	held = _held_roles(names)
	role_max_rooms = _role_max_rooms()

	slots: list[Slot] = []
	for row in rows:
		day = frappe.utils.getdate(row["date"])
		if day not in days:
			continue
		person = people.get(row["employee"], {})
		role, certain = row["scheduling_role"], True
		if not role:
			candidates = [
				held_row["scheduling_role"]
				for held_row in held.get(row["employee"], [])
				if _in_window(held_row, day)
			]
			role, certain = infer_role(
				candidates,
				disciplines.get(row["shift_location"] or ""),
				role_disciplines,
				role_order,
			)
		ceiling = rooms_covered(row["employee"], role, day, held, role_max_rooms)
		# Measured only under `room_load_objective`; 0 otherwise, and on every run solved
		# before the field existed, where the ceiling is all there is to draw.
		taken = int(row["rooms"] or 0)
		slots.append(
			Slot(
				date=day,
				shift_type=row["shift_type"],
				employee=row["employee"],
				employee_name=person.get("employee_name") or "",
				label=short_label(
					row["employee"], person.get("employee_name") or "", person.get(INITIALS_FIELD)
				),
				# The run slot carries its own branch; the location is a fallback
				# for runs written before that field existed.
				branch=row["branch"] or branches.get(row["shift_location"] or ""),
				scheduling_role=role,
				rooms=taken or ceiling,
				max_rooms=ceiling if taken else 0,
				kind=KIND_ADDED,
				forced=bool(row["forced"]),
				role_certain=certain,
				sort_value=_sort_value(role, person, chip_sort),
				room_index=_parse_room_index(row["room_index"]),
			)
		)
	return slots


LEAVE_FIELDS = ["name", "employee", "leave_type", "from_date", "to_date", "half_day", "half_day_date"]


def _expected_shifts(
	employees: set[str], days: list[datetime.date]
) -> dict[tuple[str, datetime.date], set[str]]:
	"""(employee, date) -> the Shift Types that day would otherwise have held.

	What makes a day's leave attributable to one half of it rather than to the
	whole week. Two sources, and neither is the leave itself: HRMS records a
	half-day as a flag and a date (`Leave Application.half_day`) and never says
	*which* half, so the evidence has to come from what the person works.

	Their rota leads — a settled pattern is the only thing that says "this one
	works Tuesday mornings" about a day nothing was ever scheduled on — and the
	books fill in a leave filed after the assignment was made. Deliberately not
	`rota.materialize.settled_rows`: that subtracts the days already recorded and
	keeps bound employees only, and both of those are days this needs.

	Empty for somebody with neither, which is a real answer rather than a failure:
	the chart then reports their leave without claiming a half.
	"""
	if not employees:
		return {}
	first, last = days[0], days[-1]
	out: dict[tuple[str, datetime.date], set[str]] = defaultdict(set)

	for row in frappe.get_all(
		"Shift Assignment",
		filters={"employee": ["in", sorted(employees)], "docstatus": 1, "start_date": ["<=", last]},
		or_filters=[["end_date", ">=", first], ["end_date", "is", "not set"]],
		fields=["employee", "shift_type", "start_date", "end_date"],
	):
		day = max(frappe.utils.getdate(row["start_date"]), first)
		end = min(frappe.utils.getdate(row["end_date"]), last) if row["end_date"] else last
		while day <= end:
			out[(row["employee"], day)].add(row["shift_type"])
			day += datetime.timedelta(days=1)

	from autoshift.rota import materialize as rota

	for pattern in rota.load_rotas(employees, rota.RoleContext(employees, first, last)):
		for day in rota.occurrences(pattern, first, last):
			out[(pattern.employee, day)].add(pattern.shift_type)
	return dict(out)


def _not_working(
	employees: set[str],
	days: list[datetime.date],
	worked_weekdays: set[int] | None = None,
) -> dict[str, set[datetime.date]]:
	"""Per employee, the days of the week they do not work.

	`rota.holidays.calendars` — the same resolution the solver's availability is built on
	(`data_loader._availability`), so the chart and the model cannot disagree about which
	days are worked. The company's weekly offs apply to everybody, each employee's own
	dated holidays to them alone.

	Deliberately **not** a weekday test. A practice that starts opening on Saturdays says
	so by taking Saturday out of its Holiday List, and everything here follows without a
	code change — which is exactly what hardcoding the weekend would have cost.

	A site with no resolvable calendar blocks nothing, so every leave day reads as due and
	the chart is as noisy as it was before. That is a configuration error a solve refuses
	to plan through; a chart has no business refusing to draw over it.

	`worked_weekdays` is the reader's own answer to the same question, for a site whose
	calendar cannot give one yet (see `api._default_worked`). It **narrows and never
	widens**: a weekday left out closes that day for everybody, a weekday left in says
	only "not on my account" and the calendar still has its say. So the control cannot be
	used to open a day the calendar closed — that belongs in the Holiday List, which is
	also the only place a solve would ever read it.
	"""
	if not employees or not days:
		return {}
	from autoshift.rota.holidays import calendars

	blocked = calendars(sorted(employees), days[0], days[-1]).blocked
	if worked_weekdays is None:
		return blocked
	closed = {day for day in days if day.weekday() not in worked_weekdays}
	return {employee: blocked.get(employee, set()) | closed for employee in employees}


def _in_disciplines(employees: set[str], days: list[datetime.date], disciplines: set[str]) -> set[str]:
	"""Which of `employees` hold an in-window Scheduling Role in one of `disciplines`.

	How the leave list follows the chart's own filter. It also drops everybody
	holding no role at all, which is the whole of the site's non-clinical staff:
	they never appear on the chart, so their leave was only ever noise next to it.
	"""
	held = _held_roles(employees)
	role_disciplines = _role_disciplines()
	return {
		employee
		for employee in employees
		if any(
			role_disciplines.get(row["scheduling_role"]) in disciplines
			and any(_in_window(row, day) for day in days)
			for row in held.get(employee, [])
		)
	}


def _due(
	employee: str,
	day: datetime.date,
	shift_types: list[str],
	not_working: dict[str, set[datetime.date]],
	scheduled: set[str],
) -> bool:
	"""Was this person due to work that day at all — i.e. did their leave empty anything?

	A leave is recorded as a span of dates, so it covers every day in between whether or
	not the person was ever going to be there. A Saturday nobody works, or a Wednesday a
	four-day week never included, is emptied by nothing, and reporting it says only that
	the leave is long.

	Two pieces of evidence, both negative:

	- **their calendar** — the company's weekly offs, or a dated holiday of their own
	  (:func:`_not_working`). Decisive for everybody, and the reason a long leave no
	  longer drags the weekend onto the chart.
	- **their rota** — where we have a week for them at all (`scheduled`) and it puts
	  them nowhere on that day. This is what catches a four-day week, or an alternating
	  rota's off week.

	Absence of evidence is not evidence: somebody with no rota and nothing on the books
	is `due` on any day their calendar allows, and their leave is reported with no half
	named. Guessing "probably off" there would hide a real absence.
	"""
	if day in not_working.get(employee, ()):
		return False
	return bool(shift_types) or employee not in scheduled


def leaves(
	monday: datetime.date,
	speculated: set[str] | None = None,
	disciplines: set[str] | None = None,
	worked_weekdays: set[int] | None = None,
) -> list[dict]:
	"""Approved (plus optionally speculated) leave in the week, one entry per day.

	Not a `Slot`: somebody on leave is not in a chair, so they have no cell. They
	are the answer to "why is this chair empty", though, which is the whole point
	of the chart, so they are reported alongside it.

	Each entry names the Shift Types that day would otherwise have held
	(`shift_types`, see :func:`_expected_shifts`), so the chart can print a day's
	leave under the half it actually empties — morning leave below the morning
	table — instead of once per week with no half named at all. The list is empty
	where nothing says which half, and `half_day` is on where HRMS says the day is
	a half one without saying which half it is.

	`disciplines` narrows to employees holding a role in one of them; omitted, the
	whole site's leave comes back. `worked_weekdays` is the reader's weekday control,
	which closes a day for everybody — see :func:`_not_working`.
	"""
	days = week_dates(monday)
	first, last = days[0], days[-1]
	rows = frappe.get_all(
		"Leave Application",
		filters={"status": "Approved", "from_date": ["<=", last], "to_date": [">=", first]},
		fields=LEAVE_FIELDS,
	)
	if speculated:
		rows += frappe.get_all(
			"Leave Application",
			filters={"name": ["in", list(speculated)]},
			fields=LEAVE_FIELDS,
		)
	# One Leave Application can arrive twice — approved *and* named as a
	# speculation — and is one leave either way.
	deduped = list({row["name"]: row for row in rows}.values())

	employees = {row["employee"] for row in deduped}
	if disciplines is not None:
		employees = _in_disciplines(employees, days, disciplines)
		deduped = [row for row in deduped if row["employee"] in employees]

	people = _employees(employees)
	expected = _expected_shifts(employees, days)
	not_working = _not_working(employees, days, worked_weekdays)
	# Somebody whose week we have at all: a rota or the books put them somewhere in this
	# window. Without that, "no shift on Tuesday" is ignorance rather than a day off, and
	# the leave has to be reported — see `due` below.
	scheduled = {employee for employee, _day in expected}
	out: list[dict] = []
	for row in deduped:
		person = people.get(row["employee"], {})
		day = frappe.utils.getdate(row["from_date"])
		end = frappe.utils.getdate(row["to_date"])
		half_day_date = frappe.utils.getdate(row["half_day_date"]) if row["half_day_date"] else None
		while day <= end:
			if day in days:
				shift_types = sorted(expected.get((row["employee"], day), ()))
				out.append(
					{
						"employee": row["employee"],
						"employee_name": person.get("employee_name") or "",
						"label": short_label(
							row["employee"],
							person.get("employee_name") or "",
							person.get(INITIALS_FIELD),
						),
						"leave_type": row["leave_type"],
						"speculative": bool(speculated and row["name"] in speculated),
						"date": day.isoformat(),
						# A half-day HRMS cannot place: shown under every half the
						# person works, flagged, rather than guessed at.
						"half_day": bool(row["half_day"]) and (half_day_date is None or half_day_date == day),
						"shift_types": shift_types,
						"due": _due(row["employee"], day, shift_types, not_working, scheduled),
					}
				)
			day += datetime.timedelta(days=1)
	out.sort(key=lambda e: (e["date"], e["label"].upper(), e["employee"]))
	return out


def in_scope(
	slots: list[Slot], disciplines: list[str] | None = None, branches: list[str] | None = None
) -> tuple[list[Slot], list[Slot]]:
	"""Split slots into the ones a filtered chart draws and the ones it hides.

	A slot's discipline is its Scheduling Role's, which is also what decides the
	band it lands in — so a slot surviving this is a slot `chart.build` can place,
	and one that doesn't would otherwise have turned up under `Unplaced` as a
	chip from a discipline the reader just asked not to see.

	Both halves come back because the hidden ones are still worth *counting*: a
	filter is an explicit act, but silently losing a scheduled person is the one
	thing this chart never does, so the payload says how many it set aside.
	"""
	if not disciplines and not branches:
		return list(slots), []
	role_disciplines = _role_disciplines()
	wanted_disciplines = set(disciplines or ())
	wanted_branches = set(branches or ())
	kept: list[Slot] = []
	hidden: list[Slot] = []
	for slot in slots:
		discipline = role_disciplines.get(slot.scheduling_role)
		if wanted_disciplines and discipline not in wanted_disciplines:
			hidden.append(slot)
		elif wanted_branches and slot.branch not in wanted_branches:
			hidden.append(slot)
		else:
			kept.append(slot)
	return kept, hidden


def holidays(monday: datetime.date, mode: str = "Bounded") -> tuple[dict[str, str], set[int], bool]:
	"""The week's non-working days, which weekdays are weekly offs, and whether a calendar
	answered at all.

	`({ISO date: holiday description}, weekly-off weekdays, resolved)`. **Weekly offs are
	in the dict too** — an ERPNext `Holiday List` stores them as dated `Holiday` rows — so
	it is every day the companies' calendars say is not worked, not only the public
	holidays. They also come back separately, as weekday numbers, because that is the
	calendar's answer to "which days does this practice work" and `api._default_worked`
	seeds the reader's weekday control from it.

	The two extra returns distinguish the three states a calendar can be in, which the
	dict alone cannot: nothing resolved (`resolved` false), resolved and naming its weekly
	offs, and resolved while naming none — the last being a site that has simply never
	generated them, where a weekday default has to come from somewhere else.

	A column header is shared by everyone on the chart, so this is the *company*
	calendar — the same one `optimizer.data_loader` takes its weekly offs from. A holiday
	that applies to one employee and not another is a per-person fact and belongs on their
	chip, not on the day.

	The chart dims a non-working day rather than dropping the column, so an
	assignment that landed on one is still visible — which is exactly the kind of
	thing worth seeing.

	`mode` is accepted and ignored: it selected between the two `Optimizer Settings` lists
	that used to live here, and there is only one calendar now.
	"""
	from autoshift.rota.holidays import base_list_for

	lists = {
		name
		for company in frappe.get_all("Company", pluck="name")
		if (name := base_list_for(company, monday))
	}
	if not lists:
		return {}, set(), False
	days = {day.isoformat() for day in week_dates(monday)}
	out: dict[str, str] = {}
	weekly: set[int] = set()
	for row in frappe.get_all(
		"Holiday",
		filters={"parent": ["in", sorted(lists)], "parenttype": "Holiday List"},
		fields=["holiday_date", "description", "weekly_off"],
	):
		date = frappe.utils.getdate(row["holiday_date"])
		if date.isoformat() not in days:
			continue
		out.setdefault(date.isoformat(), row["description"] or "")
		if row["weekly_off"]:
			weekly.add(date.weekday())
	return out, weekly, True
