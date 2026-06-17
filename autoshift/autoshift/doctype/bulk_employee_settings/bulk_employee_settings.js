// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

import hrms from "hrms";

frappe.ui.form.on("Bulk Employee Settings", {
	setup(frm) {
		hrms.setup_employee_filter_group(frm);
	},

	refresh(frm) {
		frm.disable_save();
		frm.page.set_primary_action(__("Create Settings"), () => frm.trigger("create_settings"));
		frm.trigger("get_employees");
		hrms.handle_realtime_bulk_action_notification(
			frm,
			"completed_bulk_employee_settings_creation",
			"Employee Settings"
		);
	},

	company(frm) {
		frm.trigger("get_employees");
	},
	department(frm) {
		frm.trigger("get_employees");
	},
	designation(frm) {
		frm.trigger("get_employees");
	},

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
			{ name: "employee", id: "employee", content: __("Employee") },
			{ name: "employee_name", id: "employee_name", content: __("Name") },
			{ name: "department", id: "department", content: __("Department") },
			{ name: "designation", id: "designation", content: __("Designation") },
		].map((c) => ({
			...c,
			dropdown: false,
			align: "left",
			editable: false,
			focusable: false,
		}));

		const no_data_message = frm.doc.company
			? __("All active employees already have Employee Settings, or none match the filters.")
			: __("Please select a Company.");

		hrms.render_employees_datatable(frm, columns, employees, no_data_message, null, {
			onCheckRow() {
				frm.checked_rows_indexes = frm.employees_datatable.rowmanager.getCheckedRows();
			},
		});
	},

	create_settings(frm) {
		const checked = frm.checked_rows_indexes || [];
		if (!checked.length) {
			frappe.throw({
				message: __("Please select at least one employee."),
				title: __("No Employees Selected"),
			});
		}

		const rows = frm.employees_datatable.getRows();
		const selected = checked.map((idx) => {
			const emp = {};
			rows[idx].forEach((cell) => {
				if (cell.column.name === "employee") emp["employee"] = cell.content;
			});
			return emp;
		});

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
				});
			}
		);
	},
});
