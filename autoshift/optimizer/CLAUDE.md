# Optimizer engine (`autoshift/optimizer/`)

Pure-Python where possible, for testability. **`data_loader.py` is the only Frappe-dependent
module.** Covered by `tests/test_optimizer.py` and `tests/test_diagnostics.py`.

- Exact arithmetic of every rule: `docs/formulation.md`.
- Why any of it is the way it is: `docs/design-notes.md`.

---

## Modules

**`types.py`** — `DataPackage`, the engine's only input shape, plus SHA256 `input_hash()` for
caching and `planning_days()` (raises `NotImplementedError` for `"Unbounded"`).

- **Availability is structural.** `employee_days` (sparse, subtractive, omitted from
  `input_hash` when empty) says which days an employee may work where that differs from
  `working_days`; read it through `days_of()` / `available()`. `model_builder` builds no `x`
  or `p` outside it, so no rule forbids a holiday and no diagnostic has to explain a zero.
  It carries **only dated holidays** — a bound employee's rota days off stay with the
  `role_binding` rules, at the strength the ruleset chose.
- **Assignment modes** — `MODE_FLEXIBLE` / `MODE_EXCLUSIVE` / `MODE_COLLATERAL`, with the
  sparse `role_mode` / `role_mode_overrides` dicts and the helpers every rule reads them
  through: `mode()`, `working_roles()`, `collateral_roles()`, `exclusive_roles()`,
  `bound_employees()`, `forced_presence()`.
- **Room gating** is its own sparse dict, `role_gates_rooms` (+ `gates_rooms()` /
  `gating_roles()`), defaulting to "every role gates except a collateral duty" so packages
  captured before the flag read as they did.
- Sparse and **omitted from `input_hash` when empty**, so a site not using the feature keeps
  its cache hits: `employee_days`, `role_mode`, `role_mode_overrides`, `role_gates_rooms`,
  `role_value`, `barebones`, `role_suitability`.
- `resolve_assignment_role()` is here (Frappe-free, unit-tested) — see **Resolving a role**
  below.

**`rules.py`** — constraint groups *and* objective terms as named rules. `BUILTIN_RULES` is
populated by the `@builtin_rule` decorator; `STANDARD_RULES` is the `standard=True` subset the
seeding puts in the Standard Ruleset. See **The registry** below.

**`data_loader.py`** — `load(run_doc)` hydrates a `DataPackage`. See **What the loader
resolves** below.

**`model_builder.py`** — builds the PuLP MILP. Variables:

- `x[employee, role, shift, day, branch]`, built **sparse** over the `(employee, role)` pairs
  each employee actually holds and each employee's own `days_of()`;
- `p[employee, shift, day, branch]` — **presence**: is this person in for that shift at all;
- `active_rooms[discipline, shift, day, branch]`.

The constraints tying `p` to `x` — one working role per presence, collateral duties beside it,
never a presence spent on nothing — are built **here, not in a rule**: they are what `p`
*means*, the same way `active_rooms`'s room cap lives in the variable bound. Rules key workload
and binding on `p`. Constraints and objective both come from `rules.apply_rules`; the maximized
objective sums `ctx.objective_terms` (empty = constant 0, a pure feasibility check).

Room-level assignment's `y[employee, role, shift, day, branch, room_index]` and
`room_active[discipline, shift, day, branch, room_index]` are **not** built here — they are
rule-owned, created only by `room_coverage_matched_rooms`, so a ruleset keeping the pooled
`room_coverage` pays nothing for room identity. That rule ties pooled `active_rooms` to
`Σ_n room_active[...,n]`, so every rule reading `active_rooms` works unchanged under either.

**`solver.py`** — runs CBC (5 s sync, escalating to a 3600 s background job via
`frappe.enqueue(queue="long")` on timeout); caches by input hash against prior runs in
`{Solved, Failed, Approved, Committed}`. Persists `solution_table` (`x`), `coverage_table`
(`active_rooms`) and `objective_breakdown` (`rules.objective_tree`). `solution_table`'s
`rooms` / `room_index` prefer the exact per-holder room numbers `ctx.room_occupancy` measured
over `ctx.room_load`'s linearized estimate, falling back to 0 ("unmeasured") when neither rule
ran — **one figure covers both mechanisms**, so `wallchart/source.py` needs no preference logic.
**`build()` returns its `RuleContext` as a fifth element** so the breakdown can be evaluated
against solved variables — update test call sites if you change that shape.

