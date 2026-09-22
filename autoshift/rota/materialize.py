# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Reading a `Shift Schedule` as the `Shift Assignment` records it implies, and
writing them on request.

The DB half of this package; see `autoshift.rota` for why any of it exists and
`autoshift.rota.cycle` for the expansion rule itself. Everything here is scoped
to employees whose schedule is **binding** — a Scheduling Role marked
`assignments_binding`, minus anyone whose `Employee Scheduling Role` overrides it
off. Nobody else's Shift Schedule is read: the optimizer is supposed to decide
their week.

Reading is :func:`settled_rows`, and it is what the optimizer consumes — a solve never
writes a record, because submitted Shift Assignments are real documents with
notifications attached, and churning them ahead of every preview is not acceptable.
Writing is :func:`materialize`, reached only from the wall chart's explicit "Create them".

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


def _in_window(row, first: datetime.date | None, last: datetime.date | None) -> bool:
	"""Is this `Employee Scheduling Role` row valid anywhere in `[first, last]`?

	The same predicate `optimizer.data_loader` applies to its own role rows: "(valid_from
	unset or <= window end) and (valid_to unset or >= window start)". No window at all
	accepts every active row.
	"""
	if first is None or last is None:
		return True
	if row.valid_from and frappe.utils.getdate(row.valid_from) > last:
		return False
	return not (row.valid_to and frappe.utils.getdate(row.valid_to) < first)


class RoleContext:
	"""Everything needed to say which Scheduling Role a rota is worked in, read once.

	`load_rotas` asks the same question of a `Shift Schedule Assignment` that
	`optimizer.data_loader` asks of a `Shift Assignment`, and answers it with the same
	function (`types.resolve_assignment_role`), so a rota and the record materialised from
	it can never disagree about the role. Built per call rather than cached: it is four
	small configuration tables, and a stale answer here would be written onto documents.

	`first`/`last` scope the `Employee Scheduling Role` rows to the ones valid over that
	span, exactly as `data_loader` scopes its own `employee_roles`. :func:`settled_rows`
	passes its horizon, so a solve never inherits a role its own package says the person
	does not hold. Left out — as the Rota Editor leaves it out — every active row counts,
	which is the right reading for a pattern that has been worked for years.
	"""

	__slots__ = ("binding", "held", "location_discipline", "role_discipline", "working")

	def __init__(
		self,
		employees: set[str] | None = None,
		first: datetime.date | None = None,
		last: datetime.date | None = None,
	):
		from autoshift.optimizer import data_loader
		from autoshift.optimizer.types import MODE_COLLATERAL

		roles = {
			row.name: row
			for row in frappe.get_all(
				"Scheduling Role",
				filters={"active": 1},
				fields=["name", "discipline", "assignment_mode"],
			)
		}
		self.role_discipline: dict[str, str] = {
			name: row.discipline for name, row in roles.items() if row.discipline
		}
		binding_pairs = data_loader.configured_binding_pairs()
		self.binding: dict[str, set[str]] = {}
		for employee, role in binding_pairs:
			self.binding.setdefault(employee, set()).add(role)

		filters: dict = {"active": 1}
		if employees is not None:
			filters["employee"] = ["in", sorted(employees)] if employees else ["in", [""]]
		self.held: dict[str, set[str]] = {}
		self.working: dict[str, set[str]] = {}
		for row in frappe.get_all(
			"Employee Scheduling Role",
			filters=filters,
			fields=["employee", "scheduling_role", "assignment_mode_override", "valid_from", "valid_to"],
		):
			role = roles.get(row.scheduling_role)
			if not role:
				continue
			if not _in_window(row, first, last):
				continue
			self.held.setdefault(row.employee, set()).add(row.scheduling_role)
			mode = row.assignment_mode_override or role.assignment_mode
			if mode != MODE_COLLATERAL:
				# A collateral duty is never inferred — it is an explicit editorial act,
				# and a pattern that names no role is a shift somebody worked, not a duty.
				self.working.setdefault(row.employee, set()).add(row.scheduling_role)

		self.location_discipline: dict[str, str] = {
			row.name: row.custom_discipline
			for row in frappe.get_all(
				"Shift Location", fields=["name", "custom_discipline"], limit_page_length=0
			)
			if row.custom_discipline
		}

	def resolve(self, employee: str, recorded: str | None, shift_location: str | None) -> str | None:
		"""The role this pattern is worked in, or None where it cannot be said.

		Unlike the optimizer's copy, an unresolvable rota is not an error here: the Rota
		Editor's whole job is to let a planner look at one and settle it, so it draws as
		unattributed rather than refusing to draw.
		"""
		from autoshift.optimizer import types

		discipline = self.location_discipline.get(shift_location) if shift_location else None
		held = self.held.get(employee, set())
		working = self.working.get(employee, set())
		candidates = sorted(role for role in working if self.role_discipline.get(role) == discipline)
		outcome, role = types.resolve_assignment_role(
			recorded,
			held,
			discipline,
			candidates,
			self.binding.get(employee, set()),
			working,
		)
		return role if outcome == types.ROLE_RESOLVED else None

	def discipline_of(self, role: str | None, shift_location: str | None) -> str | None:
		"""Which discipline a pattern belongs to — see `Rota.discipline`."""
		if role and role in self.role_discipline:
			return self.role_discipline[role]
		return self.location_discipline.get(shift_location) if shift_location else None


