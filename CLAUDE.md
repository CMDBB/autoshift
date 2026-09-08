# Autoshift

Frappe app (requires `frappe/hrms`) that schedules employee shifts across branches and
disciplines using MILP (PuLP + bundled COIN-OR CBC). Reads Employee / Shift Type / Leave
Application / Holiday List / Shift Assignment from Frappe HR, combines them with
app-specific config, and produces a schedule a user reviews, approves and commits into real
`Shift Assignment` records.

- **Using the app:** `README.md`.
- **Why a decision was made:** `docs/design-notes.md`. Keep rationale, postmortems and
  superseded design there, not here.

**Early-stage, single-developer WIP.** Check "Not built yet" below before assuming something
is broken rather than simply absent. **Ask the user about design intentions early and
often.**

---

## App boundary

Three apps split the responsibility; keep them separate.

| App | Owns | Visibility |
|---|---|---|
| **autoshift** (this repo) | The scheduling engine and its doctypes. Generic — every practice-specific value is a doctype record a user enters, never a constant in code. | Public (`github.com/CMDBB/autoshift`) |
| `../zawin2frappe` | Migrating data out of the legacy ZaWin system into Frappe HR. Reads a *profile* for everything practice-specific. | Public |
| `../cmdb_frappe` | This practice: the zawin profile, curated overrides, investigation docs. `required_apps = ["autoshift", "zawin2frappe"]`. | **Private** |

- **No practice data, ever** — not in code, fixtures, tests or notebook outputs. Branch
  names, company abbreviations, discipline names, employee ids and real run dates together
  fingerprint the practice. Tests use neutral placeholders (`E1`, `B1`, `D1`).
- `sandbox/` is a live-data hazard: `sandbox/*.json` is gitignored and an `nbstripout`
  pre-commit hook clears `playground.ipynb` outputs. Don't defeat either.
- Don't add config doctypes or seed data for one practice's setup. A new practice-specific
  value becomes a *field* on an existing config doctype; the value lives in `cmdb_frappe`.
- **Custom field ownership.** This app owns (module `Autoshift`, in
  `autoshift/fixtures/custom_field.json`): `Shift Location.custom_discipline`,
  `Shift Location.custom_branch`, `Employee.custom_fte`, and `custom_manually_edited` on both
  `Shift Schedule` and `Shift Schedule Assignment`. `zawin2frappe` owns
  `Shift Assignment.custom_zawin_key` and `Employee.custom_initials` — **do not re-add
  them here**; two apps shipping one fieldname under different modules fight on every
  `migrate`. Check the other apps' fixtures before adding any `Custom Field`.

---

## Doctypes (`autoshift/autoshift/doctype/`)

- **Optimizer Run** — the transactional doc. `Draft → Solving → Solved/Failed → Approved →
  Committed`. Immutable once solved; re-running is `duplicate()` into a new Draft. Points at
  an **Optimization Ruleset** (`ruleset`, required, default `"Standard Ruleset"`).
- **Optimization Rule** — one MILP constraint or objective term as a document: NL
  `description` plus an optional implementation. `implementation_type` is `Not Implemented`
  (description-only backlog item), `Built-in` (`builtin_key` into the `optimizer/rules.py`
  registry) or `Custom Code` (Python defining `apply(ctx)`, run only after a developer checks
  `validated`; editing the code clears the flag). `rule_kind` is synced from the registry for
  built-ins, developer-declared for custom code.
  **Custom code executes at solve time**, so the implementation fields
  (`implementation_type`, `builtin_key`, `implementation_code`, `validated`, `rule_kind`) are
  permlevel 1 (System Manager write only); HR Manager may edit name + NL description only.
  The controller overrides `validate_higher_perm_levels` — Frappe runs it *before* `validate`
  and silently resets permlevel-protected fields — to also warn/clean non-developer
  implementation edits and hard-refuse `validated` flips.
  `optimization_rule.js` upgrades the code editor: domain completions from the whitelisted
  `get_code_completions` (backed by `optimizer/editor_support.completion_items()`,
  introspected from RuleContext/DataPackage) via the Code control's `autocompletions` hook,
  plus inline Ruff lint (WASM webworker) through the `ace-linters` +
  `ace-python-ruff-linter` npm deps, self-hosted at `/assets/autoshift/node_modules/…` by
  `bench build`'s node_modules symlink.
