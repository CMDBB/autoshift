# Desk pages (`autoshift/autoshift/page/`)

Five pages, each with a thin whitelisted backend. The wall chart has its own notes
(`autoshift/wallchart/CLAUDE.md`), as do the Rota Editor's engine (`autoshift/rota/CLAUDE.md`)
and the Horizon Scanner's (`autoshift/scanner/CLAUDE.md`).

| Page | Route | Backend |
|---|---|---|
| Optimizer Studio | `optimizer-studio` | `autoshift/optimizer_studio.py` |
| Horizon Scanner | `horizon-scanner` | `autoshift/scanner/api.py` |
| Rota Editor | `rota-editor` | `autoshift/rota/editor.py` |
| Role Matrix | `role-matrix` | `autoshift/role_matrix.py` |
| Synergy Matrix | `synergy-matrix` | `autoshift/employee_role_synergy.py` |

---

## Optimizer Studio

A workspace-level abstraction *over* Optimizer Run + Optimization Ruleset, and the first
"automatic"-run surface: Planning Mode / Start Date / a human-readable rule-toggle panel, two
prefill link pickers, and a **Preview Schedule** action.

**The schedule view sits above the toggle panel**, which is the page's whole premise: the week
is the thing being worked on and the rules are how it is nudged, so everything that isn't the
chart is either a page-header field or inside `.op-config`.

**The panel is built so a ruleset `check_ruleset` would reject is structurally unreachable** —
choice groups render as radios including an explicit *None*; `requires` becomes **nesting**
(`index_catalog` builds a forest, a child drawn inside its parent with the checkbox disabled
until the parent is on); `excludes` disables and unchecks its targets. Everything else is a
checkbox, with a weight input on Objective/Mixed rules. **Read the design notes before touching
`index_catalog` / `blocked_reasons` / `sync_dependencies`.** Two traps: the decision half
(`blocked_reasons`) is deliberately DOM-free so the invariant is testable, and
`sync_dependencies()` re-runs to a fixpoint because unchecking a parent can orphan a
grandchild; and **rows nest, so every DOM read must be scoped to a row's own controls**
(`own_toggle` / `own_weight`) — a plain `.find()` reaches into child rows.

Every preview overwrites one ruleset per user (`Studio Draft — <user>`, `is_system=0`) and
really does create an Optimizer Run, with `type="Automatic"`. Solving reuses
`OptimizerRun.solve()` unchanged. **Save Ruleset As** promotes the draft via `frappe.copy_doc`.

Both pickers are one-shot prefills over the same `get_ruleset_selection` / `prefill_from_run`,
never a persistent link. **Load Ruleset** doubles as a provenance label showing what the panel
was last seeded from, so `show_ruleset` records the name *before* writing the field
(`set_value` fires `change` too), and every other seeding path — the initial Standard Ruleset,
Populate From Run, Save Ruleset As — points it at the ruleset it used. Clearing it forgets
that, which is how a user reloads a ruleset over their own hand edits. `apply_selection` warns
about rows the panel cannot draw, since `get_rule_catalog` lists implemented rules only and a
ruleset may legally carry unimplemented ones.

### Both solve entry points

The Optimizer Run form's Solve button and Studio's Preview both pre-flight before running:

- a **binding-gap check** (`OptimizerRun.check_binding_rule_gap` /
  `optimizer_studio.check_binding_rule_gap`, thin wrappers over `data_loader.binding_rule_gap`)
  confirms when the site marks roles binding but the selection omits a `role_binding` rule;
