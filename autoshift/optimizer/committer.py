"""
Converts an approved Optimizer Run into submitted Shift Assignment records.
"""

from __future__ import annotations

import frappe


def commit(run_name: str) -> None:
	run = frappe.get_doc("Optimizer Run", run_name)

	if run.status != "Approved":  # pyright: ignore[reportAttributeAccessIssue]
		frappe.throw(frappe._("Only Approved runs can be committed."))

	# `committed_assignments` was removed from Optimizer Run while the run -> Shift
	# Assignment link-back mechanism is redesigned (see CLAUDE.md "To be implemented").
	# Fail before creating/submitting anything so a commit attempt can't leave behind
	# real Shift Assignments with no way to trace them back to this run.
	frappe.throw(
		frappe._(
			"Committing is not yet implemented: the run-to-Shift-Assignment link-back "
			"mechanism is still being designed. No records have been created."
		),
		exc=NotImplementedError,
	)
