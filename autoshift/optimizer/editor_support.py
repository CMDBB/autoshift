"""
Editor support for authoring Custom Code Optimization Rules in Desk.

Pure Python (no Frappe imports) — safe to use in tests.

:func:`completion_items` produces autocompletion entries for the
``implementation_code`` Ace editor (fed to Frappe's Code control via its
``autocompletions`` field property). The ``ctx.*`` and ``ctx.data.*`` entries
are introspected from :class:`~autoshift.optimizer.rules.RuleContext` and
:class:`~autoshift.optimizer.types.DataPackage`, so they cannot drift from the
API custom rules actually run against; the ``pulp``/``itertools`` entries are a
curated subset of what :func:`~autoshift.optimizer.rules.compile_custom_rule`
pre-imports into the rule's namespace.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools

import pulp

from .rules import RuleContext
from .types import DataPackage

# pulp members most useful inside apply(ctx); completion_items() asserts they exist
PULP_MEMBERS = (
	"lpSum",
	"lpDot",
	"LpVariable",
	"LpAffineExpression",
	"LpBinary",
	"LpInteger",
	"LpContinuous",
	"value",
)

# itertools members the built-in rules themselves rely on
ITERTOOLS_MEMBERS = ("product", "chain", "combinations", "permutations")

# local util methods from the rules.py
RULE_UTILS = (
	"cname",  # _cname in rules.py
	"vname",  # _vname in rules.py — names auxiliary variables a rule introduces
)

# ranking in the completion popup: domain API above library members
_SCORES = {
	"ctx": 100,
	"data": 90,
	"utils": 85,
	"pulp": 80,
	"itertools": 70,
}


def _public_methods(cls) -> list[str]:
	return [
		name
		for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
		if not name.startswith("_")
	]


def completion_items() -> list[dict]:
	"""Autocompletion entries (``{"value", "meta", "score"}``) for the rule-code editor."""
	items: list[dict] = []

	def add(value: str, meta: str) -> None:
		items.append({"value": value, "meta": meta, "score": _SCORES[meta]})

	# ctx fields and methods (RuleContext)
	for f in dataclasses.fields(RuleContext):
		if not f.name.startswith("_"):
			add(f"ctx.{f.name}", "ctx")
	for name in _public_methods(RuleContext):
		add(f"ctx.{name}", "ctx")

	# ctx.data fields (DataPackage) and its flag constants
	for f in dataclasses.fields(DataPackage):
		add(f"ctx.data.{f.name}", "data")
	for name, value in vars(DataPackage).items():
		if not name.startswith("_") and isinstance(value, str):
			add(f"ctx.data.{name}", "data")

	# curated members of the modules compile_custom_rule pre-imports
	for name in PULP_MEMBERS:
		getattr(pulp, name)  # raise early if pulp ever renames one
		add(f"pulp.{name}", "pulp")
	for name in ITERTOOLS_MEMBERS:
		getattr(itertools, name)
		add(f"itertools.{name}", "itertools")
	for name in RULE_UTILS:
		add(name, "utils")

	return items
