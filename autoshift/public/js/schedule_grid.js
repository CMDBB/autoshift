// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// Shared schedule-grid renderer: an (employee x day) roster grid coloring proposed
// assignments, existing Shift Assignments, and leaves. Used by the Optimizer Run form
// (a solved run's own result) and by Optimizer Studio (an unsaved preview solve) — both
// feed it the same `{days, employees, events}` shape `get_schedule_events`/`preview`
// return, so the rendering only lives once.
//
// NOTE: no `import`/`export` here, deliberately — see bulk_employee_settings.js for why
// doctype/page scripts on this app stay plain scripts. Loaded via frappe.require() and
// accessed through the frappe.provide("autoshift.schedule_grid") namespace below.

frappe.provide("autoshift.schedule_grid");

autoshift.schedule_grid.inject_styles = function () {
	if (document.getElementById("autoshift-schedule-grid-styles")) return;
	const css = `
		.autoshift-schedule-grid { margin-bottom: 1rem; }
		.autoshift-schedule-grid .asg-legend {
			display: flex; gap: 1.25rem; flex-wrap: wrap;
			margin-bottom: 0.6rem; font-size: var(--text-sm); color: var(--text-muted);
		}
		.autoshift-schedule-grid .asg-key { display: inline-flex; align-items: center; gap: 0.4rem; }
		.autoshift-schedule-grid .asg-swatch {
			width: 0.9rem; height: 0.9rem; border-radius: 3px; border: 1.5px solid;
		}
		.autoshift-schedule-grid .asg-fullscreen { margin-left: auto; }
		.autoshift-schedule-grid:fullscreen,
		.autoshift-schedule-grid:-webkit-full-screen {
			background: var(--fg-color); padding: 1rem; overflow: auto;
		}
		.autoshift-schedule-grid:fullscreen .asg-scroll,
		.autoshift-schedule-grid:-webkit-full-screen .asg-scroll {
			max-height: calc(100vh - 5rem);
		}
		.autoshift-schedule-grid .asg-scroll {
			overflow: auto; max-height: 45rem;
			border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
		}
		.autoshift-schedule-grid .asg-table { border-collapse: separate; border-spacing: 0; }
		.autoshift-schedule-grid th, .autoshift-schedule-grid td {
			min-width: 9rem; max-width: 9rem; padding: 0.4rem;
			border-bottom: 1px solid var(--border-color);
			border-right: 1px solid var(--border-color);
			vertical-align: top; font-size: var(--text-sm);
		}
		.autoshift-schedule-grid thead th {
			position: sticky; top: 0; z-index: 2;
			background: var(--fg-color); font-weight: 500; text-align: center;
		}
		.autoshift-schedule-grid .asg-emp {
			position: sticky; left: 0; z-index: 1; background: var(--fg-color);
			min-width: 13rem; max-width: 13rem; text-align: left;
		}
		.autoshift-schedule-grid thead .asg-emp { z-index: 3; }
		.autoshift-schedule-grid .asg-emp-name { font-weight: 500; }
		.autoshift-schedule-grid .asg-emp-sub { color: var(--text-muted); font-size: var(--text-xs); }
		.autoshift-schedule-grid .asg-chip {
			border: 1.5px solid; border-radius: var(--border-radius);
			padding: 0.35rem 0.45rem; margin-bottom: 0.35rem; color: var(--text-color);
		}
		.autoshift-schedule-grid .asg-chip:last-child { margin-bottom: 0; }
		.autoshift-schedule-grid .asg-existing {
			background: var(--gray-100, #f3f4f6); border-color: var(--gray-400, #9ca3af);
			border-style: dashed;
		}
		.autoshift-schedule-grid .asg-leave {
			background: #fdf2f8; border-color: #f9a8d4;
		}
		.autoshift-schedule-grid .asg-chip-title { font-weight: 500; }
		.autoshift-schedule-grid .asg-chip-line { color: var(--text-muted); font-size: var(--text-xs); }
		.autoshift-schedule-grid .asg-forced { color: var(--text-on-yellow, #b45309); }
	`;
	const style = document.createElement("style");
	style.id = "autoshift-schedule-grid-styles";
	style.textContent = css;
	document.head.appendChild(style);
};

function build_legend() {
	const keys = [
		[__("Proposed"), 'style="background:#eff6ff;border-color:#93c5fd;"'],
		[__("Existing assignment"), 'class="asg-existing"'],
		[__("Leave"), 'class="asg-leave"'],
	];
	const items = keys
		.map(([label, attr]) => {
			return `<span class="asg-key"><span class="asg-swatch" ${attr}></span>${frappe.utils.escape_html(
				label
			)}</span>`;
		})
		.join("");
	const fullscreen = `<button type="button" class="btn btn-default btn-xs asg-fullscreen">${__(
		"Fullscreen"
	)}</button>`;
	return `<div class="asg-legend">${items}${fullscreen}</div>`;
}