- **Optimization Ruleset** (+ child **Optimization Ruleset Rule**) — reusable bundle of rules
  (compiling/validating rules is slow; rulesets are not). Each row's `weight` scales that
  rule's objective contribution (no-op on constraint rules; save warns). Unimplemented rules
  may be drafted in (save warns) but `data_loader._load_rules` throws at solve time; a
  ruleset with no Objective/Mixed rule warns but solves (constant-0 objective = feasibility
  check). `validate()` runs `BuiltinRule.check_ruleset` over the row set's built-in keys
  (Custom Code rows skipped, as in `apply_rules`), so a missing `requires` or two rules
  sharing a choice `group` is a save-time throw, not only a solve-time one.
  `create_standard_optimization_rules` (patch + `after_install`) creates one rule doc per
  built-in, keeps the Standard Ruleset's rows in sync with the registry, and backfills
  `ruleset` on pre-existing runs.
- **Optimizer Run Slot** — child; one row per assigned shift (the `x` variables that came
  back 1).
- **Optimizer Run Coverage** — child; the `active_rooms` counterpart. One row per
  (discipline, branch, date, shift) with `staffed_rooms` vs `capacity`. Zero-staffed rows are
  kept on purpose. Pre-table runs fall back to `_derive_coverage_matrix`.
- **Optimizer Settings** — singleton: holiday lists.
- **Discipline Branch Config** (+ child **… Shift Type**) — per (discipline, branch): room
  count and the Shift Types in scope. A Shift Type on no config row is treated as
  non-clinical and excluded.
- **Scheduling Role** — the optimizer's unit of *capability* and the scheduling axis that
  replaced designation: names exactly one discipline (Link to `Department`) plus a
  max-rooms-per-holder figure. `assignments_binding` (Check, default off) marks a role whose
  schedule is settled by its holders. `display_order_key` (Int, default 0) orders wall-chart
  lanes.
