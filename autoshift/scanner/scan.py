# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Walking a scan's windows: one Optimizer Run each, one checkpoint each.

The Frappe half of the horizon scanner. `scanner/horizon.py` holds everything that can be
decided without a database and is unit-tested there; this module creates the runs, solves
them, and writes the verdict back.

**One `Optimizer Run` per window, kept.** A scan is a sequence of ordinary runs
(`type = "Scan"`), which is what makes a flagged day worth clicking: the wall chart, the
statistics panel and the solver log all already read a run, so the drill-down costs
nothing. It also means a window that has not changed need not be solved twice — the run's
`input_hash` is the same hash every other caching path uses, so `find_cached_runs` hands
back the earlier run and the window links it instead (`reuse_cached`, on by default).

**Checkpoint per window.** Each window commits as soon as it is solved, so a scan survives
a worker restart, a `Ctrl-C` and a cancel: whatever was solved is solved, and resuming
picks up at the first `Pending` row. Nothing here is transactional across windows on
purpose — a half-finished scan is a *useful* scan, and the calendar says which days have
no verdict yet rather than pretending they are clean.

**A scan never writes a `Shift Assignment`.** It is dozens of solves and a solve reads; the
settled days a bound employee's rota implies reach the model through
`rota.materialize.settled_rows` exactly as they do for a single run.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

import frappe

from autoshift.optimizer import solver
from autoshift.optimizer.types import planning_days
from autoshift.scanner import horizon
from autoshift.utils import background_workers_alive

#: Seconds a single window's solve may take before the scan gives up on it and moves on.
#: Lower than a hand-driven run's hour: a scan is a sweep, and one pathological week must
#: not hold the other fifty-one hostage. The window is left `Timed Out` and retried by the
#: next resume, so nothing is lost by being impatient here.
DEFAULT_WINDOW_TIME_LIMIT = 120

#: Total wall-clock budget for one sweep job. A sweep that runs out leaves its remaining
#: windows `Pending` and the scan on "Scanning" — Resume picks them up, which is the same
#: path a killed worker takes. Generous, because the point is that a long scan finishes.
JOB_TIMEOUT = 6 * 3600

STATUS_PENDING = "Pending"
STATUS_SOLVING = "Solving"
STATUS_SOLVED = "Solved"
STATUS_FAILED = "Failed"
STATUS_TIMED_OUT = "Timed Out"


def job_id(scan_name: str) -> str:
	return f"autoshift-horizon-scan::{scan_name}"


# ── planning the windows ────────────────────────────────────────────────────────


def plan(scan) -> None:
	"""Bring the window rows in line with the scan's own start/end/mode.

	Idempotent, and **never discards a solved window**: a row whose span the new horizon
	still contains keeps its run and its findings, so widening a scan by a month costs
	only the new months. Rows the horizon no longer covers are dropped — they describe a
	window nobody asked about any more, and their runs survive as records regardless.
	"""
	start = frappe.utils.getdate(scan.start_date)
	end = frappe.utils.getdate(scan.end_date)
	if end < start:
		frappe.throw(frappe._("A scan's end date cannot precede its start date."))

	windows = horizon.plan_windows(start, end, scan.mode)
	rows = scan.get("windows") or []
	spans = [(frappe.utils.getdate(row.window_start), frappe.utils.getdate(row.window_end)) for row in rows]

	if spans != windows:
		# Only when the horizon actually moved: re-appending deletes and re-inserts every
		# child row, and a sweep in flight is holding their names.
		existing = {span: row for span, row in zip(spans, rows, strict=True)}
		scan.set("windows", [])
		for window in windows:
			row = existing.get(window)
			if row is not None:
				scan.append("windows", _row_values(row))
			else:
				scan.append(
					"windows",
					{"window_start": window[0], "window_end": window[1], "status": STATUS_PENDING},
				)

	scan.weeks_total = len(windows)
	scan.weeks_done = sum(1 for row in scan.get("windows") or [] if row.status not in horizon.UNSCANNED)


