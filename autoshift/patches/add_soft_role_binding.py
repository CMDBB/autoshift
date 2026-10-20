"""Re-run the rule seeding so "Bind settled schedules" (soft) exists and the Standard
Ruleset uses it in place of the strict rule.

Soft binding fixes a bound holder's *unworked* combinations to 0 like the strict rule, but
leaves the shifts on their books as free variables warm-started to 1, so a week that was
worked double-booked or off-config comes back as a schedule instead of "Infeasible". Both
rules sit in the `role_binding` choice group, so a ruleset carrying the strict rule keeps
it — only the Standard Ruleset's row set is re-synced to `STANDARD_RULES`, with every
surviving row's hand-tuned weight preserved (see ``create_standard_optimization_rules``).

A patch needs a *new* name to run on a site that already migrated, hence this shim.
"""

from autoshift.patches.create_standard_optimization_rules import execute as seed


def execute():
	seed()
