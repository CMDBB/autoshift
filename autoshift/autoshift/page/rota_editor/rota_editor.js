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

function inject_rota_editor_styles() {
	if (document.getElementById("rota-editor-styles")) return;
	const css = `
		.rota-editor .re-hint { margin-bottom: 0.75rem; }
		.rota-editor .re-grid-wrap { overflow-x: auto; }
		.rota-editor .re-table { border-collapse: collapse; font-size: var(--text-sm); }
		.rota-editor .re-table th, .rota-editor .re-table td {
			border: 1px solid var(--border-color); padding: 0.15rem 0.3rem; text-align: center;
		}
		.rota-editor .re-emp-col {
			text-align: left; white-space: nowrap; position: sticky; left: 0;
			background: var(--fg-color); z-index: 1;
		}
		.rota-editor .re-day-col { font-weight: normal; color: var(--text-muted); white-space: nowrap; }
		.rota-editor .re-cell { min-width: 2.2rem; height: 1.8rem; }
		.rota-editor .re-chip {
			display: inline-block; padding: 0.05rem 0.4rem; border-radius: var(--border-radius);
			background: var(--chip-bg, #cce0ff); cursor: grab; font-weight: 500; color: var(--chip-fg, inherit);
			border: 1px solid var(--chip-border, rgba(0, 0, 0, 0.1));
		}
		.rota-editor .re-chip-pending { background: var(--yellow-100, #fff3cd); cursor: not-allowed; opacity: 0.85; color: inherit; }
		.rota-editor .re-chip-cadence {
			font-size: 0.6em; opacity: 0.75; margin-left: 1px; vertical-align: super;
		}
		.rota-editor .re-cell-empty { display: block; width: 100%; height: 100%; min-height: 1.2rem; cursor: pointer; }
		.rota-editor .re-cell-empty:hover { background: var(--control-bg); }
		.rota-editor .re-cell-occupied { opacity: 0.4; background: #777777 }
		.rota-editor .re-row-hidden td { opacity: 0.6; background: var(--disabled-bg, #f2f2f2); }
		.rota-editor .re-row-hidden .re-emp-col { background: var(--disabled-bg, #f2f2f2); }
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
					"Drag a chip to move a shift within the same person's row — any day, shift type or branch in this discipline. Drop it on Remove to drop it, or click an empty cell to add one."
				)}</div>
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

	stage_add(employee, shift_type, date) {
		const options = (this.state.branches && this.state.branches[shift_type]) || [];
		const commit = (branch) =>
			this.stage({
				op: "add",
				employee,
				to_shift_type: shift_type,
				to_weekday: this.day_labels[date],
				to_phase: this.day_phases[date],
				to_branch: branch,
			});
		if (options.length === 1) {
			commit(options[0]);
		} else if (options.length > 1) {
			this.prompt_branch(options, null, commit);
		} else {
			frappe.msgprint(
				__("No branch is configured for {0} in this discipline.", [shift_type])
			);
		}
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
		this.render_grid();
		this.render_transcript();
	}

	// One <tr> for an employee. `readOnly` employees (period-incompatible with this
	// view — see `edit.rota_view_weeks`) get no drag/drop affordances at all: their
	// cells carry a `{occupied, cycle_weeks, branch}` fraction summary instead of a
	// concrete `{branch, assignment}` chip — see `editor._hidden_cells`.
	render_employee_row(emp, sections, days, occupied_dates, readOnly) {
		const cadence_note =
			readOnly && emp.cycle_weeks
				? ` <span class="text-muted">(${emp.cycle_weeks.join(", ")}-week)</span>`
				: "";
		let row = `<tr class="${
			readOnly ? "re-row-hidden" : ""
		}"><td class="re-emp-col">${frappe.utils.escape_html(
			emp.employee_name
		)}${cadence_note}</td>`;
		sections.forEach((section) => {
			const shift_type = frappe.utils.escape_html(section.name);
			days.forEach((d) => {
				const cell = emp.cells[`${section.name}|${d.date}`];
				if (readOnly) {
					row += `<td class="re-cell re-cell-readonly">`;
					if (cell) {
						if (cell.occupied >= cell.cycle_weeks) {
							const branch = frappe.utils.escape_html(cell.branch || "");
							const color = branch_color(cell.branch || "");
							row +=
								`<span class="re-chip" draggable="false" ` +
								`style="--chip-bg: ${color.bg}; --chip-fg: ${color.fg}; --chip-border: ${color.border};" ` +
								`title="${branch}">${(cell.branch || "?").slice(0, 3)}</span>`;
						} else {
							row += `<span class="re-fraction" title="${__(
								"Occurs {0} of every {1} weeks",
								[cell.occupied, cell.cycle_weeks]
							)}">${cell.occupied}/${cell.cycle_weeks}</span>`;
						}
					}
					row += "</td>";
					return;
				}
				const is_occupied_different_shift =
					occupied_dates[emp.employee].has(d.date) && !cell;
				const cell_class = is_occupied_different_shift ? " re-cell-occupied" : "";
				row += `<td class="re-cell${cell_class}" data-employee="${emp.employee}" data-shift-type="${shift_type}" data-date="${d.date}">`;
				if (cell) {
					const pending = String(cell.assignment).indexOf("NEW-") === 0;
					const branch = frappe.utils.escape_html(cell.branch || "");
					const color = pending ? {} : branch_color(cell.branch || "");
					const style = pending
						? ""
						: `style="--chip-bg: ${color.bg}; --chip-fg: ${color.fg}; --chip-border: ${color.border};"`;
					const cadence =
						cell.cycle_weeks > 1
							? `<sup class="re-chip-cadence">${cell.cycle_weeks}w</sup>`
							: "";
					const cadence_title =
						cell.cycle_weeks > 1
							? " — " + __("every {0} weeks", [cell.cycle_weeks])
							: "";
					row +=
						`<span class="re-chip${pending ? " re-chip-pending" : ""}" ` +
						`draggable="${pending ? "false" : "true"}" ` +
						`data-assignment="${frappe.utils.escape_html(cell.assignment)}" ` +
						`data-employee="${emp.employee}" data-shift-type="${shift_type}" data-date="${d.date}" ` +
						`data-branch="${branch}" ${style} title="${branch}${
							pending ? " — " + __("pending, apply first") : cadence_title
						}">` +
						`${(cell.branch || "?").slice(0, 3)}${cadence}</span>`;
				} else {
					row +=
						`<span class="re-cell-empty" data-employee="${emp.employee}" ` +
						`data-shift-type="${shift_type}" data-date="${d.date}"></span>`;
				}
				row += "</td>";
			});
		});
		row += "</tr>";
		return row;
	}

	render_grid() {
		const state = this.state;
		const sections = state.shift_types || [];
		const days = state.days || [];
		const employees = state.employees || [];
		const hidden = state.hidden_employees || [];
		const $grid = this.$body.find(".re-grid");

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

		let head1 = `<tr><th class="re-emp-col">${__("Employee")}</th>`;
		let head2 = `<tr><th class="re-emp-col"></th>`;
		sections.forEach((section) => {
			head1 += `<th colspan="${days.length}">${frappe.utils.escape_html(section.name)}</th>`;
			days.forEach((d) => {
				head2 += `<th class="re-day-col">${d.weekday.slice(0, 3)}<br>${d.date.slice(
					5
				)}</th>`;
			});
		});
		head1 += "</tr>";
		head2 += "</tr>";

		// Build a map of occupied dates per employee (dates that have a chip, regardless of shift type)
		const occupied_dates = {};
		employees.forEach((emp) => {
			occupied_dates[emp.employee] = new Set();
			sections.forEach((section) => {
				days.forEach((d) => {
					if (emp.cells[`${section.name}|${d.date}`]) {
						occupied_dates[emp.employee].add(d.date);
					}
				});
			});
		});

		let rows = "";
		employees.forEach((emp) => {
			rows += this.render_employee_row(emp, sections, days, occupied_dates, false);
		});
		if (hidden.length) {
			const colspan = 1 + sections.length * days.length;
			rows +=
				`<tr class="re-divider"><td colspan="${colspan}">${__(
					"Longer cadence than this view — shown below as a read-only average over each pattern's own cycle"
				)}</td></tr>` +
				hidden
					.map((emp) => this.render_employee_row(emp, sections, days, {}, true))
					.join("");
		}

		$grid.html(
			`<table class="re-table"><thead>${head1}${head2}</thead><tbody>${rows}</tbody></table>`
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
							message: __("Created {0}, replaced {1}.", [
								message.created,
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
