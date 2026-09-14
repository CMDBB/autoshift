# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import getdate


class EmployeeSchedulingRole(Document):
	def validate(self):
		self._validate_unique()
		self._validate_window()
		self._warn_on_overcommitted_fte()

	def _validate_unique(self):
		"""The autoname already collides on a duplicate; say so legibly."""
		duplicate = frappe.db.exists(
			"Employee Scheduling Role",
			{
				"employee": self.employee,
				"scheduling_role": self.scheduling_role,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				frappe._("{0} already holds the Scheduling Role {1} ({2}).").format(
					frappe.bold(self.employee), frappe.bold(self.scheduling_role), duplicate
				)
			)

	def _validate_window(self):
		if self.valid_from and self.valid_to and getdate(self.valid_to) < getdate(self.valid_from):
			frappe.throw(frappe._("Valid To cannot be earlier than Valid From."))

	def _warn_on_overcommitted_fte(self):
		"""Warn, don't block: an over-committed split is expressible, it just can't all be met.

		The agreed figures are informal expectations feeding an objective term, so the
		solver degrades gracefully — but a total above the employee's FTE guarantees a
		residual penalty no schedule can remove, which is worth saying out loud once.
		"""
		if not self.role_fte or not self.active:
			return

		others = frappe.get_all(
			"Employee Scheduling Role",
			filters={"employee": self.employee, "active": 1, "name": ["!=", self.name]},
			pluck="role_fte",
		)
		total = float(self.role_fte) + sum(float(f or 0.0) for f in others)
		employee_fte = frappe.db.get_value("Employee", self.employee, "custom_fte") or 100.0

		if total > float(employee_fte) + 1e-6:
			frappe.msgprint(
				frappe._(
					"Agreed role FTE for {0} now totals {1}%, above their {2}% FTE. "
					"The optimizer will get as close as the FTE ceiling allows."
				).format(frappe.bold(self.employee), round(total, 2), round(float(employee_fte), 2)),
				indicator="orange",
			)
