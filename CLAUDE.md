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

User docs: `README.md`.

## Architecture

Doctypes (`autoshift/autoshift/doctype/`):
- **Optimizer Run** — main transactional doc. Lifecycle `Draft → Solving → Solved/Failed →
  Approved → Committed`. Immutable once solved; re-running = `duplicate()` into a new Draft.
  Points to an **Optimization Ruleset** (`ruleset`, required, default `"Standard Ruleset"`).
- **Optimization Rule** — one MILP constraint as a document: NL `description` + optional
  implementation. `implementation_type` = `Not Implemented` (description-only backlog item),
  `Built-in` (`builtin_key` into the `optimizer/rules.py` registry), or `Custom Code`
  (Python defining `apply(ctx)`, runs only after a developer checks `validated`; editing code
  clears the flag). Write access System Manager only — custom code executes at solve time.
- **Optimization Ruleset** (+ child `Optimization Ruleset Rule`) — reusable bundle of rules
  (compiling/validating rules is slow, rulesets are not). Unimplemented rules may be drafted
  into a ruleset (save warns) but `data_loader._load_rules` throws at solve time. The patch
  `create_standard_optimization_rules` seeds one rule doc per built-in + the Standard Ruleset,
  and backfills `ruleset` on pre-existing runs.
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
2. `rules.py` — the constraint groups as named rules: `BUILTIN_RULES` registry (six rules,
   formerly hardcoded in `model_builder`), `compile_custom_rule()` (exec's Custom Code rule
   source, expects `apply(ctx)`), `apply_rules()` (applies `DataPackage.rules` selection;
   empty selection = all built-ins, the pre-ruleset behaviour unit tests rely on).
3. `data_loader.py` — `load(run_doc)` hydrates a `DataPackage` from the Frappe DB; resolves
   the run's ruleset into `(rule_name, builtin_key, custom_code)` triples (throws on
   unimplemented/unvalidated rules; sorted by name for hash stability); normalizes
   preferences via temperature-scaled softmax (no weight deviates >50% from uniform).
4. `model_builder.py` — builds the PuLP MILP. Vars: `x[employee,shift,day,branch]`,
   `active_rooms[discipline,shift,day,branch]`. Constraints via `rules.apply_rules`.
   Objective (still hardcoded) = `turnover_weight × room utilization + Σ
   preference·assignment`.
5. `solver.py` — runs CBC (5s sync, escalates to 3600s background job via
   `frappe.enqueue(queue="long")` on timeout); caches by input hash against prior runs in
   `{Solved, Failed, Approved, Committed}`.
6. `committer.py` — converts an Approved run into submitted `Shift Assignment` records.
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
different Shift Types.

CLI (`autoshift/commands.py`): `dump-dev-data` / `seed-dev-data` for snapshotting/seeding a
dev site.

## To be implemented (scaffolding exists; feature path is incomplete, not "broken")

- gh issue #5 **Run → `Shift Assignment` link-back after commit.** `73e98fa` ("start online
  modifications") removed the `committed_assignments` field from `optimizer_run.json` as the
  first step of an in-progress redesign of how a committed run stays linked to the records it
  created. Plan: re-add it as a table on `Optimizer Run`, unless a better mechanism is found.
  `committer.py` raises `NotImplementedError` unconditionally until this lands.
- gh issue #8 **`Unbounded` planning mode.** Selectable, `planning_days()` returns infinite days, but
  the model builder truncates it to 100 days. Backlog — intended for future tools like automatic
  dynamic calendar speculation; no near-term design work planned.
- gh issue #9 **Room-level assignment.** `Optimizer Run Slot.shift_location` and
  `Shift Location.custom_discipline` exist as scaffolding, but `model_builder.py` only tracks
  an aggregate room *count* per discipline/slot — nothing assigns a specific room yet.
  Backlog, same as `Unbounded` mode.

## Working conventions

- **Don't hand-edit DocType JSON files** (`autoshift/autoshift/doctype/**/*.json`). Instead, 
  direct the user to make the change via the Frappe Desk UI in developer mode 
  (`bench set-config developer_mode 1`, then edit/create the DocType in the browser) — saving
  there auto-exports the JSON correctly. Enforced by a `PreToolUse` hook in
  `.claude/settings.json` that denies Edit/Write on these paths. Controller `.py`/`.js` files
  for the same doctype are not affected.

## Process / repo notes

- `developement` branch is the main branch, tracks `upstream/developement` at 
  `github.com/CMDBB/autoshift`. There is a `version-16` branch which development merges into
  by PR.
- CI (`.github/workflows/ci.yml`) runs the real test suite against MariaDB+Redis on push/PR.
  `linter.yml` runs pre-commit, Frappe's Semgrep correctness rules, and `pip-audit`, gated on
  PRs. They however cannot be run locally, and don't cover everything.
- `tests/test_optimizer.py` is a pure-Python unit suite (no Frappe context) covering
  planning-day generation, hashing, every MILP constraint group. The doctype-level
  `IntegrationTestCase` stubs (`employee_settings`, `optimizer_settings`) are
  left as autogenerated by frappe, except for `test_optimizer_run.py`.
- `hooks.py`'s `app_description` says "WIP (... natural language constraints and
  preference, explainable decisions)" — Optimization Rule documents now carry an NL
  description with an optional (LLM- or developer-written, developer-validated)
  implementation, but automatic NL→code generation and explainable decisions still
  have no code.

## Dev commands

```bash
uv run pytest tests/ # unit tests of the optimizer
pre-commit # ruff, eslint, prettier, pyupgrade
bench --site development.localhost seed-dev-data --input /path/to/dev_data
```
