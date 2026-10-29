# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HorizonScanWindow(Document):
	"""One window of a Horizon Scan: its run, its verdict, and its per-day findings.

	Rows are planned by ``scanner.scan.plan`` and filled in by ``scanner.scan.solve_window``
	— written straight to the database, one window at a time, so a sweep neither fights the
	parent's timestamp nor rewrites its siblings. ``findings`` is the raw JSON of
	``scanner.horizon.dumps``: which capacity a shortfall is measured against is the
	reader's question, applied on read.
	"""
