"""Re-run the rule seeding so the Standard Ruleset drops "Honor existing Shift
Assignments".

Pinning every employee to the books made most historical weeks infeasible under the
rest of the standard rules; only the people whose schedule is settled by them and not
by the planner are frozen now, by "Bind settled schedules". The rule itself stays
available and Optimizer Studio still offers it — it simply left ``STANDARD_RULES``, and
the seeding syncs the Standard Ruleset's rows to that set. Every other row's
hand-tuned weight survives (see ``create_standard_optimization_rules``).

A patch needs a *new* name to run on a site that already migrated, hence this shim.
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
