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

#: `Scheduling Role.assignment_mode`. How a shift in the role is worked, and therefore what
#: a rota slot in it settles. **Flexible**: the slot means "present", and any non-exclusive
#: role the employee holds may fill it. **Exclusive**: worked in exactly this role, with no
#: collateral duty beside it and no swapping out of it. **Collateral**: a duty worked on top
#: of another shift at the same shift and branch, or on its own where the rooms are already
#: staffed.
MODE_FLEXIBLE = "Flexible"
MODE_EXCLUSIVE = "Exclusive"
MODE_COLLATERAL = "Collateral"


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

	# (employee, role) pairs whose schedule is settled and not the optimizer's to decide:
	# the `bind_role_assignments` rule freezes every one of their variables at the warm
	# start, so they work exactly the Shift Assignments already on the books and nothing
	# else. Sourced from Scheduling Role.assignments_binding, per-holder overridable via
	# Employee Scheduling Role.binding_override. Empty unless a role is marked binding.
	binding_pairs: frozenset[tuple[str, str]] = frozenset()

	# existing Shift Assignments dropped because the employee is on (or speculated on)
	# leave that day: (employee, role, shift_type, date, branch). Leave wins over a settled
	# schedule, but a planner needs to be told — the run-statistics panel reports these.
	# Sorted, so it does not perturb `input_hash` between otherwise identical runs.
	binding_conflicts: tuple[tuple[str, str, str, datetime.date, str], ...] = ()

	# existing Shift Assignments the loader could not place — no branch or discipline on
	# their Shift Location, or the employee holds no Scheduling Role in that discipline:
	# (employee, date, reason). They are dropped rather than fatal, because most of the
	# books are disregarded now and one mis-filed location should not abort a run; a bound
	# employee is the exception and still throws, since their schedule is input. Sorted,
	# so it does not perturb `input_hash` between otherwise identical runs.
	unresolved_assignments: tuple[tuple[str, datetime.date, str], ...] = ()

	# Substitution suitability of an (employee, role) pair, from Employee Scheduling
	# Role.suitability: 1 is a regular holder, 1.2 a good backup, 3 a terrible but feasible
	# one. Sparse — only pairs that differ from 1 are present, so a site that never touches
	# the Role Matrix hashes and solves exactly as before.
	role_suitability: dict[tuple[str, str], float] = dataclasses.field(default_factory=dict)

	# Assignment mode per role, from Scheduling Role.assignment_mode and the per-holder
	# override on Employee Scheduling Role. Sparse in two ways: only roles that are *not*
	# MODE_FLEXIBLE appear, and a pair whose holder overrides the role's mode is keyed by
	# (employee, role) rather than by the role. A site using neither hashes and solves
	# exactly as it did before modes existed.
	role_mode: dict[str, str] = dataclasses.field(default_factory=dict)
	role_mode_overrides: dict[tuple[str, str], str] = dataclasses.field(default_factory=dict)

	# Scheduling Role.gates_rooms: does a room in the discipline need somebody in this role.
	# Sparse and explicit — a role absent here falls back to `gates_rooms`'s default, which
	# is "every role gates except a collateral duty". Kept separate from the mode because the
	# two really are different questions: the mode says how a shift in the role is *worked*,
	# gating says whether a room waits on it. Most collateral duties do not gate and most
	# working roles do, but a floater or an administrative role is a working role that opens
	# no rooms, and a practice that may not run without a lead on site is a gating duty.
	role_gates_rooms: dict[str, bool] = dataclasses.field(default_factory=dict)

	# Scheduling Role.assignment_value: what one shift in the role is worth beyond the rooms
	# it staffs, in objective points. Sparse — only roles carrying a non-zero figure — so a
	# site that has never set one hashes and solves exactly as before. Read by
	# `rules.role_value_objective`; every other rule ignores it.
	role_value: dict[str, float] = dataclasses.field(default_factory=dict)

	def suitability(self, employee: str, role: str) -> float:
		return self.role_suitability.get((employee, role), 1.0)

	def mode(self, employee: str, role: str) -> str:
		"""How `employee` works `role`: their own override, else the role's own mode."""
		return self.role_mode_overrides.get((employee, role)) or self.role_mode.get(role, MODE_FLEXIBLE)

	def value_of(self, role: str) -> float:
		"""Objective points one shift in `role` is worth on its own. Zero unless set."""
		return self.role_value.get(role, 0.0)

	def gates_rooms(self, role: str) -> bool:
		"""Does room coverage in this role's discipline wait on somebody working it.

		A property of the role, not of the holder: a room either needs the role filled or it
		does not. The default keeps packages that predate the flag (and every hand-built one)
		reading the way the mode alone used to imply.
		"""
		explicit = self.role_gates_rooms.get(role)
		if explicit is not None:
			return explicit
		return self.role_mode.get(role, MODE_FLEXIBLE) != MODE_COLLATERAL

	def gating_roles(self, employee: str) -> tuple[str, ...]:
		"""The employee's roles a room in their discipline actually waits on."""
		return tuple(r for r in self.employee_roles.get(employee, ()) if self.gates_rooms(r))

	def is_collateral(self, employee: str, role: str) -> bool:
		"""A duty worked on top of a shift (or on its own), not a way of spending the shift."""
		return self.mode(employee, role) == MODE_COLLATERAL

	def working_roles(self, employee: str) -> tuple[str, ...]:
		"""The roles that *are* a way of spending a shift — everything but collateral duties.

		At most one of these is worked per presence, which is what makes presence and
		collateral duties countable separately.
		"""
		return tuple(r for r in self.employee_roles.get(employee, ()) if not self.is_collateral(employee, r))

	def collateral_roles(self, employee: str) -> tuple[str, ...]:
		return tuple(r for r in self.employee_roles.get(employee, ()) if self.is_collateral(employee, r))

	def exclusive_roles(self, employee: str) -> tuple[str, ...]:
		return tuple(
			r for r in self.employee_roles.get(employee, ()) if self.mode(employee, r) == MODE_EXCLUSIVE
		)

	def bound_employees(self) -> frozenset[str]:
		"""Whose presence is settled.

		Presence is personal: holding any binding role settles somebody's week, and a
		non-binding role they also hold only widens which role can fill a slot of it — it
		never adds a day. So the binding rules key on the employee, not on the pair.
		"""
		return frozenset(employee for employee, _role in self.binding_pairs)

	def forced_presence(self) -> set[tuple[str, str, datetime.date, str]]:
		"""(employee, shift, day, branch) slots the books put somebody at.

		Derived from `forced` rather than stored: a settled schedule is a set of shifts, and
		which role each is worked in is exactly the part the optimizer may now decide.
		"""
		return {(e, s, d, b) for e, _r, s, d, b in self.forced}

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
		if not self.role_suitability:
			# keep the cache hits of runs solved before the field existed
			del payload["role_suitability"]
		for name in ("role_mode", "role_mode_overrides", "role_gates_rooms", "role_value"):
			if not getattr(self, name):  # idem, for sites where every role is Flexible
				del payload[name]
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
			"binding_pairs": [[employee, role] for employee, role in sorted(self.binding_pairs)],
			"binding_conflicts": [
				[employee, role, shift_type, date.isoformat(), branch]
				for employee, role, shift_type, date, branch in self.binding_conflicts
			],
			"unresolved_assignments": [
				[employee, date.isoformat(), reason] for employee, date, reason in self.unresolved_assignments
			],
			"role_suitability": [
				[employee, role, factor] for (employee, role), factor in sorted(self.role_suitability.items())
			],
			"role_mode": self.role_mode,
			"role_gates_rooms": self.role_gates_rooms,
			"role_value": self.role_value,
			"role_mode_overrides": [
				[employee, role, mode] for (employee, role), mode in sorted(self.role_mode_overrides.items())
			],
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
			# absent from packages captured before role binding existed: nobody was bound
			binding_pairs=frozenset((employee, role) for employee, role in payload.get("binding_pairs", [])),
			binding_conflicts=tuple(
				(employee, role, shift_type, datetime.date.fromisoformat(date), branch)
				for employee, role, shift_type, date, branch in payload.get("binding_conflicts", [])
			),
			unresolved_assignments=tuple(
				(employee, datetime.date.fromisoformat(date), reason)
				for employee, date, reason in payload.get("unresolved_assignments", [])
			),
			# absent from packages captured before the Role Matrix existed: everyone was a holder
			role_suitability={
				(employee, role): factor for employee, role, factor in payload.get("role_suitability", [])
			},
			# absent from packages captured before assignment modes existed: every role was
			# a way of spending a whole shift, which is what MODE_FLEXIBLE means
			role_mode=payload.get("role_mode", {}),
			# absent before the flag existed: gating followed the mode, which is what
			# `gates_rooms` falls back to
			role_gates_rooms=payload.get("role_gates_rooms", {}),
			# absent before the field existed: a shift was worth exactly what it staffed
			role_value=payload.get("role_value", {}),
			role_mode_overrides={
				(employee, role): mode for employee, role, mode in payload.get("role_mode_overrides", [])
			},
		)


