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
		.optimizer-studio .op-topic {
			border-top: 1px solid var(--border-color); padding: 0.35rem 0;
		}
		.optimizer-studio .op-topic:first-child { border-top: none; }
		.optimizer-studio .op-topic-summary {
			cursor: pointer; font-weight: 500; padding: 0.35rem 0; list-style-position: outside;
		}
		.optimizer-studio .op-topic-body { padding-left: 0.25rem; }
		/* the first title inside an open section sits right under its summary */
		.optimizer-studio .op-topic-body .op-section-title:first-child { margin-top: 0.35rem; }
		.optimizer-studio .op-group {
			border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
			padding: 0.6rem 0.75rem; margin-bottom: 0.6rem;
		}
		.optimizer-studio .op-group-label { font-weight: 500; margin-bottom: 0.3rem; }
		.optimizer-studio .op-row { padding: 0.3rem 0; }
		/* a rule drawn inside the rule it requires — the parent acts as its fieldset */
		.optimizer-studio .op-children {
			margin-left: 0.6rem; padding-left: 0.75rem;
			border-left: 2px solid var(--border-color);
		}
		.optimizer-studio .op-row-parent > .op-check-label > span { font-weight: 500; }
		.optimizer-studio .op-row-blocked > .op-check-label,
		.optimizer-studio .op-row-blocked > .op-radio-label { opacity: 0.45; cursor: not-allowed; }
		.optimizer-studio .op-row-blocked > .op-desc { opacity: 0.45; }
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
				// delegated, so it survives the catalog being re-rendered
				this.$body
					.find(".op-catalog")
					.off("change.opdeps")
					.on("change.opdeps", ".op-toggle, .op-toggle-group", () =>
						this.sync_dependencies()
					);
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

	// ── dependency forest ─────────────────────────────────────────────────────
	// `requires` is turned into nesting: a rule that requires another is drawn inside it,
	// and its checkbox is disabled until the parent is on. Together with the group radios
	// and the `excludes` handling in sync_dependencies(), that makes every ruleset the
	// panel can express one that passes BuiltinRule.check_ruleset — the point is that a
	// failing ruleset should be unreachable, not merely rejected later.
	index_catalog() {
		this.by_name = new Map(this.catalog.map((r) => [r.name, r]));
		this.parent_of = new Map();
		this.children_of = new Map();
		for (const rule of this.catalog) {
			// A grouped rule stays where its group is: a choice set whose members have
			// different requirements (as `existing_assignments` does) cannot be split
			// across nesting levels without breaking its radios. Its dependency is still
			// enforced dynamically, just not drawn.
			if (rule.group) continue;
			const parent = (rule.requires || []).filter((n) => this.by_name.has(n)).sort()[0];
			if (!parent || parent === rule.name) continue;
			this.parent_of.set(rule.name, parent);
			if (!this.children_of.has(parent)) this.children_of.set(parent, []);
			this.children_of.get(parent).push(rule);
		}
		// Guard against a requires-cycle drawing an infinite tree: drop the back edge.
		for (const name of [...this.parent_of.keys()]) {
			const seen = new Set([name]);
			let cur = this.parent_of.get(name);
			while (cur) {
				if (seen.has(cur)) {
					this.children_of.set(
						this.parent_of.get(name),
						(this.children_of.get(this.parent_of.get(name)) || []).filter(
							(r) => r.name !== name
						)
					);
					this.parent_of.delete(name);
					break;
				}
				seen.add(cur);
				cur = this.parent_of.get(cur);
			}
		}
	}

	ancestors_of(name) {
		const chain = [];
		let cur = this.parent_of.get(name);
		while (cur) {
			chain.push(cur);
			cur = this.parent_of.get(cur);
		}
		return chain;
	}

	build_catalog_html() {
		this.index_catalog();
		// Bucket by topic first so the panel reads as a handful of collapsible sections
		// rather than one flat list. Only top-level rules are bucketed — a child follows
		// its parent into whichever section the parent landed in, because the dependency
		// is the more useful thing to see.
		const topics = new Map();
		for (const rule of this.catalog) {
			if (this.parent_of.has(rule.name)) continue;
			const key = rule.topic || "";
			if (!topics.has(key)) {
				topics.set(key, { topic: rule.topic, order: rule.topic_order || 0, rules: [] });
			}
			topics.get(key).rules.push(rule);
		}
		// untopiced rules last, then by display order, then by name
		const sections = [...topics.values()].sort(
			(a, b) =>
				Number(!a.topic) - Number(!b.topic) ||
				a.order - b.order ||
				(a.topic || "").localeCompare(b.topic || "")
		);

		if (!this.catalog.length) {
			return `<div class="text-muted">${__("No implemented rules found.")}</div>`;
		}
		if (sections.length === 1 && !sections[0].topic) {
			// nothing is filed anywhere — no point wrapping the whole list in one section
			return this.build_rules_html(sections[0].rules);
		}
		return sections
			.map(
				(section) => `
				<details class="op-topic" open>
					<summary class="op-topic-summary">${frappe.utils.escape_html(
						section.topic || __("Other")
					)}</summary>
					<div class="op-topic-body">${this.build_rules_html(section.rules)}</div>
				</details>`
			)
			.join("");
	}

	build_rules_html(rules) {
		const groups = {};
		const standalone = [];
		for (const rule of rules) {
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
			const kind_rules = standalone.filter((r) => r.kind === kind);
			if (!kind_rules.length) continue;
			html += `<div class="op-section-title">${__(kind)}</div>`;
			html += kind_rules.map((r) => this.render_toggle_row(r)).join("");
		}
		return html;
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
		const children = this.children_of.get(rule.name) || [];
		const nested = children.length
			? `<div class="op-children">${children
					.map((child) => this.render_toggle_row(child))
					.join("")}</div>`
			: "";
		return `
			<div class="op-row${children.length ? " op-row-parent" : ""}"
				data-rule="${frappe.utils.escape_html(rule.name)}">
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
				${nested}
			</div>`;
	}

	// Reads directly off the DOM (`.op-row[data-rule]` pairs a toggle with its optional
	// weight input) rather than keying lookups by rule name — rule titles are free text
	// and not safe to interpolate into a CSS attribute selector.
	//
	// Rows nest (see index_catalog), so every lookup here is scoped to the row's OWN
	// controls: a plain `.find()` would reach into a child row's toggle and report the
	// parent as selected whenever any descendant was.
	own_toggle($row) {
		return $row
			.children(".op-check-label, .op-radio-label")
			.find(".op-toggle, .op-toggle-group");
	}

	own_weight($row) {
		return $row.children(".op-weight");
	}

	get_selection() {
		const rows = {};
		this.$body.find(".op-catalog .op-row").each((_, rowEl) => {
			const $row = $(rowEl);
			const rule = $row.data("rule");
			if (!rule || rule === NONE_VALUE) return;
			if (!this.own_toggle($row).is(":checked")) return;
			const $weight = this.own_weight($row);
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
			this.own_toggle($row).prop("checked", rule === NONE_VALUE ? false : has);
			if (has) {
				const $weight = this.own_weight($row);
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
		this.sync_dependencies();
	}

	// ── keeping the selection valid by construction ───────────────────────────
	// Enforces the two dependency-graph rules the solver would otherwise throw on:
	// a rule may not be on unless everything it `requires` is on, and may not be on
	// while something that `excludes` it is. (The third — at most one member of a
	// choice group — is already structural, the group renders as radios.) Anything
	// blocked is unchecked, disabled, and given a tooltip saying which rule blocked it.
	title_of(name) {
		return (this.by_name.get(name) || {}).title || name;
	}

	// Pure half: given the set of rules currently on, which rules must not be on, and why.
	// Kept free of the DOM so the "no expressible selection can fail check_ruleset"
	// invariant is testable on its own.
	blocked_reasons(checked) {
		const forbidden = new Map();
		for (const name of checked) {
			for (const other of (this.by_name.get(name) || {}).excludes || []) {
				if (!forbidden.has(other)) forbidden.set(other, name);
			}
		}
		const reasons = new Map();
		for (const rule of this.catalog) {
			// An unmet requirement — drawn as nesting for standalone rules, and still
			// enforced for grouped ones, whose members cannot be nested (see index_catalog).
			const missing = (rule.requires || []).filter(
				(req) => this.by_name.has(req) && !checked.has(req)
			);
			if (missing.length) {
				reasons.set(
					rule.name,
					__("Requires {0}", [missing.map((n) => this.title_of(n)).join(", ")])
				);
			} else if (forbidden.has(rule.name)) {
				reasons.set(
					rule.name,
					__("Incompatible with {0}", [this.title_of(forbidden.get(rule.name))])
				);
			}
		}
		return reasons;
	}

	// DOM half: read what is on, apply blocked_reasons, repeat until it settles
	// (unchecking a parent can orphan a grandchild).
	sync_dependencies() {
		const $rows = this.$body.find(".op-catalog .op-row");
		const checked = new Set();
		$rows.each((_, rowEl) => {
			const $row = $(rowEl);
			const rule = $row.data("rule");
			if (rule && rule !== NONE_VALUE && this.own_toggle($row).is(":checked")) {
				checked.add(rule);
			}
		});
		const reasons = this.blocked_reasons(checked);

		let changed = false;
		$rows.each((_, rowEl) => {
			const $row = $(rowEl);
			const rule = $row.data("rule");
			if (!rule || rule === NONE_VALUE) return;
			const $toggle = this.own_toggle($row);
			const blocked_by = reasons.get(rule) || null;

			$toggle.prop("disabled", !!blocked_by);
			this.own_weight($row).prop("disabled", !!blocked_by);
			$row.toggleClass("op-row-blocked", !!blocked_by);
			$row.attr("title", blocked_by || "");
			if (blocked_by && $toggle.is(":checked")) {
				$toggle.prop("checked", false);
				changed = true;
			}
		});

		// A group whose selected member just got blocked falls back to "None".
		this.$body.find(".op-group").each((_, groupEl) => {
			const $group = $(groupEl);
			if (!$group.find(".op-toggle-group:checked").length) {
				$group.find(`.op-toggle-group[value="${NONE_VALUE}"]`).prop("checked", true);
			}
		});

		if (changed) this.sync_dependencies();
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

		// A settled schedule that no rule enforces is silently re-planned, and nothing in
		// the resulting grid would show that — so ask before solving, not after.
		frappe
			.call({
				method: "autoshift.optimizer_studio.check_binding_rule_gap",
				args: { rows: JSON.stringify(rows) },
			})
			.then(({ message: binding }) => {
				if (!binding || !binding.gap) return this.run_preview(mode, date, rows);
				frappe.confirm(
					__(
						"{0} employee(s) hold a Scheduling Role whose assignments are binding ({1}), but <b>Bind settled schedules</b> is not selected. Their settled schedules will be ignored and re-planned from scratch. Preview anyway?",
						[binding.employees, frappe.utils.escape_html(binding.roles.join(", "))]
					),
					() => this.run_preview(mode, date, rows)
				);
			});
	}

	run_preview(mode, date, rows) {
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
			<div class="op-stats"></div>
			<div class="op-grid"></div>
		`);
		$result.find(".op-open-run").on("click", () => {
			frappe.set_route("Form", "Optimizer Run", message.run);
		});
		$result.find(".op-save-ruleset").on("click", () => this.save_ruleset_as());

		frappe.require("/assets/autoshift/js/run_stats.js", () => {
			autoshift.run_stats.render($result.find(".op-stats"), () => message.statistics);
		});
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