def _row_values(row) -> dict:
	"""A window row's own fields, free of the parent/idx bookkeeping a re-append redoes."""
	return {
		field: row.get(field)
		for field in (
			"window_start",
			"window_end",
			"status",
			"optimizer_run",
			"input_hash",
			"reused",
			"severity",
			"message",
			"findings",
		)
	}


def coverage_rule_gap(ruleset: str) -> bool:
	"""Does this ruleset leave `active_rooms` unconstrained, making the verdict meaningless?

	The scan's headline finding is room coverage, read straight off the solver's
	`active_rooms`. A ruleset selecting no rule from the `room_coverage` choice group never
	ties that variable to who is actually staffing the slot, so every cell would read as
	shut and the whole calendar would go red for a reason that has nothing to do with the
	schedule. Worth saying once, at save time, rather than fifty-two times in a sweep.
	"""
	from autoshift.optimizer import data_loader
	from autoshift.optimizer.rules import BUILTIN_RULES, GROUP_ROOM_COVERAGE

	names = frappe.get_all(
		"Optimization Ruleset Rule",
		filters={"parent": ruleset, "parenttype": "Optimization Ruleset"},
		pluck="rule",
	)
	keys = data_loader.builtin_keys_of(names)
	if not keys:
		# An empty selection means "every built-in at weight 1" (see `rules.apply_rules`),
		# which includes a coverage rule.
		return False
	return not any(BUILTIN_RULES[key].group == GROUP_ROOM_COVERAGE for key in keys if key in BUILTIN_RULES)


# ── running the sweep ───────────────────────────────────────────────────────────


def start(scan_name: str, resume: bool = True) -> str:
	"""Queue the sweep. Returns the scan's new status.

	`resume=False` resets every window first, which is the re-run of a whole scan: the
	rows go back to `Pending` and their runs are unlinked (never deleted — a run is a
	record). With `reuse_cached` still on, an unchanged window re-links the very run it
	had, so a re-run of a scan nothing has changed under costs one `DataPackage` per
	window and no solve at all.
	"""
	scan = frappe.get_doc("Horizon Scan", scan_name)
	scan.check_permission("write")

	# Checked before anything is written: a throw rolls the request back, but the status
	# has to be committed before the job is queued (the worker reads it), so marking the
	# scan "Scanning" first would leave it saying so for ever.
	if not background_workers_alive():
		frappe.throw(
			frappe._(
				"A scan runs as a background job, but no background worker is running to pick "
				"it up. Start one (e.g. <code>bench worker</code>, or run the bench via "
				"<code>bench start</code>), or sweep it from the shell with "
				"<code>bench scan-horizon --scan {0}</code>."
			).format(scan_name)
		)

	if not resume:
		reset(scan)
	elif not any(row.status == STATUS_PENDING for row in scan.get("windows") or []):
		frappe.throw(frappe._("Every window of this scan has been solved. Re-run it to solve again."))

	scan.db_set({"cancel_requested": 0, "status": "Scanning"}, update_modified=False)
	# The worker reads this row; it has to be visible before the job is queued.
	frappe.db.commit()  # nosemgrep

	frappe.enqueue(
		"autoshift.scanner.scan.sweep",
		scan_name=scan_name,
		queue="long",
		timeout=JOB_TIMEOUT,
		job_id=job_id(scan_name),
		deduplicate=True,
	)
	return "Scanning"


def request_stop(scan_name: str) -> str:
	"""Ask the sweep to stop after the window it is on.

	Cooperative rather than a kill: the running solve is CBC in a subprocess and the
	window it belongs to is worth finishing, since a window abandoned mid-solve would be
	re-solved from scratch on resume anyway.
	"""
	frappe.db.set_value("Horizon Scan", scan_name, "cancel_requested", 1, update_modified=False)
	# The flag is a message to another process, so it is worth nothing uncommitted.
	frappe.db.commit()  # nosemgrep
	return "Stopping"


