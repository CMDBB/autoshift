import traceback

import frappe
from frappe.model.document import Document

from autoshift.optimizer import data_loader


class OptimizerRun(Document):
	def before_save(self):
		if not self.status:
			self.status = "Draft"

	@frappe.whitelist()
	def enqueue_solve(self):
		if self.status != "Draft":
			frappe.throw(frappe._("Only Draft runs can be solved."))
		try:
			data = data_loader.load(self)
			frappe.enqueue(
				"autoshift.optimizer.solver.run_solve",
				run_name=self.name,
				data=data,
				queue="long",
				timeout=3600,
			)
		except Exception:
			tb = traceback.format_exc()
			frappe.log_error(tb, f"Optimizer Run failed: {self.name}")
			try:
				self.set("solver_log", (str(self.get("solver_log")) or "") + f"\n\nException:\n{tb}")
				self.db_set("status", "Failed")
				self.save(ignore_permissions=True)
			except Exception:
				pass

		self.set("status", "Solving")
		self.save()
		return "Solving"

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
