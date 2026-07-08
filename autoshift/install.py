"""
Install hooks. Fresh installs mark all patches as completed WITHOUT running them
(frappe.installer.set_all_patches_as_completed), so data seeding needed on new
sites must run from after_install — the create_standard_optimization_rules patch
covers the same seeding for sites that installed before it existed.
"""


def after_install():
	from autoshift.patches.create_standard_optimization_rules import execute

	execute()
