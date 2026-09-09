// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// Creating the Shift Assignments a settled week implies, from the browser's side.
// Shared by the Optimizer Run form, Optimizer Studio and the wall chart, all three of
// which have to say the same thing about the same records.
//
// The server half is `autoshift/rota/` — see its docstring for why HRMS is not doing
// this itself, and why everything here is a workaround with a shelf life.
//
// NOTE: no `import`/`export` here, deliberately — see bulk_employee_settings.js for why
// doctype/page scripts on this app stay plain scripts. Loaded via frappe.require() and
// reached through the namespace below.

frappe.provide("autoshift.rota");

/**
 * The paragraph a pre-solve confirm carries when a settled week has no records yet.
 *
 * Phrased as a statement rather than a question: creating them is not optional, because
 * binding freezes those people against exactly these records and a horizon without them
 * would freeze them to an empty week.
 */
autoshift.rota.pending_note = function (pending) {
	if (!pending || !pending.count) return "";
	return `<p>${__(
		"{0} settled shift(s) for {1} practitioner(s) fall in this horizon per their Shift Schedule but have no Shift Assignment yet. They will be created before solving.",
		[pending.count, pending.employees]
	)}</p>`;
};

/**
 * Report the rows HRMS refused. One bad record never blocks the rest, so there is
 * usually nothing to say — but when there is, it names the day rather than a count.
 */
autoshift.rota.report_failures = function (made) {
	if (!made || !(made.failed || []).length) return made;
	frappe.msgprint({
		title: __("Some settled shifts could not be created"),
		indicator: "orange",
		message: made.failed
			.map((row) =>
				frappe.utils.escape_html(
					`${row.employee_name || row.employee} ${row.date} ${row.shift_type}: ${
						row.reason
					}`
				)
			)
			.join("<br>"),
	});
	return made;
};

/**
 * Run `call` (a thunk returning frappe's `{message}` promise) and report any refusals.
 *
 * A thunk rather than a method name because the three surfaces address this differently:
 * a document method, a studio helper, and a span-addressed whitelisted call. Named
 * `create` rather than `materialize` so it does not read like the server module of that
 * name, which is what those thunks actually call.
 */
autoshift.rota.create = function (call) {
	return Promise.resolve(call()).then((response) =>
		autoshift.rota.report_failures(response && response.message)
	);
};
