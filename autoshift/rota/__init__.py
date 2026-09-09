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

## Hand-editing a rota (`edit.py` + `editor.py`)

The detection above is a read-proxy for whatever zawin2frappe emitted, and that
detection is imperfect in practice — a schedule that has since changed, one
zawin2frappe never saw. `edit.py` (pure) and `editor.py` (the DB half, same split as
`cycle.py`/`materialize.py`) back a Rota Editor page that lets a planner drag a bound
employee's shifts around directly: same discipline, but freely across shift type, day
and branch.

A hand edit is **gold standard** — it is the planner correcting the record, not a guess
— so it is never expressed as a patch on top of the detected schedule. Editing an
assignment always replaces it wholesale with a fresh `Shift Schedule Assignment` (+ a
private `Shift Schedule` backing it), tagged `custom_manually_edited`. That tag is the
whole mechanism for keeping this app and zawin2frappe from fighting over the same
record: **zawin2frappe's import must skip any row already carrying it** rather than
overwriting a planner's correction on the next re-run. (That check lives in
zawin2frappe, not here — this app only sets the tag and never touches a schedule that
doesn't carry it, so a shared, zawin2frappe-owned `Shift Schedule` is never edited or
deleted, only unlinked by removing the one `Shift Schedule Assignment` row that pointed
at it.)

A created assignment is `enabled = 0` / `shift_status = "Inactive"`, exactly like an
imported one — HRMS's nightly generator staying off it is the entire point regardless of
who authored the pattern, so a hand edit gets no different treatment there than an
import does.

### Multi-week rotas

A `Rota`'s weekday set does not vary from one cycle to the next — `cycle_weeks` only
means "skip N-1 weeks between occurrences", never "different weekdays in week 2 than in
week 1". A genuinely varying multi-week pattern is several `Shift Schedule Assignment`s
sharing a cadence at different phases (anchors), each with its own fixed weekday set —
the shape zawin2frappe actually emits (see "Rota" above).

That makes the editor's view-width question a pure visibility predicate
(`edit.rota_view_weeks`), not a rendering trick: given a view `view_weeks` wide, a rota
of cadence `cycle_weeks` is shown, tiled by `cycle.occurrences` like anything else,
exactly when `view_weeks % cycle_weeks == 0`. A weekly rota therefore renders
identically in every week of a wider view with no special-casing — the view is just
wide enough to show it more than once. A four-week rota shown in a one-week view would
show only whichever single phase that week happens to be — not its actual pattern, and
indistinguishable from "this person works two days a week" — so instead of drawing that,
the employee is **filtered out of that view** with the reason stated; switching to a
4-week view (or any width divisible by 4) brings them back with every phase visible at
once, editable as what it is. An employee with more than one cadence among their own
rotas (rare) is filtered by the narrowest of them, since showing half a person's rota is
no better than showing none of it.
"""

from .cycle import FREQUENCY_WEEKS, WEEKDAY_INDEX, Rota, monday_of, occurrences

__all__ = ["FREQUENCY_WEEKS", "WEEKDAY_INDEX", "Rota", "monday_of", "occurrences"]
