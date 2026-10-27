# Autoshift

Frappe app (requires `frappe/hrms`) that schedules employee shifts across branches and
disciplines using MILP (PuLP + bundled COIN-OR CBC). Reads Employee / Shift Type / Leave
Application / Holiday List / Shift Assignment from Frappe HR, combines them with app-specific
config, and produces a schedule a user reviews, approves and commits into real
`Shift Assignment` records.

**Early-stage, single-developer WIP.** Check "Not built yet" below before assuming something is
broken rather than simply absent. **Ask the user about design intentions early and often.**

---

## Where things are written down

This file is the map: the app boundary, what each package is for, and the conventions. The
detail lives next to the code it describes, and is loaded when you work there.

| File | Holds |
|---|---|
| `README.md` | **Using the app** — setup, the Desk surfaces, running a solve |
| `docs/design-notes.md` | **Why a decision was made** — rationale, postmortems, superseded design. Keep all of that there, not here |
| `docs/formulation.md` | **The exact arithmetic a rule puts into the model** — every built-in's constraints and objective terms as formulas, with the notation and the definitional `p`/`active_rooms` constraints. Formulas only; no rationale |
| `autoshift/autoshift/doctype/CLAUDE.md` | every doctype and its fields |
| `autoshift/optimizer/CLAUDE.md` | the engine: `DataPackage`, the rule registry, the loader, the solver, diagnostics, role binding |
| `autoshift/wallchart/CLAUDE.md` | the week chart: derived layout, placement, coverage, filters, the PDF export |
| `autoshift/rota/CLAUDE.md` | settled schedules: materialising rotas, per-employee holiday lists, hand-editing |
| `autoshift/scanner/CLAUDE.md` | the forward scan: windows, the per-day verdict, resuming, the calendars |
| `autoshift/autoshift/page/CLAUDE.md` | the four Desk pages and the shared schedule view |

---

## App boundary

Three apps split the responsibility; keep them separate.

| App | Owns | Visibility |
|---|---|---|
| **autoshift** (this repo) | The scheduling engine and its doctypes. Generic — every practice-specific value is a doctype record a user enters, never a constant in code. | Public (`github.com/CMDBB/autoshift`) |
| `../zawin2frappe` | Migrating data out of the legacy ZaWin system into Frappe HR. Reads a *profile* for everything practice-specific. | Public |
| `../cmdb_frappe` | This practice: the zawin profile, curated overrides, investigation docs. `required_apps = ["autoshift", "zawin2frappe"]`. | **Private** |

- **No practice data, ever** — not in code, fixtures, tests or notebook outputs. Branch names,
  company abbreviations, discipline names, employee ids and real run dates together fingerprint
  the practice. Tests use neutral placeholders (`E1`, `B1`, `D1`).
- `sandbox/` is a live-data hazard: `sandbox/*.json` is gitignored and an `nbstripout`
  pre-commit hook clears `playground.ipynb` outputs. Don't defeat either.
- Don't add config doctypes or seed data for one practice's setup. A new practice-specific
  value becomes a *field* on an existing config doctype; the value lives in `cmdb_frappe`.

**Custom field ownership.** This app owns (module `Autoshift`, in
`autoshift/fixtures/custom_field.json`): `Shift Location.custom_discipline`,
`Shift Location.custom_branch`, `Employee.custom_fte`,
`Shift Schedule Assignment.custom_unconfirmed`,
`Shift Schedule Assignment.custom_create_shifts_until`, `Shift Schedule.custom_manually_edited`,
and — on both `Shift Assignment` and `Shift Schedule Assignment` — `custom_scheduling_role`
(Link) plus `custom_collateral_roles` (Table MultiSelect over the **Collateral Scheduling Role**
child doctype). `zawin2frappe` owns `Shift Assignment.custom_zawin_key` and
`Employee.custom_initials` — **do not re-add them here**; two apps shipping one fieldname under
different modules fight on every `migrate`. **Check the other apps' fixtures before adding any
`Custom Field`.**

---

## The shape of the thing

**Scheduling Role is the axis.** It replaced designation, which is payroll data and is no
longer read. A role names exactly one discipline; an employee holds roles through
**Employee Scheduling Role**, and **an employee holding no in-window role is not scheduled at
all**. A role declares how a shift in it is *worked* (`assignment_mode`: Flexible / Exclusive /
Collateral) and, separately, whether a room waits on it (`gates_rooms`).

