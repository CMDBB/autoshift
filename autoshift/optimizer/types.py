"""
Pure-Python data types for the optimizer. No Frappe imports — safe to use in tests.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass
class DataPackage:
	# Index sets
	employees: list[str]
	shift_types: list[str]
	working_days: list[datetime.date]
	branches: list[str]

	# Employee attributes
	designation: dict[str, str]  # employee -> designation name
	department: dict[str, str]  # employee -> department (discipline) name
	is_salaried: dict[str, bool]

	# FTE targets (number of shifts, computed from fte% * horizon length * 2 shifts/day)
	target_shifts: dict[str, int]

	# max rooms this designation can cover in one slot (e.g. 3 for orthodontists, 1 otherwise)
	max_rpe: dict[str, int]

	# rooms[(discipline, branch)] -> capacity
	rooms: dict[tuple[str, str], int]

	# disciplines that appear in config
	disciplines: list[str]

	# assistant designations per discipline: discipline -> [designation, ...]
	assistant_designations: dict[str, list[str]]

	# leave blocklist: (employee, date) pairs that must be unassigned
	leave_blocked: set[tuple[str, datetime.date]]

	# forced assignments: (employee, shift_type, date, branch) fixed to 1
	forced: set[tuple[str, str, datetime.date, str]]

	# per-employee shift preference weights: employee -> {shift_type -> weight}
	# missing entries default to 0.0 (neutral); higher = more preferred
	shift_preferences: dict[str, dict[str, float]]

	# optimizer policy
	fte_tolerance: float  # e.g. 0.05 = ±5%
	turnover_weight: float


def planning_days(start_date: datetime.date, mode: str) -> list[datetime.date]:
	"""Return the ordered list of working days for the given planning horizon."""
	weeks = {"1-week": 1, "2-week": 2, "4-week": 4}
	if mode not in weeks:
		raise NotImplementedError(f"Planning mode '{mode}' not yet implemented")
	return [start_date + datetime.timedelta(days=i) for i in range(weeks[mode] * 7)]
