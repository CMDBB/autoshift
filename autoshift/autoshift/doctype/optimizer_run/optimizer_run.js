// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see bulk_employee_settings.js. The schedule
// grid rendering itself lives in public/js/schedule_grid.js, shared with Optimizer
// Studio, and is loaded on demand via frappe.require().

function render_schedule_grid(frm) {
	const field = frm.fields_dict.schedule_view_html;
	if (!field) return;

	let $wrapper = field.$wrapper.find(".autoshift-schedule-grid");
	if (!$wrapper.length) {
		$wrapper = $("<div></div>").appendTo(field.$wrapper);
	}

	// Only solved-and-later runs have a schedule worth visualizing.
	if (!["Solved", "Approved", "Committed"].includes(frm.doc.status)) {
		$wrapper.empty();
		return;
	}

	frappe.require("/assets/autoshift/js/schedule_grid.js", () => {
		autoshift.schedule_grid.render($wrapper, () =>
			frm.call("get_schedule_events").then((r) => r.message)
		);
	});
}

frappe.ui.form.on("Optimizer Run", {
	refresh(frm) {
		render_schedule_grid(frm);

		if (frm.doc.status === "Failed") {
			frm.disable_save();
		}

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Solve"), () => {
				frm.call("check_duplicates").then(
					({ message: { n: cache_hits_n, cached_runs_list_link: link } }) => {
						frappe.confirm(
							cache_hits_n == 0
								? __(
										"Run the optimizer? Large problems that don't finish quickly will automatically continue in the background."
								  )
								: __(
										"Identical run detected: {0} {1} already solved this exact input. Run the optimizer anyway?",
										[cache_hits_n, link]
								  ),
							() => {
								frm.call("solve").then((r) => {
									frm.reload_doc();
								});
							}
						);
					}
				);
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Solving") {
			// Poll until the (background) job completes
			setTimeout(() => frm.reload_doc(), 5000);
		}

		if (frm.doc.status === "Solved") {
			frm.add_custom_button(__("Approve"), () => {
				frappe.confirm(
					__("Approve this schedule? No changes can be made after approval."),
					() => {
						frm.call("approve").then(() => frm.reload_doc());
					}
				);
			}).addClass("btn-success");
		}

		if (frm.doc.status === "Approved") {
			frm.add_custom_button(__("Commit"), () => {
				frappe.confirm(
					__("Create Shift Assignment records for all slots? This cannot be undone."),
					() => {
						frm.call("commit").then(() => frm.reload_doc());
					}
				);
			}).addClass("btn-danger");
		}

		// Runs are immutable once solving starts: re-trying, restarting, or
		// abandoning a stuck Solving run is done via a duplicate Draft
		if (["Solving", "Solved", "Failed", "Approved", "Committed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Re-run (New Copy)"), () => {
				frappe.confirm(
					__(
						"Create a new Draft run with the same configuration? This run is left untouched."
					),
					() => {
						frm.call("duplicate").then((r) => {
							frappe.set_route("Form", "Optimizer Run", r.message);
						});
					}
				);
			}).addClass(frm.doc.status === "Failed" ? "btn-primary" : "");
		}
	},
});
