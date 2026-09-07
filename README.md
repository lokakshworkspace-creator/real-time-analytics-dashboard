# Real-Time Data Analytics Dashboard

> Status: Phase 2 (backend foundation) complete. Ingest endpoint validates and stores only — no anomaly detection yet, no simulator, no frontend.

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
/backend
  app/
    main.py       FastAPI app, CORS, lifespan (Mongo connect/close), GET /health
    config.py     Settings (reads .env at repo root, sane localhost defaults)
    database.py   Motor client lifecycle + get_database() dependency
    models.py     MetricIn / MetricOut (Pydantic v2) + ObjectId→str conversion
    routers/
      metrics.py  POST /api/metrics (validate + store only — no detection yet)
  requirements.txt
/frontend   React (Vite) dashboard (not yet implemented)
docker-compose.yml   Local MongoDB
.env.example         Environment variable template
```

## Setup

_To be filled in as each phase lands._

### Local MongoDB (Docker)

```bash
docker compose up -d
```

This starts MongoDB on `localhost:27017` with a persisted volume, database name `analytics`.

Copy `.env.example` to `.env` and adjust values as needed. The backend's defaults already match this compose file, so `.env` is optional for local dev — only needed to override something (e.g. an Atlas URI).

> **Port conflict note:** if you already have MongoDB installed natively on this machine (e.g. running as a Windows service on `27017`), it will silently win over the Docker container for any connection to `localhost:27017` / `127.0.0.1:27017`, since the native install binds that address specifically while Docker's proxy binds the wildcard address. `docker compose ps` looking healthy does **not** mean the app is actually talking to it. Either stop the native MongoDB service while working on this project, or remap the container's port in `docker-compose.yml` (e.g. `"27018:27017"`) and point `MONGODB_URI` at the new port.

### Backend (FastAPI)

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

The API runs at `http://localhost:8000`. Interactive docs (Swagger UI) at `http://localhost:8000/docs`.

**Manual test:**

```bash
# Health check — confirms the API is up and can reach MongoDB
curl http://localhost:8000/health

# Ingest a metric
curl -X POST http://localhost:8000/api/metrics \
  -H "Content-Type: application/json" \
  -d '{"metric":"cpu_usage","value":72.5,"source":"server-2"}'
```

Or use `/docs` and try `POST /api/metrics` from the browser — it has a built-in "Try it out" form.

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
2. ✅ Backend foundation (FastAPI app, Motor connection, Pydantic models, `POST /api/metrics`, CORS)
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

**Phase 2:**
- `metric` is a `Literal` of the five known metric names, not a bare `str`. Catches typos/garbage at the API boundary; trade-off is that adding a metric later means editing the type.
- MongoDB's `_id` (BSON `ObjectId`) is converted to a plain string at one boundary function (`metric_document_to_out`) rather than writing a custom Pydantic `ObjectId` type — simpler to read and explain, and it's the only place in the codebase that needs to know Mongo's id representation.
- Motor connect/close is wired into FastAPI's `lifespan` context (not opened lazily per-request), so a bad `MONGODB_URI` fails fast at startup instead of on the first request.
- `anomaly` is hard-coded `false` at write time for now, not left unset — keeps `MetricOut`'s shape stable across phases so the frontend contract set up in Phase 6 won't need to change when Phase 5 wires in real detection.

## What I built vs what I'd add next

_To be written in Phase 10._
