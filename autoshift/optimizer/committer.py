"""
Converts an approved Optimizer Run into submitted Shift Assignment records.
"""

from __future__ import annotations

import frappe.defaults


def commit(run_name: str) -> None:
	run = frappe.get_doc("Optimizer Run", run_name)

	if run.status != "Approved":
		frappe.throw(frappe._("Only Approved runs can be committed."))

	company = frappe.defaults.get_defaults().get("company")
	if not company:
		companies = frappe.get_all("Company", pluck="name", limit=1)
		company = companies[0] if companies else None

	created = []
	for slot in run.solution_table:
		sa = frappe.new_doc("Shift Assignment")
		sa.set("employee", slot.employee)
		sa.set("shift_type", slot.shift_type)
		sa.set("start_date", slot.date)
		sa.set("end_date", slot.date)
		sa.set("company", company)
		if slot.get("branch"):
			sa.set("branch", slot.branch)
		sa.insert(ignore_permissions=True)
		sa.submit()
		created.append(sa.name)

	# Link back to the run via the committed_assignments table
	# Table MultiSelect stores link values directly
	run.db_set("status", "Committed")
	# Re-fetch and attach committed assignment names
	run.reload()
	for sa_name in created:
		run.append("committed_assignments", {"link_doctype": "Shift Assignment", "link_name": sa_name})
	run.save(ignore_permissions=True)

	frappe.msgprint(
		frappe._("{0} Shift Assignment(s) created and submitted.").format(len(created)),
		indicator="green",
	)
