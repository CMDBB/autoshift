# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimizerRunCoverage(Document):
	"""One (discipline, branch, date, shift) slot of a solved run's room coverage.

	Rows are written by the solver (see ``optimizer/solver.run_solve``) from the
	``active_rooms`` variable values — the room-coverage counterpart to the
	``Optimizer Run Slot`` rows carrying the ``x`` assignment values.
	"""