**`committer.py`** — converts an Approved run into submitted `Shift Assignment` records.
Raises `NotImplementedError` unconditionally; see "Not built yet" in the root `CLAUDE.md`.

**`diagnostics.py`** — Frappe-free. Why a model is infeasible, and what it contains. Three
instruments, in increasing cost (design notes explain why three):

- `conflict_scan` replays `pinned_assignments` **and `pinned_presence`** (binding settles a
  half-day without settling its role) against the *arithmetic* of the selected constraint
  rules, naming the offending Shift Assignment without solving anything;
- `model_dump` / `write_lp` print variables and constraints grouped by the rule that emitted
  them (`_cname`'s `prefix:` convention);
- `elastic_analysis` slacks every constraint and minimizes the total, so the non-zero slacks
  *are* the infeasibility. `INELASTIC_GROUPS` keeps `bind_presence`, the definitional
  `presence_*` constraints and `active_rooms_eq_matched` rigid — they are input or definition,
  and relaxing them blames binding for every collision instead of the rule it collides with.
  The genuine matching constraints (`room_occ_unique`, `room_active_le` / `room_active_ge`)
  stay elastic.

`relax_integrality` / `lp_relaxation` / `shadow_prices` drop integrality so CBC returns duals
(`pi` / `dj` are `None` on a MILP). **`solver.run_solve` appends `report()` to a Failed run's
Solver Log**; `bench diagnose-model` is the on-demand surface.

**`editor_support.py`** — introspects RuleContext/DataPackage for the rule editor's completions.

**`rule_scratchpad.py`** — a drafting space mirroring the rule namespace. **Committed *and*
gitignored**, so local edits don't bubble up.

---

## The registry

**26 built-ins.** Constraints: `one_shift_per_day`, `warm_start` (kind `Other`),
`leave_blocklist`, `use_existing_assignments`, `bind_role_assignments`,
`soft_bind_role_assignments`, `one_branch_per_shift`, `room_coverage`,
`room_coverage_matched_rooms`, `barebones_branches`, `fte_ceiling`, `role_fte_ceiling`,
`exclusive_role_purity`. Objectives: `room_utilization_objective`, `room_value_objective`,
`employee_value_objective`, `synergy_value_objective`, `room_load_objective`,
`collateral_room_value_objective`, `collateral_capacity_value_objective`, `fte_soft_ceiling`,
`role_fte_target_objective`, `shift_preference_objective`, `suitability_preference_objective`,
`role_value_objective`, `weigh_assignments_objective`.

**Seven choice groups** (a ruleset picks at most one member; "neither" is always legal):

| Group | Members (**standard** in bold) |
|---|---|
| `existing_assignments` | `use_existing_assignments`, `weigh_assignments_objective` |
| `role_binding` | **`soft_bind_role_assignments`**, `bind_role_assignments` |
| `workload_ceiling` | **`fte_ceiling`**, `fte_soft_ceiling` |
| `collateral_value` | **`collateral_capacity_value_objective`**, `collateral_room_value_objective` |
| `shift_preference` | **`suitability_preference_objective`**, `shift_preference_objective` |
| `room_coverage` | **`room_coverage`** (pooled, per-role minimum), `room_coverage_matched_rooms` |
| `room_value_choice` | **`room_utilization_objective`** (flat weight), `room_value_objective` |

`employee_value_objective` / `synergy_value_objective` are non-standard and both `requires`
`room_value_objective`. They add a flat, non-compounding bonus — `(-1 + multiplier) *
room_value(...)` — gated by a linearized AND of the room being matched (`room_active`) and the
specific holder(s) occupying it (`RuleContext.room_occupancy`, the `y[...]` family
`room_coverage_matched_rooms` builds).

Mechanics:

- `_cname()` / `_vname()` name constraints and auxiliary variables.
  `role_fte_target_objective` linearizes an absolute deviation with a pair of them,
  `fte_soft_ceiling` a one-sided one with a single variable, `room_load_objective` a convex
  per-holder room-load cost with one-room tranches (convex, so no binaries).
- `compile_custom_rule()` execs Custom Code source expecting `apply(ctx)`.
- `apply_rules()` applies `DataPackage.rules`; **an empty selection means all built-ins at
  weight 1.0** (pre-ruleset behaviour that unit tests rely on). It pre-registers every built-in
  Objective rule, so one contributing nothing this run still reports its 0.
- Objective rules call `ctx.add_objective(expr, path(...))`. The term is scaled by the ruleset
  row weight; `path` is reporting only, folded into the drill-down tree by
  `rules.objective_tree` after the solve.
- `BuiltinRule.requires` / `.excludes` / `.group` / `.default_weight` / `.topic` are
  hand-authored and **built-ins only** — Custom Code rules carry none of it and are exempt from
  every check. `BuiltinRule.check_ruleset` is what `Optimization Ruleset.validate()` runs.
- **`apply_rules` reorders specs via `order_specs`, a stable topological sort over `requires`,
  holding every Custom Code spec back until the built-ins have run — do not drop this.** See
  design notes; dropping it silently made `leave_blocklist` inert.

---

## What the loader resolves

`_availability` reads each employee's calendar from HR via **`rota.holidays.calendars`** —
shared with the wall chart, so the picture and the model cannot drift about which days are
worked. Only the *policy* lives here. It resolves through `applicable_lists` (**not** HRMS's
`get_holiday_dates_between_range`, which resolves a company through `Holiday List Assignment`
only and never falls back to `Company.default_holiday_list`):

- the **company's** weekly offs define `working_days` (their union over employees, so a day
  nobody works is not in the horizon);
