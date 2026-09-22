// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see bulk_employee_settings.js for why. Page
// scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift");

frappe.pages["rota-editor"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Rota Editor"),
		single_column: true,
	});
	wrapper.rota_editor = new autoshift.RotaEditor(page);
};

// Loaded once per Desk session, so a link arriving later (a pre-solve warning's, say)
// has to be applied on every show, not only on load.
frappe.pages["rota-editor"].on_page_show = function (wrapper) {
	if (wrapper.rota_editor) wrapper.rota_editor.apply_route_options();
};

function inject_rota_editor_styles() {
	if (document.getElementById("rota-editor-styles")) return;
	const css = `
		/* The right-click role menu is positioned against this, so it must be the
		   offset parent — see open_role_menu. */
		.rota-editor { position: relative; }
		.rota-editor .re-hint { margin-bottom: 0.75rem; }
		.rota-editor .re-banner { margin-bottom: 0.75rem; }
		.rota-editor .re-grid-wrap { overflow-x: auto; }
		.rota-editor .re-table { border-collapse: collapse; font-size: var(--text-sm); }
		.rota-editor .re-table th, .rota-editor .re-table td {
			border: 1px solid var(--border-color); padding: 0.15rem 0.3rem; text-align: center;
		}
		/* Zebra striping, one row's shift type at a time now that they stack under one
		   employee instead of sitting side by side — plain alternation, not grouped by
		   employee, is what actually keeps adjacent rows readable at a glance. Hidden
		   (read-only) rows and the divider between the two sections keep their own look. */
		/* background-color (opaque) + background-image (the tint), not the background
		   shorthand: a shorthand here would reset background-color to this same
		   translucent rgba, and .re-emp-col/.re-shift-col are sticky -- a translucent
		   sticky column lets the content scrolling underneath show through it. */
		.rota-editor .re-table tbody tr:nth-child(even):not(.re-row-hidden):not(.re-divider) td {
			background-color: var(--fg-color);
			background-image: linear-gradient(var(--zebra-bg, rgba(128, 128, 128, 0.06)), var(--zebra-bg, rgba(128, 128, 128, 0.06)));
		}
		.rota-editor .re-table tbody tr:nth-child(even):not(.re-row-hidden):not(.re-divider) .re-emp-col,
		.rota-editor .re-table tbody tr:nth-child(even):not(.re-row-hidden):not(.re-divider) .re-shift-col {
			background-color: var(--fg-color);
			background-image: linear-gradient(var(--zebra-bg, rgba(128, 128, 128, 0.06)), var(--zebra-bg, rgba(128, 128, 128, 0.06)));
		}
		.rota-editor .re-emp-col {
			text-align: left; white-space: nowrap; position: sticky; left: 0; min-width: 6rem;
			background: var(--fg-color); z-index: 2; vertical-align: top;
		}
		.rota-editor .re-shift-col {
			text-align: left; white-space: nowrap; position: sticky; left: 6rem; min-width: 3rem;
			background: var(--fg-color); z-index: 1; color: var(--text-muted); font-weight: normal;
		}
		.rota-editor .re-day-col { font-weight: normal; color: var(--text-muted); white-space: nowrap; }
		.rota-editor .re-cell { min-width: 2.2rem; height: 1.8rem; }
		.rota-editor .re-chip {
			display: inline-block; padding: 0.05rem 0.4rem; border-radius: var(--border-radius);
			background: var(--chip-bg, #cce0ff); cursor: grab; font-weight: 500; color: var(--chip-fg, inherit);
			border: 1px solid var(--chip-border, rgba(0, 0, 0, 0.1));
		}
		.rota-editor .re-chip-pending { background: var(--yellow-100, #fff3cd); opacity: 0.85; color: inherit; }
		/* Silver standard: imported, nobody has confirmed it yet. Declared after .re-chip so
		   it wins over the branch colour's border without !important. */
		.rota-editor .re-chip-unconfirmed { border: 2px dotted var(--gray-500, #888); }
		.rota-editor .re-promote { margin-top: 0.2rem; }
		.rota-editor .re-chip-cadence {
			font-size: 0.6em; opacity: 0.75; margin-left: 1px; vertical-align: super;
		}
		.rota-editor .re-cell-empty { display: block; width: 100%; height: 100%; min-height: 1.2rem; cursor: pointer; }
		.rota-editor .re-cell-empty:hover { background: var(--control-bg); }
		.rota-editor .re-cell-occupied { opacity: 0.4; background: #777777 }
		.rota-editor .re-row-hidden td { opacity: 0.6; background: var(--disabled-bg, #f2f2f2); }
		.rota-editor .re-row-hidden .re-emp-col,
		.rota-editor .re-row-hidden .re-shift-col { background: var(--disabled-bg, #f2f2f2); }
		.rota-editor .re-fraction { font-size: 0.75em; color: var(--text-muted); }
		.rota-editor .re-divider td {
			font-style: italic; color: var(--text-muted); border: none !important;
			background: transparent !important; padding-top: 0.6rem; text-align: left;
		}
		.rota-editor .re-periodicity-notes { color: var(--orange-600, #b35900); }
		.rota-editor .re-trash {
			display: inline-block; margin: 0.5rem 0; padding: 0.4rem 0.8rem;
			border: 1px dashed var(--border-color); border-radius: var(--border-radius-md);
			color: var(--text-muted);
		}
		.rota-editor .re-transcript { margin-top: 1rem; border-top: 1px solid var(--border-color); padding-top: 0.75rem; }
		.rota-editor .re-transcript-title { font-weight: 500; margin-bottom: 0.3rem; }
		.rota-editor .re-today-col { background: var(--blue-50, #eff6ff); }
		.rota-editor .re-today-col.re-row-hidden { background: var(--blue-50, #eff6ff); }
		/* Another discipline's settled half-day: drawn so a clash is visible, never
		   editable — see rota/editor.py, is_native. */
		.rota-editor .re-chip-foreign {
			background: transparent; border: 1px dashed var(--gray-400, #b0b0b0);
			color: var(--text-muted); cursor: not-allowed; font-style: italic;
			font-weight: normal; opacity: 0.55;
		}
		/* The same half-day booked twice. Declared after .re-chip-foreign so it wins. */
		.rota-editor .re-chip-clash {
			border: 1px solid var(--red-500, #e24c4c); color: var(--red-600, #c0392b); opacity: 0.9;
		}
		.rota-editor .re-cell-clash { box-shadow: inset 0 0 0 2px var(--red-300, #f0a9a2); }
		.rota-editor .re-chip-role {
			font-size: 0.6em; opacity: 0.8; margin-left: 2px; vertical-align: sub;
		}
		.rota-editor .re-row-foreign td { background: var(--disabled-bg, #f2f2f2); opacity: 0.85; }
		.rota-editor .re-row-foreign .re-emp-col,
		.rota-editor .re-row-foreign .re-shift-col { background: var(--disabled-bg, #f2f2f2); }
		.rota-editor .re-menu {
			position: absolute; z-index: 1000; background: var(--fg-color); min-width: 11rem;
			border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
			box-shadow: var(--shadow-md, 0 2px 8px rgba(0, 0, 0, 0.15)); padding: 0.25rem 0;
		}
		.rota-editor .re-menu-title {
			padding: 0.25rem 0.75rem; font-size: 0.85em; color: var(--text-muted);
		}
		.rota-editor .re-menu-item { padding: 0.25rem 0.75rem; cursor: pointer; white-space: nowrap; }
		.rota-editor .re-menu-item:hover { background: var(--control-bg); }
		.rota-editor .re-menu-item.re-menu-current { font-weight: 600; cursor: default; }
		.rota-editor .re-menu-item.re-menu-current:hover { background: transparent; }
	`;
	const style = document.createElement("style");
	style.id = "rota-editor-styles";
	style.textContent = css;
	document.head.appendChild(style);
}

