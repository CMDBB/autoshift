// Requires coinor-libcbc-dev at link time.
// On Ubuntu/Debian: `sudo apt install coinor-libcbc-dev` before `cargo test`.

use std::collections::{ HashMap, HashSet };

use crate::models::{ Shift, SolveRequest, SolveResponse, SolveStatus, Worker };
use crate::solver::solve;

// ── builder helpers ───────────────────────────────────────────────────────────

fn worker(id: &str, max_hours: f64, availability: &[&str]) -> Worker {
    Worker {
        id: id.into(),
        max_hours,
        availability: availability
            .iter()
            .map(|s| s.to_string())
            .collect(),
        preferences: HashMap::new(),
    }
}

fn worker_with_prefs(
    id: &str,
    max_hours: f64,
    availability: &[&str],
    prefs: &[(&str, f64)]
) -> Worker {
    Worker {
        id: id.into(),
        max_hours,
        availability: availability
            .iter()
            .map(|s| s.to_string())
            .collect(),
        preferences: prefs
            .iter()
            .map(|(k, v)| (k.to_string(), *v))
            .collect(),
    }
}

fn shift(id: &str, hours: f64, min: usize, max: usize) -> Shift {
    Shift {
        id: id.into(),
        duration_hours: hours,
        min_workers: min,
        max_workers: max,
    }
}

fn request(workers: Vec<Worker>, shifts: Vec<Shift>) -> SolveRequest {
    SolveRequest { workers, shifts }
}

// ── assertion helpers ─────────────────────────────────────────────────────────

fn is_optimal(r: &SolveResponse) -> bool {
    matches!(r.status, SolveStatus::Optimal)
}

fn is_infeasible(r: &SolveResponse) -> bool {
    matches!(r.status, SolveStatus::Infeasible)
}

fn assigned(r: &SolveResponse, worker: &str, shift: &str) -> bool {
    r.assignments.iter().any(|a| a.worker_id == worker && a.shift_id == shift)
}

fn count_for_shift(r: &SolveResponse, shift_id: &str) -> usize {
    r.assignments
        .iter()
        .filter(|a| a.shift_id == shift_id)
        .count()
}

fn approx_eq(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-6
}

// ═══════════════════════════════════════════════════════════════════════════════
// Infeasible scenarios
// ═══════════════════════════════════════════════════════════════════════════════

#[test]
fn no_workers_is_infeasible() {
    let r = solve(&request(vec![], vec![shift("s1", 8.0, 1, 2)]));
    assert!(is_infeasible(&r));
    // The shift that could not be staffed is reported
    assert_eq!(r.unmet_shifts.len(), 1);
    assert_eq!(r.unmet_shifts[0].shift_id, "s1");
    assert_eq!(r.unmet_shifts[0].required, 1);
    assert_eq!(r.unmet_shifts[0].assigned, 0);
}

#[test]
fn no_shifts_is_infeasible() {
    let r = solve(&request(vec![worker("alice", 40.0, &[])], vec![]));
    assert!(is_infeasible(&r));
    assert!(r.unmet_shifts.is_empty());
}

#[test]
fn worker_not_available_for_any_shift_is_infeasible() {
    // Alice has an empty availability list — the shift requiring 1 worker cannot be covered.
    let r = solve(&request(vec![worker("alice", 40.0, &[])], vec![shift("s1", 8.0, 1, 1)]));
    assert!(is_infeasible(&r));
}

#[test]
fn fewer_available_workers_than_minimum_required_is_infeasible() {
    // Shift needs 2 workers but only 1 is available for it.
    let r = solve(&request(vec![worker("alice", 40.0, &["s1"])], vec![shift("s1", 8.0, 2, 3)]));
    assert!(is_infeasible(&r));
}

#[test]
fn worker_hours_budget_exhausted_across_two_mandatory_shifts_is_infeasible() {
    // Alice has 4 h. Both morning and afternoon are 4 h each and both require 1 worker.
    // She can cover one but not both — infeasible because both are mandatory.
    let r = solve(
        &request(
            vec![worker("alice", 4.0, &["morning", "afternoon"])],
            vec![shift("morning", 4.0, 1, 1), shift("afternoon", 4.0, 1, 1)]
        )
    );
    assert!(is_infeasible(&r));
}

