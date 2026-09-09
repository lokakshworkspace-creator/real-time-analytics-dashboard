# Real-Time Data Analytics Dashboard

> Status: Phase 4 (analytics endpoints) complete. Backend ingests, stores, indexes, and now serves read-side aggregates (`/metrics/latest`, `/metrics/stats`). No anomaly detection yet, no frontend.

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
    database.py   Motor client lifecycle + get_database() dependency + ensure_indexes()
    models.py     MetricIn/MetricOut/LatestMetric/MetricStats (Pydantic v2) + ObjectId→str conversion
    routers/
      metrics.py    POST /api/metrics (validate + store only — no detection yet)
      analytics.py  GET /metrics/latest, GET /metrics/stats (aggregation pipeline)
  simulator.py  Standalone synthetic event producer, POSTs to the API over HTTP
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

Copy `.env.example` to `.env` and adjust values as needed. The backend's defaults already match this compose file, so `.env` is optional for local dev — only needed to override something (e.g. an Atlas URI). `.env` is git-ignored (see `.gitignore`); each clone needs its own copy.

> **Before you start the container:** if you have MongoDB installed natively on this machine, read [Known issues / gotchas](#known-issues--gotchas) below — it can silently shadow the Docker container on the same port.

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

### Simulator (synthetic data)

```bash
cd backend
.venv\Scripts\activate          # if not already active
python simulator.py
```

With the backend running, this posts one metric event per tick to `POST /api/metrics` over HTTP — it's a client of the API, not something that writes to MongoDB directly, so it exercises the exact same validation/storage path a real producer would. Runs until you press Ctrl+C.

Options:
```bash
python simulator.py --interval 1        # seconds between events (default: 1.5)
python simulator.py --duration 60       # stop automatically after N seconds (default: run forever)
python simulator.py --url http://localhost:8000   # backend base URL (default shown)
```

Each event is one of the 5 metrics for a random source (`server-1`/`server-2`/`server-3`), fluctuating within a believable range. Roughly every 30–50 events it deliberately injects a spike — a value clearly outside that metric's normal range (e.g. `response_time` jumping from ~150–250ms to 800–1500ms) — so there's something for the anomaly detector built in Phase 5 to actually find. If the backend isn't reachable, failed posts are logged and the simulator keeps running rather than crashing.

> **This is synthetic data**, generated by this script for demo purposes — not live production traffic, and not measurements from any real system. Baseline ranges and spike magnitudes were chosen to *look* plausible, not derived from any dataset. Said plainly here so it's never mistaken for something it isn't.

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

## API

| Method | Path | Purpose | Status |
|---|---|---|---|
| POST | `/api/metrics` | Ingest one event, validate, store | ✅ (detection still a no-op — Phase 5) |
| GET | `/metrics/latest?source=` | Most recent document per metric (optionally filtered by source) | ✅ |
| GET | `/metrics/stats?metric=X&minutes=60` | count/avg/min/max for one metric over a trailing window, via aggregation pipeline | ✅ |
| GET | `/metrics/anomalies?limit=50` | Flagged events | ⬜ Phase 5 — no anomaly data exists yet, so there's nothing to build here until then |

Note the deliberate path inconsistency: ingestion is `/api/metrics`, the three read endpoints are bare `/metrics/...`. That's what CLAUDE.md's own endpoint list specifies, not an oversight — kept as-is rather than "fixed".

**`GET /metrics/latest` example:**
```bash
curl http://localhost:8000/metrics/latest
curl "http://localhost:8000/metrics/latest?source=server-2"
```
Returns a JSON array with **up to 5 entries** — one per metric that has ever received an event, each the single most-recent document for that metric (optionally scoped to one source). A metric with zero events is omitted, not padded with a placeholder.

**`GET /metrics/stats` example:**
```bash
curl "http://localhost:8000/metrics/stats?metric=cpu_usage&minutes=60"
# {"metric":"cpu_usage","minutes":60,"count":24,"avg":56.4,"min":40.0,"max":98.5}
```
`minutes` defaults to 60 if omitted. Returns **404** if there's no data for that metric in the window (not a 200 with zeroed-out numbers — see Design decisions).

## Phase plan

1. ✅ Scaffold (repo structure, README skeleton, `.env.example`, Docker Compose for local MongoDB)
2. ✅ Backend foundation (FastAPI app, Motor connection, Pydantic models, `POST /api/metrics`, CORS)
3. ✅ Simulator (posts realistic events on an interval, with deliberate spikes)
4. ✅ Analytics endpoints (`/metrics/latest`, `/metrics/stats`, indexes)
5. ⬜ Anomaly detection (z-score on ingest, `/metrics/anomalies`); 5b: Isolation Forest (stretch)
6. ⬜ React dashboard (metric cards, trend chart, anomaly panel, loading/error states)
7. ⬜ Polling layer (`useEffect` + `setInterval`, cleanup on unmount)
8. ⬜ Polish (error handling, ObjectId serialization, unit tests for z-score)
9. ⬜ Deployment (Docker for both services, Atlas + free API host + static frontend hosting)
10. ⬜ Final README (architecture diagram, setup instructions, what I built vs what I'd add next)

## Known issues / gotchas

**Native MongoDB service silently shadowing the Docker container (Windows).** If MongoDB is already installed as a Windows service on this machine, it binds `127.0.0.1:27017` specifically. Docker's port-forwarding proxy for `analytics-mongodb` binds the wildcard address (`0.0.0.0:27017`). Windows prefers the more specific binding for a loopback connection, so `mongodb://localhost:27017` — the URI the backend uses by default — resolves to the **native** service, not the container, even while `docker compose ps` reports the container healthy and running. There's no error; the app just quietly reads and writes the wrong database.

How this was caught: after Phase 2's manual testing, `POST /api/metrics` and `GET /health` both succeeded, but `docker exec analytics-mongodb mongosh analytics --eval "db.metrics.countDocuments({})"` showed `0` documents. Checking `netstat -ano` for port `27017` showed two listeners — the Docker proxy and a `mongod.exe` Windows service — and the inserted documents turned up in the native instance instead.

Fix used here: **stop the native MongoDB Windows service** while working on this project (`sc query MongoDB` / stop it from Services), so only the Docker container owns port `27017`. The alternative, if you need the native service running for something else, is to remap the container's port in `docker-compose.yml` (e.g. `"27018:27017"`) and point `MONGODB_URI` in `.env` at `mongodb://localhost:27018` instead.

To verify which Mongo you're actually talking to at any point: `docker exec analytics-mongodb mongosh analytics --quiet --eval "db.metrics.countDocuments({})"` right after a test POST — if the count doesn't match what you just inserted, requests are going somewhere else.

## Design decisions

Documented here as each phase introduces a real trade-off (not before — no decisions without code behind them yet).

**Phase 2:**
- `metric` is a `Literal` of the five known metric names, not a bare `str`. Catches typos/garbage at the API boundary; trade-off is that adding a metric later means editing the type.
- MongoDB's `_id` (BSON `ObjectId`) is converted to a plain string at one boundary function (`metric_document_to_out`) rather than writing a custom Pydantic `ObjectId` type — simpler to read and explain, and it's the only place in the codebase that needs to know Mongo's id representation.
- Motor connect/close is wired into FastAPI's `lifespan` context (not opened lazily per-request), so a bad `MONGODB_URI` fails fast at startup instead of on the first request.
- `anomaly` is hard-coded `false` at write time for now, not left unset — keeps `MetricOut`'s shape stable across phases so the frontend contract set up in Phase 6 won't need to change when Phase 5 wires in real detection.
- `MetricOut.timestamp` has an explicit `field_serializer` forcing UTC ISO-8601 with a `Z` suffix on every response, regardless of whether the underlying datetime is naive or tz-aware. This matters because Motor/PyMongo hand back **naive** datetimes on read (BSON dates carry no tzinfo), which Pydantic would otherwise serialize without any offset — ambiguous to API consumers about what timezone it's in. Naive values are treated as UTC (the only thing they can be, given the write path); aware values are converted to UTC. Output always matches the `...Z` format used in the data model example above.

**Phase 3:**
- The simulator lives at `backend/simulator.py`, not a top-level `simulator/` folder — it's one ~200-line script with a single extra dependency (`requests`), installable into the same venv as the backend. A dedicated project-level folder would overstate how much structure it needs; it doesn't import anything from `app/` (HTTP only), so it isn't "part of" the FastAPI package despite living next to it.
- It talks to the backend over HTTP (`POST /api/metrics`), never touching MongoDB directly — matching the architecture diagram, where a producer is just another client of the API. This also means it can point at a deployed backend later (`--url`) with no code changes.
- Spike cadence is a deterministic counter (re-randomized between 30–50 events after each spike fires), not a flat per-event probability. A probability can go quiet for a long, unlucky stretch; the counter guarantees a spike shows up within a bounded window, which matters for both live demos and for giving Phase 5's detector something to find in any reasonably short run.
- `orders` spikes *downward* (toward zero), while every other metric spikes *upward*. An unusually busy period isn't the anomaly that matters for an orders metric — a sudden collapse toward zero (outage, broken checkout) is. Spiking it upward like the others would be modeling the wrong failure mode for that metric.
- Normal-range values are drawn from a Gaussian (mean/stdev per metric) and clipped to that metric's stated range, rather than uniform noise — the ask was for values that fluctuate with a believable shape, and a hard clip keeps "normal" and "spike" unambiguous from each other in the data itself.

**Phase 4:**
- `GET /metrics/latest` runs 5 independent `find_one()` queries (one per known metric, concurrently via `asyncio.gather`), instead of one `$group` aggregation over the whole collection. Each query is an equality match on `metric` sorted by `timestamp` descending — exactly what the `metric_1_timestamp_-1` index is for, confirmed via `.explain()` (`IXSCAN`, `docsExamined == nReturned == 1`). A single cross-collection `$group` would only be able to lean on the flatter `timestamp_-1` index and touch more documents to do the same job.
- Indexes are created with **explicit names** (`metric_1_timestamp_-1`, `timestamp_-1`) rather than left to PyMongo's auto-naming, and wrapped in a per-index try/except around `create_index()`. Repeated calls with an unchanged key spec are already no-ops in MongoDB; the explicit name + try/except only matters for the case where a name later points at a different key spec (e.g. this list changes in some future phase) — that raises `OperationFailure`, and one bad index definition shouldn't take the whole API down at startup.
- `GET /metrics/stats` returns **404**, not a 200 with `count: 0, avg: null, ...`, when the window has no data. A "no answer" and "the answer is zero" are different things, and a null-filled 200 is easy to mistake for a real (if boring) result.
- `LatestMetric` and `MetricStats` are new, separate Pydantic models rather than reusing `MetricOut` for `/metrics/latest` — even though today the fields largely overlap, each endpoint's contract is conceptually distinct (a live snapshot vs. "the document just written"), and giving each its own type keeps the OpenAPI docs and any future frontend types honest about which endpoint produced them. The UTC-`Z` timestamp formatting logic itself is still shared (one `_format_utc_z()` helper), so the duplication is only the model shape, not the serialization behavior.

## What I built vs what I'd add next

_To be written in Phase 10._
