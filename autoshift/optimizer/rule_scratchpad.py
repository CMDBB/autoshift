"""
Use this file to draft your Optimization Rule implementations
(Refer to the rules in rules.py)
The Namespace is designed to be as similar as possible to the rules environment
This file is commited AND gitignored, so changes don't bubble up
"""

import itertools

import pulp

from autoshift.optimizer.rules import RuleContext, path
from autoshift.optimizer.rules import _cname as cname
from autoshift.optimizer.rules import _vname as vname


def _custom_rule_scratchpad(ctx: RuleContext) -> None:
	# Prototype: enforce room preferences
	payload = {
		(0, 1, 57),
		(0, 2, 139),
		(0, 3, 139),
		(0, 4, 139),
		(0, 5, 57),
		(0, 6, 180),
		(0, 1, 139),
		(0, 2, 139),
		(0, 3, 180),
		(0, 4, 139),
		(0, 5, 141),
		(0, 6, 139),
		(1, 1, 139),
		(1, 2, 139),
		(1, 3, 141),
		(1, 4, 57),
		(1, 5, 198),
		(1, 1, 180),
		(1, 2, 139),
		(1, 3, 139),
		(1, 4, 139),
		(1, 6, 139),
		(2, 1, 57),
		(2, 2, 139),
		(2, 3, 139),
		(2, 4, 139),
		(2, 5, 180),
		(2, 6, 139),
		(2, 1, 198),
		(2, 2, 57),
		(2, 3, 139),
		(2, 4, 139),
		(2, 5, 141),
		(3, 1, 57),
		(3, 2, 198),
		(3, 3, 139),
		(3, 4, 139),
		(3, 5, 139),
		(3, 6, 139),
		(3, 1, 139),
		(3, 2, 57),
		(3, 3, 139),
		(3, 4, 178),
		(3, 5, 180),
		(3, 6, 139),
		(4, 1, 139),
		(4, 2, 139),
		(4, 3, 180),
		(4, 4, 139),
		(4, 5, 198),
		(4, 6, 178),
		(4, 1, 180),
		(4, 2, 57),
		(4, 3, 178),
		(4, 4, 198),
		(4, 6, 139),
		(4, 1, 180),
		(4, 2, 139),
		(4, 3, 139),
		(4, 4, 57),
		(4, 5, 198),
		(4, 6, 139),
		(4, 1, 139),
		(4, 2, 139),
		(4, 3, 180),
		(4, 4, 139),
		(4, 6, 178),
	}
	employees_concerned = {e for _, _, e in payload}

	for (e, role, shift, day, branch, room_idx), var in ctx.room_occupancy.items():
		if e in employees_concerned and (day.weekday(), room_idx, e) not in payload:
			ctx.add_objective(-1 * var, path(Weekday=day.weekday(), Room=room_idx, Employee=e))
