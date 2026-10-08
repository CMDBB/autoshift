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
      .max_rooms                  how many rows a chip in that lane covers
      .gates_rooms                whether a room waits on that lane being filled
    Shift Type                    one stacked table each, ordered by start time

So there is no layout file and nothing to keep in sync: configure a discipline
at a branch and its band appears; add a Scheduling Role and its column appears,
empty until somebody covers it. That emptiness is the point — the chart's job is
to show what the configuration says *should* be staffed next to what actually
is.

The first judgement here is lane order, and it is `Scheduling Role`'s optional
`display_order_key` first, then `max_rooms` descending, then name — see
`lane_sort_key`. Left to itself it is a property of the site's own data, not a
fact this package asserts about anybody's job; where the derived order reads
wrong, the key is the site's way of saying so.

Band order follows the same reasoning, one level up: the minimum
`display_order_key` among a discipline's roles leads, so a band holding a
keyed role sorts by it. But two bands can both hold nothing but unkeyed
roles (every role at its default 0), and alphabetic order of a bare
discipline/branch pair is not always what a site wants either — so
`Discipline Branch Config` carries its own `display_order_key`, read only as
the tiebreak between bands the role-based key cannot separate. Branch then
discipline name is the last resort, kept only so the chart is stable between
rebuilds.

The one thing here that is *not* derived from the configuration is the reader's
own filter: `derive(disciplines, branches)` narrows the bands, and
`filter_options()` lists what there is to narrow to. It belongs in this module
rather than in `api.py` because dropping a band also drops the sections only that
band appeared in, and nothing outside here knows which those are.

A third, narrower judgement is row order *within* a lane's day — see
`Scheduling Role.chip_sort_field` and `chart._order_lane`. `derive()` only
carries the direction (`chip_sort_descending`) onto each `Lane`; resolving the
field itself into a value per employee is `source.py`'s job, since it is the
only module that reads Employee.
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


def shift_type_order(in_scope: set[str] | None = None) -> list[str]:
	"""Every Shift Type in scope anywhere, in the order the day runs.

	`start_time` is what makes the morning table print above the afternoon one
	without anybody declaring it. Shift Types that share a start time — or record
	none — fall back to name order so the chart is stable between rebuilds.

	`in_scope` narrows it to the Shift Types a given set of configs puts in scope,
	which is how a filtered chart drops a table no surviving band appears in
	instead of printing it empty. Omitted, every configured one is in scope.
	"""
	if in_scope is None:
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


#: Fields `lane_sort_key` reads. Kept next to it so a caller cannot select too
#: few and get a silently different order.
ROLE_ORDER_FIELDS = ["name", "display_order_key", "max_rooms"]


def lane_sort_key(role) -> tuple[int, int, str]:
	"""Where a Scheduling Role's column sits within its band.

	`display_order_key` leads: an optional hand-set position, defaulting to 0, so
	a negative value pulls a role ahead of every role nobody has ordered. Then
	`max_rooms` descending — a role whose holder covers more rooms sorts left,
	which puts a practitioner ahead of an assistant wherever that is how the
	numbers happen to fall. Then name, so the chart is stable between rebuilds.

	`source.infer_role` breaks its ties on the same key, so a Shift Assignment
	whose role cannot be determined lands in the leftmost lane the employee could
	plausibly have worked rather than an arbitrary one.
	"""
	return (int(role.display_order_key or 0), -(role.max_rooms or 0), role.name)


def role_order() -> dict[str, tuple[int, int, str]]:
	"""Every active role's lane sort key, by role name."""
	return {
		role.name: lane_sort_key(role)
		for role in frappe.get_all("Scheduling Role", filters={"active": 1}, fields=ROLE_ORDER_FIELDS)
	}


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


#: How many names an option's subtitle lists before it gives up and counts.
_SUMMARY_CAP = 4


def _summarize(names: list[str], rooms: int) -> str:
	"""An option's subtitle: what selecting it actually puts on the chart.

	The counterpart axis plus the rooms it comes to — a discipline lists the
	branches it runs at, a branch the disciplines in it — because "which of these
	do I want" is a question about bands, and a band is a (discipline, branch)
	pair. Long lists are counted rather than printed: a filter picker that needs
	scrolling to read one line is worse than one that says "+3".

	Rooms are the chart's own unit, so a total is the most useful single number
	here; omitted at 0, which is a `Discipline Branch Config` nobody has given a
	room count rather than a real answer.
	"""
	shown = " · ".join(names[:_SUMMARY_CAP])
	if len(names) > _SUMMARY_CAP:
		shown += f" · +{len(names) - _SUMMARY_CAP}"
	if not rooms:
		return shown
	counted = f"{rooms} room" if rooms == 1 else f"{rooms} rooms"
	return f"{shown} — {counted}" if shown else counted