def reset(scan, windows: list[str] | None = None) -> None:
	"""Put windows back to `Pending`, unlinking (never deleting) the runs they solved."""
	for row in scan.get("windows") or []:
		if windows is not None and row.name not in windows:
			continue
		row.status = STATUS_PENDING
		row.optimizer_run = None
		row.input_hash = None
		row.reused = 0
		row.severity = None
		row.message = None
		row.findings = None
	scan.weeks_done = sum(1 for row in scan.get("windows") or [] if row.status not in horizon.UNSCANNED)
	scan.save()


def sweep(scan_name: str) -> None:
	"""Solve every `Pending` window of the scan, oldest first, checkpointing as it goes.

	The entry point of the background job, and of `bench scan-horizon`. Safe to call on a
	scan that is already finished (it finds nothing to do) and safe to call twice in a row
	— the job is deduplicated on `job_id`, and the row statuses are the real interlock.
	"""
	scan = frappe.get_doc("Horizon Scan", scan_name)
	pending = [row.name for row in (scan.get("windows") or []) if row.status == STATUS_PENDING]

	for row_name in pending:
		if frappe.db.get_value("Horizon Scan", scan_name, "cancel_requested"):
			_finish(scan_name, "Paused")
			return
		scan = frappe.get_doc("Horizon Scan", scan_name)
		row = next((r for r in scan.get("windows") if r.name == row_name), None)
		if row is None or row.status != STATUS_PENDING:
			continue  # the scan was re-planned under us; its new rows are the job's next run
		solve_window(scan, row)
		# The checkpoint, and the reason a scan is resumable: one window's result is never
		# held hostage by the next one's solve (see this module's docstring).
		frappe.db.commit()  # nosemgrep

	_finish(scan_name, "Complete")


def _finish(scan_name: str, status: str) -> None:
	scan = frappe.get_doc("Horizon Scan", scan_name)
	done = sum(1 for row in (scan.get("windows") or []) if row.status not in horizon.UNSCANNED)
	if status == "Complete" and any(row.status == STATUS_PENDING for row in scan.get("windows") or []):
		status = "Paused"  # a re-plan added rows while the sweep was draining the old ones
	scan.db_set(
		{
			"status": status,
			"weeks_done": done,
			"cancel_requested": 0,
			"last_scanned_on": frappe.utils.now_datetime(),
		},
		update_modified=False,
	)
	# End of a long job: nothing else will commit this.
	frappe.db.commit()  # nosemgrep


