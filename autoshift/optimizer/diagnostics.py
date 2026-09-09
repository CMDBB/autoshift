"""
Introspection for the MILP: what the model actually contains, and — when it comes back
infeasible — which constraints the input makes impossible to satisfy.

CBC tells you "Infeasible" and nothing else, which is useless when the suspicion is that
reality (existing Shift Assignments, frozen in place by ``bind_role_assignments``) is
illegal under the ruleset. Three tools, in increasing cost:

1. :func:`conflict_scan` — no solver at all. Replays the pinned assignments against the
   arithmetic of the selected constraint rules and names the ones that cannot hold. This
   is the fast answer to "which day of whose settled rota breaks the model".
2. :func:`model_dump` — the variables and constraints themselves, grouped by the rule
   that emitted them (the ``_cname``/``_vname`` ``prefix:`` convention). Plus
   :func:`write_lp` for the full LP file when nothing less will do.
3. :func:`elastic_analysis` — adds a non-negative slack to every constraint and minimizes
   the total violation. Every model is feasible once elasticized, so the slacks that come
   back non-zero *are* the infeasibility, measured in units of the constraint they broke.
   This finds conflicts :func:`conflict_scan` cannot reason about, at the cost of a solve.

:func:`lp_relaxation` drops integrality so CBC returns duals; ``LpConstraint.pi`` and
``LpVariable.dj`` are meaningful only on the relaxation, never on the MILP.

Pure Python (no Frappe imports) — usable from tests and the sandbox notebook.
"""

from __future__ import annotations

import collections
import datetime
import itertools
from dataclasses import dataclass, field

import pulp

from . import model_builder
from .rules import (
	BUILTIN_RULES,
	RuleContext,
	bind_role_assignments,
	fte_ceiling,
	leave_blocklist,
	legacy_ruleset,
	one_branch_per_shift,
	one_shift_per_day,
	role_fte_ceiling,
	use_existing_assignments,
)
from .types import DataPackage

#: Slack variables the elastic analysis introduces are named with this prefix so they
#: never collide with a rule's own auxiliary variables (`_vname`).
SLACK_PREFIX = "__elastic"

#: Below this a slack is rounding noise from CBC, not a real violation.
VIOLATION_EPS = 1e-6

COMB = tuple[str, str, str, datetime.date, str]  # (employee, role, shift_type, date, branch)


# ── Grouping ─────────────────────────────────────────────────────────────────


def group_of(name: str) -> str:
	"""
	The rule-ish family a variable or constraint name belongs to.

	``_cname``/``_vname`` emit ``"<prefix>:<prefix>_<parts...>"``, so everything a rule
	created shares one prefix. PuLP's own ``add_variable_dict`` names are
	``"<prefix>_<index parts...>"`` instead, which is why the underscore is a fallback
	rather than the primary split.
	"""
	head, sep, _ = name.partition(":")
	if sep:
		return head
	return name.split("_")[0] or name


def _constraints(prob: pulp.LpProblem) -> list[pulp.LpConstraint]:
	"""``LpProblem.constraints`` is a callable deprecation shim in PuLP 3.x, a dict before."""
	cons = prob.constraints
	if callable(cons):
		return list(cons())
	return list(cons.values())


# ── 1. Solver-free conflict scan ─────────────────────────────────────────────


@dataclass(frozen=True)
class Conflict:
	"""One reason the pinned assignments cannot satisfy a selected constraint rule."""

	rule: str  # built-in key of the rule the pinned data breaks
	subject: str  # who/what it is about, in the model's own vocabulary
	detail: str  # the arithmetic, spelled out

	def __str__(self) -> str:
		return f"[{self.rule}] {self.subject}: {self.detail}"


def selected_builtins(data: DataPackage) -> set[str]:
	"""
	The built-in keys this package's ruleset applies.

	Resolved exactly the way ``apply_rules`` resolves it, ``legacy_ruleset`` fallback
	included — a scan that disagreed with the model about which rules are in play would
	report conflicts the model does not have, or miss the one it does.
	"""
	return {key for _name, key, _code, _weight in (data.rules or legacy_ruleset(data)) if key}


