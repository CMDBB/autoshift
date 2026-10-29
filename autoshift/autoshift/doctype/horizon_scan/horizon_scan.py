# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from autoshift.scanner import horizon
from autoshift.scanner import scan as scan_engine


class HorizonScan(Document):
	"""A forward sweep of the optimizer: one window at a time, as far as the end date.

	The document carries only the question (which ruleset, how far ahead, in windows of
	what length) and the answer so far (one ``Horizon Scan Window`` row per window). The
	sweep itself lives in ``autoshift/scanner/scan.py`` and the page actions in
	``autoshift/scanner/api.py``; nothing here solves anything.

	Saving re-plans the windows, which is deliberately safe mid-sweep: a window already
	solved keeps its run and its findings, so pushing the end date out by a month costs
	only that month.
	"""

	def validate(self):
		if not self.status:  # ty:ignore[unresolved-attribute]
			self.status = "Draft"  # ty:ignore[unresolved-attribute]
		# A window is a whole week and an Optimizer Run starts on a Monday, so a scan
		# asked for from a Wednesday starts on the Monday before it rather than drawing a
		# short first week nobody asked for.
		self.start_date = horizon.monday_of(frappe.utils.getdate(self.start_date))  # ty:ignore[unresolved-attribute]
		scan_engine.plan(self)
		if self.has_value_changed("ruleset") and scan_engine.coverage_rule_gap(self.ruleset):  # ty:ignore[unresolved-attribute]
			# Said once, here, rather than fifty-two times inside the sweep.
			frappe.msgprint(
				frappe._(
					"{0} selects no room-coverage rule, so nothing ties staffed rooms to who is "
					"working them: every slot will read as shut and every day of this scan will "
					"be flagged. Pick a rule from the Room Coverage group."
				).format(self.ruleset),  # ty:ignore[unresolved-attribute]
				title=frappe._("Coverage cannot be measured"),
				indicator="orange",
			)
