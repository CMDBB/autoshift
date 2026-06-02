use std::collections::HashMap;

use good_lp::{
    coin_cbc, constraint, variable, Expression, ProblemVariables, ResolutionError, Solution,
    SolverModel,
};

use crate::models::{Assignment, SolveRequest, SolveResponse, SolveStatus, UnmetShift};

pub fn solve(req: &SolveRequest) -> SolveResponse {
    let workers = &req.workers;
    let shifts = &req.shifts;

    if workers.is_empty() || shifts.is_empty() {
        let unmet = shifts
            .iter()
            .filter(|s| s.min_workers > 0)
            .map(|s| UnmetShift {
                shift_id: s.id.clone(),
                assigned: 0,
                required: s.min_workers,
            })
            .collect();
        return make_infeasible(unmet);
    }

    let mut vars = ProblemVariables::new();

    // Binary variable x[(i,j)] = 1 iff worker i is assigned to shift j.
    // Only created for (worker, shift) pairs where the worker is available.
    let mut x: HashMap<(usize, usize), good_lp::Variable> = HashMap::new();
    for (i, worker) in workers.iter().enumerate() {
        for (j, shift) in shifts.iter().enumerate() {
            if worker.availability.contains(&shift.id) {
                x.insert((i, j), vars.add(variable().binary()));
            }
        }
    }

    // Objective: maximise sum of preference scores for assigned pairs.
    // Falls back to 1.0 per assignment when no explicit preference is set.
    let objective: Expression = x
        .iter()
        .map(|((i, j), &var)| {
            let pref = workers[*i]
                .preferences
                .get(&shifts[*j].id)
                .copied()
                .unwrap_or(1.0);
            pref * var
        })
        .sum();

    let mut problem = vars.maximise(objective.clone()).using(coin_cbc);
    problem.set_parameter("logLevel", "0");

    // Shift coverage: min_workers <= sum_i x[i,j] <= max_workers
    for (j, shift) in shifts.iter().enumerate() {
        let assigned: Expression = (0..workers.len())
            .filter_map(|i| x.get(&(i, j)).copied())
            .map(Expression::from)
            .sum();

        problem = problem
            .with(constraint!(assigned.clone() >= shift.min_workers as f64))
            .with(constraint!(assigned <= shift.max_workers as f64));
    }

    // Worker hours: sum_j duration[j] * x[i,j] <= max_hours[i]
    for (i, worker) in workers.iter().enumerate() {
        let hours: Expression = (0..shifts.len())
            .filter_map(|j| x.get(&(i, j)).map(|&var| shifts[j].duration_hours * var))
            .sum();

        problem = problem.with(constraint!(hours <= worker.max_hours));
    }

    match problem.solve() {
        Ok(solution) => {
            let mut assignments: Vec<Assignment> = x
                .iter()
                .filter_map(|((i, j), &var)| {
                    (solution.value(var) > 0.5).then(|| Assignment {
                        worker_id: workers[*i].id.clone(),
                        shift_id: shifts[*j].id.clone(),
                    })
                })
                .collect();

            // Stable order: sort by shift then worker so the output is deterministic
            assignments.sort_by(|a, b| {
                a.shift_id
                    .cmp(&b.shift_id)
                    .then(a.worker_id.cmp(&b.worker_id))
            });

            SolveResponse {
                status: SolveStatus::Optimal,
                assignments,
                objective_value: solution.eval(objective),
                unmet_shifts: vec![],
            }
        }
        Err(ResolutionError::Infeasible) => make_infeasible(vec![]),
        Err(e) => {
            tracing::error!("CBC solver error: {e:?}");
            SolveResponse {
                status: SolveStatus::Error,
                assignments: vec![],
                objective_value: 0.0,
                unmet_shifts: vec![],
            }
        }
    }
}

fn make_infeasible(unmet_shifts: Vec<UnmetShift>) -> SolveResponse {
    SolveResponse {
        status: SolveStatus::Infeasible,
        assignments: vec![],
        objective_value: 0.0,
        unmet_shifts,
    }
}
