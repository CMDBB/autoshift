// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see bulk_employee_settings.js for why. Page
// scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift");

frappe.pages["role-matrix"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Role Matrix"),
		single_column: true,
	});
	wrapper.role_matrix = new autoshift.RoleMatrix(page);
};

function inject_role_matrix_styles() {
	if (document.getElementById("role-matrix-styles")) return;
	const css = `
		.role-matrix .rm-hint { margin-bottom: 0.75rem; }
		.role-matrix .rm-grid-wrap { overflow: auto; max-height: calc(100vh - 16rem); }
		.role-matrix .rm-table { border-collapse: separate; border-spacing: 0; font-size: var(--text-sm); }
		.role-matrix .rm-table th, .role-matrix .rm-table td {
			border-right: 1px solid var(--border-color); border-bottom: 1px solid var(--border-color);
			padding: 0.15rem 0.3rem; text-align: center; background: var(--fg-color);
		}
		.role-matrix .rm-table thead th {
			position: sticky; z-index: 3; color: var(--text-muted); font-weight: normal;
			vertical-align: bottom; white-space: nowrap;
		}
		.role-matrix .rm-table thead tr:first-child th { top: 0; }
		.role-matrix .rm-table thead tr.rm-head-roles th { top: var(--rm-head-offset, 0); }
		.role-matrix .rm-disc-head { font-weight: 500 !important; color: var(--text-color) !important; }
		.role-matrix .rm-role-head.rm-binding { text-decoration: underline dotted; }
		.role-matrix .rm-emp-col {
			position: sticky; left: 0; z-index: 2; text-align: left; white-space: nowrap; min-width: 11rem;
		}
		.role-matrix .rm-settings-col {
			position: sticky; left: 11rem; z-index: 2; min-width: 7rem; max-width: 9rem;
			border-right: 2px solid var(--border-color) !important;
		}
		.role-matrix thead .rm-emp-col, .role-matrix thead .rm-settings-col { z-index: 4; }
		.role-matrix .rm-initials { color: var(--text-muted); margin-left: 0.3rem; font-size: 0.85em; }
		.role-matrix tbody tr:nth-child(even) td { background: var(--zebra-bg, rgba(128, 128, 128, 0.04)); }
		.role-matrix .rm-chip {
			display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
			white-space: nowrap; padding: 0.05rem 0.45rem; border-radius: var(--border-radius);
			background: var(--blue-100, #dbeafe); color: var(--text-color); text-decoration: none;
			border: 1px solid var(--border-color);
		}
		.role-matrix .rm-chip:hover { text-decoration: none; filter: brightness(0.95); }
		.role-matrix .rm-chip-none { background: transparent; color: var(--text-muted); border-style: dashed; }
		.role-matrix .rm-chip-inactive { background: var(--gray-100, #f3f3f3); color: var(--text-muted); }
		.role-matrix td.rm-cell { position: relative; padding: 0; min-width: 2.6rem; }
		.role-matrix .rm-input {
			width: 100%; min-width: 2.6rem; max-width: 3.2rem; height: 1.8rem; border: none; background: transparent;
			text-align: center; color: inherit; font-variant-numeric: tabular-nums;
		}
		.role-matrix .rm-input:focus { outline: 2px solid var(--primary, #2490ef); outline-offset: -2px; }
		.role-matrix .rm-input[readonly] { cursor: not-allowed; }
		.role-matrix td.rm-band-1 { background: var(--green-100, #dcfce7) !important; }
		.role-matrix td.rm-band-2 { background: var(--yellow-100, #fef9c3) !important; }
		.role-matrix td.rm-band-3 { background: var(--orange-100, #ffedd5) !important; }
		.role-matrix td.rm-band-4 { background: var(--red-100, #fee2e2) !important; }
		.role-matrix td.rm-idle .rm-input { opacity: 0.45; text-decoration: line-through; }
		.role-matrix td.rm-pending { box-shadow: inset 0 0 0 2px var(--yellow-500, #eab308); }
		.role-matrix td.rm-pending .rm-input { font-weight: 600; }
		.role-matrix .rm-open {
			position: absolute; top: 0; right: 0.1rem; font-size: 0.7em; line-height: 1;
			color: var(--text-muted); visibility: hidden; text-decoration: none;
		}
		.role-matrix td.rm-cell:hover .rm-open { visibility: visible; }
		.role-matrix .rm-legend { margin: 0.5rem 0; display: flex; gap: 0.4rem; flex-wrap: wrap; align-items: center; }
		.role-matrix .rm-legend span { padding: 0.05rem 0.45rem; border-radius: var(--border-radius); }
		.role-matrix .rm-transcript { margin-top: 1rem; border-top: 1px solid var(--border-color); padding-top: 0.75rem; }
		.role-matrix .rm-transcript-title { font-weight: 500; margin-bottom: 0.3rem; }
	`;
	const style = document.createElement("style");
	style.id = "role-matrix-styles";
	style.textContent = css;
	document.head.appendChild(style);
}

