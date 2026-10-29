"""Re-run the rule seeding so "Objective: FTE soft ceiling" exists as a rule document.

The statutory limits on working time a hard `fte_ceiling` stands in for are written
against a full-time week, so a part-timer's agreed percentage is a courtesy rather than a
cap — and a run that fails outright because honoring it was impossible helps nobody. The
soft rule penalizes the excess instead. Both sit in the new `workload_ceiling` choice
group; `fte_ceiling` keeps its place in the Standard Ruleset, so no row set changes here
and every hand-tuned weight survives (see ``create_standard_optimization_rules``).

A patch needs a *new* name to run on a site that already migrated, hence this shim.
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