def solve_window(scan, row) -> str:
	"""Solve one window and write its verdict onto `row`. Returns the row's new status.

	The run is created first and kept whatever happens: its `DataPackage` is what the
	findings are computed against, and a failed window's run is the one a planner most
	wants to open (its Solver Log carries `diagnostics.report`).
	"""
	_set_row(scan, row, {"status": STATUS_SOLVING})
	# Published before the solve, so the page can say which window is in flight.
	frappe.db.commit()  # nosemgrep

	run = _draft_run(scan, row)
	try:
		data = run.cache_datapackage()
		input_hash = data.input_hash()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Horizon scan window failed to load: {row.window_start}")
		run.db_set("status", "Failed", update_modified=False)
		return _record_failure(scan, row, run, frappe._("The window's data could not be loaded."))

	reused = None
	if scan.reuse_cached:
		cached = solver.find_cached_runs(input_hash, exclude_name=run.name)
		if cached:
			reused = frappe.get_doc("Optimizer Run", cached[0])

	if reused is not None:
		# Nothing about this window's input has changed since that run solved it, and an
		# identical input solves to an identical schedule. Keep the earlier run, drop the
		# draft this window made: an empty Draft run per scanned week is the sort of
		# litter that makes the run list useless.
		from autoshift.autoshift.doctype.optimizer_run.optimizer_run import datapackage_cache_key

		if frappe.cache is not None:
			frappe.cache.delete_value(datapackage_cache_key(run.name))
		run.delete(ignore_permissions=True)
		run = reused
	else:
		timed_out = solver.run_solve(
			run.name, data, time_limit=int(scan.window_time_limit or DEFAULT_WINDOW_TIME_LIMIT)
		)
		if timed_out:
			return _record_failure(
				scan,
				row,
				run,
				frappe._("No conclusive result within {0}s — resume the scan to retry it.").format(
					int(scan.window_time_limit or DEFAULT_WINDOW_TIME_LIMIT)
				),
				status=STATUS_TIMED_OUT,
			)
		run.reload()

	if run.status not in ("Solved", "Approved", "Committed"):
		return _record_failure(
			scan, row, run, frappe._("The window has no feasible schedule."), input_hash=input_hash
		)

	days = list(planning_days(frappe.utils.getdate(row.window_start), scan.mode))
	findings = horizon.window_findings(
		data,
		[slot.as_dict() for slot in run.get("solution_table") or []],
		[cell.as_dict() for cell in run.get("coverage_table") or []],
		days,
	)
	_set_row(
		scan,
		row,
		{
			"status": STATUS_SOLVED,
			"optimizer_run": run.name,
			"input_hash": input_hash,
			"reused": 1 if reused is not None else 0,
			"severity": horizon.window_severity(findings, STATUS_SOLVED, scaled=False),
			"message": None,
			"findings": frappe.as_json(horizon.dumps(findings), indent=None),
		},
	)
	return STATUS_SOLVED


def _record_failure(
	scan, row, run, message: str, status: str = STATUS_FAILED, input_hash: str | None = None
) -> str:
	"""Record a window that produced no schedule, keeping its run for the diagnosis.

	No findings are stored: there is no coverage to report, and :func:`horizon.window_days`
	paints the whole window from the status alone.
	"""
	_set_row(
		scan,
		row,
		{
			"status": status,
			"optimizer_run": run.name,
			"input_hash": input_hash,
			"reused": 0,
			"severity": horizon.SEV_FAILED,
			"message": message,
			"findings": None,
		},
	)
	return status


def _set_row(scan, row, values: dict) -> None:
	"""Write a window row straight to the database, bypassing the parent's save.

	A sweep is a long job and the scan it is updating is a document somebody is looking
	at; saving the parent on every window would both fight their timestamp and rewrite
	every sibling row. The parent's own counters go the same way.
	"""
	row.update(values)
	frappe.db.set_value("Horizon Scan Window", row.name, values, update_modified=False)
	done = sum(1 for sibling in (scan.get("windows") or []) if sibling.status not in horizon.UNSCANNED)
	scan.db_set({"weeks_done": done, "last_scanned_on": frappe.utils.now_datetime()}, update_modified=False)


def _draft_run(scan, row) -> "frappe.model.document.Document":
	"""A Draft `Optimizer Run` for this window, or the Draft one it already has.

	Reuses the row's own run where it is still a Draft, which is how a `Timed Out` window
	retries without leaving its first attempt behind.
	"""
	if row.optimizer_run:
		existing = frappe.db.get_value(
			"Optimizer Run", row.optimizer_run, ["status", "mode", "date", "ruleset"], as_dict=True
		)
		if (
			existing
			and existing.status == "Draft"
			and existing.mode == scan.mode
			and frappe.utils.getdate(existing.date) == frappe.utils.getdate(row.window_start)
			and existing.ruleset == scan.ruleset
		):
			return frappe.get_doc("Optimizer Run", row.optimizer_run)

	run = frappe.new_doc("Optimizer Run")
	run.set("mode", scan.mode)
	run.set("date", frappe.utils.getdate(row.window_start))
	run.set("ruleset", scan.ruleset)
	run.set("type", "Scan")
	run.insert(ignore_permissions=True)
	return run


# ── reading a scan back ────────────────────────────────────────────────────────