#[test]
fn availability_mismatch_across_all_workers_is_infeasible() {
    // Three workers, none available for the only shift.
    let r = solve(
        &request(
            vec![
                worker("alice", 40.0, &["other"]),
                worker("bob", 40.0, &["other"]),
                worker("carol", 40.0, &[])
            ],
            vec![shift("s1", 8.0, 1, 2)]
        )
    );
    assert!(is_infeasible(&r));
}

// ═══════════════════════════════════════════════════════════════════════════════
// Optimal / feasible scenarios
// ═══════════════════════════════════════════════════════════════════════════════

#[test]
fn single_worker_single_shift_baseline() {
    let r = solve(&request(vec![worker("alice", 8.0, &["s1"])], vec![shift("s1", 8.0, 1, 1)]));
    assert!(is_optimal(&r));
    assert_eq!(r.assignments.len(), 1);
    assert!(assigned(&r, "alice", "s1"));
}

#[test]
fn higher_preference_worker_is_selected_over_lower() {
    // Two workers compete for 1 slot. Bob's preference is 4× higher → Bob wins.
    let r = solve(
        &request(
            vec![
                worker_with_prefs("alice", 40.0, &["s1"], &[("s1", 0.5)]),
                worker_with_prefs("bob", 40.0, &["s1"], &[("s1", 2.0)])
            ],
            vec![shift("s1", 8.0, 1, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert_eq!(r.assignments.len(), 1);
    assert!(assigned(&r, "bob", "s1"));
    assert!(approx_eq(r.objective_value, 2.0));
}

#[test]
fn max_workers_cap_is_respected() {
    // Three candidates for one slot — exactly one should be assigned.
    let r = solve(
        &request(
            vec![
                worker("alice", 40.0, &["s1"]),
                worker("bob", 40.0, &["s1"]),
                worker("carol", 40.0, &["s1"])
            ],
            vec![shift("s1", 8.0, 1, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert_eq!(count_for_shift(&r, "s1"), 1);
}

#[test]
fn multiple_shifts_with_exclusive_availability_all_covered() {
    // Each worker is only available for their own shift → forced 1-to-1 mapping.
    let r = solve(
        &request(
            vec![
                worker("alice", 40.0, &["morning"]),
                worker("bob", 40.0, &["afternoon"]),
                worker("carol", 40.0, &["evening"])
            ],
            vec![
                shift("morning", 8.0, 1, 1),
                shift("afternoon", 8.0, 1, 1),
                shift("evening", 8.0, 1, 1)
            ]
        )
    );
    assert!(is_optimal(&r));
    assert_eq!(r.assignments.len(), 3);
    assert!(assigned(&r, "alice", "morning"));
    assert!(assigned(&r, "bob", "afternoon"));
    assert!(assigned(&r, "carol", "evening"));
}

#[test]
fn single_worker_covers_two_shifts_within_hours_budget() {
    // Alice has 16 h — enough to cover both 8-hour shifts back to back.
    let r = solve(
        &request(
            vec![worker("alice", 16.0, &["morning", "afternoon"])],
            vec![shift("morning", 8.0, 1, 1), shift("afternoon", 8.0, 1, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert!(assigned(&r, "alice", "morning"));
    assert!(assigned(&r, "alice", "afternoon"));
}

#[test]
fn preferences_drive_cross_shift_assignment_to_maximum() {
    // Alice prefers morning; Bob prefers afternoon.
    // Both are available for both shifts. Each shift takes exactly 1 worker.
    // Optimal: Alice→morning (2.0) + Bob→afternoon (2.0) = 4.0.
    // Suboptimal would be swapping them for a total of 1.0.
    let r = solve(
        &request(
            vec![
                worker_with_prefs(
                    "alice",
                    40.0,
                    &["morning", "afternoon"],
                    &[
                        ("morning", 2.0),
                        ("afternoon", 0.5),
                    ]
                ),
                worker_with_prefs(
                    "bob",
                    40.0,
                    &["morning", "afternoon"],
                    &[
                        ("morning", 0.5),
                        ("afternoon", 2.0),
                    ]
                )
            ],
            vec![shift("morning", 8.0, 1, 1), shift("afternoon", 8.0, 1, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert!(assigned(&r, "alice", "morning"));
    assert!(assigned(&r, "bob", "afternoon"));
    assert!(approx_eq(r.objective_value, 4.0));
}

#[test]
fn objective_value_matches_sum_of_assigned_preferences() {
    // Disjoint availability → forced assignments, objective must equal 1.5 + 3.0.
    let r = solve(
        &request(
            vec![
                worker_with_prefs("alice", 40.0, &["s1"], &[("s1", 1.5)]),
                worker_with_prefs("bob", 40.0, &["s2"], &[("s2", 3.0)])
            ],
            vec![shift("s1", 8.0, 1, 1), shift("s2", 8.0, 1, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert!(approx_eq(r.objective_value, 4.5));
}

#[test]
fn optional_shift_skipped_when_worker_hours_are_exhausted() {
    // s1 is mandatory (min=1); s2 is optional (min=0).
    // Alice has exactly 8 h — enough for s1 but not both.
    // Expected: s1 covered, s2 left empty (still optimal because s2 is optional).
    let r = solve(
        &request(
            vec![worker("alice", 8.0, &["s1", "s2"])],
            vec![shift("s1", 8.0, 1, 1), shift("s2", 8.0, 0, 1)]
        )
    );
    assert!(is_optimal(&r));
    assert!(assigned(&r, "alice", "s1"));
    assert!(!assigned(&r, "alice", "s2"));
}

#[test]
fn shift_with_zero_minimum_is_not_required() {
    // No worker is available for s2, but min=0 so it's fine.
    let r = solve(
        &request(
            vec![worker("alice", 40.0, &["s1"])],
            vec![
                shift("s1", 8.0, 1, 1),
                shift("s2", 8.0, 0, 2) // nobody available, but not required
            ]
        )
    );
    assert!(is_optimal(&r));
    assert!(assigned(&r, "alice", "s1"));
    assert_eq!(count_for_shift(&r, "s2"), 0);
}

#[test]
fn assignments_are_returned_in_stable_order() {
    // Assignments must be sorted shift-first then worker so callers can rely on order.
    let r = solve(
        &request(
            vec![worker("zara", 40.0, &["s1"]), worker("alice", 40.0, &["s1"])],
            vec![shift("s1", 8.0, 2, 2)]
        )
    );
    assert!(is_optimal(&r));
    assert_eq!(r.assignments.len(), 2);
    assert_eq!(r.assignments[0].worker_id, "alice");
    assert_eq!(r.assignments[1].worker_id, "zara");
}

// ═══════════════════════════════════════════════════════════════════════════════
// Sick-call stability — morning / afternoon / evening schedule
//
// Five workers with skewed preferences across three shifts.  The gaps between
// each primary worker's dominant preference and every alternative are large
// enough that the LP's Day-1 optimum is unique, which in turn makes the
// Day-2 stable-assignment assertions mathematically forced rather than
// coincidental.
//
//           morning  afternoon  evening
//  alice      5.0       1.0       0.5    primary: morning
//  bob        1.0       4.5       0.5    primary: afternoon
//  carol      0.5       1.0       4.5    primary: evening
//  dave       2.0       1.5       1.0    backup — best morning replacement
//  eve        1.5       1.0       2.0    backup — second-best morning replacement
//
// Day-1 unique optimum : alice(5.0) + bob(4.5) + carol(4.5) = 14.0
// Day-2 (alice absent) : bob(4.5)  + carol(4.5) + dave(2.0) = 11.0
// ═══════════════════════════════════════════════════════════════════════════════

const MAE: &[&str] = &["morning", "afternoon", "evening"];

fn mae_shifts() -> Vec<Shift> {
    vec![shift("morning", 8.0, 1, 1), shift("afternoon", 8.0, 1, 1), shift("evening", 8.0, 1, 1)]
}

fn full_roster() -> Vec<Worker> {
    vec![
        worker_with_prefs(
            "alice",
            40.0,
            MAE,
            &[
                ("morning", 5.0),
                ("afternoon", 1.0),
                ("evening", 0.5),
            ]
        ),
        worker_with_prefs(
            "bob",
            40.0,
            MAE,
            &[
                ("morning", 1.0),
                ("afternoon", 4.5),
                ("evening", 0.5),
            ]
        ),
        worker_with_prefs(
            "carol",
            40.0,
            MAE,
            &[
                ("morning", 0.5),
                ("afternoon", 1.0),
                ("evening", 4.5),
            ]
        ),
        worker_with_prefs(
            "dave",
            40.0,
            MAE,
            &[
                ("morning", 2.0),
                ("afternoon", 1.5),
                ("evening", 1.0),
            ]
        ),
        worker_with_prefs(
            "eve",
            40.0,
            MAE,
            &[
                ("morning", 1.5),
                ("afternoon", 1.0),
                ("evening", 2.0),
            ]
        )
    ]
}

fn roster_without(sick_id: &str) -> Vec<Worker> {
    full_roster()
        .into_iter()
        .filter(|w| w.id != sick_id)
        .collect()
}

// ── sick-call tests ───────────────────────────────────────────────────────────

/// The solver has no explicit stability constraint.  Stability emerges from the
/// preference gaps: once a primary worker is removed, no reshuffling of the
/// remaining workers can beat keeping everyone in their dominant slot.
#[test]
fn sick_call_preserves_all_other_assignments() {
    let sick = "alice";

    let day1 = solve(&request(full_roster(), mae_shifts()));
    assert!(is_optimal(&day1), "day-1 must be solvable");

    // Record every (worker, shift) pair that doesn't involve the absent worker.
    let stable: HashSet<(String, String)> = day1.assignments
        .iter()
        .filter(|a| a.worker_id != sick)
        .map(|a| (a.worker_id.clone(), a.shift_id.clone()))
        .collect();

    let day2 = solve(&request(roster_without(sick), mae_shifts()));
    assert!(is_optimal(&day2), "day-2 must still be solvable — backup workers exist");

    // Every stable assignment from day 1 must survive unchanged in day 2.
    for (worker_id, shift_id) in &stable {
        assert!(
            assigned(&day2, worker_id, shift_id),
            "worker '{worker_id}' was moved off '{shift_id}' after the sick call — schedule stability violated"
        );
    }

    // The absent worker must not appear in the day-2 schedule.
    assert!(
        day2.assignments.iter().all(|a| a.worker_id != sick),
        "'{sick}' appeared in the day-2 schedule despite calling in sick"
    );

    // Losing the best morning worker (pref 5.0 → backup 2.0) must hurt the objective.
    assert!(
        day2.objective_value < day1.objective_value,
        "objective should decrease after sick call ({:.1} → {:.1})",
        day1.objective_value,
        day2.objective_value
    );
}

/// Parametric version: removing *any* primary worker forces a worse replacement
/// and therefore a strictly lower objective — regardless of which shift is affected.
#[test]
fn sick_call_objective_decreases_for_every_primary_worker() {
    let day1 = solve(&request(full_roster(), mae_shifts()));
    assert!(is_optimal(&day1));
    let baseline = day1.objective_value;

    for sick in ["alice", "bob", "carol"] {
        let day_n = solve(&request(roster_without(sick), mae_shifts()));
        assert!(
            is_optimal(&day_n),
            "schedule should remain feasible when {sick} calls in sick (backup workers exist)"
        );
        assert!(
            day_n.objective_value < baseline,
            "objective should drop when {sick} is sick ({baseline:.1} → {:.1})",
            day_n.objective_value
        );
    }
}

/// Among the two backup workers available for morning, the solver must choose the
/// one with the higher preference score (dave 2.0 > eve 1.5).
#[test]
fn sick_call_selects_highest_preference_replacement() {
    let day2 = solve(&request(roster_without("alice"), mae_shifts()));
    assert!(is_optimal(&day2));
    assert!(
        assigned(&day2, "dave", "morning"),
        "dave (morning pref 2.0) should cover morning — highest among remaining workers"
    );
    assert!(
        !assigned(&day2, "eve", "morning"),
        "eve (morning pref 1.5) should not cover morning when dave is available"
    );
}

/// When there are no backup workers and the only person who can cover a shift
/// calls in sick, the problem must be reported as infeasible — not silently
/// dropped or panicked.
#[test]
fn sick_call_with_no_backup_is_infeasible() {
    // Three primary workers, each available only for their own shift.
    let primary_only = vec![
        worker_with_prefs("alice", 40.0, &["morning"], &[("morning", 5.0)]),
        worker_with_prefs("bob", 40.0, &["afternoon"], &[("afternoon", 4.5)]),
        worker_with_prefs("carol", 40.0, &["evening"], &[("evening", 4.5)])
    ];

    // alice calls in sick — morning has zero available workers.
    let day2 = solve(
        &request(
            primary_only
                .into_iter()
                .filter(|w| w.id != "alice")
                .collect(),
            mae_shifts()
        )
    );
    assert!(is_infeasible(&day2));
}

// ═══════════════════════════════════════════════════════════════════════════════
// Random-preference stability audit
//
// Preferences are drawn near 1.0, to test that stability is kept even with
// minimal preference differences. For flakiness reasons, this test only fails if
// more than a fixed percentage of sick calls cause cascades.
// Any cascades are however logged.
// ═══════════════════════════════════════════════════════════════════════════════

const WORKER_IDS: &[&str] = &["alice", "bob", "carol", "dave", "eve"];
const DAYS: &[&str] = &["mon", "tue", "wed", "thu", "fri"];
const NUM_DAYS: usize = 5;
const SINGLE_CASCADE_FAIL_THRESHOLD: f64 = 0.5; //50%, each length of cascade should be half as likely as the previous one

/// Minimal deterministic LCG — no external crate, fixed seed → reproducible runs.
struct Lcg(u64);

impl Lcg {
    fn next_f64(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 11) as f64) / ((1u64 << 53) as f64)
    }

    /// Uniform sample from [centre − half_width, centre + half_width].
    fn near(&mut self, centre: f64, half_width: f64) -> f64 {
        centre + (self.next_f64() * 2.0 - 1.0) * half_width
    }
}

fn mae_shifts_week() -> Vec<Shift> {
    let mut days = Vec::with_capacity(NUM_DAYS);
    for i in 0..NUM_DAYS {
        days.push(DAYS[i % DAYS.len()]);
    }
    days.iter()
        .flat_map(|d| MAE.iter().map(move |t| format!("{}-{}", d, t)))
        .map(|id| shift(&id, 8.0, 1, 1))
        .collect()
}

/// Build a worker roster from a [worker][shift] preference matrix.
/// `exclude` is the index into `WORKER_IDS` of the absent (sick) worker.
fn roster_from_prefs(
    prefs: &[[f64; MAE.len() * NUM_DAYS]; 5],
    exclude: Option<(usize, usize)>
) -> Vec<Worker> {
    let shifts = mae_shifts_week();
    WORKER_IDS.iter()
        .zip(prefs)
        .enumerate()
        .filter(|&(i, _)| exclude.map_or(true, |(e, _)| i != e))
        .map(|(i, (id, ps))| {
            let p = shifts
                .iter()
                .zip(ps.iter())
                .map(|(s, p)| (s.id.as_str(), *p))
                .collect::<Vec<_>>();
            worker_with_prefs(
                id,
                40.0,
                exclude
                    .filter(|&(e, _)| e == i)
                    .map(|_| -> &[&str] { &[] })
                    .unwrap_or(
                        &shifts
                            .iter()
                            .map(|s| s.id.as_str())
                            .collect::<Vec<_>>()
                    ),
                &p
            )
        })
        .collect()
}

fn fmt_prefs(prefs: &[[f64; MAE.len() * NUM_DAYS]; 5]) -> String {
    WORKER_IDS.iter()
        .enumerate()
        .map(|(i, id)| {
            let scores = prefs[i]
                .iter()
                .map(|p| format!("{p:.2}"))
                .collect::<Vec<_>>()
                .join("/");
            format!("{id}=[{scores}]")
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn fmt_assignments(r: &SolveResponse) -> String {
    if r.assignments.is_empty() {
        return "(none)".into();
    }
    r.assignments
        .iter()
        .map(|a| format!("{}→{}", a.worker_id, a.shift_id))
        .collect::<Vec<_>>()
        .join(", ")
}
use std::fs::OpenOptions;
use std::io::Write;

#[test]
fn sick_call_stability_audit_with_random_preferences() {
    for seed in tqdm::tqdm(0..10).desc(Some("Seeds")) {
        const ITERATIONS: usize = 500;
        let mut rng = Lcg(0xdead_beef_cafe_babe + seed);
        let mut cascade_totals = [0usize; 15];
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open("cascades.log")
            .expect("failed to open cascades.log");
        let mut instabilities = Vec::new();
        // let shifts2 = mae_shifts_week().split_off(3);

        for iter in tqdm::tqdm(0..500).desc(Some("Iterations")) {
            // 5 workers × 3 shifts × 5 days, all preferences drawn from U[0.7, 1.3]
            let mut prefs = [[0.0f64; MAE.len() * NUM_DAYS]; 5];
            for row in &mut prefs {
                for cell in row.iter_mut() {
                    *cell = rng.near(1.0, 0.3).max(0.05);
                }
            }

            let day1_plan = solve(&request(roster_from_prefs(&prefs, None), mae_shifts_week()));
            if !is_optimal(&day1_plan) {
                continue;
            }
            let day2_unsick_plan = solve(
                &request(roster_from_prefs(&prefs, None), mae_shifts_week())
            );
            assert!(is_optimal(&day2_unsick_plan), "day-2 must be solvable without the sick call");
            // in fact it should be the same as day 1's plan
            let instability: i32 = day2_unsick_plan.assignments
                .iter()
                .skip(3)
                .map(|a| if assigned(&day1_plan, &a.worker_id, &a.shift_id) { 0i32 } else { 1i32 })
                .sum();
            if instability > 0 {
                instabilities.push(instability);
                continue;
            }

            for (sick_idx, sick_id) in WORKER_IDS.iter().enumerate() {
                // Only interesting when the absent worker was actually on the schedule.
                if !day1_plan.assignments.iter().any(|a| a.worker_id == *sick_id) {
                    continue;
                }

                let day2_plan = solve(
                    &request(roster_from_prefs(&prefs, Some((sick_idx, 2))), mae_shifts_week())
                );
                if !is_optimal(&day2_plan) {
                    continue; // infeasible sick calls are covered by dedicated tests
                }

                let stable_assignments: Vec<_> = day2_unsick_plan.assignments
                    .iter()
                    .filter(|a| a.worker_id != *sick_id)
                    .map(|a| (a.worker_id.as_str(), a.shift_id.as_str()))
                    .collect();
                let mut cascades = Vec::new();
                for (worker_id, shift_id) in stable_assignments {
                    if !assigned(&day2_plan, worker_id, shift_id) {
                        cascades.push(format!("'{worker_id}' off '{shift_id}'"));
                    }
                }

                cascade_totals[cascades.len()] += 1;
                if !cascades.is_empty() {
                    writeln!(
                        f,
                        "iter {iter}: sick='{sick_id}' prefs=[{}] day1=[{}] day2=[{}] cascades={}",
                        fmt_prefs(&prefs),
                        fmt_assignments(&day1_plan),
                        fmt_assignments(&day2_plan),
                        cascades.join(", ")
                    ).expect("failed to write to cascades.log");
                }
            }
        }

        let actual_iterations = (ITERATIONS - instabilities.len()) * WORKER_IDS.len();
        for i in 0..cascade_totals.len() {
            let failure_rate = (cascade_totals[i] as f64) / (actual_iterations as f64);
            let failure_threshold = SINGLE_CASCADE_FAIL_THRESHOLD.powi(i.try_into().unwrap());
            assert!(
                failure_rate <= failure_threshold,
                "failure rate {:.1}% exceeds threshold of {:.1}% ({} {i}-length cascades in {actual_iterations} iterations)",
                failure_rate * 100.0,
                failure_threshold * 100.0,
                cascade_totals[i]
            );
            eprintln!(
                "failure rate {:.1}% under threshold of {:.1}% ({} {i}-length cascades in {actual_iterations} iterations)",
                failure_rate * 100.0,
                failure_threshold * 100.0,
                cascade_totals[i]
            );
        }
    }
}
