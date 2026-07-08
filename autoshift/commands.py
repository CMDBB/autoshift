import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("dump-dev-data")
@click.option("--output", default="./dev_data", help="Output directory")
@pass_context
def dump_dev_data(context, output):
	"""Export transactional dev data (employees, leave applications, etc.)"""
	import json
	import os

	site = get_site(context)
	frappe.init(site=str(site))
	frappe.connect()

	os.makedirs(output, exist_ok=True)

	doctypes_to_dump = [
		"Company",
		"Employee",
		"Department",
		"Designation",
		"Branch",
		"Discipline Designation Branch Config",
		"Shift Type",
		"Optimizer Settings",
		"Employee Settings",
		"Holiday List",
	]

	for dt in doctypes_to_dump:
		records = frappe.get_all(dt, fields=["*"])
		path = os.path.join(output, f"{dt}.json")
		# developer-run bench command writing to a developer-supplied local path;
		# no web/user input involved
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal
		with open(path, "w") as f:
			json.dump(records, f, indent=2, default=str)
		click.echo(f"  Exported {len(records): >3} {dt: <40} records to {path}")

	frappe.destroy()


@click.command("seed-dev-data")
@click.option("--input", "input_dir", default="./dev_data", help="Input directory")
@click.option("--overwrite", is_flag=True, default=False)
@click.option(
	"--clobber-designations",
	is_flag=True,
	default=False,
	help="Delete existing Designation records if Designations.json is present",
)
@pass_context
def seed_dev_data(context, input_dir, overwrite, clobber_designations):
	"""Import transactional dev data from a previous dump"""
	import json
	import os

	site = get_site(context)
	frappe.init(site=str(site))
	frappe.connect()

	filenames = sorted(os.listdir(input_dir))
	has_designations_file = "Designation.json" in filenames
	if clobber_designations and not has_designations_file:
		raise click.ClickException(
			"The --clobber-designations flag requires Designations.json to be present in the input directory."
		)

	if has_designations_file and clobber_designations:
		frappe.db.sql("DELETE FROM `tabDesignation`")
		# CLI script outside the request/transaction lifecycle; the destructive
		# clobber is committed before the import starts touching other doctypes
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
		frappe.db.commit()
		click.echo("  Cleared existing Designation records before import")

	for filename in filenames:
		if not filename.endswith(".json"):
			continue
		dt = filename.replace(".json", "")
		path = os.path.join(input_dir, filename)
		# developer-run bench command reading a developer-supplied local path;
		# no web/user input involved
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal
		with open(path) as f:
			records = json.load(f)

		for record in records:
			if frappe.db.exists(dt, record.get("name")):
				if overwrite:
					doc = frappe.get_doc(dt, record["name"])
					doc.update(record)
					doc.save()
			else:
				doc = frappe.get_doc({"doctype": dt, **record})
				doc.insert()

		# CLI script outside the request/transaction lifecycle; checkpoint per
		# doctype so a failing file doesn't roll back everything imported so far
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
		frappe.db.commit()
		click.echo(f"  Imported {len(records): >3} {dt: <40} records from {filename}")

	frappe.destroy()


commands = [dump_dev_data, seed_dev_data]
