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
		.autoshift-wall-chart .awc-cell { min-width: 3.2rem; cursor: default; }
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
		.autoshift-wall-chart .awc-who.awc-traced {
			outline: 2px solid var(--primary, #2490ef); outline-offset: 1px;
		}
		.autoshift-wall-chart .awc-mark { font-size: 0.7em; vertical-align: super; }
		.autoshift-wall-chart .awc-leaves {
			margin-top: 0.6rem; font-size: var(--text-sm);
		}
		.autoshift-wall-chart .awc-leaves table { width: auto; }
		.autoshift-wall-chart .awc-leaves .awc-who { background: #fdf2f8; border-color: #f9a8d4; color: #9d174d; }
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

function day_label(day) {
	const dt = frappe.datetime.str_to_obj(day.date);
	const name = dt.toLocaleDateString(undefined, { weekday: "short" });
	const num = frappe.datetime.str_to_user(day.date).slice(0, 5);
	return `${name}<span class="text-muted"> ${esc(num)}</span>`;
}

// A day column carries two independent facts: whether the practice works it at
// all (weekend / Holiday List), and whether the run being compared even looked
// at it. They read differently on purpose — an empty Sunday is nothing, an empty
// day the run skipped is a scope question, and an empty working day is a finding.
function day_classes(day, run, today) {
	const classes = [];
	if (!day.working) classes.push("awc-nonworking");
	if (run && run.first_day && !day.in_window) classes.push("awc-outside");
	if (today && day.date === today) classes.push("awc-today-col");
	return classes;
}

function cell_markup(cell) {
	if (!cell) return "";
	const classes = ["awc-who", `awc-${cell.kind}`];
	if (cell.uncertain) classes.push("awc-uncertain");
	const title = [
		cell.employee_name || cell.employee,
		cell.role,
		cell.branch,
		cell.changed,
		cell.uncertain ? __("role inferred, not recorded") : "",
		cell.kind === "dropped" ? __("on the books; this run does not schedule it") : "",
		cell.kind === "added" ? __("proposed; nothing on the books for it") : "",
	]
		.filter(Boolean)
		.join(" — ");
	const marks =
		(cell.forced ? `<span class="awc-mark" title="${__("Pinned")}">★</span>` : "") +
		(cell.changed ? `<span class="awc-mark" title="${esc(cell.changed)}">→</span>` : "");
	return `<span class="${classes.join(" ")}" data-who="${esc(cell.employee)}" title="${esc(
		title
	)}">${esc(cell.label)}</span>${marks}`;
}

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

function head_markup(days, run, width, today) {
	return days
		.map((day, index) => {
			const classes = ["awc-day", ...day_classes(day, run, today)];
			if (index) classes.push("awc-day-start");
			const title = day.holiday ? ` title="${esc(day.holiday)}"` : "";
			return `<th class="${classes.join(" ")}" colspan="${width}"${title}>${day_label(
				day
			)}</th>`;
		})
		.join("");
}

function band_markup(band, days, run, width, today) {
	const lanes = band.lanes.length ? band.lanes : [{ key: "_", label: "" }];
	const spans = lane_spans(lanes.length, width);
	const classes = ["awc-band"];
	if (band.overflow) classes.push("awc-overflow");
	const branch = band.branch ? `<span class="awc-band-branch">${esc(band.branch)}</span>` : "";

	// Lane names are per band, not global, because each band is its own
	// discipline: the roles under Monday differ from one band to the next.
	const lane_header = days
		.map((day, day_index) =>
			lanes
				.map((lane, lane_index) => {
					const cls = ["awc-lane", ...day_classes(day, run, today)];
					if (day_index && !lane_index) cls.push("awc-day-start");
					return `<th class="${cls.join(" ")}" colspan="${
						spans[lane_index]
					}" scope="col" title="${lane.label}">${esc(
						lane.label
							.match(/(?<!\p{L})[\p{L}]/gu)
							.map((c) => c.toUpperCase())
							.join("")
					)}</th>`;
				})
				.join("")
		)
		.join("");

	const rows = [
		`<tr><th class="${classes.join(" ")}" rowspan="${band.height + 1}" scope="rowgroup">${esc(
			band.discipline
		)}${branch}</th><td class="awc-ord"></td>${lane_header}</tr>`,
	];
	for (let row = 0; row < band.height; row++) {
		const cells = [`<td class="awc-ord">${band.numbered ? row + 1 : ""}</td>`];
		days.forEach((day, day_index) => {
			lanes.forEach((_lane, lane_index) => {
				const cls = ["awc-cell", ...day_classes(day, run, today)];
				if (day_index && !lane_index) cls.push("awc-day-start");
				cells.push(
					`<td class="${cls.join(" ")}" colspan="${spans[lane_index]}">${cell_markup(
						(band.rows[row][lane_index] || [])[day_index]
					)}</td>`
				);
			});
		});
		rows.push(`<tr>${cells.join("")}</tr>`);
	}
	return rows.join("");
}

function section_markup(section, days, run, width, today) {
	const bands = section.bands.map((band) => band_markup(band, days, run, width, today)).join("");
	if (!bands) return "";
	return `<tr><th class="awc-section-title" colspan="${2 + days.length * width}">${esc(
		section.title
	)}</th></tr>${bands}`;
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

function leaves_markup(payload) {
	const entries = payload.days
		.map((day) => [day, payload.leaves[day.date] || []])
		.filter(([, people]) => people.length);
	if (!entries.length) return "";
	const rows = entries
		.map(([day, people]) => {
			const chips = people
				.map(
					(p) =>
						`<span class="awc-who" data-who="${esc(p.employee)}" title="${esc(
							`${p.employee_name || p.employee} — ${p.leave_type}${
								p.speculative ? ` (${__("speculative")})` : ""
							}`
						)}">${esc(p.label)}</span>`
				)
				.join(" ");
			return `<tr><td class="awc-band">${day_label(day)}</td><td>${chips}</td></tr>`;
		})
		.join("");
	return `<div class="awc-leaves"><b>${__(
		"On leave this week"
	)}</b><table>${rows}</table></div>`;
}

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
	const swatches = keys
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
		<button type="button" class="btn btn-default btn-xs awc-export" title="${__(
			"Opens a print dialog — choose “Save as PDF” as the destination"
		)}">${__("Export PDF")}</button>
		<button type="button" class="btn btn-default btn-xs awc-fullscreen">${__("Fullscreen")}</button>
	</div>`;
}

// Settled schedules the week is missing. HRMS is supposed to generate these from
// the Shift Schedule and cannot for a rota longer than a week (see autoshift/rota),
// so the chart offers to — landing on a week nobody has generated yet is exactly
// the moment somebody is in a position to say yes.
function pending_markup(payload) {
	const pending = payload.pending_bound || {};
	if (!pending.count) return "";
	const who = (pending.employee_names || []).join(", ");
	return `<div class="awc-pending">
		<span>${__(
			"{0} settled shift(s) for {1} practitioner(s) fall in this week per their Shift Schedule, but nothing on the books records them.",
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
		"configured room-slots staffed on working days"
	)} (${pct}%)${diff}</div>`;
}

autoshift.wall_chart.build_html = function (payload) {
	const { days, run } = payload;
	const today = frappe.datetime.get_today();
	const warnings = (payload.warnings || [])
		.map((w) => `<div class="awc-warning">${esc(w)}</div>`)
		.join("");
	const width = lane_width(payload);
	const sections = payload.sections
		.map((section) => section_markup(section, days, run, width, today))
		.filter(Boolean)
		.join("");

	if (!sections) {
		return `${bar_markup(payload)}${pending_markup(
			payload
		)}${warnings}<div class="awc-empty-note">${__(
			"Nothing to draw for this week. The chart's bands come from Discipline Branch Config — one band per (discipline, branch), as tall as its room count."
		)}</div>`;
	}

	return `${bar_markup(payload)}${totals_markup(payload)}${pending_markup(payload)}${warnings}
		<div class="awc-scroll"><table>
			<thead><tr>
				<th class="awc-band"></th><th class="awc-ord"></th>${head_markup(days, run, width, today)}
			</tr></thead>
			<tbody>${sections}</tbody>
		</table></div>${leaves_markup(payload)}`;
};

// A self-contained stylesheet for the exported page: no `var(--…)` theme
// tokens (the popup never loads the desk's CSS), no sticky positioning (the
// export is not scrolled — `<th rowspan>` already keeps a band's label next
// to all of its rows) and no interactive chrome. Colors are the screen
// stylesheet's own fallback values, so the export reads the same way the
// chart does in a browser that has never seen the desk theme.
const EXPORT_CSS = `
	* { box-sizing: border-box; }
	body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; color: #1f2937; margin: 1.2rem; }
	h2 { margin: 0 0 0.15rem; font-size: 1.1rem; }
	.awc-legend { display: flex; gap: 0.7rem; flex-wrap: wrap; font-size: 0.75rem; color: #6b7280; margin-bottom: 0.6rem; }
	.awc-key { display: inline-flex; align-items: center; gap: 0.3rem; }
	.awc-swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px; border: 1.2px solid; }
	.awc-totals { font-size: 0.85rem; color: #6b7280; margin-bottom: 0.5rem; }
	.awc-totals b { color: #1f2937; }
	.awc-warning {
		border-left: 3px solid #facc15; padding: 0.3rem 0.55rem; margin-bottom: 0.3rem;
		font-size: 0.8rem; color: #4b5563; background: #fafafa;
	}
	table { border-collapse: separate; border-spacing: 0; width: 100%; margin-bottom: 0.8rem; }
	th, td {
		border-bottom: 1px solid #d1d5db; border-right: 1px solid #d1d5db;
		padding: 0.15rem 0.3rem; font-size: 7.5pt; text-align: center; white-space: nowrap;
	}
	thead { display: table-header-group; }
	thead th { font-weight: 600; background: #f9fafb; }
	.awc-lane { font-weight: 400; color: #6b7280; font-size: 6.5pt; }
	.awc-section-title {
		text-align: left; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase;
		background: #f3f4f6; font-size: 6.5pt;
	}
	.awc-band {
		text-align: left; vertical-align: top; min-width: 8rem; max-width: 8rem;
		white-space: normal; font-weight: 600;
	}
	.awc-band-branch { display: block; font-weight: 400; color: #6b7280; font-size: 6.5pt; }
	.awc-band.awc-overflow { color: #dc2626; }
	.awc-ord { color: #6b7280; font-size: 6.5pt; min-width: 1.4rem; max-width: 1.4rem; }
	.awc-day-start { border-left: 2px solid #9ca3af; }
	.awc-nonworking { background: #f4f5f6; }
	.awc-outside { opacity: 0.55; }
	.awc-today-col { background: #eff6ff; }
	.awc-who {
		display: inline-block; border: 1.2px solid transparent; border-radius: 3px;
		padding: 0.03rem 0.25rem; font-family: "SFMono-Regular", Consolas, monospace; font-size: 7pt;
	}
	.awc-existing { background: #f3f4f6; border-color: #9ca3af; }
	.awc-kept { background: #eef2ff; border-color: #a5b4fc; color: #312e81; }
	.awc-added { background: #ecfdf5; border-color: #6ee7b7; color: #065f46; }
	.awc-dropped { background: #fef2f2; border-color: #fca5a5; color: #991b1b; text-decoration: line-through; }
	.awc-uncertain { border-style: dashed; }
	.awc-mark { font-size: 0.7em; vertical-align: super; }
	.awc-leaves { margin-top: 0.5rem; font-size: 0.8rem; }
	.awc-leaves table { width: auto; }
	.awc-leaves .awc-who { background: #fdf2f8; border-color: #f9a8d4; color: #9d174d; }
	tr { page-break-inside: avoid; }
	@page { size: landscape; margin: 10mm; }
`;

// The exported page's body: same data, same placement functions as the
// on-screen table (`section_markup`, `head_markup`, `leaves_markup`,
// `totals_markup`), just without the week-navigation and fullscreen controls
// that make no sense on paper.
function export_markup(payload) {
	const { days, run } = payload;
	const today = frappe.datetime.get_today();
	const width = lane_width(payload);
	const sections = payload.sections
		.map((section) => section_markup(section, days, run, width, today))
		.filter(Boolean)
		.join("");
	const warnings = (payload.warnings || [])
		.map((w) => `<div class="awc-warning">${esc(w)}</div>`)
		.join("");
	const title = __("Week of {0}", [esc(frappe.datetime.str_to_user(payload.week))]);
	const heading = `<h2>${title}</h2>${legend_markup(payload)}`;

	if (!sections) {
		return `${heading}<div>${__("Nothing to draw for this week.")}</div>`;
	}

	return `${heading}${totals_markup(payload)}${warnings}
		<table>
			<thead><tr>
				<th class="awc-band"></th><th class="awc-ord"></th>${head_markup(days, run, width, today)}
			</tr></thead>
			<tbody>${sections}</tbody>
		</table>${leaves_markup(payload)}`;
}

/**
 * Open the current week in its own window, pre-filled with the print dialog —
 * choosing "Save as PDF" there is the export. A real print, not a server-side
 * render, so what comes out is exactly what the browser just showed: same
 * data, same placement, no second rendering pipeline to keep in sync with
 * `build_html`.
 *
 * A new window rather than printing the desk page in place: the desk's own
 * chrome (sidebar, navbar, other panes) would otherwise have to be hidden by
 * CSS trickery that is fragile across themes and print engines, and the
 * popup's stylesheet can stay small and self-contained instead of overriding
 * the desk's.
 */
autoshift.wall_chart.export_pdf = function (payload) {
	const win = window.open("", "_blank");
	if (!win) {
		frappe.msgprint(
			__(
				"Your browser blocked the export window. Please allow pop-ups for this site and try again."
			)
		);
		return;
	}
	win.document.title = __("Wall chart — week of {0}", [
		frappe.datetime.str_to_user(payload.week),
	]);
	const style = win.document.createElement("style");
	style.textContent = EXPORT_CSS;
	win.document.head.appendChild(style);
	win.document.body.innerHTML = export_markup(payload);
	win.addEventListener("afterprint", () => win.close());
	win.focus();
	win.print();
};

/**
 * Render the wall chart into `$wrapper`.
 *
 * `fetch` is called with an ISO Monday (or null for the server's default week)
 * and returns a promise of the `get_week_chart` payload. The week arrows call it
 * again, so navigation costs one round trip and no state lives here beyond the
 * week currently shown.
 */
autoshift.wall_chart.render = function ($wrapper, fetch, week) {
	autoshift.wall_chart.inject_styles();

	if (!$wrapper.hasClass("autoshift-wall-chart")) {
		$wrapper.addClass("autoshift-wall-chart");
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

	$wrapper.html(`<div class="awc-empty-note">${__("Loading week…")}</div>`);

	return Promise.resolve(fetch(week || null)).then((payload) => {
		if (!payload) {
			$wrapper.html(`<div class="awc-empty-note">${__("No schedule data.")}</div>`);
			return;
		}
		$wrapper.html(autoshift.wall_chart.build_html(payload));
		$wrapper
			.find(".awc-prev")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, payload.prev_week));
		$wrapper
			.find(".awc-next")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, payload.next_week));
		$wrapper
			.find(".awc-today")
			.on("click", () => autoshift.wall_chart.render($wrapper, fetch, null));
		$wrapper.find(".awc-export").on("click", () => autoshift.wall_chart.export_pdf(payload));
		$wrapper.find(".awc-materialize").on("click", () => {
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
