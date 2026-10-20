// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// Settled weeks, from the browser's side: the pre-solve warning about unconfirmed rotas
// (Optimizer Run form, Optimizer Studio) and the wall chart's explicit "Create them".
// A solve never creates records — it reads the rotas directly.
//
// The server half is `autoshift/rota/` — see its docstring for why HRMS is not doing
// this itself, and why everything here is a workaround with a shelf life.
//
// NOTE: no `import`/`export` here, deliberately — see bulk_employee_settings.js for why
// doctype/page scripts on this app stay plain scripts. Loaded via frappe.require() and
// reached through the namespace below.

frappe.provide("autoshift.rota");

/**
 * The paragraph a pre-solve confirm carries when the horizon binds people to rotas the
 * import inferred and nobody has confirmed yet. A warning only: the solve still runs on
 * them, but the planner should know the week being frozen is the old agenda's reading.
 * Each discipline links to the Rota Editor on the horizon's first week, in a new tab so
 * the dialog (and the run) stay where they are.
 */
autoshift.rota.unconfirmed_note = function (unconfirmed) {
	if (!unconfirmed || !unconfirmed.employees) return "";
	const links = unconfirmed.disciplines
		.map((row) => {
			const params = new URLSearchParams({ discipline: row.discipline });
			if (unconfirmed.editor_start) params.set("start", unconfirmed.editor_start);
			return `<a href="/app/rota-editor?${params}" target="_blank">${frappe.utils.escape_html(
				row.discipline
			)}</a> (${row.employees})`;
		})
		.join(", ");
	return `<p class="text-warning">${__(
		"{0} employee(s) are bound to imported rotas nobody has confirmed yet ({1} pattern(s)). Their week will be frozen as the old agenda reads it. Review in the Rota Editor: {2}",
		[unconfirmed.employees, unconfirmed.patterns, links]
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
 * A thunk rather than a method name so the caller owns how it addresses the server. Named
 * `create` rather than `materialize` so it does not read like the server module of that
 * name, which is what those thunks actually call.
 */
autoshift.rota.create = function (call) {
	return Promise.resolve(call()).then((response) =>
		autoshift.rota.report_failures(response && response.message)
	);
};
