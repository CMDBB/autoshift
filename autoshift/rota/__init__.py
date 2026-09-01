# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

"""Settled schedules read from HRMS's own `Shift Schedule`, and made real.

## Why this exists at all

A practitioner whose week is settled is described by a **rule**, and stock HR has
somewhere to put a rule: a `Shift Schedule` (a shift type, a frequency, the
weekdays it falls on) plus a `Shift Schedule Assignment` joining it to a person.
zawin2frappe writes those. HRMS's nightly `process_auto_shift_creation` is then
supposed to turn them into the `Shift Assignment` records everything downstream
reads — this app's `bind_role_assignments` rule and the wall chart included.

It cannot, for anything longer than a weekly cycle:

- **`Every N Weeks` re-anchors itself.** `create_shifts` takes its week boundary
  from `create_shifts_after`, and `create_individual_assignment` then overwrites
  that field with the last *shift's* end date rather than the end of a week. One
  long call is correct; the nightly job resumes mid-pattern, the boundary moves,
  and the cycle collapses toward weekly. Measured by zawin2frappe over twelve
  weeks in thirty-day chunks, `Every 4 Weeks` fired on weeks 0, 4, 4, 5, 8, 9,
  10, 11, 12.
- So zawin2frappe emits a rota **disabled, `Inactive` and tagged
  `DO NOT ENABLE`**, as the faithful shape of a real pattern that must not be
  switched on as it stands.

That leaves the people whose schedule is *least* the planner's to set with no
`Shift Assignment` records at all for any week the import did not already cover
— and `bind_role_assignments` freezes a bound pair against exactly those
records, so an empty week freezes them to nothing rather than to their week.

## What this package does about it

It expands a Shift Schedule over a date span itself (`cycle.occurrences`, a
corrected re-implementation of `create_shifts`) and creates the missing
`Shift Assignment` records on demand — when the wall chart lands on a week that
has none, and before a run solves a horizon that has none.

**This is a workaround for someone else's bug and is signposted as one.** The
upstream issue is not in active development, which is what makes it worth
carrying; the day `create_shifts` anchors its weeks properly, everything here is
deleted and `enabled = 1` on the Shift Schedule Assignments does the same job.

Two consequences of that stance, both deliberate:

- `Shift Schedule Assignment.enabled` and `.shift_status` are **ignored**. They
  are HRMS's switches for HRMS's generator, and a rota is off precisely because
  that generator would run it wrongly. Generated assignments are always `Active`
  — they are shifts the person genuinely works — and link back via
  `Shift Assignment.shift_schedule_assignment`, so the provenance is on the record.
- `create_shifts_after` is **never written**. It is the phase anchor as well as
  the handover boundary, and moving it is the upstream bug. Idempotency comes
  from checking what is already on the books instead, which needs no state.

Nothing here is practice-specific: which roles are binding is
`Scheduling Role.assignments_binding`, site data (see CLAUDE.md, "App boundary").
"""

from .cycle import FREQUENCY_WEEKS, WEEKDAY_INDEX, Rota, monday_of, occurrences

__all__ = ["FREQUENCY_WEEKS", "WEEKDAY_INDEX", "Rota", "monday_of", "occurrences"]
