# Settled schedules (`autoshift/rota/`)

Materialises `Shift Schedule` / `Shift Schedule Assignment` patterns into the `Shift
Assignment` records everything else reads, because **HRMS's own nightly job mis-phases any
cycle longer than a week**. `create_shifts` takes its week boundary from `create_shifts_after`
and then overwrites that field, so the job resumes mid-pattern and the cycle collapses toward
weekly.

**This is a workaround for an upstream bug and is signposted as one throughout.** The day
`create_shifts` anchors its weeks properly, this package is deleted and `enabled = 1` does the
same job.

Same Frappe-free/DB split as the optimizer:

| Frappe-free (tested) | DB half |
|---|---|
| `cycle.py` — `tests/test_rota.py` | `materialize.py` |
| `edit.py` — `tests/test_rota_edit.py` | `editor.py` |
| `calendar.py` — `tests/test_holidays.py` | `holidays.py` |

---

## `cycle.py` — the arithmetic

`Rota` + `occurrences`: weekdays in `repeat_on_days`, one week in every `cycle_weeks`, counting
from the week after `create_shifts_after`, stopping after `custom_create_shifts_until`. Weeks
are **ISO (Monday-based)**.

**A rota is an interval** — `create_shifts_after` is written once and never moved, in either
direction, because it carries the *phase* as well as the start.
`backdated_anchor` / `anchor_cutoff` / `ANCHOR_LEAD_WEEKS` are **historical**: nothing calls
them on new data. They survive only because `backdate_rota_anchors` already ran with them (on
`enabled = 0` rows only — HRMS would back-fill enabled ones). Those anchors are **not**
restored; everything before today is one epoch.

## `materialize.py` — creating the records

`pending` / `materialize`. Records carry the rota's own `custom_scheduling_role` and
`custom_collateral_roles`, which is why it builds the `Shift Assignment` itself rather than
calling HRMS's `create_shift_assignment`: the role has to be set before submit.

- **`create_shifts_after` is never written here.** Idempotency comes from comparing against the
  books.
- `enabled` / `shift_status` are ignored — they are HRMS's switches for HRMS's generator, and a
  rota is off precisely because that generator would run it wrongly. Records are created
  `Active` and link back via `Shift Assignment.shift_schedule_assignment`.
- Coverage is keyed on `(employee, date)` or `(employee, date, shift_type)` per
  `HR Settings.allow_multiple_shift_assignments`.
- One record per day, one savepoint per row; failures are collected and reported, never fatal.
- `RoleContext` + `types.resolve_assignment_role` fill a blank role in inside `load_rotas`, and
  `Rota.discipline` follows from it (falling back to `Shift Location.custom_discipline`).
  `settled_rows` passes its horizon so the window matches `data_loader`'s `employee_roles`.

**A solve never creates records.** `data_loader.load` reads `materialize.settled_rows` — each
day a bound employee's rota puts them on that no submitted record covers — straight into
`forced`, as if it were on the books, so the package **and its `input_hash`** are the same
whether or not those records exist. Churning submitted Shift Assignments (and their
notifications) ahead of every preview is what this replaced. Records are written only by the
wall chart's on-demand "Create them" (`materialize_between`, via
`autoshift/public/js/rota.js`), and in future by `committer.py`.

---

## Per-employee holiday lists (`calendar.py` + `holidays.py`)

Frappe HR counts leave by subtracting the employee's *own* holidays
(`get_number_of_leave_days` → `get_holidays` → `Holiday List Assignment`, which ignores
`Employee.holiday_list` entirely), so a settled four-day week must be written down as a holiday
list or a week off costs five days.

`derive` = the company's assigned calendar (verbatim — an assignment of one's own kills the
company fallback) ∪ every unworked day of the window; `worked` is rotas expanded plus what the
books already record.

- **Bound employees only** (nobody else has a pattern to derive from); **whole days only**
  (`Holiday.is_half_day` exists but `get_holidays` counts dates, so a morning-only week is
  inexpressible — a dead end, not a gap).
- Lists are named by content digest and shared; the window is the base list's own.
- Succession needs no end date — HLA is latest-`from_date`-wins — and mints a new list +
  assignment from `first_difference`.
- **On demand only**: the Rota Editor's **Update Holiday Lists…** menu item, or
  `bench update-holiday-lists [--dry-run]`. It submits HR documents, so it is never a side
  effect of applying a rota edit.

**Partly read back.** `holidays.calendars` — read by `optimizer.data_loader._availability` and
by the wall chart's `source._not_working` — takes these lists' **dated** holidays and the
**company's** weekly offs, never the rota-derived days off, which an employee's own list
carries as *weekly* offs. Those lag a rota edit, and which days a bound employee works is a
ruleset choice (`role_binding`), not a deleted variable. Only the *policy* differs between the
two callers: a solve throws on a company with no resolvable calendar; the chart warns and
carries on.

---

## Hand-editing (`edit.py` + `editor.py`)

The **Rota Editor** Desk page lets a planner drag a bound employee's shifts to a different day,
shift type or branch within their own discipline, add one, or right-click a chip to change the
**Scheduling Role** it is worked in (`retag`). Edits stage into a `Rota Edit Draft`
(server-side, survives a reload) and apply as one `EditPlan`.