- each employee's own **dated** holidays remove their days alone;
- **the books outrank the calendar** — a `forced` day inside the horizon is added back to that
  employee's availability, so a submitted assignment on somebody's holiday can never crash on a
  missing variable or be silently dropped;
- **guarded on resolution, not on row count**: a company with no resolvable list throws, but a
  list that resolves with no rows in this window legitimately means "every day is worked". A
  calendar whose own `from_date`/`to_date` stops short of the horizon warns instead — those days
  come back worked, weekends included.

Also:

- Bound employees' unrecorded rota days come from `rota.materialize.settled_rows`, never from
  records created first. **A solve never writes anything.**
- The ruleset resolves into `(rule_name, builtin_key, custom_code, weight)` tuples — throws on
  unimplemented or unvalidated rules, sorted by name for hash stability.
- Preferences are normalized via temperature-scaled softmax (no weight deviates >50% from
  uniform).
- Binding pairs → `binding_pairs`; leave-vs-books collisions → `binding_conflicts`; unplaceable
  assignments → `unresolved_assignments`, but it **throws** for a bound employee.
- `binding_rule_gap` backs both solve entry points' pre-flight confirm.

### Resolving a role

An existing assignment's role comes from `Shift Assignment.custom_scheduling_role` via
`types.resolve_assignment_role`. The ladder, in order:

1. the recorded role;
2. the single non-collateral role they hold *anywhere* (`working=`, **opt-in** — the one rung
   that can out-vote a mis-filed location);
3. the single role the employee holds in the location's discipline;
4. holding several there, whichever single one of those is `binding` for them (a settled
   half-day is presence, not a choice of role).

**Never a guess between two roles neither of which settles it** — which is what the field exists
to stop. `custom_collateral_roles` rows become extra `forced` combinations on the same slot, and
go down with their host on a leave day.

---

## Role binding

Two rules in the `role_binding` choice group, so a ruleset picks at most one. Both key on the
**employee** (`DataPackage.bound_employees`, anybody holding a binding role) and freeze
**presence**, not assignments: which role a settled half-day is worked in stays the optimizer's
choice among the roles they hold — an `Exclusive` role and a collateral duty excepted, since
neither is a way of spending a shift that something else could substitute for.

- **`soft_bind_role_assignments` (standard)** fixes presence to 0 on every half-day the books
  do not have, and leaves the booked ones free at their warm-start value of 1. The holder is
  never in on a day they don't already work, but a settled week that breaks the rest of the
  ruleset loses a half-day instead of failing the whole solve.
- **`bind_role_assignments` (strict, off by default)** makes each booked half-day an equality
  (`Σ_branches p == 1`) and pins an exclusive or collateral booking's own variable.

A day with nothing on the books stays empty under either. Leave wins over a settled assignment
(the loader drops the collision into `binding_conflicts` rather than making the run infeasible).
autoshift ships the mechanism defaulting to off; `zawin2frappe` populates which roles are
binding. `use_existing_assignments` is **not** in `STANDARD_RULES`.
