// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// The week wall chart: rooms down the page, days across. Shared by the Optimizer
// Run form and Optimizer Studio, both of which hand it the payload
// `autoshift.wallchart.api.get_week_chart` returns — every band, row, lane and
// cell already decided server-side, so this file only draws.
//
// It is a real <table> because the chart is real tabular data, and because a
// band's label spanning its rows is exactly what <th rowspan> is for.
//
// Two things the drawing is parameterised on, both bundled into the `ctx` that
// every markup function takes (see `view`):
//
//   which day columns to draw — a printout drops an empty weekend, the screen
//   never does, and either way a cell is looked up by the day's *original*
//   index, because the payload is always seven days wide.
//   how much to say — the on-screen chart is a working instrument and shows
//   provenance (kept/added/dropped colours, ★ pinned, → moved, dashed and
//   dotted borders, the legend, the warnings); a `clean` printout is a finished
//   sheet and shows the schedule. Hatching survives both: "this room is not
//   open" is a fact about the week, not about where a chip came from.
//
// NOTE: no `import`/`export` here, deliberately — see bulk_employee_settings.js
// for why doctype/page scripts on this app stay plain scripts. Loaded via
// frappe.require() and reached through the namespace below.

frappe.provide("autoshift.wall_chart");

autoshift.wall_chart.inject_styles = function () {
	if (document.getElementById("autoshift-wall-chart-styles")) return;
	const css = `
		.autoshift-wall-chart { margin-bottom: 1rem; }
		.autoshift-wall-chart .awc-bar {
			display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
			margin-bottom: 0.6rem;
		}
		.autoshift-wall-chart .awc-week { font-weight: 600; min-width: 12rem; text-align: center; }
		.autoshift-wall-chart .awc-legend {
			display: flex; gap: 0.9rem; flex-wrap: wrap; margin-left: auto;
			font-size: var(--text-sm); color: var(--text-muted);
		}
		.autoshift-wall-chart .awc-key { display: inline-flex; align-items: center; gap: 0.35rem; }
		.autoshift-wall-chart .awc-swatch {
			width: 0.85rem; height: 0.85rem; border-radius: 3px; border: 1.5px solid;
		}
		.autoshift-wall-chart .awc-filters {
			display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;
			margin-bottom: 0.5rem; font-size: var(--text-sm);
		}
		.autoshift-wall-chart .awc-filters:empty { display: none; }
		.autoshift-wall-chart .awc-filter-label { color: var(--text-muted); }
		.autoshift-wall-chart .awc-filter { min-width: 11rem; }
		.autoshift-wall-chart .awc-filter .multiselect-list { min-width: 11rem; }
		.autoshift-wall-chart .awc-filter-days,
		.autoshift-wall-chart .awc-filter-days .multiselect-list { min-width: 8rem; }
		.autoshift-wall-chart .awc-filter-gap { margin-left: 0.6rem; }
		.autoshift-wall-chart .awc-hidden-note { font-size: var(--text-xs); }
		.autoshift-wall-chart .awc-totals {
			font-size: var(--text-sm); color: var(--text-muted); margin-bottom: 0.5rem;
		}
		.autoshift-wall-chart .awc-totals b { color: var(--text-color); }
		.autoshift-wall-chart:fullscreen, .autoshift-wall-chart:-webkit-full-screen {
			background: var(--fg-color); padding: 1rem; overflow: auto;
		}
		.autoshift-wall-chart:fullscreen .awc-scroll { max-height: calc(100vh - 8rem); }
		.autoshift-wall-chart .awc-scroll {
			overflow: auto; max-height: 44rem;
			border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
		}
		.autoshift-wall-chart table { border-collapse: separate; border-spacing: 0; width: 100%; }
		.autoshift-wall-chart th, .autoshift-wall-chart td {
			border-bottom: 1px solid var(--border-color);
			border-right: 1px solid var(--border-color);
			padding: 0.2rem 0.35rem; font-size: var(--text-sm); text-align: center;
			white-space: nowrap;
		}
		.autoshift-wall-chart thead th {
			position: sticky; top: 0; z-index: 3; background: var(--fg-color);
			font-weight: 500;
		}
		.autoshift-wall-chart .awc-lane {
			font-weight: 400; color: var(--text-muted); font-size: var(--text-xs);
			background: var(--fg-color);
		}
		.autoshift-wall-chart .awc-section-title {
			text-align: left; font-weight: 600; letter-spacing: 0.04em;
			background: var(--bg-light-gray, var(--subtle-fg)); text-transform: uppercase;
			font-size: var(--text-xs);
		}
		.autoshift-wall-chart .awc-band {
			position: sticky; left: 0; z-index: 2; background: var(--fg-color);
			text-align: left; vertical-align: top; min-width: 11rem; max-width: 11rem;
			white-space: normal; font-weight: 500;
		}
		.autoshift-wall-chart .awc-band-branch {
			display: block; font-weight: 400; color: var(--text-muted); font-size: var(--text-xs);
		}
		.autoshift-wall-chart .awc-band.awc-overflow { color: var(--red-600, #dc2626); }
		.autoshift-wall-chart .awc-ord {
			position: sticky; left: 11rem; z-index: 2; background: var(--fg-color);
			color: var(--text-muted); font-size: var(--text-xs);
			min-width: 1.8rem; max-width: 1.8rem;
		}
		.autoshift-wall-chart thead .awc-band { z-index: 4; }
		.autoshift-wall-chart thead .awc-ord { z-index: 4; }
		.autoshift-wall-chart .awc-day-start { border-left: 2px solid var(--border-color); }
		.autoshift-wall-chart .awc-nonworking { background: var(--bg-light-gray, #f4f5f6); }
		.autoshift-wall-chart .awc-outside { opacity: 0.55; }
		.autoshift-wall-chart .awc-cell { min-width: 3.2rem; cursor: default; vertical-align: middle; }
		.autoshift-wall-chart .awc-uncovered {
			background-image: repeating-linear-gradient(
				45deg, transparent, transparent 4px,
				var(--border-color, #e5e7eb) 4px, var(--border-color, #e5e7eb) 5px
			);
		}
		.autoshift-wall-chart .awc-aside { background-color: var(--bg-light-gray, #fafafa); }
		.autoshift-wall-chart th.awc-aside { font-style: italic; }
		.autoshift-wall-chart .awc-who {
			display: inline-block; border: 1.5px solid transparent; border-radius: var(--border-radius);
			padding: 0.05rem 0.3rem; font-family: var(--font-stack-mono, monospace);
			font-size: var(--text-xs); line-height: 1.5;
		}
		.autoshift-wall-chart .awc-existing { background: var(--bg-light-gray, #f3f4f6); border-color: var(--gray-400, #9ca3af); }
		.autoshift-wall-chart .awc-kept { background: #eef2ff; border-color: #a5b4fc; color: #312e81; }
		.autoshift-wall-chart .awc-added { background: #ecfdf5; border-color: #6ee7b7; color: #065f46; }
		.autoshift-wall-chart .awc-dropped {
			background: #fef2f2; border-color: #fca5a5; color: #991b1b;
			text-decoration: line-through;
		}
		.autoshift-wall-chart .awc-uncertain { border-style: dashed; }
		.autoshift-wall-chart .awc-virtual { border-style: dotted; font-style: italic; }
		.autoshift-wall-chart .awc-who.awc-traced {
			outline: 2px solid var(--primary, #2490ef); outline-offset: 1px;
		}
		.autoshift-wall-chart .awc-mark { font-size: 0.7em; vertical-align: super; }
		.autoshift-wall-chart .awc-leave-band { font-style: italic; font-weight: 400; }
		.autoshift-wall-chart .awc-leave-cell {
			white-space: normal; text-align: left; vertical-align: top;
			background: var(--bg-light-gray, #fafafa);
		}
		.autoshift-wall-chart .awc-who.awc-leave { background: #fdf2f8; border-color: #f9a8d4; color: #9d174d; }
		.autoshift-wall-chart .awc-warning {
			border-left: 3px solid var(--yellow-400, #facc15); padding: 0.35rem 0.6rem;
			margin-bottom: 0.35rem; font-size: var(--text-sm); color: var(--text-muted);
			background: var(--bg-light-gray, #fafafa);
		}
		.autoshift-wall-chart .awc-empty-note { padding: 0.75rem 0; color: var(--text-muted); }
		.autoshift-wall-chart .awc-pending {
			display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;
			border-left: 3px solid var(--blue-400, #60a5fa); padding: 0.4rem 0.6rem;
			margin-bottom: 0.5rem; font-size: var(--text-sm);
			background: var(--bg-light-gray, #fafafa);
		}
		.autoshift-wall-chart .awc-pending-who { color: var(--text-muted); }
		.autoshift-wall-chart .awc-today-col { background: var(--blue-50, #eff6ff); }
		.autoshift-wall-chart .awc-today-col.awc-nonworking { background: var(--blue-50, #eff6ff); }
	`;
	const style = document.createElement("style");
	style.id = "autoshift-wall-chart-styles";
	style.textContent = css;
	document.head.appendChild(style);
};

