import frappe
from frappe.model.document import Document

from autoshift.optimizer.rules import BUILTIN_RULES

# The "developer" role: the only one allowed to implement or validate rules. The
# permlevel-1 lock on the implementation fields mirrors this in the UI; the controller
# checks below are the authoritative guard (they also cover API writes).
DEVELOPER_ROLE = "System Manager"

IMPLEMENTATION_FIELDS = ("implementation_type", "builtin_key", "implementation_code", "rule_kind")


class OptimizationRule(Document):
	def validate(self):
		if self.implementation_type == "Built-in":
			builtin = self._validate_builtin_key()
			# kind is defined by the registered implementation, not the document
			self.rule_kind = builtin.kind.title()
		elif self.implementation_type == "Custom Code":
			self._validate_custom_code()
		self.implemented = 1 if self.is_implemented() else 0

	def validate_higher_perm_levels(self):
		# The framework resets permlevel>0 fields *silently*, and it does so before
		# validate() runs (see Document.save/insert) — so this override is the only
		# point where a non-developer's attempted implementation changes are still
		# visible on the doc. Warn/refuse here, then let the framework reset proceed
		# as a second net.
		if not self._is_developer():
			self._clean_non_developer_changes()
		super().validate_higher_perm_levels()

	def _is_developer(self) -> bool:
		return (
			bool(self.flags.ignore_permissions)
			or frappe.session.user == "Administrator"
			or DEVELOPER_ROLE in frappe.get_roles()
		)

	def _clean_non_developer_changes(self):
		"""Non-developers may only author the rule name and NL description.

		Flipping `validated` on is refused outright; other implementation-field
		changes are cleaned up (new doc → forced to Not Implemented, existing doc →
		reverted) with a warning rather than an error.
		"""
		before = (
			None
			if self.is_new()
			else frappe.db.get_value(
				"Optimization Rule",
				self.name,
				[*IMPLEMENTATION_FIELDS, "validated"],
				as_dict=True,
			)
		)

		if self.validated and not (before and before.validated):
			frappe.throw(
				frappe._("Only a {0} can validate an Optimization Rule implementation.").format(
					frappe.bold(frappe._(DEVELOPER_ROLE))
				),
				frappe.PermissionError,
			)

		if before is None:
			# new document by a non-developer: force a description-only rule
			if self.implementation_type != "Not Implemented" or self.builtin_key or self.implementation_code:
				self.implementation_type = "Not Implemented"
				self.builtin_key = None
				self.implementation_code = None
				frappe.msgprint(
					frappe._(
						"Only a {0} can implement rules; this rule was saved as {1} with its "
						"description only."
					).format(frappe.bold(frappe._(DEVELOPER_ROLE)), frappe.bold("Not Implemented")),
					indicator="orange",
				)
			return

		reverted = []
		for fieldname in (*IMPLEMENTATION_FIELDS, "validated"):
			# `or None` — an empty Code/Data field is "" on the doc but NULL in the DB
			if (before.get(fieldname) or None) != (self.get(fieldname) or None):
				self.set(fieldname, before.get(fieldname))
				reverted.append(frappe.bold(self.meta.get_label(fieldname)))
		if reverted:
			frappe.msgprint(
				frappe._("Only a {0} can change the implementation; reverted: {1}").format(
					frappe.bold(frappe._(DEVELOPER_ROLE)), ", ".join(reverted)
				),
				indicator="orange",
			)

	def is_implemented(self) -> bool:
		"""Whether this rule can be used by the optimizer."""
		if self.implementation_type == "Built-in":
			return bool(self.builtin_key) and self.builtin_key in BUILTIN_RULES
		if self.implementation_type == "Custom Code":
			return bool(self.implementation_code) and bool(self.validated)
		return False

	def _validate_builtin_key(self):
		builtin = BUILTIN_RULES.get(self.builtin_key or "")
		if builtin is None:
			frappe.throw(
				frappe._("Built-in key {0} is not registered in code. Registered keys: {1}").format(
					frappe.bold(self.builtin_key or ""), ", ".join(sorted(BUILTIN_RULES))
				)
			)
		return builtin

	def _validate_custom_code(self):
		# Syntax-only check: compile() does not execute the code. Unvalidated
		# (e.g. freshly LLM-generated) code must never run during save.
		try:
			compiled = compile(self.implementation_code or "", f"<Optimization Rule: {self.name}>", "exec")
		except SyntaxError as exc:
			frappe.throw(frappe._("Implementation code has a syntax error: {0}").format(exc))
			return
		if "apply" not in compiled.co_names:
			# cheap structural check; the authoritative check (callable apply(ctx))
			# happens when the rule is compiled at solve time
			frappe.msgprint(
				frappe._("Implementation code does not appear to define {0}.").format(
					frappe.bold("apply(ctx)")
				),
				indicator="orange",
			)

		# Editing validated code un-validates it, unless the developer (re)checked
		# the flag in this very save.
		before = self.get_doc_before_save()
		if (
			before
			and before.implementation_code != self.implementation_code
			and before.validated
			and self.validated
		):
			self.validated = 0
			frappe.msgprint(
				frappe._("Implementation code changed; the Validated flag was cleared for re-review."),
				indicator="orange",
			)
