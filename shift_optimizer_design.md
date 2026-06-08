# Dental Practice Shift Optimizer — Design Document

## 1. Context and Scope

This document describes the design of a Frappe custom app (`autoshift`) that interfaces with an existing Frappe HR / ERPNext instance to produce shift schedules for a multi-discipline dental practice using Mixed Integer Linear Programming (MILP).

As much data as possible is inferred from the Frappe HR / ERPNext instances, with the app itself only containing minimal configuration doctypes for the optimizer runs.

The app is read-only with respect to Frappe HR data during the optimisation phase. It may eventually write back to Frappe HR only when a proposed schedule is explicitly approved, at which point it would bulk-create `Shift Assignment` records. But its main purpose is to generate schedules dynamically for HR to act on manually.

### Practice structure

- **Disciplines**: Omnipractice, Orthodontics, Dental Hygiene
- **Shifts**: two atomic shifts per day — AM and PM; no half-shifts; one shift per employee per day maximum
- **Planning horizon**: 4 weeks periodic schedule (default), or unbounded shift planning
- **Staff types**:
  - Salaried staff (receptionists, nurses/assistants, practice manager) — fixed monthly pay
  - Doctors paid on turnover — variable pay; scheduling still subject to monthly FTE target
- **Branches**: two branches with some shared staff and some fixed staff

All of the above are examples and should be inferred from main app data, and then configured by custom app data references. For example, there is a table in the custom app that lets the user specify which departments, shifts, locations and employee groups should be included in the optimizer.

### Room and staffing constraints

Every discipline requires **one assistant per active room**. Orthodontics additionally allows **one orthodontist to supervise up to 3 rooms simultaneously**; all other disciplines have a 1:1 doctor-to-room ratio.

---

## 2. Frappe HR Data Consumed by the Optimizer

The optimizer reads the following existing Frappe HR / ERPNext DocTypes. No modifications to these DocTypes are made by the app except where explicitly noted.

### 2.1 Employee

Key fields read: `designation`, `department`, `holiday_list`, `status`.

### 2.2 Shift Type

Fields read: `name`, `start_time`, `end_time`. Two shift types are defined per discipline (AM and PM), giving six shift types total (plus any non-clinical variants).
The app doesn't assume this and reads it every time from source of truth.

### 2.3 Shift Location

Fields read: `name`. The app uses these as the room index set. Extra data fields are added by the app.

### 2.4 Holiday List

Fields read: dates. Only in the unbounded planning mode, used to exclude non-working days from the available slot count for the planning. In periodic schedule mode

### 2.5 Leave Application

Fields read: `employee`, `from_date`, `to_date`, `status`. Approved leave applications produce a per-employee date blocklist that forces the corresponding decision variables to zero.
Pending applications can be ignored or, if a config option is selected, treated as approved to produce a feasibility analysis. This config is again granularized in the app's config, the user is able to select which pending application to analyse, if any.

### 2.6 Shift Assignment (existing)

Pre-existing submitted Shift Assignments within the planning month are read as **forced variables** (fixed to 1) before the MILP is solved.

---

## 3. New DocTypes in `autoshift`

Additional data is needed for autoshift to work. This can be added as fixtures on existing DocTypes, or as new Doctypes referencing the unmodified instances instances.

### 3.1 `Discipline-Designation-Branch Config`

Stores per-discipline capacity and staffing ratio parameters. One record per (discipline, employee designation, branch) tuple.
The rows are added in user space.

| Field | Type | Description |
| --- | --- | --- |
| `discipline` | Link → Department | Which department is relevant |
| `employee_type` | Link → Employee Designation | One row per employee designation in each department |
| `max_rooms_for_employee_type` | Int | i.e. 1 for omni/hygiene, 3 for orthodontics, 1 for all assistant types |
| `rooms_num` | Int, >0 | Number of rooms of this designation at this branch (0 if row is missing) |
| `branch` | Link → Branch | |

### 3.2 `Employee Settings`

| Field | Type | Description |
| --- | --- | --- |
| `employee` | Link → Employee | One row per employee |
| `fte` | Float, 0–100 | Full time equivalent (percentage) |
| `preferred_branch` | Table | For each branch, a Float (0<=p<=1) marking preference, totaling 1 across branches (by default 1/\|B\|) |

### 3.3 `Optimizer Settings`

Singleton. Stores policy parameters and objective weights. Versioned via `amended_from` if settings history is needed.

