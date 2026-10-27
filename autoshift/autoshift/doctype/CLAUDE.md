# Doctypes (`autoshift/autoshift/doctype/`)

**DocType JSON files may be edited directly**, but must stay valid Frappe schema — keep
`field_order` in sync with `fields` and `name` matching the directory. Prefer round-tripping
through the Desk UI in developer mode (`bench set-config developer_mode 1`) when a change is
fiddly, since saving there auto-exports a canonical file.

**Every practice-specific value is a record a user enters, never a constant in code.** A new
practice-specific value becomes a *field* on an existing config doctype; the value lives in
`cmdb_frappe`. Don't add a config doctype or seed data for one practice's setup.

---

## The run

**Optimizer Run** — the transactional doc. `Draft → Solving → Solved/Failed → Approved →
Committed`. Immutable once solved; re-running is `duplicate()` into a new Draft. Points at an
**Optimization Ruleset** (`ruleset`, required, default `"Standard Ruleset"`). `type` is
`Manual` / `Copy` / `Automatic`; Automatic (Studio previews) is hidden from the list by default.

**Optimizer Run Slot** — child; one row per assigned shift (the `x` variables that came back 1),
with `collateral` set on rows that are a duty worked on top of one. Counting rows therefore
double-counts a collateral duty's half-day: count distinct (employee, date, shift type) for
**presence**, as the statistics panel does. `rooms` is the rooms that assignment takes;
`room_index` the solved room number. 0 = unmeasured, and the wall chart falls back to
`max_rooms`.

**Optimizer Run Coverage** — child; the `active_rooms` counterpart. One row per (discipline,
branch, date, shift) with `staffed_rooms` vs `capacity`. **Zero-staffed rows are kept on
purpose** — an empty slot is the thing a planner needs to see. Pre-table runs fall back to
`_derive_coverage_matrix`.

**Leave Speculation** — child of Optimizer Run; treats a *pending* leave as approved, for
feasibility analysis only.

---

## The scan

**Horizon Scan** — a forward sweep: a ruleset, a start and end date, and a window length
(`mode`, default `1-week`), with one **Horizon Scan Window** row per window. Saving re-plans
the rows (`scanner.scan.plan`), keeping every window the new horizon still contains — so
pushing `end_date` out by a month costs only that month. `Draft → Scanning → Paused/Complete`;
`Stop` sets `cancel_requested`, which the sweep reads *between* windows. `reuse_cached` (on)
links an earlier run with the same `input_hash` instead of solving again; `window_time_limit`
is what one window may spend before the sweep leaves it `Timed Out` and moves on.

**Horizon Scan Window** — child; one window's run (`optimizer_run`, kept whatever the
outcome), its `status` (`Pending → Solving → Solved/Failed/Timed Out`, **the only progress
state a resume reads**), its worst `severity`, and `findings`: that window's per-day numbers as
one compact JSON blob (`scanner.horizon.dumps`). A blob, not a child table — a year-long scan
is ~52 rows where a row per (day, discipline, branch, shift) would be thousands — and raw
numbers, because which capacity a shortfall is measured against is the reader's question,
applied on read. See `autoshift/scanner/CLAUDE.md`.

---

## Rules

**Optimization Rule** — one MILP constraint or objective term as a document: an NL
`description` plus an optional implementation. `implementation_type` is `Not Implemented`
(description-only backlog item), `Built-in` (`builtin_key` into the `optimizer/rules.py`
registry) or `Custom Code` (Python defining `apply(ctx)`, run only after a developer checks
`validated`; editing the code clears the flag). `rule_kind` is synced from the registry for
built-ins, developer-declared for custom code.

**Custom code executes at solve time**, so the implementation fields (`implementation_type`,
`builtin_key`, `implementation_code`, `validated`, `rule_kind`) are **permlevel 1** (System
Manager write only); HR Manager may edit name + NL description only. The controller overrides
`validate_higher_perm_levels` — Frappe runs it *before* `validate` and silently resets
permlevel-protected fields — to also warn/clean non-developer implementation edits and
hard-refuse `validated` flips.