def _days_of(scan, row) -> tuple[horizon.Day, ...]:
	"""A window row's stored findings, or bare days where it has none to store.

	A failed window persisted no coverage — there was none — but its days are still
	squares on the calendar and still have to be paintable from the status alone.
	"""
	days = horizon.loads(frappe.parse_json(row.findings) if row.findings else None)
	if days:
		return days
	if row.status in horizon.FAILED_STATUSES:
		return tuple(
			horizon.Day(date=day, worked=True)
			for day in planning_days(frappe.utils.getdate(row.window_start), scan.mode)
		)
	return ()


def verdicts(
	scan, scaled: bool, disciplines: Sequence[str] = (), branches: Sequence[str] = ()
) -> dict[str, dict]:
	"""Every scanned day of the scan as a calendar square, keyed by ISO date.

	The stored findings are raw numbers, so both of the reader's questions — which
	capacity a shortfall is measured against (`scaled`), and which bands count
	(`disciplines` / `branches`) — are answered on read. Neither re-solves anything, and
	a window still `Pending` contributes no square at all: an unscanned day is not a
	clean day.
	"""
	out: dict[str, dict] = {}
	for row in scan.get("windows") or []:
		if row.status in horizon.UNSCANNED:
			continue
		days = [horizon.narrow(day, disciplines, branches) for day in _days_of(scan, row)]
		for square in horizon.window_days(days, row.status, scaled):
			square["window"] = frappe.utils.getdate(row.window_start).isoformat()
			square["run"] = row.optimizer_run
			square["message"] = row.message
			out[square["date"]] = square
	return out


def window_rows(
	scan, scaled: bool, disciplines: Sequence[str] = (), branches: Sequence[str] = ()
) -> list[dict]:
	"""The scan's windows as the progress list the page draws beside the calendars."""
	rows = []
	for row in scan.get("windows") or []:
		days = [horizon.narrow(day, disciplines, branches) for day in _days_of(scan, row)]
		rows.append(
			{
				"name": row.name,
				"window_start": frappe.utils.getdate(row.window_start).isoformat(),
				"window_end": frappe.utils.getdate(row.window_end).isoformat(),
				"status": row.status,
				"severity": horizon.window_severity(days, row.status, scaled)
				if row.status not in horizon.UNSCANNED
				else None,
				"run": row.optimizer_run,
				"reused": bool(row.reused),
				"message": row.message,
			}
		)
	return rows


def day_detail(
	scan, date: str, scaled: bool, disciplines: Sequence[str] = (), branches: Sequence[str] = ()
) -> dict:
	"""One day's itemised findings, plus the window row that produced them."""
	target = frappe.utils.getdate(date)
	for row in scan.get("windows") or []:
		first = frappe.utils.getdate(row.window_start)
		last = frappe.utils.getdate(row.window_end)
		if not (first <= target <= last):
			continue
		day = next((d for d in _days_of(scan, row) if d.date == target), None)
		detail = (
			horizon.day_detail(horizon.narrow(day, disciplines, branches), scaled)
			if day
			else {"date": date, "severity": horizon.SEV_OK}
		)
		if row.status in horizon.FAILED_STATUSES:
			detail["severity"] = horizon.SEV_FAILED
		return {
			"day": detail,
			"window": {
				"name": row.name,
				"window_start": first.isoformat(),
				"window_end": last.isoformat(),
				"status": row.status,
				"message": row.message,
				"run": row.optimizer_run,
				"reused": bool(row.reused),
			},
		}
	return {}


def horizon_bounds(scan) -> tuple[datetime.date, datetime.date]:
	"""The first and last day any of the scan's windows covers."""
	rows = scan.get("windows") or []
	if not rows:
		start = frappe.utils.getdate(scan.start_date)
		return start, frappe.utils.getdate(scan.end_date)
	return (
		min(frappe.utils.getdate(row.window_start) for row in rows),
		max(frappe.utils.getdate(row.window_end) for row in rows),
	)
