// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// NOTE: no `import` here, deliberately — see bulk_employee_settings.js. The rendering
// itself lives in public/js/schedule_view.js (the tab shell) and the panes it loads,
// all shared with Optimizer Studio and pulled in on demand via frappe.require().

function render_schedule_view(frm) {
	const field = frm.fields_dict.schedule_view_html;
	if (!field) return;

	let $wrapper = field.$wrapper.find(".autoshift-schedule-view");
	if (!$wrapper.length) {
		$wrapper = $('<div class="autoshift-schedule-view"></div>').appendTo(field.$wrapper);
	}

	// Rendered in every state, unsaved Drafts included. The Week tab reads the
	// Shift Assignments on the books when the run has nothing of its own to show,
	// and a run that failed to solve is precisely the one whose week needs looking
	// at — which the old solved-runs-only guard made impossible.
	frappe.require("/assets/autoshift/js/schedule_view.js", () => {
		autoshift.schedule_view.render($wrapper, {
			run: frm.is_new() ? null : frm.doc.name,
			status: frm.doc.status,
			week: frm.doc.date || null,
			week_chart: (week) =>
				frappe
					.call({
						method: "autoshift.wallchart.api.get_week_chart",
						args: {
							week,
							run: frm.is_new() ? null : frm.doc.name,
							mode: frm.doc.mode || "Bounded",
						},
					})
					.then((r) => r.message),
			statistics: () => frm.call("get_run_statistics").then((r) => r.message),
			schedule: () => frm.call("get_schedule_events").then((r) => r.message),
			solver_log: () => frm.doc.solver_log || "",
		});
	});
}

// Everything a planner has to be told before a solve, in one dialog rather than a stack
// of them: whether this input has already been solved, whether the ruleset is about to
// re-plan somebody whose schedule is not the planner's to set, and whether that person's
// settled week needs writing to the books first (HRMS cannot — see autoshift/rota).
function confirm_and_solve(frm) {
	frappe.require("/assets/autoshift/js/rota.js", () => {
		Promise.all([
			frm.call("check_duplicates"),
			frm.call("check_binding_rule_gap"),
			frm.call("check_pending_bound_shifts"),
		]).then(([{ message: duplicates }, { message: binding }, { message: pending }]) => {
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
						[binding.employees, frappe.utils.escape_html(binding.roles.join(", "))]
					)}</p>` + `<p>${msg}</p>`;
			}
			msg = autoshift.rota.pending_note(pending) + msg;

			frappe.confirm(msg, () => {
				const ready =
					pending && pending.count
						? autoshift.rota.create(() => frm.call("materialize_bound_shifts"))
						: Promise.resolve();
				ready.then(() => frm.call("solve").then(() => frm.reload_doc()));
			});
		});
	});
}

frappe.ui.form.on("Optimizer Run", {
	refresh(frm) {
		render_schedule_view(frm);

		if (frm.doc.status === "Failed") {
			frm.disable_save();
		}

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Solve"), () => confirm_and_solve(frm)).addClass(
				"btn-primary"
			);
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
