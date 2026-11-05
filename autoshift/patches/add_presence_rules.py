"""Seed the two rules the presence model added, and refresh the Standard Ruleset.

`exclusive_role_purity` (nothing collateral beside an exclusive role) and
`collateral_room_value_objective` (a collateral duty is worth the rooms it oversees) are both
standard, and both inert on a site that has no exclusive or collateral roles — which is every
site until somebody sets an Assignment Mode. The seeding creates one Optimization Rule per
built-in and syncs the Standard Ruleset's rows to the standard set, preserving the weight on
every row that survives (see `create_standard_optimization_rules`).
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