| Field | Type | Description |
| --- | --- | --- |
| `fte_tolerance_pct` | Float | Allowed deviation from FTE target (e.g. 0.05 = ±5%) |
| `turnover_weight` | Float | Weight on revenue maximisation for doctor slots |

### 3.4 `Optimizer Run`

One record per attempt. Tracks the full lifecycle from problem construction through solution to commitment.

| Field | Type | Description |
| --- | --- | --- |
| `mode` | Select | 1-week / 2-week / 4-week / Unbounded |
| `date` | Date | First monday of the planning (in schedule mode); any day (in unbounded mode) |
| `leaves_speculations` | Table | Links to the unapproved leaves to count as approved for analysis (empty for actual planning) |
| `disregard_assignments` | Select | Use / Ignore / Weigh (Not yet implemented) |
| `status` | Select | Draft / Solving / Solved / Approved / Committed / Failed |
| `solver_log` | Text | Raw solver output for debugging |
| `objective_value` | Float | Optimal objective value achieved |
| `solution_table` | Table → `Optimizer Run Slot` | Child rows, one per decision variable = 1 |
| `committed_assignments` | Table | Links to created Shift Assignment records after commit |

### 3.5 `Optimizer Run Slot` (child of Optimizer Run)

Each row represents one assigned shift in the proposed solution.

| Field | Type | Description |
| --- | --- | --- |
| `employee` | Link → Employee | |
| `shift_type` | Link → Shift Type | |
| `date` | Date | |
| `shift_location` | Link → Shift Location | Specific room |
| `forced` | Check | True if this slot was pre-fixed from an existing Shift Assignment |

---

## 4. MILP Model

### 4.1 Index Sets

| Symbol | Description | Source |
| --- | --- | --- |
| ***K*** | Disciplines | {Omnipractice, Orthodontics, Dental Hygiene} |
| ***E*** | Employees | Employee (status = Active, designation ∈ Discipline-Designation-Branch Config) |
| ***S*** | Shift slots (i.e. AM, PM) | Shift Type |
| ***D*** | Working days in span | Calendar (minus Holiday List dates for Unbounded mode) |
| ***B*** | Branches | |
| ***W*** | Salaried Staff | (for membership checks) |
| ***T*** | Turnover-paid Staff | (idem) |

### 4.2 Parameters

| Symbol | Description | Source |
| --- | --- | --- |
| `fte[e]` | FTE fraction | `Employee.custom_fte` |
| `target_shifts[e]` | round(fte[e] × \|D\| × 2) | Derived at runtime, stops early if target isn't within `fte_tolerance_pct` |
| `max_rpe[e]` | Max rooms per employee type | `Discipline-Designation-Branch Config[e.designation,b].max_rooms_per_employee_type` |
| `leave[e,d]` | 1 if employee on approved leave | Leave Application |
| `forced[e,s,d]` | 1 if pre-assigned | Existing Shift Assignment if in ``Use`` mode |
| `rooms[k,b]` | number of rooms per discipline per branch | `Discipline-Designation-Branch Config[k,b].rooms_num` |

### 4.3 Decision Variables

```math
x[e, s, d, b] ∈ {0, 1}
```

1 if employee *e* is assigned to slot *s* on day *d* in branch *b*.

```math
\mathtt{active\_rooms}[k, s, d, b] ∈ {0, ... , rooms[k,b]}
```

Number of rooms staffed in discipline *k*, in slot *s* on day *d* in branch *b*.

### 4.4 Constraints

**One shift per employee per day:**

```math
∀ e,d :\quad ∑_s ∑_r  x[e, s, d, b]  ≤  1
```

**Leave blocklist:**

```math
∀ e, d,\mathtt{leave}[e,d] = 1 :\quad ∑_s ∑_r  x[e, s, d, b]  =  0
```

**Forced assignments:**

```math
∀ e, d,\mathtt{forced}[e,s,d] = 1 :\quad x[e, s, d, b]  =  1
```

**Max rooms per employee:**

```math
∀ e,s,r :\quad ∑_r  x[e, s, d, b] ≤ \mathtt{max\_rpe}[e.k]     
```

**Assistant coverage:**

```math
∀k,s,d,b :\quad ∑_{e : e.k=\mathtt{assistant}}  x[e, s, d, b]  ≥  \mathtt{active\_rooms}[k, s, d, b]
```

**FTE target (salaried staff):**

```math
∀ e ∈ W:\quad (1 - tol) × \mathtt{target\_shifts}[e]  ≤  ∑_{s,d,b} x[e,s,d,b]  ≤  (1 + tol) × \mathtt{target\_shifts}[e]
```

