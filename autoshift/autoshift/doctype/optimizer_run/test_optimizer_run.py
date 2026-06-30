# Copyright (c) 2026, CMDBB and Contributors
# See license.txt

import datetime

import frappe
from frappe.tests import IntegrationTestCase

from autoshift.autoshift.doctype.optimizer_run.optimizer_run import OptimizerRun

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]

MONDAY = datetime.date(year=2026, month=6, day=22)


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
		self.test_record = self.globalTestRecords["Optimizer Run"][0]
		self.test_record = frappe.get_doc(dict(self.test_record))
		self.test_record.insert()

	def tearDown(self):
		super().tearDown()
		frappe.delete_doc("Optimizer Run", self.test_record.name, force=True)

	def test_override_pkg(self):
		"""
		Tests:
		- The cache mechnism
		- The solve() method (on a controlled data package)
		- stability of the table generation (get_schedule_events())
		"""
		from autoshift.optimizer.types import DataPackage

		# inject a DataPackage into the cache for the test record, so that the OptimizerRun.solve() method can retrieve it
		data = DataPackage(
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
			fte_tolerance=0.05,
			turnover_weight=1.0,
		)
		assert self.cache is not None, "Cache is not available in the test environment"

		self.test_record.solve()
		self.test_record.reload()
		self.cache.set_value(f"DataPackage:{self.test_record.name}", data.dumps())
		with self.subTest("Check that the run status is 'Solved'"):
			self.assertEqual(self.test_record.status, "Solved")
		with self.subTest("Check that the solution table is not empty"):
			s = self.test_record.get_schedule_events()
			assert len(s) > 0
