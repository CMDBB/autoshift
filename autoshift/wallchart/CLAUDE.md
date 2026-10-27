# Wall chart (`autoshift/wallchart/` + `autoshift/public/js/wall_chart.js`)

Rooms down the page, days across. It answers **"is Tuesday morning covered"**, which the
per-employee roster grid structurally cannot. The **Week** pane of the schedule view, and the
only **always-on** one: it renders on a Draft, on a Failed run and in Studio before the first
preview, because it falls back to the submitted `Shift Assignment`s on the books.

Split like the optimizer:

| Module | Role |
|---|---|
| `chart.py` | **Frappe-free** placement arithmetic — covered by `tests/test_wallchart.py` |
| `layout.py` | derives the band/lane/section structure from the DB |
| `source.py` | reads slots, books, rotas, leave, employees |
| `api.py` | the whitelisted `get_week_chart(week, run, mode, disciplines, branches, weekdays, highlight_roles)` |
| `wall_chart.js` | all rendering; `view()` bundles the two things drawing is parameterised on — which day columns, and how much to say — into one `ctx`, so screen and sheet share every markup function |

Rationale for most of what follows is in `docs/design-notes.md` ("Wall chart: derived layout…").

---

## Layout is derived, never declared

`layout.derive()` reads it out of Discipline Branch Config + Scheduling Role + Shift Type: one
band per config row, `rooms_num` numbered rows, one lane per active Scheduling Role in that
discipline, one stacked section per Shift Type ordered by `start_time`. So there is no layout
file to keep in sync, an unstaffed room is a blank row and an uncovered role is a blank column.
Anything no band claims lands in an **`Unplaced`** band with the reason stated.

Lane order is `Scheduling Role.display_order_key`, then max-rooms descending, then name. Band
order follows the minimum `display_order_key` among a discipline's roles, with
`Discipline Branch Config.display_order_key` as the tiebreak for two entirely unkeyed bands
(read after the role-based key, before branch/discipline name).