**Presence, not assignment, is what a settled week fixes.** `p[employee, shift, day, branch]`
says somebody is in for that half-day; `x[employee, role, shift, day, branch]` says what they
spend it on. Workload and binding key on `p`; room coverage and role value key on `x`.

**Rules are documents.** Every constraint and objective term is an `Optimization Rule`, bundled
into an `Optimization Ruleset` a run points at. Built-ins live in a registry in
`optimizer/rules.py`; Custom Code rules run at solve time after a developer validates them.

**A scan is a sequence of runs.** The horizon scanner solves every window from a start date
to a date ahead, one `Optimizer Run` per window (`type = "Scan"`), and colours each day by
what its window came out as. It resumes by solving whatever row is still `Pending`, and
re-uses a run whose `input_hash` already matches rather than solving it again.

**A solve reads, it never writes.** Unrecorded rota days reach the model through
`rota.materialize.settled_rows`, so a horizon hashes identically whether or not its records
exist. Records are created only on explicit request.

**Everything Frappe-free is unit-tested.** Each package splits its arithmetic from its DB
reads on purpose — `optimizer/` minus `data_loader`, `wallchart/chart.py`, `rota/cycle.py`,
`rota/edit.py`, `rota/calendar.py`, `scanner/horizon.py`.

---

## Not built yet (scaffolding exists; the feature path is incomplete, not broken)

