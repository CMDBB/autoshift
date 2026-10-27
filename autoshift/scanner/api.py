# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The whitelisted surface of the horizon scanner: one payload the browser can draw.

Everything needing a judgement — which capacity a shortfall is measured against, which
bands count, what colour a day is — is settled here and in `scanner/horizon.py`, so
`horizon_calendar.js` is a renderer and nothing more. The shape:

	{
	  "scan":    {name, ruleset, start_date, end_date, mode, status, weeks_total,
				  weeks_done, last_scanned_on, reuse_cached, window_time_limit} | null,
	  "scans":   [{name, status, ruleset, start_date, end_date, weeks_done, weeks_total}, …],
	  "months":  [{year, month, label, weeks: [[ISO date|null, …7], …]}, …],
	  "days":    {ISO date: {severity, worked, staffed, capacity, shortfall, …}},
	  "windows": [{name, window_start, window_end, status, severity, run, reused, message}, …],
	  "totals":  {ok, info, warning, critical, failed, unscanned},
	  "filters": {disciplines: {options, selected}, branches: {options, selected},
				  scaled: bool},
	}

`days` holds a square only for a day some window has already solved: a day with no
verdict is **not** a clean day, and the calendar has to be able to say so. The same read
serves the page and the Horizon Scan form.
"""

from __future__ import annotations

import datetime

import frappe

from autoshift.scanner import horizon
from autoshift.scanner import scan as scan_engine
from autoshift.wallchart import layout as layout_mod


def _selection(raw) -> list[str]:
	"""A filter argument as a list. An empty selection means *everything*, never nothing."""
	if not raw:
		return []
	if isinstance(raw, str):
		raw = frappe.parse_json(raw)
	return [str(value) for value in (raw or [])]


def _scan_doc(name: str):
	doc = frappe.get_doc("Horizon Scan", name)
	doc.check_permission("read")
	return doc


def _summary(scan) -> dict:
	return {
		"name": scan.name,
		"ruleset": scan.ruleset,
		"start_date": frappe.utils.getdate(scan.start_date).isoformat(),
		"end_date": frappe.utils.getdate(scan.end_date).isoformat(),
		"mode": scan.mode,
		"status": scan.status,
		"weeks_total": int(scan.weeks_total or 0),
		"weeks_done": int(scan.weeks_done or 0),
		"last_scanned_on": str(scan.last_scanned_on or ""),
		"reuse_cached": bool(scan.reuse_cached),
		"window_time_limit": int(scan.window_time_limit or scan_engine.DEFAULT_WINDOW_TIME_LIMIT),
	}


@frappe.whitelist()
def list_scans(limit: int = 20) -> list[dict]:
	"""The most recent scans, for the page's picker."""
	frappe.has_permission("Horizon Scan", throw=True)
	return frappe.get_all(
		"Horizon Scan",
		fields=[
			"name",
			"status",
			"ruleset",
			"start_date",
			"end_date",
			"mode",
			"weeks_done",
			"weeks_total",
		],
		order_by="creation desc",
		limit=int(limit),
	)


@frappe.whitelist()
def get_scan(
	scan: str | None = None,
	scaled: int | str | bool = 0,
	disciplines: str | None = None,
	branches: str | None = None,
) -> dict:
	"""The calendars, the progress list and the pickers for one scan.

	`scan` omitted means the most recent one, which is what the page opens on. With no
	scan at all the payload still carries `scans` and `filters`, so the page can offer to
	start one.

	`scaled` picks the capacity a shortfall is measured against: off, the configuration's
	own `rooms_num`; on, what the **exclusive** gating roles scheduled in a slot could
	ever open, so only a shortage of flexible staff around them is flagged. Stored
	findings carry both figures, so this costs a read and never a solve.
	"""
	frappe.has_permission("Horizon Scan", throw=True)
	scaled = bool(frappe.utils.cint(scaled))
	disciplines, branches = _selection(disciplines), _selection(branches)

	scans = list_scans()
	if not scan and scans:
		scan = scans[0]["name"]
	options = layout_mod.filter_options()
	payload = {
		"scans": scans,
		"scan": None,
		"months": [],
		"days": {},
		"windows": [],
		"totals": {},
		"filters": {
			"disciplines": {"options": options["disciplines"], "selected": disciplines},
			"branches": {"options": options["branches"], "selected": branches},
			"scaled": scaled,
		},
	}
	if not scan:
		return payload

	doc = _scan_doc(scan)
	first, last = scan_engine.horizon_bounds(doc)
	days = scan_engine.verdicts(doc, scaled, disciplines, branches)
	payload.update(
		{
			"scan": _summary(doc),
			"months": horizon.month_grids(first, last),
			"days": days,
			"windows": scan_engine.window_rows(doc, scaled, disciplines, branches),
			"totals": _totals(days, first, last),
		}
	)
	return payload


