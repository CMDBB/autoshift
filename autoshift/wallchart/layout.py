# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The chart's shape, derived from the configuration rather than declared.

A practice that draws its week on paper describes that sheet somewhere — which
bands exist, how many lines each has, which columns sit under a weekday. In
autoshift none of that needs saying twice, because the configuration already
carries it:

    Discipline Branch Config      one band, at (branch, discipline)
      .rooms_num                  how many rows it has
      .shift_types                which of the stacked tables it appears in
    Scheduling Role               one lane per role of the band's discipline
    Shift Type                    one stacked table each, ordered by start time

So there is no layout file and nothing to keep in sync: configure a discipline
at a branch and its band appears; add a Scheduling Role and its column appears,
empty until somebody covers it. That emptiness is the point — the chart's job is
to show what the configuration says *should* be staffed next to what actually
is.

The one judgement here is lane order, and it is `max_rooms` descending then
name. A role whose holder covers more rooms sorts left, which puts a
practitioner ahead of an assistant wherever that is how the numbers fall — a
property of the site's own data, not a fact this package asserts about anybody's
job.
"""

from __future__ import annotations

import datetime

import frappe

from .chart import Band, Lane, Layout, Section

#: Band label for slots no configured band claimed.
OVERFLOW_LABEL = "Unplaced"

#: Sort key for a Shift Type recording no start time: after every real one, so a
#: half-configured Shift Type prints at the bottom rather than above the morning.
_NO_START_TIME = datetime.timedelta(days=1)


def _department_labels(names: set[str]) -> dict[str, str]:
	"""Department name -> `department_name`.

	ERPNext names a Department "<department_name> - <company abbr>", and the
	suffix is noise on a chart where every band shares it. Looked up rather than
	stripped by pattern, the same reasoning as
	`zawin2frappe.loaders.frappe_sink._load_departments`.
	"""
	if not names:
		return {}
	return {
		row.name: row.department_name or row.name
		for row in frappe.get_all(
			"Department",
			filters={"name": ["in", list(names)]},
			fields=["name", "department_name"],
		)
	}


def shift_type_order() -> list[str]:
	"""Every Shift Type in scope anywhere, in the order the day runs.

	`start_time` is what makes the morning table print above the afternoon one
	without anybody declaring it. Shift Types that share a start time — or record
	none — fall back to name order so the chart is stable between rebuilds.
	"""
	in_scope = {
		row.shift_type
		for row in frappe.get_all(
			"Discipline Branch Config Shift Type",
			fields=["shift_type"],
		)
		if row.shift_type
	}
	if not in_scope:
		return []
	rows = frappe.get_all(
		"Shift Type",
		filters={"name": ["in", list(in_scope)]},
		fields=["name", "start_time"],
	)
	return [row.name for row in sorted(rows, key=lambda r: (r.start_time or _NO_START_TIME, r.name))]


def _config_shift_types() -> dict[str, set[str]]:
	"""Discipline Branch Config name -> the Shift Types it puts in scope."""
	out: dict[str, set[str]] = {}
	for row in frappe.get_all(
		"Discipline Branch Config Shift Type",
		fields=["parent", "shift_type"],
	):
		if row.shift_type:
			out.setdefault(row.parent, set()).add(row.shift_type)
	return out


def derive() -> Layout:
	"""Build the chart's layout out of the site's configuration."""
	configs = frappe.get_all(
		"Discipline Branch Config",
		fields=["name", "discipline", "branch", "rooms_num"],
	)
	roles = frappe.get_all(
		"Scheduling Role",
		filters={"active": 1},
		fields=["name", "role_name", "discipline", "max_rooms"],
	)
	by_discipline: dict[str, list] = {}
	for role in roles:
		by_discipline.setdefault(role.discipline, []).append(role)
	# max_rooms descending, then name. See the module docstring: this is the only
	# ordering judgement in the derivation and it is made out of the site's data.
	for group in by_discipline.values():
		group.sort(key=lambda r: (-(r.max_rooms or 0), r.name))

	labels = _department_labels({c.discipline for c in configs if c.discipline})
	scoped = _config_shift_types()

	bands = []
	for config in sorted(configs, key=lambda c: (c.branch or "", c.discipline or "")):
		lanes = tuple(
			Lane(role.name, role.role_name or role.name) for role in by_discipline.get(config.discipline, [])
		)
		bands.append(
			Band(
				key=config.name,
				branch=config.branch,
				discipline=config.discipline,
				branch_label=config.branch or "",
				discipline_label=labels.get(config.discipline, config.discipline or ""),
				rooms=int(config.rooms_num or 0),
				lanes=lanes,
				shift_types=frozenset(scoped.get(config.name, set())),
			)
		)

	sections = tuple(Section(name, name) for name in shift_type_order())
	return Layout(sections=sections, bands=tuple(bands), overflow_label=OVERFLOW_LABEL)


def unconfigured_disciplines() -> list[str]:
	"""Disciplines a Scheduling Role names but no Discipline Branch Config covers.

	Nobody holding one of those roles can be placed, so they land under
	`OVERFLOW_LABEL`. Reporting it next to the chart turns "why is this person in
	Unplaced" into a configuration task instead of a mystery.
	"""
	configured = {row.discipline for row in frappe.get_all("Discipline Branch Config", fields=["discipline"])}
	named = {
		row.discipline
		for row in frappe.get_all("Scheduling Role", filters={"active": 1}, fields=["discipline"])
		if row.discipline
	}
	missing = named - configured
	return sorted(_department_labels(missing).get(name, name) for name in missing)
