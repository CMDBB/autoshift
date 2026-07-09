"""
The objective function became rule-driven: seed the two built-in objective rule
documents, append them to the Standard Ruleset (carrying the legacy
turnover_weight over as the room-utilization row's weight), and backfill
rule_kind on existing built-in rule docs. All delegated to the idempotent
shared seeding logic.
"""

from autoshift.patches.create_standard_optimization_rules import execute as _seed


def execute():
	_seed()