def _totals(days: dict[str, dict], first, last) -> dict:
	"""How the horizon's days came out, by severity, plus the ones nothing has judged.

	Counted off the squares actually drawn rather than off the window rows, for the wall
	chart's reason: a headline counting days the reader is not being shown would be worse
	than no headline. Days nobody works are left out of every bucket — an empty calendar
	square the practice is closed on is not a verdict.
	"""
	totals = {severity: 0 for severity in horizon.SEVERITY_RANK}
	totals["unscanned"] = 0
	totals["closed"] = 0
	day = first
	while day <= last:
		square = days.get(day.isoformat())
		if square is None:
			totals["unscanned"] += 1
		elif not square["worked"]:
			totals["closed"] += 1
		else:
			totals[square["severity"]] = totals.get(square["severity"], 0) + 1
		day += datetime.timedelta(days=1)
	return totals


@frappe.whitelist()
def get_day(
	scan: str,
	date: str,
	scaled: int | str | bool = 0,
	disciplines: str | None = None,
	branches: str | None = None,
) -> dict:
	"""One day's itemised findings — the panel behind a click on a calendar square."""
	frappe.has_permission("Horizon Scan", throw=True)
	return scan_engine.day_detail(
		_scan_doc(scan),
		date,
		bool(frappe.utils.cint(scaled)),
		_selection(disciplines),
		_selection(branches),
	)


@frappe.whitelist()
def create_scan(
	ruleset: str,
	start_date: str,
	end_date: str,
	mode: str = "1-week",
	reuse_cached: int | str | bool = 1,
	window_time_limit: int | str | None = None,
) -> str:
	"""Create a scan and plan its windows, without solving anything yet."""
	scan = frappe.new_doc("Horizon Scan")
	scan.set("ruleset", ruleset)
	scan.set("start_date", start_date)
	scan.set("end_date", end_date)
	scan.set("mode", mode)
	scan.set("reuse_cached", frappe.utils.cint(reuse_cached))
	scan.set(
		"window_time_limit",
		frappe.utils.cint(window_time_limit) or scan_engine.DEFAULT_WINDOW_TIME_LIMIT,
	)
	scan.insert()
	return scan.name


@frappe.whitelist()
def start_scan(scan: str, resume: int | str | bool = 1) -> str:
	"""Start or resume the sweep. `resume=0` re-runs every window from scratch."""
	return scan_engine.start(scan, resume=bool(frappe.utils.cint(resume)))


@frappe.whitelist()
def stop_scan(scan: str) -> str:
	"""Ask the sweep to stop after the window it is on."""
	frappe.get_doc("Horizon Scan", scan).check_permission("write")
	return scan_engine.request_stop(scan)


@frappe.whitelist()
def rerun_windows(scan: str, windows: str | None = None) -> str:
	"""Put named windows (or all of them) back to `Pending` and sweep again.

	Re-running a window the input of which has not changed re-links the very run it had,
	unless the scan turns `reuse_cached` off: an identical input solves to an identical
	schedule, and a scan is the one surface where that saves an hour rather than a second.
	"""
	doc = frappe.get_doc("Horizon Scan", scan)
	doc.check_permission("write")
	scan_engine.reset(doc, _selection(windows) or None)
	return scan_engine.start(scan, resume=True)
