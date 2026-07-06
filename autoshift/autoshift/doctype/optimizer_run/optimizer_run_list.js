// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.listview_settings["Optimizer Run"] = {
	// Automatic runs (created by future automated tools, not by a user) are
	// noise in the default view; they're still reachable by clearing the filter.
	onload: function (listview) {
		frappe.route_options = {
			type: ["not in", ["Automatic", "Test"]],
			status: ["not in", ["Approved"]],
		};
	},
};