const esc = (value) => frappe.utils.escape_html(String(value == null ? "" : value));

const same_set = (a, b) =>
	(a || []).length === (b || []).length && (a || []).every((value) => (b || []).includes(value));

function day_label(day) {
	const dt = frappe.datetime.str_to_obj(day.date);
	const name = dt.toLocaleDateString(undefined, { weekday: "short" });
	const num = frappe.datetime.str_to_user(day.date).slice(0, 5);
	return `${name}<span class="text-muted"> ${esc(num)}</span>`;
}

// ── the view context ────────────────────────────────────────────────────────

/**
 * Everything the markup functions need that is not the payload itself.
 *
 * One object rather than five positional arguments, because the screen and the
 * printout now differ in more than one way — see the module header. `options`
 * is `{}` for the screen and the export dialog's answers for a printout.
 */
function view(payload, options) {
	options = options || {};
	return {
		columns: columns_of(payload, options),
		run: payload.run,
		width: lane_width(payload),
		// A "today" column on a sheet of paper for next week is noise, and a run's
		// planning window is provenance, so both go with the other working marks.
		today: options.clean ? null : frappe.datetime.get_today(),
		clean: !!options.clean,
		// `== null`, not `!== false`: the dialog's answers come from a Frappe Check,
		// which is 0 or 1 — only an absent option means "the screen's default, on".
		leaves: options.leaves == null ? true : !!options.leaves,
		// One table per section, each repeating the day header: how a long chart
		// breaks across printed pages where it should. The screen stays one table,
		// which is what the sticky header and the single scrollbar want.
		per_section: !!options.per_section,
	};
}

// The day columns to draw, each carrying the index it has in the payload: the
// payload is always seven days wide (`chart.week_dates`), so a cell is always
// looked up by `col.index`, never by its position on screen.
function columns_of(payload, options) {
	const all = payload.days.map((day, index) => Object.assign({}, day, { index }));
	if (!options.working_only) return all;
	const busy = busy_days(payload, options.clean);
	return all.filter((col) => col.working || busy.has(col.index));
}

// A non-working day is dropped from a printout only when it is genuinely empty.
// A stray Saturday assignment is exactly the kind of thing this chart exists to
// show, and a sheet that hid it would be the one place it could not be seen.
// "Empty" means empty *as drawn*, though: a Saturday whose only chip is one the
// sheet is not printing has nothing on it.
function busy_days(payload, clean) {
	const busy = new Set();
	const mark = (entries, index) => entries && entries.length && busy.add(index);
	payload.sections.forEach((section) => {
		section.bands.forEach((band) =>
			band.rows.forEach((lanes) =>
				lanes.forEach((days) =>
					days.forEach(
						(cell, index) => cell && !hidden_chip(cell, clean) && busy.add(index)
					)
				)
			)
		);
		(section.leaves || []).forEach(mark);
	});
	(payload.leaves_unknown || []).forEach(mark);
	return busy;
}

function lane_width(payload) {
	let width = 1;
	payload.sections.forEach((section) =>
		section.bands.forEach((band) => {
			width = Math.max(width, band.lanes.length || 1);
		})
	);
	return width;
}

// ── cells ───────────────────────────────────────────────────────────────────

// A day column carries two independent facts: whether the practice works it at
// all (weekend / Holiday List), and whether the run being compared even looked
// at it. They read differently on purpose — an empty Sunday is nothing, an empty
// day the run skipped is a scope question, and an empty working day is a finding.
function day_classes(day, ctx) {
	const classes = [];
	if (!day.working) classes.push("awc-nonworking");
	if (!ctx.clean && ctx.run && ctx.run.first_day && !day.in_window) classes.push("awc-outside");
	if (ctx.today && day.date === ctx.today) classes.push("awc-today-col");
	return classes;
}

// A chip the current mode does not draw. One case today — a `dropped` chip on a
// clean sheet — but it is asked twice, by `cell_markup` and by the row arithmetic
// in `drawn_rows`, and the two have to agree: a row whose only occupant is never
// printed must not be drawn either.
function hidden_chip(cell, clean) {
	return !!clean && cell.kind === "dropped";
}

