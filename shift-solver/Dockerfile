# Build context must be the workspace root (autoshift/), not this directory.
# From frappe_docker compose: context: ../autoshift
# ── build stage ───────────────────────────────────────────────────────────────
FROM rust:slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    coinor-libcbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy workspace manifest and member manifest before source so Docker can
# cache the dependency compilation layer independently of source changes.
COPY Cargo.toml ./
COPY shift-solver/Cargo.toml ./shift-solver/
RUN mkdir -p shift-solver/src \
    && echo "fn main() {}" > shift-solver/src/main.rs \
    && cargo build --release \
    && rm -rf shift-solver/src

COPY shift-solver/src ./shift-solver/src
RUN touch shift-solver/src/main.rs && cargo build --release

# ── runtime stage ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    coinor-libcbc3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/target/release/shift-solver /usr/local/bin/shift-solver

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["shift-solver"]
