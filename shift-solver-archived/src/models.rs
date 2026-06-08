use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Clone)]
pub struct Worker {
    pub id: String,
    /// Maximum hours this worker may be scheduled in this planning window
    pub max_hours: f64,
    /// IDs of shifts this worker is available for
    pub availability: Vec<String>,
    /// shift_id -> preference score (0.0 neutral, higher = more desired)
    #[serde(default)]
    pub preferences: HashMap<String, f64>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Shift {
    pub id: String,
    pub duration_hours: f64,
    pub min_workers: usize,
    pub max_workers: usize,
}

#[derive(Debug, Deserialize, Clone)]
pub struct SolveRequest {
    pub workers: Vec<Worker>,
    pub shifts: Vec<Shift>,
}

// ── response types ────────────────────────────────────────────────────────────

#[derive(Debug, Serialize, Clone)]
pub struct Assignment {
    pub worker_id: String,
    pub shift_id: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
pub enum SolveStatus {
    Optimal,
    Infeasible,
    Error,
}

#[derive(Debug, Serialize, Clone)]
pub struct UnmetShift {
    pub shift_id: String,
    pub assigned: usize,
    pub required: usize,
}

#[derive(Debug, Serialize, Clone)]
pub struct SolveResponse {
    pub status: SolveStatus,
    pub assignments: Vec<Assignment>,
    pub objective_value: f64,
    /// Populated on infeasible: shifts that could not be fully staffed
    pub unmet_shifts: Vec<UnmetShift>,
}
