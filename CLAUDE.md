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
  not upgraded). `is_system` (hidden Check, read-only, seeded true on the Standard Ruleset)
  marks a ruleset as app-curated rather than hand-authored — a marker for future tooling, not
  an edit lock; nothing currently stops hand-editing a system ruleset. `validate()` also runs
  `BuiltinRule.check_ruleset` over the row set's built-in keys (Custom Code rows are skipped,
  same as `apply_rules`), so a bad combination — a missing `requires`, or two rules sharing a
  choice `group` — is now a save-time `frappe.throw`, not just a solve-time one.
- **Optimizer Run Slot** — child; one row per assigned shift in a solution (the `x`
  decision variables that came back 1).
- **Optimizer Run Coverage** — child; the `active_rooms` counterpart to the above. One row
  per (discipline, branch, date, shift) slot with `staffed_rooms` vs `capacity`, written by
  the solver alongside the slots. Zero-staffed rows are kept on purpose — an empty slot is
  the thing a planner needs to see. Runs solved before this table existed still report
  coverage: `_derive_coverage_matrix` reconstructs the same numbers from the assignment
  slots (verified to match the persisted table exactly on a re-solve).
- **Optimizer Settings** — singleton: holiday lists.
- **Discipline Branch Config** (+ child `Discipline Branch Config Shift Type`) — per
  (discipline, branch): room count + the Shift Types in scope there.
- **Scheduling Role** — the optimizer's unit of *capability*, and what replaced designation
  as the scheduling axis: a role names exactly one discipline (Link to `Department`) and a
  max-rooms-per-holder figure. Designation is payroll data and is no longer read.
  `assignments_binding` (Check, default off) marks a role whose schedule is settled by its
  holders rather than by the planner — see "Role binding" below.
- **Employee Scheduling Role** — the employee x role relation, a **standalone doctype rather
  than a child table** so `zawin2frappe` can import into it directly. Carries `role_fte` (the
  *informally* agreed FTE % in that role — blank means no expectation), an optional
  `max_rooms` override, a `binding_override` Select (blank inherits the role's
  `assignments_binding`, same nullable-override convention as `max_rooms`), `active`, and a
  `valid_from`/`valid_to` window. An employee holding no in-window role is not scheduled at
  all; that is how non-clinical staff stay out of scope.
- **Scheduling Rule Topic** — an optional heading an `Optimization Rule` files itself under
  (`Optimization Rule.topic`, a Link), so Optimizer Studio's toggle panel renders as
  collapsible sections instead of one flat list. **Orthogonal to `BuiltinRule.group`**: a
  group is a mutual-exclusion choice set `check_ruleset` enforces, a topic constrains
  nothing. Built-ins declare theirs in `rules.py` (`TOPIC_*` constants, `TOPIC_ORDER` for
  display order) and the seeding re-syncs both the topic documents (`is_system=1`) and each
  rule's `topic` on every migrate; hand-authored topics (`is_system=0`) are never touched,
  and Custom Code rules may file themselves under any of them. Topics bucket only the
  *top-level* rules — a rule nested by `requires` follows its parent into whichever section
  the parent landed in, because the dependency is the more useful thing to see.
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
  pickles a reference rather than dragging the Document through. The **inline path reports
  its result in the response**, not over realtime (`_run(realtime=False)`): a bench whose
  socketio is down — a common dev state — otherwise leaves the caller with no feedback at
  all, which reads as the tool having silently done nothing. A large selection also falls
  back to running inline when `autoshift.utils.background_workers_alive()` finds no worker
  registered, since the queued job would otherwise sit in redis forever.