- **[#5](https://github.com/CMDBB/autoshift/issues/5) Run → `Shift Assignment` link-back after
  commit.** `73e98fa` removed `committed_assignments` from `optimizer_run.json` as step one of
  an in-progress redesign. Plan: re-add it as a table on Optimizer Run unless a better mechanism
  turns up. `committer.py` raises `NotImplementedError` until this lands.
- **[#8](https://github.com/CMDBB/autoshift/issues/8) `Unbounded` planning mode.** Selectable;
  `planning_days()` returns infinite days but the model builder truncates to 100. Backlog.
- **[#9](https://github.com/CMDBB/autoshift/issues/9) Room-level assignment — landed, opt-in.**
  `room_coverage_matched_rooms` matches a specific holder of every gating role into a specific
  numbered room (`1..Discipline Branch Config.rooms_num`, no new Room doctype), the prerequisite
  for `room_value_objective` / `employee_value_objective` / `synergy_value_objective`. All four
  are `standard=False`; the pooled `room_coverage` (headcount only, no pairing) stays the
  default. `Optimizer Run Slot.shift_location` / `Shift Location.custom_discipline` remain
  unused scaffolding: rooms are still bare ordinals, not tied to a `Shift Location` record. The
  wall chart **does** draw from the solved `room_index` where it exists, one row per room
  number, holes and all. Still deferred: giving a room its own persistent identity beyond one
  run's solved ordinals. **Benchmark solve time before ever proposing the matched rule as
  standard** — `y`'s index set multiplies `active_rooms`'s by `rooms_num`, with no
  symmetry-breaking constraint yet.
- **Free-seat / chair auction** and **dependency-graph inference for Custom Code rules** — no
  issue filed, no design work started; see design notes.
- **Branch Preferences** (`Employee Branch Preference`) are stored but not read by the solver.

---

## Working conventions

- **Patches do NOT run on fresh installs.** `install_app` marks every `patches.txt` entry
  completed without executing it (`set_all_patches_as_completed`); patches only run on `migrate`
  of already-installed sites. **Data seeding new sites need must therefore also run from
  `after_install`** (`autoshift/install.py`), sharing one idempotent function with the patch.
  Doctype schema needs neither — `install-app` syncs all doctype JSONs.
- **The built-in rule registry re-syncs on every migrate, not via a patch per rule.**
  `hooks.py`'s `after_migrate` calls `patches.create_standard_optimization_rules.execute()`
  unconditionally. Unlike a patch, an `after_migrate` hook has no "already ran" tracking, so
  this is safe precisely because that function is idempotent — upsert by `builtin_key`, drop
  unsupported leftovers, never touch a hand-tuned ruleset row's weight. Adding or renaming a
  built-in therefore needs **no new patch** to reach an already-migrated site; the historical
  per-rule patches (`add_objective_rules`, `add_role_value_rule`, …) predate the hook and stay
  for sites migrating from further back. The same function is exposed as
  `optimization_rule.reload_builtin_rules` (System Manager only), wired to a **Reload Built-in
  Rules** menu item on the Optimization Rule list view for picking up a code change without a
  full migrate.
- **DocType JSON files may be edited directly**, but must stay valid Frappe schema — keep
  `field_order` in sync with `fields`, keep `name` matching the directory. Prefer
  round-tripping through the Desk UI in developer mode (`bench set-config developer_mode 1`)
  when a change is fiddly, since saving there auto-exports a canonical file.
- **Two local sites, different jobs.** `development.localhost` is a quasi-staging site served to
  the developer for UI-based no-code changes and exploration — its state is not reproducible, so
  **never run integration tests against it**. `dev.test.localhost` is a pristine, never-served
  site for CI-like local runs (`allow_tests` already set). Keep it disposable; don't seed dev
  data into it.
- **Integration tests must pass on a pristine site.** Frappe's recursive test-record generation
  walks every link field; pulling in hrms doctypes (e.g. `Leave Application`) drags the whole
  hrms/erpnext record graph, which only ever generated cleanly on sites with warmed-up state.
  Prune with `IGNORE_TEST_RECORD_DEPENDENCIES` — see `test_optimizer_run.py`.
- CI (`.github/workflows/ci.yml`) runs the real suite against MariaDB + Redis on push/PR;
  `linter.yml` runs pre-commit, Frappe's Semgrep correctness rules and `pip-audit`, gated on
  PRs. Neither can be run locally in full, and they don't cover everything.
- Branches: work happens on **`development`**, which merges into **`version-16`** by PR.
  `upstream` is `github.com/CMDBB/autoshift`, whose default branch is `version-16` (there is no
  `upstream/development`).

---

## Tests

`uv run pytest tests/` — pure-Python, no Frappe context, ~2 s.

| Module | Covers |
|---|---|
| `test_optimizer.py` | planning days, hashing, every rule |
| `test_wallchart.py` | `wallchart/chart.py` placement, overflow, the run-vs-books merge, filing leave under the half-day it empties |
| `test_rota.py` | `rota/cycle.py` phase, handover boundary |
| `test_rota_edit.py` | `rota/edit.py` staging, merging, describing |
| `test_diagnostics.py` | the infeasibilities binding actually produces, found by both the solver-free scan and the elastic analysis |
| `test_holidays.py` | `rota/calendar.py` derivation, digest sharing, where two calendars first differ |
| `test_scanner.py` | `scanner/horizon.py` — window planning, the per-day verdict, scaling a shortfall to the exclusive holders present, narrowing, month grids |

Doctype-level `IntegrationTestCase` stubs are left as frappe autogenerated them, except
`test_optimizer_run.py` and `test_optimization_rule.py` (the developer-only
implementation/validation gate).

---

## Dev commands

```bash
uv run pytest tests/                       # unit tests of the optimizer
pre-commit                                 # ruff, eslint, prettier, pyupgrade
bench --site dev.test.localhost run-tests --app autoshift   # integration, as CI runs it
bench --site development.localhost seed-dev-data --input /path/to/dev_data
bench --site development.localhost diagnose-model --run OR-0001      # why is it infeasible
bench --site development.localhost update-holiday-lists --dry-run    # per-employee holiday lists
bench --site development.localhost scan-horizon --until 2027-03-01   # sweep a horizon, in the foreground
bench diagnose-model --snapshot sandbox/snapshots/OR-0001.json --lp /tmp/m.lp   # offline, no site
```

`dump-dev-data` / `seed-dev-data` snapshot and restore **only Autoshift's own configuration**
(`DEV_DATA_DOCTYPES`: Holiday List, Scheduling Role, Discipline Branch Config, Employee
Scheduling Role, Employee Settings, Optimizer Settings). Company / Branch / Designation / Shift
Type / Employee are zawin2frappe's job — seeding them here would duplicate it and dump real
personnel to disk. `capture-datapackage` snapshots a run's resolved `DataPackage` for the
sandbox notebook.
