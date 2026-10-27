// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.ui.form.on("Horizon Scan", {
	refresh: function (frm) {
		frm.add_custom_button(__("Horizon Scanner"), () => {
			frappe.set_route("horizon-scanner", { scan: frm.doc.name });
		});
		render_calendar(frm);
	},
});

// The same calendars the Horizon Scanner page draws, off the same payload — the scan's
// own form is where somebody lands from the list view, and a scan without its calendar is
// a table of window rows.
function render_calendar(frm) {
	const $wrapper = frm.get_field("scan_view_html").$wrapper;
	if (frm.is_new()) {
		$wrapper.html(
			`<div class="text-muted">${__(
				"Save the scan to plan its windows, then scan it."
			)}</div>`
		);
		return;
	}
	frappe.require("/assets/autoshift/js/horizon_calendar.js", () => {
		autoshift.horizon_calendar.render($wrapper, { scan: frm.doc.name });
	});
}
