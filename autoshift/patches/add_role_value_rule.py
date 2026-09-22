"""Seed "Objective: Value of working a role" and add it to the Standard Ruleset.

Inert until a `Scheduling Role` carries a non-zero `assignment_value`, which is every
site until somebody prices a standby role — so it is standard, and costs nothing to a
site that never uses it. The seeding preserves the weight on every row that survives
(see `create_standard_optimization_rules`).
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