function cell_markup(cell, ctx) {
	if (!cell) return "";
	// A finished sheet is the schedule as planned, not a diff against the books.
	// Without its colour and its strikethrough a dropped chip would claim the room
	// is staffed, so it comes off the sheet entirely rather than off its styling —
	// `open_rows` already counts it for nothing, so the hatching stays right.
	if (hidden_chip(cell, ctx.clean)) return "";
	const classes = ["awc-who", ctx.clean ? "awc-plain" : `awc-${cell.kind}`];
	if (!ctx.clean && cell.uncertain) classes.push("awc-uncertain");
	if (!ctx.clean && cell.virtual) classes.push("awc-virtual");
	const title = (
		ctx.clean
			? [cell.employee_name || cell.employee, cell.role]
			: [
					cell.employee_name || cell.employee,
					cell.role,
					cell.branch,
					cell.changed,
					cell.room ? __("Room {0}", [cell.room]) : "",
					cell.max_rooms ? __("{0} of {1} rooms", [cell.rooms, cell.max_rooms]) : "",
					cell.uncertain ? __("role inferred, not recorded") : "",
					cell.virtual
						? __("from the Shift Schedule; no Shift Assignment records it yet")
						: "",
					cell.kind === "dropped"
						? __("on the books; this run does not schedule it")
						: "",
					cell.kind === "added" ? __("proposed; nothing on the books for it") : "",
			  ]
	)
		.filter(Boolean)
		.join(" — ");
	const marks = ctx.clean
		? ""
		: (cell.forced ? `<span class="awc-mark" title="${__("Pinned")}">★</span>` : "") +
		  (cell.changed ? `<span class="awc-mark" title="${esc(cell.changed)}">→</span>` : "");
	return `<span class="${classes.join(" ")}" data-who="${esc(cell.employee)}" title="${esc(
		title
	)}">${esc(cell.label)}</span>${marks}`;
}

// Somebody on leave has no chair, so no cell — but "½" is a fact about the week
// and not a working mark, so it survives a clean sheet. HRMS records a half-day
// as a flag and a date and never says which half, so where the server could not
// tell, the entry is drawn under both halves the person works, marked.
function leave_chip(entry, ctx) {
	const classes = ["awc-who", "awc-leave"];
	if (ctx.clean) classes.push("awc-plain");
	else if (entry.speculative) classes.push("awc-uncertain");
	const title = [
		entry.employee_name || entry.employee,
		entry.leave_type,
		entry.half_day ? __("half day, which half not recorded") : "",
		entry.speculative ? __("speculative") : "",
	]
		.filter(Boolean)
		.join(" — ");
	return `<span class="${classes.join(" ")}" data-who="${esc(entry.employee)}" title="${esc(
		title
	)}">${esc(entry.label)}${entry.half_day ? "½" : ""}</span>`;
}

// ── rows, bands, sections ───────────────────────────────────────────────────

// Every band is a different discipline, so no two need the same lanes — but a day
// boundary has to fall in the same place for every band or the chart cannot be
// read down a column. So the table is `width` lane-columns wide per day (the
// widest band's lane count) and a narrower band spreads its lanes across them
// with colspan, rather than padding with dead cells.
function lane_spans(count, width) {
	const base = Math.floor(width / count);
	const spans = new Array(count).fill(base);
	for (let i = 0; i < width - base * count; i++) spans[i] += 1;
	return spans;
}

/**
 * How many rows of a band to draw.
 *
 * `Discipline Branch Config.rooms_num` is the floor and it is the truth: those
 * rooms exist whether or not anybody is in them, and an empty one is the thing
 * this chart was built to show. Rows *past* it are not rooms at all — they only
 * exist because more people turned up on one half-day than the branch has rooms —
 * so they are drawn only while something in them is still being drawn. Hide the
 * `dropped` chips and the lines they were holding go with them, instead of
 * leaving a blank numbered room the practice does not have.
 *
 * Monotonic by construction: a row is one table row across every lane and every
 * day, so the deepest visible one pulls every row above it along, blanks
 * included. There is no skipping a line in the middle.
 */
function drawn_rows(band, ctx) {
	let rows = Math.min(band.rooms || 0, band.height);
	band.rows.forEach((lanes, index) => {
		if (index + 1 <= rows) return;
		const visible = lanes.some((days) =>
			days.some((cell) => cell && !hidden_chip(cell, ctx.clean))
		);
		if (visible) rows = index + 1;
	});
	return rows;
}

function lane_initials(label) {
	return ((label || "").match(/(?<!\p{L})[\p{L}]/gu) || [])
		.map((character) => character.toUpperCase())
		.join("");
}

function head_markup(ctx) {
	return ctx.columns
		.map((day, position) => {
			const classes = ["awc-day", ...day_classes(day, ctx)];
			if (position) classes.push("awc-day-start");
			const title = day.holiday ? ` title="${esc(day.holiday)}"` : "";
			return `<th class="${classes.join(" ")}" colspan="${ctx.width}"${title}>${day_label(
				day
			)}</th>`;
		})
		.join("");
}

function band_markup(band, ctx) {
	const lanes = band.lanes.length ? band.lanes : [{ key: "_", label: "" }];
	const spans = lane_spans(lanes.length, ctx.width);
	const classes = ["awc-band"];
	// The band itself stays on a clean sheet — these are scheduled people with no
	// room, and dropping them would hide somebody — but its red label does not.
	// "Unplaced" is the word doing the work; the colour was only emphasis.
	if (band.overflow && !ctx.clean) classes.push("awc-overflow");
	const branch = band.branch ? `<span class="awc-band-branch">${esc(band.branch)}</span>` : "";

	// Lane names are per band, not global, because each band is its own
	// discipline: the roles under Monday differ from one band to the next.
	const lane_header = ctx.columns
		.map((day, position) =>
			lanes
				.map((lane, lane_index) => {
					const cls = ["awc-lane", ...day_classes(day, ctx)];
					if (position && !lane_index) cls.push("awc-day-start");
					if (lane.gates_rooms === false) cls.push("awc-aside");
					return `<th class="${cls.join(" ")}" colspan="${
						spans[lane_index]
					}" scope="col" title="${esc(lane.label)}">${esc(
						lane_initials(lane.label)
					)}</th>`;
				})
				.join("")
		)
		.join("");

	const height = drawn_rows(band, ctx);
	const rows = [
		`<tr><th class="${classes.join(" ")}" rowspan="${height + 1}" scope="rowgroup">${esc(
			band.discipline
		)}${branch}</th><td class="awc-ord"></td>${lane_header}</tr>`,
	];
	for (let row = 0; row < height; row++) {
		// A row is a *room* only up to the configured count. Past it the line is an
		// overflow — somebody with nowhere to be — so it gets no number (there is no
		// such room to number) and no hatching (a non-room cannot be half-staffed).
		// The band's own "covers N rooms but only M are configured" warning is what
		// explains the line; a number would have claimed it was a room.
		const is_room = band.numbered && row + 1 <= band.rooms;
		const over = band.numbered && !is_room;
		const cells = [
			`<td class="awc-ord"${
				over
					? ` title="${esc(__("beyond the {0} room(s) configured here", [band.rooms]))}"`
					: ""
			}>${is_room ? row + 1 : ""}</td>`,
		];
		ctx.columns.forEach((day, position) => {
			// Which of this band's rows are genuinely open that day. Any other line
			// either holds nobody, or holds somebody without holding everybody the
			// room needs, so it is hatched: a half-staffed room is not an open room.
			// A set of rows, not a count: a room the solver matched nobody into stays
			// a hole, so the open lines do not have to start at the top.
			const open_rows = new Set((band.open_rows || [])[day.index] || []);
			lanes.forEach((lane, lane_index) => {
				// One cell per line, always: somebody covering two rooms holds two
				// lines and arrives here as two chips, never as one spanning cell.
				const cell = (band.rows[row][lane_index] || [])[day.index];
				const cls = ["awc-cell", ...day_classes(day, ctx)];
				if (position && !lane_index) cls.push("awc-day-start");
				if (lane.gates_rooms === false) cls.push("awc-aside");
				if (is_room && !open_rows.has(row + 1)) cls.push("awc-uncovered");
				cells.push(
					`<td class="${cls.join(" ")}" colspan="${spans[lane_index]}">${cell_markup(
						cell,
						ctx
					)}</td>`
				);
			});
		});
		rows.push(`<tr>${cells.join("")}</tr>`);
	}
	return rows.join("");
}