function build_chip(ev) {
	if (ev.kind === "leave") {
		const label = ev.speculative ? `${ev.leave_type} (${__("speculative")})` : ev.leave_type;
		return `<div class="asg-chip asg-leave">
			<div class="asg-chip-title">${frappe.utils.escape_html(label || __("Leave"))}</div>
		</div>`;
	}

	const time =
		ev.start_time && ev.end_time
			? `<div class="asg-chip-line">${frappe.utils.escape_html(
					ev.start_time
			  )} – ${frappe.utils.escape_html(ev.end_time)}</div>`
			: "";
	const branch = ev.branch
		? `<div class="asg-chip-line">${frappe.utils.escape_html(ev.branch)}</div>`
		: "";

	if (ev.kind === "existing") {
		return `<div class="asg-chip asg-existing">
			<div class="asg-chip-title">${frappe.utils.escape_html(ev.shift_type)}</div>
			${time}${branch}
		</div>`;
	}

	// kind === "assigned"
	const forced = ev.forced
		? ` <span class="asg-forced" title="${__("Forced assignment")}">★</span>`
		: "";
	// The role is the point of a multi-skill schedule: it says which discipline this
	// person is covering today, which the shift type and branch alone cannot.
	const role = ev.scheduling_role
		? `<div class="asg-chip-line">${frappe.utils.escape_html(ev.scheduling_role)}</div>`
		: "";
	return `<div class="asg-chip" style="background:${ev.bg};border-color:${ev.border};">
		<div class="asg-chip-title">${frappe.utils.escape_html(ev.shift_type)}${forced}</div>
		${role}${time}${branch}
	</div>`;
}

autoshift.schedule_grid.build_html = function ({ days, employees, events }) {
	const day_header = days
		.map((d) => {
			const dt = frappe.datetime.str_to_obj(d);
			const label = `${dt.toLocaleDateString(undefined, {
				weekday: "short",
			})} ${frappe.datetime.str_to_user(d).slice(0, 5)}`;
			return `<th class="asg-day">${frappe.utils.escape_html(label)}</th>`;
		})
		.join("");

	const rows = employees
		.map((emp) => {
			const cells = days
				.map((d) => {
					const shifts = (events[emp.name] || {})[d] || [];
					const chips = shifts.map(build_chip).join("");
					return `<td class="asg-cell">${chips}</td>`;
				})
				.join("");
			const name = frappe.utils.escape_html(emp.employee_name || emp.name);
			const sub = (emp.roles || []).map((r) => frappe.utils.escape_html(r)).join(", ");
			return `<tr>
				<td class="asg-emp">
					<div class="asg-emp-name">${name}</div>
					${sub ? `<div class="asg-emp-sub">${sub}</div>` : ""}
				</td>
				${cells}
			</tr>`;
		})
		.join("");

	return `${build_legend()}<div class="asg-scroll">
		<table class="asg-table">
			<thead><tr><th class="asg-emp">${__("Employee")}</th>${day_header}</tr></thead>
			<tbody>${rows}</tbody>
		</table>
	</div>`;
};

/**
 * Render a schedule grid into `$wrapper`, fetched lazily via `fetch_events` (a function
 * returning a promise of `{days, employees, events}` or a falsy/empty result).
 * `$wrapper` gets the `autoshift-schedule-grid` class and the fullscreen click handler
 * wired once; callers may call this repeatedly (e.g. on every refresh/preview) and it
 * will just re-fetch and re-render.
 */
autoshift.schedule_grid.render = function ($wrapper, fetch_events) {
	autoshift.schedule_grid.inject_styles();

	if (!$wrapper.hasClass("autoshift-schedule-grid")) {
		$wrapper.addClass("autoshift-schedule-grid");
		$wrapper.on("click", ".asg-fullscreen", () => {
			if (document.fullscreenElement) {
				document.exitFullscreen();
			} else {
				$wrapper[0].requestFullscreen?.();
			}
		});
	}

	$wrapper.html(
		`<div class="text-muted" style="padding: 0.5rem 0;">${__("Loading schedule…")}</div>`
	);

	return Promise.resolve(fetch_events()).then((message) => {
		if (!message || !message.employees || !message.employees.length) {
			$wrapper.html(
				`<div class="text-muted" style="padding: 0.5rem 0;">${__(
					"No assigned shifts in this solution."
				)}</div>`
			);
			return;
		}
		$wrapper.html(autoshift.schedule_grid.build_html(message));
	});
};
