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
	# Prototype: force employee E1 to be scheduled only in room 2
	employee = "E1"

	for (e, role, shift, day, branch, room_idx), var in ctx.room_occupancy.items():
		if e == employee and room_idx != 2:
			ctx.prob += (var == 0, cname("force_e1_room2", e, role, shift, day, branch, room_idx))