`optimization_rule.js` upgrades the code editor: domain completions from the whitelisted
`get_code_completions` (backed by `optimizer/editor_support.completion_items()`, introspected
from RuleContext/DataPackage) via the Code control's `autocompletions` hook, plus inline Ruff
lint (WASM webworker) through the `ace-linters` + `ace-python-ruff-linter` npm deps,
self-hosted at `/assets/autoshift/node_modules/…` by `bench build`'s node_modules symlink.

**Optimization Ruleset** (+ child **Optimization Ruleset Rule**) — a reusable bundle of rules
(compiling/validating rules is slow; rulesets are not). Each row's `weight` scales that rule's
objective contribution (no-op on constraint rules; save warns). Unimplemented rules may be
drafted in (save warns) but `data_loader._load_rules` throws at solve time. A ruleset with no
Objective/Mixed rule warns but solves (constant-0 objective = feasibility check). `validate()`
runs `BuiltinRule.check_ruleset` over the row set's built-in keys (Custom Code rows skipped, as
in `apply_rules`), so a missing `requires` or two rules sharing a choice `group` is a save-time
throw, not only a solve-time one.

**Scheduling Rule Topic** — an optional heading a rule files itself under, so Studio's toggle
panel renders as collapsible sections. Built-ins declare theirs in `rules.py` (`TOPIC_*`,
`TOPIC_ORDER`); the seeding re-syncs the topic docs (`is_system=1`) and each built-in's `topic`
on every migrate. Hand-authored topics (`is_system=0`) are never touched. Orthogonal to
`BuiltinRule.group` — see design notes.

**Seeding.** `patches.create_standard_optimization_rules.execute()` creates one rule doc per
built-in, keeps the Standard Ruleset's rows in sync with the registry, and backfills `ruleset`
on pre-existing runs. It runs from `after_install` **and unconditionally from `after_migrate`**
— see "Working conventions" in the root `CLAUDE.md` for why that needs no per-rule patch.

---

## Configuration

**Discipline Branch Config** (+ child **… Shift Type**) — per (discipline, branch). A Shift
Type on no config row is treated as non-clinical and excluded.

| Field | Meaning |
|---|---|
| `rooms_num` | treatment rooms at this branch for this discipline |
| `shift_types` | which Shift Types the optimizer schedules here |
| `room_value` (Float, default 3) | prices a staffed room here in objective points; read by `room_value_objective` in place of `room_utilization_objective`'s flat weight. 0 leaves the room-level value mechanism off. **Existing rows are not backfilled on migrate** — the field is inert until a row is deliberately configured |
| `barebones` (Check, off) | this discipline staffs its rooms here and nothing beside them: `barebones_branches` forbids every shift here in a role that **gates no room**, whatever mode it is worked in. Per (discipline, branch), so a branch can be barebones in one discipline and fully staffed in another. Gating roles are out of scope — closing the rooms themselves is `rooms_num = 0`. Inert in a ruleset without the rule, and the wall chart still draws the suppressed lanes, empty |
| `display_order_key` (Int) | wall-chart band tiebreak, read *after* the band's role-derived key — see wallchart notes |

**Scheduling Role** — the optimizer's unit of *capability* and the scheduling axis that
replaced designation. Names exactly one discipline (Link to `Department`).