def pinned_assignments(data: DataPackage) -> dict[COMB, str]:
	"""
	The ``forced`` combinations the selected rules nail to 1, and which rule nails them.

	``bind_role_assignments`` pins only the combinations belonging to a binding
	``(employee, role)`` pair — and, separately, pins everything *else* of theirs to 0,
	which no conflict here can be about. ``use_existing_assignments`` pins the lot.
	``soft_bind_role_assignments`` pins *nothing* on: a bound holder's own shifts stay
	free variables under it, which is exactly why it cannot produce these conflicts and
	why it is the default.
	"""
	selected = selected_builtins(data)
	pinned: dict[COMB, str] = {}
	if use_existing_assignments.__name__ in selected:
		pinned |= dict.fromkeys(data.forced, use_existing_assignments.__name__)
	if bind_role_assignments.__name__ in selected:
		pinned |= {
			comb: bind_role_assignments.__name__
			for comb in data.forced
			if (comb[0], comb[1]) in data.binding_pairs
		}
	return pinned


def conflict_scan(data: DataPackage) -> list[Conflict]:
	"""
	Find, without solving anything, where the pinned assignments contradict a selected rule.

	Only the constraint rules whose feasibility is decidable from the pinned set alone are
	checked: an assignment pinned on either fits under a ceiling or it does not, no search
	required. ``room_coverage`` is absent on purpose — its constraints are ``>=`` against a
	variable with a zero lower bound and can never be the infeasible one.
	"""
	selected = selected_builtins(data)
	pinned = pinned_assignments(data)
	day_set = set(data.working_days)
	conflicts: list[Conflict] = []

	# Structural: a pinned combination with no variable behind it. The loader normally
	# catches this (`_unresolvable` throws for a bound employee), so reaching it means a
	# package was hand-built or captured before that check existed.
	for e, r, s, d, b in sorted(pinned):
		missing = (
			e not in data.employees
			or r not in data.employee_roles.get(e, ())
			or s not in data.shift_types
			or d not in day_set
			or b not in data.branches
		)
		if missing:
			conflicts.append(
				Conflict(
					"(structural)",
					f"{e} / {r}",
					f"pinned to {s} at {b} on {d}, but that combination has no decision "
					f"variable (unknown employee, role they do not hold, shift type, "
					f"non-working day or unknown branch)",
				)
			)

	# leave_blocklist fixes every variable of a blocked (employee, day) to 0; warm_start
	# raises on the collision first, so this reports the same thing more legibly.
	if leave_blocklist.__name__ in selected:
		for comb, by in sorted(pinned.items()):
			e, _r, s, d, b = comb
			if (e, d) in data.leave_blocked:
				conflicts.append(
					Conflict(
						leave_blocklist.__name__,
						f"{e} on {d}",
						f"on leave, but {by} pins {s} at {b} that day",
					)
				)

	# one_shift_per_day: at most one shift a day across every role, shift type and branch.
	# A practice running half-day rotas trips this the moment somebody's settled week has
	# a morning and an afternoon on the same date.
	if one_shift_per_day.__name__ in selected:
		by_day: dict[tuple[str, datetime.date], list[COMB]] = collections.defaultdict(list)
		for comb in pinned:
			by_day[(comb[0], comb[3])].append(comb)
		for (e, d), combs in sorted(by_day.items()):
			if len(combs) > 1:
				spelled = ", ".join(f"{s} at {b} as {r}" for _e, r, s, _d, b in sorted(combs))
				conflicts.append(
					Conflict(
						one_shift_per_day.__name__,
						f"{e} on {d}",
						f"pinned to {len(combs)} shifts ({spelled}), but the rule allows one per day",
					)
				)

	if one_branch_per_shift.__name__ in selected:
		by_slot: dict[tuple[str, str, datetime.date], set[str]] = collections.defaultdict(set)
		for e, _r, s, d, b in pinned:
			by_slot[(e, s, d)].add(b)
		for (e, s, d), branches in sorted(by_slot.items()):
			if len(branches) > 1:
				conflicts.append(
					Conflict(
						one_branch_per_shift.__name__,
						f"{e}, {s} on {d}",
						f"pinned at {len(branches)} branches ({', '.join(sorted(branches))}) in one shift",
					)
				)

	# fte_ceiling / role_fte_ceiling: pinned shifts count against the same ceiling the
	# optimizer's own assignments do, so a settled rota fuller than the contract is fatal.
	tol = 1.05
	if fte_ceiling.__name__ in selected:
		per_employee = collections.Counter(comb[0] for comb in pinned)
		for e, count in sorted(per_employee.items()):
			target = data.target_shifts.get(e, 0)
			if target > 0 and count > tol * target:
				conflicts.append(
					Conflict(
						fte_ceiling.__name__,
						e,
						f"pinned to {count} shifts over the horizon, ceiling is "
						f"{tol * target:.2f} (105% x an FTE target of {target})",
					)
				)

	if role_fte_ceiling.__name__ in selected:
		per_pair = collections.Counter((comb[0], comb[1]) for comb in pinned)
		for (e, r), count in sorted(per_pair.items()):
			target = data.role_target_shifts.get((e, r), 0.0)
			if target > 0 and count > tol * target:
				conflicts.append(
					Conflict(
						role_fte_ceiling.__name__,
						f"{e} / {r}",
						f"pinned to {count} shifts in this role, ceiling is {tol * target:.2f} "
						f"(105% x an agreed role FTE of {target:.2f})",
					)
				)

	return conflicts


