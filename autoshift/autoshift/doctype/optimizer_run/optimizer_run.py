import frappe
from frappe.model.document import Document

from autoshift.optimizer import data_loader

# Time given to the synchronous attempt before falling back to a background job.
SYNC_TIME_LIMIT = 5


class OptimizerRun(Document):
	def before_save(self):
		if not self.status:
			self.status = "Draft"
		if not self.type:
			self.type = "Manual"

	@frappe.whitelist()
	def solve(self):
		"""Solve the run.

		First checks whether another run already solved this exact input. If
		so, nothing is solved or persisted here. This Draft is left untouched
		(its hash stays unset) so it can still be solved later if the
		underlying data changes. The caller is told which run to look at
		instead. The hash is only recorded once a real solve attempt
		runs (see solver.run_solve).

		Otherwise attempts to solve synchronously within SYNC_TIME_LIMIT seconds.
		If CBC doesn't conclude within that window, the same data is re-queued as a
		background job with the full timeout.

		Returns {"status": ..., "cached_run": <name>|None}.
		"""
		if self.status != "Draft":
			frappe.throw(frappe._("Only Draft runs can be solved."))
		from autoshift.optimizer.solver import find_cached_run, run_solve

		data = data_loader.load(self)

		if not frappe.conf.get("developer_mode"):
			cached_name = find_cached_run(data.input_hash(), exclude_name=self.name)
			if cached_name:
				return {"status": self.status, "cached_run": cached_name}

		self.db_set("status", "Solving")

		result = run_solve(self.name, data, time_limit=SYNC_TIME_LIMIT)

		if result == "TimedOut":
			frappe.enqueue(
				"autoshift.optimizer.solver.run_solve",
				run_name=self.name,
				data=data,
				time_limit=3600,
				queue="long",
				timeout=3600,
			)
			return {"status": "Solving", "cached_run": None}

		self.reload()
		return {"status": self.status, "cached_run": None}

	@frappe.whitelist()
	def duplicate(self):
		"""Create a new Draft run with the same configuration as this one.

		Runs are immutable once solving starts. To re-try a Failed run,
		create a duplicate, the original stays untouched as a record.
		"""
		new_run = frappe.new_doc("Optimizer Run")
		new_run.set("mode", self.mode)
		new_run.set("date", self.date)
		new_run.set("disregard_assignments", self.disregard_assignments)
		new_run.set("type", "Copy")
		for row in self.get("leaves_speculations") or []:
			new_run.append("leaves_speculations", {"leave_application": row.leave_application})
		new_run.insert()
		return new_run.name

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