// The leave row of one section: who is away from *this* half-day. It sits inside
// the section's own table, under the bands, so the morning's absences read down
// the same Monday column the morning's chairs do — which a single list at the
// foot of the chart could never do.
function leave_row_markup(per_day, ctx, label) {
	if (!ctx.leaves || !per_day || !per_day.some((entries) => entries && entries.length))
		return "";
	const cells = ctx.columns
		.map((day, position) => {
			const chips = (per_day[day.index] || [])
				.map((entry) => leave_chip(entry, ctx))
				.join(" ");
			const cls = ["awc-leave-cell", ...day_classes(day, ctx)];
			if (position) cls.push("awc-day-start");
			return `<td class="${cls.join(" ")}" colspan="${ctx.width}">${chips}</td>`;
		})
		.join("");
	return `<tr class="awc-leave-row"><th class="awc-band awc-leave-band" scope="row">${esc(
		label
	)}</th><td class="awc-ord"></td>${cells}</tr>`;
}

function table_markup(body, ctx, title) {
	// The section title lives in the <thead>, not above the table: a printed table
	// repeats its header group on every page, and a title that did not would leave
	// the second page of a long band unlabelled.
	const caption = title
		? `<tr><th class="awc-section-title" colspan="${2 + ctx.columns.length * ctx.width}">${esc(
				title
		  )}</th></tr>`
		: "";
	return `<table class="awc-table"><thead>${caption}<tr>
			<th class="awc-band"></th><th class="awc-ord"></th>${head_markup(ctx)}
		</tr></thead><tbody>${body}</tbody></table>`;
}

function section_markup(section, ctx) {
	// `Unplaced` is the one band that is not drawn empty on purpose: it reports
	// people the configuration cannot place, so with none left to report it has
	// nothing to say. A configured band with no rooms still prints its label —
	// that emptiness *is* the configuration.
	const bands = section.bands
		.filter((band) => !band.overflow || drawn_rows(band, ctx))
		.map((band) => band_markup(band, ctx))
		.join("");
	if (!bands) return "";
	const body = bands + leave_row_markup(section.leaves, ctx, __("On leave"));
	if (ctx.per_section) return table_markup(body, ctx, section.title);
	return `<tr><th class="awc-section-title" colspan="${
		2 + ctx.columns.length * ctx.width
	}">${esc(section.title)}</th></tr>${body}`;
}

// Leave nothing could file under a half-day: the person works neither a rota nor
// a recorded shift that day, so there is no half to put them under. Reported
// rather than dropped — it is still the answer to "where is everybody".
function unknown_leaves_markup(payload, ctx) {
	const row = leave_row_markup(
		payload.leaves_unknown,
		ctx,
		__("On leave — no shift type on record")
	);
	if (!row) return "";
	return ctx.per_section ? table_markup(row, ctx) : row;
}

// ── chrome ──────────────────────────────────────────────────────────────────

// What the legend's trailing chip says the table is showing — shared between
// the on-screen bar and the exported page so they never disagree.
function chart_source_label(payload) {
	const run = payload.run;
	return !run
		? __("Shift Assignments on the books")
		: run.compared
		? __("Run {0} vs. the books", [run.name])
		: __("Shift Assignments on the books — run {0} is {1}", [run.name, run.status]);
}

function legend_markup(payload) {
	const run = payload.run;
	const keys =
		run && run.compared
			? [
					[__("Kept"), "awc-kept"],
					[__("Added"), "awc-added"],
					[__("Dropped"), "awc-dropped"],
			  ]
			: [[__("On the books"), "awc-existing"]];
	// A settled rota day nothing records yet is on the books as far as the optimizer
	// is concerned, so it shares the book-side colours and only its border differs.
	if (payload.pending_bound && payload.pending_bound.count) {
		keys.push([__("From Shift Schedule, not yet recorded"), "awc-existing awc-virtual"]);
	}
	const swatches = [...keys, [__("Room not fully staffed"), "awc-uncovered"]]
		.map(
			([label, cls]) =>
				`<span class="awc-key"><span class="awc-swatch ${cls}"></span>${esc(label)}</span>`
		)
		.join("");
	return `<span class="awc-legend">${swatches}<span class="awc-key">${esc(
		chart_source_label(payload)
	)}</span></span>`;
}

function bar_markup(payload) {
	const monday = frappe.datetime.str_to_user(payload.week);
	return `<div class="awc-bar">
		<button type="button" class="btn btn-default btn-xs awc-prev" title="${__(
			"Previous week"
		)}">&#9664;</button>
		<span class="awc-week">${__("Week of {0}", [esc(monday)])}</span>
		<button type="button" class="btn btn-default btn-xs awc-next" title="${__(
			"Next week"
		)}">&#9654;</button>
		<button type="button" class="btn btn-default btn-xs awc-today">${__("This week")}</button>
		${legend_markup(payload)}
		${pending_toggle_markup(payload)}
		<button type="button" class="btn btn-default btn-xs awc-export" title="${__(
			"Choose what goes on the sheet, then print it — pick “Save as PDF” as the destination"
		)}">${__("Export PDF…")}</button>
		<button type="button" class="btn btn-default btn-xs awc-fullscreen">${__("Fullscreen")}</button>
	</div>`;
}

// Settled schedules the week has no records for. They are already drawn, as virtual
// chips — the optimizer reads them straight off the Shift Schedule, so nothing needs
// creating for a solve. Writing them to the books is a deliberate act, so the offer
// sits behind a toggle rather than in the way: look at the week first, then create.
function pending_toggle_markup(payload) {
	const pending = payload.pending_bound || {};
	if (!pending.count) return "";
	return `<button type="button" class="btn btn-default btn-xs awc-pending-toggle" title="${__(
		"Settled shifts drawn from the Shift Schedule that no Shift Assignment records yet. Counted over the whole week, whatever the chart is filtered to."
	)}">${__("{0} not recorded", [pending.count])}</button>`;
}