# ── 2. Model dump ────────────────────────────────────────────────────────────


@dataclass
class GroupSummary:
	name: str
	count: int = 0
	fixed: int = 0  # variables pinned to a single value (lowBound == upBound)
	fixed_on: int = 0  # ... of those, pinned to something non-zero
	examples: list[str] = field(default_factory=list)


def variable_summary(prob: pulp.LpProblem, examples: int = 3) -> list[GroupSummary]:
	"""Per-family variable counts, with how many are pinned — the shape binding gives the model."""
	groups: dict[str, GroupSummary] = {}
	for var in prob.variables():
		group = groups.setdefault(group_of(var.name), GroupSummary(group_of(var.name)))
		group.count += 1
		pinned = var.lowBound is not None and var.lowBound == var.upBound
		if pinned:
			group.fixed += 1
			if var.lowBound:
				group.fixed_on += 1
				if len(group.examples) < examples:
					group.examples.append(f"{var.name} = {var.lowBound:g}")
	return sorted(groups.values(), key=lambda g: (-g.count, g.name))


def constraint_summary(prob: pulp.LpProblem, examples: int = 3) -> list[GroupSummary]:
	"""Per-rule constraint counts. The prefix is the rule that emitted them (see `_cname`)."""
	groups: dict[str, GroupSummary] = {}
	for con in _constraints(prob):
		name = str(con.name)
		group = groups.setdefault(group_of(name), GroupSummary(group_of(name)))
		group.count += 1
		if len(group.examples) < examples:
			group.examples.append(f"{name}:  {con}")
	return sorted(groups.values(), key=lambda g: (-g.count, g.name))


def model_dump(prob: pulp.LpProblem, examples: int = 3) -> str:
	"""Human-readable shape of the built problem: variable families, constraint families."""
	lines = [
		f"Problem {prob.name!r}: {len(prob.variables())} variables, "
		+ f"{len(_constraints(prob))} constraints, "
		+ f"sense={'maximize' if prob.sense == pulp.LpMaximize else 'minimize'}",
		"",
		"Variables by family (fixed = lower bound equals upper bound):",
	]
	for group in variable_summary(prob, examples):
		lines.append(f"  {group.name:<24} {group.count:>7}   fixed {group.fixed:>7}  (on: {group.fixed_on})")
		lines.extend(f"      e.g. {ex}" for ex in group.examples)
	lines += ["", "Constraints by rule:"]
	for group in constraint_summary(prob, examples):
		lines.append(f"  {group.name:<24} {group.count:>7}")
		lines.extend(f"      e.g. {ex}" for ex in group.examples)
	return "\n".join(lines)


def write_lp(prob: pulp.LpProblem, path: str) -> str:
	"""Write the full LP file. The dump of last resort — every row, every bound."""
	prob.writeLP(path)
	return path


# ── 3. Elastic infeasibility analysis ────────────────────────────────────────


@dataclass(frozen=True)
class Violation:
	constraint: str
	rule: str
	amount: float
	expression: str

	def __str__(self) -> str:
		return f"[{self.rule}] {self.constraint} violated by {self.amount:g}   ({self.expression})"


