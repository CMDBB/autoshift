// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

frappe.ui.form.on("Optimizer Run", {
	refresh(frm) {
		frm.disable_save();

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Solve"), () => {
				frappe.confirm(
					__("Start the optimizer? This may take several minutes."),
					() => {
						frm.call("enqueue_solve").then(() => {
							frm.reload_doc();
							frappe.show_alert({ message: __("Solve job enqueued"), indicator: "blue" });
						});
					}
				);
			}).addClass("btn-primary");
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
					__(
						"Create Shift Assignment records for all slots? This cannot be undone."
					),
					() => {
						frm.call("commit").then(() => frm.reload_doc());
					}
				);
			}).addClass("btn-danger");
		}

		// poll while solving
		if (frm.doc.status === "Solving") {
			setTimeout(() => frm.reload_doc(), 5000);
		}
	},
});
