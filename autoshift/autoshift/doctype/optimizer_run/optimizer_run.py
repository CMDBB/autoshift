import frappe
from frappe.model.document import Document


class OptimizerRun(Document):
	def before_save(self):
		if not self.status:
			self.status = "Draft"

	@frappe.whitelist()
	def enqueue_solve(self):
		if self.status != "Draft":
			frappe.throw(frappe._("Only Draft runs can be solved."))
		self.db_set("status", "Solving")
		frappe.enqueue(
			"autoshift.optimizer.solver.run_solve",
			run_name=self.name,
			queue="long",
			timeout=3600,
		)
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

		commit(self.name)
