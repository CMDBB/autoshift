"""
Runs the MILP solver for an Optimizer Run and persists the solution.
Called directly (sync, short time limit) or via Frappe's background job queue
(async, full time limit) — see OptimizerRun.solve().
"""

from __future__ import annotations

import os
import traceback

import frappe
import pulp

from . import model_builder

# Statuses whose result can be safely reused for an identical input hash.
CACHEABLE_STATUSES = ("Solved", "Failed", "Approved", "Committed")


def find_cached_run(input_hash: str, exclude_name: str | None = None) -> str | None:
	"""
	Look up an existing Optimizer Run that already solved this exact input.

	An identical input always solves to the same result status, so a match means
	re-solving would be redundant (for now). Returns the matching run's name, or None.
	"""
	filters: dict = {"hash": input_hash, "status": ["in", CACHEABLE_STATUSES]}
	if exclude_name:
		filters["name"] = ["!=", exclude_name]
	return frappe.db.get_value("Optimizer Run", filters, "name", order_by="creation asc")


def run_solve(run_name: str, data, time_limit: int = 3600) -> str | None:
	"""
	Build and solve the MILP, then persist the solution onto the Optimizer Run.

	time_limit: seconds passed straight to CBC's own timeLimit. If CBC stops
	without reaching a conclusive status (LpStatus "Not Solved"), nothing is
	persisted and "TimedOut" is returned so the caller can escalate (e.g. to
	a background job with a longer time_limit) instead of treating it as a
	failed solve.

	The input hash is only known once the DataPackage exists (here), so it is
	computed and persisted as part of an actual solve attempt, not on the
	preemptive cache check in OptimizerRun.solve(), which must leave a Draft
	run's hash unset so the same Draft can be solved again later if the
	underlying data changes.

	Returns "Solved", "Failed", or "TimedOut".
	"""
	run = frappe.get_doc("Optimizer Run", run_name)
	try:
		run.set("hash", data.input_hash())

		prob, x, _active_rooms = model_builder.build(data)

		logPath = "coin_run.log"
		solver = pulp.COIN_CMD(timeLimit=time_limit, logPath=logPath)
		prob.solve(solver)

		with open(logPath) as f:
			solver_log = f.read()
		os.remove(logPath)

		lp_status = pulp.LpStatus[prob.status]

		if lp_status == "Not Solved":
			# Time limit hit before CBC reached a conclusive result; let the
			# caller decide whether to escalate rather than recording a failure.
			return "TimedOut"

		run.set("solver_log", solver_log)

		if lp_status == "Optimal":
			run.set("objective_value", pulp.value(prob.objective))
			run.set("solution_table", [])

			for (e, s, d, b), var in x.items():
				val = pulp.value(var)
				if val is not None and val > 0.5:
					is_forced = (e, s, d, b) in data.forced
					run.append(
						"solution_table",
						{
							"employee": e,
							"shift_type": s,
							"date": str(d),
							"branch": b,
							"forced": 1 if is_forced else 0,
						},
					)

			run.db_set("status", "Solved")
			run.save(ignore_permissions=True)
			return "Solved"
		else:
			run.set("solver_log", solver_log + f"\n\nSolver status: {lp_status}")
			run.db_set("status", "Failed")
			run.save(ignore_permissions=True)
			return "Failed"

	except Exception:
		tb = traceback.format_exc()
		frappe.log_error(tb, f"Optimizer Run failed: {run_name}")
		try:
			run.set("solver_log", (str(run.get("solver_log")) or "") + f"\n\nException:\n{tb}")
			run.db_set("status", "Failed")
			run.save(ignore_permissions=True)
		except Exception:
			pass
		return "Failed"
