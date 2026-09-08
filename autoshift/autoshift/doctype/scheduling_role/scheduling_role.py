# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SchedulingRole(Document):
	def validate(self):
		if self.max_rooms is not None and self.max_rooms < 1:
			frappe.throw(
				frappe._("Max Rooms Per Holder must be at least 1; a role nobody can staff is not a role.")
			)
		if self.chip_sort_field and not frappe.get_meta("Employee").has_field(self.chip_sort_field):
			frappe.throw(
				frappe._("{0} is not a field on Employee, so chip order cannot use it.").format(
					self.chip_sort_field
				)
			)