function pending_markup(payload) {
	const pending = payload.pending_bound || {};
	if (!pending.count) return "";
	const who = (pending.employee_names || []).join(", ");
	return `<div class="awc-pending" hidden>
		<span>${__(
			"{0} settled shift(s) for {1} practitioner(s) fall in this week per their Shift Schedule, but nothing on the books records them. They are drawn with a dotted border, and the optimizer already plans around them.",
			[pending.count, pending.employees]
		)}</span>
		<button type="button" class="btn btn-xs btn-primary awc-materialize">${__("Create them")}</button>
		<span class="awc-pending-who">${esc(who)}</span>
	</div>`;
}

function totals_markup(payload) {
	const t = payload.totals || {};
	if (!t.capacity) return "";
	const pct = Math.round((100 * t.staffed) / t.capacity);
	const diff =
		payload.run && payload.run.compared
			? ` · ${__("{0} kept, {1} added, {2} dropped", [t.kept, t.added, t.dropped])}`
			: "";
	return `<div class="awc-totals"><b>${t.staffed}</b> ${__("of")} <b>${t.capacity}</b> ${__(
		"configured room-slots fully staffed on working days"
	)} (${pct}%)${diff}</div>`;
}

autoshift.wall_chart.build_html = function (payload) {
	const ctx = view(payload, {});
	const warnings = (payload.warnings || [])
		.map((w) => `<div class="awc-warning">${esc(w)}</div>`)
		.join("");
	const sections = payload.sections
		.map((section) => section_markup(section, ctx))
		.filter(Boolean)
		.join("");
	const leaves = unknown_leaves_markup(payload, ctx);

	if (!sections) {
		const note = `<div class="awc-empty-note">${__(
			"Nothing to draw for this week. The chart's bands come from Discipline Branch Config — one band per (discipline, branch), as tall as its room count."
		)}</div>`;
		const rest = leaves ? `<div class="awc-scroll">${table_markup(leaves, ctx)}</div>` : "";
		return `${bar_markup(payload)}${pending_markup(payload)}${warnings}${note}${rest}`;
	}

	return `${bar_markup(payload)}${totals_markup(payload)}${pending_markup(payload)}${warnings}
		<div class="awc-scroll">${table_markup(sections + leaves, ctx)}</div>`;
};

// ── the filter row ──────────────────────────────────────────────────────────

/**
 * The discipline / branch pickers, in their own row above everything else.
 *
 * Outside the rendered body on purpose: the body is replaced wholesale on every
 * fetch, and a control whose dropdown was open would be destroyed under the
 * reader's cursor. `MultiSelectList` is the control the list view's multi-value
 * filters use — pills, type-to-filter, Select All / Clear All for free — and the
 * same one the Role Matrix page picks its disciplines with.
 *
 * The selection lives on the wrapper, not in module state: two charts can be on
 * screen at once (a form behind, Studio in front) and they must not share a
 * filter. `reload` re-fetches the week currently shown with the new selection,
 * because everything derived from the band set — the sections, the coverage
 * headline, the leave list — is derived server-side.
 */
function build_filters($wrapper, payload, reload) {
	const $host = $wrapper.find(".awc-filters");
	const filters = payload.filters || {};
	// Nothing configured anywhere: the row would offer a choice between no bands
	// and no bands, so the chart keeps its own "configure a Discipline Branch
	// Config first" note and says nothing else.
	if (!filters.disciplines) return;
	if (!filters.disciplines.options.length && !filters.branches.options.length) return;
	if ($host.data("awc-built")) {
		hidden_note($host, filters.hidden);
		return;
	}
	$host.data("awc-built", true);
	$host.html(`
		<span class="awc-filter-label">${__("Show")}</span>
		<div class="awc-filter awc-filter-disciplines"></div>
		<div class="awc-filter awc-filter-branches"></div>
		<button type="button" class="btn btn-default btn-xs awc-filter-clear">${__("All bands")}</button>
		<span class="awc-filter-label awc-filter-gap">${__("Days worked")}</span>
		<div class="awc-filter awc-filter-days awc-filter-weekdays"></div>
		<button type="button" class="btn btn-default btn-xs awc-days-all" title="${__(
			"Count every day of the week as worked"
		)}">${__("All days")}</button>
		<button type="button" class="btn btn-default btn-xs awc-days-week" title="${__(
			"Count Saturday and Sunday as not worked"
		)}">${__("Weekends off")}</button>
		<span class="awc-hidden-note text-muted"></span>
	`);

	const labels = {
		disciplines: __("All disciplines"),
		branches: __("All branches"),
		weekdays: __("Days worked"),
	};
	const controls = {};
	const read = () => ({
		disciplines: controls.disciplines.get_value() || [],
		branches: controls.branches.get_value() || [],
		weekdays: controls.weekdays.get_value() || [],
	});
	// On close rather than on change: picking three disciplines is three clicks
	// inside one dropdown, and refetching after each of them would rebuild the
	// chart under the hand still choosing.
	const settle = () => {
		const next = read();
		const current = selection_of($wrapper);
		if (
			same_set(next.disciplines, current.disciplines) &&
			same_set(next.branches, current.branches) &&
			same_set(next.weekdays, current.weekdays)
		) {
			return;
		}
		$wrapper.data("awc-filters", next);
		reload();
	};

	["disciplines", "branches", "weekdays"].forEach((key) => {
		controls[key] = make_picker(
			$host.find(`.awc-filter-${key}`),
			key,
			labels[key],
			key === "weekdays" ? weekday_options(payload) : filters[key].options || [],
			(filters[key] || {}).selected || [],
			settle
		);
	});
	// The weekday control is seeded with the *effective* set, so it always shows which
	// days the chart believes are worked — including the Mon-Fri the server defaults to
	// where no Holiday List names its weekly offs. Parked on the wrapper too, or the
	// first close would read a difference against an empty selection and refetch for
	// nothing.
	$wrapper.data("awc-filters", read());

	const set_days = (weekdays) => {
		if (same_set(weekdays, read().weekdays)) return;
		controls.weekdays.set_value(weekdays);
		$wrapper.data("awc-filters", read());
		reload();
	};
	$host.find(".awc-days-all").on("click", () => set_days(ALL_WEEKDAYS.slice()));
	$host.find(".awc-days-week").on("click", () => set_days(ALL_WEEKDAYS.slice(0, 5)));

	$host.find(".awc-filter-clear").on("click", () => {
		if (!read().disciplines.length && !read().branches.length) return;
		controls.disciplines.set_value([]);
		controls.branches.set_value([]);
		$wrapper.data("awc-filters", Object.assign(read(), { disciplines: [], branches: [] }));
		reload();
	});
	hidden_note($host, filters.hidden);
}

