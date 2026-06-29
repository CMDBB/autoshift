# Autoshift

Frappe app (requires `frappe/hrms`) that schedules employee shifts for a multi-branch,
multi-discipline dental practice using Mixed Integer Linear Programming (PuLP + bundled
COIN-OR CBC solver). Reads Employee/Shift Type/Leave Application/Holiday List/Shift
Assignment from Frappe HR; combines with app-specific config; produces a schedule a user
reviews, approves, and commits into real `Shift Assignment` records.

**This is an early-stage, single-developer WIP**, not a finished product. Expect missing
features, incomplete migrations, and rough edges — see the lists below before assuming
something is "broken" vs. simply not built yet. 

**IMPORTANT**: Ask the user for clarification early and
often on any design intentions.

Design intent: `shift_optimizer_design.md`. User docs: `README.md`.

## Architecture

Doctypes (`autoshift/autoshift/doctype/`):
- **Optimizer Run** — main transactional doc. Lifecycle `Draft → Solving → Solved/Failed →
  Approved → Committed`. Immutable once solved; re-running = `duplicate()` into a new Draft.
- **Optimizer Run Slot** — child; one row per assigned shift in a solution.
- **Optimizer Settings** — singleton: `fte_tolerance_pct`, `turnover_weight`, holiday lists.
- **Discipline Designation Branch Config** — per (discipline, designation, branch): room
  counts + max-rooms-per-employee.
- **Employee Settings** (+ children `Employee Shift Preference`, `Employee Branch Preference`)
  — per-employee shift/branch preference overrides.
- **Leave Speculation** — child of Optimizer Run; treats a *pending* leave as approved for
  feasibility analysis only.
- **Bulk Employee Settings** — tool doctype, batch-creates Employee Settings (sync ≤30
  employees, async + realtime progress above that).

