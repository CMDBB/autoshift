// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see bulk_employee_settings.js for why. Page
// scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift");

frappe.pages["optimizer-studio"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Optimizer Studio"),
		single_column: true,
	});
	wrapper.optimizer_studio = new autoshift.OptimizerStudio(page);
};

const NONE_VALUE = "__none__";
const WEIGHTED_KINDS = ["Objective", "Mixed"];
const KIND_ORDER = ["Constraint", "Mixed", "Objective", "Other"];

function inject_studio_styles() {
	if (document.getElementById("optimizer-studio-styles")) return;
	const css = `
		.optimizer-studio .op-section-title {
			margin-top: 1.25rem; margin-bottom: 0.4rem; color: var(--text-muted);
			text-transform: uppercase; font-size: var(--text-xs); letter-spacing: 0.04em;
		}
		.optimizer-studio .op-group {
			border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
			padding: 0.6rem 0.75rem; margin-bottom: 0.6rem;
		}
		.optimizer-studio .op-group-label { font-weight: 500; margin-bottom: 0.3rem; }
		.optimizer-studio .op-row { padding: 0.3rem 0; }
		.optimizer-studio .op-check-label, .optimizer-studio .op-radio-label {
			display: inline-flex; align-items: center; gap: 0.4rem; font-weight: normal; margin-bottom: 0;
		}
		.optimizer-studio .op-desc {
			font-size: var(--text-xs); margin-left: 1.4rem; margin-top: 0.1rem;
		}
		.optimizer-studio .op-weight { margin-left: 0.6rem; }
		.optimizer-studio .op-leave-picker { max-width: 24rem; }
		.optimizer-studio .op-pill {
			display: inline-flex; align-items: center; gap: 0.3rem;
			border: 1px solid var(--border-color); border-radius: var(--border-radius);
			padding: 0.15rem 0.5rem; margin: 0.15rem 0.3rem 0.15rem 0; font-size: var(--text-sm);
		}
		.optimizer-studio .op-result-header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
	`;
	const style = document.createElement("style");
	style.id = "optimizer-studio-styles";
	style.textContent = css;
	document.head.appendChild(style);
}

