import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_link_to_form


class BulkEmployeeSettings(Document):
	@frappe.whitelist()
	def get_employees(self, advanced_filters: list) -> list:
		filters = [["company", "=", self.get("company")], ["status", "=", "Active"]]
		for f in ("department", "designation"):
			if self.get(f):
				filters.append([f, "=", self.get(f)])
		filters += advanced_filters

		existing = set(frappe.get_all("Employee Settings", pluck="name"))
		employees = frappe.get_all(
			"Employee",
			filters=filters,
			fields=["name as employee", "employee_name", "department", "designation"],
		)
		return [e for e in employees if e["employee"] not in existing]

	@frappe.whitelist()
	def bulk_create_settings(
		self,
		employees: list,
		favourite_shift: str | None = None,
		shift_preferences: list | None = None,
		preferred_branch: list | None = None,
	) -> None:
		if not employees:
			frappe.throw(_("Please select at least one employee."), title=_("No Employees Selected"))

		kwargs = dict(
			employees=employees,
			favourite_shift=favourite_shift,
			shift_preferences=shift_preferences or [],
			preferred_branch=preferred_branch or [],
		)
		if len(employees) <= 30:
			return self._bulk_create_settings(**kwargs)  # pyright: ignore[reportArgumentType]

		frappe.enqueue(self._bulk_create_settings, timeout=3000, **kwargs)  # pyright: ignore[reportArgumentType]
		frappe.msgprint(
			_("Creation has been queued. It may take a few minutes."),
			alert=True,
			indicator="blue",
		)

	def _bulk_create_settings(
		self,
		employees: list,
		favourite_shift: str | None,
		shift_preferences: list,
		preferred_branch: list,
	) -> None:
		success, failure = [], []
		savepoint = "before_employee_settings_creation"

		for i, emp in enumerate(employees):
			employee_id = emp["employee"] if isinstance(emp, dict) else emp
			try:
				frappe.db.savepoint(savepoint)
				if frappe.db.exists("Employee Settings", employee_id):
					continue

				doc = frappe.new_doc("Employee Settings")
				doc.set("employee", employee_id)
				doc.set("favourite_shift", favourite_shift or None)
				for row in shift_preferences:
					doc.append(
						"shift_preferences", {"shift_type": row["shift_type"], "weight": row["weight"]}
					)
				for row in preferred_branch:
					doc.append("preferred_branch", {"branch": row["branch"], "weight": row["weight"]})
				doc.insert(ignore_permissions=True)

			except Exception:
				frappe.db.rollback(save_point=savepoint)
				frappe.log_error(
					f"Bulk Employee Settings creation failed for {employee_id}.",
					reference_doctype="Employee Settings",
				)
				failure.append(employee_id)
			else:
				success.append(
					{"doc": get_link_to_form("Employee Settings", employee_id), "employee": employee_id}
				)

			frappe.publish_progress(
				(i + 1) * 100 / len(employees),
				title=_("Creating Employee Settings..."),
			)

		frappe.publish_realtime(
			"completed_bulk_employee_settings_creation",
			message={"success": success, "failure": failure},
			doctype="Bulk Employee Settings",
			after_commit=True,
		)