def elasticize(prob: pulp.LpProblem) -> dict[str, list[pulp.LpVariable]]:
	"""
	Make every constraint breakable, and minimize the total breakage.

	Each constraint gains a non-negative slack on the side that can violate it (both
	sides, for an equality), the objective is replaced by the sum of those slacks, and the
	sense is flipped to minimize. Variable *bounds* are deliberately left alone: the
	binding rules express themselves as fixed bounds, so leaving them rigid is what makes
	the resulting slacks say "given the schedule you froze, here is what breaks".

	Mutates `prob` in place — build a throwaway model for it.
	"""
	slacks: dict[str, list[pulp.LpVariable]] = {}
	for con in _constraints(prob):
		name = str(con.name)
		match con.sense:
			case pulp.LpConstraintLE:
				surplus = prob.add_variable(f"{SLACK_PREFIX}_over_{name}", lowBound=0)
				con.addInPlace(-surplus)
				slacks[name] = [surplus]
			case pulp.LpConstraintGE:
				shortfall = prob.add_variable(f"{SLACK_PREFIX}_under_{name}", lowBound=0)
				con.addInPlace(shortfall)
				slacks[name] = [shortfall]
			case _:  # equality: it can be missed from either direction
				over = prob.add_variable(f"{SLACK_PREFIX}_over_{name}", lowBound=0)
				under = prob.add_variable(f"{SLACK_PREFIX}_under_{name}", lowBound=0)
				con.addInPlace(under - over)
				slacks[name] = [over, under]

	prob.sense = pulp.LpMinimize
	prob.setObjective(pulp.lpSum(itertools.chain.from_iterable(slacks.values())))
	return slacks


def elastic_analysis(
	data: DataPackage,
	time_limit: int = 60,
	integral: bool = False,
) -> tuple[str, list[Violation]]:
	"""
	Build a throwaway copy of the model, break every constraint elastically, and report
	the smallest set of violations that makes the input satisfiable.

	:param bool integral: solve the elastic model as a MILP. The default relaxes
		integrality first, which is both much faster and, for the case this exists to
		diagnose, exact — a conflict between frozen assignments is a conflict between
		*constants*, and no amount of branching resolves it. Escalate to ``True`` when
		the relaxation reports nothing yet the real model is infeasible: that is the
		signature of an infeasibility only integrality creates.

	:returns: ``(solver status, violations sorted by size)``. An empty list with an
		``Optimal`` status means every constraint can be satisfied at once, so an
		infeasible real model is down to integrality (try ``integral=True``) or to
		variable bounds contradicting each other.
	"""
	prob, _x, _active_rooms, _logs, _ctx = model_builder.build(data)
	if not integral:
		relax_integrality(prob)
	slacks = elasticize(prob)
	prob.solve(pulp.COIN_CMD(timeLimit=time_limit, msg=False))
	status = pulp.LpStatus[prob.status]

	by_name = {str(con.name): con for con in _constraints(prob)}
	violations = []
	for name, vars_ in slacks.items():
		amount = sum(abs(pulp.value(v) or 0.0) for v in vars_)
		if amount > VIOLATION_EPS:
			violations.append(Violation(name, group_of(name), amount, str(by_name[name])))
	return status, sorted(violations, key=lambda v: (-v.amount, v.constraint))


# ── 4. LP relaxation (shadow prices) ─────────────────────────────────────────


def relax_integrality(prob: pulp.LpProblem) -> pulp.LpProblem:
	"""
	Turn every integer and binary variable continuous, in place.

	Duals only exist for an LP: CBC reports ``pi`` on constraints and ``dj`` on variables
	for a relaxation and nothing at all for a MILP, so this is the prerequisite for
	reading a shadow price. Bounds are untouched, which keeps a binary in [0, 1] and
	keeps a ``fixValue()``-ed variable pinned.
	"""
	for var in prob.variables():
		var.cat = pulp.LpContinuous
	return prob


def lp_relaxation(data: DataPackage) -> tuple[pulp.LpProblem, dict, dict, RuleContext]:
	"""Build the model and immediately relax it, for a dual-valued solve. Solve it yourself."""
	prob, x, active_rooms, _logs, ctx = model_builder.build(data)
	relax_integrality(prob)
	return prob, x, active_rooms, ctx