Optimizer engine (`autoshift/optimizer/`, pure-Python where possible for testability):
1. `types.py` — `DataPackage` dataclass (engine's only input shape), SHA256 `input_hash()`
   for caching, `planning_days()` (raises `NotImplementedError` for `"Unbounded"` mode).
2. `data_loader.py` — `load(run_doc)` hydrates a `DataPackage` from the Frappe DB; normalizes
   preferences via temperature-scaled softmax (no weight deviates >50% from uniform).
3. `model_builder.py` — builds the PuLP MILP. Vars: `x[employee,shift,day,branch]`,
   `active_rooms[discipline,shift,day,branch]`. Objective = `turnover_weight × room
   utilization + Σ preference·assignment`.
4. `solver.py` — runs CBC (5s sync, escalates to 3600s background job via
   `frappe.enqueue(queue="long")` on timeout); caches by input hash against prior runs in
   `{Solved, Failed, Approved, Committed}`.
5. `committer.py` — converts an Approved run into submitted `Shift Assignment` records.
   **The run→Shift-Assignment link-back is mid-redesign and not finished — see To Be
   Implemented.**

Custom fields on stock doctypes (`autoshift/fixtures/custom_field.json`):
`Shift Location.custom_discipline`, `Shift Location.custom_branch` (Link to `Branch` — source
of truth for a `Shift Assignment`'s branch via its `shift_location`), `Employee.custom_fte`.

`Discipline Designation Branch Config.shift_types` (Table MultiSelect, backed by the
`Discipline Designation Branch Config Shift Type` child doctype) determines which `Shift
Type`s are in scope for the optimizer — a Shift Type not listed on any config row is treated
as a non-clinical variant and excluded. This duplicates the shift-type list across every
(discipline, designation, branch) row rather than tagging Shift Type itself, so
`data_loader.py` warns (`frappe.log_error`) if rows sharing the same discipline list
different Shift Types — see the TODO at [data_loader.py:94](autoshift/optimizer/data_loader.py#L94).

CLI (`autoshift/commands.py`): `dump-dev-data` / `seed-dev-data` for snapshotting/seeding a
dev site.

## Known bugs (verified against source — real defects in existing, supposedly-working code)

1. Minor: `solver.py` only persists the input hash inside the `try`, so a run that fails
   before/during hashing gets cached as `Failed` with no hash — defeats the cache on retry.

## To be implemented (scaffolding exists; feature path is incomplete, not "broken")

- **Run → `Shift Assignment` link-back after commit.** `73e98fa` ("start online
  modifications") removed the `committed_assignments` field from `optimizer_run.json` as the
  first step of an in-progress redesign of how a committed run stays linked to the records it
  created. Plan: re-add it as a table on `Optimizer Run`, unless a better mechanism is found.
  `committer.py` raises `NotImplementedError` unconditionally until this lands.
- **`is_salaried` hardcodes a stub.** [data_loader.py:154-158](autoshift/optimizer/data_loader.py#L154-L158)
  currently assumes every employee is salaried (`is_salaried[name] = True`) as a placeholder —
  the previous string-matching logic against `employment_type` ("turnover"/"commission"/
  "casual") misclassified pay structure whenever a practice used different `Employment Type`
  labels, since it's a configurable Link doctype, not a fixed enum. Planned fix: a
  configurable name list (e.g. in Optimizer Settings) rather than hardcoded matching.
- **`disregard_assignments` = Use / Weigh.** Selectable in the UI; `data_loader.py` only
  handles `"Ignore"` and raises `NotImplementedError` for the others. `"Use"` forces existing
  Shift Assignments as hard constraints (code present, not yet wired in). `"Weigh"` is
  intended as a soft preference — existing assignments bias the objective like a shift
  preference weight, but the solver can still move them.
- **`Unbounded` planning mode.** Selectable, but `planning_days()` raises
  `NotImplementedError` immediately. Backlog — intended for future tools like automatic
  dynamic calendar speculation; no near-term design work planned.
- **Room-level assignment.** `Optimizer Run Slot.shift_location` and
  `Shift Location.custom_discipline` exist as scaffolding, but `model_builder.py` only tracks
  an aggregate room *count* per discipline/slot — nothing assigns a specific room yet.
  Backlog, same as `Unbounded` mode.

## Dropped (described in `shift_optimizer_design.md`; zero code today)

- **`shift_algorithm`** — the design doc's 3rd preference layer: a per-employee Python
  snippet, executed at solve time, that produces a `weights` dict. No field, no execution
  path, no sandboxing exists in the codebase. Feature was dropped due to low utility and
  high security risk.

## Working conventions

- **Don't hand-edit DocType JSON files** (`autoshift/autoshift/doctype/**/*.json`). Make the
  change via the Frappe Desk UI in developer mode (`bench set-config developer_mode 1`, then
  edit/create the DocType in the browser) — saving there auto-exports the JSON correctly.
  Enforced by a `PreToolUse` hook in `.claude/settings.json` that denies Edit/Write on these
  paths. Controller `.py`/`.js` files for the same doctype are not affected.

## Process / repo notes

- Single branch `version-16`, tracks `upstream/version-16` at `github.com/CMDBB/autoshift`.
  No `main`/`master`. Branch name is coupled to the targeted Frappe version.
- CI (`.github/workflows/ci.yml`) runs the real test suite against MariaDB+Redis on push/PR.
  `linter.yml` runs pre-commit, Frappe's Semgrep correctness rules, and `pip-audit`, gated on
  PRs. Both exist and pass today — but see the commit link-back item under To Be Implemented
  for what passing CI does *not* guarantee (zero Frappe-level integration coverage on the
  commit path).
- `tests/test_optimizer.py` is a solid pure-Python unit suite (no Frappe context) covering
  planning-day generation, hashing, every MILP constraint group. The three doctype-level
  `IntegrationTestCase` stubs (`optimizer_run`, `employee_settings`, `optimizer_settings`) are
  empty — zero Frappe-level integration coverage.
- `hooks.py`'s `app_description` still says "WIP (... natural language constraints and
  preference, explainable decisions)" — both are aspirational, no code exists for either.

## Dev commands

```bash
cd apps/autoshift && python -m pytest tests/ -v   # pure-Python optimizer tests, no site needed
pre-commit install                                 # ruff, eslint, prettier, pyupgrade
bench --site YOUR_SITE seed-dev-data --input ./dev_data
```