**Optimizer Studio** (`autoshift/optimizer_studio.py` + Desk Page
`autoshift/autoshift/page/optimizer_studio/`, linked from the Autoshift workspace as a
shortcut) — a workspace-level abstraction over Optimizer Run + Optimization Ruleset, and
the first "automatic"-run surface: Planning Mode / Start Date / a human-readable panel of
rule toggles (choice groups as radios incl. an explicit "None", everything else as
checkboxes with a weight input on Objective/Mixed rules).
**The panel is designed so a failing ruleset is unreachable, not merely rejected later.**
All three of `check_ruleset`'s failure modes are made structural: a choice group is radios
(≤1 member); `requires` becomes *nesting* — `index_catalog` builds a forest and a child is
drawn inside the rule it requires, its checkbox disabled until the parent is on, so the
parent reads as a fieldset; and `excludes` disables and unchecks its targets. Blocked rows
are dimmed and carry a `title` tooltip naming the rule that blocked them. The decision half
(`blocked_reasons(checked)`) is deliberately DOM-free so that invariant is testable on its
own; `sync_dependencies()` applies it to the DOM and re-runs to a fixpoint, since
unchecking a parent can orphan a grandchild. Grouped rules are the one thing not nested —
`existing_assignments`' members have different requirements, so nesting would split the
radio set — their dependency is enforced dynamically instead. Note that rows nest, so every
DOM read is scoped to a row's **own** controls (`own_toggle`/`own_weight`); a plain
`.find()` reaches into child rows and reports a parent as selected whenever a descendant
is.
Both solve entry points (the Optimizer Run form's Solve button and Studio's Preview) first
call a binding-gap check — `OptimizerRun.check_binding_rule_gap` /
`optimizer_studio.check_binding_rule_gap`, both thin wrappers over
`data_loader.binding_rule_gap` — and confirm before running when the site marks roles
binding but the selection omits `bind_role_assignments`. A settled schedule that no rule
enforces is silently re-planned and nothing downstream would show that, so it has to be
said before the solve, not after.
Studio's other pieces: a "Populate From Run" link picker to seed the panel from an existing
run's configuration, and a "Preview Schedule"
primary action. Deliberately duplicates a couple of Optimizer Run's own fields (mode,
date) in the page toolbar rather than being a form tab — it's meant to abstract *over*
runs, not edit one. Every preview writes into one ruleset per user (`Studio Draft —
<user>`, `is_system=0`), overwritten in place on each click rather than proliferating a
ruleset per click; a system (`is_system=1`) preset is never edited directly, only copied
into the draft. Solving reuses `OptimizerRun.solve()` unchanged (so the same
sync-then-background-job escalation applies), and each preview really does create an
Optimizer Run — with `type="Automatic"`, which the workspace's Optimizer Run quick list
already filtered out before this existed. "Save Ruleset As" promotes the draft to a
permanent name via `frappe.copy_doc`. The schedule-grid renderer itself
(`autoshift-schedule-grid`/`build_html`/chip styling) was extracted out of
`optimizer_run.js` into `autoshift/public/js/schedule_grid.js` (loaded via
`frappe.require`, namespaced `autoshift.schedule_grid`) so the Optimizer Run form and
Optimizer Studio render identically off the same `{days, employees, events}` shape both
`get_schedule_events()` and Studio's `preview()`/`get_run_status()` return. It is now the
**Roster** pane of the shared schedule view below, no longer the primary visualization.

**The schedule view** (`autoshift/public/js/schedule_view.js`, namespaced
`autoshift.schedule_view`) — one tab bar, **Week / Statistics / Roster / Solver Log**,
rendered by both the Optimizer Run form (into `schedule_view_html`; the second HTML field
`stats_html` was removed) and Optimizer Studio's result area. Panes load lazily on first
click and are cached. Statistics and Roster need a solved run and are *disabled with a
tooltip* rather than hidden — a tab that vanishes reads as a missing feature. Solver Log
needs only a run, so a **Failed** run's log is reachable, which the old solved-runs-only
guard made impossible. Studio's `preview()`/`get_run_status()` therefore return
`solver_log` on every outcome (shared `_run_result`), not only on failure.