autoshift.OptimizerStudio = class OptimizerStudio {
	constructor(page) {
		this.page = page;
		this.leaves = new Set();
		this.catalog = [];
		this.last_ruleset = null;

		inject_studio_styles();
		this.setup_fields();
		this.setup_body();
		this.page.set_primary_action(__("Preview Schedule"), () => this.preview());
		this.load_catalog();
	}

	setup_fields() {
		this.mode_field = this.page.add_field({
			fieldname: "mode",
			label: __("Planning Mode"),
			fieldtype: "Select",
			options: "1-week\n2-week\n4-week\nUnbounded",
			default: "1-week",
			reqd: 1,
		});
		this.mode_field.set_value("1-week");

		this.date_field = this.page.add_field({
			fieldname: "date",
			label: __("Start Date"),
			fieldtype: "Date",
			reqd: 1,
		});

		// One-shot: picking a run copies its configuration into the panel below, it
		// isn't a persistent "linked to" state — see populate_from_run().
		this.populate_field = this.page.add_field({
			fieldname: "populate_from",
			label: __("Populate From Run"),
			fieldtype: "Link",
			options: "Optimizer Run",
			change: () => this.populate_from_run(),
		});
	}

	setup_body() {
		this.$body = $(`
			<div class="optimizer-studio">
				<div class="op-section-title">${__("Treat as approved (pending leaves)")}</div>
				<div class="op-leave-picker"></div>
				<div class="op-leave-pills">
					<span class="text-muted">${__("None selected")}</span>
				</div>
				<div class="op-catalog">
					<div class="text-muted" style="padding: 0.5rem 0;">${__("Loading rules…")}</div>
				</div>
				<div class="op-result"></div>
			</div>
		`).appendTo(this.page.main);

		this.leave_field = frappe.ui.form.make_control({
			df: {
				fieldname: "add_leave",
				fieldtype: "Link",
				options: "Leave Application",
				placeholder: __("Add a pending Leave Application…"),
				get_query: () => ({ filters: { status: "Open" } }),
				change: () => {
					const value = this.leave_field.get_value();
					if (value) {
						this.leaves.add(value);
						this.leave_field.set_value("");
						this.render_leave_pills();
					}
				},
			},
			parent: this.$body.find(".op-leave-picker"),
			render_input: 1,
		});
	}

	// ── pending leaves ────────────────────────────────────────────────────────

	render_leave_pills() {
		const $pills = this.$body.find(".op-leave-pills");
		if (!this.leaves.size) {
			$pills.html(`<span class="text-muted">${__("None selected")}</span>`);
			return;
		}
		$pills.html(
			Array.from(this.leaves)
				.map(
					(name) => `
					<span class="op-pill" data-leave="${frappe.utils.escape_html(name)}">
						${frappe.utils.escape_html(name)}
						<a href="#" class="op-remove-leave" title="${__("Remove")}">&times;</a>
					</span>`
				)
				.join("")
		);
		$pills.find(".op-remove-leave").on("click", (e) => {
			e.preventDefault();
			this.leaves.delete($(e.currentTarget).closest(".op-pill").data("leave"));
			this.render_leave_pills();
		});
	}

	// ── rule catalog ──────────────────────────────────────────────────────────

	load_catalog() {
		frappe
			.call({ method: "autoshift.optimizer_studio.get_rule_catalog" })
			.then(({ message }) => {
				this.catalog = message || [];
				this.$body.find(".op-catalog").html(this.build_catalog_html());
				// Standard Ruleset is a sensible starting point; the user can always
				// re-populate from a specific run instead.
				return frappe.call({
					method: "autoshift.optimizer_studio.get_ruleset_selection",
					args: { ruleset: "Standard Ruleset" },
				});
			})
			.then(({ message }) => this.set_selection((message && message.rows) || {}));
	}

	humanize_group(key) {
		return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
	}

	weight_input_html(rule) {
		if (!WEIGHTED_KINDS.includes(rule.kind)) return "";
		return `<input type="number" step="0.1"
			class="op-weight form-control input-xs"
			style="width:4.5rem;display:inline-block;" value="1.0">`;
	}

	build_catalog_html() {
		const groups = {};
		const standalone = [];
		for (const rule of this.catalog) {
			if (rule.group) {
				(groups[rule.group] = groups[rule.group] || []).push(rule);
			} else {
				standalone.push(rule);
			}
		}

		let html = "";
		const group_keys = Object.keys(groups).sort();
		if (group_keys.length) {
			html += `<div class="op-section-title">${__("Choices")}</div>`;
			html += group_keys.map((key) => this.render_group(key, groups[key])).join("");
		}

		for (const kind of KIND_ORDER) {
			const rules = standalone.filter((r) => r.kind === kind);
			if (!rules.length) continue;
			html += `<div class="op-section-title">${__(kind)}</div>`;
			html += rules.map((r) => this.render_toggle_row(r)).join("");
		}
		return html || `<div class="text-muted">${__("No implemented rules found.")}</div>`;
	}

	render_group(key, rules) {
		const options = rules
			.map(
				(r) => `
				<div class="op-row" data-rule="${frappe.utils.escape_html(r.name)}">
					<label class="op-radio-label">
						<input type="radio" class="op-toggle-group" name="op-group-${frappe.utils.escape_html(
							key
						)}" value="${frappe.utils.escape_html(r.name)}">
						<span>${frappe.utils.escape_html(r.title)}</span>
					</label>
					${this.weight_input_html(r)}
					${
						r.description
							? `<div class="op-desc text-muted">${frappe.utils.escape_html(
									r.description
							  )}</div>`
							: ""
					}
				</div>`
			)
			.join("");
		return `
			<div class="op-group" data-group="${frappe.utils.escape_html(key)}">
				<div class="op-group-label">${frappe.utils.escape_html(this.humanize_group(key))}</div>
				<div class="op-row" data-rule="${NONE_VALUE}">
					<label class="op-radio-label">
						<input type="radio" class="op-toggle-group" name="op-group-${frappe.utils.escape_html(
							key
						)}" value="${NONE_VALUE}" checked>
						<span>${__("None")}</span>
					</label>
				</div>
				${options}
			</div>`;
	}

	render_toggle_row(rule) {
		return `
			<div class="op-row" data-rule="${frappe.utils.escape_html(rule.name)}">
				<label class="op-check-label">
					<input type="checkbox" class="op-toggle">
					<span>${frappe.utils.escape_html(rule.title)}</span>
				</label>
				${this.weight_input_html(rule)}
				${
					rule.description
						? `<div class="op-desc text-muted">${frappe.utils.escape_html(
								rule.description
						  )}</div>`
						: ""
				}
			</div>`;
	}

	// Reads directly off the DOM (`.op-row[data-rule]` pairs a toggle with its optional
	// weight input) rather than keying lookups by rule name — rule titles are free text
	// and not safe to interpolate into a CSS attribute selector.
	get_selection() {
		const rows = {};
		this.$body.find(".op-catalog .op-row").each((_, rowEl) => {
			const $row = $(rowEl);
			const rule = $row.data("rule");
			if (!rule || rule === NONE_VALUE) return;
			const $checked = $row.find(".op-toggle:checked, .op-toggle-group:checked");
			if (!$checked.length) return;
			const $weight = $row.find(".op-weight");
			const value = $weight.length ? parseFloat($weight.val()) : NaN;
			rows[rule] = Number.isFinite(value) ? value : 1.0;
		});
		return rows;
	}

	set_selection(rows) {
		rows = rows || {};
		this.$body.find(".op-catalog .op-row").each((_, rowEl) => {
			const $row = $(rowEl);
			const rule = $row.data("rule");
			const has = rule !== NONE_VALUE && Object.prototype.hasOwnProperty.call(rows, rule);
			$row.find(".op-toggle").prop("checked", has);
			$row.find(".op-toggle-group").prop("checked", rule === NONE_VALUE ? false : has);
			if (has) {
				const $weight = $row.find(".op-weight");
				if ($weight.length) $weight.val(rows[rule]);
			}
		});
		// A group with none of its members selected falls back to its "None" radio.
		this.$body.find(".op-group").each((_, groupEl) => {
			const $group = $(groupEl);
			if (!$group.find(".op-toggle-group:checked").length) {
				$group.find(`.op-toggle-group[value="${NONE_VALUE}"]`).prop("checked", true);
			}
		});
	}

	// ── populate from an existing run ────────────────────────────────────────

	populate_from_run() {
		const run = this.populate_field.get_value();
		if (!run) return;
		frappe
			.call({ method: "autoshift.optimizer_studio.prefill_from_run", args: { run } })
			.then(({ message }) => {
				if (!message) return;
				if (message.mode) this.mode_field.set_value(message.mode);
				if (message.date) this.date_field.set_value(message.date);
				this.leaves = new Set(message.leaves_speculations || []);
				this.render_leave_pills();
				this.set_selection(message.rows || {});
			});
	}

	// ── preview ───────────────────────────────────────────────────────────────

	preview() {
		const mode = this.mode_field.get_value();
		const date = this.date_field.get_value();
		if (!mode || !date) {
			frappe.msgprint(__("Set Planning Mode and Start Date first."));
			return;
		}
		const rows = this.get_selection();
		if (!Object.keys(rows).length) {
			frappe.msgprint(__("Toggle on at least one rule."));
			return;
		}

		frappe
			.call({
				method: "autoshift.optimizer_studio.preview",
				args: {
					mode,
					date,
					rows: JSON.stringify(rows),
					leaves_speculations: JSON.stringify(Array.from(this.leaves)),
				},
				freeze: true,
				freeze_message: __("Solving…"),
			})
			.then(({ message }) => {
				if (!message) return;
				this.last_ruleset = message.ruleset;
				this.handle_run_update(message);
			});
	}

	handle_run_update(message) {
		if (message.status === "Solving") {
			frappe.show_alert({
				message: __("Large problem — continuing in the background…"),
				indicator: "blue",
			});
			setTimeout(() => this.poll_run(message.run), 5000);
			return;
		}
		this.render_result(message);
	}

	poll_run(run) {
		frappe
			.call({ method: "autoshift.optimizer_studio.get_run_status", args: { run } })
			.then(({ message }) => {
				if (!message) return;
				if (message.status === "Solving") {
					setTimeout(() => this.poll_run(run), 5000);
					return;
				}
				this.render_result({ run, ...message });
			});
	}

	render_result(message) {
		const $result = this.$body.find(".op-result");

		if (message.status !== "Solved") {
			let html = `<div style="margin-top:1rem;">
				<div class="text-muted" style="margin-bottom:0.5rem;">${__("Run {0}: {1}", [
					frappe.utils.escape_html(message.run),
					frappe.utils.escape_html(message.status),
				])}</div>`;
			if (message.solver_log) {
				html += `<pre style="background:var(--bg-light);border:1px solid var(--border-color);border-radius:var(--border-radius);padding:0.75rem;font-size:var(--text-xs);overflow-x:auto;max-height:20rem;overflow-y:auto;">${frappe.utils.escape_html(
					message.solver_log
				)}</pre>`;
			}
			html += `</div>`;
			$result.html(html);
			return;
		}

		$result.html(`
			<div class="op-result-header">
				<div>${__("Objective")}: ${frappe.utils.escape_html(String(message.objective_value))}</div>
				<button class="btn btn-default btn-xs op-open-run">${__("Open Run")}</button>
				<button class="btn btn-default btn-xs op-save-ruleset">${__("Save Ruleset As…")}</button>
			</div>
			<div class="op-grid"></div>
		`);
		$result.find(".op-open-run").on("click", () => {
			frappe.set_route("Form", "Optimizer Run", message.run);
		});
		$result.find(".op-save-ruleset").on("click", () => this.save_ruleset_as());

		frappe.require("/assets/autoshift/js/schedule_grid.js", () => {
			autoshift.schedule_grid.render($result.find(".op-grid"), () => message.schedule);
		});
	}

	save_ruleset_as() {
		if (!this.last_ruleset) return;
		frappe.prompt(
			[
				{
					fieldname: "new_name",
					fieldtype: "Data",
					label: __("Ruleset Name"),
					reqd: 1,
				},
			],
			({ new_name }) => {
				frappe
					.call({
						method: "autoshift.optimizer_studio.save_ruleset_as",
						args: { ruleset: this.last_ruleset, new_name },
					})
					.then(() => {
						frappe.show_alert({
							message: __("Saved as {0}", [new_name]),
							indicator: "green",
						});
					});
			},
			__("Save Ruleset As"),
			__("Save")
		);
	}
};
