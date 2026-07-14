# Copyright (c) 2026, CMDBB and Contributors
# See license.txt

import datetime
from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from autoshift.autoshift.doctype.optimizer_run.optimizer_run import (
	OptimizerRun,
	datapackage_cache_key,
)
from autoshift.optimizer.types import DataPackage

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
# These tests inject a synthetic DataPackage into the cache and never touch real
# HR records (they *rely* on Alice/Bob not existing — see the ignore_links
# flag set). Pruning these links from the recursive test-record walk matters: the
# Leave Application subtree alone drags in the whole hrms/erpnext graph, whose
# generation only ever worked on sites with warmed-up state (e.g. frappe's own
# "Test ToDo" workflow tries to email a wkhtmltopdf print of every new ToDo and
# dies on a pristine, unserved site).
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Leave Application",
	"Employee",
	"Shift Type",
	"Shift Location",
	"Branch",
]

MONDAY = datetime.date(year=2026, month=6, day=22)


def pkg(**overrides) -> DataPackage:
	"""
	Minimal valid DataPackage: 1 salaried employee, 1 AM shift, 1 day, 1 branch.
	Override any field to build specific scenarios.
	"""
	base: dict[str, Any] = dict(
		flags=set(),
		employees=["Alice", "Bob"],
		shift_types=["Day", "Night"],
		working_days=[MONDAY + datetime.timedelta(days=i) for i in range(7)],
		branches=["Branch1"],
		designation={"Alice": "Nurse", "Bob": "Nurse"},
		department={"Alice": "ER", "Bob": "ER"},
		target_shifts={"Alice": 5, "Bob": 5},
		max_rpe={"Alice": 1, "Bob": 1},
		rooms={("ER", "Branch1"): 2},
		disciplines=["ER"],
		leave_blocked=set(),
		forced=set(),
		shift_preferences={
			"Alice": {"Day": 1.0, "Night": 0.5},
			"Bob": {"Day": 0.5, "Night": 1.0},
		},
	)
	base.update(overrides)
	if "shift_preferences" not in overrides and ("shift_types" in overrides or "employees" in overrides):
		n_shifts: int = len(base["shift_types"])
		base["shift_preferences"] = {
			e: {s: 1 / n_shifts for s in base["shift_types"]} for e in base["employees"]
		}
	return DataPackage(**base)


class IntegrationTestOptimizerRun(IntegrationTestCase):
	"""
	Integration tests for OptimizerRun.
	Use this class for testing interactions between multiple components.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cache = frappe.cache

	def setUp(self):
		super().setUp()
		self.test_record: OptimizerRun = self.globalTestRecords["Optimizer Run"][0]
		self.test_record = frappe.get_doc(self.test_record)
		self.test_record.insert()

	def tearDown(self):
		super().tearDown()
		frappe.delete_doc("Optimizer Run", self.test_record.name, force=True)

	def test_pkg_injection(self):
		"""
		Smoke test on:
		- The cache mechanism
		- The solve() method (on a basic data package)
		"""
		self.cache.set_value(datapackage_cache_key(self.test_record.name), pkg().dumps())  # ty:ignore[unresolved-attribute]
		self.test_record.flags.ignore_links = True
		self.test_record.solve()

	def test_no_overfilling_of_shifts(self):
		self.cache.set_value(  # ty:ignore[unresolved-attribute]
			datapackage_cache_key(self.test_record.name),
			pkg(
				employees=["Alice", "Bob", "Caoimhe"],
				designation={"Alice": "Nurse", "Bob": "Nurse", "Caoimhe": "Nurse"},
				department={"Alice": "ER", "Bob": "ER", "Caoimhe": "ER"},
				target_shifts={"Alice": 5, "Bob": 5, "Caoimhe": 5},
				max_rpe={"Alice": 1, "Bob": 1, "Caoimhe": 1},
				rooms={("ER", "Branch1"): 1},
			).dumps(),
		)
		self.test_record.flags.ignore_links = True
		self.test_record.solve()
		schedule = self.test_record.get_schedule_events()
		assert {"Alice", "Bob", "Caoimhe"} & {e.name for e in schedule["employees"]} == set()
