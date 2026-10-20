# MILP formulation

The exact arithmetic every built-in rule in `autoshift/optimizer/rules.py` puts into the
model. **Formulas only** — what a rule is *for*, and why it is the way it is, lives on the
rule's own `@builtin_rule` description, in `CLAUDE.md` and in `design-notes.md`.

One section per built-in, in registry order — 26 of them: 12 constraint rules, `warm_start`
(kind `Other`), and 13 objective rules. Constraint names are the `_cname` prefixes the model
actually carries, so a row in `bench diagnose-model`'s output can be looked up here directly.

---

## Notation

### Index sets

| Symbol | Meaning | Source |
|---|---|---|
| `E` | employees | `DataPackage.employees` |
| `S` | shift types | `shift_types` |
| `D` | working days in the horizon | `working_days` |
| `B` | branches | `branches` |
| `K` | disciplines | `disciplines` |
| `R(e)` | roles employee `e` holds | `employee_roles[e]` |
| `k(r)` | the one discipline of role `r` | `role_discipline[r]` |
| `W(e)` | working roles of `e` — `{r ∈ R(e) : mode(e,r) ≠ Collateral}` | `working_roles()` |
| `C(e)` | collateral roles of `e` | `collateral_roles()` |
| `X(e)` | exclusive roles of `e` | `exclusive_roles()` |
| `Ĝ(e)` | gating roles of `e` — `{r ∈ R(e) : gates(r)}` | `gating_roles()` |
| `G(k)` | gating roles in discipline `k` held by somebody | `_gating_holders` |
| `H(k,r)` | `{e : r ∈ Ĝ(e), k(r) = k}`, the holders of gating role `r` | `_gating_holders` |
| `P(k)` | `{(e,r) : r ∈ C(e), k(r) = k}`, collateral pairs in `k` | `_collateral_holders` |
| `Bd` | bound employees | `bound_employees()` |
| `F` | the books: `(e,r,s,d,b)` already settled | `forced` |
| `Fᵖ` | `{(e,s,d,b) : (e,r,s,d,b) ∈ F}` — settled *presence* | `forced_presence()` |
| `L` | `(e,d)` pairs blocked by leave | `leave_blocked` |

### Parameters

| Symbol | Meaning | Default |
|---|---|---|
| `N(k,b)` | configured rooms in discipline `k` at branch `b` | `rooms[(k,b)]`, 0 |
| `Ω` | barebones `(k,b)` pairs — rooms and nothing beside them | `barebones`, `∅` |
| `m(e,r)` | max rooms `(e,r)` covers in one slot | `max_rpe[(e,r)]`, 1 |
| `T(e)` | FTE-derived shift target over the horizon | `target_shifts[e]`, 0 |
| `T(e,r)` | agreed role FTE as a shift count | `role_target_shifts[(e,r)]`, absent |
| `pref(e,s)` | normalized shift preference weight | `shift_preferences[e][s]`, 0 |
| `suit(e,r)` | substitution suitability, `≥ 1` | `suitability()`, 1 |
| `v(r)` | objective points one shift in role `r` is worth | `value_of()`, 0 |
| `V(k,b)` | objective points a staffed room in `(k,b)` is worth | `room_value_of()`, 0 |
| `μ(e,r)` | this holder's value multiplier in a staffed room | `value_multiplier()`, 1 |
| `σ(e,e′)` | synergy multiplier of a pair sharing a room (symmetric) | `synergy_multiplier()`, 1 |
| `wρ` | ruleset row weight of rule `ρ` | 1 |

### Decision variables

```math
x[e,r,s,d,b] ∈ {0,1}     e works shift s on day d at branch b in role r
                         built only for r ∈ R(e) — role eligibility is structural,
                         a variable for a role somebody cannot work does not exist

p[e,s,d,b]   ∈ {0,1}     e is present for shift s on day d at branch b, whatever
                         they do during it

a[k,s,d,b]   ∈ ℤ,        rooms staffed in discipline k at branch b, shift s, day d
  0 ≤ a ≤ N(k,b)         the branch room cap is the variable bound, not a constraint
```

Rule-owned variables, created only by the rule named and absent otherwise:

```math
y[e,r,s,d,b,n] ∈ {0,1}   room_coverage_matched_rooms — e holds room number n in role r
ρ[k,s,d,b,n]   ∈ {0,1}   room_coverage_matched_rooms — room n is genuinely matched
t_j[e,r,s,d,b] ∈ [0,1]   room_load_objective — this holder's j-th room tranche
z, u, o        ≥ 0        auxiliary continuous variables (linearizations, below)
```

### Objective

```math
maximize   Σ_ρ  wρ · Σ (terms ρ contributed)
```

An empty term set is a constant-0 objective (pure feasibility). Rules file terms one per
slot rather than as one sum so the run's breakdown can be drilled into; the model sees the
same total either way, and `path(...)` never reaches the solver.

### Definitional constraints (`model_builder`, not a rule)

What `p` *means*. Built for every `(e,s,d,b)`:

```math
presence_role:        Σ_{r ∈ W(e)}  x[e,r,s,d,b]  ≤  p[e,s,d,b]        (if W(e) ≠ ∅)
presence_collateral:  x[e,r,s,d,b]  ≤  p[e,s,d,b]           ∀ r ∈ C(e)
presence_idle:        p[e,s,d,b]  ≤  Σ_{r ∈ W(e) ∪ C(e)} x[e,r,s,d,b]
```

So at most one working role per presence, collateral duties beside it, and no presence spent
on nothing.

---

## Constraint rules

### `one_shift_per_day` — One shift per employee per day

```math
∀ e ∈ E, d ∈ D:     Σ_{s ∈ S} Σ_{b ∈ B}  p[e,s,d,b]  ≤  1
```
`one_shift`. Standard.

### `warm_start` — Use existing Shift Assignments as a baseline

Not a constraint: an initial value on every variable, which is what the `fixValue()` rules
below pin against.

```math
x⁰[e,r,s,d,b] = 1 if (e,r,s,d,b) ∈ F  else 0
p⁰[e,s,d,b]   = 1 if (e,s,d,b)   ∈ Fᵖ else 0
```

Raises when `(e,r,s,d,b) ∈ F` and `(e,d) ∈ L` — the books and leave cannot both hold.

### `leave_blocklist` — Respect approved leaves

```math
∀ (e,d) ∈ L, s ∈ S, b ∈ B:     x[e,r,s,d,b] = 0   ∀ r ∈ R(e)
                               p[e,s,d,b]   = 0
```

Fixed at the warm-start value, which `warm_start` has already guaranteed to be 0 on a leave
day — hence `requires warm_start`. Standard.

### `use_existing_assignments` — Honor existing Shift Assignments

```math
∀ (e,r,s,d,b) ∈ F:     x[e,r,s,d,b] = 1
```

Group `existing_assignments`, non-standard. Requires `warm_start`.

### `bind_role_assignments` — Bind settled schedules (strictly)

Let `Fᵒ = {(e,s,d) : (e,s,d,b) ∈ Fᵖ}` be the booked half-days. For `e ∈ Bd` only:

```math
bind_presence:   Σ_{b ∈ B} p[e,s,d,b]  =  1            ∀ (e,s,d) ∈ Fᵒ
                 p[e,s,d,b]            =  0            ∀ (s,d) with (e,s,d) ∉ Fᵒ, ∀ b
                 x[e,r,s,d,b]          =  1            ∀ (e,r,s,d,b) ∈ F,
                                                         mode(e,r) ∈ {Exclusive, Collateral}
                 x[e,r,s,d,b]          =  0            ∀ r ∈ C(e), (e,r,s,d,b) ∉ F
```

Group `role_binding`, non-standard. Requires `warm_start`. Inert when `Bd = ∅`.

### `soft_bind_role_assignments` — Bind settled schedules

The ceiling without the floor. For `e ∈ Bd` only:

```math
                      p[e,s,d,b]  =  0                     ∀ (s,d) with (e,s,d) ∉ Fᵒ, ∀ b

soft_bind_exclusive:  Σ_{b′ ∈ B} p[e,s,d,b′]  ≤  x[e,r,s,d,b]
                                      ∀ (e,r,s,d,b) ∈ F with mode(e,r) = Exclusive
```

