# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class RotaDayNote(Document):
	"""A planner's footnote on one weekday of a rota pattern — see
	`autoshift/rota/notes.py`.

	Keyed by (`shift_schedule_assignment`, `weekday`), **not** by a calendar
	date: a note is tied to the rota, so it carries over to every future
	occurrence of that weekday for as long as the pattern itself runs
	(`cycle.occurrences` reads the same anchor/until/cycle the pattern uses to
	decide which days it covers). Autonamed from the same two fields, so
	re-adding a note that was once cleared collides on the same document.

	Edit the pattern (`rota/edit.py`'s "superseded, not deleted") and the note
	goes with it, the same as everything else tied to that
	`Shift Schedule Assignment` — a note on a day that was never real to begin
	with is not a loss worth guarding against here.

	Never read by the optimizer, or by anything in `autoshift/optimizer/` — it
	is display-only, drawn as a footnote on the wall chart.
	"""

	def validate(self):
		self.note = (self.note or "").strip()
		if not self.note:
			frappe.throw(frappe._("A note needs some text — delete the row instead of blanking it."))
