"""Seed the "Bind settled schedules" rule and the Scheduling Rule Topic documents.

Same trick as add_role_rules: create_standard_optimization_rules is already marked
completed on migrated sites, so re-invoke it to pick up the newly registered built-in,
file every built-in under its topic, and create the topics themselves.
"""

from autoshift.patches.create_standard_optimization_rules import execute as _execute


def execute():
	_execute()
