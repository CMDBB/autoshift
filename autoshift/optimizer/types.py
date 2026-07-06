"""
Pure-Python data types for the optimizer. No Frappe imports — safe to use in tests.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import itertools
import json
import typing
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class DataPackage:
	FLAG = str
	IGNORE_ASSIGNMENTS: typing.ClassVar[FLAG] = "ignore"  # same as using an empty ``forced``
	WEIGH_ASSIGNMENTS: typing.ClassVar[FLAG] = "weigh"  # use ``forced`` as a soft initialization
	USE_ASSIGNMENTS: typing.ClassVar[FLAG] = "use"  # use ``forced`` as constraint

	flags: set[FLAG]

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
		Used to detect that a new Optimizer Run would solve on the same input
		as a previous one.
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

	def dumps(self) -> str:
		"""Serialize to a JSON string. `datetime.date`s become ISO strings; round-trips via `loads`."""
		payload = {
			"flags": [flag for flag in self.flags],
			"employees": self.employees,
			"shift_types": self.shift_types,
			"working_days": [d.isoformat() for d in self.working_days],
			"branches": self.branches,
			"designation": self.designation,
			"department": self.department,
			"target_shifts": self.target_shifts,
			"max_rpe": self.max_rpe,
			"rooms": [
				[discipline, branch, capacity] for (discipline, branch), capacity in self.rooms.items()
			],
			"disciplines": self.disciplines,
			"leave_blocked": [[employee, date.isoformat()] for employee, date in self.leave_blocked],
			"forced": [
				[employee, shift_type, date.isoformat(), branch]
				for employee, shift_type, date, branch in self.forced
			],
			"shift_preferences": self.shift_preferences,
			"fte_tolerance": self.fte_tolerance,
			"turnover_weight": self.turnover_weight,
		}
		return json.dumps(payload)

	@classmethod
	def loads(cls, raw: str) -> "DataPackage":
		"""Deserialize a JSON string produced by `dumps`."""
		payload = json.loads(raw)
		return cls(
			flags={flag for flag in payload["flags"]},
			employees=payload["employees"],
			shift_types=payload["shift_types"],
			working_days=[datetime.date.fromisoformat(d) for d in payload["working_days"]],
			branches=payload["branches"],
			designation=payload["designation"],
			department=payload["department"],
			target_shifts=payload["target_shifts"],
			max_rpe=payload["max_rpe"],
			rooms={(discipline, branch): capacity for discipline, branch, capacity in payload["rooms"]},
			disciplines=payload["disciplines"],
			leave_blocked={
				(employee, datetime.date.fromisoformat(date)) for employee, date in payload["leave_blocked"]
			},
			forced={
				(employee, shift_type, datetime.date.fromisoformat(date), branch)
				for employee, shift_type, date, branch in payload["forced"]
			},
			shift_preferences=payload["shift_preferences"],
			fte_tolerance=payload["fte_tolerance"],
			turnover_weight=payload["turnover_weight"],
		)


def planning_days(start_date_raw: datetime.date, mode: str) -> Iterable[datetime.date]:
	"""Return the ordered list of days for the given planning horizon."""
	if isinstance(start_date_raw, str):
		try:
			start_date = datetime.date.fromisoformat(start_date_raw)
		except ValueError:
			start_date = datetime.datetime.strptime(start_date_raw, "%Y-%m-%d").date()
	else:
		start_date = start_date_raw

	weeks = {
		"1-week": 1,
		"2-week": 2,
		"4-week": 4,
		"UnboundedTODO": None,
	}
	if mode not in weeks:
		raise NotImplementedError(f"Planning mode '{mode}' not yet implemented")
	weeks = weeks[mode]
	if weeks is None:  # Unbounded: infinite iterator -> let the caller decide how many days to take
		return (start_date + datetime.timedelta(days=i) for i in itertools.count())
	return [start_date + datetime.timedelta(days=i) for i in range(weeks * 7)]