function make_picker($parent, fieldname, placeholder, options, selected, on_close) {
	const control = frappe.ui.form.make_control({
		df: {
			fieldname: fieldname,
			label: placeholder,
			placeholder: placeholder,
			fieldtype: "MultiSelectList",
			input_class: "input-xs",
			get_data: (txt) => match_options(options, txt),
		},
		parent: $parent,
		only_input: true,
	});
	control.refresh();
	// `refresh` builds the input for a control the dialog/form machinery would
	// otherwise build it for; the guard is `page.add_field`'s own.
	if (!control.$input) control.make_input();
	// Seeded so the closed control can print a selected value's *label* — a
	// Department's own name, not its "… - ABBR" docname — before the dropdown has
	// ever been opened and filled the option list in.
	control._options = control.process_options(normalize_options(options));
	if (selected.length) control.set_value(selected.slice());
	control.$wrapper.on("hidden.bs.dropdown", on_close);
	return control;
}

// `MultiSelectList` interpolates `option.description` straight into each row's
// subtitle, and `process_options` only defaults that field for *string* options —
// so an object option arriving without one renders the literal word "undefined".
// The server always sends one (`layout.filter_options`); this is the belt, for an
// option shape that reaches here from anywhere else.
function normalize_options(options) {
	return (options || []).map((option) => ({
		value: option.value,
		label: option.label || option.value,
		description: option.description || "",
	}));
}

function match_options(options, txt) {
	const all = normalize_options(options);
	const needle = (txt || "").trim().toLowerCase();
	if (!needle) return all;
	// Matched on the subtitle too, the way the control's own type-to-filter does:
	// the subtitle names the other axis, so "B1" finds every discipline run there.
	return all.filter((option) =>
		[option.label, option.value, option.description].some((field) =>
			field.toLowerCase().includes(needle)
		)
	);
}

// A filter is an explicit act, but a scheduled person vanishing from the chart
// never is — so the row says how many half-days it set aside.
function hidden_note($host, hidden) {
	$host
		.find(".awc-hidden-note")
		.text(hidden ? __("{0} half-day(s) hidden by this filter", [hidden]) : "");
}

function selection_of($wrapper) {
	return $wrapper.data("awc-filters") || { disciplines: [], branches: [], weekdays: [] };
}

//: Monday first, matching the payload's column order and `datetime.date.weekday()`.
const ALL_WEEKDAYS = ["0", "1", "2", "3", "4", "5", "6"];

// Weekday names are the browser's job — it has the locale, and the server only ever says
// which weekday a column is. Built off the payload's own days so the control's labels and
// the chart's headers cannot disagree about what Monday is called.
function weekday_options(payload) {
	const named = new Map(
		(payload.days || []).map((day) => [
			String(day.weekday),
			frappe.datetime
				.str_to_obj(day.date)
				.toLocaleDateString(undefined, { weekday: "long" }),
		])
	);
	return ALL_WEEKDAYS.map((weekday) => ({
		value: weekday,
		label: named.get(weekday) || weekday,
		description: "",
	}));
}

// ── export ──────────────────────────────────────────────────────────────────

// A self-contained stylesheet for the exported page: no `var(--…)` theme
// tokens (the popup never loads the desk's CSS), no sticky positioning (the
// export is not scrolled — `<th rowspan>` already keeps a band's label next
// to all of its rows) and no interactive chrome. Colors are the screen
// stylesheet's own fallback values, so the export reads the same way the
// chart does in a browser that has never seen the desk theme.
//
// Sized off three custom properties rather than hard-coded points, so the
// dialog's text-size answer scales the whole sheet consistently; `.awc-plain`
// is the clean sheet's chip, which is to say no chip at all — the grid already
// draws the box, and a second box inside it was only ever carrying a colour.
function export_css(options) {
	const scale = { Compact: 0.86, Normal: 1, Large: 1.2 }[options.size] || 1;
	const pt = (base) => `${(base * scale).toFixed(2)}pt`;
	return `
	:root {
		--awc-cell: ${pt(7.5)};
		--awc-small: ${pt(6.5)};
		--awc-chip: ${pt(7)};
	}
	* { box-sizing: border-box; }
	body {
		font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
		color: #1f2937; margin: 1.2rem;
	}
	.awc-head { margin-bottom: 0.7rem; border-bottom: 1.5px solid #374151; padding-bottom: 0.35rem; }
	.awc-head h1 { margin: 0; font-size: ${pt(15)}; letter-spacing: 0.01em; }
	.awc-head .awc-sub { color: #6b7280; font-size: ${pt(9)}; margin-top: 0.15rem; }
	.awc-legend { display: flex; gap: 0.7rem; flex-wrap: wrap; font-size: ${pt(
		7.5
	)}; color: #6b7280; margin-bottom: 0.6rem; }
	.awc-key { display: inline-flex; align-items: center; gap: 0.3rem; }
	.awc-swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px; border: 1.2px solid; }
	.awc-totals { font-size: ${pt(8.5)}; color: #6b7280; margin-bottom: 0.5rem; }
	.awc-totals b { color: #1f2937; }
	.awc-warning {
		border-left: 3px solid #facc15; padding: 0.3rem 0.55rem; margin-bottom: 0.3rem;
		font-size: ${pt(8)}; color: #4b5563; background: #fafafa;
	}
	table { border-collapse: separate; border-spacing: 0; width: 100%; margin-bottom: 0.8rem; }
	th, td {
		border-bottom: 1px solid #d1d5db; border-right: 1px solid #d1d5db;
		padding: 0.15rem 0.3rem; font-size: var(--awc-cell); text-align: center; white-space: nowrap;
	}
	thead { display: table-header-group; }
	thead th { font-weight: 600; background: #f9fafb; }
	.awc-lane { font-weight: 400; color: #6b7280; font-size: var(--awc-small); }
	.awc-section-title {
		text-align: left; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase;
		background: #eef0f3; font-size: var(--awc-small);
	}
	.awc-band {
		text-align: left; vertical-align: top; min-width: 8rem; max-width: 8rem;
		white-space: normal; font-weight: 600;
	}
	.awc-band-branch { display: block; font-weight: 400; color: #6b7280; font-size: var(--awc-small); }
	.awc-band.awc-overflow { color: #dc2626; }
	.awc-ord { color: #6b7280; font-size: var(--awc-small); min-width: 1.4rem; max-width: 1.4rem; }
	.awc-day-start { border-left: 2px solid #9ca3af; }
	.awc-nonworking { background: #f4f5f6; }
	.awc-outside { opacity: 0.55; }
	.awc-today-col { background: #eff6ff; }
	.awc-cell { vertical-align: middle; }
	.awc-uncovered {
		background-image: repeating-linear-gradient(
			45deg, transparent, transparent 4px, #e5e7eb 4px, #e5e7eb 5px
		);
	}
	.awc-aside { background-color: #fafafa; }
	th.awc-aside { font-style: italic; }
	.awc-who {
		display: inline-block; border: 1.2px solid transparent; border-radius: 3px;
		padding: 0.03rem 0.25rem; font-family: "SFMono-Regular", Consolas, monospace;
		font-size: var(--awc-chip);
	}
	.awc-plain { background: transparent; border-color: transparent; color: #111827; font-weight: 600; }
	.awc-existing { background: #f3f4f6; border-color: #9ca3af; }
	.awc-kept { background: #eef2ff; border-color: #a5b4fc; color: #312e81; }
	.awc-added { background: #ecfdf5; border-color: #6ee7b7; color: #065f46; }
	.awc-dropped { background: #fef2f2; border-color: #fca5a5; color: #991b1b; text-decoration: line-through; }
	.awc-uncertain { border-style: dashed; }
	.awc-virtual { border-style: dotted; font-style: italic; }
	.awc-mark { font-size: 0.7em; vertical-align: super; }
	.awc-leave-band { font-style: italic; font-weight: 500; }
	.awc-leave-cell { white-space: normal; text-align: left; vertical-align: top; background: #fafafa; }
	.awc-who.awc-leave { background: #fdf2f8; border-color: #f9a8d4; color: #9d174d; }
	/* On a clean sheet the leave chips keep a hairline box: several of them share
	   one cell, and without it a row of initials runs together. */
	.awc-who.awc-leave.awc-plain { background: transparent; border-color: #d1d5db; color: #374151; font-weight: 400; }
	/* A row never breaks; a table always may. Avoiding a break inside the table
	   itself would be ignored by every engine the moment one section is taller
	   than a page, and pushed to a fresh page before breaking anyway — so the
	   repeated thead is what makes a break readable, not a ban on breaking. */
	tr { page-break-inside: avoid; }
	table { page-break-inside: auto; }
	@page { size: ${options.orientation === "Portrait" ? "portrait" : "landscape"}; margin: 10mm; }
`;
}