**The week wall chart** (`autoshift/wallchart/`, namespaced `autoshift.wall_chart` in
`autoshift/public/js/wall_chart.js`) — the default pane and the **only always-on** one: it
renders on a Draft, on a Failed run and in Studio before the first preview, because it
falls back to the submitted `Shift Assignment`s on the books. Rooms down the page, days
across; it answers "is Tuesday morning covered", which the per-employee roster grid
structurally cannot, coverage being a fact about rooms.
- **The layout is derived, never declared.** `layout.derive()` reads it out of the
  configuration: one **band** per `Discipline Branch Config` row — i.e. per (branch,
  discipline) — `rooms_num` numbered **rows**, one **lane** per active `Scheduling Role` in
  that discipline, and one stacked **section** per `Shift Type`, ordered by
  `Shift Type.start_time`. A band is drawn only in the sections its config's `shift_types`
  actually list. So there is no layout file to keep in sync, and an unstaffed room is a
  blank row while an uncovered role is a blank column — which is the diagnostic.
  Lane order is `layout.lane_sort_key`: `Scheduling Role.display_order_key` (Int, default
  0, so a negative value pulls a role ahead of every unordered one), then `max_rooms`
  descending, then name. Left alone it is a property of the site's data rather than a
  claim this repo makes about anyone's job; the key is how a site overrides it.
  `source.infer_role` breaks its ties on the *same* key, so an inferred role lands in the
  leftmost lane the employee could plausibly have worked.
- Anything no band claims (a role with no config, a branch with no config, a Shift Type the
  config omits) lands in an **`Unplaced`** band with the reason stated. The chart never
  quietly loses somebody.
- With a run, the cells are a **diff against the books** — `kept` / `added` / `dropped`,
  plus `changed` ("was at …") on a kept half-day the run moved. `chart.merge` matches on
  `(employee, date, shift_type)` and deliberately **not** on role: a Shift Assignment
  records no role so `source.infer_role` guesses one, and matching on the guess would
  report a re-plan every time it disagreed with the solver.
- All seven days are always drawn; weekends and `Holiday List` days are dimmed, and days
  outside the run's own `planning_days` window are dimmed differently — an empty Sunday is
  nothing, a day the run never considered is a scope question, an empty working day is a
  finding. People on approved (or speculated) leave get a strip under the chart rather than
  a cell: they are the answer to "why is this chair empty".
- Split like the optimizer package: `chart.py` is Frappe-free (dataclasses + placement,
  covered by `tests/test_wallchart.py`), `layout.py` and `source.py` read the DB, `api.py`
  holds the whitelisted `get_week_chart(week, run, mode)`. Cells print initials —
  `Employee.custom_initials` where zawin2frappe has installed it (read, never shipped
  here), otherwise derived from the name, so it works on a bench that has never seen the
  import.
- Generalized from `cmdb_frappe/planning/`, which stays where it is: that sheet's bands,
  its practitioner/assistant tandem and its numbered chairs are one practice's paper.

