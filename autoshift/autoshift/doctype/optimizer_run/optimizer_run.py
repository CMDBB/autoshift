import frappe.utils
from frappe.model.document import Document

from autoshift.optimizer import data_loader, types

# Time given to the synchronous attempt before falling back to a background job.
SYNC_TIME_LIMIT = 5


class OptimizerRun(Document):
	def before_save(self):
		if not self.status:
			self.status = "Draft"
		if not self.type:
			self.type = "Manual"

	def memoize_datapackage(self):
		cache = frappe.cache
		if cache is None:
			return data_loader.load(self)

		dataS = cache.get_value(f"DataPackage:{self.name}")
		if dataS is not None:
			return types.DataPackage.loads(dataS)
		data = data_loader.load(self)
		cache.set_value(f"DataPackage:{self.name}", data.dumps())

		return data

	@frappe.whitelist()
	def check_duplicates(self):
		"""Checks whether another run already solved this exact input.

		Returns the underlying duplicates
		"""
		from autoshift.optimizer.solver import find_cached_runs

		data = self.memoize_datapackage()

		cached_names = find_cached_runs(data.input_hash(), exclude_name=self.name)

		return {
			"n": len(cached_names),
			"cached_runs_list_link": frappe.utils.get_filtered_list_link,
		}

	@frappe.whitelist()
	def solve(self):
		"""Solve the run.

		Attempts to solve synchronously within SYNC_TIME_LIMIT seconds.
		If CBC doesn't conclude within that window, the same data is re-queued as a
		background job with the full timeout.
		"""
		if self.status != "Draft":
			frappe.throw(frappe._("Only Draft runs can be solved."))
		from autoshift.optimizer.solver import run_solve

		data = self.memoize_datapackage()
		self.set("status", "Solving")
		result = run_solve(str(self.name), self.memoize_datapackage(), time_limit=SYNC_TIME_LIMIT)
		if result == "TimedOut":
			frappe.enqueue(
				"autoshift.optimizer.solver.run_solve",
				run_name=self.name,
				data=data,
				time_limit=3600,
				queue="long",
				timeout=3600,
			)
			self.save()
			return "Solving"

		self.reload()
		return self.status

	@frappe.whitelist()
	def duplicate(self):
		"""Create a new Draft run with the same configuration as this one.

		Runs are immutable once solving starts. To re-try a Failed run,
		create a duplicate, the original stays untouched as a record.
		"""
		new_run = frappe.new_doc("Optimizer Run")
		new_run.set("mode", self.mode)  # pyright: ignore[reportAttributeAccessIssue]
		new_run.set("date", self.date)  # pyright: ignore[reportAttributeAccessIssue]
		new_run.set("disregard_assignments", self.disregard_assignments)  # pyright: ignore[reportAttributeAccessIssue]
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
