// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// The forward scan, as calendars: every week from here to a date ahead solved on its own,
// each day coloured by what its window came out as. The page is a shell — the calendars,
// the controls and the wall chart underneath them all live in
// `public/js/horizon_calendar.js`, shared with the Horizon Scan form's Calendar tab.
//
// NOTE: no `import` here, deliberately — see bulk_employee_settings.js for why. Page
// scripts on this app stay plain scripts, loaded once per Desk session.

frappe.provide("autoshift");

frappe.pages["horizon-scanner"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Horizon Scanner"),
		single_column: true,
	});
	wrapper.$container = $('<div class="horizon-scanner"></div>').appendTo($(page.main));
	page.add_menu_item(__("All Scans"), () => frappe.set_route("List", "Horizon Scan"));
	page.set_primary_action(__("Refresh"), () => show(wrapper), "refresh");
	show(wrapper);
};

// Loaded once per Desk session, so a `?scan=` arriving later (from the list view, or a
// link in a warning) has to be applied on every show rather than only on load.
frappe.pages["horizon-scanner"].on_page_show = function (wrapper) {
	show(wrapper);
};

function show(wrapper) {
	const options = { picker: true };
	const route_options = frappe.route_options || {};
	if (route_options.scan) {
		options.scan = route_options.scan;
		// Stripped so a reload does not keep forcing the same scan open — the page
		// otherwise remembers whichever one the reader last picked.
		delete frappe.route_options.scan;
	}
	frappe.require("/assets/autoshift/js/horizon_calendar.js", () => {
		autoshift.horizon_calendar.render(wrapper.$container, options);
	});
}
