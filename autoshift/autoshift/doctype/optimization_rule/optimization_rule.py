import frappe
from frappe.model.document import Document

from autoshift.optimizer.rules import BUILTIN_RULES


class OptimizationRule(Document):
	def validate(self):
		if self.implementation_type == "Built-in":
			self._validate_builtin_key()
		elif self.implementation_type == "Custom Code":
			self._validate_custom_code()
		self.implemented = 1 if self.is_implemented() else 0

	def is_implemented(self) -> bool:
		"""Whether this rule can be used by the optimizer."""
		if self.implementation_type == "Built-in":
			return bool(self.builtin_key) and self.builtin_key in BUILTIN_RULES
		if self.implementation_type == "Custom Code":
			return bool(self.implementation_code) and bool(self.validated)
		return False

	def _validate_builtin_key(self):
		if self.builtin_key not in BUILTIN_RULES:
			frappe.throw(
				frappe._("Built-in key {0} is not registered in code. Registered keys: {1}").format(
					frappe.bold(self.builtin_key or ""), ", ".join(sorted(BUILTIN_RULES))
				)
			)

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