- **Employee Scheduling Role** — the employee × role relation. A **standalone doctype, not a
  child table**, so `zawin2frappe` can import into it directly. Carries `role_fte` (the
  *informally* agreed FTE % in that role; blank = no expectation), an optional `max_rooms`
  override, a `binding_override` Select (blank inherits the role's flag), `active`, and a
  `valid_from`/`valid_to` window. **An employee holding no in-window role is not scheduled at
  all** — that is how non-clinical staff stay out of scope.
- **Scheduling Rule Topic** — optional heading an `Optimization Rule` files itself under, so
  Studio's toggle panel renders as collapsible sections. Built-ins declare theirs in
  `rules.py` (`TOPIC_*`, `TOPIC_ORDER`); the seeding re-syncs the topic docs (`is_system=1`)
  and each built-in's `topic` on every migrate. Hand-authored topics (`is_system=0`) are
  never touched. Orthogonal to `BuiltinRule.group` — see design notes.
- **Employee Settings** (+ children **Employee Shift Preference**, **Employee Branch
  Preference**) — per-employee preference overrides.
- **Leave Speculation** — child of Optimizer Run; treats a *pending* leave as approved for
  feasibility analysis only.
- **Bulk Employee Settings** — tool doctype; batch-creates **Employee Settings** and
  **Employee Scheduling Role** from one role-based filtered employee list. Sync ≤30
  employees, async + realtime above that; an employee who already has the record is skipped,
  never overwritten.
- **Rota Edit Draft** (+ child **Rota Edit Draft Change**) — one user's staged, unapplied
  edits to one discipline's bound rotas. Autonamed from (user, discipline) so staging always
  finds the same document.

---

## Optimizer engine (`autoshift/optimizer/`)

Pure-Python where possible, for testability. `data_loader.py` is the only Frappe-dependent
module.

1. `types.py` — `DataPackage` (the engine's only input shape), SHA256 `input_hash()` for
   caching, `planning_days()` (raises `NotImplementedError` for `"Unbounded"`).
2. `rules.py` — constraint groups *and* objective terms as named rules. `BUILTIN_RULES`
   registry populated by the `@builtin_rule` decorator; `STANDARD_RULES` is the
   `standard=True` subset the seeding puts in the Standard Ruleset. Currently 15 built-ins:
   `one_shift_per_day`, `warm_start`, `leave_blocklist`, `use_existing_assignments`,
   `bind_role_assignments`, `soft_bind_role_assignments`, `one_branch_per_shift`,
   `room_coverage`, `fte_ceiling`,
   `role_fte_ceiling` (constraints) and `room_utilization_objective`, `fte_soft_ceiling`,
   `role_fte_target_objective`, `shift_preference_objective`, `weigh_assignments_objective`
   (objectives). Three choice groups: `existing_assignments`, `role_binding` and
   `workload_ceiling` (`fte_ceiling` vs `fte_soft_ceiling`). `_cname()`/`_vname()` name
   constraints and any auxiliary variables (`role_fte_target_objective` linearizes an
   absolute deviation with a pair of them, `fte_soft_ceiling` a one-sided one with a
   single variable). `compile_custom_rule()` execs Custom Code source expecting `apply(ctx)`.
   `apply_rules()` applies `DataPackage.rules`; an empty selection means all built-ins at
   weight 1.0 (the pre-ruleset behaviour unit tests rely on). Objective rules call
   `ctx.add_objective(expr)`; the term is scaled by the ruleset row weight.
   `BuiltinRule.requires`/`.excludes`/`.group`/`.default_weight`/`.topic` are hand-authored,
   **built-ins only** — Custom Code rules carry none of it and are exempt from every check.
   **`apply_rules` reorders specs via `order_specs`, a stable topological sort over
   `requires` — do not drop this**, see design notes.
3. `data_loader.py` — `load(run_doc)` hydrates a `DataPackage`; resolves the ruleset into
   `(rule_name, builtin_key, custom_code, weight)` tuples (throws on
   unimplemented/unvalidated; sorted by name for hash stability); normalizes preferences via
   temperature-scaled softmax (no weight deviates >50% from uniform). Resolves binding pairs
   into `binding_pairs`, leave-vs-books collisions into `binding_conflicts`, and unplaceable
   assignments into `unresolved_assignments` (but **throws** for a bound employee).
4. `model_builder.py` — builds the PuLP MILP. Vars `x[employee,role,shift,day,branch]` (built
   **sparse**, over the `(employee, role)` pairs each employee actually holds) and
   `active_rooms[discipline,shift,day,branch]`. Constraints and objective both via
   `rules.apply_rules`; the maximized objective sums `ctx.objective_terms` (empty = constant
   0, pure feasibility).
5. `solver.py` — runs CBC (5 s sync, escalating to a 3600 s background job via
   `frappe.enqueue(queue="long")` on timeout); caches by input hash against prior runs in
   `{Solved, Failed, Approved, Committed}`. Persists `solution_table` (`x`), `coverage_table`
   (`active_rooms`) and `objective_breakdown` (per-rule objective shares).
   **`build()` returns its `RuleContext` as a fifth element** so the breakdown can be
   evaluated against solved variables — update sandbox/test call sites if you change that
   shape.
6. `committer.py` — converts an Approved run into submitted `Shift Assignment` records.
   Raises `NotImplementedError` unconditionally; see "Not built yet".
7. `diagnostics.py` — Frappe-free. Why a model is infeasible, and what it contains.
   `conflict_scan` replays the pinned assignments against the *arithmetic* of the selected
   constraint rules and names the offending Shift Assignment without solving anything;
   `model_dump`/`write_lp` print the variables and constraints grouped by the rule that
   emitted them (`_cname`'s `prefix:` convention); `elastic_analysis` slacks every
   constraint and minimizes the total, so the non-zero slacks *are* the infeasibility.
   `relax_integrality`/`lp_relaxation`/`shadow_prices` drop integrality so CBC returns
   duals — `pi`/`dj` are `None` on a MILP. **`solver.run_solve` appends `report()` to a
   Failed run's Solver Log**; `bench diagnose-model` and the sandbox helpers are the
   on-demand surfaces. See design notes for why there are three instruments.
8. `editor_support.py` — introspects RuleContext/DataPackage for the rule editor's
   completions.
9. `rule_scratchpad.py` — a drafting space for rule implementations, mirroring the rule
   namespace. **Committed *and* gitignored**, so local edits don't bubble up.

---

## UI surfaces

**Optimizer Studio** (`autoshift/optimizer_studio.py` + Desk Page
`autoshift/autoshift/page/optimizer_studio/`) — a workspace-level abstraction *over*
Optimizer Run + Optimization Ruleset, and the first "automatic"-run surface: Planning Mode /
Start Date / a human-readable rule-toggle panel (choice groups as radios incl. an explicit
"None", everything else checkboxes with a weight input on Objective/Mixed rules), a
"Populate From Run" link picker, and a "Preview Schedule" action. The panel is built so a
ruleset `check_ruleset` would reject is structurally unreachable — see design notes before
touching `index_catalog` / `blocked_reasons` / `sync_dependencies`. Every preview overwrites
one ruleset per user (`Studio Draft — <user>`, `is_system=0`) and really does create an
Optimizer Run, with `type="Automatic"`. Solving reuses `OptimizerRun.solve()` unchanged.
"Save Ruleset As" promotes the draft via `frappe.copy_doc`.

**Both solve entry points** — the Optimizer Run form's Solve button and Studio's Preview —
first call a binding-gap check (`OptimizerRun.check_binding_rule_gap` /
`optimizer_studio.check_binding_rule_gap`, thin wrappers over `data_loader.binding_rule_gap`)
and confirm before running when the site marks roles binding but the selection omits
`bind_role_assignments`. The same confirm carries the count from `check_pending_bound_shifts`
and creates those records before solving.

**The schedule view** (`autoshift/public/js/schedule_view.js`, namespaced
`autoshift.schedule_view`) — one tab bar, **Week / Statistics / Roster / Solver Log**,
rendered by both the Optimizer Run form (into `schedule_view_html`) and Studio's result area.
Panes load lazily on first click and are cached. Statistics and Roster need a solved run and
are *disabled with a tooltip* rather than hidden; Solver Log needs only a run, so Studio's
`preview()`/`get_run_status()` return `solver_log` on every outcome (shared `_run_result`).

- **Week** — the wall chart (`autoshift/wallchart/`, `autoshift/public/js/wall_chart.js`).
  The default pane and the only **always-on** one: it renders on a Draft, on a Failed run and
  in Studio before the first preview, because it falls back to the submitted
  `Shift Assignment`s on the books. Rooms down the page, days across — it answers "is Tuesday
  morning covered", which the per-employee roster grid structurally cannot.
  Layout is **derived** by `layout.derive()` from Discipline Branch Config + Scheduling Role +
  Shift Type; anything no band claims lands in an `Unplaced` band with the reason stated.
  With a run, cells are a diff against the books (`kept`/`added`/`dropped`, plus `changed` on
  a moved half-day) via `chart.merge`. A week whose bound practitioners have no Shift
  Assignments yet carries a banner offering to create them (`pending_bound` in the payload).
  Split like the optimizer: `chart.py` is Frappe-free (covered by `tests/test_wallchart.py`),
  `layout.py`/`source.py` read the DB, `api.py` holds the whitelisted
  `get_week_chart(week, run, mode)`. Cells print initials — `Employee.custom_initials` where
  zawin2frappe has installed it (**read, never shipped here**), otherwise derived from the
  name.
- **Statistics** — `OptimizerRun.get_run_statistics()` + `autoshift/public/js/run_stats.js`.
  Room-slots staffed vs configured, per-discipline coverage meters, a discipline × day grid,
  employees below FTE target, and each rule's share of the objective
  (`objective_breakdown`, persisted as JSON from `RuleContext.objective_contributions`; the
  shares sum to the objective value — `test_objective_contributions_attribute_and_sum_to_the_objective`
  pins that). Meters carry a **role-supply bound** (`_role_supply_bounds`) drawn as a marker
  and stated as a warning where it falls below configured capacity.
- **Roster** — the per-employee grid, `autoshift/public/js/schedule_grid.js` (namespaced
  `autoshift.schedule_grid`), shared by the form and Studio off the same `{days, employees,
  events}` shape both `get_schedule_events()` and Studio's `preview()` return.

**Rota Editor** (Desk Page `autoshift/autoshift/page/rota_editor/`) — lets a planner drag a
bound employee's shifts to a different day, shift type or branch within their own discipline,
or add one. Edits stage into a `Rota Edit Draft` (server-side, survives a reload) and apply as
one `EditPlan`. See "Materialising settled schedules" below.

---

## Role binding and settled schedules

**Role binding** — two rules in the `role_binding` choice group (`rules.GROUP_ROLE_BINDING`),
so a ruleset picks at most one; `data_loader.binding_rule_gap` warns when it picks neither.
`soft_bind_role_assignments` (**standard**) fixes every variable of a bound `(employee, role)`
pair that is *not* on the books to 0 and leaves the ones that are free at their warm-start
value of 1 — the holder is never given a shift they don't already have, but a settled week
that breaks the rest of the ruleset loses a shift instead of failing the whole solve.
`bind_role_assignments` (strict, off by default) fixes *every* one of their variables, so
`warm_start`'s 1/0 init pins existing shifts on and everything else off. A day with nothing on
the books stays empty under either. Leave wins over a settled assignment (the loader drops the
collision into `binding_conflicts` rather than making the run infeasible). autoshift ships the
mechanism defaulting to off; `zawin2frappe` populates which roles are binding.
`use_existing_assignments` is **not** in `STANDARD_RULES`. Rationale for all of this is in the
design notes.

**`autoshift/rota/`** — materialises `Shift Schedule` / `Shift Schedule Assignment` patterns
into the `Shift Assignment` records everything here reads, because HRMS's own nightly job
mis-phases any cycle longer than a week. **It is a workaround for an upstream bug and is
signposted as one throughout**; the day `create_shifts` anchors its weeks properly, this
package is deleted.

- `cycle.py` — Frappe-free (`Rota` + `occurrences`, `tests/test_rota.py`). Weekdays in
  `repeat_on_days`, one week in every `cycle_weeks`, counting from the week after
  `create_shifts_after`. Weeks are ISO (Monday-based).
- `materialize.py` — `pending` / `materialize`. **`create_shifts_after` is never written**;
  idempotency comes from comparing against the books. `enabled`/`shift_status` are ignored;
  records are created `Active` and link back via `Shift Assignment.shift_schedule_assignment`.
  Coverage is keyed on `(employee, date)` or `(employee, date, shift_type)` per
  `HR Settings.allow_multiple_shift_assignments`. One record per day, one savepoint per row;
  failures are collected and reported, never fatal.
- Three surfaces, each a thin wrapper over `materialize.pending`/`.materialize`, sharing one
  browser-side helper (`autoshift/public/js/rota.js`, namespaced `autoshift.rota`): the wall
  chart's `pending_bound` banner, and `check_pending_bound_shifts` /
  `materialize_bound_shifts` on both `OptimizerRun` and `optimizer_studio`. **Creation at
  solve time is not optional** — binding freezes people against exactly those records.

**Hand-editing (`rota/edit.py` + `rota/editor.py`)** — same split: `edit.py` is Frappe-free
(`tests/test_rota_edit.py`), `editor.py` is the DB half.

- A hand edit is **gold standard**: editing an assignment replaces it wholesale with a fresh
  `Shift Schedule Assignment` (+ private `Shift Schedule`), tagged `custom_manually_edited`.
  **zawin2frappe's import must skip any row carrying that tag** (enforced there, not here). A
  shared zawin2frappe-owned `Shift Schedule` is only unlinked, never edited or deleted; a
  private schedule an edit empties is cancelled and deleted. Created assignments are
  `enabled = 0` / `shift_status = "Inactive"` — `materialize.py` stays the sole generator.
- `edit.Change` stages one edit at single-occurrence granularity (`add`/`move`/`remove`),
  identified by weekday **and** `from_phase`/`to_phase`. `apply_changes(rotas, changes,
  view_start, view_weeks) -> EditPlan` folds a batch onto the current `Rota`s.
  **Periodicity is derived, not identity** — groups key on `(employee, shift_type, branch)`
  alone and `edit.minimal_cycle` reads the cadence back out, which is how a rota's periodicity
  changes without any explicit action. Emitted as `EditPlan.cadence_changes`, surfaced as
  **Periodicity changes**, recomputed fresh on every `get_state`/`stage_change` — never
  stored.
- A rota is editable at a given view width exactly when `view_weeks % cycle_weeks == 0`;
  otherwise the employee is drawn as a greyed-out read-only row showing `edit.phase_fractions`
  (`"1/4"`, `"2/4"`…). Editable chips badge their own cadence (`"2w"`) once `cycle_weeks > 1`.
- `apply_draft(discipline, start, view_weeks)` takes the view explicitly from the toolbar
  rather than persisting one, and clears the draft's rows *before* deleting the assignments
  they reference (a live Link blocks the delete otherwise).
- `editor.py` translates between the **Branch** vocabulary the grid uses and the raw
  `Shift Location` docname `Shift Schedule Assignment.shift_location` stores
  (`_rotas_by_branch` / `_shift_location_for`, via `Shift Location.custom_branch`).
  Room-level assignment doesn't exist yet, so any Shift Location matching the target branch
  is as good as another.

---

## Not built yet (scaffolding exists; the feature path is incomplete, not broken)

- **[#5](https://github.com/CMDBB/autoshift/issues/5) Run → `Shift Assignment` link-back
  after commit.** `73e98fa` removed `committed_assignments` from `optimizer_run.json` as step
  one of an in-progress redesign. Plan: re-add it as a table on Optimizer Run unless a better
  mechanism turns up. `committer.py` raises `NotImplementedError` until this lands.
- **[#8](https://github.com/CMDBB/autoshift/issues/8) `Unbounded` planning mode.** Selectable;
  `planning_days()` returns infinite days but the model builder truncates to 100. Backlog.
- **[#9](https://github.com/CMDBB/autoshift/issues/9) Room-level assignment.**
  `Optimizer Run Slot.shift_location` and `Shift Location.custom_discipline` exist as
  scaffolding, but `model_builder.py` only tracks an aggregate room *count*. Backlog.
- **Free-seat / chair auction** and **dependency-graph inference for Custom Code rules** — no
  issue filed, no design work started; see design notes.
- **Branch Preferences** (`Employee Branch Preference`) are stored but not read by the solver.

---

## Working conventions

- **DocType JSON files may be edited directly.** They must stay valid Frappe schema — keep
  `field_order` in sync with `fields`, keep `name` matching the directory. Prefer
  round-tripping through the Desk UI in developer mode
  (`bench set-config developer_mode 1`) when a change is fiddly, since saving there
  auto-exports a canonical file.
- **Patches do NOT run on fresh installs.** `install_app` marks every `patches.txt` entry
  completed without executing it (`set_all_patches_as_completed`); patches only run on
  `migrate` of already-installed sites. **Data seeding new sites need must therefore also run
  from `after_install`** (`autoshift/install.py`), sharing one idempotent function with the
  patch. Doctype schema needs neither — `install-app` syncs all doctype JSONs.
- **Two local sites, different jobs.** `development.localhost` is a quasi-staging site served
  to the developer for UI-based no-code changes and exploration — its state is not
  reproducible, so **never run integration tests against it**. `dev.test.localhost` is a
  pristine, never-served site for CI-like local runs (`allow_tests` already set). Keep it
  disposable; don't seed dev data into it.
- **Integration tests must pass on a pristine site.** Frappe's recursive test-record
  generation walks every link field; pulling in hrms doctypes (e.g. `Leave Application`) drags
  the whole hrms/erpnext record graph, which only ever generated cleanly on sites with
  warmed-up state. Prune with `IGNORE_TEST_RECORD_DEPENDENCIES` — see `test_optimizer_run.py`.
- CI (`.github/workflows/ci.yml`) runs the real suite against MariaDB + Redis on push/PR;
  `linter.yml` runs pre-commit, Frappe's Semgrep correctness rules and `pip-audit`, gated on
  PRs. Neither can be run locally in full, and they don't cover everything.
- Branches: work happens on **`development`**, which merges into **`version-16`** by PR.
  `upstream` is `github.com/CMDBB/autoshift`, whose default branch is `version-16` (there is
  no `upstream/development`).

## Tests

- `uv run pytest tests/` — pure-Python, no Frappe context, ~2 s. `test_optimizer.py`
  (planning days, hashing, every rule), `test_wallchart.py` (`wallchart/chart.py` placement,
  overflow, the run-vs-books merge), `test_rota.py` (`rota/cycle.py` phase, handover
  boundary), `test_rota_edit.py` (`rota/edit.py` staging, merging, describing),
  `test_diagnostics.py` (the infeasibilities binding actually produces, found by both the
  solver-free scan and the elastic analysis).
- Doctype-level `IntegrationTestCase` stubs are left as frappe autogenerated them, except
  `test_optimizer_run.py` and `test_optimization_rule.py` (the developer-only
  implementation/validation gate).

## Dev commands

```bash
uv run pytest tests/                       # unit tests of the optimizer
pre-commit                                 # ruff, eslint, prettier, pyupgrade
bench --site dev.test.localhost run-tests --app autoshift   # integration, as CI runs it
bench --site development.localhost seed-dev-data --input /path/to/dev_data
bench --site development.localhost diagnose-model --run OR-0001      # why is it infeasible
bench diagnose-model --snapshot sandbox/snapshots/OR-0001.json --lp /tmp/m.lp   # offline, no site
```

`dump-dev-data` / `seed-dev-data` snapshot and restore **only Autoshift's own configuration**
(`DEV_DATA_DOCTYPES`: Holiday List, Scheduling Role, Discipline Branch Config, Employee
Scheduling Role, Employee Settings, Optimizer Settings). Company / Branch / Designation /
Shift Type / Employee are zawin2frappe's job — seeding them here would duplicate it and dump
real personnel to disk. `capture-datapackage` snapshots a run's resolved `DataPackage` for the
sandbox notebook.
