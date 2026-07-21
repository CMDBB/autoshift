"""
Pure-Python helpers for the optimizer sandbox notebook (sandbox/playground.ipynb).

No Frappe imports — works against a captured DataPackage snapshot (see the
`capture-datapackage` bench command) loaded straight from JSON.
"""

from __future__ import annotations

import dataclasses

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
	if isinstance(solve, tuple) and len(solve) == 4:
		prob, x, active_rooms, logs = solve
	else:
		prob, x, active_rooms, logs = model_builder.build(data)
	if solve:
		prob.solve(pulp.COIN_CMD(timeLimit=time_limit, msg=msg))
	return prob, x, active_rooms, logs


def status(prob) -> str:
	return pulp.LpStatus[prob.status]


def objective_breakdown(data: DataPackage, x: dict, active_rooms: dict) -> dict[str, float]:
	"""
	Per-rule contribution to the (solved) objective, keyed by rule name.

	Re-invokes each selected built-in objective rule against a throwaway problem,
	reusing the already-solved `x`/`active_rooms` variables so `pulp.value` reflects
	the real solution. Only built-in rules of kind "objective" are broken out —
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
		breakdown[name] = pulp.value(pulp.lpSum(ctx.objective_terms)) or 0.0
	return breakdown


def assignment_frame(data: DataPackage, x: dict) -> pd.DataFrame:
	"""One row per assigned (employee, shift_type, date, branch) slot."""
	rows = [
		{
			"employee": e,
			"shift_type": s,
			"date": d,
			"branch": b,
			"forced": (e, s, d, b) in data.forced,
			"ass": pulp.value(var),
		}
		for (e, s, d, b), var in x.items()
		if (pulp.value(var) or 0) > 0.0
	]
	frame = pd.DataFrame(rows, columns=["employee", "shift_type", "date", "branch", "forced", "ass"])
	return frame.sort_values(["date", "shift_type", "branch", "ass", "employee"]).reset_index(drop=True)


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


def replace(data: DataPackage, **overrides) -> DataPackage:
	"""`DataPackage` is frozen; shorthand for `dataclasses.replace`."""
	return dataclasses.replace(data, **overrides)
