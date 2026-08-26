"""
Pure-Python helpers for the optimizer sandbox notebook (sandbox/playground.ipynb).

No Frappe imports — works against a captured DataPackage snapshot (see the
`capture-datapackage` bench command) loaded straight from JSON.
"""

from __future__ import annotations

import dataclasses
import datetime

import pandas as pd
import pulp

from autoshift.optimizer import model_builder
from autoshift.optimizer.rules import (
	BUILTIN_RULES,
	KIND_CONSTRAINT,
	KIND_OBJECTIVE,
	STANDARD_RULES,
	RuleContext,
	compile_custom_rule,
)
from autoshift.optimizer.types import DataPackage


def solve(
	data: DataPackage, time_limit: int = 30, msg: bool = False, solve=True
) -> tuple[pulp.LpProblem, dict[str, pulp.LpVariable], dict[str, pulp.LpVariable], str]:
	"""Build and solve the MILP for `data`. Returns (prob, x, active_rooms, logs)."""
	if isinstance(solve, tuple) and len(solve) >= 4:
		prob, x, active_rooms, logs = solve[:4]
	else:
		prob, x, active_rooms, logs, _ctx = model_builder.build(data)
	if solve:
		prob.solve(pulp.COIN_CMD(timeLimit=time_limit, msg=msg))
	return prob, x, active_rooms, logs


def status(prob) -> str:
	return pulp.LpStatus[prob.status]


def objective_breakdown(
	data: DataPackage, x: dict, active_rooms: dict, prob: pulp.LpProblem | None = None
) -> dict[str, float]:
	"""
	Per-rule contribution to the (solved) objective, keyed by rule name.

	Re-invokes each selected built-in objective rule against a throwaway problem,
	reusing the already-solved `x`/`active_rooms` variables so `pulp.value` reflects
	the real solution. A rule that introduces auxiliary variables (a linearized
	absolute value, say) creates fresh, unsolved ones on that throwaway problem, so
	their values are copied back from the solved problem by name — without that they
	are `None` and the rule silently reports 0.0. Only built-ins of kind "objective"
	are broken out —
	constraint rules never call `ctx.add_objective` (0 contribution, skipped), and
	Custom Code rules don't carry their `rule_kind` in a captured snapshot, so
	they're left out rather than risk re-running unknown code against the live `x`.
	"""
	specs = data.rules or tuple(
		(rule.title, key, "", 1.0) for key, rule in BUILTIN_RULES.items() if key in STANDARD_RULES
	)
	breakdown: dict[str, float] = {}
	for name, builtin_key, code, weight in specs:
		if (builtin_key and BUILTIN_RULES[builtin_key].kind != KIND_OBJECTIVE) or (
			not builtin_key and not name.startswith("Objective: ")
		):
			continue
		ctx = RuleContext(prob=pulp.LpProblem(), x=x, active_rooms=active_rooms, data=data)
		ctx._current_weight = weight
		if builtin_key and BUILTIN_RULES[builtin_key].kind == KIND_OBJECTIVE:
			BUILTIN_RULES[builtin_key].apply(ctx)
		elif not builtin_key and name.startswith("Objective: "):
			compile_custom_rule(name, code)(ctx)
		else:
			raise AssertionError("impossible")
		if prob is not None:
			_adopt_solved_values(ctx.prob, prob)
		breakdown[name] = pulp.value(pulp.lpSum(ctx.objective_terms)) or 0.0
	return breakdown


def _adopt_solved_values(throwaway: pulp.LpProblem, solved: pulp.LpProblem) -> None:
	"""Give the throwaway problem's fresh variables the values the real solve found."""
	solved_values = {v.name: v.varValue for v in solved.variables()}
	for var in throwaway.variables():
		if var.varValue is None and var.name in solved_values:
			var.varValue = solved_values[var.name]


def assignment_frame(data: DataPackage, x: dict) -> pd.DataFrame:
	"""One row per assigned (employee, role, shift_type, date, branch) slot."""
	rows = [
		{
			"employee": e,
			"scheduling_role": r,
			"shift_type": s,
			"date": d,
			"branch": b,
			"forced": (e, r, s, d, b) in data.forced,
			"assigned": pulp.value(var),
			"opportunity": var.dj,
		}
		for (e, r, s, d, b), var in x.items()
		if (pulp.value(var) or 0) > 0.0
	]
	frame = pd.DataFrame(
		rows,
		columns=["employee", "scheduling_role", "shift_type", "date", "branch", "forced", "assigned"],
	)
	return frame.sort_values(["date", "shift_type", "branch", "assigned", "employee"]).reset_index(drop=True)


def constraint_frame(prob: pulp.LpProblem) -> pd.DataFrame:
	"""One row per assigned (employee, shift_type, date, branch) slot."""
	rows = [
		{
			"len": len(str(c)),
			"type": str(c.name).partition(":")[0],
			"constraint": str(c),
			"slack": c.slack,
			"pi": c.pi,
		}
		for c in prob.constraints()
	]
	frame = pd.DataFrame(rows, columns=["len", "type", "constraint", "slack", "pi"])
	return frame.sort_values(by=["len", "slack", "constraint", "pi"]).reset_index(drop=True)


def room_utilization_frame(data: DataPackage, active_rooms: dict) -> pd.DataFrame:
	"""One row per (discipline, shift_type, date, branch) with staffed vs. capacity rooms."""
	rows = [
		{
			"discipline": k,
			"shift_type": s,
			"date": d,
			"branch": b,
			"staffed": int(pulp.value(var) or 0),
			"capacity": data.rooms.get((k, b), 0),
		}
		for (k, s, d, b), var in active_rooms.items()
	]
	frame = pd.DataFrame(rows, columns=["discipline", "shift_type", "date", "branch", "staffed", "capacity"])
	return frame.sort_values(["date", "shift_type", "branch", "discipline"]).reset_index(drop=True)


def schedule_grid(data: DataPackage, x: dict) -> pd.DataFrame:
	"""Employee x day grid of the solved schedule (rows=employees, columns=days).

	Simplified, offline cousin of ``OptimizerRun.get_schedule_events``: only "assigned"
	(from `x`) and "leave" (from `data.leave_blocked`) — no "existing" Shift Assignment
	overlay, since that lives in Frappe's DB, not the DataPackage.
	"""
	cell: dict[tuple[str, datetime.date], list[str]] = {}

	unique_s = {}
	unique_b = {}
	unique_r = {}

	for (e, r, s, d, b), var in x.items():
		if (pulp.value(var) or 0) <= 0.0:
			continue
		if s not in unique_s:
			unique_s[s] = len(unique_s)
		if b not in unique_b:
			unique_b[b] = len(unique_b)
		if r not in unique_r:
			unique_r[r] = len(unique_r)
		# role included: with multi-skill staff, which discipline somebody covers is the
		# part of the cell you cannot infer from shift type and branch
		cell.setdefault((e, d), []).append(f"{unique_s[s]}@{unique_b[b]}:{unique_r[r]}")
	for e, d in data.leave_blocked:
		cell.setdefault((e, d), []).append("LEAVE")

	grid = pd.DataFrame("", index=sorted(data.employees), columns=[d.isoformat() for d in data.working_days])
	for (e, d), labels in cell.items():
		grid.loc[e, d.isoformat()] = ", ".join(sorted(labels))
	return grid


def replace(data: DataPackage, **overrides) -> DataPackage:
	"""`DataPackage` is frozen; shorthand for `dataclasses.replace`."""
	return dataclasses.replace(data, **overrides)
