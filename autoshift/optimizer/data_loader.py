"""
Reads Frappe HR data and builds the index sets and parameter dicts
needed by model_builder.py.
"""

from __future__ import annotations

import datetime
import itertools
from typing import cast

import frappe
import numpy as np
from frappe.utils import getdate as _getdate

from ..rota import materialize
from . import types
from .rules import BUILTIN_RULES
from .types import MODE_COLLATERAL, MODE_FLEXIBLE, DataPackage, resolve_assignment_role
from .types import planning_days as _planning_days


def _min_temperature(n, delta, clamp=10.0):
	num = (n - 1) * (1 + n * delta)
	den = (n - 1) - n * delta
	assert den > 0, "delta too large"
	return (2 * clamp) / np.log(num / den)


def _normalized_weights(raw, delta, clamp=10.0):
	n = len(raw)
	v = np.clip(raw, -clamp, clamp)
	T = _min_temperature(n, delta, clamp)
	v_scaled = v / T
	v_scaled -= v_scaled.max()  # numerical stability
	e = np.exp(v_scaled)
	return e / e.sum()


def getdate(*args, **kwargs) -> datetime.date:
	result = _getdate(*args, **kwargs)
	if result is None:
		raise ValueError(f"Invalid Arguments to {_getdate.__name__}, ({args},{kwargs})")
	return result


