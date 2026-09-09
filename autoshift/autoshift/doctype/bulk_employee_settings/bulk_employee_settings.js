// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately. Doctype scripts are read as raw text into the
// meta's `__js` and eval'd on the client (frappe/desk/form/meta.py), not bundled — a
// top-level ESM import is a syntax error that silently kills the whole file, taking
// every form handler with it. `hrms` is a global, published by frappe.provide("hrms")
// in the hrms bundle; hrms's own bulk tools reference it exactly this way.

frappe.ui.form.on("Bulk Employee Settings", {
	setup(frm) {
		hrms.setup_employee_filter_group(frm);
	},

	refresh(frm) {
		frm.disable_save();
		frm.trigger("render_help");

		frm.page.set_primary_action(__("Create Settings"), () => frm.trigger("create_settings"));
		frm.add_custom_button(__("Assign Scheduling Role"), () => frm.trigger("assign_role"));

		frm.trigger("get_employees");

		frm.events.on_bulk_result(
			frm,
			"completed_bulk_employee_settings_creation",
			"Employee Settings"
		);
		frm.events.on_bulk_result(
			frm,
			"completed_bulk_scheduling_role_assignment",
			"Employee Scheduling Role"
		);
	},

	// hrms.notify_bulk_action_status only knows success and failure, and renders an
	// empty dialog when handed neither. Both actions here skip employees that already
	// have the record, so a run can legitimately produce only skips.
	report_bulk_result(frm, message, doctype) {
		const success = message.success || [];
		const failure = message.failure || [];
		const skipped = message.skipped || [];

		if (success.length || failure.length) {
			hrms.notify_bulk_action_status(doctype, failure, success);
			if (skipped.length) {
				frappe.show_alert(
					{
						message: __("{0} employee(s) already had {1}; skipped.", [
							skipped.length,
							doctype,
						]),
						indicator: "blue",
					},
					7
				);
			}
		} else if (skipped.length) {
			frappe.msgprint({
				title: __("Nothing to do"),
				indicator: "blue",
				message: __("All {0} selected employee(s) already have {1}.", [
					skipped.length,
					doctype,
				]),
			});
		}

		frm.refresh();
	},

	// Realtime only carries the queued path's result. An inline run reports through
	// its own response instead (see handle_bulk_response) — this bench's socketio is
	// not guaranteed to be up, and a silent success is indistinguishable from the
	// action never having run.
	on_bulk_result(frm, event, doctype) {
		frappe.realtime.off(event);
		frappe.realtime.on(event, (message) =>
			frm.events.report_bulk_result(frm, message, doctype)
		);
	},

	handle_bulk_response(frm, message, doctype) {
		if (!message) return;
		if (message.queued) {
			frappe.show_alert(
				{
					message: __("Queued for {0} employee(s); this may take a few minutes.", [
						message.count,
					]),
					indicator: "blue",
				},
				7
			);
			return;
		}
		frm.events.report_bulk_result(frm, message, doctype);
	},

	render_help(frm) {
		const lines = [
			__("Both actions run on the employees checked below."),
			__("<b>Create Settings</b> applies the preference template."),
			__(
				"<b>Assign Scheduling Role</b> grants the role — without one, an employee is never scheduled."
			),
			__("Employees who already have the record are skipped, never overwritten."),
		];
		frm.get_field("template_html").$wrapper.html(
			`<div class="form-message blue">${lines.join(" ")}</div>`
		);
	},

	company: (frm) => frm.trigger("get_employees"),
	discipline: (frm) => frm.trigger("get_employees"),
	holds_role: (frm) => frm.trigger("get_employees"),
	coverage: (frm) => frm.trigger("get_employees"),

	get_employees(frm) {
		if (!frm.doc.company) {
			return frm.events.render_datatable(frm, []);
		}
		frm.call({
			method: "get_employees",
			doc: frm.doc,
			args: { advanced_filters: frm.advanced_filters || [] },
		}).then((r) => frm.events.render_datatable(frm, r.message || []));
	},

	render_datatable(frm, employees) {
		frm.checked_rows_indexes = [];
		const columns = [
			{ name: "employee", id: "employee", content: __("Employee"), width: 110 },
			{ name: "employee_name", id: "employee_name", content: __("Name"), width: 200 },
			{ name: "roles", id: "roles", content: __("Scheduling Roles"), width: 280 },
			{ name: "has_settings", id: "has_settings", content: __("Has Settings"), width: 110 },
		].map((c) => ({
			...c,
			dropdown: false,
			align: "left",
			editable: false,
			focusable: false,
		}));

		const no_data_message = frm.doc.company
			? __("No active employees match these filters.")
			: __("Please select a Company.");

		hrms.render_employees_datatable(frm, columns, employees, no_data_message, null, {
			onCheckRow() {
				frm.checked_rows_indexes = frm.employees_datatable.rowmanager.getCheckedRows();
			},
		});
	},

	// Checked rows, as [{ employee }] — the shape both bulk methods expect. Throws if
	// nothing is checked, so callers can use the result directly.
	get_selected_employees(frm) {
		const checked = frm.checked_rows_indexes || [];
		if (!checked.length) {
			frappe.throw({
				message: __("Please select at least one employee."),
				title: __("No Employees Selected"),
			});
		}

		const rows = frm.employees_datatable.getRows();
		return checked.map((idx) => {
			const emp = {};
			rows[idx].forEach((cell) => {
				if (cell.column.name === "employee") emp["employee"] = cell.content;
			});
			return emp;
		});
	},

	create_settings(frm) {
		const selected = frm.events.get_selected_employees(frm);

		frappe.confirm(
			__("Create Employee Settings for {0} employee(s)?", [selected.length]),
			() => {
				frm.call({
					method: "bulk_create_settings",
					doc: frm.doc,
					args: {
						employees: selected,
						favourite_shift: frm.doc.favourite_shift || null,
						shift_preferences: frm.doc.shift_preferences || [],
						preferred_branch: frm.doc.preferred_branch || [],
					},
					freeze: true,
					freeze_message: __("Creating Employee Settings..."),
				}).then((r) =>
					frm.events.handle_bulk_response(frm, r.message, "Employee Settings")
				);
			}
		);
	},

	assign_role(frm) {
		if (!frm.doc.assign_role) {
			frappe.throw({
				message: __("Please choose a Scheduling Role in the assignment section above."),
				title: __("No Role Selected"),
			});
		}
		const selected = frm.events.get_selected_employees(frm);

		frappe.confirm(
			__("Assign {0} to {1} employee(s)?", [
				frappe.utils.escape_html(frm.doc.assign_role),
				selected.length,
			]),
			() => {
				frm.call({
					method: "bulk_assign_role",
					doc: frm.doc,
					args: {
						employees: selected,
						scheduling_role: frm.doc.assign_role,
						role_fte: frm.doc.role_fte || null,
						max_rooms: frm.doc.role_max_rooms || null,
						valid_from: frm.doc.role_valid_from || null,
						valid_to: frm.doc.role_valid_to || null,
					},
					freeze: true,
					freeze_message: __("Assigning Scheduling Roles..."),
				}).then((r) =>
					frm.events.handle_bulk_response(frm, r.message, "Employee Scheduling Role")
				);
			}
		);
	},
});