**FTE target (turnover-paid-employees: minimum only):**

```math
∀ e ∈ T:\quad ∑_{k,e,s,d,b} x[e,s,d]  ≥  (1 - tol) × \mathtt{target\_shifts}[e]
```

### 4.5 Objective

Maximise a weighted sum of:

1. **Utilisation of rooms**:

   ```math
   ∑_{k,s,d,b} \mathtt{active\_rooms}[k,s,d,b]
   ```

2. **Shift balance per employee** — fairness across the eligible shift types (i.e. AM/PM).

   Individual unfairness computed as pairwise difference totals of shift types:

   ```math
   \mathtt{unfairness}(e)= ∑_{(s_1,s_2)\in S^2:s_1<s_2} \bigg|∑_{d,b} x[e, s_1, d, b] - x[e, s_2,d,b]\bigg|
   ```

   Note that the absolute value acts on the inner sum (over all days), each shift pair is treated as a separate "difference". In the simple case of only 2 possible shifts, the outer sum only has one iteration.

   Then the total unfairness difference between employees is calculated again pairwise:

   ```math
   \mathtt{total\_unfairness=}∑_{(e_1,e_2)\in E^2} | \mathtt{unfairness}(e_1) - \mathtt{unfairness}(e_2) |
   ```

   To minimize unfairness, we minimize negative unfairness: ```-total_unfairness```.
   Since we are maximizing an absolute value with a negative coefficient, the absolute values can be linearised with auxiliary variables.

---

## 5. App Architecture

### 5.1 Directory structure

```text
shift_optimizer/
├── shift_optimizer/
│   ├── doctype/
│   │   ├── room_discipline_config/
│   │   ├── optimizer_settings/
│   │   ├── optimizer_run/
│   │   └── optimizer_run_slot/
│   ├── fixtures/
│   │   └── custom_field.json        # FTE on Employee, discipline on Shift Location
│   ├── optimizer/
│   │   ├── __init__.py
│   │   ├── data_loader.py           # Reads Frappe HR data, builds index sets + params
│   │   ├── model_builder.py         # Constructs the MILP using PuLP or OR-Tools
│   │   ├── solver.py                # Runs solver, writes solution to Optimizer Run
│   │   └── committer.py             # Converts approved Optimizer Run to Shift Assignments
│   └── hooks.py
├── requirements.txt                 # MILP solver dependency (e.g. pulp, ortools)
└── setup.py
```

### 5.2 Commit workflow

1. `Optimizer Run` is created with status `Draft`.
2. User triggers solve → status moves to `Solving` (background job via Frappe's enqueue).
3. On completion, status moves to `Solved`; solution rows populate `solution_table`.
4. User reviews via the Roster (existing Frappe HR UI) or a custom list view of `Optimizer Run Slot`.
5. User approves → status moves to `Approved`.
6. User triggers commit → `committer.py` bulk-creates `Shift Assignment` records and links them back to the run; status moves to `Committed`.
7. If re-running is needed, a new `Optimizer Run` is created (old runs are preserved as audit trail).

### 5.4 Docker considerations

- The app is installed as a standard Frappe app alongside HRMS in the existing Docker Compose stack.
- The `custom_field.json` fixture is applied automatically on `bench migrate` — no manual UI steps.
- The CBC solver binary ships inside the PuLP Python package and requires no additional system packages.
- Background solve jobs use Frappe's existing Redis + RQ worker stack; no additional infrastructure needed.

---

## 6. Assumptions and Open Questions

| # | Item | Assumption made | Revisit if... |
| --- | --- | --- |
| 1 | Assistants are discipline-specific | Each assistant is assigned to one department | Cross-trained assistants exist |
| 2 | Turnover doctors have no upper shift bound | Only a minimum is enforced | Doctors request a maximum |
| 3 | AM/PM fairness is global, not per discipline | Single fairness term in objective | Disciplines need separate fairness tracking |
| 4 | Room count is fixed per month | `Discipline-Designation-Branch Config` is static | Rooms are taken offline for periods |
| 5 | Leave is the only day-level blocker | No other day-level constraints | On-call or external commitments need modelling |
| 6 | No minimum rest gap between shifts | One shift per day maximum makes this moot | Night shifts or extended hours are introduced |

---

## 7. Out of Scope

- Payroll calculation (handled by ERPNext salary structures)
- Revenue/turnover tracking per doctor per shift (handled by ERPNext Healthcare or Invoicing)
- Patient appointment scheduling (separate system)
- Real-time attendance tracking (handled natively by Frappe HR check-ins)
