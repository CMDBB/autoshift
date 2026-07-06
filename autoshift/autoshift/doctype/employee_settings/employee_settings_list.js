// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.listview_settings["Employee Settings"] = {
	formatters: {
		active(value, df, doc) {
			const checked = cint(value) ? "checked" : "";
			return `<input type="checkbox" ${checked}
				class="employee-settings-active-toggle"
				data-name="${frappe.utils.escape_html(doc.name)}">`;
		},
	},

	onload(listview) {
		// Delegated handler survives list re-renders.
		listview.$result.on("click", ".employee-settings-active-toggle", function (e) {
			// Keep the click from opening/selecting the row.
			e.stopPropagation();
		});

		listview.$result.on("change", ".employee-settings-active-toggle", function () {
			const name = $(this).attr("data-name");
			const active = $(this).is(":checked") ? 1 : 0;
			frappe.db.set_value("Employee Settings", name, "active", active).then(() => {
				frappe.show_alert({ message: __("Active updated"), indicator: "green" }, 3);
			});
		});
	},
};
