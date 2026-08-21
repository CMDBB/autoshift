# Autoshift

Frappe app (requires `frappe/hrms`) that schedules employee shifts across multiple
branches and disciplines using Mixed Integer Linear Programming (PuLP + bundled COIN-OR
CBC solver). Reads Employee/Shift Type/Leave Application/Holiday List/Shift Assignment
from Frappe HR; combines with app-specific config; produces a schedule a user reviews,
approves, and commits into real `Shift Assignment` records.

Built for a dental practice, but **nothing about a specific practice belongs in this
repo** — see "App boundary" below.

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
  clears the flag). `rule_kind` (Constraint/Objective/Mixed) is synced from the registry for
  built-ins, developer-declared for custom code — dashboard-ready metadata. Custom code
  executes at solve time, so implementation fields
  (`implementation_type`, `builtin_key`, `implementation_code`, `validated`, `rule_kind`)
  are permlevel 1
  (System Manager write only); HR Manager may create/edit name + NL description only. The
  controller overrides `validate_higher_perm_levels` (which Frappe runs *before* `validate`,
  silently resetting permlevel-protected fields) to also warn/clean non-developer
  implementation edits and hard-refuse `validated` flips. `optimization_rule.js` upgrades
  the code editor: domain completions from the whitelisted `get_code_completions` (backed
  by `optimizer/editor_support.completion_items()`, introspected from
  RuleContext/DataPackage) via the Code control's `autocompletions` hook, plus inline Ruff
  lint (WASM webworker) through the `ace-linters` + `ace-python-ruff-linter` npm deps
  (`package.json`), self-hosted at `/assets/autoshift/node_modules/…` by `bench build`'s
  node_modules symlink (Ruff `builtins` configured for the injected `pulp`/`itertools`;
  provider completion functionality disabled so it can't clobber the Frappe completer).
- **Optimization Ruleset** (+ child `Optimization Ruleset Rule`) — reusable bundle of rules
  (compiling/validating rules is slow, rulesets are not). Each row has a `weight` that scales
  the rule's objective contribution (no-op on constraint rules; save warns). Unimplemented
  rules may be drafted into a ruleset (save warns) but `data_loader._load_rules` throws at
  solve time; a ruleset without an Objective/Mixed rule warns on save but solves (constant-0
  objective = feasibility check). The shared seeding in `create_standard_optimization_rules`
  (patch + `after_install` + re-invoked by the `add_objective_rules` patch) creates one rule
  doc per built-in, keeps the Standard Ruleset's rows in sync with the registry, backfills
  `ruleset` on pre-existing runs, and carries the removed `turnover_weight` setting over as
  the Standard Ruleset room-utilization row weight (**breaking**: non-Standard rulesets are
  not upgraded).
- **Optimizer Run Slot** — child; one row per assigned shift in a solution.
- **Optimizer Settings** — singleton: holiday lists.
- **Discipline Branch Config** (+ child `Discipline Branch Config Shift Type`) — per
  (discipline, branch): room count + the Shift Types in scope there.
- **Scheduling Role** — the optimizer's unit of *capability*, and what replaced designation
  as the scheduling axis: a role names exactly one discipline (Link to `Department`) and a
  max-rooms-per-holder figure. Designation is payroll data and is no longer read.
- **Employee Scheduling Role** — the employee x role relation, a **standalone doctype rather
  than a child table** so `zawin2frappe` can import into it directly. Carries `role_fte` (the
  *informally* agreed FTE % in that role — blank means no expectation), an optional
  `max_rooms` override, `active`, and a `valid_from`/`valid_to` window. An employee holding no
  in-window role is not scheduled at all; that is how non-clinical staff stay out of scope.
- **Employee Settings** (+ children `Employee Shift Preference`, `Employee Branch Preference`)
  — per-employee shift/branch preference overrides.
- **Leave Speculation** — child of Optimizer Run; treats a *pending* leave as approved for
  feasibility analysis only.
- **Bulk Employee Settings** — tool doctype, batch-creates the two per-employee records:
  **Employee Settings** (preferences) and **Employee Scheduling Role** (the capability that
  makes someone schedulable at all). One filtered employee list, two actions. Filters are
  role-based — Company, Discipline, Holds Role, plus a Coverage select for "who is missing
  one of these" — because department/designation no longer decide scope. Sync ≤30 employees,
  async + realtime progress above that; an employee who already has the record is skipped,
  never overwritten. Workers are module-level functions, not methods, so `frappe.enqueue`
  pickles a reference rather than dragging the Document through.

Optimizer engine (`autoshift/optimizer/`, pure-Python where possible for testability):
1. `types.py` — `DataPackage` dataclass (engine's only input shape), SHA256 `input_hash()`
   for caching, `planning_days()` (raises `NotImplementedError` for `"Unbounded"` mode).
2. `rules.py` — constraint groups AND objective terms as named rules: `BUILTIN_RULES`
   registry (seven constraint + three objective rules + `warm_start`, each with a `kind`),
   `_cname()`/`_vname()` for naming constraints and any auxiliary variables a rule
   introduces (`role_fte_target_objective` is the first rule to create variables of its own —
   a linearized absolute deviation), `compile_custom_rule()`
   (exec's Custom Code rule source, expects `apply(ctx)`), `apply_rules()` (applies
   `DataPackage.rules` selection; empty selection = all built-ins at weight 1.0, the
   pre-ruleset behaviour unit tests rely on). Objective rules call `ctx.add_objective(expr)`;
   the term is scaled by the ruleset row weight.
3. `data_loader.py` — `load(run_doc)` hydrates a `DataPackage` from the Frappe DB; resolves
   the run's ruleset into `(rule_name, builtin_key, custom_code, weight)` tuples (throws on
   unimplemented/unvalidated rules; sorted by name for hash stability); normalizes
   preferences via temperature-scaled softmax (no weight deviates >50% from uniform).
4. `model_builder.py` — builds the PuLP MILP. Vars: `x[employee,role,shift,day,branch]`,
   `active_rooms[discipline,shift,day,branch]`. **`x` is built sparse, over the
   `(employee, role)` pairs each employee actually holds** — so role eligibility is
   structural, not a rule: a variable for a role somebody cannot work does not exist, so
   nothing has to forbid it, and the model is no larger than it was before roles. This is the
   one scheduling policy that is *not* switchable from the ruleset UI. Same precedent as
   `active_rooms`, whose branch room cap lives in the variable's `upBound`. Constraints and objective both via
   `rules.apply_rules`; the maximized objective is the sum of the accumulated
   `ctx.objective_terms` (empty = constant 0, pure feasibility).
5. `solver.py` — runs CBC (5s sync, escalates to 3600s background job via
   `frappe.enqueue(queue="long")` on timeout); caches by input hash against prior runs in
   `{Solved, Failed, Approved, Committed}`.
6. `committer.py` — converts an Approved run into submitted `Shift Assignment` records.
   **The run→Shift-Assignment link-back is mid-redesign and not finished — see To Be
   Implemented.**

Custom fields on stock doctypes (`autoshift/fixtures/custom_field.json`), all under module
`Autoshift`: `Shift Location.custom_discipline`, `Shift Location.custom_branch` (Link to
`Branch` — source of truth for a `Shift Assignment`'s branch via its `shift_location`),
`Employee.custom_fte`. `zawin2frappe` *populates* `custom_fte` and `custom_branch` on import
but does not own them; `Shift Assignment.custom_zawin_key` and `Employee.custom_initials`
are owned by `zawin2frappe` (module `Zawin2Frappe`) and must not be re-added here — two apps
shipping the same fieldname under different modules fight on every `migrate`.

`Discipline Branch Config.shift_types` (Table MultiSelect, backed by the `Discipline Branch
Config Shift Type` child doctype) determines which `Shift Type`s are in scope for the
optimizer — a Shift Type not listed on any config row is treated as a non-clinical variant
and excluded. Keying the config on `(discipline, branch)` rather than the old
`(discipline, designation, branch)` removed the duplicate rows that used to make one
discipline's rows disagree about their Shift Types, so `data_loader.py` no longer needs the
`frappe.log_error` warning it once carried.

CLI (`autoshift/commands.py`): `dump-dev-data` / `seed-dev-data` snapshot and restore **only
Autoshift's own configuration** (`DEV_DATA_DOCTYPES`: Holiday List, Scheduling Role,
Discipline Branch Config, Employee Scheduling Role, Employee Settings, Optimizer Settings). Company/Branch/Designation/Shift
Type/Employee are zawin2frappe's job — seeding those here would duplicate it and dump real
personnel to disk. Both go through `get_doc(...).as_dict()`, not `get_all(fields=["*"])`,
because the latter returns no child rows (it was silently dropping Employee Settings'
preference tables and the DDBC shift-type selection) and cannot read Singles at all.
`capture-datapackage` snapshots a run's resolved `DataPackage` for the sandbox notebook.

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

## App boundary

Three apps split the responsibility; keep them separate.

| App | Owns | Visibility |
|---|---|---|
| **autoshift** (this repo) | The scheduling engine and its doctypes. Generic — every practice-specific value is a doctype record a user enters, never a constant in code. | Public (`github.com/CMDBB/autoshift`) |
| **`../zawin2frappe`** | Migrating data out of the legacy ZaWin system into Frappe HR. Reads a *profile* for everything practice-specific. | Public |
| **`../cmdb_frappe`** | This practice: the zawin profile, curated overrides, investigation docs. `required_apps = ["autoshift", "zawin2frappe"]`. | **Private** |

Consequences for work in this repo:

- **No practice data, ever** — not in code, fixtures, tests, or notebook outputs. Branch
  names, company abbreviations, discipline names, employee ids and real run dates together
  fingerprint the practice; `cmdb_frappe/README.md` explains why that is a disclosure about
  identifiable people, not a technical detail. Tests use neutral placeholders (`E1`, `B1`,
  `D1`).
- `sandbox/` is a live-data hazard. `sandbox/snapshots/*.json` (captured `DataPackage`s) and
  any other `sandbox/*.json` are gitignored, and an `nbstripout` pre-commit hook clears
  `playground.ipynb` outputs — the notebook runs against a real snapshot, so its outputs
  embed live data. Don't defeat either.
- Don't add configuration doctypes or seed data for one practice's setup. If autoshift needs
  a new practice-specific value, it becomes a field on an existing config doctype that a user
  fills in; the value itself lives in `cmdb_frappe`.
- Custom fields: see the ownership note above. Check the other apps' fixtures before adding
  a `Custom Field` on a stock doctype.

## Working conventions

- **DocType JSON files may be edited directly.** There used to be a `PreToolUse` hook in
  `.claude/settings.json` denying Edit/Write on `autoshift/autoshift/doctype/**/*.json` and
  `autoshift/fixtures/custom_field.json`; it was removed in `51ddfa6` and the restriction no
  longer applies. Hand-edited JSON must still be *valid* Frappe schema — keep `field_order` in
  sync with `fields`, keep `name` matching the directory, and prefer round-tripping through
  the Desk UI in developer mode (`bench set-config developer_mode 1`) when a change is
  fiddly, since saving there auto-exports a canonical file.

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
  left as autogenerated by frappe, except for `test_optimizer_run.py` and
  `test_optimization_rule.py` (the developer-only implementation/validation gate).
- **Two local sites, different jobs.** `development.localhost` is a quasi-staging site: served
  to the developer for UI-based no-code changes and general Frappe exploration — its state is
  not reproducible, so never run integration tests against it. `dev.test.localhost` is a
  pristine, never-served site for CI-like local test runs (`allow_tests` already set):
  `bench --site dev.test.localhost run-tests --module autoshift.autoshift.doctype.<x>.test_<x>`
  (or `--app autoshift` for the full suite, as CI does). Keep it disposable; don't seed dev
  data into it.
- **Patches do NOT run on fresh installs** — `install_app` marks every patches.txt entry as
  completed without executing it (`set_all_patches_as_completed`); patches only run on
  `migrate` of already-installed sites. Data seeding that new sites need must therefore also
  run from the `after_install` hook (`autoshift/install.py`), sharing one idempotent function
  with the patch. Doctype schema needs neither — `install-app` syncs all doctype JSONs.
- Integration tests must pass on a *pristine* site. Frappe's recursive test-record generation
  walks every link field; pulling in hrms doctypes (e.g. `Leave Application`) drags the whole
  hrms/erpnext record graph, which only ever generated cleanly on sites with warmed-up state —
  prune with `IGNORE_TEST_RECORD_DEPENDENCIES` (see `test_optimizer_run.py`).
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
