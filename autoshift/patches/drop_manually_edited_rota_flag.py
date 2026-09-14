"""Retire `Shift Schedule Assignment.custom_manually_edited` for `custom_unconfirmed`.

The flag used to mark a hand edit as gold standard, which left everything else,
including a pattern HR typed straight into the Desk, looking like something an
importer was free to overwrite. It is now the other way round: an importer marks
what it *inferred* as unconfirmed (silver), and anything unflagged is gold. The new
field ships as a fixture and defaults to 0, so every row already hand-edited is gold
with no data to move. Flagging the imported rows is the importer's job (zawin2frappe
ships its own patch for that), because only it knows which rows are its own.

Fixture sync never deletes a Custom Field that left the fixture file, so the old one
is removed here. `Shift Schedule.custom_manually_edited` stays: there it only says the
schedule is private to the Rota Editor, which is what lets the editor delete it.
"""

import frappe

OLD = "Shift Schedule Assignment-custom_manually_edited"


def execute():
	if frappe.db.exists("Custom Field", OLD):
		frappe.delete_doc("Custom Field", OLD, ignore_permissions=True, force=True)