def filter_options() -> dict:
	"""Every discipline and branch the chart could draw, for the filter pickers.

	Read off `Discipline Branch Config` rather than off Department and Branch
	wholesale: a discipline nothing is configured for has no band to show or hide,
	so offering it would only be a way of filtering to an empty chart. Unfiltered
	by design — the picker has to keep offering what the current selection hid.

	Every option carries a `description`, and must: `MultiSelectList` interpolates
	that field straight into each row's subtitle and only defaults it for *string*
	options, so an object option without one renders the word "undefined".
	"""
	configs = frappe.get_all("Discipline Branch Config", fields=["discipline", "branch", "rooms_num"])
	names = {config.discipline for config in configs if config.discipline}
	labels = _department_labels(names)

	#: discipline -> its branches, and branch -> its disciplines: each option's
	#: subtitle is the other axis of the band it names.
	branches_of: dict[str, set[str]] = {}
	disciplines_of: dict[str, set[str]] = {}
	discipline_rooms: dict[str, int] = {}
	branch_rooms: dict[str, int] = {}
	for config in configs:
		rooms = int(config.rooms_num or 0)
		if config.discipline:
			discipline_rooms[config.discipline] = discipline_rooms.get(config.discipline, 0) + rooms
			if config.branch:
				branches_of.setdefault(config.discipline, set()).add(config.branch)
		if config.branch:
			branch_rooms[config.branch] = branch_rooms.get(config.branch, 0) + rooms
			if config.discipline:
				disciplines_of.setdefault(config.branch, set()).add(config.discipline)

	return {
		"disciplines": [
			{
				"value": name,
				"label": labels.get(name, name),
				"description": _summarize(sorted(branches_of.get(name, ())), discipline_rooms.get(name, 0)),
			}
			for name in sorted(names, key=lambda n: labels.get(n, n))
		],
		"branches": [
			{
				"value": branch,
				"label": branch,
				"description": _summarize(
					sorted(labels.get(d, d) for d in disciplines_of.get(branch, ())),
					branch_rooms.get(branch, 0),
				),
			}
			for branch in sorted(branch_rooms)
		],
	}


def derive(disciplines: list[str] | None = None, branches: list[str] | None = None) -> Layout:
	"""Build the chart's layout out of the site's configuration.

	`disciplines` and `branches` narrow it to the bands a reader asked for. They
	are the one thing in this package that is a view preference rather than a fact
	about the configuration, and an empty or absent list means *every* one of them
	rather than none — so the unfiltered chart stays the default and a picker
	nobody has touched is not a chart with nothing in it.

	`sections` follow the surviving configs, so filtering down to a
	mornings-only discipline drops the afternoon table instead of printing it
	empty.
	"""
	configs = frappe.get_all(
		"Discipline Branch Config",
		fields=["name", "discipline", "branch", "rooms_num", "display_order_key"],
	)
	if disciplines:
		wanted = set(disciplines)
		configs = [config for config in configs if config.discipline in wanted]
	if branches:
		wanted = set(branches)
		configs = [config for config in configs if config.branch in wanted]
	roles = frappe.get_all(
		"Scheduling Role",
		filters={"active": 1},
		fields=[*ROLE_ORDER_FIELDS, "role_name", "discipline", "chip_sort_descending", "gates_rooms"],
	)
	by_discipline: dict[str, list] = {}
	for role in roles:
		by_discipline.setdefault(role.discipline, []).append(role)
	for group in by_discipline.values():
		group.sort(key=lane_sort_key)

	labels = _department_labels({c.discipline for c in configs if c.discipline})
	scoped = _config_shift_types()
	min_keys = {
		c.name: min(
			d.get("display_order_key")
			for d in frappe.get_all(
				"Scheduling Role",
				filters={"active": 1, "discipline": c.discipline},
				fields=["display_order_key"],
			)
		)
		for c in configs
	}

	bands = []
	for config in sorted(
		configs,
		key=lambda c: (min_keys[c.name], int(c.display_order_key or 0), c.branch or "", c.discipline or ""),
	):
		lanes = tuple(
			Lane(
				role.name,
				role.role_name or role.name,
				bool(role.chip_sort_descending),
				bool(role.gates_rooms),
			)
			for role in by_discipline.get(config.discipline, [])
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

	in_scope = {shift_type for band in bands for shift_type in band.shift_types}
	sections = tuple(Section(name, name) for name in shift_type_order(in_scope))
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
