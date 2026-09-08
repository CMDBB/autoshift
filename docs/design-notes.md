# Design notes

Why things are the way they are. Nothing here is needed to *understand* the current code —
`CLAUDE.md` covers that, and `README.md` covers using the app. This file exists so the
reasoning behind a decision survives after the decision stops looking surprising, and so
neither of those two files has to carry it.

Ordered roughly by how likely you are to need it.

---

## Why a schedule used to come out half-empty (2026-08-26)

Three things suppressed fill. Measured on a 1-week, 2-shift, 51-employee run with 140
configured room-slots; the run-statistics panel was built to make them visible, and it is
what isolated them. **Two were fixed**; the third is a real-world limit the panel now
reports rather than hides.

1. **Fixed — the FTE ceiling formula was a true divide.** `_fulltime_shifts_in_period` used
   `1 - d.weekday() / 5`, which ramps down across the week (Mon 1.0 … Fri 0.2, Sun −0.2)
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
   already doing everything possible. No ruleset can beat this; the statistics panel states
   it as a warning with the limiting role named (`_role_supply_bounds`), because it is
   normally the real reason a schedule looks empty.

Combined effect on that run, under the Standard Ruleset: 6/140 → **82/140** room-slots
(154 assignments, 50 of 51 employees scheduled), with the largest discipline fully staffed
and capacity-bound rather than supply-bound. The residual gap is item 3.

**Still open:** nothing spreads coverage across days, so a supply-starved discipline clumps
(that run left one discipline at 0/6 on one weekday and 6/6 on three others).

---

## Why `apply_rules` topologically sorts its specs

`_load_rules` hands rule specs over sorted by *document name*, for `input_hash` stability.
That is not the dependency order: `warm_start`'s title sorts last, so every `fixValue()`
rule depending on it ran first and silently did nothing — `pulp.LpVariable.fixValue` is a
no-op while `varValue is None`. That made `use_existing_assignments` and `leave_blocklist`
inert on every ruleset-driven run: people on approved leave were being scheduled.

`order_specs` (a stable topological sort over `requires`) is the fix. The unit tests missed
the bug because `pkg()` sets no `rules` and the legacy fallback happens to yield definition
order; the regression tests now build specs from the real document titles (`titled_specs`).

**If you change how `_load_rules` orders or hashes specs, keep the sort.**

---

## Role binding: freeze completely, or only cap

Some roles' holders set their own schedules — a fact about a practice's power structure, so
nothing about *which* roles those are belongs in this repo. autoshift ships the mechanism,
defaulting to off; `zawin2frappe` populates the flags from the `cmdb_frappe` profile, and
the recency-biased statistical inference of whether a given person's schedule has actually
settled lives there too.

Semantics are **freeze-completely**, not "honor what exists": for a bound `(employee, role)`
pair, `bind_role_assignments` calls `fixValue()` on *every* one of their variables, so
`warm_start`'s 1/0 initialization pins their existing shifts on and everything else off. A
day they have nothing on the books stays empty. Filling those gaps is the free-seat / chair
auction question, deliberately out of scope.

### Why the *soft* rule is the default

Freeze-completely has the same failure mode as `use_existing_assignments`, on a smaller
population: a settled week that is illegal under the rest of the ruleset — two half-days on
one date, a week over the FTE ceiling, a branch the config no longer covers — makes the whole
run come back `Infeasible`, and one person's history takes the schedule down with it. That is
what `conflict_scan` exists to explain, and explaining it is not the same as producing a
schedule.

`soft_bind_role_assignments` keeps the half of the semantics that is a policy statement and
drops the half that is a hostage: everything a bound holder does **not** have on the books is
still fixed to 0 — nobody quietly grows a schedule that was never the planner's to set — while
the shifts they do have stay free variables sitting at their warm-start value of 1. Under the
standard objective a settled shift that staffs a room pays for itself and is kept; the one
that cannot coexist with the ruleset is dropped, and the rest of the practice still gets a
schedule. `binding_conflicts` and the statistics warnings already exist to make a dropped day
visible.

