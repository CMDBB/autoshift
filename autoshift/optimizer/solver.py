"""
Runs the MILP solver for an Optimizer Run and persists the solution.
Called via Frappe's background job queue.
"""

from __future__ import annotations
import os

import traceback

import frappe
import pulp

from . import data_loader, model_builder


def run_solve(run_name: str) -> None:
	run = frappe.get_doc("Optimizer Run", run_name)
	try:
		data = data_loader.load(run)
		prob, x, _active_rooms = model_builder.build(data)

		logPath = "coin_run.log"
		solver = pulp.COIN_CMD(timeLimit=3600, logPath=logPath)
		prob.solve(solver)

		# Capture CBC log from PuLP's internal buffer
		with open("coin_run.log", "r") as f:
			run.set("solver_log", f.read())
		os.remove(logPath)

		lp_status = pulp.LpStatus[prob.status]

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
		else:
			run.set("solver_log", (str(run.get("solver_log")) or "") + f"\n\nSolver status: {lp_status}")
			run.db_set("status", "Failed")
			run.save(ignore_permissions=True)

	except Exception:
		tb = traceback.format_exc()
		frappe.log_error(tb, f"Optimizer Run failed: {run_name}")
		try:
			run.set("solver_log", (str(run.get("solver_log")) or "") + f"\n\nException:\n{tb}")
			run.db_set("status", "Failed")
			run.save(ignore_permissions=True)
		except Exception:
			pass