const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));

// Word initials, the wall chart's lane-header abbreviation ("Dental Hygienist" -> "DH");
// the full name goes in the title. Falls back to the name when it has no letters at all.
function abbreviate(name) {
	const initials = String(name || "").match(/(?<!\p{L})[\p{L}]/gu);
	return initials ? initials.map((c) => c.toUpperCase()).join("") : String(name || "");
}

// 1 is a regular holder; the bands only colour how much worse than that a substitute is.
function suitability_band(value) {
	if (value == null) return "";
	if (value <= 1) return "rm-band-1";
	if (value <= 1.5) return "rm-band-2";
	if (value < 2.5) return "rm-band-3";
	return "rm-band-4";
}

function format_suitability(value) {
	return value == null ? "" : String(Math.round(value * 100) / 100);
}

autoshift.RoleMatrix = class RoleMatrix {
	constructor(page) {
		this.page = page;
		this.data = null;
		// "employee|role" -> {employee, role, suitability}; suitability null = remove the row
		this.staged = new Map();

		inject_role_matrix_styles();
		this.setup_fields();
		this.setup_body();
		this.page.set_primary_action(__("Apply Changes"), () => this.apply());
		this.page.set_secondary_action(__("Discard Changes"), () => this.discard());
		this.page.add_inner_button(__("Reload"), () => this.refresh());
		$(window).on("beforeunload.role_matrix", () => (this.staged.size ? true : undefined));
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
			<div class="role-matrix">
				<div class="rm-hint text-muted">${__(
					"One row per employee, one column per Scheduling Role. A number means the employee may work that role: 1 for a regular holder, higher for a less suitable substitute (1.2 a good backup, 3 a terrible but feasible one). Clear a cell to remove the role. Nothing is saved until you click Apply Changes."
				)}</div>
				<div class="rm-legend text-muted">
					<span style="background: var(--green-100, #dcfce7)">1</span>
					<span style="background: var(--yellow-100, #fef9c3)">≤ 1.5</span>
					<span style="background: var(--orange-100, #ffedd5)">&lt; 2.5</span>
					<span style="background: var(--red-100, #fee2e2)">≥ 2.5</span>
					<span>${__("struck through: inactive or outside its validity window today")}</span>
				</div>
				<div class="rm-grid-wrap"><div class="rm-grid"></div></div>
				<div class="rm-transcript"></div>
			</div>
		`).appendTo(this.page.main);

		this.$body
			.on("change", ".rm-input", (e) => this.stage_input($(e.currentTarget)))
			.on("keydown", ".rm-input", (e) => this.on_key(e))
			.on("focus", ".rm-input", (e) => e.currentTarget.select());
	}

	load_disciplines() {
		frappe.call({ method: "autoshift.role_matrix.list_disciplines" }).then(({ message }) => {
			const disciplines = message || [];
			this.discipline_field.df.options = [
				{ label: __("All Disciplines"), value: "" },
				...disciplines.map((d) => ({ label: d, value: d })),
			];
			this.discipline_field.refresh();
			const wanted = (frappe.route_options || {}).discipline;
			frappe.route_options = null;
			this.discipline_field.set_value(
				disciplines.includes(wanted) ? wanted : disciplines[0] || ""
			);
			this.loaded = true;
			this.refresh();
		});
	}

	refresh() {
		if (!this.loaded) return;
		frappe
			.call({
				method: "autoshift.role_matrix.get_matrix",
				args: {
					discipline: this.discipline_field.get_value() || "",
					show_all: this.show_all_field.get_value() ? 1 : 0,
				},
			})
			.then(({ message }) => this.render(message));
	}

	// ── rendering ────────────────────────────────────────────────────────────

	render(data) {
		this.data = data;
		this.render_grid();
		this.render_transcript();
	}

	render_grid() {
		const { roles = [], employees = [] } = this.data;
		const $grid = this.$body.find(".rm-grid");
		if (!roles.length) {
			$grid.html(
				`<div class="text-muted">${__(
					"No active Scheduling Role in this discipline."
				)}</div>`
			);
			return;
		}
		if (!employees.length) {
			$grid.html(
				`<div class="text-muted">${__(
					"Nobody holds a role here yet. Tick All Employees to hand one out."
				)}</div>`
			);
			return;
		}

		// Discipline header row only when more than one discipline shares the table.
		const groups = [];
		roles.forEach((r) => {
			const last = groups[groups.length - 1];
			if (last && last.discipline === r.discipline) last.span += 1;
			else groups.push({ discipline: r.discipline, span: 1 });
		});
		const grouped = groups.length > 1;

		let head = "";
		if (grouped) {
			head +=
				`<tr><th class="rm-emp-col" rowspan="2">${__("Employee")}</th>` +
				`<th class="rm-settings-col" rowspan="2">${__("Settings")}</th>` +
				groups
					.map(
						(g) =>
							`<th class="rm-disc-head" colspan="${g.span}" title="${esc(
								g.discipline
							)}">${esc(g.span > 2 ? g.discipline : abbreviate(g.discipline))}</th>`
					)
					.join("") +
				"</tr>";
		}
		head += `<tr class="rm-head-roles">`;
		if (!grouped) {
			head +=
				`<th class="rm-emp-col">${__("Employee")}</th>` +
				`<th class="rm-settings-col">${__("Settings")}</th>`;
		}
		head +=
			roles
				.map((r) => {
					const title = r.binding
						? __("{0} — assignments are binding", [r.role])
						: r.role;
					return `<th class="rm-role-head${r.binding ? " rm-binding" : ""}" title="${esc(
						title
					)}"><a href="/app/scheduling-role/${encodeURIComponent(
						r.role
					)}" target="_blank" class="text-muted">${esc(abbreviate(r.role))}</a></th>`;
				})
				.join("") + "</tr>";

		const rows = employees
			.map((emp) => {
				const cells = roles.map((r) => this.cell_html(emp, r)).join("");
				return (
					`<tr data-employee="${esc(emp.employee)}" data-search="${esc(
						`${emp.employee} ${emp.employee_name} ${emp.initials}`.toLowerCase()
					)}">` +
					`<td class="rm-emp-col" title="${esc(emp.employee)}">${esc(
						emp.employee_name || emp.employee
					)}<span class="rm-initials">${esc(emp.initials)}</span></td>` +
					`<td class="rm-settings-col">${this.settings_chip(emp)}</td>` +
					cells +
					"</tr>"
				);
			})
			.join("");

		$grid.html(`<table class="rm-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`);
		if (grouped) {
			const height = $grid.find("thead tr:first-child").outerHeight() || 0;
			$grid.find(".rm-table").css("--rm-head-offset", `${height}px`);
		}
		$grid.find("td.rm-cell").each((_, td) => this.paint($(td)));
		this.apply_search();
	}

	cell_html(emp, role) {
		const key = `${emp.employee}|${role.role}`;
		const cell = this.data.cells[key];
		const readonly = !cell && !this.data.can_create ? " readonly" : "";
		const open = cell
			? `<a class="rm-open" href="/app/employee-scheduling-role/${encodeURIComponent(
					cell.name
			  )}" target="_blank" title="${__(
					"Open Employee Scheduling Role"
			  )}" tabindex="-1">↗</a>`
			: "";
		return (
			`<td class="rm-cell" data-key="${esc(key)}">` +
			`<input class="rm-input" type="text" inputmode="decimal" autocomplete="off"${readonly} ` +
			`data-key="${esc(key)}">${open}</td>`
		);
	}

	current_value(key) {
		if (this.staged.has(key)) return this.staged.get(key).suitability;
		const cell = this.data.cells[key];
		return cell ? cell.suitability : null;
	}

	in_force(cell) {
		const today = this.data.today;
		return (
			cell.active &&
			(!cell.valid_from || cell.valid_from <= today) &&
			(!cell.valid_to || cell.valid_to >= today)
		);
	}

	// Class, title and value of one cell from `data` + `staged`, without re-rendering the
	// table — so a keyboard walk through the matrix keeps its focus.
	paint($td) {
		const key = $td.attr("data-key");
		const cell = this.data.cells[key];
		const value = this.current_value(key);
		const pending = this.staged.has(key);

		$td.removeClass("rm-band-1 rm-band-2 rm-band-3 rm-band-4 rm-pending rm-idle");
		$td.addClass(suitability_band(value));
		if (pending) $td.addClass("rm-pending");
		if (cell && value != null && !this.in_force(cell)) $td.addClass("rm-idle");
		$td.find(".rm-input").val(format_suitability(value));

		const lines = [];
		if (cell) {
			if (pending) lines.push(__("Was {0}", [format_suitability(cell.suitability)]));
			if (!cell.active) lines.push(__("Inactive"));
			if (cell.valid_from) lines.push(__("Valid from {0}", [cell.valid_from]));
			if (cell.valid_to) lines.push(__("Valid to {0}", [cell.valid_to]));
			if (cell.role_fte) lines.push(__("Agreed FTE {0}%", [cell.role_fte]));
			if (cell.max_rooms) lines.push(__("Max rooms {0}", [cell.max_rooms]));
			if (cell.binding_override) lines.push(__(cell.binding_override));
		} else if (pending) {
			lines.push(__("New"));
		}
		$td.attr("title", lines.join("\n"));
	}

	settings_chip(emp) {
		const s = emp.settings;
		if (!s) {
			return `<a class="rm-chip rm-chip-none" target="_blank" href="/app/employee-settings/new?employee=${encodeURIComponent(
				emp.employee
			)}" title="${esc(
				__("No Employee Settings: uniform shift preferences. Click to create one.")
			)}">+ ${__("Create")}</a>`;
		}
		let text;
		if (s.favourite_shift) {
			text = `★ ${s.favourite_shift}`;
		} else if (s.shift_preferences.length) {
			const top = s.shift_preferences.reduce((a, b) =>
				(b.weight || 0) > (a.weight || 0) ? b : a
			);
			text = `↑ ${top.shift_type}`;
		} else {
			text = __("Uniform");
		}
		if (s.branch_preferences.length) text += ` · ${s.branch_preferences.length} ${__("br")}`;

		const lines = [];
		if (s.favourite_shift) lines.push(__("Favourite shift: {0}", [s.favourite_shift]));
		if (s.shift_preferences.length) {
			lines.push(
				__("Shift preferences: {0}", [
					s.shift_preferences.map((p) => `${p.shift_type} ${p.weight}`).join(", "),
				])
			);
		}
		if (!s.favourite_shift && !s.shift_preferences.length) {
			lines.push(__("Uniform shift preferences"));
		}
		if (s.branch_preferences.length) {
			lines.push(
				__("Branch preferences (not read by the optimizer yet): {0}", [
					s.branch_preferences.map((p) => `${p.branch} ${p.weight}`).join(", "),
				])
			);
		}
		if (!s.active) lines.push(__("Active is unchecked"));
		return `<a class="rm-chip${
			s.active ? "" : " rm-chip-inactive"
		}" target="_blank" href="/app/employee-settings/${encodeURIComponent(
			s.name
		)}" title="${esc(lines.join("\n"))}">${esc(text)}</a>`;
	}

	apply_search() {
		const needle = (this.search_field.get_value() || "").trim().toLowerCase();
		this.$body.find(".rm-table tbody tr").each((_, tr) => {
			const $tr = $(tr);
			$tr.prop("hidden", !!needle && !$tr.attr("data-search").includes(needle));
		});
	}

	// ── staging ──────────────────────────────────────────────────────────────

	stage_input($input) {
		const key = $input.attr("data-key");
		const $td = $input.closest("td");
		const cell = this.data.cells[key];
		const raw = String($input.val() || "")
			.trim()
			.replace(",", ".");
		const value = raw === "" ? null : Number(raw);

		if (value !== null && (!Number.isFinite(value) || value < 1)) {
			frappe.show_alert({
				message: __("Suitability is a number from 1 (a regular holder) upwards."),
				indicator: "red",
			});
			this.paint($td);
			return;
		}
		if (value === null && cell && !this.data.can_delete) {
			frappe.show_alert({
				message: __("You are not permitted to delete an Employee Scheduling Role."),
				indicator: "red",
			});
			this.paint($td);
			return;
		}

		const original = cell ? cell.suitability : null;
		if (value === original) {
			this.staged.delete(key);
		} else {
			const [employee, role] = key.split("|");
			const emp = this.data.employees.find((e) => e.employee === employee);
			const who = (emp && emp.employee_name) || employee;
			this.staged.set(key, { employee, role, suitability: value, was: original, who });
		}
		this.paint($td);
		this.render_transcript();
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
		// Enter walks down the column (Shift+Enter up), like a spreadsheet.
		const $td = $input.closest("td");
		const column = $td.index();
		let $tr = $td.parent();
		do {
			$tr = e.shiftKey ? $tr.prev() : $tr.next();
		} while ($tr.length && $tr.prop("hidden"));
		$input.trigger("change");
		if ($tr.length) $tr.children().eq(column).find(".rm-input").trigger("focus");
		else $input.blur();
	}

	// Reads only the staged entry: an edit survives switching discipline, so its row may
	// no longer be in `data`.
	describe(change) {
		if (change.suitability === null) {
			return __("{0}: remove {1} (was {2})", [
				change.who,
				change.role,
				format_suitability(change.was),
			]);
		}
		if (change.was === null) {
			return __("{0}: add {1} at {2}", [
				change.who,
				change.role,
				format_suitability(change.suitability),
			]);
		}
		return __("{0}: {1} {2} → {3}", [
			change.who,
			change.role,
			format_suitability(change.was),
			format_suitability(change.suitability),
		]);
	}

	render_transcript() {
		const $t = this.$body.find(".rm-transcript");
		if (!this.staged.size) {
			$t.html(`<div class="text-muted">${__("No pending edits.")}</div>`);
			return;
		}
		const items = [...this.staged.values()]
			.map((c) => `<li>${esc(this.describe(c))}</li>`)
			.join("");
		$t.html(`<div class="rm-transcript-title">${__("Pending edits")}</div><ol>${items}</ol>`);
	}

	// ── apply / discard ──────────────────────────────────────────────────────

	apply() {
		// A cell still being typed into has not fired `change` yet.
		const focused = this.$body.find(".rm-input:focus");
		if (focused.length) this.stage_input(focused);

		const changes = [...this.staged.values()];
		if (!changes.length) {
			frappe.show_alert({ message: __("Nothing to apply."), indicator: "orange" });
			return;
		}
		const list = changes.map((c) => `<li>${esc(this.describe(c))}</li>`).join("");
		const removals = changes.some((c) => c.suitability === null)
			? `<p class="text-muted">${__(
					"Removing a role deletes its Employee Scheduling Role, including any agreed FTE, rooms override, binding override and validity window on it."
			  )}</p>`
			: "";
		frappe.confirm(
			`<p>${__("Apply {0} staged edit(s)?", [
				changes.length,
			])}</p><ul>${list}</ul>${removals}`,
			() => {
				frappe
					.call({
						method: "autoshift.role_matrix.apply_changes",
						args: { changes },
						freeze: true,
						freeze_message: __("Applying…"),
					})
					.then(({ message }) => {
						this.staged.clear();
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
		if (!this.staged.size) return;
		this.staged.clear();
		this.$body.find("td.rm-cell").each((_, td) => this.paint($(td)));
		this.render_transcript();
	}
};