def _fulltime_shifts_in_period(days) -> float:
	"""Shifts a 100%-FTE employee works over `days`: one per weekday, none at the weekend.

	Both the overall FTE ceiling and every agreed per-role split scale this one figure. If
	they were computed separately and drifted apart, a role target and the ceiling would be
	denominated differently and the objective would end up fighting the constraint.

	One shift per working day is the *attainable* maximum, not half of one: `one_shift_per_day`
	already caps an employee at a single shift a day whatever the shift types are.

	`weekday() // 5` is 0 on Mon-Fri and 1 on Sat/Sun, so the term is 1 on a weekday and 0
	at the weekend. It was written with a true divide until 2026-08-26, which ramped the
	weight down across the week (Mon 1.0 ... Fri 0.2, Sun -0.2) and totalled 3.0 for a
	Mon-Fri week instead of 5, capping every employee at ~60% of their real availability
	and starving room coverage.
	"""
	return sum(1 - d.weekday() // 5 for d in days)


def _load_rules(run_doc) -> tuple[tuple[str, str, str, float], ...]:
	"""
	Resolve the run's Optimization Ruleset into (rule_name, builtin_key, custom_code,
	weight) tuples for the DataPackage. Only implemented rules can be used: Built-in
	rules whose key is registered in code, or Custom Code rules a developer has
	validated. Sorted by rule name so two rulesets with the same rules hash identically.
	"""
	ruleset = run_doc.ruleset
	if not ruleset:
		frappe.throw(frappe._("This Optimizer Run has no Optimization Ruleset set."))

	weight_by_rule = {
		row.rule: float(row.weight if row.weight is not None else 1.0)
		for row in frappe.get_all(
			"Optimization Ruleset Rule",
			filters={"parent": ruleset, "parenttype": "Optimization Ruleset"},
			fields=["rule", "weight"],
			order_by="idx",
		)
	}
	if not weight_by_rule:
		frappe.throw(frappe._("Optimization Ruleset {0} contains no rules.").format(frappe.bold(ruleset)))

	rules = frappe.get_all(
		"Optimization Rule",
		filters={"name": ["in", list(weight_by_rule)]},
		fields=["name", "implementation_type", "builtin_key", "implementation_code", "validated"],
	)

	specs: list[tuple[str, str, str, float]] = []
	unusable: list[str] = []
	for rule in sorted(rules, key=lambda r: r.name):
		weight = weight_by_rule[rule.name]
		if rule.implementation_type == "Built-in" and rule.builtin_key in BUILTIN_RULES:
			specs.append((rule.name, rule.builtin_key, "", weight))
		elif rule.implementation_type == "Custom Code" and rule.implementation_code and rule.validated:
			specs.append((rule.name, "", rule.implementation_code, weight))
		else:
			unusable.append(rule.name)

	if unusable:
		frappe.throw(
			frappe._(
				"Optimization Ruleset {0} contains rules that are not implemented (or not yet "
				"validated by a developer) and cannot be used: {1}"
			).format(frappe.bold(ruleset), ", ".join(frappe.bold(name) for name in unusable))
		)

	return tuple(specs)


def _is_binding(row, role_binding: dict[str, bool]) -> bool:
	"""Is this Employee Scheduling Role row's schedule settled?

	A blank override inherits the Scheduling Role's ``assignments_binding``, the same
	convention ``max_rooms`` uses. Shared by :func:`load` and
	:func:`configured_binding_pairs` so the two cannot drift apart.
	"""
	if row.binding_override == "Binding":
		return True
	if row.binding_override == "Not Binding":
		return False
	return role_binding.get(row.scheduling_role, False)


def configured_binding_pairs() -> list[tuple[str, str]]:
	"""Every (employee, Scheduling Role) pair the configuration marks binding.

	Horizon-independent and cheap — two small config tables, no ``DataPackage``. Used
	wherever the question is "whose schedule is not the planner's to set" rather than
	"what does this run see": the binding-rule gap check, and ``autoshift.rota``, which
	materialises exactly these people's Shift Schedules.
	"""
	role_binding = {
		r.name: bool(r.assignments_binding)
		for r in frappe.get_all(
			"Scheduling Role", filters={"active": 1}, fields=["name", "assignments_binding"]
		)
	}
	actives = {e.name for e in frappe.get_all("Employee", filters={"status": "Active"}, fields=["name"])}
	return [
		(row.employee, row.scheduling_role)
		for row in frappe.get_all(
			"Employee Scheduling Role",
			filters={"active": 1},
			fields=["employee", "scheduling_role", "binding_override"],
		)
		if _is_binding(row, role_binding) and row.employee in actives
	]


def binding_holders() -> dict:
	"""Counts off :func:`configured_binding_pairs`, for the pre-solve gap warning.

	This answers "does this site use role binding at all", so a run whose ruleset omits
	``bind_role_assignments`` can warn that those settled schedules are about to be
	ignored.
	"""
	pairs = configured_binding_pairs()
	return {
		"pairs": len(pairs),
		"employees": len({employee for employee, _role in pairs}),
		"roles": sorted({role for _employee, role in pairs}),
	}


def builtin_keys_of(rule_names) -> set[str]:
	"""The built-in keys the given Optimization Rule documents point at.

	Custom Code rows contribute nothing — they carry no builtin key, and no dependency
	metadata either (see CLAUDE.md).
	"""
	rule_names = list(rule_names or [])
	if not rule_names:
		return set()
	return {
		key
		for key in frappe.get_all(
			"Optimization Rule",
			filters={"name": ["in", rule_names], "implementation_type": "Built-in"},
			pluck="builtin_key",
		)
		if key
	}


def binding_rule_gap(selected_keys) -> dict:
	"""Would this rule selection silently ignore the site's settled schedules?

	``{"gap": True, ...}`` when some employee-role pair is marked binding but the
	selection includes neither member of the ``role_binding`` choice group — the run will
	then schedule those people as if their schedule were the planner's to set. Either
	binding rule closes the gap; which of the two is a separate question (see
	``rules.soft_bind_role_assignments``).
	"""
	from autoshift.optimizer.rules import GROUP_ROLE_BINDING, rules_in_group

	holders = binding_holders()
	return {
		**holders,
		"gap": bool(holders["pairs"]) and not (rules_in_group(GROUP_ROLE_BINDING) & set(selected_keys)),
	}


def ruleset_binding_rule_gap(ruleset: str) -> dict:
	""":func:`binding_rule_gap` for the rules an Optimization Ruleset selects."""
	names = frappe.get_all(
		"Optimization Ruleset Rule",
		filters={"parent": ruleset, "parenttype": "Optimization Ruleset"},
		pluck="rule",
	)
	return binding_rule_gap(builtin_keys_of(names))


def load(run_doc) -> DataPackage:
	start_date = getdate(run_doc.date)
	mode = run_doc.mode

	rules = _load_rules(run_doc)

	# ── Optimizer settings ──────────────────────────────────────────────────
	settings = frappe.get_single("Optimizer Settings")

	# ── Discipline-Branch Config ─────────────────────────────────────────────
	config_rows = frappe.get_all(
		"Discipline Branch Config",
		fields=["name", "discipline", "branch", "rooms_num"],
	)
	if not config_rows:
		frappe.throw(frappe._("No Discipline Branch Config records found. Please configure them first."))

	# Build lookup structures from config
	branches = sorted({r.branch for r in config_rows if r.branch})
	disciplines = sorted({r.discipline for r in config_rows if r.discipline})

	flags: set[DataPackage.FLAG] = set()
	rooms: dict[tuple[str, str], int] = {}
	for r in config_rows:
		rooms[(r.discipline, r.branch)] = int(r.rooms_num or 0)

	# ── Planning horizon ─────────────────────────────────────────────────────
	# Needed this early because role validity windows are resolved against it.
	all_days = list(itertools.islice(_planning_days(start_date, mode), 100))
	horizon_start, horizon_end = all_days[0], all_days[-1]

	# ── Scheduling Roles ─────────────────────────────────────────────────────
	# A role names exactly one discipline, and is what makes an employee eligible for
	# anything. Roles in a discipline with no config row cannot be staffed (there are no
	# rooms), so they are dropped here rather than producing unusable variables.
	role_rows = frappe.get_all(
		"Scheduling Role",
		filters={"active": 1, "discipline": ["in", disciplines]},
		fields=[
			"name",
			"discipline",
			"max_rooms",
			"assignments_binding",
			"assignment_mode",
			"gates_rooms",
			"assignment_value",
		],
	)
	if not role_rows:
		frappe.throw(
			frappe._(
				"No active Scheduling Role exists for any configured discipline. The optimizer "
				"schedules employees by the roles they hold, so it has nothing to work with."
			)
		)
	roles = sorted(r.name for r in role_rows)
	role_discipline = {r.name: r.discipline for r in role_rows}
	role_max_rooms = {r.name: max(int(r.max_rooms or 1), 1) for r in role_rows}
	# Roles whose holders' schedules are settled by the holders, not by the planner.
	role_binding = {r.name: bool(r.assignments_binding) for r in role_rows}
	# How a shift in the role is worked. Sparse: Flexible is the default and by far the
	# common case, and leaving it out keeps the input hash of a site that has never touched
	# the field identical to what it hashed before modes existed.
	role_mode = {
		r.name: r.assignment_mode
		for r in role_rows
		if r.assignment_mode and r.assignment_mode != MODE_FLEXIBLE
	}
	# Whether a room waits on the role. Sparse in the other sense: only where the flag
	# disagrees with what the mode implies (`DataPackage.gates_rooms`), which is a floater
	# that opens no rooms, or a collateral duty no room may open without.
	role_gates_rooms = {}
	for r in role_rows:
		gates = bool(r.gates_rooms)
		if gates != ((r.assignment_mode or MODE_FLEXIBLE) != MODE_COLLATERAL):
			role_gates_rooms[r.name] = gates
	# What a shift in the role is worth beyond the rooms it staffs. Sparse: 0 is the default
	# and means "worth exactly what it staffs", which is every role until somebody prices one.
	role_value = {r.name: float(r.assignment_value) for r in role_rows if r.assignment_value}

	# ── Employee Scheduling Roles ────────────────────────────────────────────
	# The validity window is filtered in Python: the condition is
	# "(valid_from unset or <= horizon end) and (valid_to unset or >= horizon start)",
	# two independent OR groups, which frappe's filters/or_filters pair cannot express.
	# The row count is one per employee-capability, so this is cheap.
	held_rows = frappe.get_all(
		"Employee Scheduling Role",
		filters={"active": 1, "scheduling_role": ["in", roles]},
		fields=[
			"employee",
			"scheduling_role",
			"role_fte",
			"max_rooms",
			"binding_override",
			"assignment_mode_override",
			"suitability",
			"valid_from",
			"valid_to",
		],
	)

	def _held_over_horizon(row) -> bool:
		"""Blank ends are open: always held / held indefinitely."""
		if row.valid_from and getdate(row.valid_from) > horizon_end:
			return False
		return not (row.valid_to and getdate(row.valid_to) < horizon_start)

	held_rows = [row for row in held_rows if _held_over_horizon(row)]

	# ── Employees ────────────────────────────────────────────────────────────
	# Scope is "holds at least one in-window Scheduling Role". Employee.department and
	# .designation are HR/payroll data and play no part: somebody nobody has given a role
	# is simply not scheduled, which is also how non-clinical staff stay out.
	role_holders = sorted({row.employee for row in held_rows})
	raw_employees = (
		frappe.get_all(
			"Employee",
			filters={"status": "Active", "name": ["in", role_holders]},
			fields=["name", "custom_fte"],
		)
		if role_holders
		else []
	)
	active_employees = {emp.name for emp in raw_employees}

	employee_role_lists: dict[str, list[str]] = {}
	max_rpe: dict[tuple[str, str], int] = {}
	role_fte_pct: dict[tuple[str, str], float] = {}
	binding_pairs: set[tuple[str, str]] = set()
	role_suitability: dict[tuple[str, str], float] = {}
	role_mode_overrides: dict[tuple[str, str], str] = {}
	for row in held_rows:
		if row.employee not in active_employees:
			continue
		pair = (row.employee, row.scheduling_role)
		employee_role_lists.setdefault(row.employee, []).append(row.scheduling_role)
		max_rpe[pair] = int(row.max_rooms or role_max_rooms.get(row.scheduling_role, 1))
		if row.role_fte:
			role_fte_pct[pair] = float(row.role_fte)
		# blank reads as a regular holder, like the field's default; kept sparse
		if row.suitability and float(row.suitability) != 1.0:
			role_suitability[pair] = float(row.suitability)
		# blank inherits the role's mode; an override restating it is not an override
		override = row.assignment_mode_override
		if override and override != role_mode.get(row.scheduling_role, MODE_FLEXIBLE):
			role_mode_overrides[pair] = override
		if _is_binding(row, role_binding):
			binding_pairs.add(pair)
	employee_roles = {e: tuple(sorted(rs)) for e, rs in employee_role_lists.items()}
	binding_roles_by_employee: dict[str, set[str]] = {}
	for employee, role in binding_pairs:
		binding_roles_by_employee.setdefault(employee, set()).add(role)

	# Employee Settings
	emp_settings = {
		row.employee: row
		for row in frappe.get_all(
			"Employee Settings",
			fields=["*"],
		)
	}

	# ── Shift Types ──────────────────────────────────────────────────────────
	# Shift Type scope is config-driven via Discipline Branch Config.shift_types (a Table
	# MultiSelect, backed by the "Discipline Branch Config Shift Type" child doctype): a
	# Shift Type is in scope if any config row lists it. Excludes non-clinical variants
	# (design doc §2.2) without needing a field on Shift Type itself.
	config_shift_type_rows = frappe.get_all(
		"Discipline Branch Config Shift Type",
		filters={"parent": ["in", [r.name for r in config_rows]]},
		fields=["parent", "shift_type"],
	)
	shift_types_by_config: dict[str, set[str]] = {}
	for row in config_shift_type_rows:
		shift_types_by_config.setdefault(row.parent, set()).add(row.shift_type)

	shift_types = sorted({st for sts in shift_types_by_config.values() for st in sts})

	# ── Shift preferences ─────────────────────────────────────────────────────
	# 3-layer resolution (highest priority first):
	#   1. favourite_shift  → maximum allowed weight on that single shift
	#   2. shift_preferences table → raw weights, normalized if non-compliant
	#   3. uniform preferences
	shift_preferences: dict[str, dict[str, float]] = {}

	if len(shift_types) > 1:
		# delta = max absolute deviation from uniform allowed = 50% of uniform weight (1/N)
		delta = 0.5 / len(shift_types)
		for emp_name, row in emp_settings.items():
			favourite = row.get("favourite_shift")
			pref_rows = frappe.get_all(
				"Employee Shift Preference",
				fields=["shift_type", "weight"],
				filters=[["parent", "=", row.get("name")]],
			)

			clamp: float = 10.0
			weights: dict[str, float] | None = None
			if favourite:
				raw_arr = np.array([(clamp if s == favourite else -clamp) for s in shift_types])
			elif pref_rows:
				raw_arr = np.array(
					[
						float(next((r.weight for r in pref_rows if r.shift_type == s), 0.0))
						for s in shift_types
					]
				)
			else:
				raw_arr = np.array([0.0 for _ in shift_types])

			weights = dict(zip(shift_types, _normalized_weights(raw_arr, delta).tolist(), strict=True))
			shift_preferences[emp_name] = weights

	employees: list[str] = []
	target_shifts: dict[str, int] = {}
	role_target_shifts: dict[tuple[str, str], float] = {}

	fulltime_shifts = _fulltime_shifts_in_period(all_days)

	for emp in raw_employees:
		name = emp.name
		if not employee_roles.get(name):
			continue

		employees.append(name)
		fte_pct = cast(float, emp.custom_fte) or 100.0
		target_shifts[name] = round(fte_pct / 100.0 * fulltime_shifts)

	# An employee with no Employee Settings document means the same as one whose
	# preference tables are blank: uniform preferences. Without this backfill the
	# shift-preference objective read their weights as 0.0 instead of 1/N, silently
	# taxing every one of their assignments by a full -1 rather than -(N-1)/N.
	if len(shift_types) > 1:
		uniform = 1.0 / len(shift_types)
		for name in employees:
			shift_preferences.setdefault(name, dict.fromkeys(shift_types, uniform))

	# Agreed per-role splits, on the same scale as the overall ceiling above. Only pairs
	# whose Employee Scheduling Role names a figure get one — the rest are unconstrained
	# beyond that ceiling, which is the point: these expectations are informal.
	for (name, role), pct in role_fte_pct.items():
		if name in target_shifts:
			role_target_shifts[(name, role)] = pct / 100.0 * fulltime_shifts

	_holiday_list_name: str = settings.get(f"{'un' if mode == 'Unbounded' else ''}bounded_holiday_list")
	_holiday_list_doc = frappe.get_doc("Holiday List", _holiday_list_name)
	_holiday_doc_list: list = _holiday_list_doc.get("holidays")
	holiday_list = [h.get("holiday_date") for h in _holiday_doc_list]
	working_days = [d for d in all_days if d not in holiday_list]

	# ── Leave blocklist ───────────────────────────────────────────────────────
	window_start = str(working_days[0]) if working_days else str(start_date)
	window_end = str(working_days[-1]) if working_days else str(frappe.utils.add_days(start_date, 27))

	leave_blocked: set[tuple[str, datetime.date]] = set()

	# Approved leaves
	approved_leaves = frappe.get_all(
		"Leave Application",
		filters={
			"employee": ["in", employees],
			"status": "Approved",
		},
		or_filters={
			"from_date": ["<=", window_end],
			"to_date": [">=", window_start],
		},
		fields=["employee", "from_date", "to_date"],
	)
	for leave in approved_leaves:
		d = getdate(leave.from_date)
		while d <= getdate(leave.to_date):
			if d in {wd for wd in working_days}:
				leave_blocked.add((leave.employee, d))
			d += datetime.timedelta(days=1)

	# Speculated pending leaves
	speculated_names = [row.leave_application for row in (run_doc.leaves_speculations or [])]
	if speculated_names:
		pending_leaves = frappe.get_all(
			"Leave Application",
			filters={"name": ["in", speculated_names]},
			fields=["employee", "from_date", "to_date"],
		)
		for leave in pending_leaves:
			d = getdate(leave.from_date)
			while d <= getdate(leave.to_date):
				if d in {wd for wd in working_days}:
					leave_blocked.add((leave.employee, d))
				d += datetime.timedelta(days=1)

	# ── Forced assignments ────────────────────────────────────────────────────
	# Which of these are honored, weighed, or ignored is a ruleset choice now
	# (use_existing_assignments / weigh_assignments_objective), not a run-level flag.
	forced: set[tuple[str, str, str, datetime.date, str]] = set()
	binding_conflicts: list[tuple[str, str, str, datetime.date, str]] = []
	unresolved_assignments: list[tuple[str, datetime.date, str]] = []
	binding_employees = {employee for employee, _role in binding_pairs}
	existing = frappe.get_all(
		"Shift Assignment",
		filters={
			"employee": ["in", employees],
			"docstatus": 1,
			"start_date": ["<=", window_end],
		},
		fields=[
			"name",
			"employee",
			"shift_type",
			"start_date",
			"shift_location",
			"custom_scheduling_role",
		],
	)
	# Assignments outside the horizon (the query has no lower bound) and on non-working
	# days have no variable to be forced onto, so drop them before doing any resolution.
	working_day_set = set(working_days)
	existing = [sa for sa in existing if getdate(sa.start_date) in working_day_set]

	# A bound employee's settled week is read straight off their Shift Schedule, never
	# written to the books first: submitted Shift Assignments are real documents with
	# notifications attached, and creating them ahead of every solve would churn exactly
	# the records a planner has not approved yet. Each day the rota puts somebody on that
	# the books do not already cover stands in for the Shift Assignment it would become
	# (`rota.materialize.settled_rows`), so a horizon solves — and hashes — the same whether
	# or not anybody has created those records.
	settled = (
		materialize.settled_rows(working_days[0], working_days[-1], binding_employees & set(employees))
		if working_days
		else []
	)
	existing.extend(
		frappe._dict(
			name=None,
			rota=row["assignment"],
			employee=row["employee"],
			shift_type=row["shift_type"],
			start_date=getdate(row["date"]),
			shift_location=row["shift_location"],
			custom_scheduling_role=row["scheduling_role"],
			collateral_roles=row["collateral_roles"],
		)
		for row in settled
		if getdate(row["date"]) in working_day_set
	)

	# Source of truth for branch and discipline: Shift Assignment -> Shift Location ->
	# Shift Location.custom_branch / .custom_discipline.
	locations = {
		row.name: row
		for row in frappe.get_all(
			"Shift Location",
			filters={"name": ["in", list({sa.shift_location for sa in existing if sa.shift_location})]},
			fields=["name", "custom_branch", "custom_discipline"],
		)
	}
	# A Shift Assignment records its Scheduling Role in `custom_scheduling_role`. Where it
	# does not — a record entered by hand, or one older than the field and missed by the
	# backfill patch — the role is recovered: the single non-collateral role they hold at all,
	# else from its location's discipline, the one role the
	# employee holds there, or, holding several, the one of those that is binding for them
	# (a settled half-day is presence, not a choice of role — see
	# `types.resolve_assignment_role`). Guessing between two roles neither of which settles the
	# question is what the field exists to stop: a rota now settles presence, and which of
	# somebody's roles a half-day went to is precisely the thing that cannot be inferred from
	# where they stood. Collateral roles are never inferred: a duty is an explicit editorial
	# act, and an assignment with no role on it is a shift somebody worked, not a lead duty.
	# (Same rule as `patches.backfill_shift_assignment_roles`, which fills the field in from
	# here.)
	roles_by_employee_discipline: dict[tuple[str, str], list[str]] = {}
	# The same roles, not split by discipline: somebody holding exactly one of them worked
	# it, whatever discipline their Shift Location is filed under (`resolve_assignment_role`
	# rung 2). Collateral duties are excluded from both, for the same reason.
	working_roles_by_employee: dict[str, list[str]] = {}
	for name, held in employee_roles.items():
		for role in held:
			mode = role_mode_overrides.get((name, role)) or role_mode.get(role, MODE_FLEXIBLE)
			if mode == MODE_COLLATERAL:
				continue
			roles_by_employee_discipline.setdefault((name, role_discipline[role]), []).append(role)
			working_roles_by_employee.setdefault(name, []).append(role)

	# Collateral duties ride on their host's record rather than having one of their own:
	# HRMS refuses two Shift Assignments whose times overlap. A duty worked *alone* is an
	# ordinary record whose own Scheduling Role is the collateral one. A rota day already
	# carries its rota's duties.
	recorded = [sa.name for sa in existing if sa.name]
	collateral_by_assignment: dict[str, list[str]] = {}
	for row in (
		frappe.get_all(
			"Collateral Scheduling Role",
			filters={
				"parenttype": "Shift Assignment",
				"parentfield": "custom_collateral_roles",
				"parent": ["in", recorded],
			},
			fields=["parent", "scheduling_role"],
		)
		if recorded
		else []
	):
		collateral_by_assignment.setdefault(row.parent, []).append(row.scheduling_role)
	for sa in existing:
		if sa.name:
			sa.collateral_roles = collateral_by_assignment.get(sa.name, [])

	def _unresolvable(sa, reason: str) -> None:
		"""An existing assignment this run cannot place.

		For a bound employee this is fatal: their schedule is *input*, and quietly
		dropping a day of it would freeze them to a week they do not work. For anybody
		else it is a data-quality observation, not a reason to refuse to plan — the
		practice's own history is patchy, most of it is disregarded now anyway, and a
		single mis-filed location used to be able to abort the whole run. Recorded, and
		reported by the run-statistics panel.
		"""
		if sa.name:
			message = frappe._("Shift Assignment {0}: {1}").format(sa.name, reason)
		else:
			message = frappe._("Shift Schedule Assignment {0} on {1}: {2}").format(
				sa.rota, frappe.utils.formatdate(sa.start_date), reason
			)
		if sa.employee in binding_employees:
			frappe.throw(message)
		unresolved_assignments.append((sa.employee, getdate(sa.start_date), reason))

	def _resolve_role(sa) -> str | None:
		"""The Scheduling Role this assignment is worked in, or None if it cannot be said.

		The decision itself is `types.resolve_assignment_role`; this half turns the outcomes
		it cannot phrase into messages about the record they are about.
		"""
		discipline = (locations.get(sa.shift_location) or frappe._dict()).get("custom_discipline")
		candidates = roles_by_employee_discipline.get((sa.employee, discipline)) or []
		outcome, role = resolve_assignment_role(
			sa.custom_scheduling_role,
			employee_roles.get(sa.employee, ()),
			discipline,
			candidates,
			binding_roles_by_employee.get(sa.employee, ()),
			working_roles_by_employee.get(sa.employee, ()),
		)
		match outcome:
			case types.ROLE_RESOLVED:
				return role
			case types.ROLE_NOT_HELD:
				reason = frappe._(
					"records Scheduling Role {0}, which {1} does not hold over this horizon — "
					"give them the role, or correct the assignment"
				).format(sa.custom_scheduling_role, sa.employee)
			case types.ROLE_NO_DISCIPLINE:
				reason = frappe._(
					"names no Scheduling Role, and Shift Location {0} has no Discipline to infer "
					"one from — set the Scheduling Role on the assignment"
				).format(sa.shift_location)
			case types.ROLE_NONE_IN_DISCIPLINE:
				reason = frappe._(
					"{0} holds no Scheduling Role in discipline {1}, but the assignment places "
					"them there — give them the role, or correct the Shift Location"
				).format(sa.employee, discipline)
			case _:
				reason = frappe._(
					"names no Scheduling Role, and {0} holds {1} of them in discipline {2} ({3}) — "
					"which one this shift was worked in cannot be inferred, so set it on the "
					"assignment"
				).format(sa.employee, len(candidates), discipline, ", ".join(sorted(candidates)))
		_unresolvable(sa, reason)
		return None

	for sa in existing:
		location = locations.get(sa.shift_location) or frappe._dict()
		branch = location.get("custom_branch")
		if not branch:
			_unresolvable(
				sa,
				frappe._("no Shift Location with a Branch set, so it names no branch"),
			)
			continue
		role = _resolve_role(sa)
		if role is None:
			continue
		day = getdate(sa.start_date)
		combs = [(sa.employee, role, sa.shift_type, day, str(branch))]
		for collateral in sa.collateral_roles:
			if collateral in employee_roles.get(sa.employee, ()):
				combs.append((sa.employee, collateral, sa.shift_type, day, str(branch)))
			else:
				_unresolvable(
					sa,
					frappe._(
						"records a collateral duty in {0}, which {1} does not hold over this horizon"
					).format(collateral, sa.employee),
				)
		# Approved (or speculated) leave wins over an assignment already on the books —
		# forcing both would make the model infeasible. Record it so the run-statistics
		# panel can tell the planner their data disagrees with itself. A collateral duty
		# goes down with its host: it is the same half-day.
		if (sa.employee, day) in leave_blocked:
			binding_conflicts.extend(combs)
			continue
		forced.update(combs)

	return DataPackage(
		flags=flags,
		employees=employees,
		shift_types=shift_types,
		working_days=working_days,
		branches=branches,
		roles=roles,
		role_discipline=role_discipline,
		employee_roles=employee_roles,
		target_shifts=target_shifts,
		role_target_shifts=role_target_shifts,
		max_rpe=max_rpe,
		rooms=rooms,
		disciplines=disciplines,
		leave_blocked=leave_blocked,
		forced=forced,
		shift_preferences=shift_preferences,
		rules=rules,
		binding_pairs=frozenset(binding_pairs),
		role_mode=role_mode,
		role_mode_overrides=role_mode_overrides,
		role_gates_rooms=role_gates_rooms,
		role_value=role_value,
		binding_conflicts=tuple(sorted(binding_conflicts)),
		unresolved_assignments=tuple(sorted(set(unresolved_assignments))),
		role_suitability=role_suitability,
	)
