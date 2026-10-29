"""Seed "Objective: Collateral duties (by configured rooms)" and swap it into the Standard Ruleset.

The two collateral-value rules are a choice group, and the standard answer is now the one
that prices a supervised post by the rooms its branch has rather than by the rooms that
happen to be staffed: scaled by staffing, a lead is worth more where more rooms are running,
and the optimizer answers that by gathering people into the branches that have one. The
re-seeding replaces the row in the Standard Ruleset; every other row keeps its weight (see
`create_standard_optimization_rules`).

A ruleset that wants the old behaviour keeps it — the rule is still there, still selectable,
and only its `standard` flag changed.
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
