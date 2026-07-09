// Copyright (c) 2026, CMDBB and contributors
// For license information, please see license.txt

// Editor support for the Implementation Code field (Custom Code rules):
// - domain completions (ctx.*, ctx.data.*, pulp.*, itertools.*) served by
//   optimization_rule.get_code_completions, fed to Frappe's built-in Code
//   control completion hook (the `autocompletions` field property);
// - inline lint squiggles via ace-linters + Ruff (WASM, runs in a webworker;
//   assets self-hosted from this app's node_modules — see package.json).

const LINT_ASSETS = "/assets/autoshift/node_modules";

frappe.ui.form.on("Optimization Rule", {
	refresh(frm) {
		frm.trigger("setup_code_editor");
	},
	implementation_type(frm) {
		frm.trigger("setup_code_editor");
	},
	setup_code_editor(frm) {
		if (frm.doc.implementation_type !== "Custom Code") return;
		with_code_editor(frm, (field) => {
			setup_completions(frm);
			setup_linter(field);
		});
	},
});

function with_code_editor(frm, callback, attempt = 0) {
	// The Ace instance is created asynchronously (external lib) and only once
	// the depends_on makes the field visible — poll briefly instead of racing it.
	const field = frm.get_field("implementation_code");
	if (field && field.editor) return callback(field);
	if (attempt > 50) return;
	setTimeout(() => with_code_editor(frm, callback, attempt + 1), 200);
}

function setup_completions(frm) {
	if (frm._code_completions_requested) return;
	frm._code_completions_requested = true;
	frappe
		.call(
			"autoshift.autoshift.doctype.optimization_rule.optimization_rule.get_code_completions"
		)
		.then((r) => {
			frm.set_df_property("implementation_code", "autocompletions", r.message || []);
		});
}

function setup_linter(field) {
	if (field._autoshift_linter) return;
	field._autoshift_linter = true;
	frappe.require(`${LINT_ASSETS}/ace-linters/build/ace-linters.js`, () => {
		// The provider spawns a Blob webworker whose importScripts() cannot
		// resolve path-absolute URLs, so every URL must include the origin.
		const base = window.location.origin + LINT_ASSETS;
		const provider = window.LanguageProvider.fromCdn(
			{
				services: [
					{
						name: "python",
						script: "python-service.js",
						className: "PythonService",
						modes: "python",
						cdnUrl: `${base}/ace-python-ruff-linter/build`,
					},
				],
				serviceManagerCdn: `${base}/ace-linters/build`,
				includeDefaultLinters: false,
			},
			{
				// diagnostics only — in particular the provider's completion
				// functionality would overwrite the Frappe completer set up above
				functionality: {
					hover: false,
					completion: false,
					completionResolve: false,
					format: false,
					documentHighlights: false,
					signatureHelp: false,
					semanticTokens: false,
					codeActions: false,
					inlineCompletion: false,
				},
			}
		);
		// Ruff must not flag the globals compile_custom_rule() injects (F821)
		provider.setGlobalOptions("python", {
			configuration: { builtins: ["pulp", "itertools", "cname"] },
		});
		provider.registerEditor(field.editor);
	});
}