#: Outcomes of :func:`resolve_assignment_role`.
ROLE_RESOLVED = "resolved"
ROLE_NOT_HELD = "not_held"  # the record names a role this employee does not hold
ROLE_NO_DISCIPLINE = "no_discipline"  # nothing to infer from: no role, no location discipline
ROLE_NONE_IN_DISCIPLINE = "none_in_discipline"  # they hold no role where the record puts them
ROLE_AMBIGUOUS = "ambiguous"  # they hold several there, and a shift names only one


def resolve_assignment_role(
	recorded: str | None,
	held: Iterable[str],
	discipline: str | None,
	candidates: Iterable[str],
	binding: Iterable[str] = (),
) -> tuple[str, str | None]:
	"""
	Which Scheduling Role an existing Shift Assignment was worked in.

	The record's own `custom_scheduling_role` if it has one; failing that, the single role the
	employee holds in the Shift Location's discipline. Several candidates are still resolved
	when exactly one of them is `binding` — a settled week is presence, not a choice of role
	(see `rules.soft_bind_role_assignments`), so the role a bound half-day was worked in is the
	one the books already settle for them, not a guess. **Never a choice between two otherwise.**
	The loader used to pick by sort order, which was tolerable while a person had one role in a
	discipline and is wrong now: a rota settles when somebody is in, and which of their roles
	the half-day went to is the part that cannot be read off where they stood.

	Split out of `data_loader` because it is the one piece of that module worth testing on
	its own. Returns `(outcome, role)`, with the role set only on `ROLE_RESOLVED`; the caller
	turns the other outcomes into messages, since only it knows the record they are about.
	"""
	candidates = list(candidates)
	if recorded:
		return (ROLE_RESOLVED, recorded) if recorded in set(held) else (ROLE_NOT_HELD, None)
	if not discipline:
		return (ROLE_NO_DISCIPLINE, None)
	if not candidates:
		return (ROLE_NONE_IN_DISCIPLINE, None)
	if len(candidates) > 1:
		binding_candidates = [c for c in candidates if c in set(binding)]
		if len(binding_candidates) == 1:
			return (ROLE_RESOLVED, binding_candidates[0])
		return (ROLE_AMBIGUOUS, None)
	return (ROLE_RESOLVED, candidates[0])


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
