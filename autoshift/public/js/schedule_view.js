// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// The one place a schedule is looked at, wherever you are looking at it from.
//
// Four ways of reading the same run, behind a tab bar shared by the Optimizer Run
// form and Optimizer Studio so neither drifts from the other:
//
//   Week         the wall chart (public/js/wall_chart.js) — the default, and the
//                only pane that works on a run in any state, because it falls
//                back to the Shift Assignments on the books.
//   Statistics   run_stats.js — coverage meters, FTE gaps, objective shares.
//   Roster       schedule_grid.js — the per-employee grid, which answers "what
//                did this person get" where the wall chart answers "is the
//                practice covered".
//   Solver Log   CBC's output, verbatim.
//
// The last three need a solved run and say so rather than disappearing: a tab
// that vanishes reads as a missing feature, one that explains itself reads as a
// state. The Week tab is never in that position, which is the point of the
// redesign — a failed solve is exactly when somebody needs to see the week.
//
// NOTE: no `import`/`export` here, deliberately — see bulk_employee_settings.js.

frappe.provide("autoshift.schedule_view");

// On the namespace rather than a top-level `const`: a plain script re-evaluated
// (a second frappe.require after a cache miss, a hot reload) would throw on a
// duplicate lexical declaration, and a silently broken tab bar is a bad trade
// for two saved characters.
autoshift.schedule_view.panes = [
	{ key: "week", label: "Week", asset: "wall_chart.js" },
	{ key: "stats", label: "Statistics", asset: "run_stats.js", needs_solution: true },
	{ key: "roster", label: "Roster", asset: "schedule_grid.js", needs_solution: true },
	// Only needs a run, not a solution: a Failed run is exactly the one whose log
	// somebody wants, and that is the state the old form hid it in.
	{ key: "log", label: "Solver Log", needs_run: true },
];

function available(pane, options) {
	if (pane.needs_solution) {
		return ["Solved", "Approved", "Committed"].includes(options.status);
	}
	return !pane.needs_run || Boolean(options.run);
}

autoshift.schedule_view.inject_styles = function () {
	if (document.getElementById("autoshift-schedule-view-styles")) return;
	const css = `
		.autoshift-schedule-view .asv-tabs {
			display: flex; gap: 0.25rem; border-bottom: 1px solid var(--border-color);
			margin-bottom: 0.9rem;
		}
		.autoshift-schedule-view .asv-tab {
			border: none; background: none; padding: 0.45rem 0.85rem; cursor: pointer;
			color: var(--text-muted); border-bottom: 2px solid transparent;
			font-size: var(--text-md); border-radius: 0;
		}
		.autoshift-schedule-view .asv-tab:hover { color: var(--text-color); }
		.autoshift-schedule-view .asv-tab.active {
			color: var(--text-color); border-bottom-color: var(--primary, #2490ef); font-weight: 500;
		}
		.autoshift-schedule-view .asv-tab[disabled] { opacity: 0.45; cursor: default; }
		.autoshift-schedule-view .asv-pane { display: none; }
		.autoshift-schedule-view .asv-pane.active { display: block; }
		.autoshift-schedule-view .asv-note { color: var(--text-muted); padding: 0.75rem 0; }
		.autoshift-schedule-view .asv-log {
			background: var(--bg-light-gray, #fafafa); border: 1px solid var(--border-color);
			border-radius: var(--border-radius); padding: 0.75rem; font-size: var(--text-xs);
			overflow: auto; max-height: 32rem; white-space: pre-wrap;
		}
	`;
	const style = document.createElement("style");
	style.id = "autoshift-schedule-view-styles";
	style.textContent = css;
	document.head.appendChild(style);
};

function pane_html() {
	return autoshift.schedule_view.panes
		.map((p) => `<div class="asv-pane asv-pane-${p.key}" data-pane="${p.key}"></div>`)
		.join("");
}

function tabs_html(options) {
	return autoshift.schedule_view.panes
		.map(
			(p) =>
				`<button type="button" class="asv-tab asv-tab-${p.key}" data-pane="${p.key}"${
					available(p, options) ? "" : ` disabled title="${__("Needs a solved run")}"`
				}>${__(p.label)}</button>`
		)
		.join("");
}

/**
 * Render the tabbed schedule view into `$wrapper`.
 *
 * `options`:
 *   run             Optimizer Run name, or null (Studio before its first preview)
 *   status          the run's status, for deciding which panes have anything to show
 *   week            ISO date to open the Week tab on; null lets the server decide
 *   statistics()    promise of the run-statistics payload
 *   schedule()      promise of the roster-grid payload
 *   solver_log()    promise of a string
 *   active          which tab to open on; defaults to the last one used, else Week
 *
 * Panes are rendered lazily on first activation and cached, so switching tabs is
 * free and a heavy pane costs nothing until it is looked at.
 */
autoshift.schedule_view.render = function ($wrapper, options) {
	autoshift.schedule_view.inject_styles();

	if (!$wrapper.hasClass("autoshift-schedule-view")) {
		$wrapper.addClass("autoshift-schedule-view");
	}
	$wrapper.html(`<div class="asv-tabs">${tabs_html(options)}</div>${pane_html()}`);

	const rendered = {};
	const show = (key) => {
		const pane = autoshift.schedule_view.panes.find((p) => p.key === key);
		if (!pane || !available(pane, options)) return;
		$wrapper.find(".asv-tab").removeClass("active");
		$wrapper.find(`.asv-tab-${key}`).addClass("active");
		$wrapper.find(".asv-pane").removeClass("active");
		const $pane = $wrapper.find(`.asv-pane-${key}`).addClass("active");
		// Remembered on the wrapper, not in module state: two of these can be on
		// screen at once (a form in the background, Studio in front) and they
		// should not fight over which tab is open.
		$wrapper.data("asv-active", key);
		if (rendered[key]) return;
		rendered[key] = true;
		fill(pane, $pane, options);
	};

	$wrapper.find(".asv-tab").on("click", (event) => show($(event.currentTarget).data("pane")));
	// A remembered tab can have become unavailable — a re-run leaves a Draft where
	// a Solved run was — so Week is the fallback, never "no tab at all".
	const wanted = options.active || $wrapper.data("asv-active") || "week";
	const pane = autoshift.schedule_view.panes.find((p) => p.key === wanted);
	show(pane && available(pane, options) ? wanted : "week");
	return { show };
};

function fill(pane, $pane, options) {
	if (pane.key === "log") {
		$pane.html(`<div class="asv-note">${__("Loading…")}</div>`);
		Promise.resolve(options.solver_log ? options.solver_log() : "").then((log) => {
			$pane.html(
				log
					? `<pre class="asv-log">${frappe.utils.escape_html(log)}</pre>`
					: `<div class="asv-note">${__("This run recorded no solver output.")}</div>`
			);
		});
		return;
	}
	frappe.require(`/assets/autoshift/js/${pane.asset}`, () => {
		if (pane.key === "week") {
			autoshift.wall_chart.render($pane, options.week_chart, options.week);
		} else if (pane.key === "stats") {
			autoshift.run_stats.render($pane, options.statistics);
		} else {
			autoshift.schedule_grid.render($pane, options.schedule);
		}
	});
}
