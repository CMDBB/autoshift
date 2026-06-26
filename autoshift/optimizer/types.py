"""
Pure-Python data types for the optimizer. No Frappe imports — safe to use in tests.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
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

	# FTE targets (number of shifts, computed from fte% * horizon length * 2 shifts/day)
	target_shifts: dict[str, int]

	# max rooms this employee can cover in one slot (from Discipline-Designation-Branch Config)
	max_rpe: dict[str, int]

	# rooms[(discipline, branch)] -> capacity
	rooms: dict[tuple[str, str], int]

	# disciplines that appear in config
	disciplines: list[str]

	# leave blocklist: (employee, date) pairs that must be unassigned
	leave_blocked: set[tuple[str, datetime.date]]

	# forced assignments: (employee, shift_type, date, branch) fixed to 1
	forced: set[tuple[str, str, datetime.date, str]]

	# per-employee shift preference weights: employee -> {shift_type -> weight}
	shift_preferences: dict[str, dict[str, float]]

	# optimizer policy
	fte_tolerance: float  # e.g. 0.05 = ±5%
	turnover_weight: float

	def input_hash(self) -> str:
		"""
		Stable hash of every field that influences the MILP solution.
		Used to detect that two Optimizer Runs would solve to the same result,
		so a re-solve can be served from a previous run instead of re-run.
		Two runs sharing this hash are only guaranteed equivalent if the
		optimizer code itself hasn't changed in between (see developer_mode
		bypass at the call site).
		"""

		def normalize(value):
			if isinstance(value, dict):
				return {str(k): normalize(v) for k, v in value.items()}
			if isinstance(value, (set, frozenset)):
				return sorted((normalize(v) for v in value), key=repr)
			if isinstance(value, (list, tuple)):
				return [normalize(v) for v in value]
			if isinstance(value, datetime.date):
				return value.isoformat()
			return value

		payload = {f.name: normalize(getattr(self, f.name)) for f in dataclasses.fields(self)}
		blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
		return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def planning_days(start_date: datetime.date, mode: str) -> list[datetime.date]:
	"""Return the ordered list of working days for the given planning horizon."""
	weeks = {"1-week": 1, "2-week": 2, "4-week": 4}
	if mode not in weeks:
		raise NotImplementedError(f"Planning mode '{mode}' not yet implemented")
	return [start_date + datetime.timedelta(days=i) for i in range(weeks[mode] * 7)]