**Run statistics** (`OptimizerRun.get_run_statistics()` + `autoshift/public/js/run_stats.js`,
namespaced `autoshift.run_stats`, same load-once/share pattern as the schedule grid) — the
answer to "is this schedule actually full, and if not, why". Now the **Statistics** pane of
the shared schedule view rather than a block above the grid. Reports room-slots
staffed vs. configured (headline tiles), per-discipline coverage meters, a
discipline x day grid, employees below their FTE target, and each rule's share of the
objective (`objective_breakdown`, persisted as JSON by the solver from
`RuleContext.objective_contributions`; the shares sum to the objective value —
`test_objective_contributions_attribute_and_sum_to_the_objective` pins that invariant).
The meters carry a **role-supply bound** (`_role_supply_bounds`): the scarcest Scheduling
Role's total room-slot supply over the horizon, since `room_coverage` takes the *minimum*
over a discipline's roles, so one short-staffed role caps the whole discipline no matter
what the ruleset says. Where that bound is below configured capacity it is drawn as a
marker on the meter and stated as a warning — this is normally the real reason a schedule
looks empty, and it is not something the ruleset can fix.

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
   the term is scaled by the ruleset row weight. `BuiltinRule.requires`/`.excludes` (pairwise,
   hand-authored) and `.group` (a named choice set — `check_ruleset` throws if a ruleset
   selects more than one member) are code-side dependency-graph metadata, built-ins only;
   `BuiltinRule.default_weight` is the weight a *freshly seeded* ruleset row gets (only
   meaningful on Objective/Mixed rules); the seeding never overwrites a weight already on a
   row, so hand-tuned rulesets keep their own figures and changing a default needs a patch
   (`set_room_utilization_default_weight`) to reach existing sites.
   `use_existing_assignments` and `weigh_assignments_objective` share `group="existing_assignments"`
   — the old `disregard_assignments` run field's `Use`/`Weigh` choice, with `Ignore` now simply
   "neither rule selected" rather than a third state. `bind_role_assignments` deliberately
   sits *outside* that group (it is scoped by role data, not a fourth global policy) so it
   composes with either member. Custom Code rules carry none of this
   metadata and are exempt from every check.
   **`apply_rules` reorders the specs** via `order_specs` — a stable topological sort over
   `requires`. `_load_rules` hands them over sorted by *document name* (for `input_hash`
   stability), which is not the dependency order: `warm_start`'s title sorts last, so every
   `fixValue()` rule that depends on it used to run first and silently do nothing
   (`pulp.LpVariable.fixValue` is a no-op while `varValue is None`). That bug made
   `use_existing_assignments` and `leave_blocklist` inert on every ruleset-driven run —
   people on approved leave were being scheduled. The unit tests missed it because `pkg()`
   sets no `rules` and the legacy fallback happens to yield definition order; the regression
   tests now build specs from the real document titles (`titled_specs`).
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
   `{Solved, Failed, Approved, Committed}`. Persists the solution as `solution_table` (`x`),
   `coverage_table` (`active_rooms`) and `objective_breakdown` (per-rule objective shares).
   `build()` returns its `RuleContext` as a fifth element so the breakdown can be evaluated
   against the solved variables — update the sandbox/test call sites if you change that shape.
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

## Role binding (settled schedules)

Some roles' holders set their own schedules — a fact about a practice's power structure, so
**nothing about which roles those are belongs in this repo**. autoshift ships the mechanism,
defaulting to off; `zawin2frappe` populates `Scheduling Role.assignments_binding` and
`Employee Scheduling Role.binding_override` from the `cmdb_frappe` profile, and the
recency-biased statistical inference of *whether* a given person's schedule has actually
settled lives there too.

Semantics are **freeze-completely**, not "honor what exists": for a bound `(employee, role)`
pair the `bind_role_assignments` rule calls `fixValue()` on *every* one of their variables,
so `warm_start`'s 1/0 initialization pins their existing shifts on and everything else off.
A day they have nothing on the books stays empty — filling those gaps is the free-seat /
chair-auction question, deliberately out of scope. The loader resolves the pairs into
`DataPackage.binding_pairs`, and `_load` prefers a binding role when a Shift Assignment's
role is ambiguous (an employee holding two roles in one discipline), so a settled schedule is
attributed to the role that is actually settled rather than to whichever sorts first.

**Leave wins.** The loader drops any existing assignment falling on a leave-blocked day
instead of adding it to `forced` — forcing both would be infeasible — and records it in
`DataPackage.binding_conflicts`, which `get_run_statistics()` surfaces as a warning
(`run_stats.js` already styles `severity: "warning"`, so this needed no JS). This applies to
every employee, not just bound ones: it is the same physical contradiction, and it retires
the unconditional `ValueError` `warm_start` used to raise for it. That raise is kept as a
defensive invariant for hand-built packages in tests and `sandbox/`.

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
- **Free-seat / chair auction** (no issue filed yet). Role binding freezes a settled schedule
  completely, so a bound holder's empty day stays empty. Attributing those free seats — by
  auction or otherwise — is an active lead, but no design work has started.