def load_rotas(employees: set[str] | None = None, roles: "RoleContext | None" = None) -> list[Rota]:
	"""Every Shift Schedule Assignment for `employees`, joined to its schedule.

	`enabled` and `shift_status` are deliberately not filtered on — see the
	package docstring. A schedule that never got submitted is skipped, because an
	unsubmitted rule is one nobody has agreed to yet.

	A row whose `custom_scheduling_role` is blank — every pattern imported before the
	field existed — has one **resolved** here rather than left empty, by the same ladder
	the optimizer applies to a Shift Assignment (:class:`RoleContext`). The record on disk
	is not written; this is a reading, so a site that has not run the backfill patch still
	sees its rotas attributed, and a value a planner later corrects always wins. The role
	is then what gives the pattern its `discipline`, which is what lets the Rota Editor
	refuse to let one discipline's view edit another's settled week.

	Pass `roles` to share one `RoleContext` across several calls; omitted, one is built.
	"""
	filters: dict = {}
	if employees is not None:
		if not employees:
			return []
		filters["employee"] = ["in", sorted(employees)]

	rows = frappe.get_all(
		"Shift Schedule Assignment",
		filters=filters,
		fields=[
			"name",
			"employee",
			"company",
			"shift_schedule",
			"shift_location",
			"create_shifts_after",
			"custom_unconfirmed",
			"custom_scheduling_role",
		],
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

	collateral = collateral_roles([row.name for row in rows])
	if roles is None:
		roles = RoleContext({row.employee for row in rows})
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
		role = roles.resolve(row.employee, row.custom_scheduling_role, row.shift_location)
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
				unconfirmed=bool(row.custom_unconfirmed),
				scheduling_role=role,
				collateral_roles=tuple(collateral.get(row.name, ())),
				discipline=roles.discipline_of(role, row.shift_location),
			)
		)
	# Deterministic, because under the one-a-day rule two of an employee's own
	# schedules landing on the same day are settled by whichever comes first.
	rotas.sort(key=lambda r: (r.employee, r.shift_type, r.assignment))
	return rotas


def collateral_roles(assignments: list[str]) -> dict[str, list[str]]:
	"""Shift Schedule Assignment -> the collateral roles its shifts carry.

	The rota's counterpart to the same table on `Shift Assignment`: a duty worked on top
	of a shift has no record of its own to live in, so it rides on the one that generates
	it and is copied onto every `Shift Assignment` materialised from it.
	"""
	if not assignments:
		return {}
	out: dict[str, list[str]] = {}
	for row in frappe.get_all(
		"Collateral Scheduling Role",
		filters={
			"parenttype": "Shift Schedule Assignment",
			"parentfield": "custom_collateral_roles",
			"parent": ["in", assignments],
		},
		fields=["parent", "scheduling_role"],
	):
		if row.scheduling_role:
			out.setdefault(row.parent, []).append(row.scheduling_role)
	return out


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