| Field | Meaning |
|---|---|
| `max_rooms` (Int, 1) | rooms one holder covers simultaneously in a slot |
| `assignment_mode` (Select, `Flexible`) | how a shift in the role is worked: `Flexible` (a rota slot means "present", and any non-exclusive role the holder has may fill it), `Exclusive` (that role, nothing beside it, no swapping out) or `Collateral` (a duty on top of another shift at the same shift and branch, or on its own) |
| `gates_rooms` (Check, **on**, "Required To Staff A Room") | whether room coverage waits on the role — the minimum in `room_coverage` is taken over gating roles only, so a floater or a lead duty can be worked without holding every room in the discipline shut. Orthogonal to the mode (save warns on a gating collateral role, which means no room opens without one) |
| `assignments_binding` (Check, off) | a role whose **presence** is settled by its holders |
| `assignment_value` (Float, 0) | prices one shift in the role, over and above the rooms it staffs — a small positive figure on a standby role is what makes the optimizer place somebody it has no room for; negative makes the role a last resort. Read only by `role_value_objective` |
| `display_order_key` (Int, 0) | orders wall-chart lanes, and breaks ties when a role has to be inferred |
| `chip_sort_field` / `chip_sort_descending` | an Employee fieldname ordering the people *within* a lane (default alphabetical by initials), so the row ordinal can carry a meaning like seniority |
| `wallchart_abbreviation` | overrides the lane header; blank derives initials from the role name |
| `highlight_by_default` (Check, off) | seeds the wall chart's "Highlight" picker |
| `active` (Check, on), `description` | |

**Employee Scheduling Role** — the employee × role relation. A **standalone doctype, not a
child table**, so `zawin2frappe` can import into it directly.

| Field | Meaning |
|---|---|
| `role_fte` | the *informally* agreed FTE % in that role; blank = no expectation |
| `max_rooms` | optional override of the role's figure |
| `binding_override` (Select) | blank inherits the role's flag |
| `assignment_mode_override` (Select) | blank inherits the role's mode |
| `suitability` (Float ≥ 1, default 1) | 1 = regular holder, 1.2 = good backup, 3 = terrible but feasible substitute. A substitute is just a row with `suitability > 1`; the loader treats it as held like any other |
| `value_multiplier` (Float, 1) | scales this holder's share of a staffed room's value — `employee_value_objective` adds `(-1 + multiplier) * room_value(discipline, branch)` per room they are matched into, flat and never compounding, so a site leaving every multiplier at 1 hashes and solves as before |
| `active`, `valid_from` / `valid_to` | |

**An employee holding no in-window role is not scheduled at all** — that is how non-clinical
staff stay out of scope.

**Employee Role Synergy** — a **standalone, symmetric employee × employee** relation, autonamed
`{employee_a}-{employee_b}` with the pair sorted alphabetically in `before_insert` so either
order collides on one document. `synergy_multiplier` (Float, 1) prices two employees matched
into the same room together, read the same flat, non-compounding way as `value_multiplier`:
`synergy_value_objective` adds `(-1 + multiplier) * room_value(discipline, branch)` per room
both occupy, on top of the room's base value and any employee-value bonus already earned there.
Edited densely via the **Synergy Matrix** page.

**Employee Settings** (+ children **Employee Shift Preference**, **Employee Branch
Preference**) — per-employee preference overrides. Branch preferences are stored but **not read
by the solver**.

**Optimizer Settings** — singleton, **now fieldless**. Its two holiday lists were the app's own
calendar; the optimizer reads Frappe HR's `Holiday List Assignment` instead. Kept as the place
a future global setting would land.

**Bulk Employee Settings** — tool doctype; batch-creates **Employee Settings** and **Employee
Scheduling Role** from one role-based filtered employee list. Sync ≤30 employees, async +
realtime above that (falling back to inline when no worker is alive). An employee who already
has the record is **skipped, never overwritten**.

**Rota Edit Draft** (+ child **Rota Edit Draft Change**) — one user's staged, unapplied edits to
one discipline's bound rotas. Autonamed from (user, discipline) so staging always finds the same
document. See `autoshift/rota/CLAUDE.md`.

**Collateral Scheduling Role** — child doctype behind the `custom_collateral_roles` Table
MultiSelect on `Shift Assignment` and `Shift Schedule Assignment`.
