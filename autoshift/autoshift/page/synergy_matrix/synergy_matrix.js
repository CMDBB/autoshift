// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see role_matrix.js / bulk_employee_settings.js for
// why. Page scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift");

frappe.pages["synergy-matrix"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Synergy Matrix"),
		single_column: true,
	});
	wrapper.synergy_matrix = new autoshift.SynergyMatrix(page);
};

function inject_synergy_matrix_styles() {
	if (document.getElementById("synergy-matrix-styles")) return;
	const css = `
		.synergy-matrix .smx-hint { margin-bottom: 0.75rem; }
		.synergy-matrix .smx-grid-wrap { overflow: auto; max-height: calc(100vh - 16rem); }
		.synergy-matrix .smx-table { border-collapse: separate; border-spacing: 0; font-size: var(--text-sm); }
		.synergy-matrix .smx-table th, .synergy-matrix .smx-table td {
			border-right: 1px solid var(--border-color); border-bottom: 1px solid var(--border-color);
			padding: 0.15rem 0.3rem; text-align: center; background: var(--fg-color);
		}
		.synergy-matrix .smx-table thead th {
			position: sticky; top: 0; z-index: 3; color: var(--text-muted); font-weight: normal;
			vertical-align: bottom; white-space: nowrap;
		}
		.synergy-matrix .smx-emp-col {
			position: sticky; left: 0; z-index: 2; text-align: left; white-space: nowrap; min-width: 11rem;
		}
		.synergy-matrix .smx-self-col {
			min-width: 3.4rem;
			border-right: 2px solid var(--border-color) !important;
		}
		.synergy-matrix thead .smx-emp-col { z-index: 4; }
		.synergy-matrix tr.smx-self-row td { font-weight: 500; }
		.synergy-matrix tr.smx-self-row .smx-emp-col { color: var(--text-muted); font-weight: normal; }
		.synergy-matrix .smx-initials { color: var(--text-muted); margin-left: 0.3rem; font-size: 0.85em; }
		.synergy-matrix tbody tr:not(.smx-self-row):nth-child(even) td {
			/* background (shorthand) would reset background-color to this rgba itself,
			   making sticky columns translucent and letting scrolled-under content show
			   through; layering the tint as a background-image instead keeps an opaque
			   background-color underneath. */
			background-color: var(--fg-color);
			background-image: linear-gradient(var(--zebra-bg, rgba(128, 128, 128, 0.04)), var(--zebra-bg, rgba(128, 128, 128, 0.04)));
		}
		.synergy-matrix td.smx-cell { position: relative; padding: 0; min-width: 2.6rem; }
		.synergy-matrix td.smx-disabled { background: var(--gray-50, #f9fafb) !important; }
		.synergy-matrix td.smx-corner { background: var(--gray-50, #f9fafb) !important; }
		.synergy-matrix .smx-input {
			width: 100%; min-width: 2.6rem; max-width: 3.2rem; height: 1.8rem; border: none; background: transparent;
			text-align: center; color: inherit; font-variant-numeric: tabular-nums;
		}
		.synergy-matrix .smx-input:focus { outline: 2px solid var(--primary, #2490ef); outline-offset: -2px; }
		.synergy-matrix .smx-input[readonly] { cursor: not-allowed; }
		.synergy-matrix td.smx-band-penalty { background: var(--red-100, #fee2e2) !important; }
		.synergy-matrix td.smx-band-bonus-1 { background: var(--yellow-100, #fef9c3) !important; }
		.synergy-matrix td.smx-band-bonus-2 { background: var(--green-100, #dcfce7) !important; }
		.synergy-matrix td.smx-pending { box-shadow: inset 0 0 0 2px var(--yellow-500, #eab308); }
		.synergy-matrix td.smx-pending .smx-input { font-weight: 600; }
		.synergy-matrix td.smx-selected { outline: 2px solid var(--blue-500, #2490ef); outline-offset: -3px; }
		.synergy-matrix tr.smx-row-hi > td, .synergy-matrix tr.smx-row-hi > th {
			border-top: 1px solid var(--primary, #2490ef) !important; border-bottom: 1px solid var(--primary, #2490ef) !important;
		}
		.synergy-matrix td.smx-col-hi, .synergy-matrix th.smx-col-hi {
			border-left: 1px solid var(--primary, #2490ef) !important; border-right: 1px solid var(--primary, #2490ef) !important;
		}
		.synergy-matrix .smx-bulk {
			border: none; background: transparent; color: var(--text-muted); cursor: pointer;
			font-size: 0.85em; padding: 0 0.2rem; visibility: hidden;
		}
		.synergy-matrix th.smx-role-head:hover .smx-bulk,
		.synergy-matrix td.smx-emp-col:hover .smx-bulk { visibility: visible; }
		.synergy-matrix .smx-legend { margin: 0.5rem 0; display: flex; gap: 0.4rem; flex-wrap: wrap; align-items: center; }
		.synergy-matrix .smx-legend span { padding: 0.05rem 0.45rem; border-radius: var(--border-radius); }
		.synergy-matrix .smx-transcript { margin-top: 1rem; border-top: 1px solid var(--border-color); padding-top: 0.75rem; }
		.synergy-matrix .smx-transcript-title { font-weight: 500; margin-bottom: 0.3rem; }
	`;
	const style = document.createElement("style");
	style.id = "synergy-matrix-styles";
	style.textContent = css;
	document.head.appendChild(style);
}