function branch_color(branch_name) {
	// Generate a stable, distinct pastel color for each branch name using hash
	if (!branch_name) return { bg: "#e8eef5", fg: "#333", border: "rgba(0, 0, 0, 0.1)" };

	let hash = 0;
	for (let i = 0; i < branch_name.length; i++) {
		hash = (hash << 5) - hash + branch_name.charCodeAt(i);
		hash = hash & hash; // Convert to 32-bit integer
	}

	const hue = Math.abs(hash) % 360;
	const saturation = 45; // Moderate saturation for pastels
	const lightness = 75; // High lightness for pastel effect

	// Darker text for better contrast on light backgrounds
	const fg = lightness > 60 ? "#333" : "#fff";
	const bg = `hsl(${hue}, ${saturation}%, ${lightness}%)`;
	const border = `hsl(${hue}, ${saturation - 10}%, ${lightness - 15}%)`;

	return { bg, fg, border };
}

autoshift.RotaEditor = class RotaEditor {
	constructor(page) {
		this.page = page;
		this.state = null;
		this.day_labels = {};
		this.day_phases = {};
		this.drag = null;

		inject_rota_editor_styles();
		this.setup_fields();
		this.setup_body();
		this.page.set_primary_action(__("Apply Changes"), () => this.apply());
		this.page.set_secondary_action(__("Discard Changes"), () => this.discard());
		this.load_disciplines();
	}

	setup_fields() {
		this.discipline_field = this.page.add_field({
			fieldname: "discipline",
			label: __("Discipline"),
			fieldtype: "Select",
			options: [],
			change: () => this.refresh(),
		});
		this.date_field = this.page.add_field({
			fieldname: "start",
			label: __("Week Of"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
			change: () => this.refresh(),
		});
		this.view_weeks_field = this.page.add_field({
			fieldname: "view_weeks",
			label: __("View"),
			fieldtype: "Select",
			options: "1 week\n2 weeks\n4 weeks",
			default: "1 week",
			change: () => this.refresh(),
		});
	}

	view_weeks() {
		return parseInt((this.view_weeks_field.get_value() || "1 week").split(" ")[0], 10) || 1;
	}

	setup_body() {
		this.$body = $(`
			<div class="rota-editor">
				<div class="re-hint text-muted">${__(
					"Drag a chip to move a shift within the same person's row — any day, shift type or branch in this discipline. Drop it outside the table (or on Remove) to delete it, or click an empty cell to add one."
				)} ${__(
			"A dotted grey border marks a pattern imported but not yet confirmed. Editing a pattern confirms it; Promote all confirms the rest of that person's patterns as they stand."
		)} ${__(
			"Right-click a chip to change the Scheduling Role it is worked in. Faint italic chips are another discipline's settled week — read-only here, and red where they land on the same half-day as one of this discipline's."
		)}</div>
				<div class="re-banner form-message yellow" hidden></div>
				<div class="re-grid-wrap"><div class="re-grid"></div></div>
				<div class="re-trash">🗑 ${__("Remove")}</div>
				<div class="re-transcript"></div>
			</div>
		`).appendTo(this.page.main);

		this.$body
			.on("dragstart", ".re-chip[draggable='true']", (e) => {
				const $chip = $(e.currentTarget);
				this.drag = {
					assignment: $chip.attr("data-assignment"),
					employee: $chip.attr("data-employee"),
					shiftType: $chip.attr("data-shift-type"),
					date: $chip.attr("data-date"),
					branch: $chip.attr("data-branch") || "",
				};
				e.originalEvent.dataTransfer.effectAllowed = "move";
			})
			.on("dragend", ".re-chip", () => {
				this.drag = null;
			})
			.on("dragover", "td.re-cell", (e) => {
				const $td = $(e.currentTarget);
				if (this.drag && $td.attr("data-employee") === this.drag.employee) {
					e.preventDefault();
				}
			})
			.on("drop", "td.re-cell", (e) => {
				e.preventDefault();
				if (!this.drag) return;
				const $td = $(e.currentTarget);
				if ($td.attr("data-employee") !== this.drag.employee) return;
				const to_shift_type = $td.attr("data-shift-type");
				const to_date = $td.attr("data-date");
				const drag = this.drag;
				this.drag = null;
				if (to_shift_type === drag.shiftType && to_date === drag.date) return;
				this.stage_move(drag, to_shift_type, to_date);
			})
			.on("contextmenu", ".re-chip[draggable='true']", (e) => {
				// Right-click is the only way to re-role a half-day: a drag already means
				// "move it", and the role is the other thing a chip records.
				e.preventDefault();
				this.open_role_menu($(e.currentTarget), e.pageX, e.pageY);
			})
			.on("click", ".re-menu-item[data-role]", (e) => {
				const $item = $(e.currentTarget);
				this.close_role_menu();
				this.stage_retag($item.data("chip"), $item.attr("data-role"));
			})
			.on("click", ".re-promote", (e) => {
				this.stage({ op: "promote", employee: $(e.currentTarget).attr("data-employee") });
			})
			.on("click", ".re-cell-empty", (e) => {
				const $el = $(e.currentTarget);
				this.stage_add(
					$el.attr("data-employee"),
					$el.attr("data-shift-type"),
					$el.attr("data-date")
				);
			});

		this.$body
			.find(".re-trash")
			.on("dragover", (e) => {
				if (this.drag) e.preventDefault();
			})
			.on("drop", (e) => {
				e.preventDefault();
				if (!this.drag) return;
				const drag = this.drag;
				this.drag = null;
				this.stage_remove(drag);
			});

		// A chip dropped anywhere that isn't a valid target — outside the table entirely, or
		// on it but rejected (see the td.re-cell handlers above) — is a delete. This listens
		// on the whole document, not just $body, since a drag can end past the page's own
		// content (the sidebar, navbar, and so on).
		$(document)
			.on("dragover", (e) => {
				if (this.drag) e.preventDefault();
			})
			.on("drop", (e) => {
				if (!this.drag) return;
				if ($(e.target).closest(".re-table, .re-trash").length) return;
				e.preventDefault();
				const drag = this.drag;
				this.drag = null;
				this.stage_remove(drag);
			})
			.on("mousedown.re-menu", (e) => {
				if (!$(e.target).closest(".re-menu").length) this.close_role_menu();
			})
			.on("keydown.re-menu", (e) => {
				if (e.key === "Escape") this.close_role_menu();
			});
	}

	// ── changing the role a half-day is worked in ────────────────────────────

	close_role_menu() {
		if (this.$menu) {
			this.$menu.remove();
			this.$menu = null;
		}
	}

	// The roles this person holds in *this* discipline (server-side `_role_candidates`),
	// so the menu can only ever offer something the edit would be allowed to stage.
	open_role_menu($chip, x, y) {
		this.close_role_menu();
		const employee = $chip.attr("data-employee");
		const emp = (this.state.employees || []).find((e) => e.employee === employee);
		const roles = (emp && emp.roles) || [];
		const current = $chip.attr("data-role") || "";
		if (roles.length < 2) {
			frappe.show_alert({
				message: roles.length
					? __("{0} holds only one Scheduling Role in this discipline.", [employee])
					: __("{0} holds no Scheduling Role in this discipline.", [employee]),
				indicator: "orange",
			});
			return;
		}
		const chip = {
			assignment: $chip.attr("data-assignment"),
			employee,
			date: $chip.attr("data-date"),
		};
		const labels = this.state.role_labels || {};
		const items = roles
			.map((role) => {
				const badge = labels[role]
					? ` <span class="text-muted">(${labels[role]})</span>`
					: "";
				return (
					`<div class="re-menu-item${role === current ? " re-menu-current" : ""}" ` +
					`${role === current ? "" : `data-role="${frappe.utils.escape_html(role)}"`}>` +
					`${role === current ? "✓ " : ""}${frappe.utils.escape_html(
						role
					)}${badge}</div>`
				);
			})
			.join("");
		this.$menu = $(
			`<div class="re-menu"><div class="re-menu-title">${__(
				"Worked as"
			)}</div>${items}</div>`
		).appendTo(this.$body);
		this.$menu.find(".re-menu-item[data-role]").data("chip", chip);
		const offset = this.$body.offset();
		this.$menu.css({ left: x - offset.left, top: y - offset.top });
	}

	stage_retag(chip, role) {
		if (!chip) return;
		this.stage({
			op: "retag",
			employee: chip.employee,
			from_assignment: chip.assignment,
			from_weekday: this.day_labels[chip.date],
			from_phase: this.day_phases[chip.date],
			scheduling_role: role,
		});
	}

	// `?discipline=…&start=…` (a plain link) or `frappe.route_options` (set_route). Returns
	// whether it changed anything; the options are consumed so a later visit starts clean.
	take_route_options() {
		const options = Object.assign(
			{},
			frappe.utils.get_query_params(),
			frappe.route_options || {}
		);
		frappe.route_options = null;
		if (window.location.search) {
			// Otherwise the stale query would override the planner's own choice on every
			// return to the page.
			window.history.replaceState(null, "", window.location.pathname);
		}
		let changed = false;
		if (options.start && options.start !== this.date_field.get_value()) {
			this.date_field.set_value(options.start);
			changed = true;
		}
		const known = (this.discipline_field.df.options || "").split("\n");
		if (options.discipline && known.includes(options.discipline)) {
			if (options.discipline !== this.discipline_field.get_value()) {
				this.discipline_field.set_value(options.discipline);
				changed = true;
			}
		}
		return changed;
	}

	apply_route_options() {
		if (!this.disciplines_loaded) return; // load_disciplines applies them itself
		if (this.take_route_options()) this.refresh();
	}

	load_disciplines() {
		frappe.call({ method: "autoshift.rota.editor.list_disciplines" }).then(({ message }) => {
			const disciplines = message || [];
			this.discipline_field.df.options = disciplines.join("\n");
			this.discipline_field.refresh();
			if (!disciplines.length) {
				this.$body
					.find(".re-grid")
					.html(
						`<div class="text-muted">${__(
							"No discipline has a binding Scheduling Role — nothing for the Rota Editor to show."
						)}</div>`
					);
				return;
			}
			this.discipline_field.set_value(disciplines[0]);
			this.take_route_options();
			this.disciplines_loaded = true;
			this.refresh();
		});
	}

	// ── loading state ────────────────────────────────────────────────────────

	refresh() {
		const discipline = this.discipline_field.get_value();
		const start = this.date_field.get_value();
		if (!discipline || !start) return;
		frappe
			.call({
				method: "autoshift.rota.editor.get_state",
				args: { discipline, start, view_weeks: this.view_weeks() },
			})
			.then(({ message }) => this.render(message));
	}

	stage(change) {
		frappe
			.call({
				method: "autoshift.rota.editor.stage_change",
				args: {
					discipline: this.discipline_field.get_value(),
					start: this.date_field.get_value(),
					view_weeks: this.view_weeks(),
					change,
				},
				freeze: true,
			})
			.then(({ message }) => this.render(message));
	}

	// ── staging one edit ─────────────────────────────────────────────────────

	prompt_branch(options, current, callback) {
		frappe.prompt(
			[
				{
					fieldname: "branch",
					fieldtype: "Select",
					label: __("Branch"),
					options: options.join("\n"),
					default: options.includes(current) ? current : options[0],
					reqd: 1,
				},
			],
			({ branch }) => callback(branch),
			__("Choose a branch"),
			__("Continue")
		);
	}

	stage_move(drag, to_shift_type, to_date) {
		const options = (this.state.branches && this.state.branches[to_shift_type]) || [];
		const commit = (to_branch) =>
			this.stage({
				op: "move",
				employee: drag.employee,
				from_assignment: drag.assignment,
				from_weekday: this.day_labels[drag.date],
				from_phase: this.day_phases[drag.date],
				to_shift_type,
				to_weekday: this.day_labels[to_date],
				to_phase: this.day_phases[to_date],
				to_branch: to_branch || null,
			});
		if (!options.length || options.includes(drag.branch)) {
			commit(options.includes(drag.branch) ? drag.branch : null);
		} else {
			this.prompt_branch(options, drag.branch, commit);
		}
	}

	stage_remove(drag) {
		this.stage({
			op: "remove",
			employee: drag.employee,
			from_assignment: drag.assignment,
			from_weekday: this.day_labels[drag.date],
			from_phase: this.day_phases[drag.date],
		});
	}

	// A fresh pattern needs a branch and a role. The role is only *asked* for when the
	// server could not settle it on its own (`editor._default_role`: one role held here,
	// else the single binding one) — the same bargain as everywhere else in this app,
	// resolve silently where the answer is forced and ask only where it genuinely is not.
	stage_add(employee, shift_type, date) {
		const options = (this.state.branches && this.state.branches[shift_type]) || [];
		const emp = (this.state.employees || []).find((e) => e.employee === employee) || {};
		const roles = emp.roles || [];
		const ask_role = !emp.default_role && roles.length > 1;
		const commit = (branch, role) =>
			this.stage({
				op: "add",
				employee,
				to_shift_type: shift_type,
				to_weekday: this.day_labels[date],
				to_phase: this.day_phases[date],
				to_branch: branch,
				scheduling_role:
					role || emp.default_role || (roles.length === 1 ? roles[0] : null),
			});
		if (!options.length) {
			frappe.msgprint(
				__("No branch is configured for {0} in this discipline.", [shift_type])
			);
			return;
		}
		if (options.length === 1 && !ask_role) {
			commit(options[0]);
			return;
		}
		this.prompt_new_shift(options, null, roles, ask_role, commit);
	}

	prompt_new_shift(branches, current_branch, roles, ask_role, callback) {
		const fields = [];
		if (branches.length > 1) {
			fields.push({
				fieldname: "branch",
				fieldtype: "Select",
				label: __("Branch"),
				options: branches.join("\n"),
				default: branches.includes(current_branch) ? current_branch : branches[0],
				reqd: 1,
			});
		}
		if (ask_role) {
			fields.push({
				fieldname: "scheduling_role",
				fieldtype: "Select",
				label: __("Worked as"),
				options: roles.join("\n"),
				default: roles[0],
				reqd: 1,
				description: __(
					"This person holds several Scheduling Roles here, and none of them settles it on its own."
				),
			});
		}
		frappe.prompt(
			fields,
			(values) => callback(values.branch || branches[0], values.scheduling_role),
			__("Add a shift"),
			__("Continue")
		);
	}

	// ── rendering ─────────────────────────────────────────────────────────────

	render(state) {
		this.state = state;
		this.day_labels = {};
		this.day_phases = {};
		(state.days || []).forEach((d, i) => {
			this.day_labels[d.date] = d.weekday;
			this.day_phases[d.date] = Math.floor(i / 7);
		});
		this.render_banner();
		this.render_grid();
		this.render_transcript();
	}

	// Shift Types can carry long names; the row label abbreviates past 5 characters,
	// with the full name always available on hover.
	shift_type_label(shift_type) {
		return shift_type.length > 5 ? shift_type.slice(0, 5) : shift_type;
	}

	// The full name of the role a cell is worked in, for a tooltip — or a note that
	// nothing records one, which is itself worth seeing on an imported pattern.
	role_title(cell) {
		if (!cell || !cell.role) return " — " + __("no Scheduling Role recorded");
		let title = " — " + __("as {0}", [cell.role]);
		if (cell.collateral_roles && cell.collateral_roles.length) {
			title += " + " + cell.collateral_roles.join(", ");
		}
		return title;
	}

	// A role badge only where it distinguishes anything: somebody who can only be working
	// their one role in this discipline gains nothing from being told so on every chip.
	role_badge(emp, cell) {
		if (!cell || !cell.role) return "";
		if (!emp || !emp.roles || emp.roles.length < 2) return "";
		const label = (this.state.role_labels || {})[cell.role] || cell.role.slice(0, 3);
		return `<sub class="re-chip-role">${frappe.utils.escape_html(label)}</sub>`;
	}

	// Another discipline's chip: faint, dashed, never draggable, red when it collides with
	// one of this discipline's own half-days. See rota/editor.py, `_foreign_cells`.
	foreign_chip(cell) {
		const discipline = cell.discipline || "?";
		const role = cell.role ? " — " + __("as {0}", [cell.role]) : "";
		const branch = cell.branch ? " — " + cell.branch : "";
		const clash = cell.clash
			? " — " + __("clashes with a shift in this discipline on the same half-day")
			: "";
		const title = __("{0} — read-only here", [discipline]) + role + branch + clash;
		return (
			`<span class="re-chip re-chip-foreign${cell.clash ? " re-chip-clash" : ""}" ` +
			`draggable="false" title="${frappe.utils.escape_html(title)}">` +
			`${frappe.utils.escape_html(discipline.slice(0, 3))}</span>`
		);
	}

	// This discipline's own chip, drawn without any drag affordance — either because the
	// row's cadence does not tile into this view (a `{occupied, cycle_weeks}` fraction, see
	// `editor._hidden_cells`) or because the section is a Shift Type this discipline's
	// config does not cover, which has no legal drop target to move it to.
	readonly_native_chip(emp, cell, section) {
		const role = frappe.utils.escape_html(this.role_title(cell));
		if (cell.occupied !== undefined && cell.occupied < cell.cycle_weeks) {
			const title = __("Occurs {0} of every {1} weeks", [cell.occupied, cell.cycle_weeks]);
			return `<span class="re-fraction" title="${title}${role}">${cell.occupied}/${cell.cycle_weeks}</span>`;
		}
		const branch = frappe.utils.escape_html(cell.branch || "");
		const color = branch_color(cell.branch || "");
		const why =
			section && section.extra ? " — " + __("not configured in this discipline") : "";
		const classes =
			(cell.unconfirmed ? " re-chip-unconfirmed" : "") +
			(cell.clash ? " re-chip-clash" : "");
		return (
			`<span class="re-chip${classes}" draggable="false" ` +
			`style="--chip-bg: ${color.bg}; --chip-fg: ${color.fg}; --chip-border: ${color.border};" ` +
			`title="${branch}${role}${frappe.utils.escape_html(why)}">` +
			`${(cell.branch || "?").slice(0, 3)}${this.role_badge(emp, cell)}</span>`
		);
	}

	// One `<tr>` per (employee, shift type) — an employee's AM and PM rows sit directly
	// on top of each other, both under one rowspanned employee-name cell. `readOnly`
	// employees (period-incompatible with this view — see `edit.rota_view_weeks`) get
	// no drag/drop affordances at all: their cells carry a `{occupied, cycle_weeks,
	// branch}` fraction summary instead of a concrete `{branch, assignment}` chip —
	// see `editor._hidden_cells`. A section marked `extra` is a Shift Type this
	// discipline's config does not cover — usually another discipline's — and is
	// read-only for everybody, since `branches_of` offers no drop target on one.
	render_employee_rows(emp, sections, days, occupied_dates, readOnly, today) {
		const cadence_note =
			readOnly && emp.cycle_weeks
				? ` <span class="text-muted">(${emp.cycle_weeks.join(", ")}-week)</span>`
				: "";
		// Promotion needs no particular view width, so read-only rows offer it too.
		const promote = emp.unconfirmed
			? `<br><button class="btn btn-xs btn-default re-promote" data-employee="${
					emp.employee
			  }" title="${__("Confirm this person's {0} unconfirmed pattern(s) as they stand", [
					emp.unconfirmed,
			  ])}">${__("Promote all")}</button>`
			: "";
		const foreign_cells = emp.foreign_cells || {};
		let rows = "";
		sections.forEach((section, index) => {
			const shift_type = frappe.utils.escape_html(section.name);
			const shift_label = frappe.utils.escape_html(this.shift_type_label(section.name));
			const rowClass = [
				readOnly ? "re-row-hidden" : "",
				section.extra ? "re-row-foreign" : "",
			]
				.filter(Boolean)
				.join(" ");
			rows += `<tr class="${rowClass}">`;
			if (index === 0) {
				rows += `<td class="re-emp-col" rowspan="${
					sections.length
				}" title="${frappe.utils.escape_html(
					emp.employee_name
				)}">${frappe.utils.escape_html(
					emp.employee_label
				)}<br>${cadence_note}${promote}</td>`;
			}
			rows += `<td class="re-shift-col" title="${shift_type}${
				section.extra ? " — " + __("not configured in this discipline; read-only") : ""
			}">${shift_label}${section.extra ? " *" : ""}</td>`;
			days.forEach((d) => {
				const key = `${section.name}|${d.date}`;
				const cell = emp.cells[key];
				const foreign = foreign_cells[key];
				const todayClass = d.date === today ? " re-today-col" : "";
				const clashClass =
					(cell && cell.clash) || (foreign && foreign.clash) ? " re-cell-clash" : "";
				if (readOnly || section.extra) {
					rows += `<td class="re-cell re-cell-readonly${todayClass}${clashClass}">`;
					if (cell) rows += this.readonly_native_chip(emp, cell, section);
					if (foreign) rows += this.foreign_chip(foreign);
					rows += "</td>";
					return;
				}
				const is_occupied_different_shift =
					occupied_dates[emp.employee].has(d.date) && !cell && !foreign;
				const cell_class = is_occupied_different_shift ? " re-cell-occupied" : "";
				rows += `<td class="re-cell${cell_class}${todayClass}${clashClass}" data-employee="${emp.employee}" data-shift-type="${shift_type}" data-date="${d.date}">`;
				if (cell) {
					// "pending" (unapplied yet — see rota/editor.py._effective_rotas) is
					// purely a visual cue now: the chip stays draggable, and further edits
					// re-stage against it just like any chip already on the books.
					const pending = String(cell.assignment).indexOf("NEW-") === 0;
					const branch = frappe.utils.escape_html(cell.branch || "");
					const color = branch_color(cell.branch || "");
					const style = `style="--chip-bg: ${color.bg}; --chip-fg: ${color.fg}; --chip-border: ${color.border};"`;
					const cadence =
						cell.cycle_weeks > 1
							? `<sup class="re-chip-cadence">${cell.cycle_weeks}w</sup>`
							: "";
					const cadence_title =
						cell.cycle_weeks > 1
							? " — " + __("every {0} weeks", [cell.cycle_weeks])
							: "";
					const pending_title = pending ? " — " + __("pending") : "";
					const silver_title = cell.unconfirmed ? " — " + __("unconfirmed") : "";
					const clash_title = cell.clash
						? " — " + __("clashes with another discipline on this half-day")
						: "";
					const chip_class =
						(pending ? " re-chip-pending" : "") +
						(cell.unconfirmed ? " re-chip-unconfirmed" : "") +
						(cell.clash ? " re-chip-clash" : "");
					const title = `${branch}${cadence_title}${this.role_title(
						cell
					)}${pending_title}${silver_title}${clash_title} — ${__(
						"right-click to change the role"
					)}`;
					rows +=
						`<span class="re-chip${chip_class}" ` +
						`draggable="true" ` +
						`data-assignment="${frappe.utils.escape_html(cell.assignment)}" ` +
						`data-employee="${emp.employee}" data-shift-type="${shift_type}" data-date="${d.date}" ` +
						`data-branch="${branch}" data-role="${frappe.utils.escape_html(
							cell.role || ""
						)}" ` +
						`${style} title="${frappe.utils.escape_html(title)}">` +
						`${(cell.branch || "?").slice(0, 3)}${cadence}${this.role_badge(
							emp,
							cell
						)}</span>`;
				} else if (!foreign) {
					rows +=
						`<span class="re-cell-empty" data-employee="${emp.employee}" ` +
						`data-shift-type="${shift_type}" data-date="${d.date}"></span>`;
				}
				if (foreign) rows += this.foreign_chip(foreign);
				rows += "</td>";
			});
			rows += "</tr>";
		});
		return rows;
	}

	// The discipline's own Shift Types, then any this discipline's config does not cover
	// that a shown employee is nonetheless booked on — appended rather than dropped, so a
	// half-day is never invisible just because the shift is unfamiliar here. There is no
	// legal drop target on one, so those sections are read-only for everybody.
	sections_of(state) {
		return (state.shift_types || [])
			.map((section) => Object.assign({}, section, { extra: false }))
			.concat(
				(state.extra_shift_types || []).map((section) =>
					Object.assign({}, section, { extra: true })
				)
			);
	}

	render_grid() {
		const state = this.state;
		const sections = this.sections_of(state);
		const days = state.days || [];
		const employees = state.employees || [];
		const hidden = state.hidden_employees || [];
		const $grid = this.$body.find(".re-grid");
		const today = frappe.datetime.get_today();

		if (!sections.length) {
			$grid.html(
				`<div class="text-muted">${__(
					"No Discipline Branch Config covers this discipline."
				)}</div>`
			);
			return;
		}
		if (!employees.length && !hidden.length) {
			$grid.html(
				`<div class="text-muted">${__(
					"No employee holds a binding role in this discipline."
				)}</div>`
			);
			return;
		}

		let head = `<tr><th class="re-emp-col">${__("Employee")}</th><th class="re-shift-col">${__(
			"Shift"
		)}</th>`;
		days.forEach((d) => {
			const todayClass = d.date === today ? " re-today-col" : "";
			head += `<th class="re-day-col${todayClass}">${d.weekday.slice(
				0,
				3
			)}<br>${d.date.slice(5)}</th>`;
		});
		head += "</tr>";

		// Dates that already carry a chip for this employee, whatever the shift type and
		// whichever discipline books it — a day they are in elsewhere is just as much a
		// reason to shade the other half-day as one of this discipline's own.
		const occupied_dates = {};
		employees.forEach((emp) => {
			occupied_dates[emp.employee] = new Set();
			const foreign = emp.foreign_cells || {};
			sections.forEach((section) => {
				days.forEach((d) => {
					const key = `${section.name}|${d.date}`;
					if (emp.cells[key] || foreign[key]) occupied_dates[emp.employee].add(d.date);
				});
			});
		});

		let rows = "";
		employees.forEach((emp) => {
			rows += this.render_employee_rows(emp, sections, days, occupied_dates, false, today);
		});
		if (hidden.length) {
			const colspan = 2 + days.length;
			rows +=
				`<tr class="re-divider"><td colspan="${colspan}">${__(
					"Longer cadence than this view — shown below as a read-only average over each pattern's own cycle"
				)}</td></tr>` +
				hidden
					.map((emp) => this.render_employee_rows(emp, sections, days, {}, true, today))
					.join("");
		}

		$grid.html(`<table class="re-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`);
	}

	render_banner() {
		const $banner = this.$body.find(".re-banner");
		const n = this.state.unconfirmed_employees || 0;
		$banner.prop("hidden", !n);
		if (!n) return;
		$banner.text(
			__(
				"{0} employee(s) in this discipline still have imported rotas nobody has confirmed. The optimizer binds them to those patterns as they stand: edit or Promote all to confirm.",
				[n]
			)
		);
	}

	render_transcript() {
		const $t = this.$body.find(".re-transcript");
		const changes = this.state.pending_changes || [];
		const notes = this.state.periodicity_notes || [];
		if (!changes.length && !notes.length) {
			$t.html(`<div class="text-muted">${__("No pending edits.")}</div>`);
			return;
		}
		let html = "";
		if (changes.length) {
			html +=
				`<div class="re-transcript-title">${__("Pending edits")}</div>` +
				`<ol>${changes
					.map((c) => `<li>${frappe.utils.escape_html(c.description)}</li>`)
					.join("")}</ol>`;
		}
		if (notes.length) {
			html +=
				`<div class="re-transcript-title">${__("Periodicity changes")}</div>` +
				`<ul class="re-periodicity-notes">${notes
					.map((n) => `<li>${frappe.utils.escape_html(n)}</li>`)
					.join("")}</ul>`;
		}
		$t.html(html);
	}

	// ── apply / discard ──────────────────────────────────────────────────────

	apply() {
		const changes = (this.state && this.state.pending_changes) || [];
		if (!changes.length) {
			frappe.show_alert({ message: __("Nothing to apply."), indicator: "orange" });
			return;
		}
		const list = changes
			.map((c) => `<li>${frappe.utils.escape_html(c.description)}</li>`)
			.join("");
		frappe.confirm(
			`<p>${__("Apply {0} staged edit(s)?", [changes.length])}</p><ul>${list}</ul>`,
			() => {
				frappe
					.call({
						method: "autoshift.rota.editor.apply_draft",
						args: {
							discipline: this.discipline_field.get_value(),
							start: this.date_field.get_value(),
							view_weeks: this.view_weeks(),
						},
						freeze: true,
						freeze_message: __("Applying…"),
					})
					.then(({ message }) => {
						frappe.show_alert({
							message: __("Created {0}, replaced {1}, confirmed {2}.", [
								message.created,
								message.deleted,
								message.confirmed,
							]),
							indicator: "green",
						});
						this.refresh();
					});
			}
		);
	}

	discard() {
		frappe
			.call({
				method: "autoshift.rota.editor.discard_draft",
				args: {
					discipline: this.discipline_field.get_value(),
					start: this.date_field.get_value(),
					view_weeks: this.view_weeks(),
				},
			})
			.then(({ message }) => this.render(message));
	}
};