// The sheet's own header. A clean one says what week it is and, where the chart
// was filtered, what it is a chart *of* — printing one discipline's week with no
// label is how a sheet ends up on the wrong wall. Everything that says where a
// chip came from belongs to the working chart and stays there.
function export_head(payload, options) {
	const monday = frappe.datetime.str_to_user(payload.week);
	const days = payload.days || [];
	const span = days.length
		? __("{0} – {1}", [monday, frappe.datetime.str_to_user(days[days.length - 1].date)])
		: monday;
	// Labels, not docnames: a Department is named "<name> - <company abbr>" and
	// that suffix is noise on a sheet where every band shares it (the same reason
	// `layout._department_labels` exists).
	const named = (key) => {
		const side = (payload.filters || {})[key] || {};
		const labels = new Map((side.options || []).map((option) => [option.value, option.label]));
		return (side.selected || []).map((value) => labels.get(value) || value);
	};
	const scope = [...named("disciplines"), ...named("branches")];
	const title = options.heading || __("Week of {0}", [monday]);
	const sub = [span, scope.join(" · ")].filter(Boolean).join(" — ");
	return `<div class="awc-head"><h1>${esc(title)}</h1><div class="awc-sub">${esc(
		sub
	)}</div></div>`;
}

// The exported page's body: same data, same placement functions as the on-screen
// table, just answering the dialog instead of the week-navigation bar.
function export_markup(payload, options) {
	const ctx = view(payload, options);
	const sections = payload.sections
		.map((section) => section_markup(section, ctx))
		.filter(Boolean)
		.join("");
	const leaves = unknown_leaves_markup(payload, ctx);
	const head =
		export_head(payload, options) +
		(options.clean ? "" : legend_markup(payload)) +
		(options.coverage ? totals_markup(payload) : "") +
		(options.clean
			? ""
			: (payload.warnings || [])
					.map((w) => `<div class="awc-warning">${esc(w)}</div>`)
					.join(""));

	if (!sections) {
		return `${head}${
			leaves
				? table_markup(leaves, ctx)
				: `<div>${__("Nothing to draw for this week.")}</div>`
		}`;
	}
	return ctx.per_section
		? `${head}${sections}${leaves}`
		: `${head}${table_markup(sections + leaves, ctx)}`;
}

/**
 * Open the current week in its own window, pre-filled with the print dialog —
 * choosing "Save as PDF" there is the export. A real print, not a server-side
 * render, so what comes out is exactly what the browser just drew: same data,
 * same placement functions, no second rendering pipeline to keep in sync with
 * `build_html`.
 *
 * A new window rather than printing the desk page in place: the desk's own
 * chrome (sidebar, navbar, other panes) would otherwise have to be hidden by
 * CSS trickery that is fragile across themes and print engines, and the
 * popup's stylesheet can stay small and self-contained instead of overriding
 * the desk's.
 *
 * `win` is an already-opened window to fill, for the one caller that has to open
 * it before it has the data — see `run_export`.
 */
autoshift.wall_chart.export_pdf = function (payload, options, win) {
	options = options || {};
	win = win || open_export_window();
	if (!win) return;
	win.document.title =
		options.heading ||
		__("Wall chart — week of {0}", [frappe.datetime.str_to_user(payload.week)]);
	const style = win.document.createElement("style");
	style.textContent = export_css(options);
	win.document.head.appendChild(style);
	win.document.body.innerHTML = export_markup(payload, options);
	win.addEventListener("afterprint", () => win.close());
	win.focus();
	win.print();
};

/**
 * Ask what goes on the sheet, then print it.
 *
 * The dialog is the granularity: a printed chart is read by people who were not
 * in the room when it was solved, so it defaults to a finished sheet — no
 * provenance colours, no ★/→, no legend, no warnings — and lets the planner put
 * any of that back when they are printing it to argue with.
 */
