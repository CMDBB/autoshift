// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// The horizon scanner's calendars: months across the page, each day coloured by what the
// window covering it solved to. Rendered by the Horizon Scanner page and by the Horizon
// Scan form's Calendar tab off the same `autoshift.scanner.api.get_scan` payload, which
// settles every judgement server-side — this file decides nothing but layout.
//
// NOTE: no `import` here, deliberately — see bulk_employee_settings.js for why. Page and
// doctype scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift.horizon_calendar");

// Everything below is wrapped so that not one helper reaches the global object: these
// are plain scripts sharing one global scope, and `make_picker`/`build_filters` are names
// wall_chart.js already uses for its own (see schedule_view.js on re-evaluation, too).
// Only the namespace above is public.
(function () {
	// Worst wins, server-side; these only name the colours and the words.
	const AHZ_SEVERITIES = ["failed", "critical", "warning", "info", "ok"];

	function ahz_labels() {
		return {
			failed: __("No schedule"),
			critical: __("Rooms shut"),
			warning: __("Short"),
			info: __("Under FTE"),
			ok: __("Covered"),
			unscanned: __("Not scanned"),
			closed: __("Closed"),
		};
	}

	autoshift.horizon_calendar.inject_styles = function () {
		if (document.getElementById("autoshift-horizon-styles")) return;
		const css = `
		.autoshift-horizon .ahz-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; margin-bottom: 0.6rem; }
		.autoshift-horizon .ahz-label { color: var(--text-muted); font-size: var(--text-sm); }
		.autoshift-horizon .ahz-filter { min-width: 11rem; }
		.autoshift-horizon .ahz-progress {
			flex: 1 1 8rem; height: 6px; border-radius: 3px; background: var(--control-bg);
			overflow: hidden; min-width: 6rem; max-width: 18rem;
		}
		.autoshift-horizon .ahz-progress > div { height: 100%; background: var(--blue-500, #2490ef); }
		.autoshift-horizon .ahz-months {
			display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 0.9rem;
		}
		.autoshift-horizon .ahz-month { border: 1px solid var(--border-color); border-radius: var(--border-radius); padding: 0.5rem; }
		.autoshift-horizon .ahz-month-label { font-weight: 600; margin-bottom: 0.3rem; }
		.autoshift-horizon .ahz-grid { width: 100%; border-collapse: collapse; table-layout: fixed; }
		.autoshift-horizon .ahz-grid th {
			font-weight: normal; color: var(--text-muted); font-size: var(--text-xs); text-align: center; padding: 0.1rem;
		}
		.autoshift-horizon .ahz-grid td { padding: 1px; text-align: center; }
		.autoshift-horizon .ahz-day {
			display: block; border: 1px solid transparent; border-radius: var(--border-radius);
			font-size: var(--text-xs); line-height: 1.5; padding: 0.15rem 0; cursor: default;
		}
		.autoshift-horizon .ahz-day-num { font-variant-numeric: tabular-nums; }
		.autoshift-horizon .ahz-day-mark { font-size: 0.65rem; display: block; opacity: 0.8; min-height: 0.8rem; }
		/* A scanned day is clickable because there is a run behind it to open. */
		.autoshift-horizon .ahz-day[data-scanned="1"] { cursor: pointer; }
		.autoshift-horizon .ahz-sev-ok { background: var(--green-100, #e8f5e9); border-color: var(--green-300, #a5d6a7); }
		.autoshift-horizon .ahz-sev-info { background: var(--blue-100, #e3f2fd); border-color: var(--blue-300, #90caf9); }
		.autoshift-horizon .ahz-sev-warning { background: var(--orange-100, #fff3e0); border-color: var(--orange-300, #ffcc80); }
		.autoshift-horizon .ahz-sev-critical { background: var(--red-100, #ffebee); border-color: var(--red-300, #ef9a9a); }
		.autoshift-horizon .ahz-sev-failed {
			background: var(--red-200, #ffcdd2); border-color: var(--red-500, #e53935); font-weight: 600;
		}
		/* Nothing has judged this day. Deliberately not the "covered" colour: an
		   unscanned day is not a clean day, and that is the one lie this view cannot tell. */
		.autoshift-horizon .ahz-sev-unscanned { border-style: dashed; border-color: var(--border-color); color: var(--text-muted); }
		.autoshift-horizon .ahz-sev-closed { color: var(--text-light); }
		.autoshift-horizon .ahz-selected { outline: 2px solid var(--text-color); outline-offset: 1px; }
		.autoshift-horizon .ahz-legend { display: flex; flex-wrap: wrap; gap: 0.6rem; font-size: var(--text-sm); }
		.autoshift-horizon .ahz-chip { display: inline-flex; align-items: center; gap: 0.3rem; }
		.autoshift-horizon .ahz-swatch {
			width: 0.8rem; height: 0.8rem; border-radius: 2px; border: 1px solid var(--border-color); display: inline-block;
		}
		.autoshift-horizon .ahz-detail, .autoshift-horizon .ahz-chart { margin-top: 1rem; }
		.autoshift-horizon .ahz-detail-card {
			border: 1px solid var(--border-color); border-radius: var(--border-radius); padding: 0.75rem;
		}
		.autoshift-horizon .ahz-detail-head { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: baseline; }
		.autoshift-horizon .ahz-detail-head h5 { margin: 0; }
		.autoshift-horizon .ahz-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); margin-top: 0.5rem; }
		.autoshift-horizon .ahz-table th, .autoshift-horizon .ahz-table td {
			border-bottom: 1px solid var(--border-color); padding: 0.2rem 0.4rem; text-align: left;
		}
		.autoshift-horizon .ahz-table td.ahz-num { text-align: right; font-variant-numeric: tabular-nums; }
		.autoshift-horizon .ahz-note { color: var(--text-muted); font-size: var(--text-sm); }
		.autoshift-horizon .ahz-windows { max-height: 11rem; overflow-y: auto; margin-top: 0.5rem; }
	`;
		$("<style id='autoshift-horizon-styles'></style>").text(css).appendTo(document.head);
	};

	// ── state ───────────────────────────────────────────────────────────────────

	function ahz_state($wrapper, patch) {
		const current = $wrapper.data("ahz-state") || {
			scan: null,
			scaled: 0,
			disciplines: [],
			branches: [],
			date: null,
		};
		if (patch) {
			$wrapper.data("ahz-state", Object.assign(current, patch));
		}
		return $wrapper.data("ahz-state") || current;
	}

	function ahz_same_set(a, b) {
		return a.length === b.length && a.every((value) => b.includes(value));
	}

	// ── render ──────────────────────────────────────────────────────────────────

	/**
	 * Draw the scanner into `$wrapper`.
	 *
	 * `options.scan` is the scan to open on (the server's most recent one when absent) and
	 * `options.picker` adds the scan chooser and the New-scan action, which the page wants
	 * and the form — already looking at one scan — does not.
	 *
	 * Everything that survives a fetch lives in its own row: the filter dropdowns, so one is
	 * not destroyed mid-click, and the selected day, so a poll while the sweep is running
	 * does not move what the reader is looking at.
	 */
	autoshift.horizon_calendar.render = function ($wrapper, options) {
		options = options || {};
		autoshift.horizon_calendar.inject_styles();

		if (!$wrapper.hasClass("autoshift-horizon")) {
			$wrapper.addClass("autoshift-horizon");
			$wrapper.html(`
			<div class="ahz-row ahz-toolbar"></div>
			<div class="ahz-row ahz-filters"></div>
			<div class="ahz-row ahz-legend"></div>
			<div class="ahz-months"></div>
			<div class="ahz-detail"></div>
			<div class="ahz-chart"></div>
		`);
			$wrapper.data("ahz-options", options);
			bind_events($wrapper);
		}
		if (options.scan) ahz_state($wrapper, { scan: options.scan });

		return fetch_scan($wrapper);
	};

	function fetch_scan($wrapper) {
		const state = ahz_state($wrapper);
		clear_poll($wrapper);
		return frappe
			.call({
				method: "autoshift.scanner.api.get_scan",
				args: {
					scan: state.scan,
					scaled: state.scaled ? 1 : 0,
					disciplines: JSON.stringify(state.disciplines || []),
					branches: JSON.stringify(state.branches || []),
				},
			})
			.then((response) => {
				const payload = (response || {}).message;
				if (!payload) return;
				$wrapper.data("ahz-payload", payload);
				if (payload.scan) ahz_state($wrapper, { scan: payload.scan.name });
				draw($wrapper, payload);
				// A sweep reports a window at a time, so the calendar fills in while it runs.
				if (payload.scan && payload.scan.status === "Scanning") schedule_poll($wrapper);
				return payload;
			});
	}

	function clear_poll($wrapper) {
		const timer = $wrapper.data("ahz-timer");
		if (timer) clearTimeout(timer);
		$wrapper.data("ahz-timer", null);
	}

	function schedule_poll($wrapper) {
		clear_poll($wrapper);
		$wrapper.data(
			"ahz-timer",
			setTimeout(() => {
				// The page may have been left; a detached wrapper must not keep polling.
				if (!$wrapper.closest("body").length) return;
				fetch_scan($wrapper);
			}, 5000)
		);
	}

	function draw($wrapper, payload) {
		draw_toolbar($wrapper, payload);
		build_filters($wrapper, payload);
		draw_legend($wrapper, payload);
		draw_months($wrapper, payload);
		const state = ahz_state($wrapper);
		const square = state.date ? payload.days[state.date] : null;
		if (square) {
			// Only when something about the day actually moved. A poll every five seconds
			// that rebuilt the panel would also rebuild the wall chart under it, throwing
			// away whatever week the reader had navigated to in it.
			const stamp = [state.date, square.severity, square.run, state.scaled].join("|");
			if ($wrapper.data("ahz-drawn") !== stamp) {
				$wrapper.data("ahz-drawn", stamp);
				draw_detail($wrapper, state.date);
			}
		} else if (state.date) {
			// The selection fell outside the horizon (a re-plan, another scan): drop it
			// rather than draw a day this scan says nothing about.
			ahz_state($wrapper, { date: null });
			$wrapper.data("ahz-drawn", null);
			$wrapper.find(".ahz-detail, .ahz-chart").empty();
		}
	}

	function draw_toolbar($wrapper, payload) {
		const options = $wrapper.data("ahz-options") || {};
		const scan = payload.scan;
		const state = ahz_state($wrapper);
		const $host = $wrapper.find(".ahz-toolbar");

		if (!scan) {
			$host.html(
				`<span class="ahz-note">${__("No scan yet.")}</span>` +
					(options.picker
						? `<button class="btn btn-primary btn-xs ahz-new">${__(
								"New scan…"
						  )}</button>`
						: "")
			);
			$wrapper.find(".ahz-legend, .ahz-months, .ahz-detail, .ahz-chart").empty();
			return;
		}

		const done = scan.weeks_done || 0;
		const total = scan.weeks_total || 0;
		const percent = total ? Math.round((100 * done) / total) : 0;
		const running = scan.status === "Scanning";
		const indicator = { Draft: "gray", Scanning: "blue", Paused: "orange", Complete: "green" }[
			scan.status
		];

		$host.html(`
		${
			options.picker
				? `<select class="form-control input-xs ahz-scan-picker" style="width: auto">
					${payload.scans
						.map(
							(row) =>
								`<option value="${frappe.utils.escape_html(row.name)}" ${
									row.name === scan.name ? "selected" : ""
								}>${frappe.utils.escape_html(row.name)} · ${row.weeks_done || 0}/${
									row.weeks_total || 0
								}</option>`
						)
						.join("")}
				</select>
				<button class="btn btn-default btn-xs ahz-new">${__("New…")}</button>`
				: ""
		}
		<span class="indicator-pill ${indicator}">${__(scan.status)}</span>
		<span class="ahz-note">${__("{0} of {1} windows", [done, total])}</span>
		<div class="ahz-progress"><div style="width: ${percent}%"></div></div>
		<span class="ahz-note">${frappe.utils.escape_html(scan.ruleset)} · ${
			scan.mode
		} · ${frappe.datetime.str_to_user(scan.start_date)} – ${frappe.datetime.str_to_user(
			scan.end_date
		)}</span>
		<span style="flex: 1 1 auto"></span>
		${
			running
				? // Resume stands beside Stop while a sweep is running, for the sweep that is
				  // not: a killed worker leaves a scan saying "Scanning" with nobody working
				  // on it. The job is deduplicated on its id, so pressing this while one is
				  // genuinely alive queues nothing.
				  `<button class="btn btn-default btn-xs ahz-stop">${__("Stop")}</button>
					<button class="btn btn-default btn-xs ahz-start" title="${__(
						"Queue the sweep again — for a scan left mid-sweep by a restarted worker"
					)}">${__("Resume")}</button>`
				: `<button class="btn btn-primary btn-xs ahz-start">${
						done && done < total ? __("Resume") : __("Scan")
				  }</button>
					<button class="btn btn-default btn-xs ahz-rerun">${__("Re-run all")}</button>`
		}
		<label class="ahz-note" style="margin: 0 0 0 0.5rem">
			<input type="checkbox" class="ahz-scaled" ${state.scaled ? "checked" : ""}>
			${__("Only flag missing flexible staff")}
		</label>
	`);
		$host
			.find(".ahz-scaled")
			.attr(
				"title",
				__(
					"Measure a shortfall against what the exclusive gating roles actually scheduled could open, rather than against the configured room count — so a room no practitioner was in for is not reported as a scheduling hole."
				)
			);
	}

	function draw_legend($wrapper, payload) {
		const labels = ahz_labels();
		const totals = payload.totals || {};
		const keys = AHZ_SEVERITIES.concat(["unscanned", "closed"]);
		$wrapper.find(".ahz-legend").html(
			keys
				.filter((key) => totals[key])
				.map(
					(key) =>
						`<span class="ahz-chip"><span class="ahz-swatch ahz-sev-${key}"></span>${labels[key]} <b>${totals[key]}</b></span>`
				)
				.join("")
		);
	}

	function draw_months($wrapper, payload) {
		const weekdays = [
			__("Mon"),
			__("Tue"),
			__("Wed"),
			__("Thu"),
			__("Fri"),
			__("Sat"),
			__("Sun"),
		];
		const state = ahz_state($wrapper);
		const html = (payload.months || [])
			.map(
				(month) => `
			<div class="ahz-month">
				<div class="ahz-month-label">${frappe.utils.escape_html(month.label)}</div>
				<table class="ahz-grid">
					<thead><tr>${weekdays.map((day) => `<th>${day}</th>`).join("")}</tr></thead>
					<tbody>
						${month.weeks
							.map(
								(week) =>
									`<tr>${week
										.map(
											(date) => `<td>${day_cell(date, payload, state)}</td>`
										)
										.join("")}</tr>`
							)
							.join("")}
					</tbody>
				</table>
			</div>`
			)
			.join("");
		$wrapper
			.find(".ahz-months")
			.html(html || `<div class="ahz-note">${__("This scan covers no days.")}</div>`);
	}

	function day_cell(date, payload, state) {
		if (!date) return "";
		const number = Number(date.slice(8, 10));
		const square = payload.days[date];
		// Three distinct things, and only the first is a verdict: a day a window solved, a
		// day no window has reached yet, and a day the practice does not work.
		let severity = "unscanned";
		let mark = "";
		let title = `${frappe.datetime.str_to_user(date)} — ${__("not scanned yet")}`;
		if (square && !square.worked) {
			severity = "closed";
			title = `${frappe.datetime.str_to_user(date)} — ${__("not a working day")}`;
		} else if (square) {
			severity = square.severity;
			title = day_title(date, square);
			if (square.severity === "failed") mark = "!";
			else if (square.shortfall) mark = `−${square.shortfall}`;
			else if (square.conflicts) mark = "⚑";
			else if (square.under_fte) mark = "·";
		}
		const selected = state.date === date ? " ahz-selected" : "";
		return `<span class="ahz-day ahz-sev-${severity}${selected}" data-date="${date}"
			data-scanned="${square ? 1 : 0}" title="${frappe.utils.escape_html(title)}">
			<span class="ahz-day-num">${number}</span><span class="ahz-day-mark">${mark}</span>
		</span>`;
	}

	function day_title(date, square) {
		const parts = [frappe.datetime.str_to_user(date)];
		if (square.severity === "failed") {
			parts.push(square.message || __("the window has no feasible schedule"));
			return parts.join(" — ");
		}
		parts.push(__("{0} of {1} rooms staffed", [square.staffed, square.capacity]));
		if (square.capacity !== square.configured) {
			parts.push(__("{0} configured", [square.configured]));
		}
		if (square.shut_cells) parts.push(__("{0} slot(s) shut", [square.shut_cells]));
		if (square.conflicts) parts.push(__("{0} conflict(s)", [square.conflicts]));
		if (square.under_fte) parts.push(__("{0} under FTE", [square.under_fte]));
		return parts.join(" · ");
	}

	// ── the day behind a click ──────────────────────────────────────────────────

	function draw_detail($wrapper, date) {
		const state = ahz_state($wrapper, { date: date });
		$wrapper.find(".ahz-day").removeClass("ahz-selected");
		$wrapper.find(`.ahz-day[data-date="${date}"]`).addClass("ahz-selected");

		const $detail = $wrapper.find(".ahz-detail");
		$detail.html(`<div class="ahz-note">${__("Loading day…")}</div>`);
		return frappe
			.call({
				method: "autoshift.scanner.api.get_day",
				args: {
					scan: state.scan,
					date: date,
					scaled: state.scaled ? 1 : 0,
					disciplines: JSON.stringify(state.disciplines || []),
					branches: JSON.stringify(state.branches || []),
				},
			})
			.then((response) => {
				const detail = (response || {}).message;
				if (!detail || !detail.window) {
					$detail.html(
						`<div class="ahz-note">${__("No window covers this day.")}</div>`
					);
					return;
				}
				$detail.html(detail_html(date, detail));
				draw_chart($wrapper, date, detail.window);
			});
	}

	function detail_html(date, detail) {
		const day = detail.day || {};
		// `win`, not `window`: shadowing the global one inside a function that also builds
		// markup is a trap for the next reader, not a saved character.
		const win = detail.window || {};
		const labels = ahz_labels();
		const cells = (day.cells || []).filter((cell) => cell.capacity || cell.staffed);
		return `
		<div class="ahz-detail-card">
			<div class="ahz-detail-head">
				<h5>${frappe.datetime.str_to_user(date)}</h5>
				<span class="indicator-pill ${
					{
						ok: "green",
						info: "blue",
						warning: "orange",
						critical: "red",
						failed: "red",
					}[day.severity] || "gray"
				}">${labels[day.severity] || day.severity}</span>
				<span class="ahz-note">${__("Window")} ${frappe.datetime.str_to_user(
			win.window_start
		)} – ${frappe.datetime.str_to_user(win.window_end)} · ${__(win.status)}${
			win.reused ? ` · ${__("reused an identical run")}` : ""
		}</span>
				<span style="flex: 1 1 auto"></span>
				${
					win.run
						? `<a class="btn btn-default btn-xs" href="/app/optimizer-run/${encodeURIComponent(
								win.run
						  )}" target="_blank">${__("Open run")}</a>`
						: ""
				}
				<button class="btn btn-default btn-xs ahz-rerun-window" data-window="${win.name || ""}">${__(
			"Re-run window"
		)}</button>
			</div>
			${win.message ? `<div class="ahz-note">${frappe.utils.escape_html(win.message)}</div>` : ""}
			${
				cells.length
					? `<table class="ahz-table">
						<thead><tr>
							<th>${__("Discipline")}</th><th>${__("Branch")}</th><th>${__("Shift")}</th>
							<th class="ahz-num">${__("Staffed")}</th>
							<th class="ahz-num">${__("Could open")}</th>
							<th class="ahz-num">${__("Configured")}</th>
						</tr></thead>
						<tbody>${cells
							.map(
								(cell) => `<tr>
								<td>${frappe.utils.escape_html(cell.discipline)}</td>
								<td>${frappe.utils.escape_html(cell.branch)}</td>
								<td>${frappe.utils.escape_html(cell.shift_type)}</td>
								<td class="ahz-num">${cell.staffed}</td>
								<td class="ahz-num">${cell.effective}</td>
								<td class="ahz-num">${cell.capacity}</td>
							</tr>`
							)
							.join("")}</tbody>
					</table>`
					: `<div class="ahz-note">${__("No room-coverage figures for this day.")}</div>`
			}
			${rows_note(
				__("Conflicts"),
				(day.conflict_rows || []).map((row) => `${row.employee}: ${row.detail}`)
			)}
			${rows_note(
				__("Under FTE"),
				(day.under_fte_rows || []).map((row) =>
					__("{0} — {1} half-day(s) short over the window", [row.employee, row.deficit])
				)
			)}
		</div>`;
	}

	function rows_note(title, lines) {
		if (!lines.length) return "";
		const shown = lines.slice(0, 8);
		return `<div class="ahz-note" style="margin-top: 0.5rem"><b>${title}</b>: ${shown
			.map((line) => frappe.utils.escape_html(line))
			.join("; ")}${
			lines.length > shown.length
				? ` … ${__("and {0} more", [lines.length - shown.length])}`
				: ""
		}</div>`;
	}

	function monday_of(date) {
		const parsed = frappe.datetime.str_to_obj(date);
		const offset = (parsed.getDay() + 6) % 7;
		return frappe.datetime.add_days(date, -offset);
	}

	function draw_chart($wrapper, date, win) {
		const $chart = $wrapper.find(".ahz-chart");
		if (!win.run) {
			$chart.html(
				`<div class="ahz-note">${__(
					"This window has no run to draw — scan it to see its week."
				)}</div>`
			);
			return;
		}
		// Free, because the scan kept the run: the wall chart already reads one, and the week
		// it draws is the week of the day that was clicked, not the window's first.
		frappe.require("/assets/autoshift/js/wall_chart.js", () => {
			autoshift.wall_chart.render(
				$chart,
				(week, filters) =>
					frappe
						.call({
							method: "autoshift.wallchart.api.get_week_chart",
							args: {
								week: week,
								run: win.run,
								disciplines: JSON.stringify((filters || {}).disciplines || []),
								branches: JSON.stringify((filters || {}).branches || []),
								weekdays: JSON.stringify((filters || {}).weekdays || []),
								...((filters || {}).highlight_roles !== undefined
									? { highlight_roles: JSON.stringify(filters.highlight_roles) }
									: {}),
							},
						})
						.then((response) => (response || {}).message),
				monday_of(date)
			);
		});
	}

	// ── controls ────────────────────────────────────────────────────────────────

	function bind_events($wrapper) {
		$wrapper.on("click", ".ahz-day[data-scanned='1']", (event) => {
			const date = $(event.currentTarget).data("date");
			const payload = $wrapper.data("ahz-payload") || { days: {} };
			const square = payload.days[date] || {};
			$wrapper.data(
				"ahz-drawn",
				[date, square.severity, square.run, ahz_state($wrapper).scaled].join("|")
			);
			draw_detail($wrapper, date);
		});
		$wrapper.on("change", ".ahz-scaled", (event) => {
			ahz_state($wrapper, { scaled: $(event.currentTarget).is(":checked") ? 1 : 0 });
			fetch_scan($wrapper).then(() => {
				const state = ahz_state($wrapper);
				if (state.date) draw_detail($wrapper, state.date);
			});
		});
		$wrapper.on("change", ".ahz-scan-picker", (event) => {
			ahz_state($wrapper, { scan: $(event.currentTarget).val(), date: null });
			$wrapper.find(".ahz-detail, .ahz-chart").empty();
			fetch_scan($wrapper);
		});
		$wrapper.on("click", ".ahz-new", () => new_scan_dialog($wrapper));
		$wrapper.on("click", ".ahz-start", () => act($wrapper, "start_scan", {}));
		$wrapper.on("click", ".ahz-stop", () => act($wrapper, "stop_scan", {}));
		$wrapper.on("click", ".ahz-rerun", () => {
			frappe.confirm(
				__(
					"Re-run every window of this scan? Windows whose input has not changed re-link the run they already have, so only what actually changed is solved again."
				),
				() => act($wrapper, "start_scan", { resume: 0 })
			);
		});
		$wrapper.on("click", ".ahz-rerun-window", (event) => {
			const row = $(event.currentTarget).data("window");
			act($wrapper, "rerun_windows", { windows: JSON.stringify(row ? [row] : []) });
		});
	}

	function act($wrapper, method, args) {
		const state = ahz_state($wrapper);
		if (!state.scan) return;
		return frappe
			.call({
				method: `autoshift.scanner.api.${method}`,
				args: Object.assign({ scan: state.scan }, args),
				freeze: true,
			})
			.then(() => fetch_scan($wrapper))
			.catch(() => fetch_scan($wrapper));
	}

	function new_scan_dialog($wrapper) {
		const dialog = new frappe.ui.Dialog({
			title: __("New Horizon Scan"),
			fields: [
				{
					fieldname: "ruleset",
					fieldtype: "Link",
					options: "Optimization Ruleset",
					label: __("Optimization Ruleset"),
					default: "Standard Ruleset",
					reqd: 1,
				},
				{
					fieldname: "start_date",
					fieldtype: "Date",
					label: __("Start Date"),
					default: frappe.datetime.get_today(),
					description: __("Snapped back to its Monday — a window is a whole week."),
					reqd: 1,
				},
				{
					fieldname: "end_date",
					fieldtype: "Date",
					label: __("Scan Until"),
					default: frappe.datetime.add_months(frappe.datetime.get_today(), 3),
					reqd: 1,
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "mode",
					fieldtype: "Select",
					label: __("Window Length"),
					options: ["1-week", "2-week", "4-week"],
					default: "1-week",
				},
				{
					fieldname: "window_time_limit",
					fieldtype: "Int",
					label: __("Window Time Limit (s)"),
					default: 120,
				},
				{
					fieldname: "reuse_cached",
					fieldtype: "Check",
					label: __("Reuse identical runs"),
					default: 1,
				},
				{
					fieldname: "start_now",
					fieldtype: "Check",
					label: __("Start scanning now"),
					default: 1,
				},
			],
			primary_action_label: __("Create"),
			primary_action: (values) => {
				dialog.hide();
				frappe
					.call({
						method: "autoshift.scanner.api.create_scan",
						args: values,
						freeze: true,
					})
					.then((response) => {
						const name = (response || {}).message;
						if (!name) return;
						ahz_state($wrapper, { scan: name, date: null });
						$wrapper.find(".ahz-detail, .ahz-chart").empty();
						if (values.start_now) return act($wrapper, "start_scan", {});
						return fetch_scan($wrapper);
					});
			},
		});
		dialog.show();
	}

	// The dropdowns outlive a fetch, so they are built once and only their selection is
	// read back — the wall chart's bargain, for the wall chart's reason (see build_filters
	// there): a picker destroyed mid-click is a picker nobody can use.
	function build_filters($wrapper, payload) {
		const $host = $wrapper.find(".ahz-filters");
		const filters = payload.filters || {};
		if (!filters.disciplines) return;
		if (!filters.disciplines.options.length && !filters.branches.options.length) return;
		if ($host.data("ahz-built")) return;
		$host.data("ahz-built", true);
		$host.html(`
		<span class="ahz-label">${__("Show")}</span>
		<div class="ahz-filter ahz-filter-disciplines"></div>
		<div class="ahz-filter ahz-filter-branches"></div>
		<button type="button" class="btn btn-default btn-xs ahz-filter-clear">${__("All bands")}</button>
	`);

		const controls = {};
		const keys = ["disciplines", "branches"];
		const read = () =>
			keys.reduce(
				(out, key) => Object.assign(out, { [key]: controls[key].get_value() || [] }),
				{}
			);
		const settle = () => {
			const next = read();
			const state = ahz_state($wrapper);
			if (keys.every((key) => ahz_same_set(next[key] || [], state[key] || []))) return;
			ahz_state($wrapper, next);
			fetch_scan($wrapper).then(() => {
				const current = ahz_state($wrapper);
				if (current.date) draw_detail($wrapper, current.date);
			});
		};

		keys.forEach((key) => {
			controls[key] = make_picker(
				$host.find(`.ahz-filter-${key}`),
				key,
				key === "disciplines" ? __("All disciplines") : __("All branches"),
				filters[key].options || [],
				filters[key].selected || [],
				settle
			);
		});
		$host.find(".ahz-filter-clear").on("click", () => {
			if (!read().disciplines.length && !read().branches.length) return;
			keys.forEach((key) => controls[key].set_value([]));
			ahz_state($wrapper, { disciplines: [], branches: [] });
			fetch_scan($wrapper);
		});
	}

	function make_picker($parent, fieldname, placeholder, options, selected, on_close) {
		const normalized = (options || []).map((option) => ({
			value: option.value,
			label: option.label || option.value,
			// `MultiSelectList` interpolates this straight into the row subtitle and only
			// defaults it for *string* options, so a missing one prints "undefined".
			description: option.description || "",
		}));
		const control = frappe.ui.form.make_control({
			df: {
				fieldname: fieldname,
				label: placeholder,
				placeholder: placeholder,
				fieldtype: "MultiSelectList",
				input_class: "input-xs",
				get_data: (txt) =>
					normalized.filter(
						(option) =>
							!txt ||
							option.label.toLowerCase().includes(txt.toLowerCase()) ||
							option.value.toLowerCase().includes(txt.toLowerCase())
					),
			},
			parent: $parent,
			only_input: true,
		});
		control.refresh();
		if (!control.$input) control.make_input();
		// Seeded so the closed control can print a selected value's label before the
		// dropdown has ever been opened.
		control._options = control.process_options(normalized);
		if (selected.length) control.set_value(selected.slice());
		control.$wrapper.on("hidden.bs.dropdown", on_close);
		return control;
	}
})();
