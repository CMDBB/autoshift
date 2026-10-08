"""
Runs the MILP solver for an Optimizer Run and persists the solution.
Called directly (sync, short time limit) or via Frappe's background job queue
(async, full time limit) — see OptimizerRun.solve().
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback

import frappe
import pulp

from autoshift.optimizer.types import DataPackage

from . import diagnostics, model_builder
from .rules import BREAKDOWN_VERSION, objective_tree

# Statuses whose result can be safely reused for an identical input hash.
CACHEABLE_STATUSES = ("Solved", "Failed", "Approved", "Committed")

# CBC statuses that mean "the input is wrong", not "the solve went badly". These are the
# ones worth spending a second solve on to explain (see _explain_failure).
DIAGNOSABLE_STATUSES = ("Infeasible", "Undefined", "Unbounded")

# Seconds the failure diagnosis may spend on its own elastic solve. It runs after a solve
# has already failed, so a planner is waiting on it — keep it well under the sync budget.
DIAGNOSIS_TIME_LIMIT = 30


def find_cached_runs(input_hash: str, exclude_name: str | None = None) -> list[str]:
	"""
	Look up existing Optimizer Runs that already solved this exact input.

	An identical input normally solves to the same result status, so a match means
	re-solving could be redundant. Returns a list of the matching run's names.
	"""
	filters: dict = {"hash": input_hash, "status": ["in", CACHEABLE_STATUSES]}
	if exclude_name:
		filters["name"] = ["!=", exclude_name]
	return frappe.get_all("Optimizer Run", filters, order_by="creation asc")


def _explain_failure(data: DataPackage) -> str:
	"""
	Why this input has no schedule, as text for the run's Solver Log.

	CBC's own answer to an infeasible model is the word "Infeasible", which is no use at
	all when the suspicion is that the Shift Assignments a binding role froze in place are
	illegal under the ruleset. `diagnostics.report` names the offending assignments and,
	failing that, the constraints that have to give. Never allowed to raise: it runs on
	the failure path, and a broken explanation must not replace the failure it explains.
	"""
	try:
		return diagnostics.report(data, time_limit=DIAGNOSIS_TIME_LIMIT)
	except Exception:
		return f"--- Diagnostics ---\nDiagnostics failed:\n{traceback.format_exc()}"


def run_solve(run_name: str, data: DataPackage, time_limit: int = 3600) -> bool | None:
	"""
	Build and solve the MILP, then persist the solution onto the Optimizer Run.

	:param str run_name: we use the name, not the object (or a reference to it) because we want
		run_solve's arguments to be serializable
	:param DataPackage data: (see DataPackage)
	:param int time_limit: seconds passed straight to CBC's own timeLimit. If CBC stops
		without reaching a conclusive status (LpStatus "Not Solved"), nothing is
		persisted and "TimedOut" is returned so the caller can escalate (e.g. to
		a background job with a longer time_limit) instead of treating it as a
		failed solve.

	:returns: True if timed out, Falsy otherwise
	"""
	run = frappe.get_doc("Optimizer Run", run_name)
	try:
		run.set("hash", data.input_hash())

		prob, x, active_rooms, rule_logs, ctx = model_builder.build(data)

		# CBC runs as a subprocess, so the only sane way to capture output is through a file
		fd, log_path = tempfile.mkstemp(prefix="cbc_", suffix=".log")
		os.close(fd)
		try:
			solver = pulp.COIN_CMD(timeLimit=time_limit, logPath=log_path, warmStart=True)
			prob.solve(solver)

			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal (is a tempfile -> safe)
			with open(log_path) as f:
				solver_log = f.read()
		finally:
			os.unlink(log_path)

		lp_status = pulp.LpStatus[prob.status]

		if lp_status == "Not Solved":
			# Time limit hit before CBC reached a conclusive result; let the
			# caller decide whether to escalate rather than recording a failure.
			return True

		run.set(
			"solver_log",
			f"--- Rules Logs ---\n{rule_logs}\n--- Solver Logs ---\n{solver_log}",
		)

		if lp_status == "Optimal":
			# value() is None for a constant (e.g. objective-less feasibility) problem
			run.set("objective_value", pulp.value(prob.objective) or 0.0)
			run.set("solution_table", [])

			# Exact per-holder room numbers, where `room_coverage_matched_rooms` measured
			# them — preferred over `room_load`'s linearized estimate wherever both exist,
			# since a real room index needs no approximation.
			occupied_rooms: dict[tuple, list[int]] = {}
			for (e, r, s, d, b, n), var in ctx.room_occupancy.items():
				if (pulp.value(var) or 0) > 0.5:
					occupied_rooms.setdefault((e, r, s, d, b), []).append(n)

			for comb, var in x.items():
				val = pulp.value(var)
				if val is not None and val > 0.5:
					e, r, s, d, b = comb
					rooms_here = occupied_rooms.get(comb)
					if rooms_here is not None:
						rooms_here = sorted(rooms_here)
						rooms_taken = len(rooms_here)
						room_index = ",".join(str(n) for n in rooms_here)
					elif comb in ctx.room_load:
						# 0 = not measured: without a room-coverage rule that measures load,
						# a holder is credited their whole max-rooms figure and nothing says
						# how many of those rooms they actually take
						rooms_taken = 1 + round(sum(pulp.value(t) or 0 for t in ctx.room_load[comb]))
						room_index = ""
					else:
						rooms_taken = 0
						room_index = ""
					run.append(
						"solution_table",
						{
							"employee": e,
							"scheduling_role": r,
							"shift_type": s,
							"date": str(d),
							"branch": b,
							"collateral": 1 if data.is_collateral(e, r) else 0,
							"forced": 1 if comb in data.forced else 0,
							"rooms": rooms_taken,
							"room_index": room_index,
						},
					)

			# Room-coverage result: the active_rooms variable values, one row per
			# (discipline, shift, day, branch) slot with configured capacity. Zero-staffed
			# rows are kept deliberately — an empty slot is what a planner needs to see.
			run.set("coverage_table", [])
			for (k, s, d, b), var in active_rooms.items():
				capacity = data.rooms.get((k, b), 0)
				if not capacity:
					continue
				run.append(
					"coverage_table",
					{
						"discipline": k,
						"branch": b,
						"date": str(d),
						"shift_type": s,
						"staffed_rooms": int(pulp.value(var) or 0),
						"capacity": capacity,
					},
				)

			# Per-rule share of the solved objective (weighted, so shares sum to the
			# objective value up to rounding), each rule broken down by where in the
			# schedule it earned that share — see `rules.objective_tree`. Constraint
			# rules contribute nothing and are absent.
			run.set(
				"objective_breakdown",
				# Compact: a four-week run over a few dozen people is thousands of nodes,
				# and this is a payload the statistics panel reads, not prose.
				json.dumps(
					{"version": BREAKDOWN_VERSION, "rules": objective_tree(ctx)},
					separators=(",", ":"),
				),
			)

			run.set("status", "Solved")
		else:
			explanation = _explain_failure(data) if lp_status in DIAGNOSABLE_STATUSES else ""
			run.set(
				"solver_log",
				f"Solver status: {lp_status}\n\n{explanation}\n\n"
				f"--- Rules Logs ---\n{rule_logs}\n--- Solver Logs ---\n{solver_log}",
			)
			run.set("status", "Failed")
	except Exception:
		tb = traceback.format_exc()
		frappe.log_error(tb, f"Optimizer Run failed: {run_name}")
		run.set(
			"solver_log",
			f"{(str(run.get('solver_log')) or '')}\n\nException:\n{tb}\n\n{_explain_failure(data)}",
		)
		run.set("status", "Failed")
	finally:
		run.save(ignore_permissions=True)
