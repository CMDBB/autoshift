import colorsys
import heapq

import frappe.utils
from frappe.model.document import Document

from autoshift.optimizer import data_loader, types

# Time given to the synchronous attempt before falling back to a background job.
SYNC_TIME_LIMIT = 5

# Approximate hue (HSV degrees) of each named "Roster Color" on Shift Type, so a
# Shift Type that already has one anchors the auto-generated palette around it.
_NAMED_COLOR_HUE = {
	"red": 0,
	"orange": 25,
	"yellow": 48,
	"lime": 85,
	"green": 142,
	"cyan": 190,
	"blue": 217,
	"violet": 258,
	"fuchsia": 292,
	"pink": 330,
}


def _fmt_time(value) -> str:
	"""Format a Shift Type time (``datetime.timedelta``/string) as ``HH:MM``."""
	if not value:
		return ""
	parts = str(value).split(":")
	if len(parts) < 2:
		return str(value)
	return f"{int(parts[0]):02d}:{parts[1]}"


def _hsv_hex(hue: float, sat: float, val: float) -> str:
	r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360.0, sat, val)
	return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _hue_to_pair(hue: float) -> tuple[str, str]:
	"""(background, border) hex for a hue"""
	return _hsv_hex(hue, 0.7, 0.7), _hsv_hex(hue, 0.9, 0.9)


def _assign_shift_colors(shift_type_names, shift_meta) -> dict[str, tuple[str, str]]:
	"""Map each Shift Type to a (background, border) hex pair."""
	fixed: dict[str, float] = {}
	for st in sorted(shift_type_names):
		meta = shift_meta.get(st)
		named = (meta.color or "").strip().lower() if meta else ""
		if named in _NAMED_COLOR_HUE:
			fixed[st] = _NAMED_COLOR_HUE[named]
		else:
			raise ValueError(f"No color found for shift {meta}")

	return {st: _hue_to_pair(hue) for st, hue in fixed.items()}


