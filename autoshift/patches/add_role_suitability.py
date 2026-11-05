"""Seed "Objective: Shift preferences and role suitability" and swap it into the Standard Ruleset.

The new objective is `shift_preference_objective` with every assignment's cost scaled by
Employee Scheduling Role.suitability. The two share the `shift_preference` choice group and
only the new one is standard now, so the re-seeding replaces the row in the Standard
Ruleset (every other row keeps its weight — see ``create_standard_optimization_rules``).

Rows that predate the field hold a regular holder's 1: backfilled here rather than trusting
the column default, since a blank Float reads as 0 in reports and the Role Matrix.
"""

import frappe

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	frappe.db.sql(
		"""update `tabEmployee Scheduling Role`
		set suitability = 1
		where suitability is null or suitability = 0"""
	)
	seed()