def settled_rows(first: datetime.date, last: datetime.date, employees: set[str]) -> list[dict]:
	"""What `employees`' Shift Schedules say they work over the span but nothing records.

	One row per day, carrying everything a `Shift Assignment` would: the rota's shift type,
	location, Scheduling Role and collateral duties. The optimizer reads these directly as
	though they were on the books (`data_loader.load`), and :func:`materialize` is the one
	place they are ever written as records.
	"""
	if not employees:
		return []
	# Scoped to the horizon, like `data_loader`'s own `employee_roles`: a role somebody
	# held two years ago is not one this run can attribute a shift to, and resolving one
	# here would only make the loader refuse the row it just produced.
	rotas = load_rotas(employees, RoleContext(employees, first, last))
	if not rotas:
		return []

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
					"scheduling_role": rota.scheduling_role,
					"collateral_roles": list(rota.collateral_roles),
					"date": day.isoformat(),
					"cycle_weeks": rota.cycle_weeks,
				}
			)
	rows.sort(key=lambda r: (r["date"], r["employee"], r["shift_type"]))
	return rows


def pending(first, last) -> dict:
	"""What a bound employee's Shift Schedule says they work but nothing records.

	Cheap enough to call on every wall-chart week change; reads configuration and
	two indexed tables, builds no DataPackage and solves nothing.
	"""
	first, last = frappe.utils.getdate(first), frappe.utils.getdate(last)
	empty = {
		"first_day": first.isoformat(),
		"last_day": last.isoformat(),
		"count": 0,
		"employees": 0,
		"employee_names": [],
		"rows": [],
	}
	rows = settled_rows(first, last, binding_employees())
	if not rows:
		return empty

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

	Each record carries the rota's own Scheduling Role and any collateral duties, so the
	optimizer reads the role off the record instead of inferring one.

	One record per day rather than one per run of consecutive days: it is what the
	import writes, it keeps the coverage comparison above a set membership test,
	and it means a single day that cannot be created costs only that day.

	A row that HRMS refuses (an overlapping shift, most likely — somebody was
	given a conflicting assignment by hand) is collected and reported rather than
	aborting the rest, so one bad record cannot block a whole week.
	"""
	frappe.has_permission("Shift Assignment", "create", throw=True)

	found = pending(first, last)
	created, failed = [], []
	for row in found["rows"]:
		# A savepoint per row, so a refusal costs that row and not the whole span.
		frappe.db.savepoint(SAVEPOINT)
		try:
			created.append(_create_assignment(row))
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


def _create_assignment(row: dict) -> str:
	"""One day's `Shift Assignment`, built the way HRMS's own helper builds it.

	`shift_assignment_tool.create_shift_assignment` is the same handful of assignments
	plus `save()`/`submit()`, and it is not called here for one reason: the role has to be
	on the record **before** it is submitted. Everything downstream reads
	`custom_scheduling_role` rather than guessing a role from where the person stood, and
	a submitted document is not the place to start filling that in.
	"""
	doc = frappe.new_doc("Shift Assignment")
	doc.employee = row["employee"]
	doc.company = row["company"]
	doc.shift_type = row["shift_type"]
	doc.start_date = row["date"]
	doc.end_date = row["date"]
	# Always Active: `Shift Schedule Assignment.shift_status` is HRMS's switch for HRMS's
	# generator, and a rota is marked Inactive there precisely because that generator
	# would run it wrongly. These are shifts the person works.
	doc.status = "Active"
	doc.shift_location = row["shift_location"]
	doc.shift_schedule_assignment = row["assignment"]
	doc.custom_scheduling_role = row.get("scheduling_role")
	for role in row.get("collateral_roles") or ():
		doc.append("custom_collateral_roles", {"scheduling_role": role})
	doc.save()
	doc.submit()
	return doc.name


@frappe.whitelist()
def materialize_between(first: str, last: str) -> dict:
	""":func:`materialize` over an arbitrary span — the wall chart's "Create them".

	The reading half needs no endpoint of its own: every surface that asks already has
	one (`get_week_chart` carries `pending_bound`, and both solve paths wrap
	:func:`pending` behind their own horizon).
	"""
	return materialize(first, last)
