# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SchedulingRuleTopic(Document):
	"""
	A heading Optimization Rules can file themselves under, so Optimizer Studio's toggle
	panel reads as a handful of collapsible sections rather than one flat list.

	Purely presentational: unlike ``BuiltinRule.group`` (a mutually-exclusive choice set the
	solver enforces) a topic constrains nothing about which rules may be combined.
	"""

	pass
