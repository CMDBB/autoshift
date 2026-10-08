# Copyright (c) 2026, CMDBB and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RotaEditDraft(Document):
	"""A user's staged, not-yet-applied edits to one discipline's bound rotas.

	One per (user, discipline) by construction — `autoname` derives the name from both,
	so `autoshift.rota.editor.get_or_create_draft` always finds the same document rather
	than accumulating stray drafts. See `autoshift/public/js/rota_editor.js` for the page
	that reads and writes these, and `rota/edit.py` for what a row actually means.
	"""
