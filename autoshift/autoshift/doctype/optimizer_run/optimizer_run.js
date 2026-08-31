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

function render_run_stats(frm) {
	const field = frm.fields_dict.stats_html;
	if (!field) return;

	let $wrapper = field.$wrapper.find(".autoshift-run-stats");
	if (!$wrapper.length) {
		$wrapper = $("<div></div>").appendTo(field.$wrapper);
	}

	if (!["Solved", "Approved", "Committed"].includes(frm.doc.status)) {
		$wrapper.empty();
		return;
	}

	frappe.require("/assets/autoshift/js/run_stats.js", () => {
		autoshift.run_stats.render($wrapper, () =>
			frm.call("get_run_statistics").then((r) => r.message)
		);
	});
}

frappe.ui.form.on("Optimizer Run", {
	refresh(frm) {
		render_run_stats(frm);
		render_schedule_grid(frm);

		if (frm.doc.status === "Failed") {
			frm.disable_save();
		}

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Solve"), () => {
				Promise.all([
					frm.call("check_duplicates"),
					frm.call("check_binding_rule_gap"),
				]).then(([{ message: duplicates }, { message: binding }]) => {
					const { n: cache_hits_n, cached_runs_list_link: link } = duplicates;
					let msg =
						cache_hits_n == 0
							? __(
									"Run the optimizer? Large problems that don't finish quickly will automatically continue in the background."
							  )
							: __(
									"Identical run detected: {0} {1} already solved this exact input. Run the optimizer anyway?",
									[cache_hits_n, link]
							  );
					// A settled schedule that no rule enforces is silently overwritten, and
					// nothing downstream would show that — so say it before solving, not after.
					if (binding && binding.gap) {
						msg =
							`<p class="text-warning">${__(
								"{0} employee(s) hold a Scheduling Role whose assignments are binding ({1}), but this ruleset does not include the <b>Bind settled schedules</b> rule. Their settled schedules will be ignored and re-planned from scratch.",
								[
									binding.employees,
									frappe.utils.escape_html(binding.roles.join(", ")),
								]
							)}</p>` + `<p>${msg}</p>`;
					}
					frappe.confirm(msg, () => {
						frm.call("solve").then(() => {
							frm.reload_doc();
						});
					});
				});
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
