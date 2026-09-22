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


def leaves(monday: datetime.date, speculated: set[str] | None = None) -> dict[str, list[dict]]:
	"""Approved (plus optionally speculated) leave in the week, per ISO date.

	Not a `Slot`: somebody on leave is not in a chair, so they have no cell. They
	are the answer to "why is this chair empty", though, which is the whole point
	of the chart, so they are reported alongside it.
	"""
	days = week_dates(monday)
	first, last = days[0], days[-1]
	rows = frappe.get_all(
		"Leave Application",
		filters={"status": "Approved", "from_date": ["<=", last], "to_date": [">=", first]},
		fields=["name", "employee", "leave_type", "from_date", "to_date"],
	)
	if speculated:
		rows += frappe.get_all(
			"Leave Application",
			filters={"name": ["in", list(speculated)]},
			fields=["name", "employee", "leave_type", "from_date", "to_date"],
		)

	people = _employees({row["employee"] for row in rows})
	out: dict[str, list[dict]] = {}
	seen: set[str] = set()
	for row in rows:
		if row["name"] in seen:
			continue
		seen.add(row["name"])
		person = people.get(row["employee"], {})
		entry = {
			"employee": row["employee"],
			"employee_name": person.get("employee_name") or "",
			"label": short_label(
				row["employee"], person.get("employee_name") or "", person.get(INITIALS_FIELD)
			),
			"leave_type": row["leave_type"],
			"speculative": bool(speculated and row["name"] in speculated),
		}
		day = frappe.utils.getdate(row["from_date"])
		end = frappe.utils.getdate(row["to_date"])
		while day <= end:
			if day in days:
				out.setdefault(day.isoformat(), []).append(entry)
			day += datetime.timedelta(days=1)
	for entries in out.values():
		entries.sort(key=lambda e: (e["label"].upper(), e["employee"]))
	return out


def holidays(monday: datetime.date, mode: str = "Bounded") -> dict[str, str]:
	"""ISO date -> holiday description, for the week, from Optimizer Settings.

	The chart dims a non-working day rather than dropping the column, so an
	assignment that landed on one is still visible — which is exactly the kind of
	thing worth seeing.
	"""
	settings = frappe.get_single("Optimizer Settings")
	list_name = settings.get("unbounded_holiday_list" if mode == "Unbounded" else "bounded_holiday_list")
	if not list_name:
		return {}
	days = {day.isoformat() for day in week_dates(monday)}
	out: dict[str, str] = {}
	for row in frappe.get_all(
		"Holiday",
		filters={"parent": list_name, "parenttype": "Holiday List"},
		fields=["holiday_date", "description"],
	):
		iso = frappe.utils.getdate(row["holiday_date"]).isoformat()
		if iso in days:
			out[iso] = row["description"] or ""
	return out
