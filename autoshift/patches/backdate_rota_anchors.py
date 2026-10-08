"""Pull every disabled rota's `create_shifts_after` back to before the current period.

`create_shifts_after` is a rota's phase anchor *and* its handover boundary: nothing is
generated on or before it. The Rota Editor used to anchor a multi-week pattern on the
week the planner was viewing, and imports anchor wherever their history ended, so some
rotas start weeks into the future and are simply absent — to the optimizer and the wall
chart alike — before then.

Each anchor later than `cycle.ANCHOR_LEAD_WEEKS` before today moves back by whole cycles
(`cycle.backdated_anchor`), which keeps its phase exactly. Only `enabled = 0` rows are
touched: those are the rotas HRMS's own generator is kept off (see `autoshift.rota`). An
enabled one uses the field as HRMS's high-water mark, and moving it back would have HRMS
generate the intervening weeks as real Shift Assignments.

Idempotent: a second run finds every anchor already early enough.
"""

import frappe

from autoshift.rota.cycle import FREQUENCY_WEEKS, anchor_cutoff, backdated_anchor


def execute():
	cutoff = anchor_cutoff(frappe.utils.getdate(frappe.utils.today()))
	rows = frappe.get_all(
		"Shift Schedule Assignment",
		filters={"enabled": 0, "create_shifts_after": [">", cutoff]},
		fields=["name", "shift_schedule", "create_shifts_after"],
	)
	if not rows:
		return
	frequency = dict(
		frappe.get_all(
			"Shift Schedule",
			filters={"name": ["in", list({r.shift_schedule for r in rows if r.shift_schedule})]},
			fields=["name", "frequency"],
			as_list=True,
		)
	)
	for row in rows:
		cycle = FREQUENCY_WEEKS.get(frequency.get(row.shift_schedule))
		if cycle is None:
			continue  # unknown cadence: `load_rotas` skips these too, so leave them be
		anchor = backdated_anchor(frappe.utils.getdate(row.create_shifts_after), cycle, cutoff)
		frappe.db.set_value(
			"Shift Schedule Assignment", row.name, "create_shifts_after", anchor, update_modified=False
		)
