"""
Reads Frappe HR data and builds the index sets and parameter dicts
needed by model_builder.py.
"""

from __future__ import annotations

import datetime
from typing import cast

import frappe
import numpy as np
from frappe.utils import add_days
from frappe.utils import getdate as _getdate

from .types import DataPackage
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


def load(run_doc) -> DataPackage:
	start_date = getdate(run_doc.date)
	mode = run_doc.mode

	# ── Optimizer settings ──────────────────────────────────────────────────
	settings = frappe.get_single("Optimizer Settings")
	fte_tolerance = cast(float, settings.get("fte_tolerance_pct") or 0.05)
	turnover_weight = cast(float, settings.get("turnover_weight") or 1.0)

	# ── Discipline-Designation-Branch Config ─────────────────────────────────
	config_rows = frappe.get_all(
		"Discipline Designation Branch Config",
		fields=["name", "discipline", "employee_type", "branch", "max_rooms_for_employee_type", "rooms_num"],
	)
	if not config_rows:
		frappe.throw(
			frappe._("No Discipline Designation Branch Config records found. Please configure them first.")
		)

	# Build lookup structures from config
	branches = sorted({r.branch for r in config_rows if r.branch})
	disciplines = sorted({r.discipline for r in config_rows if r.discipline})

	rooms: dict[tuple[str, str], int] = {}
	for r in config_rows:
		rooms[(r.discipline, r.branch)] = int(r.rooms_num or 0)

	# max_rpe per (designation, discipline) — take max across branches for simplicity
	max_rpe_by_desig: dict[str, int] = {}
	for r in config_rows:
		key = r.employee_type
		max_rpe_by_desig[key] = max(max_rpe_by_desig.get(key, 0), int(r.max_rooms_for_employee_type or 1))

	# ── Employees ────────────────────────────────────────────────────────────
	valid_designations = {r.employee_type for r in config_rows}
	raw_employees = frappe.get_all(
		"Employee",
		filters={"status": "Active", "designation": ["in", list(valid_designations)]},
		fields=["name", "designation", "department", "custom_fte"],
	)

	# Employee Settings
	emp_settings = {
		row.employee: row
		for row in frappe.get_all(
			"Employee Settings",
			fields=["*"],
		)
	}

	# ── Shift Types ──────────────────────────────────────────────────────────
	# Shift Type scope is config-driven via Discipline Designation Branch Config.shift_types
	# (a Table MultiSelect, backed by the "Discipline Designation Branch Config Shift Type"
	# child doctype): a Shift Type is in scope if any DDBC row lists it. Excludes non-clinical
	# variants (design doc §2.2) without needing a field on Shift Type itself.
	ddbc_shift_type_rows = frappe.get_all(
		"Discipline Designation Branch Config Shift Type",
		filters={"parent": ["in", [r.name for r in config_rows]]},
		fields=["parent", "shift_type"],
	)
	shift_types_by_ddbc: dict[str, set[str]] = {}
	for row in ddbc_shift_type_rows:
		shift_types_by_ddbc.setdefault(row.parent, set()).add(row.shift_type)

	shift_types = sorted({st for sts in shift_types_by_ddbc.values() for st in sts})

	# TODO: the same Shift Type selection has to be re-entered on every DDBC row of a given
	# discipline (one per designation x branch), so nothing stops two rows of the *same*
	# discipline from listing different Shift Types. Detect and warn rather than silently
	# unioning across a drifted config - the optimizer's idea of "what shifts exist for
	# Endo" shouldn't depend on which row happened to define them.
	shift_type_variants_by_discipline: dict[str, set[frozenset[str]]] = {}
	for r in config_rows:
		shift_type_variants_by_discipline.setdefault(r.discipline, set()).add(
			frozenset(shift_types_by_ddbc.get(r.name, set()))
		)
	for discipline, variants in shift_type_variants_by_discipline.items():
		if len(variants) > 1:
			frappe.log_error(
				title="Inconsistent Shift Types across Discipline Designation Branch Config rows",
				message=(
					f"Discipline {discipline!r} has Discipline Designation Branch Config rows "
					f"with differing Shift Types selections: {[sorted(v) for v in variants]}. "
					"The optimizer uses the union of all selections for this discipline; "
					"align the rows to avoid surprises."
				),
			)

	# ── Shift preferences ─────────────────────────────────────────────────────
	# 3-layer resolution (highest priority first):
	#   1. favourite_shift  → maximum allowed weight on that single shift
	#   2. shift_preferences table → raw weights, normalized if non-compliant
	#   3. uniform preferences
	# An employee absent from this dict contributes 0.0 (neutral) in the objective.
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

	employees = []
	designation: dict[str, str] = {}
	department: dict[str, str] = {}
	is_salaried: dict[str, bool] = {}
	target_shifts: dict[str, int] = {}
	max_rpe: dict[str, int] = {}
	employee_holiday_lists: dict[str, str] = {}

	all_days = _planning_days(start_date, mode)

	for emp in raw_employees:
		name = emp.name
		desig = emp.designation or ""
		if desig not in valid_designations:
			continue

		employees.append(name)
		designation[name] = desig
		department[name] = emp.department or ""
		employee_holiday_lists[name] = emp.holiday_list or ""

		# TODO: Employment Type is a configurable Link doctype. Replace with a configurable name list (e.g.
		# in Optimizer Settings) once defined. For now, assume everyone is salaried.
		is_salaried[name] = True

		fte_pct = cast(float, emp.custom_fte) or 100.0
		fte_fraction = fte_pct / 100.0
		# Two shifts per day * number of working days * FTE fraction
		n_slots = len(all_days) * 2
		target_shifts[name] = round(fte_fraction * n_slots)

		max_rpe[name] = max_rpe_by_desig.get(desig, 1)

	_holiday_list_name: str = settings.get(f"{'un' if mode == 'Unbounded' else ''}bounded_holiday_list")  # pyright: ignore[reportAssignmentType]
	_holiday_list_doc = frappe.get_doc("Holiday List", _holiday_list_name)
	_holiday_doc_list: list = _holiday_list_doc.get("holidays")  # pyright: ignore[reportAssignmentType]
	holiday_list = [h.get("holiday_date") for h in _holiday_doc_list]
	working_days = [d for d in all_days if d not in holiday_list]

	# ── Leave blocklist ───────────────────────────────────────────────────────
	window_start = str(working_days[0]) if working_days else str(start_date)
	window_end = str(working_days[-1]) if working_days else str(add_days(start_date, 27))

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
	if run_doc.disregard_assignments in ("Use", "Weigh"):
		raise NotImplementedError(
			f"disregard_assignments = '{run_doc.disregard_assignments}' is not yet implemented; only 'Ignore' is supported"
		)

	forced: set[tuple[str, str, datetime.date, str]] = set()
	if run_doc.disregard_assignments == "Use":
		# unreachable: TODO implement Use and test it
		existing = frappe.get_all(
			"Shift Assignment",
			filters={
				"employee": ["in", employees],
				"docstatus": 1,
				"start_date": ["<=", window_end],
			},
			fields=["employee", "shift_type", "start_date", "shift_location"],
		)
		# Source of truth for branch: Shift Assignment -> Shift Location ->
		# Shift Location.custom_branch (Link to Branch).
		location_branch = {
			row.name: row.custom_branch
			for row in frappe.get_all(
				"Shift Location",
				filters={"name": ["in", list({sa.shift_location for sa in existing if sa.shift_location})]},
				fields=["name", "custom_branch"],
			)
		}
		for sa in existing:
			branch = location_branch.get(sa.shift_location)
			if not branch:
				frappe.throw(
					frappe._(
						"Shift Assignment {0} has no Shift Location with a Branch set; cannot "
						"resolve which branch it belongs to."
					).format(sa.name)
				)
			forced.add((sa.employee, sa.shift_type, getdate(sa.start_date), str(branch)))

	return DataPackage(
		employees=employees,
		shift_types=shift_types,
		working_days=working_days,
		branches=branches,
		designation=designation,
		department=department,
		target_shifts=target_shifts,
		max_rpe=max_rpe,
		rooms=rooms,
		disciplines=disciplines,
		leave_blocked=leave_blocked,
		forced=forced,
		shift_preferences=shift_preferences,
		fte_tolerance=fte_tolerance,
		turnover_weight=turnover_weight,
	)
