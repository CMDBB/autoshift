# Horizon scanner (`autoshift/scanner/` + `autoshift/public/js/horizon_calendar.js`)

Solves **every window from here to a date ahead**, one Optimizer Run each, and draws the
result as month calendars with one square per day. It answers **"what breaks in March"**,
which a single run structurally cannot.

Same Frappe-free/DB split as the optimizer:

| Module | Role |
|---|---|
| `horizon.py` | **Frappe-free** — the verdict arithmetic and the calendar layout; covered by `tests/test_scanner.py` |
| `scan.py` | the sweep: planning windows, creating/solving runs, checkpointing, reading a scan back |
| `api.py` | the whitelisted `get_scan` / `get_day` / `create_scan` / `start_scan` / `stop_scan` / `rerun_windows` |
| `horizon_calendar.js` | all rendering, shared by the Horizon Scanner page and the Horizon Scan form's Calendar tab |

Doctypes: **Horizon Scan** + child **Horizon Scan Window** (`autoshift/autoshift/doctype/CLAUDE.md`).
Page: `horizon-scanner` (`autoshift/autoshift/page/CLAUDE.md`). `bench scan-horizon` runs the
same sweep in the foreground, which is how a bench with no worker scans anything.

---

## One run per window, kept

A scan is a sequence of ordinary `Optimizer Run`s with `type = "Scan"` — which is what makes
a flagged day worth clicking: the wall chart, the statistics panel and the solver log all
already read a run, so the drill-down costs nothing and clicking a day draws **that week's
wall chart under the calendars**. `Scan` is hidden from the run list by default
(`optimizer_run_list.js`) for the reason `Automatic` is: dozens per scan.

It also means an unchanged window need not solve twice. The run's `input_hash` is the hash
every other caching path uses, so `solver.find_cached_runs` hands back the earlier run and
the window links it instead (`reuse_cached`, on by default, `reused` on the row). A re-run of
a scan nothing has changed under therefore costs one `DataPackage` per window and **no
solves** — which is the whole point of "re-runnable": you re-run after editing a rota, and
only what the edit touched is solved again.

**Windows do not overlap** (`horizon.plan_windows`): each day is judged once, by one run.
The first starts on the Monday of `start_date` (an Optimizer Run takes a Monday) and the last
is kept **whole** even past `end_date` — truncating it would solve a short week and then
report the resulting hole as a finding.

**A scan never writes a `Shift Assignment`.** It is dozens of solves and a solve reads.

## Resumable because it checkpoints, not because it tracks

Each window commits as soon as it is solved, and the **row statuses are the only state**:
resuming is "solve every `Pending` row, oldest first". So a worker restart, a `Ctrl-C`, a
`Stop` and a time-limited window all leave the same thing behind — a partly solved scan that
knows exactly what it has left. Nothing is transactional across windows on purpose: a
half-finished scan is a *useful* scan.

- **Stop is cooperative** (`cancel_requested`, read between windows). The window in flight is
  finished first: CBC is a subprocess, and a window abandoned mid-solve would be re-solved
  from scratch anyway.
- A window that exceeds `window_time_limit` is left **`Timed Out`** with its Draft run kept;
  the next resume retries that same run. One pathological week must not hold the other
  fifty-one hostage.
- Saving the scan **re-plans** its windows, and a window whose span survives keeps its run
  and findings — so pushing the end date out by a month costs only that month. `plan()`
  rewrites the child rows **only when the horizon actually moved**, because a sweep in flight
  is holding their names.
- The job is deduplicated on `job_id`, so a double-click cannot start two sweeps.
- Row writes go **straight to the database** (`_set_row`), never through the parent's save: a
  sweep is a long job and the scan is a document somebody is watching.

## The verdict

Per **day**, out of four independent kinds of finding — three stored per day, one belonging to
the window:

| Finding | Severity | Source |
|---|---|---|
| room coverage shortfall | `warning`, or `critical` where the slot is **shut** | `Optimizer Run Coverage` vs. the capacity |
| binding / unresolved conflicts | `warning` | the loader's `binding_conflicts`, `unresolved_assignments` |
| under-FTE | `info` | `target_shifts` vs. solved presences |
| the window never solved | `failed` | the row's status, applied by `window_days` |

**Worst wins** at every level (cell → day → window → scan). A day nobody works carries no
cells and no verdict. **A day no window has reached is not a clean day** — it has no square in
`days` at all, and the calendar draws it dashed; painting it green is the one lie this view
cannot afford.

Non-coverage findings only raise a *floor*: a conflict is a `warning` however full the rooms
are (a record contradicting another record is not answered by the schedule being fine around
it), under-FTE only `info` (nobody is missing from a room over it).

### The two readings of a shortfall

Both capacities are stored per cell, and the reader picks — **nothing re-solves**:

- `capacity` — `Discipline Branch Config.rooms_num`, what the configuration asks for;
- `exclusive_capacity` — what the **exclusive gating roles actually scheduled in that slot**
  could ever open: the scarcest such role's holders, each contributing their `max_rpe`.

A practice with three practitioners and four rooms is not short a room on the day only three
are in — it is short a practitioner, which is a hiring question. Scaling therefore flags
**only a lack of flexible staff** around the holders who are there, which is the
schedulable problem. Mechanics worth keeping straight:

- a **Flexible** role never caps it — that is the shortage a scan is for — unless every
  holder *present* overrides it to Exclusive, in which case nobody else could have been put
  there either;
- a **non-gating** exclusive role (an administrative post) caps nothing;
- **nobody in an exclusive gating role is a supply of zero**, not a role to skip: the rooms
  genuinely cannot open, so the scaled reading calls that day fine and the strict one calls it
  shut. Both are true statements about different questions.

## Narrowing

`horizon.narrow` drops the cells of bands the reader filtered out **server-side**, for the
wall chart's reason: severity, shortfall and counts all have to come off the same cells being
looked at. An empty selection is *every* band. An employee-level finding carries the
employee's own disciplines so it can be narrowed too — but **a finding naming no discipline
survives every filter**, since an assignment nothing could place is exactly what a narrowed
view must not swallow.

## Storage

`Horizon Scan Window.findings` is `horizon.dumps` — compact raw numbers, one blob per window,
not a child table: a year-long scan is ~52 rows, where a row per (day, discipline, branch,
shift) would be thousands. `FINDINGS_VERSION` guards the shape; a payload from another version
reads back as **no findings** rather than being guessed at.

## The page

`horizon_calendar.js` is a renderer — every judgement is already in the payload. It is wrapped
in an IIFE, unlike the other scripts here, because `make_picker` / `build_filters` are names
`wall_chart.js` already uses for its own and these are plain scripts sharing one global scope.

- The filter row **outlives a fetch** (built once) so a dropdown is not destroyed mid-click,
  and the selected day outlives it too.
- While a scan is `Scanning` the page **polls every 5 s**, because the sweep reports a window
  at a time and the calendar visibly fills in. The timer stops when the wrapper is detached.
- A square's mark says which finding it is: `!` no schedule, `−N` rooms short, `⚑` a conflict,
  `·` under FTE.

## Edges worth knowing

- **A ruleset with no `room_coverage`-group rule makes the verdict meaningless** — nothing
  ties `active_rooms` to who is staffing the slot, so every cell reads as shut. `plan` warns
  about it once on save (`coverage_rule_gap`), rather than fifty-two times mid-sweep.
- **`Unbounded` is not a window length** (`plan_windows` raises): a scan is a finite sweep of
  finite windows by definition. See issue #8.
- **A scan carries no leave speculations.** Its runs are created bare, so a scan reports the
  horizon as the books and the approved leave have it. Speculating a pending leave is still a
  single-run question.
- `bench scan-horizon` runs `sweep` in the foreground — the same function the queue runs, so
  a `Ctrl-C` leaves exactly what a killed worker would.