- **Dependency-graph inference for Custom Code rules** (no issue filed yet). `requires` /
  `excludes` / `group` are built-in-only, hand-authored in `rules.py`; a Custom Code rule
  declares none of it, so a user ruleset combining custom rules gets no compatibility
  checking at all. Backlog idea: statically introspect a Custom Code rule's `ctx.data.*` /
  `ctx.x` access to suggest (not enforce) likely conflicts. No design work started, but
  **the value went up** once Studio started rendering `requires` as nesting and `excludes`
  as disabling: a custom rule currently sits flat and unconstrained in a panel where every
  built-in visibly declares what it depends on, and it is the one way left to reach a
  ruleset the panel cannot otherwise express. Inferred metadata would feed straight into
  the existing `index_catalog` / `blocked_reasons` machinery.

## Why a schedule used to come out half-empty (2026-08-26)

Three things suppressed fill. Measured on a 1-week, 2-shift, 51-employee run with 140
configured room-slots; the run-statistics panel above was built to make them visible, and
it is what isolated them. **Two are fixed**; the third is a real-world limit the panel now
reports rather than hides.

1. **Fixed — the FTE ceiling formula was a true divide.** `_fulltime_shifts_in_period` used
   `1 - d.weekday() / 5`, which ramps down across the week (Mon 1.0 ... Fri 0.2, Sun -0.2)
   and totals **3.0** for a Mon–Fri week. The intent was `1 - d.weekday() // 5` — 1 on a
   weekday, 0 at the weekend, so **5**. Every employee was capped at ~60% of their real
   availability. One shift per working day is the attainable maximum, not half of one:
   `one_shift_per_day` already allows only one shift a day whatever the shift types are.
2. **Fixed — room utilization could not outbid the cost of an assignment.**
   `shift_preference_objective` contributes `(-1 + pref) * x`, i.e. `<= 0` for *every*
   assignment (uniform `pref` is `1/N`). That is deliberate: it doubles as a rough
   cost-to-company proxy until that becomes its own rule, so it stays at weight 1. But
   `room_coverage` takes the *minimum* over a discipline's roles, so opening one room costs
   two or more assignments — at weight 1 the room reward broke even and the solver mostly
   declined to schedule anyone (**6 of 140** room-slots, 2 of 51 employees). Room
   utilization now declares `default_weight=3.0`, matching the working calibration that one
   objective point is loosely ~100 CHF/h.
3. **Not a bug — role supply genuinely caps coverage.** Coverage is the minimum over a
   discipline's roles, so the scarcest role sets the ceiling. Before the fixes all three
   disciplines sat at *exactly* their role-supply bound (39/39, 7/7, 6/6): the solver was
   already doing everything possible. No ruleset can beat this; the panel states it as a
   warning with the limiting role named.

Combined effect on that run, under the **Standard Ruleset**: 6/140 -> **82/140** room-slots
(154 assignments, 50 of 51 employees scheduled), with Omnipractice now fully staffed at
60/60 and capacity-bound rather than supply-bound. The residual gap is item 3 — Orthodontics
12/60 and Sterilization 10/20, both at their role-supply ceiling.

Still open: nothing spreads coverage across days, so a supply-starved discipline clumps
(that run left Omnipractice at 0/6 one weekday and 6/6 on three others).

`tests/test_optimizer.py::test_objective_less_ruleset_assigns_nobody` **fails on a clean
checkout**, unrelated to all of the above: with a constant-zero objective every feasible
point is optimal, so CBC returning one assignment is legal — "no reason to assign" is not
"will not assign". Decide whether the intended semantics need a small anti-assignment
epsilon, or whether the assertion should just be relaxed.

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
  planning-day generation, hashing, every MILP constraint group; `tests/test_wallchart.py`
  is the same bargain for `wallchart/chart.py` (placement, overflow, the run-vs-books
  merge). The doctype-level
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
