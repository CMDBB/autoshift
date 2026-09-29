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

## Presence, role modes and collateral work (2026-09-15)

**Status (2026-09-17): built end to end, import included.**
Landed: the mode and room-gating fields, the role/collateral custom fields and their backfill
patch, `DataPackage`'s modes and presence helpers, the `p` variables and their linking
constraints, every rule reworked onto presence, the two new rules,
`conflict_scan` / `elastic_analysis`, the loader (roles read off the record, modes, gating,
collateral duties), the solved slots' `collateral` flag, and the statistics panel's counting.
Also landed: rotas carry their role and collateral duties onto every Shift Assignment they
materialise, the editor preserves them through a drag, and the wall chart draws a chip over
the rooms it covers and hatches the lines no room is actually open on. zawin2frappe writes the role onto every
Shift Assignment and rota it imports (a person's *primary* role — the agenda records where
somebody stood, never which capability they were exercising), which leaves
`patches.backfill_shift_assignment_roles` responsible only for the records that predate the
field. Still unpriced and unmoded there: assignment modes, room gating and role values are
autoshift configuration a planner sets, and nothing in ZaWin has an opinion about them. Supersedes the
`(employee, role)` keying of role binding described in the next section.

### The observation

Rotas were detected and bound per role, but that is not how the practice works. People
have a mostly fixed *presence* — which half-days they are in — and work one role or another
during it depending on demand. The role still matters, because some roles cannot be swapped
into or out of, and some can be worked on top of another shift. So a rota settles
**presence**, and the role is the optimizer's to choose within limits the role declares.

### Role modes

A Select on `Scheduling Role`, with a blank-inherits override on `Employee Scheduling Role`
(the `binding_override` / `max_rooms` convention):

- **Flexible** (default) — a rota slot in this role means "present". The optimizer may fill
  it with *any* non-exclusive role the employee holds, at any branch.
- **Exclusive** — a rota slot in this role is worked in exactly this role, with no
  collateral duty alongside it.
- **Collateral** — a duty worked *on top of* a shift (e.g. Lead Worker), at the same shift
  and branch. It may also stand alone: a lead whose team is fully staffed may spend the day
  on the lead duty only.

`Scheduling Role.assignments_binding` stays on the role. Its meaning becomes "a rota slot in
this role fixes presence". **Presence is personal:** somebody holding any binding role has
their presence settled on every day, and a non-binding role they also hold only widens which
role can fill a rota slot, never adds days. That also resolves the old *Open* note on
substitutes in binding roles: a substitute row no longer pins anyone to zero.

### The model

A presence variable `p[e, s, d, b]` (binary) is added alongside `x`, and everything hangs off
it:

| Constraint | Meaning |
|---|---|
| `Σ_{s,b} p[e,s,d,b] ≤ 1` | `one_shift_per_day`, now over presence. Never AM+PM: shift loadouts may overlap at another practice, and a genuine double shift is a dedicated Shift Type |
| `Σ_{non-collateral r} x[e,r,s,d,b] ≤ p[e,s,d,b]` | at most one working role per presence |
| `c[e,rc,s,d,b] ≤ p[e,s,d,b]` | collateral needs presence at the same shift and branch, not a host |
| `p ≤ Σ x + Σ c` | no presence without a duty |
| `c[e,·,s,d,b] ≤ 1 − x_excl[e,s,d,b]` | nothing collateral beside an exclusive role |
| strict: `Σ_b p[e,s,d,b] = rota(e,s,d)`; soft: `≤` | binding fixes presence; branch and Flexible role stay free |
| an Exclusive rota slot pins its own `x` | no deviation from an exclusive rota |

`c` is simply `x` over a collateral role (one variable dict; rules tell them apart by mode).
Custom Code rules summing `ctx.x` will count collateral duties — worth a note in the editor
completions.

### Gating rooms is a switch, not a consequence of the mode

`room_coverage` is a minimum over a discipline's roles, so *every* role it counts is a role
no room can open without. That is right for a practitioner and an assistant and wrong for a
lead duty, and the first cut inferred the difference from the mode: collateral roles were
skipped, everything else gated.

`Scheduling Role.gates_rooms` makes it a decision instead, because the inference is wrong in
both directions. A **floater** or an administrative role is an ordinary working role that
opens no rooms; counted in the minimum it would hold every room in its discipline shut
whenever nobody is floating. And a practice that may not run without a lead on site has a
**gating collateral duty** — legal here, warned about on save, since it is the strong reading.

