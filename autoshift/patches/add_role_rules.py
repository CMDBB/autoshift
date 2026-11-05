"""Seed the Optimization Rule documents for the Scheduling Role built-ins.

Same trick as add_objective_rules: create_standard_optimization_rules is already marked
completed on migrated sites, so re-invoke it to pick up newly registered built-ins.
"""

from autoshift.patches.create_standard_optimization_rules import execute as _execute


def execute():
	_execute()
