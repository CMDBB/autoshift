# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class EmployeeRoleSynergy(Document):
	def before_insert(self):
		# canonicalize before `set_new_name` resolves the autoname format, so (A, B) and
		# (B, A) always collide on the same document rather than needing a reverse-pair query
		self._canonicalize_pair()

	def validate(self):
		self._canonicalize_pair()
		self._validate_not_self_paired()
		self._validate_multiplier()

	def _canonicalize_pair(self):
		"""The relation is symmetric: keep the pair sorted so a later edit can't desync it
		from the document name an insert already canonicalized."""
		if self.employee_a and self.employee_b and self.employee_a > self.employee_b:
			self.employee_a, self.employee_b = self.employee_b, self.employee_a

	def _validate_not_self_paired(self):
		if self.employee_a and self.employee_b and self.employee_a == self.employee_b:
			frappe.throw(frappe._("An employee cannot have a synergy value with themselves."))

	def _validate_multiplier(self):
		if not self.synergy_multiplier:
			# a Float left blank arrives as 0; the loader reads that as no bonus, same as 1
			self.synergy_multiplier = 1