function export_dialog($wrapper, payload, fetch) {
	const filters = payload.filters || {};
	const selected = selection_of($wrapper);
	const dialog = new frappe.ui.Dialog({
		title: __("Export this week"),
		fields: [
			{
				fieldname: "heading",
				fieldtype: "Data",
				label: __("Heading"),
				description: __("Printed above the chart. Defaults to the week's date."),
			},
			{
				fieldname: "disciplines",
				fieldtype: "MultiSelectList",
				label: __("Disciplines"),
				get_data: (txt) => match_options((filters.disciplines || {}).options || [], txt),
				description: __("Nothing selected prints every one."),
			},
			{
				fieldname: "branches",
				fieldtype: "MultiSelectList",
				label: __("Branches"),
				get_data: (txt) => match_options((filters.branches || {}).options || [], txt),
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "clean",
				fieldtype: "Check",
				label: __("Finished sheet"),
				default: 1,
				description: __(
					"Drops everything that says where a shift came from: the kept/added/dropped colours, the ★ and → marks, the dashed and dotted borders, the legend and the warnings. Half-days this run re-planned away come off the sheet entirely — with no colour and no strikethrough left, they would read as staffed. Rooms that are not fully staffed stay hatched."
				),
			},
			{
				fieldname: "leaves",
				fieldtype: "Check",
				label: __("Include who is on leave"),
				default: 1,
			},
			{
				fieldname: "coverage",
				fieldtype: "Check",
				label: __("Include the coverage summary"),
				default: 0,
			},
			{
				fieldname: "working_only",
				fieldtype: "Check",
				label: __("Working days only"),
				default: 1,
				description: __(
					"Drops an empty weekend or holiday column. A non-working day somebody is scheduled on is always kept."
				),
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "per_section",
				fieldtype: "Check",
				label: __("A table per shift type"),
				default: 1,
				description: __(
					"Repeats the day header above each table, so a long chart breaks between pages instead of through a band."
				),
			},
			{
				fieldname: "orientation",
				fieldtype: "Select",
				label: __("Orientation"),
				options: ["Landscape", "Portrait"],
				default: "Landscape",
			},
			{
				fieldname: "size",
				fieldtype: "Select",
				label: __("Text size"),
				options: ["Compact", "Normal", "Large"],
				default: "Normal",
			},
		],
		primary_action_label: __("Print…"),
		primary_action: (values) => {
			dialog.hide();
			run_export(payload, values, fetch);
		},
	});
	dialog.show();
	// After `show`, so the controls exist to take them. The sheet starts at
	// whatever the screen is filtered to, which is almost always what a planner
	// looking at one discipline wanted to print.
	if (selected.disciplines.length) dialog.set_value("disciplines", selected.disciplines.slice());
	if (selected.branches.length) dialog.set_value("branches", selected.branches.slice());
}

function open_export_window() {
	const win = window.open("", "_blank");
	if (!win) {
		frappe.msgprint(
			__(
				"Your browser blocked the export window. Please allow pop-ups for this site and try again."
			)
		);
	}
	return win;
}

// Re-fetched rather than filtered in the browser when the sheet's scope differs
// from the screen's: the sections, the coverage headline and the leave list all
// follow the band set, and `api.get_week_chart` is the only thing that knows how.
function run_export(payload, options, fetch) {
	// The weekday control is a statement about the site's week, not a per-print choice,
	// so the sheet inherits whatever the screen is set to.
	const wanted = {
		disciplines: options.disciplines || [],
		branches: options.branches || [],
		weekdays: ((payload.filters || {}).weekdays || {}).selected || [],
	};
	const shown = payload.filters || {};
	const unchanged =
		same_set(wanted.disciplines, (shown.disciplines || {}).selected || []) &&
		same_set(wanted.branches, (shown.branches || {}).selected || []);
	if (unchanged) {
		autoshift.wall_chart.export_pdf(payload, options);
		return;
	}
	// Opened in the click's own turn and filled later: a window opened after an
	// await has lost the user gesture, and every pop-up blocker eats it.
	const win = open_export_window();
	if (!win) return;
	win.document.body.textContent = __("Preparing the sheet…");
	Promise.resolve(fetch(payload.week, wanted)).then((fresh) => {
		if (fresh) autoshift.wall_chart.export_pdf(fresh, options, win);
		else win.close();
	});
}

// ── render ──────────────────────────────────────────────────────────────────

/**
 * Render the wall chart into `$wrapper`.
 *
 * `fetch` is called with an ISO Monday (or null for the server's default week)
 * and the discipline/branch selection, and returns a promise of the
 * `get_week_chart` payload. The week arrows and the filter row call it again, so
 * navigation costs one round trip and no state lives here beyond the week shown
 * and the selection, both parked on the wrapper.
 */
autoshift.wall_chart.render = function ($wrapper, fetch, week) {
	autoshift.wall_chart.inject_styles();

	if (!$wrapper.hasClass("autoshift-wall-chart")) {
		$wrapper.addClass("autoshift-wall-chart");
		// Two rows, and only the second is ever replaced: the filter controls have
		// to outlive a fetch (see `build_filters`).
		$wrapper.html('<div class="awc-filters"></div><div class="awc-body"></div>');
		$wrapper.on("click", ".awc-fullscreen", () => {
			if (document.fullscreenElement) document.exitFullscreen();
			else $wrapper[0].requestFullscreen?.();
		});
		// Follow one person across the week — the chart prints initials, and two
		// people sharing a pair of them is the normal case, not an edge one.
		$wrapper.on("click", ".awc-who", (event) => {
			const who = $(event.currentTarget).data("who");
			const on = $(event.currentTarget).hasClass("awc-traced");
			$wrapper.find(".awc-traced").removeClass("awc-traced");
			if (!on) $wrapper.find(`.awc-who[data-who="${who}"]`).addClass("awc-traced");
		});
	}

	const $body = $wrapper.find(".awc-body");
	$body.html(`<div class="awc-empty-note">${__("Loading week…")}</div>`);

	return Promise.resolve(fetch(week || null, selection_of($wrapper))).then((payload) => {
		if (!payload) {
			$body.html(`<div class="awc-empty-note">${__("No schedule data.")}</div>`);
			return;
		}
		$body.html(autoshift.wall_chart.build_html(payload));
		build_filters($wrapper, payload, () =>
			autoshift.wall_chart.render($wrapper, fetch, payload.week)
		);
		// The toggle's state outlives a week change, so a planner walking through
		// weeks to create their records does not have to reopen it on every one.
		$body.find(".awc-pending").prop("hidden", !$wrapper.data("awc-pending-open"));
		$body.find(".awc-pending-toggle").on("click", () => {
			const open = !$wrapper.data("awc-pending-open");
			$wrapper.data("awc-pending-open", open);
			$body.find(".awc-pending").prop("hidden", !open);
		});
		$body
			.find(".awc-prev")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, payload.prev_week));
		$body
			.find(".awc-next")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, payload.next_week));
		$body
			.find(".awc-today")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, null));
		$body.find(".awc-export").on("click", () => export_dialog($wrapper, payload, fetch));
		$body.find(".awc-materialize").on("click", () => {
			const pending = payload.pending_bound;
			frappe.require("/assets/autoshift/js/rota.js", () => {
				autoshift.rota
					.create(() =>
						frappe.call({
							method: "autoshift.rota.materialize.materialize_between",
							args: { first: pending.first_day, last: pending.last_day },
							freeze: true,
							freeze_message: __("Creating Shift Assignments…"),
						})
					)
					.then((made) => {
						if (made) {
							frappe.show_alert({
								message: __("{0} Shift Assignment(s) created", [made.created]),
								indicator: made.created ? "green" : "orange",
							});
						}
						autoshift.wall_chart.render($wrapper, fetch, payload.week);
					});
			});
		});
		return payload;
	});
};
