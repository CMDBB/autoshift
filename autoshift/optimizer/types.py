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

	# Scheduling Roles. A role is the optimizer's unit of capability: it names exactly one
	# discipline, and an employee may hold several. This is what replaces the old
	# one-employee-one-department/designation assumption — `Employee.department` and
	# `.designation` are HR/payroll data and deliberately absent here.
	roles: list[str]
	role_discipline: dict[str, str]  # role -> discipline (Department) name
	employee_roles: dict[str, tuple[str, ...]]  # employee -> roles held over this horizon

	# FTE targets (number of shifts, computed from fte% * horizon length * 2 shifts/day)
	target_shifts: dict[str, int]

	# Agreed FTE per role, as a shift count: (employee, role) -> target. Only present where
	# an Employee Scheduling Role names one. Informal — a soft objective, never a constraint.
	role_target_shifts: dict[tuple[str, str], float]

	# max rooms this (employee, role) pair covers in one slot (from Scheduling Role,
	# optionally overridden per Employee Scheduling Role)
	max_rpe: dict[tuple[str, str], int]

	# rooms[(discipline, branch)] -> capacity
	rooms: dict[tuple[str, str], int]

	# disciplines that appear in config (every value of role_discipline is one of these)
	disciplines: list[str]

	# leave blocklist: (employee, date) pairs that must be unassigned
	leave_blocked: set[tuple[str, datetime.date]]

	# forced assignments: (employee, role, shift_type, date, branch) fixed to 1. A Shift
	# Assignment records no role, so the loader resolves one from its Shift Location's
	# discipline — see data_loader._resolve_forced_role.
	forced: set[tuple[str, str, str, datetime.date, str]]

	# per-employee shift preference weights: employee -> {shift_type -> weight}
	shift_preferences: dict[str, dict[str, float]]

	# rules selected for this run, as (rule_document_name, builtin_key, custom_code, weight)
	# tuples from the run's Optimization Ruleset. Exactly one of builtin_key/custom_code
	# is non-empty per tuple; weight scales the rule's objective contribution (no effect
	# on constraint rules). Empty tuple = apply every built-in rule at weight 1.0
	# (pre-ruleset behaviour, kept for tests and cached packages serialized before
	# rulesets existed).
	RULE_DOCUMENT_NAME = str
	BUILTIN_KEY = str
	CUSTOM_CODE = str
	WEIGHT = float
	RULE = tuple[RULE_DOCUMENT_NAME, BUILTIN_KEY, CUSTOM_CODE, WEIGHT]
	rules: tuple[RULE, ...] = ()

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
			"roles": self.roles,
			"role_discipline": self.role_discipline,
			"employee_roles": {e: list(roles) for e, roles in self.employee_roles.items()},
			"target_shifts": self.target_shifts,
			"role_target_shifts": [
				[employee, role, target] for (employee, role), target in self.role_target_shifts.items()
			],
			"max_rpe": [[employee, role, cap] for (employee, role), cap in self.max_rpe.items()],
			"rooms": [
				[discipline, branch, capacity] for (discipline, branch), capacity in self.rooms.items()
			],
			"disciplines": self.disciplines,
			"leave_blocked": [[employee, date.isoformat()] for employee, date in self.leave_blocked],
			"forced": [
				[employee, role, shift_type, date.isoformat(), branch]
				for employee, role, shift_type, date, branch in self.forced
			],
			"shift_preferences": self.shift_preferences,
			"rules": [list(rule) for rule in self.rules],
		}
		return json.dumps(payload)

	@classmethod
	def loads(cls, raw: str) -> "DataPackage":
		"""Deserialize a JSON string produced by `dumps`."""
		payload = json.loads(raw)
		if "department" in payload or "roles" not in payload:
			raise ValueError(
				"This snapshot predates Scheduling Role and cannot be loaded: it carries "
				"one department per employee where the optimizer now needs the roles they "
				"hold. Recapture it with `bench capture-datapackage`."
			)
		return cls(
			flags={flag for flag in payload["flags"]},
			employees=payload["employees"],
			shift_types=payload["shift_types"],
			working_days=[datetime.date.fromisoformat(d) for d in payload["working_days"]],
			branches=payload["branches"],
			roles=payload["roles"],
			role_discipline=payload["role_discipline"],
			employee_roles={e: tuple(roles) for e, roles in payload["employee_roles"].items()},
			target_shifts=payload["target_shifts"],
			role_target_shifts={
				(employee, role): target for employee, role, target in payload["role_target_shifts"]
			},
			max_rpe={(employee, role): cap for employee, role, cap in payload["max_rpe"]},
			rooms={(discipline, branch): capacity for discipline, branch, capacity in payload["rooms"]},
			disciplines=payload["disciplines"],
			leave_blocked={
				(employee, datetime.date.fromisoformat(date)) for employee, date in payload["leave_blocked"]
			},
			forced={
				(employee, role, shift_type, datetime.date.fromisoformat(date), branch)
				for employee, role, shift_type, date, branch in payload["forced"]
			},
			shift_preferences=payload["shift_preferences"],
			# pad pre-weight 3-element specs (cached packages) with weight 1.0
			rules=tuple(
				(rule[0], rule[1], rule[2], rule[3] if len(rule) > 3 else 1.0)
				for rule in payload.get("rules", [])
			),
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

	match mode:
		case "Unbounded":  # infinite -> caller decides how many to take
			return (start_date + datetime.timedelta(days=i) for i in itertools.count())
		case "1-week" | "2-week" | "4-week":
			n_days = int(mode[0]) * 7
			return [start_date + datetime.timedelta(days=i) for i in range(n_days)]
		case _:
			raise NotImplementedError(f"Planning mode {mode!r} not yet implemented")
