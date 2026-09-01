import colorsys
import heapq
import itertools
import json

import frappe.utils.caching
from frappe.model.document import Document

from autoshift.optimizer import data_loader, types
from autoshift.utils import background_workers_alive as _background_workers_alive

# Time given to the synchronous attempt before falling back to a background job.
SYNC_TIME_LIMIT = 5


def datapackage_cache_key(run_name) -> str:
	# v3: packages cached before role binding existed carry no binding_pairs /
	# binding_conflicts, so a stale entry would silently unfreeze a settled schedule
	# v4: adds unresolved_assignments, and the Shift Assignments a run sees can now change
	# under it (autoshift.rota materialises settled schedules just before a solve)
	return f"DataPackage:v4:{run_name}"


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

	@frappe.utils.caching.redis_cache()
	def cache_datapackage(self):
		cache = frappe.cache
		if cache is None:
			return data_loader.load(self)

		dataS = cache.get_value(datapackage_cache_key(self.name))
		if dataS is not None:
			return types.DataPackage.loads(dataS)
		data = data_loader.load(self)
		cache.set_value(datapackage_cache_key(self.name), data.dumps())

		return data

	@frappe.whitelist()
	def check_duplicates(self):
		"""Checks whether another run already solved this exact input.

		Returns the underlying duplicates
		"""
		from autoshift.optimizer.solver import find_cached_runs

		data = self.cache_datapackage()

		cached_names = find_cached_runs(data.input_hash(), exclude_name=self.name)

		return {
			"n": len(cached_names),
			"cached_runs_list_link": frappe.utils.get_filtered_list_link("Optimizer Run", cached_names),
		}

	@frappe.whitelist()
	def check_binding_rule_gap(self):
		"""Does this run's ruleset omit the binding rule while the site marks roles binding?

		Cheap config query — no DataPackage, so it is safe to call before solving.
		"""
		return data_loader.ruleset_binding_rule_gap(self.ruleset)  # ty:ignore[unresolved-attribute]

	def planning_window(self):
		"""(first day, last day) of this run's horizon.

		Truncated at 100 days exactly as ``data_loader.load`` truncates it, so an
		``Unbounded`` run reports the span it will actually solve.
		"""
		days = list(itertools.islice(types.planning_days(frappe.utils.getdate(self.date), self.mode), 100))
		return days[0], days[-1]

	@frappe.whitelist()
	def check_pending_bound_shifts(self):
		"""Settled schedules this horizon needs that no Shift Assignment records yet.

		HRMS cannot generate them for a rota longer than a week (see ``autoshift.rota``),
		so the run has to, or ``bind_role_assignments`` would freeze those people to an
		empty week. Cheap config query — safe to call before solving.
		"""
		from autoshift.rota import materialize as rota

		first, last = self.planning_window()
		found = rota.pending(first, last)
		return {key: value for key, value in found.items() if key != "rows"}

	@frappe.whitelist()
	def materialize_bound_shifts(self):
		"""Create the Shift Assignments :meth:`check_pending_bound_shifts` reports missing."""
		from autoshift.rota import materialize as rota

		first, last = self.planning_window()
		result = rota.materialize(first, last)
		# The package is built from those very records, so a cached one is now stale.
		if result["created"] and frappe.cache:
			frappe.cache.delete_value(datapackage_cache_key(self.name))
		return result

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

		data = self.cache_datapackage()
		self.set("status", "Solving")
		timed_out = run_solve(str(self.name), data, time_limit=SYNC_TIME_LIMIT)
		if timed_out:
			if not _background_workers_alive():
				# Without this guard the run would sit in "Solving" forever with the job
				# rotting in an unserved queue — a dev bench running only `bench serve`
				# has no workers, and nothing else surfaces that.
				frappe.throw(
					frappe._(
						"This problem needs more than {0}s and would continue as a background "
						"job, but no background worker is running to pick it up. Start one "
						"(e.g. <code>bench worker</code>, or run the bench via "
						"<code>bench start</code>) and solve again."
					).format(SYNC_TIME_LIMIT)
				)
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
		new_run.set("ruleset", self.ruleset)  # ty:ignore[unresolved-attribute]
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
		      "employees": [{"name", "employee_name", "roles", "image"}, ...],
		      "events": {employee: {"YYYY-MM-DD": [{"kind", ...}, ...]}},
		    }
		"""
		import datetime

		getdate = data_loader.getdate

		data = self.cache_datapackage()
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
					"scheduling_role": s.scheduling_role,
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
				fields=["name", "employee_name", "image"],
				order_by="employee_name asc",
			)
			if active
			else []
		)
		# Roles, not designation: designation is payroll data the optimizer no longer reads,
		# and the roles are what explain why somebody appears in a given discipline at all.
		for emp in employees:
			emp["roles"] = list(data.employee_roles.get(emp["name"], ()))

		return {"days": days, "employees": employees, "events": events}

	@frappe.whitelist()
	def get_run_statistics(self):
		"""Aggregate statistics of a solved run: how full the schedule actually is, and why.

		Returns None unless the run carries a solution. Shape:

		    {
		      "totals": {room_slots_staffed, room_slots_capacity, assignments,
		                 assignments_forced, assignments_bound, target_shifts,
		                 employees_considered, employees_scheduled, objective_value},
		      "coverage": [{discipline, staffed, capacity, supply_bound, limiting_role,
		                    branches: [{branch, staffed, capacity}]}],
		      "matrix": [{discipline, branch, date, shift_type, staffed, capacity}],
		      "employees": [{employee, employee_name, target, assigned}],  # by deficit
		      "binding_conflicts": [{employee, scheduling_role, shift_type, date, branch}],
		      "unresolved_assignments": [{employee, date, reason}],
		      "warnings": [{severity, message}],
		      "objective_breakdown": {rule_name: value} | None,
		    }

		Coverage comes from the persisted ``coverage_table`` (the solver's actual
		``active_rooms`` values); runs solved before that table existed fall back to
		deriving the same numbers from the assignment slots. ``supply_bound`` is the
		scarcest Scheduling Role's total room-slot supply over the horizon (holders'
		FTE ceilings x their max-rooms figures) — the reason a discipline cannot fill
		its configured capacity is almost always that this number is the smaller one.
		"""
		if self.status not in ("Solved", "Approved", "Committed"):
			return None

		data = self.cache_datapackage()
		slots = self.get("solution_table") or []

		coverage_rows = self.get("coverage_table") or []
		if coverage_rows:
			matrix = [
				{
					"discipline": row.discipline,
					"branch": row.branch,
					"date": str(row.date),
					"shift_type": row.shift_type,
					"staffed": int(row.staffed_rooms or 0),
					"capacity": int(row.capacity or 0),
				}
				for row in coverage_rows
			]
		else:
			matrix = _derive_coverage_matrix(data, slots)

		# ── per-discipline coverage summary ─────────────────────────────────────
		by_disc: dict[str, dict] = {}
		for cell in matrix:
			disc = by_disc.setdefault(
				cell["discipline"],
				{"discipline": cell["discipline"], "staffed": 0, "capacity": 0, "branches": {}},
			)
			disc["staffed"] += cell["staffed"]
			disc["capacity"] += cell["capacity"]
			branch = disc["branches"].setdefault(
				cell["branch"], {"branch": cell["branch"], "staffed": 0, "capacity": 0}
			)
			branch["staffed"] += cell["staffed"]
			branch["capacity"] += cell["capacity"]

		supply_bounds = _role_supply_bounds(data)
		coverage = []
		for disc in sorted(by_disc.values(), key=lambda d: d["discipline"]):
			bound = supply_bounds.get(disc["discipline"])
			coverage.append(
				{
					**disc,
					"branches": sorted(disc["branches"].values(), key=lambda b: b["branch"]),
					"supply_bound": bound["supply"] if bound else None,
					"limiting_role": bound["role"] if bound else None,
				}
			)

		# ── employees: assigned vs. FTE target ──────────────────────────────────
		assigned_per_emp: dict[str, int] = {}
		for s in slots:
			assigned_per_emp[s.employee] = assigned_per_emp.get(s.employee, 0) + 1
		employee_names = (
			{
				row.name: row.employee_name
				for row in frappe.get_all(
					"Employee",
					filters={"name": ["in", list(data.employees)]},
					fields=["name", "employee_name"],
				)
			}
			if data.employees
			else {}
		)
		employee_rows = sorted(
			(
				{
					"employee": e,
					"employee_name": employee_names.get(e, e),
					"target": data.target_shifts.get(e, 0),
					"assigned": assigned_per_emp.get(e, 0),
				}
				for e in data.employees
			),
			key=lambda r: r["assigned"] - r["target"],
		)

		# ── warnings ────────────────────────────────────────────────────────────
		warnings = []
		with_settings = (
			set(
				frappe.get_all(
					"Employee Settings", filters={"employee": ["in", list(data.employees)]}, pluck="employee"
				)
			)
			if data.employees
			else set()
		)
		missing_settings = len(data.employees) - len(with_settings)
		if missing_settings:
			warnings.append(
				{
					"severity": "warning",
					"message": frappe._(
						"{0} of {1} scheduled employees have no Employee Settings — their shift and "
						"branch preferences fall back to uniform."
					).format(missing_settings, len(data.employees)),
				}
			)
		for disc in coverage:
			if disc["supply_bound"] is not None and disc["supply_bound"] < disc["capacity"]:
				warnings.append(
					{
						"severity": "warning",
						"message": frappe._(
							"{0}: configured capacity is {1} room-slots over this horizon, but role "
							"{2} can supply at most {3} — coverage cannot exceed that, whatever the "
							"ruleset."
						).format(
							disc["discipline"], disc["capacity"], disc["limiting_role"], disc["supply_bound"]
						),
					}
				)
		if data.binding_conflicts:
			sample = ", ".join(
				f"{employee} ({date})"
				for employee, _role, _shift, date, _branch in data.binding_conflicts[:3]
			)
			warnings.append(
				{
					"severity": "warning",
					"message": frappe._(
						"{0} existing Shift Assignment(s) fall on a day the employee is on leave and "
						"were dropped — leave wins over a settled schedule. Fix the underlying "
						"records if that is not what should happen: {1}{2}"
					).format(
						len(data.binding_conflicts),
						sample,
						"…" if len(data.binding_conflicts) > 3 else "",
					),
				}
			)
		if data.unresolved_assignments:
			sample = ", ".join(
				f"{employee} ({date})" for employee, date, _reason in data.unresolved_assignments[:3]
			)
			warnings.append(
				{
					"severity": "warning",
					"message": frappe._(
						"{0} existing Shift Assignment(s) could not be placed and were ignored — "
						"their Shift Location names no branch or discipline, or the employee holds "
						"no Scheduling Role there: {1}{2}"
					).format(
						len(data.unresolved_assignments),
						sample,
						"…" if len(data.unresolved_assignments) > 3 else "",
					),
				}
			)
		under_target = sum(1 for r in employee_rows if r["assigned"] < r["target"])
		if under_target:
			warnings.append(
				{
					"severity": "info",
					"message": frappe._(
						"{0} employee(s) end below their FTE target — once the scarcest role in a "
						"discipline is exhausted, additional assignments open no rooms and are not made."
					).format(under_target),
				}
			)

		breakdown = None
		if self.get("objective_breakdown"):
			try:
				breakdown = json.loads(self.get("objective_breakdown"))
			except ValueError:
				breakdown = None

		return {
			"totals": {
				"room_slots_staffed": sum(c["staffed"] for c in matrix),
				"room_slots_capacity": sum(c["capacity"] for c in matrix),
				"assignments": len(slots),
				"assignments_forced": sum(1 for s in slots if s.forced),
				"assignments_bound": sum(
					1 for s in slots if (s.employee, s.scheduling_role) in data.binding_pairs
				),
				"target_shifts": sum(data.target_shifts.get(e, 0) for e in data.employees),
				"employees_considered": len(data.employees),
				"employees_scheduled": len(assigned_per_emp),
				"objective_value": self.objective_value,  # ty:ignore[unresolved-attribute]
			},
			"coverage": coverage,
			"matrix": matrix,
			"employees": employee_rows,
			"binding_conflicts": [
				{
					"employee": employee,
					"scheduling_role": role,
					"shift_type": shift_type,
					"date": str(date),
					"branch": branch,
				}
				for employee, role, shift_type, date, branch in data.binding_conflicts
			],
			"unresolved_assignments": [
				{"employee": employee, "date": str(date), "reason": reason}
				for employee, date, reason in data.unresolved_assignments
			],
			"warnings": warnings,
			"objective_breakdown": breakdown,
		}

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


def _derive_coverage_matrix(data, slots) -> list[dict]:
	"""Reconstruct the room-coverage matrix from assignment slots.

	Fallback for runs solved before the solver persisted ``coverage_table``: rebuilds
	what ``active_rooms`` must have been under the ``room_coverage`` rule — per slot,
	the minimum over the discipline's roles of the room-slots their assignees
	contribute, capped at the branch's configured capacity. Exact for solutions where
	that rule was selected with the room-utilization objective (the solver never
	leaves a coverable room unclaimed); an upper bound otherwise.
	"""
	roles_per_disc: dict[str, set[str]] = {}
	for role, disc in data.role_discipline.items():
		roles_per_disc.setdefault(disc, set()).add(role)

	staffed_by_role: dict[tuple, int] = {}
	for s in slots:
		disc = data.role_discipline.get(s.scheduling_role)
		if not disc:
			continue
		key = (disc, s.scheduling_role, str(s.date), s.shift_type, s.branch)
		staffed_by_role[key] = staffed_by_role.get(key, 0) + data.max_rpe.get(
			(s.employee, s.scheduling_role), 1
		)

	matrix = []
	for disc in data.disciplines:
		roles = roles_per_disc.get(disc, set())
		for branch in data.branches:
			capacity = data.rooms.get((disc, branch), 0)
			if not capacity:
				continue
			for day in data.working_days:
				for shift_type in data.shift_types:
					staffed = min(
						(staffed_by_role.get((disc, r, str(day), shift_type, branch), 0) for r in roles),
						default=0,
					)
					matrix.append(
						{
							"discipline": disc,
							"branch": branch,
							"date": str(day),
							"shift_type": shift_type,
							"staffed": min(staffed, capacity),
							"capacity": capacity,
						}
					)
	return matrix


def _role_supply_bounds(data) -> dict[str, dict]:
	"""Per discipline: the scarcest role's total room-slot supply over the horizon.

	A discipline's coverage is the minimum over its roles, and each holder can work at
	most their FTE ceiling (each shift contributing their max-rooms figure), so the
	scarcest role's supply bounds what any ruleset can staff. Optimistic where an
	employee holds several roles (counted fully in each) — presented as "at most",
	never as a promise.
	"""
	tol = 0.05  # keep in sync with rules.fte_ceiling
	bounds: dict[str, dict] = {}
	for disc in data.disciplines:
		role_supplies: dict[str, int] = {}
		for role, role_disc in data.role_discipline.items():
			if role_disc != disc:
				continue
			supply = 0
			for e in data.employees:
				if role in data.employee_roles.get(e, ()):
					ceiling = int((1 + tol) * data.target_shifts.get(e, 0))
					supply += data.max_rpe.get((e, role), 1) * ceiling
			role_supplies[role] = supply
		if role_supplies:
			limiting = min(role_supplies, key=lambda r: role_supplies[r])
			bounds[disc] = {"role": limiting, "supply": role_supplies[limiting], "roles": role_supplies}
	return bounds
