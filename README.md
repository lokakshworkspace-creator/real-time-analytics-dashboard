# Real-Time Data Analytics Dashboard

> Status: Phase 1 (scaffold) complete. Business logic not yet implemented.

A full-stack real-time analytics platform that ingests streaming metric events, stores them in MongoDB via a FastAPI backend, runs statistical anomaly detection, and visualizes trends and alerts in a React dashboard.

## Architecture

_Diagram to be added in Phase 10._

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

## Tech stack

- **Backend:** FastAPI, Pydantic v2, Motor (async MongoDB driver)
- **Database:** MongoDB (local via Docker Compose, or Atlas free tier)
- **Frontend:** React (Vite), Recharts, `fetch` + `useState`/`useEffect`
- **Anomaly detection:** z-score (MVP), Isolation Forest (stretch goal, kept separate)
- **Real-time updates:** polling every 5s (stateless backend, trivial horizontal scaling — see rationale in Phase 5/7 notes once written)

## Repository structure

```
/backend    FastAPI application (not yet implemented)
/frontend   React (Vite) dashboard (not yet implemented)
docker-compose.yml   Local MongoDB
.env.example         Environment variable template
```

## Setup

_To be filled in as each phase lands. Currently only local MongoDB is available._

### Local MongoDB (Docker)

```bash
docker compose up -d
```

This starts MongoDB on `localhost:27017` with a persisted volume, database name `analytics`.

Copy `.env.example` to `.env` and adjust values as needed.

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

Simulated metrics: `orders`, `response_time`, `cpu_usage`, `failed_requests`, `memory_usage`.

## API (planned)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/metrics` | Ingest one event, validate, run detection, store |
| GET | `/metrics/latest?limit=50` | Most recent value per metric |
| GET | `/metrics/stats?metric=X&minutes=60` | Aggregates via MongoDB aggregation pipeline |
| GET | `/metrics/anomalies?limit=50` | Flagged events |

Not implemented yet — see Phase plan below.

## Phase plan

1. ✅ Scaffold (repo structure, README skeleton, `.env.example`, Docker Compose for local MongoDB)
2. ⬜ Backend foundation (FastAPI app, Motor connection, Pydantic models, `POST /api/metrics`, CORS)
3. ⬜ Simulator (posts realistic events on an interval, with deliberate spikes)
4. ⬜ Analytics endpoints (`/metrics/latest`, `/metrics/stats`, indexes)
5. ⬜ Anomaly detection (z-score on ingest, `/metrics/anomalies`); 5b: Isolation Forest (stretch)
6. ⬜ React dashboard (metric cards, trend chart, anomaly panel, loading/error states)
7. ⬜ Polling layer (`useEffect` + `setInterval`, cleanup on unmount)
8. ⬜ Polish (error handling, ObjectId serialization, unit tests for z-score)
9. ⬜ Deployment (Docker for both services, Atlas + free API host + static frontend hosting)
10. ⬜ Final README (architecture diagram, setup instructions, what I built vs what I'd add next)

## Design decisions

Documented here as each phase introduces a real trade-off (not before — no decisions without code behind them yet).

## What I built vs what I'd add next

_To be written in Phase 10._