const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));

// Word initials, the wall chart's lane-header abbreviation ("Dental Hygienist" -> "DH");
// the full name goes in the title. Falls back to the name when it has no letters at all.
// Only used as a fallback — `e.initials` (short_label's answer, custom_initials-aware) is
// what actually gets shown.
function abbreviate(name) {
	const initials = String(name || "").match(/(?<!\p{L})[\p{L}]/gu);
	return initials ? initials.map((c) => c.toUpperCase()).join("") : String(name || "");
}

// 1 is no bonus; below is a penalty, above a bonus. Unlike suitability's bands (which only
// ever get worse than 1), a multiplier can move either way, so this has two bonus tiers and
// one penalty tier rather than a scale from "good" to "bad". Shared by pair and self cells.
function synergy_band(value) {
	if (value == null || value === 1) return "";
	if (value < 1) return "smx-band-penalty";
	if (value <= 1.5) return "smx-band-bonus-1";
	return "smx-band-bonus-2";
}

function format_multiplier(value) {
	return value == null ? "" : String(Math.round(value * 100) / 100);
}

function self_key(employee) {
	return `self:${employee}`;
}

autoshift.SynergyMatrix = class SynergyMatrix {
	constructor(page) {
		this.page = page;
		this.data = null;
		// key -> {kind: "pair", employee_a, employee_b, value, was, who}
		//      | {kind: "self", employee, scheduling_role, value, was, who}
		// "empA|empB" (empA < empB) for a pair, "self:emp" for a self cell; null value =
		// remove the pair / reset the self value to 1.
		this.staged = new Map();
		this.selected = new Set();
		this.anchor = null;
		this.anchor_key = null;

		inject_synergy_matrix_styles();
		this.setup_fields();
		this.setup_body();
		this.page.set_primary_action(__("Apply Changes"), () => this.apply());
		this.page.set_secondary_action(__("Discard Changes"), () => this.discard());
		this.page.add_inner_button(__("Set Selected Cells…"), () => this.bulk_edit_selection());
		this.page.add_inner_button(__("Reload"), () => this.refresh());
		$(window).on("beforeunload.synergy_matrix", () => (this.staged.size ? true : undefined));
		this.load_disciplines();
	}

	setup_fields() {
		this.discipline_field = this.page.add_field({
			fieldname: "disciplines",
			label: __("Disciplines"),
			fieldtype: "MultiSelectList",
			get_data: (txt) => this.filter_disciplines(txt),
			change: () => this.refresh(),
		});
		this.show_all_field = this.page.add_field({
			fieldname: "show_all",
			label: __("All Employees"),
			fieldtype: "Check",
			change: () => this.refresh(),
		});
		this.search_field = this.page.add_field({
			fieldname: "search",
			label: __("Filter Employees"),
			fieldtype: "Data",
		});
		this.search_field.$input.on("input", () => this.apply_search());
	}

	setup_body() {
		this.$body = $(`
			<div class="synergy-matrix">
				<div class="smx-hint text-muted">${__(
					"Rows and columns are two different groups — each discipline's gating roles split between them, since pairing only ever happens between different roles. A pair cell is the pair's Synergy Multiplier, applied when they are matched into the same room together: 1 is no bonus, above a bonus, below a penalty. The Self row/column is a different figure — each employee's own Value Multiplier — shown here for convenience. Clear a cell to remove it (a self cell resets to 1 instead). Ctrl/Cmd+click or Shift+click to select several cells, then Set Selected Cells to edit them at once. Nothing is saved until you click Apply Changes."
				)}</div>
				<div class="smx-legend text-muted">
					<span style="background: var(--red-100, #fee2e2)">${__("&lt; 1 penalty")}</span>
					<span>${__("1 no bonus")}</span>
					<span style="background: var(--yellow-100, #fef9c3)">${__("&le; 1.5 bonus")}</span>
					<span style="background: var(--green-100, #dcfce7)">${__("&gt; 1.5 bonus")}</span>
				</div>
				<div class="smx-grid-wrap"><div class="smx-grid"></div></div>
				<div class="smx-transcript"></div>
			</div>
		`).appendTo(this.page.main);

		this.$body
			.on("mousedown", ".smx-input", (e) => this.on_cell_mousedown(e))
			.on("change", ".smx-input", (e) => this.stage_input($(e.currentTarget)))
			.on("keydown", ".smx-input", (e) => this.on_key(e))
			.on("focus", ".smx-input", (e) => {
				e.currentTarget.select();
				this.highlight($(e.currentTarget).closest("td"));
			})
			.on("blur", ".smx-input", () => this.clear_highlight())
			.on("click", ".smx-bulk", (e) => {
				e.preventDefault();
				this.bulk_edit_row($(e.currentTarget).attr("data-employee"));
			});
	}

	load_disciplines() {
		frappe
			.call({ method: "autoshift.employee_role_synergy.list_disciplines" })
			.then(({ message }) => {
				this.all_disciplines = message || [];
				const wanted = (frappe.route_options || {}).discipline;
				frappe.route_options = null;
				const preset = this.all_disciplines.includes(wanted)
					? [wanted]
					: this.all_disciplines.slice(0, 1);
				this.discipline_field.set_value(preset);
				this.loaded = true;
				this.refresh();
			});
	}

	filter_disciplines(txt) {
		const needle = (txt || "").trim().toLowerCase();
		return (this.all_disciplines || []).filter(
			(d) => !needle || d.toLowerCase().includes(needle)
		);
	}

	refresh() {
		if (!this.loaded) return;
		frappe
			.call({
				method: "autoshift.employee_role_synergy.get_matrix",
				args: {
					disciplines: JSON.stringify(this.discipline_field.get_value() || []),
					show_all: this.show_all_field.get_value() ? 1 : 0,
				},
			})
			.then(({ message }) => this.render(message));
	}

	// ── keys ─────────────────────────────────────────────────────────────────

	pair_key(a, b) {
		return a < b ? `${a}|${b}` : `${b}|${a}`;
	}

	// Row and column employees combined — used wherever a lookup doesn't care which side an
	// employee is on (staging, "who" labels).
	all_employees() {
		return [...(this.data.row_employees || []), ...(this.data.col_employees || [])];
	}

	// ── rendering ────────────────────────────────────────────────────────────

	render(data) {
		this.data = data;
		this.selected.clear();
		this.anchor = null;
		this.anchor_key = null;
		this.render_grid();
		this.render_transcript();
	}

	render_grid() {
		const row_employees = this.data.row_employees || [];
		const col_employees = this.data.col_employees || [];
		const $grid = this.$body.find(".smx-grid");
		if (!row_employees.length || !col_employees.length) {
			$grid.html(
				`<div class="text-muted">${__(
					"Not enough here to pair up: a discipline needs a holder of each of at least two different gating roles."
				)}</div>`
			);
			return;
		}

		let head =
			`<tr><th class="smx-emp-col">${__("Employee")}</th>` +
			`<th class="smx-self-col" title="${esc(
				__(
					"Each employee's own Value Multiplier — a different field from synergy, shown here for convenience."
				)
			)}">${__("Self")}</th>`;
		head += col_employees.map((e) => this.col_header_html(e)).join("");
		head += "</tr>";

		const self_row =
			`<tr class="smx-self-row">` +
			`<td class="smx-emp-col">${__("Self")}</td>` +
			`<td class="smx-self-col smx-corner"></td>` +
			col_employees.map((e) => this.self_cell_html(e)).join("") +
			`</tr>`;

		const rows = row_employees
			.map((row_emp) => {
				const cells = col_employees
					.map((col_emp) => this.pair_cell_html(row_emp, col_emp))
					.join("");
				return (
					`<tr data-employee="${esc(row_emp.employee)}" data-search="${esc(
						`${row_emp.employee} ${row_emp.employee_name} ${row_emp.initials}`.toLowerCase()
					)}">` +
					`<td class="smx-emp-col" title="${esc(row_emp.employee)}">` +
					`<button type="button" class="smx-bulk" data-employee="${esc(
						row_emp.employee
					)}" tabindex="-1" title="${esc(
						__("Set every pair with {0}", [row_emp.employee_name || row_emp.employee])
					)}">&#9776;</button> ${esc(
						row_emp.employee_name || row_emp.employee
					)}<span class="smx-initials">${esc(row_emp.initials)}</span></td>` +
					this.self_cell_html(row_emp) +
					cells +
					"</tr>"
				);
			})
			.join("");

		$grid.html(
			`<table class="smx-table"><thead>${head}</thead><tbody>${self_row}${rows}</tbody></table>`
		);
		// Cell markup carries no value of its own — paint() is what fills each input in from
		// data/staged, the same way a selection or bulk-edit repaint does.
		$grid.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
		this.apply_search();
	}

	col_header_html(e) {
		// `e.initials` is already `short_label`'s answer — the site's own custom_initials
		// where set, name-derived otherwise — so this reuses it rather than re-deriving from
		// the name here and silently ignoring custom_initials when it exists.
		return (
			`<th class="smx-role-head" title="${esc(e.employee_name || e.employee)}">` +
			`<button type="button" class="smx-bulk" data-employee="${esc(
				e.employee
			)}" tabindex="-1" title="${esc(
				__("Set every pair with {0}", [e.employee_name || e.employee])
			)}">&#9776;</button> ${esc(
				e.initials || abbreviate(e.employee_name || e.employee)
			)}</th>`
		);
	}

	// Rows and columns are two different role-based groups (see get_matrix): a discipline's
	// gating roles split so that a role's holders land wholly on one side, which is what
	// makes same-role pairing — never a considered feature — structurally impossible rather
	// than merely discouraged. Every cell here is therefore an ordinary pair cell; the rare
	// edge case of one employee holding a role on both sides is left to the server's own
	// "two distinct employees" check rather than special-cased on the client.
	pair_cell_html(row_emp, col_emp) {
		const key = this.pair_key(row_emp.employee, col_emp.employee);
		const cell = this.data.cells[key];
		const readonly = !cell && !this.data.can_create ? " readonly" : "";
		return (
			`<td class="smx-cell" data-key="${esc(key)}">` +
			`<input class="smx-input" type="text" inputmode="decimal" autocomplete="off"${readonly} ` +
			`data-key="${esc(key)}" title="${esc(
				`${row_emp.employee_name || row_emp.employee} × ${
					col_emp.employee_name || col_emp.employee
				}`
			)}"></td>`
		);
	}

	self_cell_html(emp) {
		const key = self_key(emp.employee);
		const self = this.data.self && this.data.self[emp.employee];
		if (!self) {
			return `<td class="smx-cell smx-self-col smx-disabled" title="${esc(
				__("Holds no gating role this matrix can attribute a value to.")
			)}"></td>`;
		}
		const readonly = this.data.can_write_esr ? "" : " readonly";
		return (
			`<td class="smx-cell smx-self-col" data-key="${esc(key)}">` +
			`<input class="smx-input" type="text" inputmode="decimal" autocomplete="off"${readonly} ` +
			`data-key="${esc(key)}" title="${esc(
				__("{0}'s own Value Multiplier", [emp.employee_name || emp.employee])
			)}"></td>`
		);
	}

	current_value(key) {
		if (this.staged.has(key)) return this.staged.get(key).value;
		if (key.startsWith("self:")) {
			const self = this.data.self && this.data.self[key.slice(5)];
			return self ? self.value_multiplier : null;
		}
		const cell = this.data.cells[key];
		return cell ? cell.synergy_multiplier : null;
	}

	// Class and value of one cell from `data` + `staged`, without re-rendering the table —
	// so a keyboard walk through the matrix keeps its focus.
	paint($td) {
		const key = $td.attr("data-key");
		if (!key) return; // no-data cell, nothing to paint
		const value = this.current_value(key);
		const pending = this.staged.has(key);
		const is_self = key.startsWith("self:");

		$td.removeClass(
			"smx-band-penalty smx-band-bonus-1 smx-band-bonus-2 smx-pending smx-selected"
		);
		$td.addClass(synergy_band(value));
		if (pending) $td.addClass("smx-pending");
		if (this.selected.has(key)) $td.addClass("smx-selected");
		$td.find(".smx-input").val(format_multiplier(value));

		const lines = [];
		if (is_self) {
			const self = this.data.self && this.data.self[key.slice(5)];
			if (pending)
				lines.push(
					__("Was {0}", [format_multiplier(self ? self.value_multiplier : null)])
				);
		} else {
			const cell = this.data.cells[key];
			if (cell && pending)
				lines.push(__("Was {0}", [format_multiplier(cell.synergy_multiplier)]));
			else if (pending) lines.push(__("New"));
			if (cell && !cell.active) lines.push(__("Inactive"));
		}
		if (lines.length) $td.attr("title", lines.join("\n"));
	}

	apply_search() {
		const needle = (this.search_field.get_value() || "").trim().toLowerCase();
		this.$body.find(".smx-table tbody tr:not(.smx-self-row)").each((_, tr) => {
			const $tr = $(tr);
			$tr.prop("hidden", !!needle && !$tr.attr("data-search").includes(needle));
		});
	}

	// ── row/column highlight ────────────────────────────────────────────────

	highlight($td) {
		this.clear_highlight();
		$td.closest("tr").addClass("smx-row-hi");
		const col = $td.index();
		this.$body.find(".smx-table tr").each((_, tr) => {
			$(tr).children().eq(col).addClass("smx-col-hi");
		});
	}

	clear_highlight() {
		this.$body.find(".smx-row-hi").removeClass("smx-row-hi");
		this.$body.find(".smx-col-hi").removeClass("smx-col-hi");
	}

	// ── multi-cell selection (Ctrl/Cmd+click toggles, Shift+click ranges) ─────

	on_cell_mousedown(e) {
		if (e.button !== 0) return; // left click only
		const $td = $(e.currentTarget).closest("td");
		const key = $td.attr("data-key");
		if (!key) return;
		const row = $td.closest("tr").index();
		const col = $td.index();

		if (e.ctrlKey || e.metaKey) {
			e.preventDefault(); // stay on the currently focused input; just toggle membership
			// The typical gesture is a plain click to start, then Ctrl+click to add more —
			// so the cell that plain click focused (this.anchor_key) belongs in the
			// selection too, the first time Ctrl+click grows it from empty.
			if (!this.selected.size && this.anchor_key && this.anchor_key !== key) {
				this.selected.add(this.anchor_key);
			}
			if (this.selected.has(key)) this.selected.delete(key);
			else this.selected.add(key);
			this.anchor = { row, col };
			this.anchor_key = key;
			this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
			return;
		}
		if (e.shiftKey && this.anchor) {
			e.preventDefault();
			this.select_range(this.anchor, { row, col });
			return;
		}
		// Plain click: clear any selection and let the default focus/edit behavior proceed.
		this.anchor = { row, col };
		this.anchor_key = key;
		if (this.selected.size) {
			this.selected.clear();
			this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
		}
	}

	select_range(a, b) {
		const r0 = Math.min(a.row, b.row);
		const r1 = Math.max(a.row, b.row);
		const c0 = Math.min(a.col, b.col);
		const c1 = Math.max(a.col, b.col);
		this.selected.clear();
		const $rows = this.$body.find(".smx-table tbody tr");
		for (let r = r0; r <= r1; r++) {
			const $tr = $rows.eq(r);
			if (!$tr.length) continue;
			for (let c = c0; c <= c1; c++) {
				const key = $tr.children().eq(c).attr("data-key");
				if (key) this.selected.add(key);
			}
		}
		this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
	}

	bulk_edit_selection() {
		if (!this.selected.size) {
			frappe.show_alert({
				message: __("Ctrl/Cmd+click or Shift+click cells first to select more than one."),
				indicator: "orange",
			});
			return;
		}
		const count = this.selected.size;
		frappe.prompt(
			{
				fieldname: "value",
				fieldtype: "Data",
				label: __("Multiplier for {0} selected cell(s)", [count]),
				description: __("1 = no bonus. Leave blank to remove (self cells reset to 1)."),
			},
			(values) => {
				const raw = String(values.value || "")
					.trim()
					.replace(",", ".");
				const value = raw === "" ? null : Number(raw);
				if (value !== null && (!Number.isFinite(value) || value < 0)) {
					frappe.show_alert({
						message: __("Multiplier is a number 0 or above."),
						indicator: "red",
					});
					return;
				}
				[...this.selected].forEach((key) =>
					this.stage_value(key, value, { silent: true })
				);
				this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
				this.render_transcript();
			},
			__("Set selected cells"),
			__("Apply")
		);
	}

	// ── staging ──────────────────────────────────────────────────────────────

	// Shared by a single-cell edit, the row/column bulk edit and the multi-selection bulk
	// edit: validates, stages (or un-stages a no-op back to the original), then repaints.
	stage_value(key, value, { silent } = {}) {
		if (value !== null && (!Number.isFinite(value) || value < 0)) {
			if (!silent) {
				frappe.show_alert({
					message: __("Multiplier is a number 0 or above."),
					indicator: "red",
				});
			}
			return false;
		}

		if (key.startsWith("self:")) {
			const employee = key.slice(5);
			const self = this.data.self && this.data.self[employee];
			if (!self) return false; // no role to attribute a value to
			const original = self.value_multiplier;
			const normalized = value === null ? 1 : value; // clearing resets, never deletes
			if (normalized === original) {
				this.staged.delete(key);
			} else {
				const emp = this.all_employees().find((e) => e.employee === employee);
				this.staged.set(key, {
					kind: "self",
					employee,
					scheduling_role: self.scheduling_role,
					value: normalized,
					was: original,
					who: (emp && emp.employee_name) || employee,
				});
			}
			return true;
		}

		const cell = this.data.cells[key];
		if (!cell && !this.data.can_create && value !== null) {
			if (!silent) {
				frappe.show_alert({
					message: __("You are not permitted to create an Employee Role Synergy."),
					indicator: "red",
				});
			}
			return false;
		}
		if (value === null && cell && !this.data.can_delete) {
			if (!silent) {
				frappe.show_alert({
					message: __("You are not permitted to delete an Employee Role Synergy."),
					indicator: "red",
				});
			}
			return false;
		}

		const original = cell ? cell.synergy_multiplier : null;
		if (value === original) {
			this.staged.delete(key);
		} else {
			const [a, b] = key.split("|");
			const by_id = Object.fromEntries(this.all_employees().map((e) => [e.employee, e]));
			const who = `${(by_id[a] && by_id[a].employee_name) || a} × ${
				(by_id[b] && by_id[b].employee_name) || b
			}`;
			this.staged.set(key, {
				kind: "pair",
				employee_a: a,
				employee_b: b,
				value,
				was: original,
				who,
			});
		}
		return true;
	}

	stage_input($input) {
		const key = $input.attr("data-key");
		const $td = $input.closest("td");
		const raw = String($input.val() || "")
			.trim()
			.replace(",", ".");
		const value = raw === "" ? null : Number(raw);

		// Typing into a cell that is part of an active multi-selection edits the whole
		// selection — the same "type once, fill the selected range" a spreadsheet gives —
		// rather than only the one cell that happened to have focus.
		if (this.selected.size > 1 && this.selected.has(key)) {
			if (value !== null && (!Number.isFinite(value) || value < 0)) {
				frappe.show_alert({
					message: __("Multiplier is a number 0 or above."),
					indicator: "red",
				});
				this.paint($td);
				return;
			}
			[...this.selected].forEach((k) => this.stage_value(k, value, { silent: true }));
			this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
			this.render_transcript();
			return;
		}

		if (!this.stage_value(key, value)) {
			this.paint($td);
			return;
		}
		this.paint($td);
		this.render_transcript();
	}

	// The row/column bulk edit the matrix's symmetry asks for: row `employee` and column
	// `employee` are the same underlying pairs, so one prompt covers both at once. Scoped to
	// pairs only — the "Self" column already has its own cell for the employee's own value.
	// `employee` may be on either side, so the "others" it pairs against is whichever of the
	// two lists it is *not* in.
	bulk_edit_row(employee) {
		const row_employees = this.data.row_employees || [];
		const col_employees = this.data.col_employees || [];
		const in_row_side = row_employees.some((e) => e.employee === employee);
		const others = in_row_side ? col_employees : row_employees;

		const emp = this.all_employees().find((e) => e.employee === employee);
		const who = (emp && emp.employee_name) || employee;
		frappe.prompt(
			{
				fieldname: "value",
				fieldtype: "Data",
				label: __("Synergy Multiplier for every pair with {0}", [who]),
				description: __("1 = no bonus. Leave blank to clear every pair with {0}.", [who]),
			},
			(values) => {
				const raw = String(values.value || "")
					.trim()
					.replace(",", ".");
				const value = raw === "" ? null : Number(raw);
				if (value !== null && (!Number.isFinite(value) || value < 0)) {
					frappe.show_alert({
						message: __("Synergy Multiplier is a number 0 or above."),
						indicator: "red",
					});
					return;
				}
				others.forEach((other) => {
					this.stage_value(this.pair_key(employee, other.employee), value, {
						silent: true,
					});
				});
				this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
				this.render_transcript();
			},
			__("Set row/column"),
			__("Apply")
		);
	}

	on_key(e) {
		const $input = $(e.currentTarget);
		if (e.key === "Escape") {
			this.paint($input.closest("td"));
			$input.blur();
			return;
		}
		if (e.key !== "Enter") return;
		e.preventDefault();
		const $td = $input.closest("td");
		const column = $td.index();
		let $tr = $td.parent();
		do {
			$tr = e.shiftKey ? $tr.prev() : $tr.next();
		} while ($tr.length && $tr.prop("hidden"));
		$input.trigger("change");
		if ($tr.length) $tr.children().eq(column).find(".smx-input").trigger("focus");
		else $input.blur();
	}

	// Reads only the staged entry: an edit survives switching discipline, so its row may
	// no longer be in `data`.
	describe(change) {
		if (change.kind === "self") {
			return __("{0} (self): {1} → {2}", [
				change.who,
				format_multiplier(change.was),
				format_multiplier(change.value),
			]);
		}
		if (change.value === null) {
			return __("{0}: remove (was {1})", [change.who, format_multiplier(change.was)]);
		}
		if (change.was === null) {
			return __("{0}: add at {1}", [change.who, format_multiplier(change.value)]);
		}
		return __("{0}: {1} → {2}", [
			change.who,
			format_multiplier(change.was),
			format_multiplier(change.value),
		]);
	}

	render_transcript() {
		const $t = this.$body.find(".smx-transcript");
		if (!this.staged.size) {
			$t.html(`<div class="text-muted">${__("No pending edits.")}</div>`);
			return;
		}
		const items = [...this.staged.values()]
			.map((c) => `<li>${esc(this.describe(c))}</li>`)
			.join("");
		$t.html(`<div class="smx-transcript-title">${__("Pending edits")}</div><ol>${items}</ol>`);
	}

	// ── apply / discard ──────────────────────────────────────────────────────

	apply() {
		const focused = this.$body.find(".smx-input:focus");
		if (focused.length) this.stage_input(focused);

		const changes = [...this.staged.values()];
		if (!changes.length) {
			frappe.show_alert({ message: __("Nothing to apply."), indicator: "orange" });
			return;
		}
		const payload = changes.map((c) =>
			c.kind === "self"
				? {
						kind: "self",
						employee: c.employee,
						scheduling_role: c.scheduling_role,
						value_multiplier: c.value,
				  }
				: {
						kind: "pair",
						employee_a: c.employee_a,
						employee_b: c.employee_b,
						synergy_multiplier: c.value,
				  }
		);
		const list = changes.map((c) => `<li>${esc(this.describe(c))}</li>`).join("");
		frappe.confirm(
			`<p>${__("Apply {0} staged edit(s)?", [changes.length])}</p><ul>${list}</ul>`,
			() => {
				frappe
					.call({
						method: "autoshift.employee_role_synergy.apply_changes",
						args: { changes: payload },
						freeze: true,
						freeze_message: __("Applying…"),
					})
					.then(({ message }) => {
						this.staged.clear();
						this.selected.clear();
						frappe.show_alert({
							message: __("Added {0}, updated {1}, removed {2}.", [
								message.created,
								message.updated,
								message.deleted,
							]),
							indicator: "green",
						});
						this.refresh();
					});
			}
		);
	}

	discard() {
		this.selected.clear();
		if (!this.staged.size) {
			this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
			return;
		}
		this.staged.clear();
		this.$body.find("td.smx-cell[data-key]").each((_, td) => this.paint($(td)));
		this.render_transcript();
	}
};