- **unconfirmed rotas** (`check_unconfirmed_rotas`, over `rota.editor.unconfirmed_rotas`) warn
  without blocking, with one link per discipline into the Rota Editor (`?discipline=…&start=…`,
  applied by the page's `take_route_options` and then stripped from the URL);
- the run form additionally offers the **duplicate input hash** prompt.

**Neither entry point creates any `Shift Assignment`.** A solve reads unrecorded rota days
straight out of `rota.materialize.settled_rows` — see `autoshift/rota/CLAUDE.md`.

---

## The schedule view

`autoshift/public/js/schedule_view.js`, namespaced `autoshift.schedule_view` — one tab bar,
**Week / Statistics / Roster / Solver Log**, rendered by both the Optimizer Run form (into
`schedule_view_html`) and Studio's result area. Panes load lazily on first click and are
cached. Statistics and Roster need a solved run and are *disabled with a tooltip* rather than
hidden; Solver Log needs only a run, so Studio's `preview()` / `get_run_status()` return
`solver_log` on every outcome (shared `_run_result`).

- **Week** — the wall chart. The default pane and the only always-on one; see
  `autoshift/wallchart/CLAUDE.md`.
- **Statistics** — `OptimizerRun.get_run_statistics()` + `autoshift/public/js/run_stats.js`.
  Room-slots staffed vs configured, per-discipline coverage meters, a discipline × day grid,
  employees below FTE target, and each rule's share of the objective. Shares come from
  `objective_breakdown`, persisted as JSON from `RuleContext.objective_contributions`, and sum
  to the objective value (`test_objective_contributions_attribute_and_sum_to_the_objective`
  pins that). Each share **opens into a tree** of where it was earned, down to the (discipline,
  branch, day, shift): a rule labels each term with `rules.path(...)` and `rules.objective_tree`
  folds them after the solve. A node carries no level of its own (it is `levels[depth - 1]` of
  its rule); breadth past `MAX_CHILDREN` folds into "… and N more"; a subtree past
  `MAX_NODES_PER_RULE` drops its *deepest* levels (named in `trimmed`). Runs solved before the
  tree read back as flat rule rows — `_objective_breakdown` normalizes both shapes and relabels
  the `Employee` level with real names. The panel builds a node's children on first expand.
  Meters carry a **role-supply bound** (`_role_supply_bounds`) drawn as a marker and stated as
  a warning where it falls below configured capacity.
- **Roster** — the per-employee grid, `autoshift/public/js/schedule_grid.js` (namespaced
  `autoshift.schedule_grid`), shared by the form and Studio off the same
  `{days, employees, events}` shape both `get_schedule_events()` and Studio's `preview()`
  return.

---

## Horizon Scanner

A shell over `autoshift/public/js/horizon_calendar.js` and nothing else: the page owns the
title, a **New…** action and the route option (`?scan=`, applied on every `on_page_show`
because the script is loaded once per Desk session, then stripped so a reload does not keep
forcing one scan open). Everything else — the scan picker, Scan/Stop/Re-run, the
shortfall-reading checkbox, the band pickers, the month calendars, the day panel and the wall
chart under it — is in the shared renderer, because the **Horizon Scan form's Calendar tab
draws the same thing off the same payload** and a scan reached from the list view must not be
a worse view than one reached from the page.

Clicking a day renders that week's wall chart underneath (`wall_chart.render` against the
window's own run) — free, because a scan keeps one run per window. See
`autoshift/scanner/CLAUDE.md` for the verdict itself.

## Role Matrix

`Employee Scheduling Role` drawn as the dense employee × role matrix it is a sparse
representation of. Rows are active employees holding a role among the shown columns (or all,
with the toggle); one **Settings** column holds a one-chip summary of Employee Settings (opens
the doc, or a new one, in a new tab); each role cell is an inline `suitability` input, blank =
no row.

Edits stage **client-side only** (no draft doctype) and `apply_changes` writes the batch in one
request, so one failing row rolls it all back. Clearing a cell deletes the row — HR Manager has
no delete permission on the doctype, and the page refuses the edit up front.

## Synergy Matrix

The same stage-then-apply grid pattern, but **genuinely rectangular**. `get_matrix` splits each
scoped discipline's gating Scheduling Roles into two sides (`_split_by_side`, alternating by
role name, sorted for determinism — which role lands on which side is arbitrary, since
`Employee Role Synergy` carries no role of its own), so a role's holders land wholly on one
side and same-role pairing is *structurally* impossible rather than merely hidden behind a
disabled diagonal.

Rows are one side's holders, columns the other's; cell `(A, B)` / `(B, A)` share one staged key
regardless (canonicalized alphabetically, matching the doctype's own `before_insert`). A
per-employee control sets every pair in that employee's row/column at once, pairing against
whichever side it is *not* on.

A dedicated **Self** row (under the header) and column (right after Employee, mirroring Role
Matrix's Settings column) carry a different field entirely —
`Employee Scheduling Role.value_multiplier` — so the employee-value half of the same room-value
mechanism can be edited alongside synergy without leaving the page. Clearing a self cell resets
it to 1 rather than deleting the role-holding record.

Ctrl/Cmd+click toggles a cell into a multi-selection and Shift+click extends it as a rectangular
range (by table position, so a selection may span the Self row/column and the pair grid
together); an inner-button prompt sets every selected cell at once. Focusing any cell highlights
its row and column.

**The page is routed `synergy-matrix`, apart from the doctype it edits** — `Employee Role
Synergy`'s own list view already claims `employee-role-synergy`.