The two questions are genuinely different: the mode says how a shift in the role is *worked*
(can it be swapped, can something ride alongside it), gating says whether a room waits on it.
`DataPackage.role_gates_rooms` is sparse and explicit, with `gates_rooms()` falling back to
the old inference, so every package captured before the flag still reads correctly.
`_role_supply_bounds` (the statistics panel's marker) skips non-gating roles for the same
reason `room_coverage` does: a role no room waits on can never be the scarce one.

Knock-on changes to the existing rules:

- **`room_coverage` counts gating roles only.** By default that excludes collateral duties,
  which would otherwise cap a discipline at however many rooms its leads span.
- **Collateral is valued through the rooms it oversees, never as rooms of its own.** Two
  rules, a choice group — see "What a supervised post is worth" below.
- **FTE rules count `p`**, still at one shift per weekday for 100% (actual-time accounting
  is backlog).
- **Preference cost moves to `p`**; the suitability surcharge `(−1 + pref)(suitability − 1)`
  stays on each `x`/`c`. With no collateral and every suitability at 1, `p = Σ x` and this is
  exactly today's objective.

### Decisions taken while building it

- **Presence is a binary variable, and the linking constraints are structural.** They live in
  `model_builder`, not in a rule, because they are what `p` *means* — a rule could otherwise
  be deselected and leave every workload and binding rule counting a free variable. The cost
  is real: `p` adds `employees × shifts × days × branches` binaries and about three
  constraints each, roughly half again the size of `x`. Continuous `p` in `[0, 1]` would be
  squeezed to the same values by the duty bounds in every ruleset that charges presence, but
  not in one that charges nothing, so it stays integral until that shows up as a solve-time
  problem.
- **A half-day booked at two branches at once is no longer infeasible.** Strict binding used
  to pin both assignments and fail; presence settles *that* somebody is in, so its equality
  (`Σ_branches p == 1`) keeps one and drops the other. Double-booked branches are a data
  error, and the presence model absorbs them the way the soft rule absorbs everything else.
- **`bind_presence` and the `presence_*` constraints are inelastic** (`INELASTIC_GROUPS` in
  `diagnostics`). An equality is the cheapest thing in the model to slack, so elasticizing it
  would blame binding for every collision instead of the ceiling or the leave it collides
  with — the same reason `elasticize` has always left variable bounds alone.
- **`conflict_scan` attributes a pinned presence to a role only where there is exactly one it
  could be** (`_pinned_role_counts`). Binding no longer pins a role, so a settled week that
  breaks an agreed role split would otherwise go unreported for the single-role holders it
  most often describes.

### What a supervised post is worth (2026-09-17)

The first pricing valued a collateral duty at `min(active_rooms, Σ max_rooms · c)` — the
rooms *actually staffed* where it is worked. It is the intuitive reading and it has a tail:
a lead is then worth more where more rooms are running, so the optimizer earns points by
**gathering people into the branches that have one**. That is real supervision economics and
somebody will want it, but it is not this practice's intent, and it is a strange thing to
discover as a side effect of pricing a lead.

`collateral_capacity_value_objective` is the same shape with `active_rooms` replaced by the
branch's *configured* room count, which is a constant and therefore lives in the value
variable's own upper bound rather than in a constraint (the `active_rooms` precedent). The
post is worth the same wherever it is staffed, so pricing it cannot become a reason to move
anybody. It is **standard**; the staffing-scaled rule stays, in a `collateral_value` choice
group with it, for the sites where a busy half-day really is worth supervising more than a
quiet one.

Both remain capped by the duty's own max-rooms figure, and both are worth nothing until
somebody actually works the duty: the cap is a ceiling on a value that still has to be
earned.

### The wall chart shows coverage rather than heads

Two changes, both following from the same arithmetic the solver uses:

- **A practitioner covering two rooms occupies two lines of the band**, which is how the
  paper sheet has always drawn it, and it makes a lane's height the sum `room_coverage` puts
  on the left of its inequality rather than a headcount that happens to look like one. Those
  two lines started out as one chip with a `rowspan`; see "One chip per room" below for why
  they are now two chips.
- **Lines that are not open rooms are hatched.** Coverage is the minimum over the band's
  *gating* lanes, so a line with a practitioner and no assistant is not an open room, and
  the chart no longer implies it is. `dropped` chips sort to the bottom of their lane and
  are left out: what the run sends home does not staff anything, and counting it would
  overstate coverage. (Coverage is now read per row rather than as a count from the top —
  see "An unstaffed room stays a hole" below for the case that forced that.)

The headline total switched to the same measure (rooms fully staffed, not cells occupied),
because a headline that disagreed with the picture under it is worse than no headline.

### Pricing a role, rather than paying a bonus for being scheduled

Room coverage answers what a shift is worth only while a room waits on it. It has nothing to
say about somebody the schedule has no room for, so the optimizer's honest answer for them is
to schedule nothing — a standby or float role is worth zero and costs the usual per-assignment
preference charge, so leaving them at home always wins.

The obvious patch is a blanket reward per assignment, and it is the wrong one: it pays for
*being scheduled* rather than for the work, so it also pays to over-staff a room, to spread
a discipline thin, and to prefer any assignment to none everywhere at once. The thing being
decided is local — is this person, in this role, worth having in — so the price belongs on
the role.

`Scheduling Role.assignment_value` is that price, in the same objective points as everything
else (a staffed room pays 3; an assignment costs about 1 in preference terms). It defaults to
**0**, which keeps the current behaviour exactly: a shift is worth what it staffs. Priced
above roughly 1, a standby role starts winning against leaving somebody unassigned; priced
negative, a role becomes a last resort the optimizer reaches for only when something else
pays for it. `role_value_objective` charges it **per assignment, not per presence**: the
question is what this role is worth, so a priced collateral duty earns its own value beside
the shift it rides on — which is how a lead duty gets paid for at all.

### Spreading room load, and what it cannot promise (2026-09-18)

`room_coverage` credits a multi-room holder with their whole max-rooms figure the moment they
are assigned, so 3+3+0 and 2+2+2 are the same six rooms to it — and the first is one presence
cheaper. Under soft binding that is exactly how a settled holder got sent home: a colleague
able to absorb their rooms made their half-day pay nothing.

`room_load_objective` prices the rooms each holder actually takes: the first is free, their
last allowed room costs the rule weight, the ones in between lie on a straight line. It
re-states coverage over per-holder one-room tranches, so it needs `room_coverage` (and is
redundant with it, harmlessly, once the tranches fill). **No binaries**, which is what makes
the "tranche linearization" cheap here: the cost is convex, so the cheapest tranche always
fills first without anything forcing the order, and given the assignments the tranche LP is
one row of ones — totally unimodular — so the rooms per holder come back whole.

Normalised by `m - 1` so the last room costs `w` whatever `m` is: a room nobody else can
take still opens (its costliest tranche is `w` per gating role against the 3 a room pays).

**What it cannot promise.** Moving a room off somebody at two onto somebody at zero saves
`w/(m-1)` and costs that person's presence charge (~0.5–1). At `w = 1`, `m = 3` that is a tie,
so four bound holders at six rooms still come back 2+2+2+0. Raising `w` pushes the break-even
out but also pulls *unbound* staff in to relieve a colleague — the rule cannot tell the two
apart, because the cost sits on the load, not on whose presence it is. If the requirement is
"a bound holder's settled half-day is kept whenever it can be", that is a price on dropping a
booked presence (a soft-binding concern), not a load cost.

### Storage

HRMS refuses time-overlapping Shift Assignments for one employee even with
`HR Settings.allow_multiple_shift_assignments` on (`validate_overlapping_shifts` checks
timings regardless), so a collateral duty cannot be its own record beside its host. autoshift
owns new custom fields on both `Shift Assignment` and `Shift Schedule Assignment`:
`custom_scheduling_role` (Link) and `custom_collateral_roles` (Table MultiSelect). A
standalone collateral day is a record whose `custom_scheduling_role` is a collateral role.
One presence a day means the HR Settings flag stays off.

This retires `data_loader`'s inference of a role from the Shift Location's discipline, which
guessed by sort order once someone held two roles in a discipline — the normal case now. The
loader still infers where exactly one role is possible and refuses to guess between two.
Records that predate the field are backfilled by a patch using that old inference **once**;
everything imported since carries the role from zawin2frappe.

---

## Role binding: freeze completely, or only cap

Some roles' holders work a fixed week that the plan has to fit around. That was first
modelled as a practitioner privilege, but it has since turned out to be true of most staff.
Which roles, at any one practice, is still site data, so nothing about it belongs in this
repo. autoshift ships the mechanism, defaulting to off; `zawin2frappe` populates the flags
(binding unless its profile opts a job out), and the recency-biased statistical inference
of whether a given person's schedule has actually settled lives there too.

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

## Role substitution: suitability scales the cost, not the reward

A role an employee only *substitutes* in is still a capability — the loader has to create
their variables for it, so it is an Employee Scheduling Role row like any other, with
`suitability > 1`. Keeping it on the one doctype is what lets the Role Matrix be a plain
dense view of that sparse relation; a separate substitution doctype would have made it a
merge of two.

The user-facing meaning is "a divisor for the desirability of the resulting shift". The
existing preference term is not a desirability, though: `(-1 + pref) * x` is ≤ 0 for every
assignment, a cost that doubles as a rough cost-to-company proxy (see "half-empty" above).
Dividing a negative number by the suitability would make a poor substitute *cheaper* than
the holder. So the cost is multiplied instead: `(-1 + pref) * suitability`. Two alternatives were weighed:

- `-1 + pref / suitability` — literal divisor on the reward only. With `pref ≈ 0.5`, even
  suitability 3 costs less than 0.5 extra, so nothing distinguishes "terrible" from "good".
- `pref - suitability` — so harsh that suitability 3 (-2.5) nearly cancels a room's +3.

With the multiplied cost, 1.2 is -0.6 against a holder's -0.5 and 3 is -1.5: a room nobody
else can staff (worth 3 at the default weights) still opens with a terrible backup, which is
what "feasible" was meant to say.

It is a **new rule in a choice group with `shift_preference_objective`**, not an edit of
it: the new one is standard (the seeding swaps the Standard Ruleset row; that one row's
weight resets to the default), and a ruleset that wants to ignore the matrix can still pick
the old one. With every suitability at 1 the two are the same model, and
`DataPackage.role_suitability` is sparse and omitted from `input_hash` when empty, so a site
that never opens the Role Matrix keeps its cache hits.

**Open:** a substitute row inherits `binding_override` like every other row, so on a role
marked binding the substitute is bound too — and with nothing on the books in that role,
both binding rules pin them to zero there. Substitution in binding roles therefore does
nothing yet.

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
- **`create_shifts_after` is never moved forward.** It is the phase anchor as well as the
  handover boundary (nothing is generated on or before it — those records are the import's),
  and moving it by less than a cycle *is* the upstream bug. Idempotency comes from comparing
  against the books instead, which needs no high-water mark.
- **…but it is moved back, by whole cycles (2026-09-18).** The Rota Editor anchored a
  multi-week pattern on whichever week the planner was viewing, and an import anchors where
  its history ended, so rotas could start weeks into the future and be missing from exactly
  the weeks a solve and the wall chart read first. `cycle.backdated_anchor` moves an anchor
  back to at least `ANCHOR_LEAD_WEEKS` (4) before today by a multiple of the cycle, so the
  phase is untouched and only the boundary moves earlier. The cost is that days in that
  lead-in which the books never recorded now read as rota days; the coverage check still
  leaves every recorded day alone. Only `enabled = 0` rows are touched, because on an
  enabled one the field is HRMS's high-water mark and backdating it has HRMS back-fill real
  Shift Assignments.
- **`enabled` / `shift_status` are ignored.** They are HRMS's switches for HRMS's generator,
  and a rota is off precisely because that generator would run it wrongly.
- **A day carrying the *other* half-day counts as covered, not as a conflict**, under
  `HR Settings.allow_multiple_shift_assignments = 0`. The schedule's AM/PM label is fitted
  from history; the record on the books is the record.
- **One record per day, one savepoint per row** — a refusal costs that day, not the span.

### A solve reads rotas; it does not materialise them (2026-09-18)

Until 2026-09-18 both solve entry points created the missing Shift Assignments before
solving, because the loader only read the books and binding freezes people against exactly
those records. That was a leftover of the optimizer having been built to design around
existing assignments, and it is wrong once in production: every preview would submit real
Shift Assignments — notifications included — for weeks nobody has approved, and a re-plan
would have to cancel and amend them.

`data_loader.load` now calls `materialize.settled_rows` for the horizon's bound employees and
treats each unrecorded rota day as the record it would materialise as: same shift type,
location, role and collateral duties, same `(employee, date)` / `(employee, date,
shift_type)` coverage test, same leave-wins and unresolvable-is-fatal-for-a-bound-employee
handling (the message names the Shift Schedule Assignment and day instead of a record). The
consequence worth relying on: **a horizon hashes identically whether or not its rota days
were materialised**, so a run cached before this change still hits.

Records still get written, but only on an explicit request (the wall chart's "Create them")
and, once it exists, at commit — which is where an approved plan is meant to become real.

The wall chart reads the same rows, so it shows the week the solver sees: an unrecorded rota
day is a `virtual` chip on the book side (dotted border, italic), and a run reproducing one
reads as `kept`, not `added` — whether a day is written down yet is a fact about the books,
not about the run. Seeing those chips *before* bulk-creating them is the point, so the
"Create them" offer moved behind an "N not recorded" toggle instead of greeting every week.

---

## Rota editor: why imports are silver standard, and why periodicity is derived

**Mark the guess, not the truth.** This used to be the other way round: a hand edit was
tagged `custom_manually_edited` (gold) and everything else was fair game for a re-import. Two
things turned that over. Fixed schedules turned out to be the norm across a practice's staff,
not something only practitioners have, so the editor stopped being a niche correction tool.
And a legacy agenda, however close to reality, is not a legally binding record of anyone's
contract. The import is a guess that has to be confirmed, and flagging only the hand edits
left a pattern HR typed straight into the Desk looking like one more guess to overwrite.

So `Shift Schedule Assignment.custom_unconfirmed` marks a pattern an importer *inferred*
(silver). Unflagged, which is the default, is gold. **An importer may overwrite only a
flagged row**, enforced in zawin2frappe, not here. A silver pattern turns gold in two ways:

- **Editing it.** An edit replaces the pattern wholesale with a fresh, unflagged
  `Shift Schedule Assignment` (+ a private `Shift Schedule`). Because a group is one
  `edit.group_key`, moving one Tuesday confirms that whole row's weekday set,
  and a move across groups confirms both. The planner was looking at both when they did it.
- **Promote all**, per employee. It stages a `promote` change, which clears the flag in place
  on every silver pattern that person has left (`EditPlan.promote`). Nothing is replaced, so
  the `custom_zawin_key` provenance survives. It is per employee rather than per chip because
  a chip is one occurrence of a pattern, and "confirm this Tuesday" would silently confirm
  every other day in the pattern.

A shared, zawin2frappe-owned `Shift Schedule` is never edited or deleted, only unlinked; a
private schedule an edit empties out is cancelled and deleted. `Shift Schedule` keeps
`custom_manually_edited` for exactly that ownership test. On a schedule the flag says whose
record it is, never whether it is true.

**Periodicity is derived, not identity.** A group is keyed on `(employee, shift_type,
branch, scheduling_role, collateral_roles)` — no cadence, no anchor. Every member `Rota` is resampled into a
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

### The role is part of a pattern's identity (2026-09-21)

`group_key` gained `scheduling_role` and `collateral_roles`. The forcing argument is small
and total: `Shift Schedule Assignment.custom_scheduling_role` is **single-valued**. Two
half-days at the same shift type and branch worked in different roles cannot share a
document, so a grouping that folds them together does not produce a wrong-looking schedule —
it silently re-roles one of them on the way to disk. The same holds for the collateral duties
riding on a pattern, which live in a child table of that one document.

The consequence is that `|Shift Schedule Assignment|` now scales with
(shift type x branch x role x duty set) rather than (shift type x branch), and re-roling one
Tuesday of a Mon–Wed pattern splits one document into two. **That is the intended shape.** A
document per distinguishable pattern is the only one that can record what is actually worked;
the previous count was smaller only because it was recording less.

Cadence deliberately stays *out* of the key — see above. Role is identity because a document
can only hold one; cadence is an outcome because a document's `frequency` is derived from
what the phases turned out to be.

**"Retag" is a move between two groups that differ only in their role.** It reuses
`apply_changes`'s move branch exactly, which is not a shortcut but the meaning: the half-day
stays where it is and the work done in it changes. It is per *occurrence*, like every other
edit here, so "this Tuesday is a lead duty now" does not quietly re-role the Monday too.

### Why the role is resolved on read, not only by a patch

`materialize.load_rotas` resolves a blank `custom_scheduling_role` through
`types.resolve_assignment_role` — the same ladder `data_loader` applies to a
`Shift Assignment` — rather than leaving it to `patches.fill_single_role_assignments`.

The patch alone would have been enough to fill the field in. It would not have been enough to
make the field *reliable*: a site that has not migrated, a pattern the ladder can only settle
once somebody gains a second role, and a hand-entered assignment all leave it blank, and the
editor's discipline scoping is only as good as the attribution behind it. Resolving on read
means the Rota Editor, the wall chart and a solve agree about which discipline a pattern
belongs to without anybody having run anything. The patch then writes down the answer those
readers already give, which is the right order: the reading is the definition, the stored
value is a cache of it.

The ladder gained one rung for this: **the single non-collateral role the employee holds
anywhere**, ahead of the Shift Location's discipline. Somebody with one role has no second
answer, and needing a correctly-filed location to say so was the main thing keeping imported
rotas unattributed. It is opt-in (`working=None` skips it) because it is the one rung that
can out-vote the location — a single-role employee whose record sits at a location filed
under another discipline now resolves to their role rather than failing. That is the intended
reading: a mis-filed location is a data-entry slip, not evidence of a second role.

`RoleContext` takes the horizon from `settled_rows` and applies `data_loader`'s own
validity-window predicate, so a solve can never inherit a role its own `DataPackage` says the
person does not hold over that span — which would only make the loader refuse the row it had
just produced.

### One week, two disciplines

An employee holding binding roles in two disciplines has **one** settled week. Before the
role was recorded there was no way to say which discipline a pattern belonged to, so each
discipline's view showed whatever shift types it happened to share with the other, and
editing from the wrong view rewrote the pattern — with that view's idea of the role, or with
none.

Now `Rota.discipline` comes off the resolved role (falling back to the Shift Location's own
`custom_discipline`), and `editor.is_native` decides what a view may touch. Another
discipline's patterns are **drawn** — faint, dashed, never draggable — because the thing a
planner most needs to see is the half-day their colleague has already spoken for. They are
red on both sides where they collide, using `HR Settings.allow_multiple_shift_assignments` to
decide what "collide" means: with the setting off, which is HRMS's default and what
`materialize._covered` already reads, a person has one shift a day, so *any* two rotas on one
date collide however their shift types are labelled.

An **unattributed** pattern — no resolvable role, no discipline on its location — is native to
whichever view is looking at it. Nobody owns it, so refusing every view the right to edit it
would strand it forever; instead `apply_draft` fills in `_default_role` when the replacement
still names none, which is how such a pattern acquires a role simply by being touched.

Shift Types outside the discipline's `Discipline Branch Config` are drawn as extra read-only
sections rather than dropped. `branches_of` offers no legal drop target on one, so they could
not be edited in this view anyway — but they used not to be drawn at all, which made a
double-booking on an unfamiliar shift invisible rather than merely uneditable.

---

## A rota is an interval, and a changed pattern is succeeded rather than rewritten (2026-09-29)

`Shift Schedule Assignment` has one date on it, `create_shifts_after`, and it carries two
facts: **when the pattern started** (everything up to it belongs to whoever wrote the records
already on the books) and **what phase it is in** (`cycle.first_covered_week` counts from the
week after it, which is how several same-cadence rows sit at different offsets). Two facts,
one field.

That overload is what produced the bug this note replaces. A pattern whose anchor sat in the
future was simply absent from the weeks a planner and a solve look at first, so the app pulled
the anchor *backwards* by whole cycles — `cycle.backdated_anchor`, applied by the editor on
every create and once to existing rows by `patches.backdate_rota_anchors`. Whole cycles keep
the phase exactly, so as a rendering trick it worked. As bookkeeping it was false in two ways:
the record then claimed a pattern had been in force since before it was agreed, and
`materialize` would back-fill `Shift Assignment`s for weeks the person had actually worked
differently (mitigated only by `_covered` skipping days a record already existed for, which
says nothing about weeks that were simply never recorded). Applying an edit then
`frappe.delete_doc`'d the superseded row outright, so even a correct anchor left no trace of
what came before.

Upstream agrees, for what it is worth: `ShiftScheduleAssignment.validate` throws if
`create_shifts_after` changes on a row that already has `Shift Assignment`s past the new date.
The patch only got away with it by writing through `frappe.db.set_value`, which skips
`validate`.

**The fix is a second date, not a cleverer use of the first.** A rota now runs over
`(create_shifts_after, custom_create_shifts_until]`. A pattern that changes is *ended* on the
day before the turning point and a fresh row starts on it; `create_shifts_after` is written
once, at insert, and never touched again.

### Why the end has to be stored

The obvious economy is to skip the custom field and read each row's end off its successor's
start, the way `Holiday List Assignment` works — `from_date <= as_on`, latest wins, no end
date anywhere. It does not survive contact with the phase overload:

- An employee has **one** applicable holiday list at a time, so for HLA "the next one" is
  well defined. They have **many** concurrent SSAs, so succession has to be keyed on
  something — `edit.group_key` is the natural candidate.
- But a cadence longer than a week is *one row per phase within the same key*, and those rows
  deliberately carry **different** anchors a week apart. Under "end = the next
  `create_shifts_after` in this key", a fortnightly pattern's phase-1 row terminates its own
  phase-0 sibling one week in. The rule needs the start date to be a clean generation
  boundary; it is a phase offset.
- Nothing supersedes a pattern that is simply **removed**, and nothing in the old key
  supersedes one that is **retagged** or moved to another branch (both change `group_key`).
  HLA never meets this case because an employee always has *some* holiday list.

Three ways round it were considered and rejected. A weekday-less **marker row** at each
boundary works uniformly (only markers terminate, phase rows do not) but
`Shift Schedule.repeat_on_days` is `reqd = 1`, so it inserts only with `ignore_mandatory` or a
weekday that is a lie, and it costs an extra SSA + Shift Schedule per edited pattern per edit.
Moving **phase into its own field** is still a custom field, and a worse one — an Int with no
meaning outside our arithmetic, against a Date any HR user can read. Restricting to **weekly
cycles** is the one thing this package exists to avoid.

So: `custom_create_shifts_until`, autoshift-owned, blank meaning open-ended. It is less of an
invention than it looks — `ShiftScheduleAssignment.create_shifts(start_date, end_date=None)`
already accepts an end; the doctype simply never stored one.

### The turning point is the view, even when the view is in the past

`edit.apply_changes` ends every superseded row at `view_start - 1 day` and starts every
replacement at `view_start`, because the window on screen is how a planner says *which weeks
they mean*. Editing a window that has already passed therefore really does rewrite from that
date. That is deliberate: correcting a pattern that was wrong last month is a real thing to
want, and the alternatives (clamping silently forward, or refusing) either lie about what the
edit did or remove the only retroactive-correction path there is. `EditPlan.retroactive` says
when it is happening, the editor's Apply dialog says so before the fact and `apply_draft`
`msgprint`s it after.

Two consequences fall out of the same rule:

- **A row with no past is deleted, not ended.** It never generated a day, so a tombstone for
  it records nothing. `edit._runs_before` asks the question of the rota's own arithmetic
  rather than of the books, because a pattern that was in force and happened to generate
  nothing (a fortnight of leave) is still a true statement about what was arranged.
- **A row that already ended before the turning point is left out of grouping entirely**, so
  no later edit can end or delete it a second time.

`_phase_anchor` is now set for **every** cadence, weekly included. It used to be `None` for a
weekly pattern — harmless when nothing was ever ended, since a rota claiming every week there
has ever been overlapped nothing. Once it supersedes a predecessor on a specific day it has to
start there, so the anchor is the day before its own first week (a Sunday, `view_start` being a
Monday — which is also the alignment zawin2frappe's phase anchoring produces).

`cycle.backdated_anchor` / `anchor_cutoff` / `ANCHOR_LEAD_WEEKS` survive as dead-but-documented
code: `patches.backdate_rota_anchors` already ran with them on every existing site. The
anchors it moved are **not** restored — there is no record of what they were, so everything
before today is one epoch and correctness starts here.

### Making an invisible pattern visible, without lying about it

A pattern anchored after the window on screen is now genuinely absent from it. That is
correct and unhelpful at once, so `edit.Change` gained a **`backdate`** op: the editor lists
such patterns above the grid ("2 pattern(s) exist for a later date and are not shown in this
window") with a button that stages a request to start one from this view instead. It goes
through the draft, the transcript and Apply like every other edit — which is the difference
from what it replaces. Backdating used to happen silently, to every row, on every create.

Mechanically it is the one op that touches no occurrence. `apply_changes` re-seeds the
group's `phases` by resampling its members over *their own* future weeks, shifted back by
`min(first_covered_week) - view_start`, then lets the ordinary create path anchor the result
here. The shift is taken over the whole group and in whole weeks, so several rows sitting at
different phases keep their offsets from each other. The future rows are then deleted rather
than ended, since by definition they never ran a day.

---

## Per-employee holiday lists: an export, not a second source of truth (2026-09-29)

Frappe HR charges leave by subtracting holidays from the span applied for —
`get_number_of_leave_days` is `date_diff + 1` minus `get_holidays(employee, from, to)` unless
the Leave Type sets `include_holiday`. That resolves through
`hrms.utils.holiday_list.get_holiday_list_for_employee`, which reads submitted **`Holiday
List Assignment`** documents for the employee and falls back to their company's. So HR
expects a holiday list *per employee*, with their own non-working days in it; an employee
whose week is four days, holding only the company calendar, is charged five days for a week
off.

This app already knows those days, so `rota/holidays.py` derives the list rather than asking
anyone to type it: the company calendar, unioned with every day of the window the employee
does not work. The arithmetic is `rota/calendar.py`, Frappe-free and unit-tested, the same
split as `cycle.py`/`materialize.py`.

Four decisions worth keeping:

- **Read back only in part.** The optimizer takes these lists' *dated* holidays and the
  company's weekly offs; the rota-derived days off written here it deliberately ignores. See
  "Availability is structure" below for why the line falls there.
- **The company's assigned list is the base.** Not `Optimizer Settings`, whose two fields
  were the app's own second opinion about the calendar and are now gone. The base is unioned
  in *verbatim*, not
  supplemented: an employee with any assignment of their own stops falling back to the
  company's entirely, so a public holiday missing from the generated list simply vanishes for
  them. A base holiday also survives a rota that covers that day, since a pattern knows
  nothing about public holidays.
- **Bound employees only.** An unbound employee's week is the optimizer's to decide and
  differs week to week, so there is no pattern to derive from. They keep the company calendar
  and a part-timer among them is charged as if they worked every weekday. Inventing a nominal
  week for them would put a false statement in HR's records rather than an incomplete one.
- **Whole days only, and this is a dead end rather than a gap.** `Holiday` carries
  `is_half_day`, but `get_holidays` returns `len(dates)` and never reads it. An employee who
  works five mornings therefore has no derivable holidays at all, and no per-employee list can
  fix it.

**Succession, and why it needs no end date.** Generation mints and never edits: a changed
rota produces a new `Holiday List` and a new assignment starting at the first date the two
derivations disagree (`calendar.first_difference`), with the old pair left submitted. HLA
resolution is `from_date <= as_on` ordered descending, limit 1, so a successor implicitly ends
its predecessor — the asymmetry with `Shift Schedule Assignment`, which needed
`custom_create_shifts_until` because an employee has one applicable holiday list at a time and
many concurrent rotas. Already-approved leave is unaffected either way:
`Leave Application.total_leave_days` is computed on validate and stored.

Lists are named by a content digest and shared, so a practice of a hundred people on a handful
of patterns produces a handful of documents. The window is the **base list's own** — a
generated list cannot reach past the dates the company has published, and "the calendar was
extended into next year" is then drift that shows up on the next check rather than a range to
invent. Triggered on demand only (the Rota Editor's menu, or `bench update-holiday-lists`),
never as a side effect of applying a rota edit: it submits HR documents.

---

## Availability is structure, not a constraint (2026-09-29)

Per-employee holiday lists had to reach the optimizer once they existed, and the obvious
route — a `leave_blocked`-shaped set and a rule that fixes those variables to zero — is the
wrong one. A public holiday is not a preference the model weighs against something else; it
is a day that does not exist for that person. So it belongs where role eligibility already
lives: in which variables get built at all.

`DataPackage.employee_days` is sparse and subtractive — an entry only where an employee
deviates from `working_days`, `days_of(e)` falling back to it otherwise — so a site where
everybody shares one calendar builds the same model, hashes the same and keeps every cached
run it had. `model_builder` indexes `x` and `p` over `days_of(e)`; every per-employee loop in
`rules.py` follows, and the handful of loops keyed on (discipline, shift, day, branch) that
sum across employees gained an `available(e, d)` guard. Nothing fixes a variable to zero,
nothing has to explain one, and `elastic_analysis` cannot blame a holiday for an
infeasibility because there is no constraint there to relax.

**Optimizer Settings' two holiday lists are gone.** They were the app keeping its own opinion
about which days are worked, next to the one Frappe HR already maintains through
`Holiday List Assignment` — and the bounded one described itself as weekends-only, so dated
public holidays were not honoured at all. `data_loader._availability` now reads HR per
employee. The singleton stays, fieldless, as the place a future global setting would land.

The split inside that read is the load-bearing part:

- **The company's weekly offs** make a week five days rather than seven. Everybody's, so
  they define `working_days` — taken as the union of what each employee has left, which means
  a day nobody can work is simply not in the horizon and the model is no larger than when one
  settings list said the same thing.
- **Each employee's own dated holidays** remove their days and nobody else's.
- Both reads resolve through `rota.holidays.applicable_lists`, which is HRMS's own
  resolution **plus `Company.default_holiday_list`**. HRMS dropped that fallback when
  `hrms.utils.holiday_list.get_holiday_list_for_employee` took the `employee_holiday_list`
  hook over from erpnext's version, so a site that set the company default and never created
  an assignment reads as having no calendar at all through its helpers — and every day of
  the week comes back working. Using its range helper directly is what made the first
  version of this refuse to start.
- **A bound employee's rota days off are not read**, although their generated holiday list
  records them. Two reasons, and the second is the important one. Those lists are regenerated
  on demand, so they lag a rota edit — and lag that deletes variables is lag that breaks a
  settled run. More fundamentally, which days the books put somebody on is what the
  `role_binding` rules decide, at the strength a ruleset picked (`soft_bind_role_assignments`
  proposes, `bind_role_assignments` nails down, `binding_rule_gap` warns when neither is
  chosen). Deleting their non-rota variables would make binding unconditional and quietly
  retire that choice.

**The books outrank the calendar.** A submitted `Shift Assignment` on somebody's holiday is a
fact about a day they worked, so `load` adds every `forced` day back into that employee's
availability before building the package. Without it a stale or wrong calendar would either
crash `bind_role_assignments` on a missing variable or drop a settled half-day in silence.
Only within the horizon, though: a day *nobody* works has no place in the model to put it,
which is the pre-existing behaviour for weekend assignments and unchanged here.

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

**Chip order within a lane is alphabetical unless a role says otherwise
(`Scheduling Role.chip_sort_field`).** The row number was always drawn — a band's rows are
numbered because chairs are (see "room identity" above) — but with nothing to sort by,
alphabetical was the only stable choice, and a stable *label* for the number ("row 2") is not
the same claim as a stable *meaning* ("second-year"). Apprentice lanes are where that gap
shows: the number reads as apprenticeship year to anyone looking at the chart, and alphabetical
order makes that reading wrong by construction, not by mistake. `chip_sort_field` names an
Employee field to resolve per person instead (`source._chip_sort_config`/`_sort_value`); it is
deliberately a fieldname the site types in, not a fixed concept like "seniority", for the same
reason `display_order_key` is a number and not an enum — the meaning of the row is the site's
to declare, this package only offers the mechanism. Resolution happens in `source.py`, the only
module that reads Employee, and rides on the `Slot` as `sort_value` so `chart.py` stays
Frappe-free; `chart._order_lane` puts a `None` value (no field configured, the field vanished
since, or this employee has none) after every ranked one rather than raising, the same "never
silently drop somebody" bargain the rest of the chart makes.

Generalized from `cmdb_frappe/planning/`, which stays where it is: that sheet's bands, its
practitioner/assistant tandem and its numbered chairs are one practice's paper.

### An unstaffed room stays a hole (2026-09-23)

Under `room_coverage_matched_rooms` the solver matches holders into numbered rooms, and
**nothing in the model prefers one room number to another**: the rooms of a band are
interchangeable, so opening rooms 2 and 3 while room 1 stays shut is exactly as optimal as
any other pick of two. That is a property of the model, and the right one — a preference for
low numbers would be a constraint nobody asked for, and the symmetry is also why gh#9 still
warns about solve time.

The chart's first cut compacted a day's distinct room indices into a dense `1..K` before
placing them, on the reasoning that the solver's numbering is an arbitrary label and a gap in
the stack would look like a drawing error. It is the opposite: closing the gap draws rooms 2
and 3 *as* rooms 1 and 2, which is a schedule the solver did not produce, and it sends a
reader who notices room 1 empty on the Optimizer Run looking for a logic bug in the model
instead of at a co-optimal answer. A chart that silently renumbers is worse than one with a
hole in it, because only one of the two can be checked against the run.

So a measured `room_index` **is** the row. Two consequences worth naming:

- **Coverage had to become per-row.** It was the length of the run of lines every gating lane
  reached from the top, which reads 0 the moment room 1 is empty — the whole band would
  hatch. `_coverage` now intersects the gating lanes' *staffed* rows and `Chart.covered` holds
  that set (`open_rows` in the payload). Where no run measured room identity every lane still
  fills from the top, the intersection is a prefix, and the figure is the one the minimum
  always gave — which is why nothing about pooled `room_coverage` runs changed.
- **Dropped chips had to be excluded explicitly.** The old prefix count relied on
  `_order_lane` sinking them below the proposal; with holes allowed, "below" no longer means
  "outside the count", so `_fill` tracks the rows a lane *staffs* separately from the rows it
  *occupies*. A dropped chip still holds its line on the chart and still staffs nothing.

### One chip per room, not one chip spanning rooms (2026-09-24)

The same symmetry that leaves room 1 shut also **interlaces** rooms: nothing ties one
holder's rooms together, so a practitioner in rooms 1 and 3 beside one in rooms 2 and 4 is an
ordinary co-optimal answer. A chip drawn as a `<td rowspan>` over the rooms it covers cannot
express that — there is nothing contiguous to span — and the first cut handled it by refusing
to pin such a slot at all, falling back to incidental fill order. That is the worst of both:
the chart quietly abandons the solved rooms in exactly the case room identity was added for.

So a slot covering N rooms is N placements, one per line, each carrying the room number it
stands for (`Placement.room`). `rowspan` and the `SPANNED` sentinel are gone from the payload
and the renderer. Adjacent rooms are deliberately *not* merged back into one chip: merging
would reintroduce the span case for the sake of a cosmetic gain, and the two chips already
read as one person holding two lines — which is all the paper sheet ever claimed.

The one thing this costs: a chip is no longer a half-day, so anything counting assignments
counts distinct `Slot.match_key` instead of placements (`api._totals`, `chart.build`'s
inferred-role warning). Coverage is unaffected — it was already a fact about lines.

A measured room whose line is already taken in the same lane falls back to the leftover
lines. Only the `Unplaced` band can produce that, by pooling two bands' room 1 into one lane;
dropping the chip would break the one promise this chart makes without exception.

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

## The objective breakdown is a tree the rules build themselves (2026-09-21)

"Room utilization: 3240" is a number, not an answer. The run already recorded each rule's
share of the solved objective; what a planner (and, more often, whoever is tuning a weight)
actually asks next is *where* — which discipline, which branch, which Tuesday morning, and
how many rooms that half-day really opened. The statistics panel now answers that by
opening the share out into a drill-down tree.

The decomposition is done by **the rule that earned the value**, not by a reporting module
downstream of it. A rule is the only thing that knows what its terms mean: room utilization
is per (discipline, branch, day, shift), the FTE courtesy is one number per person and has
no finer grain at all, and the preference charge is per half-day worked. So
`ctx.add_objective(term, path(Discipline=k, Branch=b, Day=d, Shift=s))` labels each term as
it is contributed, and `objective_tree` folds the labelled terms into the tree after the
solve. A rule that passes no path is not broken — it files everything under the empty path
and reports as one total, which is exactly what the flat breakdown did.

Consequences worth naming:

- **Rules contribute many small terms instead of one `lpSum`.** The model is identical —
  the objective is the same sum either way — but a rule now calls `add_objective` once per
  slot. This is what makes the mechanism general: the next objective rule gets its
  breakdown by naming its levels, with no reporting code to extend.
- **`apply_rules` pre-registers every built-in objective rule**, so a rule that legitimately
  contributes nothing this run (nobody staffs a second room, no role is priced) reports the
  0 it scored instead of dropping out of the breakdown. Two rules used to contribute a
  hand-written constant-0 term purely to stay visible; that hack is gone.
- **A node carries no level of its own.** Its level is `levels[depth - 1]` of the rule it
  sits under, because a four-week run over a few dozen people is a few thousand nodes and
  repeating the word "Employee" on all of them is most of the payload.
- **Trimming drops depth, never breadth-first detail.** Breadth past `MAX_CHILDREN` folds
  into one "… and N more" row carrying the rest of the value; a subtree past
  `MAX_NODES_PER_RULE` loses its *deepest* level and re-folds, repeatedly, until it fits.
  Coarse levels answer "where did this come from", so they are the last to go — and the
  levels dropped are named on the node, so the reader is told what they are not seeing
  rather than mistaking the leaves for the finest grain available.
- **The tree is persisted, not re-derived.** The values are only meaningful against solved
  variables, and a solved run is immutable, so there is nothing to recompute against later.
  Runs solved before this landed carry the flat `{rule: value}` map (breakdown version 1)
  and read back as rule rows with no children — the detail is genuinely not recoverable for
  them, and pretending otherwise would mean re-running the rules against a model that no
  longer exists.
- **Employee ids are relabelled in `optimizer_run.py`, not in the engine.**
  `optimizer/rules.py` is Frappe-free and only ever sees docnames; the shared `LEVEL_*`
  constants are what let the reporting layer recognize a level it can put names to.

The panel renders a node's children on first expand rather than up front, for the same
reason the payload leaves levels implicit: the tree is built to be *opened*, one branch at
a time, not to be laid out in full.


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