A booked half-day keeps its warm-start value of 1 but is otherwise free. Group
`role_binding`, standard. Requires `warm_start`; inert when `Bd = ∅`.

### `one_branch_per_shift` — One Branch per Shift

```math
∀ e ∈ E, s ∈ S, d ∈ D:     Σ_{b ∈ B}  p[e,s,d,b]  ≤  1
```
`one_branch`. Non-standard; redundant while `one_shift_per_day` is selected.

### `room_coverage` — Room coverage per discipline (pooled)

`a` is the minimum, over the discipline's gating roles, of the room-slots that role's
assignees contribute; the branch cap is already `a`'s upper bound.

```math
room_coverage:   Σ_{e ∈ H(k,r)}  m(e,r) · x[e,r,s,d,b]   ≥   a[k,s,d,b]
                                            ∀ (k,s,d,b), ∀ r ∈ G(k)
```

Group `room_coverage`, standard.

### `room_coverage_matched_rooms` — Room coverage per discipline (matched pairing)

For each `(k,s,d,b)` with `G(k) ≠ ∅` and `N = N(k,b) > 0`, rooms `n ∈ {1..N}`, writing
`occ(r,n) = Σ_{e ∈ H(k,r)} y[e,r,s,d,b,n]`:

```math
room_occ_unique:         occ(r,n)  ≤  1                              ∀ r ∈ G(k), n

room_occ_cap:            Σ_{n=1}^{N} y[e,r,s,d,b,n]  ≤  m(e,r) · x[e,r,s,d,b]
                                                                     ∀ r ∈ G(k), e ∈ H(k,r)

room_active_le:          ρ[k,s,d,b,n]  ≤  occ(r,n)                   ∀ r ∈ G(k), n
room_active_ge:          ρ[k,s,d,b,n]  ≥  Σ_{r ∈ G(k)} occ(r,n) − (|G(k)| − 1)     ∀ n

active_rooms_eq_matched: a[k,s,d,b]  =  Σ_{n=1}^{N} ρ[k,s,d,b,n]
```

The two `room_active` rows are the standard linearization of `ρ = ⋀_{r ∈ G(k)} occ(r,n)`.
Group `room_coverage`, non-standard.

### `barebones_branches` — Barebones branches

A barebones `(k,b)` opens no shift in a role of `k` that gates no room, whatever mode it is
worked in. One row per suppressed lane, over the whole horizon:

```math
barebones:   Σ_{e : r ∈ R(e)} Σ_{s ∈ S} Σ_{d ∈ D}  x[e,r,s,d,b]  =  0
                              ∀ (k,b) ∈ Ω, ∀ r with k(r) = k and ¬gates(r)
```

Gating roles are untouched — `N(k,b) = 0` is how a branch closes rooms. Standard; inert
while `Ω = ∅`.

### `fte_ceiling` — FTE ceiling

With tolerance `tol = 0.05`, for every `e` with `T(e) > 0`:

```math
fte_max:     Σ_{s ∈ S} Σ_{d ∈ D} Σ_{b ∈ B}  p[e,s,d,b]   ≤   (1 + tol) · T(e)
```

Group `workload_ceiling`, standard.

### `role_fte_ceiling` — Agreed role FTE ceiling

With `tol = 0.05`, for every `(e,r)` with `T(e,r) > 0`:

```math
role_fte_max:     Σ_{s,d,b}  x[e,r,s,d,b]   ≤   (1 + tol) · T(e,r)
```

Non-standard, no group — the hard reading of what `role_fte_target_objective` expresses
softly.

### `exclusive_role_purity` — Exclusive roles admit no collateral duty

For every `e` with `X(e) ≠ ∅` and `C(e) ≠ ∅`, and every `(s,d,b)`:

```math
exclusive_purity:  Σ_{r ∈ C(e)} x[e,r,s,d,b]  ≤  |C(e)| · ( 1 − Σ_{r ∈ X(e)} x[e,r,s,d,b] )
```

One row per slot, not per `(exclusive, collateral)` pair: the right-hand side switches every
collateral duty off at once. `Σ_{r ∈ X(e)} x ≤ 1` already holds from `presence_role`, since
`X(e) ⊆ W(e)`. Standard; inert unless somebody holds both an exclusive and a collateral role.