The two are one **choice group** (`role_binding`), not an `excludes` pair: they are the same
policy at two strengths, "neither" is a legal answer, and Studio renders a group as radios
plus an explicit *None*. `binding_rule_gap` therefore asks whether *any* member is selected
(`rules_in_group`), not whether the strict rule is.

**The warm start is currently advisory only.** `solver.py` calls CBC without
`warmStart=True`, so `setInitialValue` reaches the solver for nothing but `fixValue()`'s
benefit; what actually keeps a settled shift under the soft rule is the objective. Adding
`weigh_assignments_objective` buys an explicit epsilon tie-break toward the books. Passing
`warmStart=True` would make the initial values a real incumbent, but PuLP would then write a
MIPSTART missing every `active_rooms` and auxiliary variable, so it is a separate decision.

**Leave wins.** The loader drops an existing assignment falling on a leave-blocked day
rather than adding it to `forced` — forcing both would be infeasible — and records it in
`DataPackage.binding_conflicts`, surfaced as a statistics warning. This applies to every
employee, not just bound ones: it is the same physical contradiction. It retires the
unconditional `ValueError` `warm_start` used to raise, which is kept only as a defensive
invariant for hand-built packages in tests and `sandbox/`.

**Why the loader still throws for a bound employee** whose existing assignment cannot be
placed (no branch or discipline on its Shift Location, or no Scheduling Role in that
discipline): their schedule is *input*, and quietly dropping a day of it would freeze them
to a week they do not work. For everybody else it records the reason in
`DataPackage.unresolved_assignments` and carries on.

### Why everyone else's books are disregarded by default

`use_existing_assignments` ("Honor existing Shift Assignments") **left** `STANDARD_RULES`.
Pinning every employee to the books made most historical weeks *infeasible* under the rest
of the ruleset — weeks worked short-handed, double-booked or off-config — which is what the
wall chart surfaced once Studio made those weeks easy to look at. The rule still exists and
Studio still offers it; the books are otherwise a `warm_start` tie-break, and only people
whose schedule is genuinely not the planner's to set are frozen.

The `rebind_standard_ruleset_to_settled_schedules` patch re-runs the seeding, which syncs
the Standard Ruleset's rows to that set and preserves every surviving row's hand-tuned
weight.

---

## The FTE ceiling: a law in one country, a courtesy in the same one

Statutory limits on working time are written against a **full-time week**. Scaled to a
part-timer they stop binding long before their agreed percentage does: somebody at 40% can
work two extra half-days and still be nowhere near anything a labour inspector cares about.
What their agreed percentage *is* is a promise the practice made — worth keeping, and worth
breaking before the schedule is.

So `fte_ceiling` (105% x the FTE-derived target, hard) and `fte_soft_ceiling` (a per-shift
penalty on the excess, no cap) are one **choice group**, `workload_ceiling`. Same figure,
two readings, and "neither" is legal — the same shape as `role_binding`, and for the same
reason: a hard reading that cannot be satisfied takes the whole run down with it, and
`Infeasible` is a worse answer than a schedule where one person is a half-day over.

- **One-sided linearization.** `max(0, assigned - target)` needs one non-negative variable
  bounded below by `assigned - target`; the negative objective coefficient squeezes it to
  exactly that maximum. `role_fte_target_objective` needs *two* slacks and an equality
  because it penalizes deviation in both directions; this rule does not care about working
  under, which room utilization and the agreed role split already push against.

- **`default_weight=4.0`, calibrated against room utilization.** One more assignment opens
  at most one more room (worth 3) and costs the ~1 the preference objective charges per
  assignment, so a penalty above ~2 makes the courtesy hold wherever the schedule has any
  other way to staff that room — while still yielding rather than failing when it does not.
  Lower the weight to let coverage outbid the courtesy; raise it to approach the hard cap.

- **The hard rule stays standard.** Practices whose agreed percentages *are* contractual
  want the cap, and it is the behaviour every existing ruleset already encodes. Picking the
  soft one is a deliberate policy statement about a particular practice.