class OptimizerRun(Document):
	def before_save(self):
		if not self.status:
			self.status = "Draft"
		if not self.type:
			self.type = "Manual"

	def memoize_datapackage(self):
		cache = frappe.cache
		if cache is None:
			return data_loader.load(self)

		dataS = cache.get_value(f"DataPackage:{self.name}")
		if dataS is not None:
			return types.DataPackage.loads(dataS)
		data = data_loader.load(self)
		cache.set_value(f"DataPackage:{self.name}", data.dumps())

		return data

	@frappe.whitelist()
	def check_duplicates(self):
		"""Checks whether another run already solved this exact input.

		Returns the underlying duplicates
		"""
		from autoshift.optimizer.solver import find_cached_runs

		data = self.memoize_datapackage()

		cached_names = find_cached_runs(data.input_hash(), exclude_name=self.name)

		return {
			"n": len(cached_names),
			"cached_runs_list_link": frappe.utils.get_filtered_list_link("Optimizer Run", cached_names),
		}

	@frappe.whitelist()
	def solve(self):
		"""Solve the run.

		Attempts to solve synchronously within SYNC_TIME_LIMIT seconds.
		If CBC doesn't conclude within that window, the same data is re-queued as a
		background job with the full timeout.
		"""
		if self.status != "Draft":
			frappe.throw(frappe._("Only Draft runs can be solved."))
		from autoshift.optimizer.solver import run_solve

		data = self.memoize_datapackage()
		self.set("status", "Solving")
		timed_out = run_solve(str(self.name), self.memoize_datapackage(), time_limit=SYNC_TIME_LIMIT)
		if timed_out:
			frappe.enqueue(
				"autoshift.optimizer.solver.run_solve",
				run_name=self.name,
				data=data,
				time_limit=3600,
				queue="long",
				timeout=3600,
			)
			self.save()
			return "Solving"

		self.reload()
		return self.status

	@frappe.whitelist()
	def duplicate(self):
		"""Create a new Draft run with the same configuration as this one.

		Runs are immutable once solving starts. To re-try a Failed run,
		create a duplicate, the original stays untouched as a record.
		"""
		new_run = frappe.new_doc("Optimizer Run")
		new_run.set("mode", self.mode)  # ty:ignore[unresolved-attribute]
		new_run.set("date", self.date)  # ty:ignore[unresolved-attribute]
		new_run.set("disregard_assignments", self.disregard_assignments)  # ty:ignore[unresolved-attribute]
		new_run.set("type", "Copy")
		for row in self.get("leaves_speculations") or []:
			new_run.append("leaves_speculations", {"leave_application": row.leave_application})
		new_run.insert()
		return new_run.name

	@frappe.whitelist()
	def get_schedule_events(self):
		"""Return the solved schedule plus context as a roster-style grid payload (read-only).

		Three event *kinds*, each rendered in a distinct colour by the form, so the
		proposed schedule can be eyeballed against what already exists:

		  - ``"assigned"``: a slot from THIS run's solution (coloured by Shift Type)
		  - ``"existing"``: a committed ``Shift Assignment`` already on the books
		  - ``"leave"``: an approved (or this run's speculated) ``Leave Application``

		Scope (employees + window) comes from the run's ``DataPackage`` so the
		overlay matches exactly what the optimizer considered. Nothing here touches
		hrms or creates any ``Shift Assignment`` records.

		    {
		      "days": ["YYYY-MM-DD", ...],
		      "employees": [{"name", "employee_name", "designation", "image"}, ...],
		      "events": {employee: {"YYYY-MM-DD": [{"kind", ...}, ...]}},
		    }
		"""
		import datetime

		getdate = data_loader.getdate

		data = self.memoize_datapackage()
		in_scope = list(data.employees)

		days = [d.isoformat() for d in types.planning_days(self.date, self.mode)]  # ty:ignore[unresolved-attribute]
		day_set = set(days)
		window_start, window_end = days[0], days[-1]

		events: dict[str, dict[str, list[dict]]] = {}

		def _add(employee, day, payload):
			events.setdefault(employee, {}).setdefault(day, []).append(payload)

		# ── Proposed assignments (this run's solution) ──────────────────────────
		slots = self.get("solution_table") or []
		shift_type_names = {s.shift_type for s in slots}

		# ── Existing committed Shift Assignments overlapping the window ──────────
		# overlap = start_date <= window_end AND (end_date >= window_start OR open-ended)
		existing = (
			frappe.get_all(
				"Shift Assignment",
				filters={"employee": ["in", in_scope], "docstatus": 1, "start_date": ["<=", window_end]},
				or_filters=[["end_date", ">=", window_start], ["end_date", "is", "not set"]],
				fields=["employee", "shift_type", "shift_location", "start_date", "end_date"],
			)
			if in_scope
			else []
		)
		shift_type_names |= {sa.shift_type for sa in existing}

		# Shift Type colour/time metadata, shared by proposed and existing chips
		shift_meta = {
			st.name: st
			for st in (
				frappe.get_all(
					"Shift Type",
					filters={"name": ["in", list(shift_type_names)]},
					fields=["name", "color", "start_time", "end_time"],
				)
				if shift_type_names
				else []
			)
		}

		# Distinct colour per Shift Type for the proposed chips.
		shift_colors = _assign_shift_colors(shift_type_names, shift_meta)

		# Branch source of truth: Shift Assignment -> Shift Location.custom_branch
		loc_names = list({sa.shift_location for sa in existing if sa.shift_location})
		loc_branch = {
			row.name: row.custom_branch
			for row in (
				frappe.get_all(
					"Shift Location",
					filters={"name": ["in", loc_names]},
					fields=["name", "custom_branch"],
				)
				if loc_names
				else []
			)
		}

		for s in slots:
			meta = shift_meta.get(s.shift_type)
			bg, border = shift_colors.get(s.shift_type, ("#eff6ff", "#93c5fd"))
			day = s.date.isoformat() if hasattr(s.date, "isoformat") else str(s.date)
			_add(
				s.employee,
				day,
				{
					"kind": "assigned",
					"shift_type": s.shift_type,
					"bg": bg,
					"border": border,
					"start_time": _fmt_time(meta.start_time) if meta else "",
					"end_time": _fmt_time(meta.end_time) if meta else "",
					"branch": s.branch,
					"shift_location": s.shift_location,
					"forced": bool(s.forced),
				},
			)

		for sa in existing:
			meta = shift_meta.get(sa.shift_type)
			branch = loc_branch.get(sa.shift_location)
			day = getdate(sa.start_date)
			end = getdate(sa.end_date) if sa.end_date else getdate(window_end)
			while day <= end:
				iso = day.isoformat()
				if iso in day_set:
					_add(
						sa.employee,
						iso,
						{
							"kind": "existing",
							"shift_type": sa.shift_type,
							"start_time": _fmt_time(meta.start_time) if meta else "",
							"end_time": _fmt_time(meta.end_time) if meta else "",
							"branch": branch,
							"shift_location": sa.shift_location,
						},
					)
				day += datetime.timedelta(days=1)

		# ── Leaves: approved in-window + this run's speculated pending leaves ────
		speculated = {r.leave_application for r in (self.get("leaves_speculations") or [])}
		leave_rows = (
			frappe.get_all(
				"Leave Application",
				filters={
					"employee": ["in", in_scope],
					"status": "Approved",
					"from_date": ["<=", window_end],
					"to_date": [">=", window_start],
				},
				fields=["name", "employee", "leave_type", "from_date", "to_date"],
			)
			if in_scope
			else []
		)
		if speculated:
			leave_rows += frappe.get_all(
				"Leave Application",
				filters={"name": ["in", list(speculated)]},
				fields=["name", "employee", "leave_type", "from_date", "to_date"],
			)

		seen_leaves: set[str] = set()
		for lv in leave_rows:
			if lv.name in seen_leaves:
				continue
			seen_leaves.add(lv.name)
			day = getdate(lv.from_date)
			end = getdate(lv.to_date)
			while day <= end:
				iso = day.isoformat()
				if iso in day_set:
					_add(
						lv.employee,
						iso,
						{"kind": "leave", "leave_type": lv.leave_type, "speculative": lv.name in speculated},
					)
				day += datetime.timedelta(days=1)

		# Rows: in-scope employees that actually have something to show
		active = [e for e in in_scope if e in events]
		employees = (
			frappe.get_all(
				"Employee",
				filters={"name": ["in", active]},
				fields=["name", "employee_name", "designation", "image"],
				order_by="employee_name asc",
			)
			if active
			else []
		)

		return {"days": days, "employees": employees, "events": events}

	@frappe.whitelist()
	def approve(self):
		if self.status != "Solved":
			frappe.throw(frappe._("Only Solved runs can be approved."))
		self.db_set("status", "Approved")

	@frappe.whitelist()
	def commit(self):
		if self.status != "Approved":
			frappe.throw(frappe._("Only Approved runs can be committed."))
		from autoshift.optimizer.committer import commit

		commit(str(self.name))
