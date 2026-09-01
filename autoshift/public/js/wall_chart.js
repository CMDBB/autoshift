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
function day_classes(day, run) {
	const classes = [];
	if (!day.working) classes.push("awc-nonworking");
	if (run && run.first_day && !day.in_window) classes.push("awc-outside");
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

function head_markup(days, run, width) {
	return days
		.map((day, index) => {
			const classes = ["awc-day", ...day_classes(day, run)];
			if (index) classes.push("awc-day-start");
			const title = day.holiday ? ` title="${esc(day.holiday)}"` : "";
			return `<th class="${classes.join(" ")}" colspan="${width}"${title}>${day_label(
				day
			)}</th>`;
		})
		.join("");
}

function band_markup(band, days, run, width) {
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
					const cls = ["awc-lane", ...day_classes(day, run)];
					if (day_index && !lane_index) cls.push("awc-day-start");
					return `<th class="${cls.join(" ")}" colspan="${
						spans[lane_index]
					}" scope="col">${esc(lane.label)}</th>`;
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
				const cls = ["awc-cell", ...day_classes(day, run)];
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

function section_markup(section, days, run, width) {
	const bands = section.bands.map((band) => band_markup(band, days, run, width)).join("");
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

function bar_markup(payload) {
	const monday = frappe.datetime.str_to_user(payload.week);
	const run = payload.run;
	const source = !run
		? __("Shift Assignments on the books")
		: run.compared
		? __("Run {0} vs. the books", [run.name])
		: __("Shift Assignments on the books — run {0} is {1}", [run.name, run.status]);
	const keys =
		run && run.compared
			? [
					[__("Kept"), "awc-kept"],
					[__("Added"), "awc-added"],
					[__("Dropped"), "awc-dropped"],
			  ]
			: [[__("On the books"), "awc-existing"]];
	const legend = keys
		.map(
			([label, cls]) =>
				`<span class="awc-key"><span class="awc-swatch ${cls}"></span>${esc(label)}</span>`
		)
		.join("");
	return `<div class="awc-bar">
		<button type="button" class="btn btn-default btn-xs awc-prev" title="${__(
			"Previous week"
		)}">&#9664;</button>
		<span class="awc-week">${__("Week of {0}", [esc(monday)])}</span>
		<button type="button" class="btn btn-default btn-xs awc-next" title="${__(
			"Next week"
		)}">&#9654;</button>
		<button type="button" class="btn btn-default btn-xs awc-today">${__("This week")}</button>
		<span class="awc-legend">${legend}<span class="awc-key">${esc(
		source
	)}</span><button type="button" class="btn btn-default btn-xs awc-fullscreen">${__(
		"Fullscreen"
	)}</button></span>
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
	const warnings = (payload.warnings || [])
		.map((w) => `<div class="awc-warning">${esc(w)}</div>`)
		.join("");
	const width = lane_width(payload);
	const sections = payload.sections
		.map((section) => section_markup(section, days, run, width))
		.filter(Boolean)
		.join("");

	if (!sections) {
		return `${bar_markup(payload)}${warnings}<div class="awc-empty-note">${__(
			"Nothing to draw for this week. The chart's bands come from Discipline Branch Config — one band per (discipline, branch), as tall as its room count."
		)}</div>`;
	}

	return `${bar_markup(payload)}${totals_markup(payload)}${warnings}
		<div class="awc-scroll"><table>
			<thead><tr>
				<th class="awc-band"></th><th class="awc-ord"></th>${head_markup(days, run, width)}
			</tr></thead>
			<tbody>${sections}</tbody>
		</table></div>${leaves_markup(payload)}`;
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
		return payload;
	});
};
