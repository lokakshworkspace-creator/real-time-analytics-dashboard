# Project Brief: Real-Time Data Analytics Dashboard

Paste this whole file into Claude Code as your first message (or save it as `CLAUDE.md` in the repo root so it's picked up automatically). Build it **phase by phase** — stop after each phase, show me what was built, and wait for me to say "continue" before moving to the next one. Do not skip ahead or build multiple phases in one shot.

## What we're building

A full-stack real-time analytics platform that ingests streaming metric events, stores them in MongoDB via a FastAPI backend, runs statistical anomaly detection, and visualizes trends + alerts in a React dashboard.

This is a portfolio project for a Data Intelligence interview. Code quality, clear separation of concerns, and defensible technical choices matter more than feature count. Every design decision must have a real reason behind it — no cargo-culting ("MongoDB because NoSQL is fast" is not acceptable reasoning anywhere in code comments or README).

## Tech stack (locked in — don't substitute)

- **Backend:** FastAPI, Pydantic v2, Motor (async MongoDB driver)
- **Database:** MongoDB (local via Docker, or Atlas free tier — ask me which before Phase 2)
- **Frontend:** React (Vite), Recharts for charts, plain `fetch` + `useState`/`useEffect` for data (no Redux — state graph doesn't justify it)
- **Anomaly detection:** z-score for MVP; Isolation Forest (scikit-learn) as a Phase 5b stretch goal, clearly separated so I can talk about both honestly
- **Real-time updates:** polling on a fixed interval (5s), with a documented rationale (stateless backend, trivial horizontal scaling, sufficient for this update frequency) — not WebSockets/SSE for MVP

## Architecture

```
Simulator/Producer ──POST /api/metrics──▶ FastAPI ──▶ MongoDB
                                             │
                                             ▼
                                    Anomaly Detection
                                    (z-score, rolling window per metric+source)
                                             │
                                             ▼
                                       REST endpoints
                                             │
                                             ▼
                                   React Dashboard (polls every 5s)
```

Each layer talks only to its neighbours. React never touches MongoDB directly — FastAPI owns validation, business logic, and DB credentials.

## Data model

```json
{
  "timestamp": "2026-08-30T10:31:06Z",
  "metric": "response_time",
  "value": 987,
  "source": "server-2",
  "anomaly": true
}
```

Metrics to simulate: `orders`, `response_time`, `cpu_usage`, `failed_requests`, `memory_usage`.

Indexes:
- `{metric: 1, timestamp: -1}` — dominant query pattern (recent data for a given metric)
- `{anomaly: 1, timestamp: -1}` — anomaly panel

## Endpoints (MVP)

- `POST /api/metrics` — ingest one event, validate, run detection, store, return 201
- `GET /metrics/latest?limit=50` — most recent value per metric
- `GET /metrics/stats?metric=X&minutes=60` — aggregates via MongoDB aggregation pipeline ($match → $group), not Python-side computation
- `GET /metrics/anomalies?limit=50` — flagged events

## Anomaly detection (MVP)

Rolling window per `metric+source`, z-score against mean/stdev of that window, flag if `|z| > 3`. Guard against `std == 0` (constant series). Require a minimum window size before detection activates (cold start).

Detection runs **on write** — flag stored with the document, so reads are cheap. Document this trade-off (changing detection logic later requires reprocessing) in a code comment.

## Phase plan — work through these one at a time

1. **Scaffold** — repo structure (`/backend`, `/frontend`), README skeleton, `.env.example`, Docker Compose for local MongoDB. No business logic yet.
2. **Backend foundation** — FastAPI app, Motor connection, Pydantic `MetricIn`/`MetricOut` models, `POST /api/metrics` (no detection logic yet — just validate + store), CORS middleware for `localhost:5173`.
3. **Simulator** — a script that posts realistic events for the 5 metrics on an interval, with occasional deliberate spikes so anomalies have something to catch.
4. **Analytics endpoints** — `GET /metrics/latest`, `GET /metrics/stats` (aggregation pipeline), indexes created on startup.
5. **Anomaly detection** — z-score implementation wired into ingest, `GET /metrics/anomalies`. **5b (stretch):** Isolation Forest as an alternate/additional detector, kept clearly separate in code (e.g. `detectors/zscore.py` vs `detectors/isolation_forest.py`) so I can honestly say which one is actually running.
6. **React dashboard** — metric cards, trend chart (Recharts), anomaly panel, loading/error states.
7. **Polling layer** — `useEffect` + `setInterval` with proper cleanup (`clearInterval` on unmount).
8. **Polish** — error handling (422/404/500 paths), ObjectId serialization, basic unit tests for the z-score function (deterministic input → known output).
9. **Deployment** — Dockerize both services, deployment instructions (Atlas + a free host for the API + static hosting for the frontend).
10. **README** — architecture diagram, setup instructions, and an honest "what I built vs what I'd add next" section.

## Ground rules

- After each phase: summarize what was built, list any files added/changed, and explicitly flag anything that deviates from this brief or that you had to make a judgment call on.
- Don't add authentication, WebSockets, or Kafka — those are documented as "next steps," not built.
- Keep the z-score detector and Isolation Forest detector clearly separable in code — I need to know exactly which one is live for interview honesty.
- Write code I can actually explain line-by-line — favor clarity over cleverness.

Start with **Phase 1** now.