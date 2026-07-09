"""
Use this file to draft your Optimization Rule implementations
(Refer to the rules in rules.py)
The Namespace is designed to be as similar as possible to the rules environment
This file is commited AND gitignored, so changes don't bubble up
"""

import itertools

import pulp

from autoshift.optimizer.rules import RuleContext
from autoshift.optimizer.rules import _cname as cname


def _custom_rule_scratchpad(ctx: RuleContext) -> None:
	data = ctx.data
	# Departments covered by ArG Art 9 al. 1a
	al1a = {"Admin", "Sales"}
	al1a = {dep.partition(" ")[0] for dep in set(data.department.values()) if dep[:5] in al1a}
	print(f"Departments at 45h/w max: {al1a}")

	# Delimit weeks
	wd = data.working_days
	weeks = [
		wd[start:end]
		for start, end in itertools.pairwise(
			[0, *[i for i, monday in enumerate(wd) if monday.weekday() == 0], len(wd)]
		)
	]
	for e in data.employees:
		wh_ceiling = 45 if data.department[e] in al1a else 50
		for i, week in enumerate(weeks):
			weekly_total = pulp.lpSum(
				ctx.x[(e, s, d, b)] for s in data.shift_types for d in week for b in data.branches
			)
			ctx.prob += (8 * weekly_total <= wh_ceiling, cname("week_max", e, i))
