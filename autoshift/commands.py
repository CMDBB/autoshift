import click
import frappe
from frappe.commands import get_site, pass_context

#: Autoshift's own configuration — the only thing this app is the source of truth for.
#: Everything a site otherwise needs (Company, Branch, Department, Designation, Shift
#: Type, Employee) is provisioned by zawin2frappe from its practice profile, so dumping
#: it here would both duplicate that app and drop real personnel records into a directory
#: on disk. Run the zawin2frappe import first, then seed this on top.
#:
#: Ordered by dependency — seeding walks this tuple, not the directory listing.
DEV_DATA_DOCTYPES = (
	"Holiday List",
	"Scheduling Role",
	"Discipline Branch Config",
	"Employee Scheduling Role",
	"Employee Settings",
	"Optimizer Settings",
)

#: Framework bookkeeping. Frappe regenerates these on insert, and the user links point at
#: accounts that need not exist on the target site.
_VOLATILE_FIELDS = frozenset(
	{
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"_assign",
		"_comments",
		"_liked_by",
		"_user_tags",
	}
)


def _strip_volatile(record: dict) -> dict:
	"""Drop framework bookkeeping, recursing into child tables."""
	cleaned = {}
	for key, value in record.items():
		if key in _VOLATILE_FIELDS:
			continue
		if isinstance(value, list):
			# a child table: rows carry their own name/parent, which are re-derived on insert
			cleaned[key] = [
				_strip_volatile({k: v for k, v in row.items() if k not in ("name", "parent")})
				for row in value
				if isinstance(row, dict)
			]
		else:
			cleaned[key] = value
	return cleaned


def _export_records(doctype: str) -> list[dict]:
	"""
	All records of `doctype` as plain dicts, child tables included.

	Deliberately not `frappe.get_all(fields=["*"])`: that returns only the parent table's
	columns, so Employee Settings' preference rows and the Discipline Branch
	Config shift-type selection would silently vanish. Singles have no `tab<Doctype>` table
	at all (they live in `tabSingles`), so get_all cannot read them either.
	"""
	if frappe.get_meta(doctype).issingle:
		return [_strip_volatile(frappe.get_single(doctype).as_dict())]
	return [
		_strip_volatile(frappe.get_doc(doctype, name).as_dict())
		for name in frappe.get_all(doctype, pluck="name")
	]


@click.command("dump-dev-data")
@click.option("--output", default="./dev_data", help="Output directory")
@pass_context
def dump_dev_data(context, output):
	"""Export this site's Autoshift configuration (see DEV_DATA_DOCTYPES)"""
	import json
	import os

	site = get_site(context)
	frappe.init(site=str(site))
	frappe.connect()

	os.makedirs(output, exist_ok=True)

	for dt in DEV_DATA_DOCTYPES:
		records = _export_records(dt)
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
@click.option("--overwrite", is_flag=True, default=False, help="Update records that already exist")
@pass_context
def seed_dev_data(context, input_dir, overwrite):
	"""Import Autoshift configuration from a previous dump

	Only covers what this app owns. The Company, Branch, Designation, Shift Type and
	Employee records these link to must already exist — provision them with zawin2frappe
	(or by hand) first.
	"""
	import json
	import os

	site = get_site(context)
	frappe.init(site=str(site))
	frappe.connect()

	for dt in DEV_DATA_DOCTYPES:
		path = os.path.join(input_dir, f"{dt}.json")
		if not os.path.exists(path):
			click.echo(f"  Skipped  {dt: <40} (no {dt}.json in {input_dir})")
			continue

		# developer-run bench command reading a developer-supplied local path;
		# no web/user input involved
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal
		with open(path) as f:
			records = [_strip_volatile(record) for record in json.load(f)]

		written = 0
		if frappe.get_meta(dt).issingle:
			written += _seed_single(dt, records[0], overwrite) if records else 0
		else:
			for record in records:
				if frappe.db.exists(dt, record.get("name")):
					if overwrite:
						doc = frappe.get_doc(dt, record["name"])
						doc.update(record)
						doc.save()
						written += 1
				else:
					frappe.get_doc({"doctype": dt, **record}).insert()
					written += 1

		# CLI script outside the request/transaction lifecycle; checkpoint per
		# doctype so a failing file doesn't roll back everything imported so far
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
		frappe.db.commit()
		skipped = len(records) - written
		detail = f" ({skipped} already present, pass --overwrite to update)" if skipped else ""
		click.echo(f"  Imported {written: >3} {dt: <40} records from {dt}.json{detail}")

	frappe.destroy()


