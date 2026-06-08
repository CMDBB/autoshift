import frappe
from frappe.model.document import Document


class EmployeeSettings(Document):
	def validate(self):
		self._validate_branch_weights()

	def _validate_branch_weights(self):
		if not self.preferred_branch:
			return
		total = sum(row.weight for row in self.preferred_branch)
		if total and abs(total - 1.0) > 1e-6:
			frappe.msgprint(
				frappe._("Branch preference weights sum to {0}; they should sum to 1.0.").format(
					round(total, 4)
				),
				indicator="orange",
			)
