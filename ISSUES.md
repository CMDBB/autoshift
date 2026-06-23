# Draft GitHub issues

One section per issue. File these on `CMDBB/autoshift` at your own pace — title is the
heading, suggested labels are noted, body is ready to paste as-is.

---

## 1. `solver.py` caches a hashless `Failed` run, defeating retry caching

**Labels:** bug

Input hash is only persisted inside the `try` block in `solver.py`. If a run fails
before/during hashing, it gets cached as `Failed` with no hash attached. On retry, the
cache lookup (by input hash) can't find this run, so a known-bad input isn't recognized
and the solver re-attempts it from scratch.

**Fix:** compute and persist the input hash before entering the `try`, or in a `finally`,
so every run — solved, failed, or erroring early — is cached under its real hash.

---

## 2. No source of truth for branch on `Shift Assignment`; no filter on which `Shift Type`s the optimizer considers

**Labels:** design, needs-discussion

`data_loader.py:95` loads every `Shift Type` unconditionally. The design doc (§2.2)
anticipates "non-clinical variants" existing among Shift Types, but nothing excludes them
from the optimizer, and there's no config doctype that says which shifts are in scope.

This compounds a second problem: resolving forced assignments from existing
`Shift Assignment` records (`disregard_assignments = "Use"`) needs to know which branch
each assignment belongs to, but there's no reliable doctype-level link to derive it from.
`Employee.branch` doesn't constrain it, since employees move freely between branches. The
previous implementation guessed branch by checking whether a branch name was a substring
of the shift type name — fragile, and `model_builder.py` silently dropped any forced tuple
with an unresolved branch (a previously committed shift could silently disappear from a
re-solve).

**Current stopgap** (`data_loader.py:219-233`): assume a single configured branch, throw if
more than one exists. This unblocks single-branch practices but doesn't solve multi-branch.

**Needs a design decision** before `"Use"` mode can be wired in for real — options include
an explicit shift-assignment-level branch field, or a different source of truth entirely.
Same root problem (no scoping config) likely also needs solving for the Shift Type filter.

---

## 3. Re-add `Optimizer Run` → `Shift Assignment` link-back after commit

**Labels:** enhancement, in-progress

`73e98fa` ("start online modifications") removed the `committed_assignments` field from
`optimizer_run.json` as the first step of an in-progress redesign of how a committed run
stays linked to the records it created. `committer.py` currently raises
`NotImplementedError` unconditionally — no Shift Assignments are created until this lands,
by design (avoids leaving behind untraceable records).

**Plan:** re-add it as a table on `Optimizer Run`, unless a better mechanism turns up
during implementation.

---

## 4. `is_salaried` is a hardcoded stub — needs a configurable name list

**Labels:** enhancement

`data_loader.py:154-158` currently assumes every employee is salaried
(`is_salaried[name] = True`) as a placeholder. The previous implementation matched
`employment_type` against hardcoded strings ("turnover"/"commission"/"casual"), which
misclassified pay structure whenever a practice used different `Employment Type` labels,
since it's a configurable Link doctype, not a fixed enum.

**Fix:** add a configurable name list (e.g. on Optimizer Settings) that lets each practice
specify which Employment Type values count as turnover-paid, replacing the stub.

---

## 5. Implement `disregard_assignments` = `Use` and `Weigh`

**Labels:** enhancement

Both are selectable in the UI; `data_loader.py` only handles `"Ignore"` and raises
`NotImplementedError` for the other two.

- `"Use"` forces existing Shift Assignments as hard constraints. Code path exists
  (currently unreachable behind the `NotImplementedError` guard) but isn't wired in or
  tested. Blocked on issue #2 above (branch resolution) for multi-branch correctness.
- `"Weigh"` is intended as a soft preference — existing assignments bias the objective like
  a shift preference weight, but the solver can still move them. No implementation yet.

---

## 6. `Unbounded` planning mode (backlog)

**Labels:** backlog, future

Selectable in the UI, but `planning_days()` raises `NotImplementedError` immediately.
Intended for future tools like automatic dynamic calendar speculation. No near-term design
work planned — tracking this so it isn't forgotten, not requesting active work.

---

## 7. Room-level assignment (backlog)

**Labels:** backlog, future

`Optimizer Run Slot.shift_location` and `Shift Location.custom_discipline` exist as
scaffolding, but `model_builder.py` only tracks an aggregate room *count* per
discipline/slot — nothing assigns a specific room yet. Same priority as #6: backlog, not
near-term.