def _seed_single(doctype: str, record: dict, overwrite: bool) -> int:
	"""
	Seed a Single doctype. A Single always exists, so `--overwrite` would make it
	unseedable on a fresh site; write it when nothing would be clobbered instead, and
	defer to the flag only where the site already holds a different value.
	"""
	current = frappe.get_single(doctype)
	conflicts = [
		field
		for field, value in record.items()
		if field not in ("doctype", "name")
		and value not in (None, "")
		and current.get(field) not in (None, "", value)
	]
	if conflicts and not overwrite:
		click.echo(f"  Skipped  {doctype: <40} (would overwrite {', '.join(conflicts)}; pass --overwrite)")
		return 0
	current.update(record)
	current.save()
	return 1


def _default_snapshot_dir():
	# bench chdirs into sites/ before dispatching commands, so a plain relative
	# default would land under sites/sandbox/snapshots instead of the app's own
	# sandbox/ — anchor it to this file's location instead.
	import os

	app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	return os.path.join(app_root, "sandbox", "snapshots")


@click.command("capture-datapackage")
@click.option(
	"--run",
	"run_name",
	required=True,
	help="Optimizer Run to capture (its date/mode/ruleset/leave speculations/existing-assignments "
	"mode decide what data_loader.load resolves)",
)
@click.option("--output", default=_default_snapshot_dir, help="Output directory")
@pass_context
def capture_datapackage(context, run_name, output):
	"""Snapshot the DataPackage an Optimizer Run resolves to, for offline sandbox play"""
	import os

	from autoshift.optimizer import data_loader

	site = get_site(context)
	frappe.init(site=str(site))
	frappe.connect()

	run = frappe.get_doc("Optimizer Run", run_name)
	data = data_loader.load(run)

	os.makedirs(output, exist_ok=True)
	path = os.path.join(output, f"{run_name}.json")
	# developer-run bench command writing to a developer-supplied local path;
	# no web/user input involved
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal
	with open(path, "w") as f:
		f.write(data.dumps())
	click.echo(
		f"  Captured DataPackage for {run_name} ({len(data.employees)} employees, "
		f"{len(data.working_days)} days) to {path}"
	)

	frappe.destroy()


@click.command("diagnose-model")
@click.option("--run", "run_name", help="Optimizer Run whose DataPackage to diagnose (needs a site)")
@click.option(
	"--snapshot",
	"snapshot_path",
	help="A DataPackage JSON from `capture-datapackage`, diagnosed offline instead of --run",
)
@click.option("--lp", "lp_path", help="Also write the full LP file (every row and bound) here")
@click.option(
	"--elastic/--no-elastic",
	default=True,
	help="Solve the elastic model to find the minimum set of constraints that must give",
)
@click.option("--integral", is_flag=True, default=False, help="Solve the elastic model as a MILP")
@click.option("--time-limit", default=60, help="Seconds the elastic solve may take")
@click.option("--examples", default=3, help="Sample rows to print per variable/constraint family")
@pass_context
def diagnose_model(context, run_name, snapshot_path, lp_path, elastic, integral, time_limit, examples):
	"""Explain what an Optimizer Run's MILP contains, and why it has no solution

	The counterpart to a Failed run's Solver Log, for when you want the diagnosis without
	burning a run — or want it against a captured snapshot, with no site at all.
	"""
	from autoshift.optimizer import diagnostics, model_builder
	from autoshift.optimizer.types import DataPackage

	if bool(run_name) == bool(snapshot_path):
		raise click.UsageError("Pass exactly one of --run or --snapshot.")

	if snapshot_path:
		# developer-run bench command reading a developer-supplied local path
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal
		with open(snapshot_path) as f:
			data = DataPackage.loads(f.read())
	else:
		from autoshift.optimizer import data_loader

		site = get_site(context)
		frappe.init(site=str(site))
		frappe.connect()
		try:
			data = data_loader.load(frappe.get_doc("Optimizer Run", run_name))
		finally:
			frappe.destroy()

	click.echo(diagnostics.report(data, elastic=elastic, examples=examples, time_limit=time_limit))

	if integral and elastic:
		click.echo("\n--- Elastic analysis (integral) ---")
		status, violations = diagnostics.elastic_analysis(data, time_limit=time_limit, integral=True)
		click.echo(f"elastic MILP solved: {status}")
		for violation in violations:
			click.echo(f"  {violation}")

	if lp_path:
		prob, *_ = model_builder.build(data)
		click.echo(f"\nLP written to {diagnostics.write_lp(prob, lp_path)}")


commands = [dump_dev_data, seed_dev_data, capture_datapackage, diagnose_model]
