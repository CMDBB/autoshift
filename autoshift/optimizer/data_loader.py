"""
Reads Frappe HR data and builds the index sets and parameter dicts
needed by model_builder.py.
"""

from __future__ import annotations

import datetime

import frappe
from frappe.utils import add_days, getdate

from .types import DataPackage, planning_days as _planning_days


def _exclude_holidays(days: list[datetime.date], employee_holiday_lists: dict) -> set[datetime.date]:
	"""Return the set of dates that are holidays for ALL employees (global non-working days)."""
	if not employee_holiday_lists:
		return set()
	# Count how many employees have each date as a holiday
	holiday_counter: dict[datetime.date, int] = {}
	for hl_name in set(employee_holiday_lists.values()):
		if not hl_name:
			continue
		dates = frappe.get_all(
			"Holiday",
			filters={"parent": hl_name, "holiday_date": ["in", [str(d) for d in days]]},
			pluck="holiday_date",
		)
		for d in dates:
			holiday_counter[getdate(d)] = holiday_counter.get(getdate(d), 0) + 1
	total = len(employee_holiday_lists)
	return {d for d, count in holiday_counter.items() if count == total}


def load(run_doc) -> DataPackage:
	start_date = getdate(run_doc.date)
	mode = run_doc.mode

	# ── Optimizer settings ──────────────────────────────────────────────────
	settings = frappe.get_single("Optimizer Settings")
	fte_tolerance = float(settings.fte_tolerance_pct or 0.05)
	turnover_weight = float(settings.turnover_weight or 1.0)

	# ── Discipline-Designation-Branch Config ─────────────────────────────────
	config_rows = frappe.get_all(
		"Discipline Designation Branch Config",
		fields=["discipline", "employee_type", "branch", "max_rooms_for_employee_type", "rooms_num"],
	)
	if not config_rows:
		frappe.throw(frappe._("No Discipline Designation Branch Config records found. Please configure them first."))

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

	# Determine which designations are "assistant" type (max_rooms == 1 and not a doctor)
	# We treat any designation with max_rooms_for_employee_type == 1 that appears as an
	# assistant-level role as an assistant. The caller configures this via rooms_num and
	# max_rooms — we rely on the discipline config to know which designations cover rooms.
	# For simplicity: assistant designations are those with max_rooms_per_employee_type == 1
	# and rooms_num > 0. Doctors supervise multiple rooms.
	assistant_designations: dict[str, list[str]] = {d: [] for d in disciplines}
	for r in config_rows:
		if int(r.max_rooms_for_employee_type or 1) == 1:
			lst = assistant_designations.setdefault(r.discipline, [])
			if r.employee_type not in lst:
				lst.append(r.employee_type)

	# ── Employees ────────────────────────────────────────────────────────────
	valid_designations = {r.employee_type for r in config_rows}
	raw_employees = frappe.get_all(
		"Employee",
		filters={"status": "Active", "designation": ["in", list(valid_designations)]},
		fields=["name", "designation", "department", "holiday_list", "employment_type"],
	)

	# Employee Settings: FTE overrides
	emp_settings = {
		row.employee: row
		for row in frappe.get_all(
			"Employee Settings",
			fields=["employee", "fte"],
		)
	}

	# Shift preferences from Employee Settings child table.
	# Employee Settings are named by employee (autoname = field:employee),
	# so parent == employee name in the child rows.
	shift_pref_rows = frappe.get_all(
		"Employee Shift Preference",
		fields=["parent", "shift_type", "weight"],
	)
	shift_preferences: dict[str, dict[str, float]] = {}
	for row in shift_pref_rows:
		shift_preferences.setdefault(row.parent, {})[row.shift_type] = float(row.weight or 0.0)

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

		# Employment type: treat "Salaried" as salaried, everything else as turnover-paid
		is_salaried[name] = (emp.employment_type or "").lower() not in ("turnover", "commission", "casual")

		# FTE: prefer Employee Settings, fall back to Employee.custom_fte
		if name in emp_settings:
			fte_pct = float(emp_settings[name].fte or 100)
		else:
			fte_pct = float(frappe.db.get_value("Employee", name, "custom_fte") or 100)

		fte_fraction = fte_pct / 100.0
		# Two shifts per day × number of working days × FTE fraction
		n_slots = len(all_days) * 2
		target_shifts[name] = round(fte_fraction * n_slots)

		max_rpe[name] = max_rpe_by_desig.get(desig, 1)

	if mode == "Unbounded":
		global_holidays = _exclude_holidays(all_days, employee_holiday_lists)
		working_days = [d for d in all_days if d not in global_holidays]
	else:
		working_days = all_days

	# ── Shift Types ──────────────────────────────────────────────────────────
	shift_types = frappe.get_all("Shift Type", pluck="name")

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
	forced: set[tuple[str, str, datetime.date, str]] = set()
	if run_doc.disregard_assignments == "Use":
		existing = frappe.get_all(
			"Shift Assignment",
			filters={
				"employee": ["in", employees],
				"docstatus": 1,
				"start_date": ["between", [window_start, window_end]],
			},
			fields=["employee", "shift_type", "start_date", "branch"],
		)
		for sa in existing:
			forced.add((sa.employee, sa.shift_type, getdate(sa.start_date), sa.branch or ""))

	return DataPackage(
		employees=employees,
		shift_types=shift_types,
		working_days=working_days,
		branches=branches,
		designation=designation,
		department=department,
		is_salaried=is_salaried,
		target_shifts=target_shifts,
		max_rpe=max_rpe,
		rooms=rooms,
		disciplines=disciplines,
		assistant_designations=assistant_designations,
		leave_blocked=leave_blocked,
		forced=forced,
		shift_preferences=shift_preferences,
		fte_tolerance=fte_tolerance,
		turnover_weight=turnover_weight,
	)