- **`_role_supply_bounds` assumes the hard reading** (statistics' "at most" marker). Under
  the soft rule the figure can be exceeded outright, which the "at most" wording tolerates;
  making the marker rule-aware was not worth the coupling.

---

## Why `autoshift/rota/` exists at all

A settled week is a rule, and stock HR has somewhere to put a rule: a `Shift Schedule`
(shift type + frequency + weekdays) plus a `Shift Schedule Assignment` joining it to a
person. HRMS's nightly `process_auto_shift_creation` is supposed to turn those into the
`Shift Assignment` records everything here reads — and **cannot, for any cycle longer than a
week**.

`create_shifts` takes its week boundary from `create_shifts_after`, then overwrites that
field with the last *shift's* end date, so the nightly job resumes mid-pattern and the cycle
collapses toward weekly. Measured by zawin2frappe: `Every 4 Weeks` firing on weeks 0, 4, 4,
5, 8, 9, 10, 11, 12. So zawin2frappe emits rotas `enabled = 0`, `shift_status = "Inactive"`,
tagged `DO NOT ENABLE` — leaving the people whose schedule is *least* the planner's to set
with no Shift Assignments for any week the import did not already cover, and
`bind_role_assignments` freezing them against exactly that emptiness.

`autoshift/rota/` expands the schedule itself. **It is a workaround for someone else's bug
and is signposted as one throughout.** The upstream issue is not in active development,
which is what makes it worth carrying; the day `create_shifts` anchors its weeks properly,
this package is deleted and `enabled = 1` does the same job.

Consequences that look arbitrary without that context:

- **ISO weeks.** `cycle.occurrences` counts Monday-based weeks where `create_shifts` chops
  arbitrary seven-day blocks. The two agree whenever the anchor is a Sunday, which is what
  zawin2frappe's phase anchoring produces.
- **`create_shifts_after` is never written.** It is the phase anchor as well as the handover
  boundary (nothing is generated on or before it — those records are the import's), and
  moving it *is* the upstream bug. Idempotency comes from comparing against the books
  instead, which needs no high-water mark.
- **`enabled` / `shift_status` are ignored.** They are HRMS's switches for HRMS's generator,
  and a rota is off precisely because that generator would run it wrongly.
- **A day carrying the *other* half-day counts as covered, not as a conflict**, under
  `HR Settings.allow_multiple_shift_assignments = 0`. The schedule's AM/PM label is fitted
  from history; the record on the books is the record.
- **One record per day, one savepoint per row** — a refusal costs that day, not the span.

**Creating records before a solve is not optional**: binding freezes people against exactly
those records, so declining would silently re-plan the one group whose week is settled.

---

## Rota editor: why edits are gold standard, and why periodicity is derived

**Gold standard, never a patch.** Editing an assignment replaces it wholesale with a fresh
`Shift Schedule Assignment` (+ a private `Shift Schedule`), tagged `custom_manually_edited`.
That tag is the whole mechanism keeping this app and zawin2frappe from fighting over the
same record — **zawin2frappe's import must skip any row already carrying it**, enforced
there, not here. A shared, zawin2frappe-owned `Shift Schedule` is never edited or deleted,
only unlinked; a private schedule an edit empties out is cancelled and deleted.

**Periodicity is derived, not identity.** A group is keyed on `(employee, shift_type,
branch)` alone — no cadence, no anchor. Every member `Rota` is resampled into a
`phase -> weekdays` map over `view_weeks` before any change lands, and `edit.minimal_cycle`
reads back the smallest cadence that map still needs once the batch is folded in.

This is the entire mechanism for auto-detecting a periodicity change. Editing one occurrence
in a view wider than the pattern's current cadence — deleting only the second week's Friday
of what was a 1-week rota, seen in a 2-week view — is *how* that rota becomes a 2-week one.
There is no separate "make this a rota" action; `apply_changes` just notices the phases
stopped agreeing, and demotes back to weekly just as readily if a later edit makes them
agree again.

**Why the view-width rule is a predicate, not a rendering trick.** A `Rota`'s weekday set
never varies from cycle to cycle — `cycle_weeks` only skips weeks, it never changes which
weekdays are worked — so a genuinely varying multi-week pattern is several
`Shift Schedule Assignment`s at different phases, exactly what zawin2frappe emits. At a view
`view_weeks` wide, a rota is editable exactly when `view_weeks % cycle_weeks == 0`. A weekly
rota then renders identically in every week of a wider view with no special-casing, while a
four-week rota in a one-week view would show only whichever phase that week lands on —
indistinguishable from "this person works two days a week", which is why such an employee is
shown read-only instead.

Their cells show `edit.phase_fractions` rather than that one ambiguous phase: the fraction
of the pattern's own cadence that puts them there, averaged over one full cycle. Stable
under navigation, since sampling any `cycle_weeks`-long span of a periodic pattern gives the
same per-weekday counts.

**Why `apply_draft` takes the view explicitly** rather than persisting one on the draft: Apply
must fold the batch exactly as the grid and transcript on screen already show it.

**Why draft rows are cleared before the assignments they reference are deleted:** a live Link
blocks the delete otherwise.

---

## Optimizer Studio: a failing ruleset should be unreachable, not rejected

The rule-toggle panel is designed so all three of `check_ruleset`'s failure modes are
*structural*:

- a choice `group` is rendered as radios (≤1 member selectable);
- `requires` becomes **nesting** — `index_catalog` builds a forest, a child is drawn inside
  the rule it requires with its checkbox disabled until the parent is on, so the parent reads
  as a fieldset;
- `excludes` disables and unchecks its targets.

Blocked rows are dimmed with a `title` naming the blocker. The decision half
(`blocked_reasons(checked)`) is deliberately DOM-free so the invariant is testable on its
own; `sync_dependencies()` applies it to the DOM and re-runs to a fixpoint, since unchecking
a parent can orphan a grandchild.

**Grouped rules are the one thing not nested.** `existing_assignments`' members have
different requirements, so nesting would split the radio set; their dependency is enforced
dynamically instead.

**Rows nest, so every DOM read must be scoped to a row's own controls**
(`own_toggle`/`own_weight`). A plain `.find()` reaches into child rows and reports a parent
as selected whenever any descendant is.

**Why Studio duplicates mode/date in its toolbar** rather than being an Optimizer Run form
tab: it abstracts *over* runs, it does not edit one.

**Why every preview overwrites one ruleset per user** (`Studio Draft — <user>`): a ruleset
per click would proliferate. A system preset is never edited directly, only copied into the
draft.

**Why the binding-gap confirm happens before the solve, not after:** a settled schedule that
no rule enforces is silently re-planned, and nothing downstream would show that.

---

## Wall chart: derived layout, and why the diff ignores role

**The layout is derived, never declared.** `layout.derive()` reads it out of the
configuration — one band per `Discipline Branch Config` row, `rooms_num` numbered rows, one
lane per active `Scheduling Role` in that discipline, one stacked section per `Shift Type`
ordered by `start_time`. So there is no layout file to keep in sync, and an unstaffed room is
a blank row while an uncovered role is a blank column — which is the diagnostic.

Lane order left alone is a property of the site's data rather than a claim this repo makes
about anyone's job; `Scheduling Role.display_order_key` is how a site overrides it.
`source.infer_role` breaks ties on the *same* key, so an inferred role lands in the leftmost
lane the employee could plausibly have worked.

**`chart.merge` matches on `(employee, date, shift_type)` and deliberately not on role.** A
Shift Assignment records no role, so `source.infer_role` guesses one; matching on the guess
would report a re-plan every time it disagreed with the solver.

**Why an `Unplaced` band exists** rather than dropping unplaceable people: the chart must
never quietly lose somebody.

**Why all seven days are always drawn, dimmed three ways:** an empty Sunday is nothing, a day
outside the run's `planning_days` is a scope question, an empty working day is a finding.

**Why people on leave get a strip instead of a cell:** they are the answer to "why is this
chair empty".

Generalized from `cmdb_frappe/planning/`, which stays where it is: that sheet's bands, its
practitioner/assistant tandem and its numbered chairs are one practice's paper.

---

## Diagnosing infeasibility: three instruments, not one

Binding turned "Infeasible" from a rare modelling slip into the routine answer. The books
are the practice's real history, and pinning them makes the model assert that history is
legal under a ruleset that was never written with it in mind. CBC's entire contribution to
that conversation is the word `Infeasible`: no row, no variable, no hint. `autoshift/optimizer/diagnostics.py`
exists to answer the follow-up question.

CBC computes no IIS (irreducible infeasible subset), so there is nothing to just ask it for.
Three instruments instead, deliberately in increasing cost, because the cheapest one answers
the common case:

- **`conflict_scan` — no solver at all.** Replays the pinned assignments against the
  *arithmetic* of the selected constraint rules. Two shifts pinned on one date under
  `one_shift_per_day`; more pinned shifts than 105% of an FTE target; two branches in one
  slot. These need no search: an assignment pinned to 1 either fits under the ceiling or it
  does not. It names the Shift Assignment, in the planner's vocabulary, which is what a
  planner can actually act on. Rules whose constraints cannot be the infeasible one are
  deliberately absent — `room_coverage` is `>=` against a variable with a zero lower bound
  and always has a satisfying assignment.

- **`model_dump` — the shape.** Variables and constraints grouped by the rule that emitted
  them, off the `_cname`/`_vname` `prefix:` convention that already existed for the
  sandbox's `constraint_frame`. Its most useful column is *fixed*: how much of the model
  binding has turned into constants. `write_lp` is the escape hatch when only every row
  will do.

- **`elastic_analysis` — the general instrument.** Adds a non-negative slack to every
  constraint and minimizes the total. Every model is feasible once elasticized, so the
  slacks that come back non-zero *are* the infeasibility, sized in the units of the
  constraint they broke. It finds conflicts the scan cannot reason about (anything needing
  search, or a Custom Code rule, which declares no metadata to scan). Variable **bounds are
  left rigid on purpose**: the binding rules express themselves as `fixValue()`, so relaxing
  bounds would relax away the very thing under suspicion. It runs on the LP relaxation by
  default — a conflict between frozen assignments is a conflict between constants, and
  branching does not resolve it — and escalates to `integral=True` only when the relaxation
  reports nothing and the real model is still infeasible.

**Why it runs automatically.** A Failed run's Solver Log gets the report appended
(`solver._explain_failure`, on `Infeasible`/`Undefined`/`Unbounded` and on the exception
path where the model would not even build). Diagnosis on demand is diagnosis nobody runs;
the Solver Log tab is already the place a planner looks after a failure, and it is reachable
in Studio too. It is capped well under the sync budget and can never raise — a broken
explanation must not replace the failure it explains.

**`lp_relaxation` / `shadow_prices` are the same machinery pointed at a solved model.**
`LpConstraint.pi` and `LpVariable.dj` are `None` for a MILP; CBC only reports duals for an
LP. So the relaxation is a prerequisite for asking which ceiling is the one actually holding
the schedule back — one more unit of `fte_max:<employee>` is worth exactly one staffed room,
say. The sandbox's `relaxed_solve` exists so `constraint_frame`'s long-empty `pi` column
finally has values in it.

---

## Smaller decisions worth not re-litigating

- **`x` is sparse over the `(employee, role)` pairs each employee actually holds.** Role
  eligibility is therefore structural, not a rule: a variable for a role somebody cannot work
  does not exist, so nothing has to forbid it, and the model is no larger than before roles.
  This is the one scheduling policy *not* switchable from the ruleset UI. Same precedent as
  `active_rooms`, whose branch room cap lives in the variable's `upBound`.

- **`Employee Scheduling Role` is a standalone doctype, not a child table**, so
  `zawin2frappe` can import into it directly.

- **The binding rules sit outside the `existing_assignments` choice group.** They are scoped
  by role data, not a fourth global policy, so either composes with either member of that
  group. They form a choice group of their own (`role_binding`) instead.

- **Zero-staffed rows are kept in `Optimizer Run Coverage`** — an empty slot is the thing a
  planner needs to see. Runs solved before that table existed still report coverage via
  `_derive_coverage_matrix`, verified to match the persisted table exactly on a re-solve.

- **Statistics/Roster tabs are disabled with a tooltip rather than hidden** — a tab that
  vanishes reads as a missing feature. Solver Log needs only a run, so a Failed run's log is
  reachable, which the old solved-runs-only guard made impossible.

- **Bulk Employee Settings' workers are module-level functions, not methods**, so
  `frappe.enqueue` pickles a reference rather than dragging the Document through. **The
  inline path reports its result in the response**, not over realtime: a bench whose socketio
  is down — a common dev state — otherwise leaves the caller with no feedback at all, which
  reads as the tool having silently done nothing. A large selection also falls back to inline
  when `autoshift.utils.background_workers_alive()` finds no worker, since the queued job
  would otherwise sit in redis forever.

- **`dump-dev-data` goes through `get_doc(...).as_dict()`, not `get_all(fields=["*"])`.** The
  latter returns no child rows (it was silently dropping Employee Settings' preference tables
  and the DDBC shift-type selection) and cannot read Singles at all.

- **Keying `Discipline Branch Config` on `(discipline, branch)`** rather than the old
  `(discipline, designation, branch)` removed the duplicate rows that used to make one
  discipline's rows disagree about their Shift Types, so `data_loader` no longer needs the
  `frappe.log_error` warning it once carried.

- **`Scheduling Rule Topic` is orthogonal to `BuiltinRule.group`.** A group is a
  mutual-exclusion choice set `check_ruleset` enforces; a topic constrains nothing. Topics
  bucket only *top-level* rules — a rule nested by `requires` follows its parent, because the
  dependency is the more useful thing to see.

- **`is_system` is a marker, not an edit lock.** Nothing currently stops hand-editing a
  system ruleset; it marks app-curated rather than hand-authored, for future tooling.

- **`default_weight` applies only to freshly seeded rows.** The seeding never overwrites a
  weight already on a row, so hand-tuned rulesets keep their figures — which is why changing
  a default needs a patch (`set_room_utilization_default_weight`) to reach existing sites.

---

## Superseded / removed

- **`disregard_assignments`** (Optimizer Run field, `Use`/`Weigh`/`Ignore`) → the
  `existing_assignments` rule choice group. `Ignore` is now "neither rule selected" rather
  than a third state.
- **`turnover_weight`** (setting) → carried over by the seeding as the Standard Ruleset's
  room-utilization row weight. **Breaking:** non-Standard rulesets were not upgraded.
- **`committed_assignments`** (Optimizer Run field) → removed in `73e98fa` as the first step
  of the link-back redesign; `committer.py` raises `NotImplementedError` until it lands
  ([#5](https://github.com/CMDBB/autoshift/issues/5)).
- **`stats_html`** (Optimizer Run field) → folded into the single `schedule_view_html` pane
  host.
- **Designation as the scheduling axis** → `Scheduling Role`. Designation is payroll data and
  is no longer read.

---

## Ideas with no design work yet

- **Free-seat / chair auction.** Role binding freezes a settled schedule completely, so a
  bound holder's empty day stays empty. Attributing those free seats is an active lead.
- **Dependency-graph inference for Custom Code rules.** `requires` / `excludes` / `group` are
  built-in-only and hand-authored, so a ruleset combining custom rules gets no compatibility
  checking at all. Backlog idea: statically introspect a custom rule's `ctx.data.*` / `ctx.x`
  access to *suggest* (not enforce) likely conflicts. The value went up once Studio started
  rendering `requires` as nesting and `excludes` as disabling: a custom rule now sits flat and
  unconstrained in a panel where every built-in visibly declares what it depends on, and it is
  the one way left to reach a ruleset the panel cannot otherwise express. Inferred metadata
  would feed straight into the existing `index_catalog` / `blocked_reasons` machinery.
- **Automatic NL→code rule generation and explainable decisions**, which `hooks.py`'s
  `app_description` still promises. Rules carry an NL description with an optional
  developer-validated implementation; the generation half has no code.
