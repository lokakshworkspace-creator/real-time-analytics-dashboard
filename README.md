# Real-Time Data Analytics Dashboard

> Status: Phase 7 (real-time polling) complete. The whole dashboard refreshes every 5s without ever blanking existing data, degrades gracefully on backend outages, and recovers automatically once the backend returns.

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
- **Frontend:** React (Vite, JavaScript — not TypeScript, no strong reason to deviate from CLAUDE.md's plain-JS default), Recharts, `fetch` + `useState`/`useEffect`
- **Anomaly detection:** z-score (MVP), Isolation Forest (stretch goal, kept separate)
- **Real-time updates:** polling every 5s, not WebSockets/SSE — stateless backend (trivial horizontal scaling — no connection state to share across instances), and 5s is frequent enough for this app's update cadence. See Phase 7's Design decisions for how polling avoids blanking the UI on every refresh.

## Repository structure

```
/backend
  app/
    main.py       FastAPI app, CORS, lifespan (Mongo connect/close), GET /health
    config.py     Settings (reads .env at repo root, sane localhost defaults)
    database.py   Motor client lifecycle + get_database() dependency + ensure_indexes()
    models.py     MetricIn/MetricOut/LatestMetric/MetricStats/AnomalyEvent (Pydantic v2) + ObjectId→str conversion
    detectors/
      zscore.py             MVP detector — runs live on every POST /api/metrics
      isolation_forest.py   Phase 5b stretch — offline only, invoked via `python -m`
    routers/
      metrics.py    POST /api/metrics (validate, run z-score detection, store)
      analytics.py  GET /metrics/latest, /metrics/stats, /metrics/anomalies, /metrics/history
  simulator.py  Standalone synthetic event producer, POSTs to the API over HTTP
  requirements.txt
/frontend
  src/
    App.jsx                    Layout: header, metric cards row, chart + anomaly panel below
    api/client.js               fetch() wrapper, one function per GET endpoint
    constants.js                 POLL_INTERVAL_MS = 5000 (one named constant, used by all 3 pollers)
    hooks/useApiData.js         Fetch-on-mount + optional polling -> {data, loading, error, isRefreshing, pollError, lastUpdated}
    components/
      MetricCardsRow.jsx / MetricCard.jsx   GET /metrics/latest, one card per metric
      TrendChart.jsx                        GET /metrics/history, Recharts line chart
      AnomalyPanel.jsx                      GET /metrics/anomalies
      LoadingState.jsx / ErrorState.jsx     Shared blocking loading/error presentation
      RefreshIndicator.jsx                  Shared non-blocking "refreshing" / "trouble refreshing" indicator
    utils/formatters.js         Relative time, metric display units — presentational only
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

### Frontend (React dashboard)

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173` (Vite's default — confirmed empirically, not assumed, during Phase 6 verification; `FRONTEND_ORIGIN` in `.env` must match whatever port Vite actually prints). Reads `VITE_API_BASE_URL` from the **repo-root** `.env` (`vite.config.js` sets `envDir` up one level — see Design decisions), so no separate `frontend/.env` is needed.

With the backend (and ideally the simulator, for real data) running, the dashboard fetches on page load and then **every 5 seconds** — metric cards, the CPU usage trend chart, and the anomaly panel each poll independently. The first load of each shows a full loading spinner / red error box as before; every refresh after that leaves existing data on screen untouched and shows only a small "Refreshing…" pulse in that section's header while the new data is in flight. If a poll fails (e.g. the backend restarts mid-session), the last-known-good data stays exactly as it was and a small "⚠ Trouble refreshing — showing data from Ns ago" note appears instead of an error screen; the next successful poll clears it automatically, with no page reload needed.

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
| POST | `/api/metrics` | Ingest one event, validate, run z-score detection, store | ✅ |
| GET | `/metrics/latest?source=` | Most recent document per metric (optionally filtered by source) | ✅ |
| GET | `/metrics/stats?metric=X&minutes=60` | count/avg/min/max for one metric over a trailing window, via aggregation pipeline | ✅ |
| GET | `/metrics/anomalies?limit=50&metric=X` | Flagged events, newest first | ✅ |
| GET | `/metrics/history?metric=X&minutes=60` | Raw (timestamp, value) points, oldest first — powers the Phase 6 trend chart | ✅ (added in Phase 6, not in CLAUDE.md's original list — see Design decisions) |

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

**`GET /metrics/anomalies` example:**
```bash
curl http://localhost:8000/metrics/anomalies
curl "http://localhost:8000/metrics/anomalies?metric=cpu_usage&limit=10"
# [{"id":"...","metric":"cpu_usage","value":94.6,"source":"server-1","timestamp":"...Z","anomaly":true,"z_score":4.83}, ...]
```

**`GET /metrics/history` example:**
```bash
curl "http://localhost:8000/metrics/history?metric=cpu_usage&minutes=60"
# [{"timestamp":"...Z","value":56.4}, {"timestamp":"...Z","value":58.1}, ...]  (oldest first)
```
Returns `[]` (200), not 404, when the window has no points — see Design decisions for why that's a deliberate difference from `/metrics/stats`.

## Anomaly detection

**z-score (live, MVP — `detectors/zscore.py`).** Runs synchronously inside `POST /api/metrics`, before the new event is inserted. For the incoming event's exact `metric`+`source` pair, it pulls up to the last 30 stored values (the rolling window), computes their mean and sample stdev, and flags the new value if `|z| > 3`. Below 10 prior points ("cold start"), or if the window is perfectly constant (`stdev == 0`), no verdict is possible and the event is stored as `anomaly: false` rather than erroring or guessing. The z-score itself is persisted on every document (`z_score` field) — not just the boolean — so every flag (or non-flag) is inspectable after the fact, not just asserted.

Known, real limitation observed while testing this phase: a value already **in** the rolling window can mask detection of a similar value that follows it — the first outlier drags the window's mean and stdev up, which can pull a second, equally extreme value's z-score back under the threshold. Confirmed directly: two identical extreme values posted back-to-back scored `z=None` (cold start) then `z=2.85` (just under the 3.0 cutoff) — not `z=huge, z=huge`. This isn't a bug in the implementation; it's an inherent property of a small rolling-window z-score, worth being able to name honestly rather than claim the detector catches everything.

**Isolation Forest (offline only, Phase 5b stretch — `detectors/isolation_forest.py`).** Never runs inside the ingest path and never writes to the `anomaly` field — it's a comparison tool, invoked on demand:
```bash
cd backend
python -m app.detectors.isolation_forest --minutes 120
```
For each metric+source group with enough samples, it fits a fresh `IsolationForest` (one feature: the value itself, mirroring z-score's own single-variable scope) and prints every event either detector flagged, labeled `BOTH flag` / `IF only` / `z-score only`, plus a summary. See Design decisions below for what running it actually showed.

## Phase plan

1. ✅ Scaffold (repo structure, README skeleton, `.env.example`, Docker Compose for local MongoDB)
2. ✅ Backend foundation (FastAPI app, Motor connection, Pydantic models, `POST /api/metrics`, CORS)
3. ✅ Simulator (posts realistic events on an interval, with deliberate spikes)
4. ✅ Analytics endpoints (`/metrics/latest`, `/metrics/stats`, indexes)
5. ✅ Anomaly detection (z-score on ingest, `/metrics/anomalies`); 5b: Isolation Forest (stretch) — ✅ both built
6. ✅ React dashboard (metric cards, trend chart, anomaly panel, loading/error states)
7. ✅ Polling layer (`useEffect` + `setInterval`, cleanup on unmount)
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

**Phase 5:**
- Detection is scoped to metric+**source** (not metric alone) — a rolling window mixing `server-1`'s and `server-2`'s `cpu_usage` would treat each server's own normal range as noise in someone else's baseline. There's no `metric+source` compound index for this query, though (only `metric_1_timestamp_-1` and `timestamp_-1` exist) — the `source` filter is applied as a fetch-time filter across that metric's documents rather than its own index range. Deliberately not added: this phase's instructions only asked for one new index (`anomaly_1_timestamp_-1`), and at this project's data volume the extra scan cost is negligible. At real production scale this would be the first index to add.
- `z_score` is persisted on every document but deliberately **not** added to `MetricOut` or `LatestMetric`'s response shape — only `AnomalyEvent` (`GET /metrics/anomalies`) surfaces it. Storing it costs nothing and makes every flag inspectable later; exposing it on every metric card wasn't asked for and would be schema growth beyond what this phase needed.
- Isolation Forest's `contamination` parameter is left at scikit-learn's `"auto"` default rather than hand-tuned to the simulator's actual known spike rate (~1-in-30-to-50 events). Tuning it to a number pulled from `simulator.py`'s own source would be fitting the detector to the test data — a real deployment doesn't get to peek at its own anomaly rate in advance.
- **What comparing the two actually showed** (see `python -m app.detectors.isolation_forest`, run against ~414 live events): z-score flagged 7 events; Isolation Forest flagged 133 — and every one of z-score's 7 was inside that 133 (100% overlap from z-score's side). The other ~126 "IF only" flags were, on inspection, ordinary in-range values (e.g. `cpu_usage` at 41.5% and 66.9%, both squarely inside its 40-75% normal band). The honest read: with only 18-39 samples per metric+source group, `IsolationForest` doesn't have enough data to calibrate a stable anomaly boundary, and `"auto"` contamination ends up wildly over-flagging as a result — this is not "Isolation Forest is smarter and catches more," it's a real limitation of applying an ensemble method the original paper designed for hundreds-to-thousands of samples to rolling windows this small. Documented here rather than hidden so it's possible to talk about honestly, per CLAUDE.md's whole point in asking for this comparison in the first place.
- A single outlier already sitting inside the rolling window can mask a subsequent, equally-extreme value — confirmed directly during cold-start testing (two back-to-back `value=999` posts scored `z=None` then `z=2.85`, not `z=huge` twice). Documented as a known property of small-window z-score, not silently smoothed over.

**Phase 6:**
- **History data: added `GET /metrics/history?metric=X&minutes=Y`**, rather than building the chart's data by polling `/metrics/latest` client-side. This wasn't really a close call: `/metrics/latest` only ever returns one most-recent point per metric, so the *only* way to accumulate a time series from it is to poll it repeatedly over time — which is Phase 7's interval/polling layer, explicitly out of scope here. A dedicated endpoint was the only way to have a real, multi-point chart render from a single fetch on mount. Kept minimal and additive: one new model (`HistoryPoint` — just `timestamp`+`value`), reuses the existing `metric_1_timestamp_-1` index, doesn't touch `/metrics/latest`, `/metrics/stats`, or `/metrics/anomalies`.
- `GET /metrics/history` returns `[]` (200) for an empty window, not 404 like `/metrics/stats` does. Deliberately different: `/metrics/stats` returning zeroed-out numbers for "no data" would be indistinguishable from a real (if boring) answer, so it 404s instead — but an empty *list* isn't ambiguous the same way; a chart with no points yet is a legitimate, self-explanatory state, not a missing one.
- Each of the three data components (`MetricCardsRow`, `TrendChart`, `AnomalyPanel`) fetches its own data independently via a shared `useApiData` hook, rather than `App` fetching everything and passing data down as props. Each section fails and loads independently — a backend outage shows three separate, specific error boxes instead of one component's failure blanking the whole page — and each can grow its own Phase 7 polling interval later without coordinating with the other two.
- `useApiData` is a small custom hook (~25 lines), not a data-fetching library (React Query, SWR, etc.) — three components needing the same `{data, loading, error}` shape doesn't justify a dependency, matching CLAUDE.md's "no Redux, state complexity doesn't justify it" reasoning applied one layer further.
- `cpu_usage` was picked as the one charted metric (CLAUDE.md's own suggested example) because it fluctuates continuously with no floor-clamped gaps, unlike `orders`/`failed_requests`, which makes a single line the most legible choice for a first trend chart.
- Metric **display units** (`%`, `ms`) are a frontend-only lookup table (`utils/formatters.js`) — the API returns bare numbers, on purpose (Phase 2's `MetricOut` was never going to carry presentation concerns), so "how to label a value" is decided once, in the one layer that actually renders it.
- `vite.config.js` sets `envDir` to the repo root rather than adding a second `frontend/.env` — Vite loads env files from the project root by default, which would otherwise mean `VITE_API_BASE_URL` living in two places. One `.env`, shared by backend, docker-compose, and frontend, stays true to the Phase 1 scaffold's original design.
- Confirmed rather than assumed (per this phase's explicit instruction): Vite's dev server really does default to port 5173 on this machine — checked its actual startup output — so `FRONTEND_ORIGIN=http://localhost:5173` in `.env` needed no change. Also confirmed the CORS preflight actually succeeds for that origin, not just that the numbers matched on paper.

**Phase 7:**
- `useApiData` was **extended**, not rewritten or duplicated: it's the same hook Phase 6 built, with an optional `{ intervalMs }` second argument. Omit it and the code path is identical to Phase 6 — confirmed live, not just by reading the code: temporarily called it with no options on one component while the other two kept `intervalMs` set, and watched network traffic over 12s — the no-options component made exactly 1 request, the other two made 4 each, side by side in the same running app.
- The hook distinguishes "blocking" from "non-disruptive" not by *which call this is* (first vs. Nth) but by **whether real data already exists** (`hasDataRef`). That's the detail the whole correctness requirement hinges on: a poll retry after an initial failure (no data yet) still shows the full loading state — there's nothing on screen to preserve — while every poll after a real success only ever sets `isRefreshing`/`pollError` and leaves `data` completely untouched until a new result actually arrives. Getting this distinction right (data-presence, not call-count) is what makes "never blank existing data" actually hold in the failure-after-success case, not just the happy path.
- Verified the "no re-blanking" requirement by continuous observation, not a spot-check: sampled the DOM every 300ms for 17s (>3 poll cycles) and confirmed `.status-state--loading` was absent in all 55 samples, while the metric cards' actual values changed between the first and last sample (proving polling was genuinely happening, not just not-blanking because nothing was fetched).
- Verified `clearInterval` actually runs, not just that it's present in the code: loaded the dashboard, confirmed 12 requests fired over 11s while mounted, navigated to `about:blank` (unmounting the whole React tree), then watched for 17 more seconds — zero further requests. An interval that leaked past unmount would have kept firing into a dead component tree; it didn't.
- Verified the outage/recovery cycle against a real, running backend, not a mocked failure: killed the actual `uvicorn` process mid-session, confirmed the last-known-good data froze in place (relative-time labels climbed from "just now" to "20-30s ago" instead of resetting, proving no new data was silently arriving) and a "Trouble refreshing" indicator appeared in all three sections, then restarted the backend and confirmed the *same open page* recovered on its own next poll tick — no reload triggered.
- The failed-poll guard also skips starting a new fetch if the previous one hasn't resolved yet (`isFetchingRef`) — not explicitly requested, but a natural extension of "handle a poll tick that fails" to slow-network conditions: without it, a fetch slower than 5s could stack overlapping requests instead of just waiting for the next clean tick.
- `RefreshIndicator` is a new shared component, not folded into `LoadingState`/`ErrorState` — those two are deliberately *blocking* (replace the section's content), while refresh/trouble states are deliberately *non-blocking* (sit beside existing content). Conflating them risked exactly the bug this phase was about avoiding.

## What I built vs what I'd add next

_To be written in Phase 10._
