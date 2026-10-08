"""Small shared helpers that belong to no single doctype."""

import frappe


def background_workers_alive() -> bool:
	"""Whether any RQ worker is registered to serve this bench's queues.

	Development benches often run only ``bench serve``: everything enqueued then rots
	in redis with no error anywhere. Callers about to hand work to the queue use this
	to fall back (or fail loudly) instead of going silent. A crashed worker's
	registration key lingers until its TTL expires, so a brief false positive right
	after a worker dies is possible — acceptable for a warning heuristic.
	"""
	try:
		from frappe.utils.background_jobs import get_workers

		return bool(get_workers())
	except Exception:
		# Being unable to inspect redis is not this check's problem to raise on;
		# report "alive" so callers keep the normal queueing path.
		frappe.log_error(frappe.get_traceback(), "background_workers_alive check failed")
		return True