**Silver and gold.** An imported pattern is **silver standard**,
`Shift Schedule Assignment.custom_unconfirmed = 1` (set by the importer); unflagged is gold.
**zawin2frappe's import may only overwrite a flagged row** (enforced there, not here). Editing
a pattern replaces it wholesale with a fresh, unflagged `Shift Schedule Assignment` (+ a private
`Shift Schedule`, tagged `Shift Schedule.custom_manually_edited`, which now only means "the
editor owns this and may delete it"). A shared zawin2frappe-owned `Shift Schedule` is only
unlinked, never edited or deleted; a private schedule an edit empties is cancelled and deleted.
Created assignments are `enabled = 0` / `shift_status = "Inactive"` — `materialize.py` stays the
sole generator. **Promote all** (per employee) stages a `promote` `Change`; `EditPlan.promote`
lists that employee's silver assignments not already being replaced, and Apply clears the flag
in place. Silver chips draw with a grey dotted border (`re-chip-unconfirmed`).

**A superseded pattern is ended, not deleted.** The turning point is the first day of the view
the edit was made in: every source row already running then gets `custom_create_shifts_until =
view_start - 1` (`EditPlan.terminate`) and its replacement starts on `view_start`; a row that
had not begun is deleted (`EditPlan.delete`), having recorded nothing. `edit._runs_before`
decides which, off the rota's own arithmetic rather than the books. Editing a past window
genuinely rewrites from that date — `EditPlan.retroactive` drives a warning in the Apply dialog
and an `apply_draft` msgprint, not a refusal. `EditPlan.successions` states each ending in the
transcript ("Pattern changes"), recomputed like `cadence_changes` and never stored. Deriving the
end from the successor's `create_shifts_after` instead does **not** work — see design notes.

**The role is part of a pattern's identity, and a `move` never changes it.** `edit.group_key` is
`(employee, shift_type, branch, scheduling_role, collateral_roles)` —
`custom_scheduling_role` is single-valued, so two differently-roled half-days *cannot* share a
document. `|SSA|` therefore scales with the role and duty set; that is intended.
`Rota.scheduling_role` / `.collateral_roles` come off the Shift Schedule Assignment (blank ones
resolved on read) and are carried onto the `NewAssignment` that replaces a pattern. An `add` has
nothing to inherit, so its `Change` carries a role the way it already carries a company
(`editor._default_role`: the one non-collateral role held in that discipline, else the single
binding one; the page prompts only when neither settles it). A `retag` is the one edit that
*does* change it — mechanically a move between two groups differing only in role, per
occurrence like everything else here.

**Periodicity is derived, not identity.** Cadence and anchor stay *out* of `group_key`;
`edit.minimal_cycle` reads the cadence back out, which is how a rota's periodicity changes with
no explicit action. Emitted as `EditPlan.cadence_changes`, surfaced as **Periodicity changes**,
recomputed fresh on every `get_state` / `stage_change` — never stored.

**A rota is editable at a given view width exactly when `view_weeks % cycle_weeks == 0`**;
otherwise the employee is drawn as a greyed-out read-only row showing `edit.phase_fractions`
(`"1/4"`, `"2/4"`…). Editable chips badge their own cadence (`"2w"`) once `cycle_weeks > 1`.

**A pattern starting after the window on screen is offered, not hidden.** `get_state`'s
`upcoming_patterns` lists them (grouped by `group_key`, skipped where the view is too narrow for
the cadence) and the page stages a `backdate` change to start one from this view. The one op
that touches no occurrence: `apply_changes` resamples the group over its own future weeks,
shifted back by whole weeks so phases keep their offsets, and the future rows are deleted (they
never ran).

**Other mechanics.**

- `edit.Change` stages one edit at single-occurrence granularity
  (`add` / `move` / `remove` / `retag` / `backdate`; `promote` is per employee), identified by
  weekday **and** `from_phase` / `to_phase`. `apply_changes(rotas, changes, view_start,
  view_weeks) -> EditPlan` folds a batch onto the current `Rota`s.
- `apply_draft(discipline, start, view_weeks)` takes the view explicitly from the toolbar rather
  than persisting one, and clears the draft's rows *before* deleting the assignments they
  reference (a live Link blocks the delete otherwise).
- `editor.py` translates between the **Branch** vocabulary the grid uses and the raw
  `Shift Location` docname `Shift Schedule Assignment.shift_location` stores
  (`_rotas_by_branch` / `_shift_location_for`, via `Shift Location.custom_branch`). Room-level
  assignment doesn't exist yet, so any Shift Location matching the target branch is as good as
  another.
- Only *this* discipline's patterns are editable (`editor.is_native`, off `Rota.discipline`).
  Another discipline's are drawn faint/dashed/read-only as `foreign_cells` and go **red on both
  sides** where they land on the same half-day (`_mark_clashes`, keyed on the date alone unless
  `HR Settings.allow_multiple_shift_assignments`). Shift Types outside the discipline's config
  become read-only `extra_shift_types` sections.
- `editor.unconfirmed_rotas` backs the solve-time warning and the editor's own banner
  (`get_state`'s `unconfirmed_employees`, counted on the draft-folded rotas).
