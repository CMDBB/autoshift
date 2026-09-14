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
from autoshift.optimizer.rules import _vname as vname


def _custom_rule_scratchpad(ctx: RuleContext) -> None:
	data = ctx.data
	# Disciplines covered by ArG Art 9 al. 1a. An employee's discipline comes from the
	# Scheduling Roles they hold, not from Employee.department — somebody may hold roles
	# in more than one, so the stricter ceiling applies if any of them is covered.
	al1a = {"Admin", "Sales"}
	al1a = {disc.partition(" ")[0] for disc in set(data.role_discipline.values()) if disc[:5] in al1a}
	print(f"Disciplines at 45h/w max: {al1a}")

	# Delimit weeks
	wd = data.working_days
	weeks = [
		wd[start:end]
		for start, end in itertools.pairwise(
			[0, *[i for i, monday in enumerate(wd) if monday.weekday() == 0], len(wd)]
		)
	]
	for e in data.employees:
		held = data.employee_roles.get(e, ())
		wh_ceiling = 45 if any(data.role_discipline[r] in al1a for r in held) else 50
		for i, week in enumerate(weeks):
			weekly_total = pulp.lpSum(
				ctx.x[(e, r, s, d, b)]
				for r in held
				for s in data.shift_types
				for d in week
				for b in data.branches
			)
			ctx.prob += (8 * weekly_total <= wh_ceiling, cname("week_max", e, i))

			# Auxiliary variables go through ctx.prob (pulp.LpVariable(...) is deprecated in
			# PuLP 4.0) and are named with vname, the counterpart to cname. This one measures
			# how far under the ceiling the week ran — see role_fte_target_objective in
			# rules.py for the same trick used to linearize an absolute deviation.
			slack = ctx.prob.add_variable(vname("week_slack", e, i), lowBound=0)
			ctx.prob += (slack == wh_ceiling - 8 * weekly_total, cname("week_slack", e, i))