---

## Objective rules

Every term below is added to the maximized sum and multiplied by that rule's ruleset weight
`wρ`; `default_weight` is the figure a freshly-seeded ruleset row carries.

### `room_utilization_objective` — Room utilization

```math
+ Σ_{(k,s,d,b)}  a[k,s,d,b]
```

Group `room_value_choice`, standard, `default_weight = 3`. Requires `room_coverage`.

### `room_value_objective` — Room value

```math
+ Σ_{(k,s,d,b,n)}  V(k,b) · ρ[k,s,d,b,n]                 (terms with V(k,b) ≠ 0 only)
```

Group `room_value_choice`, non-standard. Requires `room_coverage_matched_rooms`.

### `employee_value_objective` — Employee value

For every `y[e,r,s,d,b,n]` with `μ(e,r) ≠ 1`, `V(k,b) ≠ 0` (where `k = k(r)`) and
`ρ[k,s,d,b,n]` existing, one continuous `z ≥ 0`:

```math
emp_value_le_y:       z  ≤  y[e,r,s,d,b,n]
emp_value_le_active:  z  ≤  ρ[k,s,d,b,n]
emp_value_ge:         z  ≥  y[e,r,s,d,b,n] + ρ[k,s,d,b,n] − 1

+ (−1 + μ(e,r)) · V(k,b) · z
```

`z` appears in no other term, so its sign in the objective alone drives it to
`y ⋀ ρ` — up to `min(y,ρ)` for a bonus, down to `max(0, y+ρ−1)` for a penalty; both equal
the AND of two binaries. Requires `room_value_objective`.

### `synergy_value_objective` — Synergy value

Writing `occ(e,k,s,d,b,n) = Σ_{r : y[e,r,s,d,b,n] exists, k(r)=k} y[e,r,s,d,b,n]`, then for
each configured pair `(eₐ,e_b)` with `σ(eₐ,e_b) ≠ 1` and each room `(k,s,d,b,n)` both can
occupy, with `V(k,b) ≠ 0` and `ρ` existing, one continuous `z ≥ 0`:

```math
synergy_le_a:       z  ≤  occ(eₐ,k,s,d,b,n)
synergy_le_b:       z  ≤  occ(e_b,k,s,d,b,n)
synergy_le_active:  z  ≤  ρ[k,s,d,b,n]
synergy_ge:         z  ≥  occ(eₐ,…) + occ(e_b,…) + ρ[k,s,d,b,n] − 2

+ (−1 + σ(eₐ,e_b)) · V(k,b) · z
```

The 3-way analogue of the employee bonus. Requires `room_value_objective`. Inert while
`employee_synergy` is empty.

### `room_load_objective` — Spread room load

For each `(k,s,d,b)`, each `r ∈ G(k)` and each `e ∈ H(k,r)`, with `m = m(e,r)`; tranches
`t_j ∈ [0,1]` for `j = 2..m` exist only where `m > 1`:

```math
room_load:        Σ_{j=2}^{m}  t_j[e,r,s,d,b]   ≤   (m − 1) · x[e,r,s,d,b]

room_load_cover:  Σ_{e ∈ H(k,r)} ( x[e,r,s,d,b] + Σ_{j=2}^{m(e,r)} t_j[e,r,s,d,b] )
                      ≥   a[k,s,d,b]                          ∀ r ∈ G(k)

−  Σ_{j=2}^{m}  ((j − 1) / (m − 1)) · t_j[e,r,s,d,b]
```

A convex per-holder cost, so tranche order needs no binaries and no SOS2: tranche `j` costs
`(j−1)/(m−1)`, cheapest first. `room_load_cover` restates coverage over the tranches and
binds tighter than `room_coverage`'s `m·x` whenever the tranches are not full. Standard,
`default_weight = 1`. Requires `room_coverage`. Inert where every `m(e,r) ≤ 1`.

### `collateral_room_value_objective` — Collateral duties

`min( a, Σ m·x )`, linearized. For each `(k,s,d,b)` with `P(k) ≠ ∅`, one continuous
`u ≥ 0`:

```math
collateral_rooms:  u  ≤  a[k,s,d,b]
collateral_span:   u  ≤  Σ_{(e,r) ∈ P(k)}  m(e,r) · x[e,r,s,d,b]

+ u
```

The objective only pushes `u` up, so it is squeezed to exactly the minimum — two
inequalities, no binaries. Group `collateral_value`, non-standard. Requires `room_coverage`.

### `fte_soft_ceiling` — FTE soft ceiling

`max(0, assigned − T(e))`, linearized. For each `e` with `T(e) > 0`, one continuous
`o ≥ 0`:

```math
fte_over:  o  ≥  Σ_{s,d,b} p[e,s,d,b]  −  T(e)

− o
```

The negative coefficient squeezes `o` down to exactly that maximum, so no equality and no
second slack. Group `workload_ceiling`, non-standard, `default_weight = 4`.

### `role_fte_target_objective` — Agreed role FTE split

`|assigned − T(e,r)|`, linearized. For each `(e,r)` with an agreed figure, two continuous
slacks `o, u ≥ 0`:

```math
role_dev:  Σ_{s,d,b} x[e,r,s,d,b]  −  T(e,r)   =   o  −  u

− (o + u)
```

Both carry a negative coefficient, so exactly one ends up non-zero and equals the absolute
deviation. Standard.

### `shift_preference_objective` — Shift preferences

```math
+ Σ_{(e,s,d,b)}  (−1 + pref(e,s)) · p[e,s,d,b]
```

`≤ 0` for every presence (uniform `pref` is `1/|S|`), so it doubles as a per-half-day cost
proxy. Group `shift_preference`, non-standard.

### `suitability_preference_objective` — Shift preferences and role suitability

```math
+ Σ_{(e,s,d,b)}    (−1 + pref(e,s)) · p[e,s,d,b]
+ Σ_{(e,r,s,d,b)}  (1 − suit(e,r))  · x[e,r,s,d,b]
```

The two sum to `(−1 + pref(e,s)) · suit(e,r)` whenever a presence is spent on exactly one
role, and reduce to `shift_preference_objective` when every `suit(e,r) = 1`. Group
`shift_preference`, standard.

### `collateral_capacity_value_objective` — Collateral duties (by configured rooms)

`min( N(k,b), Σ m·x )`, linearized. For each `(k,s,d,b)` with `P(k) ≠ ∅` and `N(k,b) > 0`,
one continuous `u` whose upper bound carries the constant half of the minimum:

```math
                     0  ≤  u  ≤  N(k,b)
collateral_capacity:  u  ≤  Σ_{(e,r) ∈ P(k)}  m(e,r) · x[e,r,s,d,b]

+ u
```

Reads no `a` at all. Group `collateral_value`, standard.

### `role_value_objective` — Value of working a role

```math
+ Σ_{(e,r,s,d,b)}  v(r) · x[e,r,s,d,b]                    (terms with v(r) ≠ 0 only)
```

Per assignment, not per presence. Standard; inert while every `v(r) = 0`.

### `weigh_assignments_objective` — Conserve Existing Assignments

With `ε = 2⁻¹⁰`:

```math
− ε · Σ_{(e,r,s,d,b) ∉ F}  x[e,r,s,d,b]
```

A penalty on everything the books do not have, which is a tie-break toward what they do have
up to an additive constant. Group `existing_assignments`, non-standard.

---

## Rule interaction, as arithmetic

- **Choice groups** — at most one member of each may appear in a ruleset
  (`BuiltinRule.check_ruleset`): `existing_assignments`, `role_binding`,
  `workload_ceiling`, `collateral_value`, `shift_preference`, `room_coverage`,
  `room_value_choice`. "None of them" is always legal.
- **`requires`** — `leave_blocklist`, `use_existing_assignments` and both binding rules
  require `warm_start`, because `fixValue()` pins a variable at its initial value and is a
  silent no-op without one. `apply_rules` therefore applies specs in topological order over
  `requires` (`order_specs`), not in the order they arrive.
- Rules that count somebody's workload or freeze their week key on `p`; rules that price
  *what* is worked key on `x`.