def shadow_prices(prob: pulp.LpProblem, threshold: float = VIOLATION_EPS) -> list[tuple[str, float]]:
	"""
	Non-zero constraint duals of a *solved LP relaxation*, largest magnitude first.

	The dual of a constraint is what one more unit of its right-hand side is worth to the
	objective — which room cap, which FTE ceiling, is the one actually holding the
	schedule back. ``None`` on every constraint means the problem was still a MILP when
	it solved (see :func:`relax_integrality`) or the solver returned no duals.
	"""
	priced = [
		(str(con.name), float(con.pi))
		for con in _constraints(prob)
		if con.pi is not None and abs(con.pi) > threshold
	]
	return sorted(priced, key=lambda row: (-abs(row[1]), row[0]))


# ── Report ───────────────────────────────────────────────────────────────────


def package_summary(data: DataPackage) -> str:
	"""The input's dimensions and the pinning that acts on it."""
	pinned = pinned_assignments(data)
	bound_employees = {e for e, _r in data.binding_pairs}
	selected = sorted(selected_builtins(data))
	custom = [name for name, key, code, _w in (data.rules or ()) if code and not key]
	origin = "" if data.rules else "  (no ruleset on the run — the legacy standard selection)"
	unknown = sorted(set(selected) - set(BUILTIN_RULES))
	lines = [
		f"input hash        {data.input_hash()}",
		f"employees         {len(data.employees)}",
		f"working days      {len(data.working_days)}"
		+ (f"  ({data.working_days[0]} .. {data.working_days[-1]})" if data.working_days else ""),
		f"shift types       {len(data.shift_types)}  ({', '.join(data.shift_types)})",
		f"branches          {len(data.branches)}",
		f"disciplines       {len(data.disciplines)}",
		f"roles             {len(data.roles)}",
		f"leave-blocked     {len(data.leave_blocked)} (employee, day) pairs",
		f"existing assign.  {len(data.forced)} resolved, {len(data.binding_conflicts)} lost to leave, "
		+ f"{len(data.unresolved_assignments)} unplaceable",
		f"binding pairs     {len(data.binding_pairs)} over {len(bound_employees)} employees",
		f"pinned to 1       {len(pinned)} assignments",
		f"rules             {len(selected)} built-in{origin}",
		f"                  {', '.join(selected)}",
	]
	if custom:
		lines.append(f"custom-code rules {len(custom)}: {', '.join(sorted(custom))} (not scanned)")
	if unknown:
		lines.append(f"UNKNOWN KEYS      {', '.join(unknown)}")
	return "\n".join(lines)


def report(
	data: DataPackage,
	*,
	elastic: bool = True,
	examples: int = 3,
	time_limit: int = 60,
) -> str:
	"""
	The whole picture as text: input, conflicts, model shape, and (optionally) the
	elastic analysis. This is what a Failed run's solver log gets appended, and what the
	``diagnose-run`` bench command prints.
	"""
	sections = [
		"=" * 78,
		"AUTOSHIFT MODEL DIAGNOSTICS",
		"=" * 78,
		"",
		"--- Input ---",
		package_summary(data),
		"",
		"--- Pinned-assignment conflicts (no solver) ---",
	]

	conflicts = conflict_scan(data)
	if conflicts:
		sections.append(
			f"{len(conflicts)} conflict(s). Each is an existing Shift Assignment that a "
			f"selected rule forbids; the model cannot be feasible while all of them are pinned."
		)
		sections.extend(f"  {conflict}" for conflict in conflicts)
	else:
		sections.append("None. Nothing pinned contradicts a rule this scan can decide on its own.")

	try:
		prob, _x, _ar, _logs, _ctx = model_builder.build(data)
	except Exception as exc:  # a model that will not even build has no shape to dump
		sections += ["", "--- Model ---", f"build() raised: {exc!r}"]
		return "\n".join(sections)

	sections += ["", "--- Model ---", model_dump(prob, examples)]

	if elastic:
		sections += ["", "--- Elastic analysis (minimum total constraint violation) ---"]
		status, violations = elastic_analysis(data, time_limit=time_limit)
		sections.append(f"elastic LP relaxation solved: {status}")
		if violations:
			total = sum(v.amount for v in violations)
			sections.append(f"{len(violations)} constraint(s) must give, by {total:g} in total:")
			sections.extend(f"  {violation}" for violation in violations)
		elif status == "Optimal":
			sections.append(
				"No constraint needs to give: every constraint is satisfiable at once in the "
				"relaxation. An infeasible real model is then down to integrality (re-run "
				"elastic_analysis(integral=True)) or to variable bounds that contradict "
				"each other."
			)
	return "\n".join(sections)