Lane headers print `Scheduling Role.wallchart_abbreviation` where the site set one, otherwise
initials derived from the role name (`wall_chart.js`'s `lane_initials`).

Cells print initials — `Employee.custom_initials` where zawin2frappe has installed it (**read,
never shipped here**), otherwise derived from the name. Chip order within a lane is
alphabetical unless `Scheduling Role.chip_sort_field` names an Employee field to rank by
(resolved in `source.py`, the only module that reads Employee, and carried on the `Slot` as
`sort_value` so `chart.py` stays Frappe-free). `chart._order_lane` puts a `None` value after
every ranked one rather than raising.

---

## Placement

A holder gets **one chip per room they cover** (`Slot.rooms` = the run's measured `rooms` where
it has one, else the holder's `max_rooms`) — one `Placement`, and so one payload cell, per line.
There is no `rowspan` and no `SPANNED` sentinel: two holders' rooms interlace (1+3 against 2+4)
and a single tall chip cannot draw that. `Placement.room` carries the solved room number that
chip stands for, 0 where none was measured.

**Anything counting half-days counts distinct `Slot.match_key`, not placements** (`_totals`'
kept/added/dropped, `build`'s inferred-role warning).

**Where a run measured `room_coverage_matched_rooms`, rows are real rooms, not fill order.**
`Slot.room_index` is a solved room number and **that number is the row** — two lanes' chips
sharing an index land on the same row, which is what draws a genuine pairing rather than two
lanes stacked in the same visual order by coincidence. **A room nobody was matched into stays
an empty line**: no rule prefers a low room number, every room of a band is interchangeable, and
compacting the gap would draw rooms 2–3 as rooms 1–2 — a schedule the solver did not produce.
Anything without a measured index (no matched-rooms run, a non-gating role, a book slot, a
multi-room chip whose indices are not contiguous) fills the rows left over, exactly as before.
The index shows in the chip tooltip ("Room 3") via `api._cell`'s `room_index`; reporting only,
since row placement is already decided by then.

**Only the first `rooms_num` rows are rooms.** A band grows past its room count rather than hide
anybody, but those extra lines are neither numbered nor hatched — there is no such room to
number and a non-room cannot be half-staffed. The ordinal's tooltip and `build`'s "covers N
rooms but only M are configured" warning explain them. They are drawn only while something in
them is drawn (`wall_chart.js`'s `drawn_rows`), so an overflow line holding nothing but a
`dropped` chip the current view does not print collapses with every other empty trailing row.
**Nothing on this chart ever hides a scheduled person** — only a genuinely blank line past the
configured count.

`Unplaced` is dropped outright when nothing in it is drawn: it reports unplaceable people, so
with none to report it says nothing. A *configured* band with no rooms still prints its label,
because that emptiness is the configuration.

---

## Coverage

`Chart.covered` is the **set of open rows** per (shift type, band, day), not a count
(`covered_rows` places them, `covered_rooms` counts them), reaching the browser as the band's
`open_rows`. An open room need not be at the top of the stack, so `_coverage` **intersects the
gating lanes' staffed rows** instead of taking a minimum over prefixes.

Any line **not** in `covered` is hatched **on screen**, because a half-staffed room is not an
open room. `dropped` chips sink to the bottom of their lane and count toward neither. The
headline counts fully-staffed rooms for the same reason.

---

## Diff against the books

With a run, cells are `kept` / `added` / `dropped`, plus `changed` on a moved half-day, via
`chart.merge` (matched on `(employee, date, shift_type)` and deliberately **not** on role).

"The books" include bound employees' unrecorded rota days (`source.from_settled_rotas`, off
`pending_bound`'s rows, leave days dropped) — the same days the loader reads — drawn as
`virtual` chips (dotted, italic), so the week can be checked before anything is created. The
"Create them" banner sits behind an "N not recorded" toolbar toggle and calls
`materialize_between` through `autoshift/public/js/rota.js`.

---

## The reader's controls

Four pickers in a row of their own above the chart — the only part of the view that outlives a
fetch, so a dropdown is not destroyed mid-click. They re-fetch on close, not on every toggle.

**Discipline** and **Branch** narrow **server-side** — `get_week_chart(..., disciplines,
branches)` → `layout.derive(disciplines, branches)` — because the sections, the coverage
headline, the leave list and which slots even load all follow the band set. **An empty selection
is *every* band, never none.** `layout.filter_options` lists what there is to narrow to
(unfiltered, so the picker keeps offering what the selection hid); each option carries a
`description`, **which is not optional** — `MultiSelectList` renders that field into the row
subtitle and Frappe's `process_options` only defaults it for *string* options, so an object
option without one prints "undefined". Ours names the band's other axis plus its room total
("B1 · B2 — 6 rooms"). `source.in_scope` splits the slots and hands back the hidden ones so
`filters.hidden` can state a count: **a filter may hide, it may not hide silently.**

**Days worked** (`filters.weekdays`, presets "All days" / "Weekends off") says which weekdays the
practice runs, for a site whose Holiday List cannot say yet. `api._default_worked` seeds it from
the calendar's weekly offs where it names any, else Mon–Fri (`api.WEEKENDS`, now **only** a
default, not a rule). It **narrows and never widens**: "All days" cannot reopen a day a Holiday
List closed, because that belongs in the list, which is the only answer a solve reads. It drives
the working-day shading, the headline's capacity, and — the reason it is a server argument —
which days a leave can have emptied. Parsed by `api._weekdays`, apart from `_selection`, because
**Monday is 0** and a falsy filter would eat it.

**Highlight** marks a holder of any of the chosen `Scheduling Role`s wherever their chip is
drawn, **including a half-day worked in a different role** — so an apprentice stands out
whatever they are covering. It **narrows nothing**; it is in the filter payload only so the
picker can show what is currently marked. Its argument is tri-state, unlike the others
(`api._highlight_selection`): omitted (`None`) means "seed from
`Scheduling Role.highlight_by_default`", an explicit **empty list means highlight nobody**. Its
options are every active role regardless of the band filter, for the same reason it is not
narrowed by band.

`day.working` comes from the **calendar** plus that control, not from the weekday: a Holiday
List stores its weekly offs as dated `Holiday` rows, so "not in the calendar" is "worked".

---

## Leave

**Filed under the half-day it empties**, one row per section inside that section's own table
(AM chart → AM leave → PM chart → PM leave), with the unattributable ones in a leftovers row at
the foot. HRMS records a half-day as a flag plus a date and never which half, so the attribution
comes from what the person would otherwise have worked — `source._expected_shifts`, off their
rota and the books. `chart.bucket_leaves` (Frappe-free, tested) files into three buckets. All
day off ⇒ listed under both halves. Leave follows the filter and, with it, drops everybody
holding no Scheduling Role at all.

**A leave day that emptied nothing is set aside** (`source._due` → `bucket_leaves`'s third
bucket, `leaves_not_due`, which nothing draws): a leave is a span of dates, so a long one covers
weekends and a four-day week's off day too, and reporting those says only that the leave is
long. `due` is false where their **calendar** says they do not work that day, or where we have a
week for them and their **rota** puts them nowhere on it. Somebody with neither is `due`,
reported with no half named — absence of evidence is not evidence.

---

## "Export PDF…" — a dialog, not a print

Per-print granularity: disciplines, branches, a heading, working-days-only (an empty weekend
column goes; one somebody is scheduled on never does), include-leave,
include-coverage-summary, a table per shift type (repeated day header, so a page break lands
between bands), orientation and text size.

Its **"Finished sheet" default is on**, dropping everything that says where a chip came from —
the kept/added/dropped colours, ★/→, the dashed/dotted borders, the legend, the warnings, the
`Unplaced` red and the today/out-of-window shading. A `dropped` chip still prints — hiding a
scheduled person is the one thing this chart never does — but loses its colour and strikethrough
and prints in **italics under a dashed underline** (`.awc-idle`), needing no colour to read as
"not actually here"; `open_rows` never counted it, so its room draws as the empty one it is.

**`export_css` paints nothing: no background, no gradient, no colour fill.** Not a preference —
a browser prints with backgrounds off by default and drops every `background-color` and
`background-image`, so anything a sheet says that way is lost on paper. Forcing it back with
`print-color-adjust: exact` was worse: the rasterizer had to paint every gradient tile and wavy
decoration at print resolution across hundreds of cells, which made the export crawl and still
came out coarse. So the sheet is **ink only** — text, borders, text decorations — saying the
cheap, honest version of each thing: a dot in an empty seat (`.awc-room:empty`, the `awc-room`
class being "this line is a seat", distinct from the screen's stronger `awc-uncovered`), a rule
above a section heading, an italic header for a non-working day, and a bare border plus text
colour where a chip used to carry a fill. `legend_markup` takes a `printed` flag for the one key
the two media cannot share.

**Before adding anything to that stylesheet, check it is ink.**

A scope the screen is not already showing is re-fetched, and the popup is opened in the click's
own turn and filled after (a window opened post-`await` is blocked). The dialog inherits the
screen's **Days worked** setting rather than asking again: it is a statement about the site's
week, not a per-print choice.
