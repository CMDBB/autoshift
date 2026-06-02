use axum::{
    extract::Json,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Router,
};
use std::net::SocketAddr;
use tracing::info;

#[warn(unused_extern_crates)]
#[forbid(unsafe_code)]

mod models;
mod solver;
#[cfg(test)]
mod tests;

async fn handle_solve(Json(req): Json<models::SolveRequest>) -> impl IntoResponse {
    info!(workers = req.workers.len(), shifts = req.shifts.len(), "solve request");
    let resp = solver::solve(&req);
    (StatusCode::OK, Json(resp))
}

async fn handle_health() -> &'static str {
    "ok"
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "shift_solver=info".into()),
        )
        .init();

    let app = Router::new()
        .route("/solve", post(handle_solve))
        .route("/health", get(handle_health));

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    info!("shift-solver listening on {addr}");

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
