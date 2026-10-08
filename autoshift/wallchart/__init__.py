# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""The week wall chart: rooms down the page, days across, one glance.

Autoshift's roster grid is per-employee, which answers "what did this person
get" and cannot answer "is Tuesday morning covered" — coverage is a fact about
rooms, and rooms are not one of that grid's axes. This package is the other
view, and it is the default one: always on, on a run in any state, falling back
to the Shift Assignments already on the books when there is nothing solved to
show.

    chart.py    Frappe-free. The dataclasses, and where each slot lands.
    layout.py   The chart's shape, derived from Discipline Branch Config +
                Scheduling Role rather than declared in a layout file.
    source.py   A week of slots, out of Shift Assignment and/or an Optimizer Run.
    api.py      `get_week_chart`, the whitelisted payload the page draws.

Generalized from the hand-authored wall chart in `cmdb_frappe/planning/`, which
stays where it is: that sheet's bands, its tandem columns and its numbered
chairs are one practice's paper, and the App boundary in CLAUDE.md keeps them
out of here. What autoshift ships is the same idea with every band, row and
column read out of the configuration instead.
"""
