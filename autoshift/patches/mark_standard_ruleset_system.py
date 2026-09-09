"""Backfill `is_system` on the Standard Ruleset.

Same trick as add_objective_rules/add_role_rules: create_standard_optimization_rules is
already marked completed on migrated sites, so re-invoke it to pick up the `is_system`
flag added to the seeding logic after those sites first ran it.
"""

from autoshift.patches.create_standard_optimization_rules import execute as _execute


def execute():
	_execute()
