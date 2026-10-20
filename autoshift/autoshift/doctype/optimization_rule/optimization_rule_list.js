// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.listview_settings["Optimization Rule"] = {
	onload(listview) {
		// Same gate as implementing a rule at all (see optimization_rule.py); the server
		// enforces it too, this just keeps the action off a list a non-developer can't use.
		if (!frappe.user_roles.includes("System Manager")) return;

		listview.page.add_menu_item(__("Reload Built-in Rules"), () => {
			frappe
				.call({
					method: "autoshift.autoshift.doctype.optimization_rule.optimization_rule.reload_builtin_rules",
					freeze: true,
					freeze_message: __("Reloading…"),
				})
				.then(() => {
					frappe.show_alert({
						message: __("Built-in rules reloaded from code."),
						indicator: "green",
					});
					listview.refresh();
				});
		});
	},
};
