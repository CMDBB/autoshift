// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.listview_settings["Optimizer Run"] = {
	// Automatic runs (created by future automated tools, not by a user) are
	// noise in the default view; they're still reachable by clearing the filter.
	// A Horizon Scan's runs are the same noise in bulk — one per scanned window, dozens
	// per scan — and are reached from the scan's own calendar instead.
	onload: function (listview) {
		frappe.route_options = {
			type: ["not in", ["Automatic", "Scan", "Test"]],
			status: ["not in", ["Approved"]],
		};
	},
};
