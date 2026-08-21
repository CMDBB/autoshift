"""Bulk creation of the two records that make an employee schedulable.

The optimizer decides scope from `Employee Scheduling Role` alone — see the comment
in `optimizer/data_loader.py` above `role_holders`: Employee.department and
.designation are HR/payroll data and "play no part". Somebody nobody has given a role
is simply never scheduled.

That makes this tool's two jobs:

  Scheduling Role     the capability. Without one an employee is invisible to the
                      optimizer, whatever their designation says.
  Employee Settings   the preferences. Optional — they only tilt the objective — but
                      tedious to create one at a time.

Both actions run over the same filtered, checked employee list, so the filters and
the datatable are shared and the two are separated only by which button is pressed.

The workers are module-level functions rather than methods. `frappe.enqueue` hands the
callable to RQ, which pickles it: a plain function pickles by reference, whereas a bound
method drags the whole Document instance through pickle with it.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_link_to_form

COVERAGE_NO_SETTINGS = "Without Employee Settings"
COVERAGE_NO_ROLE = "Without Any Scheduling Role"

#: Above this many rows the work is queued rather than run in the request.
INLINE_LIMIT = 30

EVENT_SETTINGS = "completed_bulk_employee_settings_creation"
EVENT_ROLES = "completed_bulk_scheduling_role_assignment"


class BulkEmployeeSettings(Document):
	# -- listing -----------------------------------------------------------

	@frappe.whitelist()
	def get_employees(self, advanced_filters: list) -> list:
		"""Active employees matching the filters, each carrying the state both
		actions care about: which roles they already hold, and whether they
		already have an Employee Settings record.

		Nothing is excluded on that state — it is reported in the list instead,
		because the two actions have different notions of "already done" and a
		row hidden for one would be a row missing for the other. Use `coverage`
		to narrow deliberately.
		"""
		filters = [["company", "=", self.get("company")], ["status", "=", "Active"]]
		filters += advanced_filters

		employees = frappe.get_all(
			"Employee",
			filters=filters,
			fields=["name as employee", "employee_name"],
			order_by="employee_name asc",
		)
		if not employees:
			return []

		names = [e["employee"] for e in employees]
		roles = _roles_by_employee(names)
		with_settings = set(
			frappe.get_all("Employee Settings", filters={"employee": ["in", names]}, pluck="employee")
		)
		wanted = self._role_restriction()
		coverage = self.get("coverage")

		rows = []
		for emp in employees:
			held = roles.get(emp["employee"], [])
			if wanted is not None and not (wanted & set(held)):
				continue
			if coverage == COVERAGE_NO_SETTINGS and emp["employee"] in with_settings:
				continue
			if coverage == COVERAGE_NO_ROLE and held:
				continue

			rows.append(
				{
					**emp,
					"roles": ", ".join(held),
					"has_settings": _("Yes") if emp["employee"] in with_settings else "",
				}
			)
		return rows

	def _role_restriction(self) -> set[str] | None:
		"""Role names the `discipline` / `holds_role` filters allow, or None when
		neither is set. An empty set means the filters together match nobody."""
		clauses = []
		if self.get("holds_role"):
			clauses.append({self.get("holds_role")})
		if self.get("discipline"):
			clauses.append(
				set(
					frappe.get_all(
						"Scheduling Role",
						filters={"discipline": self.get("discipline")},
						pluck="name",
					)
				)
			)
		if not clauses:
			return None
		return set.intersection(*clauses)

	# -- actions -----------------------------------------------------------

	@frappe.whitelist()
	def bulk_create_settings(
		self,
		employees: list,
		favourite_shift: str | None = None,
		shift_preferences: list | None = None,
		preferred_branch: list | None = None,
	) -> None:
		_dispatch(
			run_create_settings,
			employees,
			dict(
				employees=_employee_ids(employees),
				favourite_shift=favourite_shift,
				shift_preferences=shift_preferences or [],
				preferred_branch=preferred_branch or [],
			),
		)

	@frappe.whitelist()
	def bulk_assign_role(
		self,
		employees: list,
		scheduling_role: str | None = None,
		role_fte: float | None = None,
		max_rooms: int | None = None,
		valid_from: str | None = None,
		valid_to: str | None = None,
	) -> None:
		if not scheduling_role:
			frappe.throw(_("Please choose the Scheduling Role to assign."), title=_("No Role Selected"))

		_dispatch(
			run_assign_role,
			employees,
			dict(
				employees=_employee_ids(employees),
				scheduling_role=scheduling_role,
				role_fte=role_fte,
				max_rooms=max_rooms,
				valid_from=valid_from,
				valid_to=valid_to,
			),
		)


# -- workers ---------------------------------------------------------------


def run_create_settings(
	employees: list[str],
	favourite_shift: str | None = None,
	shift_preferences: list | None = None,
	preferred_branch: list | None = None,
) -> None:
	def create(employee_id: str) -> str | None:
		if frappe.db.exists("Employee Settings", employee_id):
			return None

		doc = frappe.new_doc("Employee Settings")
		doc.set("employee", employee_id)
		# Created live. The field defaults to 0, which would otherwise make every
		# record this tool produces read as switched-off in the list view.
		doc.set("active", 1)
		doc.set("favourite_shift", favourite_shift or None)
		for row in shift_preferences or []:
			doc.append("shift_preferences", {"shift_type": row["shift_type"], "weight": row["weight"]})
		for row in preferred_branch or []:
			doc.append("preferred_branch", {"branch": row["branch"], "weight": row["weight"]})
		doc.insert(ignore_permissions=True)
		return doc.name

	_run(
		employees,
		create,
		doctype="Employee Settings",
		event=EVENT_SETTINGS,
		title=_("Creating Employee Settings..."),
	)


def run_assign_role(
	employees: list[str],
	scheduling_role: str,
	role_fte: float | None = None,
	max_rooms: int | None = None,
	valid_from: str | None = None,
	valid_to: str | None = None,
) -> None:
	def create(employee_id: str) -> str | None:
		if frappe.db.exists(
			"Employee Scheduling Role",
			{"employee": employee_id, "scheduling_role": scheduling_role},
		):
			return None

		doc = frappe.new_doc("Employee Scheduling Role")
		doc.set("employee", employee_id)
		doc.set("scheduling_role", scheduling_role)
		doc.set("role_fte", role_fte or None)
		doc.set("max_rooms", max_rooms or None)
		doc.set("valid_from", valid_from or None)
		doc.set("valid_to", valid_to or None)
		doc.set("active", 1)
		doc.insert(ignore_permissions=True)
		return doc.name

	_run(
		employees,
		create,
		doctype="Employee Scheduling Role",
		event=EVENT_ROLES,
		title=_("Assigning Scheduling Roles..."),
	)


# -- plumbing --------------------------------------------------------------


def _employee_ids(employees: list) -> list[str]:
	"""The datatable sends [{employee: id}]; the CLI and tests send bare ids."""
	return [e["employee"] if isinstance(e, dict) else e for e in employees]


def _dispatch(worker, employees: list, kwargs: dict) -> None:
	"""Run inline for a small selection, queue for a large one."""
	if not employees:
		frappe.throw(_("Please select at least one employee."), title=_("No Employees Selected"))

	if len(employees) <= INLINE_LIMIT:
		return worker(**kwargs)

	frappe.enqueue(worker, timeout=3000, **kwargs)
	frappe.msgprint(
		_("Creation has been queued. It may take a few minutes."),
		alert=True,
		indicator="blue",
	)


def _run(employees: list[str], create, doctype: str, event: str, title: str) -> None:
	"""Create one record per employee, isolating failures to their own row.

	`create` returns the new docname, or None when the employee already had the
	record. That third outcome is reported separately: it is not a failure, but
	counting it as a success would claim something was written that was not.
	Each row gets its own savepoint so one bad record cannot roll back the work
	already done.
	"""
	success, failure, skipped = [], [], []
	savepoint = "before_bulk_autoshift_action"

	for i, employee_id in enumerate(employees):
		try:
			frappe.db.savepoint(savepoint)
			created = create(employee_id)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				f"Bulk {doctype} creation failed for {employee_id}.",
				reference_doctype=doctype,
			)
			failure.append(employee_id)
		else:
			if created:
				success.append({"doc": get_link_to_form(doctype, created), "employee": employee_id})
			else:
				skipped.append(employee_id)

		frappe.publish_progress((i + 1) * 100 / len(employees), title=title)

	frappe.publish_realtime(
		event,
		message={"success": success, "failure": failure, "skipped": skipped},
		doctype="Bulk Employee Settings",
		after_commit=True,
	)


def _roles_by_employee(employees: list[str]) -> dict[str, list[str]]:
	"""Active Scheduling Roles held, per employee.

	Validity windows are deliberately not applied: this is a configuration
	screen, not a planning run, so a role held only from next month should still
	read as held rather than look like a gap waiting to be filled.
	"""
	rows = frappe.get_all(
		"Employee Scheduling Role",
		filters={"employee": ["in", employees], "active": 1},
		fields=["employee", "scheduling_role"],
	)
	held: dict[str, list[str]] = {}
	for row in rows:
		held.setdefault(row.employee, []).append(row.scheduling_role)
	return {employee: sorted(roles) for employee, roles in held.items()}
