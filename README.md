# Real-Time Data Analytics Dashboard

**[Live demo →](https://real-time-analytics-dashboard-nine.vercel.app)** · [API docs](https://real-time-analytics-dashboard-3ewt.onrender.com/docs) · [Source](https://github.com/lokakshworkspace-creator/real-time-analytics-dashboard)

A synthetic, real-time-feeling analytics dashboard: a FastAPI backend ingests streaming metric events into MongoDB, flags statistical anomalies on write with a z-score detector, and a React frontend polls for updates every 5 seconds — metric cards, a trend chart, and an anomaly panel, backed by data that's actually live end-to-end, not mocked. Built as a 10-phase portfolio project, prioritizing defensible technical choices and honestly-documented trade-offs over feature count.

## Live demo

- **Frontend:** https://real-time-analytics-dashboard-nine.vercel.app
- **Backend API:** https://real-time-analytics-dashboard-3ewt.onrender.com (interactive docs at `/docs`)

Two things worth knowing before you click:

**The backend free tier sleeps after ~15 minutes idle.** The first request after a quiet period takes 30–60s to wake it back up — the dashboard's own loading/error states handle this gracefully (it looks slow, not broken), same behavior verified locally in Phase 7's outage/recovery testing.

**The simulator does not run continuously against the deployed backend.** Running a synthetic-data generator 24/7 just so a demo looks alive isn't worth the cost or complexity — the same minimalism CLAUDE.md applies to skipping auth/WebSockets/Kafka. If the live dashboard looks empty, generate a few minutes of fresh data yourself:

```bash
cd backend
pip install -r requirements.txt   # only `requests` is actually needed for this
python simulator.py --url https://real-time-analytics-dashboard-3ewt.onrender.com --interval 1 --duration 180
```
Then reload the page. (If you run the simulator against the deployed backend more than once, clean up between runs — MongoDB is shared, persistent storage, not an ephemeral sandbox, so leftover data from an earlier run silently becomes part of the next run's anomaly-detection rolling windows. This isn't hypothetical: it caused a real masking-effect false negative during deployment testing — see Known issues.)

## Architecture

```
Simulator/Producer ──POST /api/metrics──▶ FastAPI ──▶ MongoDB
                                             │
                                             ▼
                                    Anomaly Detection
                                    (z-score, rolling window per metric+source)
                                             │
                                             ▼
                                  REST endpoints (/api/metrics/*)
                                             │
                                             ▼
                                   React Dashboard (polls every 5s)
```
Deployed as: React static build (Vercel) → FastAPI in Docker (Render) → MongoDB (Atlas, free M0). Locally: the same React dev server → the same FastAPI app under `uvicorn` → MongoDB via Docker Compose. Same code, same container image, both places — see Setup below.

### Tech stack, and why

- **FastAPI + Motor (async MongoDB driver).** Async end-to-end matters here specifically because detection runs synchronously inside the ingest request (`POST /api/metrics` queries the rolling window, scores it, then writes) — a sync driver would block the event loop on every single write. Pydantic v2 gives request validation and response serialization for free, which is most of what this API's routes actually do.
- **MongoDB.** Each metric event is a small, self-contained document (timestamp, metric, value, source, anomaly, z-score) with no relational structure to model — nothing in this app ever joins across collections. Its aggregation pipeline is what `/api/metrics/stats` actually needs: computing avg/min/max/count *in the database*, not by pulling documents into Python (a MongoDB feature this project genuinely exercises, not a "NoSQL is fast" assumption).
- **React (Vite, JavaScript — not TypeScript).** No strong reason to deviate from CLAUDE.md's plain-JS default for a project this size. Plain `fetch` + `useState`/`useEffect` (via one small shared hook, `useApiData`) instead of a data-fetching library or Redux — three components needing `{data, loading, error}` doesn't justify either dependency.
- **Polling every 5s, not WebSockets/SSE.** Keeps the backend stateless — no per-client connection state to share if it ever ran on more than one instance — and 5s is frequent enough for this app's actual update cadence. The real engineering cost of polling isn't the fetch itself, it's *not* blanking the UI on every refresh; Phase 7 built and verified that specifically (see Known issues / the Phase 7 build log entry).
- **z-score, not Isolation Forest, as the live detector.** Not a default choice — a measured one. See Anomaly detection below for the actual comparison data; the short version is that Isolation Forest needs far more samples per group than this app ever accumulates to calibrate a stable boundary, and badly over-flags as a result.

## Repository structure

```
/backend
  app/
    main.py       FastAPI app, CORS, lifespan (Mongo connect/close + index creation),
                   global exception handlers (Mongo errors → 503, sanitized 422s), GET /health
    config.py     Settings (reads .env at repo root; FRONTEND_ORIGIN supports a comma-
                   separated list so local dev and a deployed frontend both work at once)
    database.py   Motor client lifecycle + get_database() dependency + ensure_indexes()
    models.py     MetricIn/MetricOut/LatestMetric/MetricStats/AnomalyEvent/HistoryPoint
                   (Pydantic v2) + ObjectId→str conversion + shared UTC-Z timestamp formatting
    detectors/
      zscore.py             Live detector — compute_zscore() (pure) + score() (Mongo I/O wrapper)
      isolation_forest.py   Offline-only comparison tool, invoked via `python -m`
    routers/
      metrics.py    POST /api/metrics (validate, run z-score detection, store)
      analytics.py  GET /api/metrics/{latest,stats,anomalies,history}
  tests/
    conftest.py          test-database fixtures — never the real `analytics` DB
    test_zscore.py        unit tests: compute_zscore(), deterministic input → known output
    test_metrics_api.py   integration tests: TestClient against a disposable test DB
  Dockerfile / .dockerignore   what Render actually builds and deploys
  pytest.ini
  simulator.py    Standalone synthetic event producer — an HTTP client of the API, never
                   touches MongoDB directly (works against localhost or a deployed URL)
  requirements.txt
/frontend
  src/
    App.jsx                    Layout: header, metric cards row, chart + anomaly panel below
    api/client.js               fetch() wrapper, /api prefix applied once
    constants.js                 POLL_INTERVAL_MS = 5000, one named constant
    hooks/useApiData.js         Fetch-on-mount + optional polling → {data, loading, error,
                                 isRefreshing, pollError, lastUpdated}
    components/
      MetricCardsRow.jsx / MetricCard.jsx   One card per metric
      TrendChart.jsx                        Recharts line chart (cpu_usage, last 60 min)
      AnomalyPanel.jsx                      Flagged events, newest first
      LoadingState.jsx / ErrorState.jsx     Shared *blocking* loading/error presentation
      RefreshIndicator.jsx                  Shared *non-blocking* refresh/trouble indicator
    utils/formatters.js         Relative time, metric display units — presentational only
docker-compose.yml   Local MongoDB
.env.example         Environment variable template
```

## Setup (local)

### 1. MongoDB

```bash
docker compose up -d
```
Starts MongoDB on `localhost:27017`, database `analytics`, with a persisted volume.

> **Windows gotcha:** if MongoDB is also installed as a native Windows service on this machine, it silently wins over the Docker container for anything connecting to `localhost:27017` — see [Known issues](#known-issues--gotchas) before assuming the container is what you're actually talking to.

Copy `.env.example` to `.env` if you want to override anything (an Atlas URI, a different port); the backend's built-in defaults already match this compose file, so `.env` is optional for local dev. It's git-ignored — each clone needs its own copy.

### 2. Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
uvicorn app.main:app --reload
```
Runs at `http://localhost:8000`. Interactive docs (and a manual-test UI) at `/docs`.

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/metrics \
  -H "Content-Type: application/json" \
  -d '{"metric":"cpu_usage","value":72.5,"source":"server-2"}'
```

### 3. Tests

```bash
pytest -v
```
**32 tests, all passing** (verified for this README, not quoted from an earlier phase — see below). Needs MongoDB reachable (step 1), but never touches the real `analytics` database — see Testing below.

### 4. Simulator (synthetic data)

```bash
python simulator.py
```
Posts one event per tick to `POST /api/metrics` over HTTP — a client of the API, not something that writes to MongoDB directly. Runs until Ctrl+C. Useful flags: `--interval 1` (seconds between events, default 1.5), `--duration 60` (stop automatically, default: run forever), `--url http://localhost:8000` (target a different backend, including a deployed one).

Every ~30–50 events it deliberately injects a spike — a value clearly outside that metric's normal range — so the anomaly detector has something real to find. This is entirely synthetic data generated for demo purposes; baseline ranges and spike magnitudes were chosen to *look* plausible, not derived from any real system.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev
```
Opens at `http://localhost:5173` (confirmed empirically, not assumed — see Known issues if you automate starting/stopping this). `FRONTEND_ORIGIN` in `.env` must include whatever origin the frontend is actually served from. Reads `VITE_API_BASE_URL` from the **repo-root** `.env` (`vite.config.js` points `envDir` up one level), so no separate `frontend/.env` is needed.

With the backend (and ideally the simulator) running, the dashboard fetches on load and then every 5 seconds. First load shows a real loading spinner or a red error box; every refresh after that leaves existing data on screen and shows only a small "Refreshing…" pulse — a failed poll shows "⚠ Trouble refreshing" without disturbing the last-known-good data, and clears itself automatically on the next successful poll.

## Data model

```json
{
  "timestamp": "2026-08-30T10:31:06.000000Z",
  "metric": "response_time",
  "value": 987,
  "source": "server-2",
  "anomaly": true
}
```
Five simulated metrics: `orders`, `response_time`, `cpu_usage`, `failed_requests`, `memory_usage`. Timestamps are always UTC ISO-8601 with an explicit `Z` and fixed-width microseconds — `dt.isoformat()` silently drops the fractional-seconds field whenever it's exactly zero, which produced inconsistent output shapes until this was found and fixed (`strftime` instead, unconditionally).

## API

Every route lives under one consistent `/api/` prefix — verified directly against the current router source for this document, not carried over from an earlier phase's notes (`POST /api/metrics` had it from Phase 2; the four `GET` endpoints didn't until Phase 8 standardized it).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process + MongoDB connectivity check |
| POST | `/api/metrics` | Ingest one event: validate, run z-score detection, store |
| GET | `/api/metrics/latest?source=` | Most recent document per metric (up to 5; a metric with zero events is omitted, not padded) |
| GET | `/api/metrics/stats?metric=X&minutes=60` | count/avg/min/max over a trailing window, via aggregation pipeline. 404 if the window has no data (not zeroed numbers). `minutes` capped at ~1 year. |
| GET | `/api/metrics/anomalies?limit=50&metric=X` | Flagged events, newest first, with the `z_score` that triggered each one |
| GET | `/api/metrics/history?metric=X&minutes=60` | Raw `(timestamp, value)` points, oldest first — powers the trend chart. Returns `[]` (200), not 404, for an empty window |

```bash
curl "http://localhost:8000/api/metrics/stats?metric=cpu_usage&minutes=60"
# {"metric":"cpu_usage","minutes":60,"count":24,"avg":56.4,"min":40.0,"max":98.5}

curl "http://localhost:8000/api/metrics/anomalies?metric=cpu_usage&limit=10"
# [{"id":"...","metric":"cpu_usage","value":94.6,"source":"server-1","timestamp":"...Z","anomaly":true,"z_score":4.83}]
```

## Anomaly detection

**z-score (live — `detectors/zscore.py`).** Runs synchronously inside `POST /api/metrics`, before the event is inserted. For the incoming event's exact `metric`+`source` pair, it pulls up to the last 30 stored values, computes mean and sample stdev, and flags `|z| > 3`. Below 10 prior points ("cold start") or a perfectly constant window (`stdev == 0`), no verdict is possible — stored as `anomaly: false` rather than erroring or guessing. The z-score itself is persisted on every document, flagged or not, so any verdict is inspectable after the fact.

**A real, confirmed limitation: masking.** A value already sitting in the rolling window can suppress detection of a later, similarly-extreme value — the first outlier drags the window's mean/stdev up enough that the second one's z-score lands back under 3. Confirmed twice, independently: locally in Phase 5 (two identical extreme values posted back-to-back scored `z=None` then `z=2.85`, not two large numbers), and again against real production data during Phase 9 deployment testing — an 8-minute live run produced one genuine same-run masking case (`z=2.57`, one spike suppressing detection of a very similar later one) and one contamination-driven case caused by not clearing data between two simulator runs (`z=-2.45`). This isn't an implementation bug; it's an inherent property of a small rolling-window z-score, worth naming honestly rather than claiming the detector catches everything.

**Isolation Forest (offline only — `detectors/isolation_forest.py`).** Never runs inside the ingest path, never writes to `anomaly` — a deliberately separate, on-demand comparison tool:
```bash
python -m app.detectors.isolation_forest --minutes 120
```
Fits a fresh `IsolationForest` per metric+source group (one feature: the value itself, mirroring z-score's own scope) and prints every event either detector flagged, labeled `BOTH` / `IF only` / `z-score only`. Run against ~414 real events: **z-score flagged 7; Isolation Forest flagged 133**, and all 7 of z-score's were inside that 133. The other ~126 were ordinary in-range values (e.g. `cpu_usage` at 41.5% and 66.9%, both squarely inside its 40–75% normal band). With only 18–39 samples per group, `IsolationForest` doesn't have enough data to calibrate a stable boundary — `contamination="auto"` badly over-flags as a result. Not "Isolation Forest is more sensitive"; a real limitation of applying a method designed for hundreds-to-thousands of samples to windows this small. `contamination` was deliberately left at scikit-learn's default rather than tuned to the simulator's known spike rate — doing that would be fitting the detector to the test data.

## Testing

```bash
cd backend
pytest -v
```
**32 tests, 32 passing** (re-run for this document — real current output, not a number carried over from an earlier phase).

- **`tests/test_zscore.py`** — unit tests against `compute_zscore()`, the pure function extracted from the detector specifically so this is possible without mocking Motor's async cursor. Deterministic input → known output: normal values, extreme values in both directions once past the minimum window, the exact `MIN_WINDOW_SIZE - 1` boundary, the divide-by-zero guard, and one test that independently recomputes the textbook z-score formula to confirm it's applied correctly.
- **`tests/test_metrics_api.py`** — integration tests via FastAPI's `TestClient`, wired through `dependency_overrides` to a disposable `analytics_test` database (`tests/conftest.py`) — MongoDB must be reachable, but the real `analytics` database is never touched, and the test database is dropped after every test. Covers POST validation (valid/invalid/`NaN`/`Infinity` payloads), `/latest`, `/anomalies` (including a full real detection cycle through the actual ingest path), `/stats`, `/history`, and two regression tests locking in the `/api` prefix (old bare paths now 404).

## Known issues / gotchas

**Native MongoDB service can silently shadow the Docker container (Windows).** If MongoDB is also installed as a native Windows service, it binds `127.0.0.1:27017` specifically; Docker's proxy binds the wildcard address. Windows prefers the more specific binding for a loopback connection, so the app quietly talks to the *native* instance instead of the container — no error, `docker compose ps` looks fine regardless. Caught originally by a document count mismatch after a test POST. Fix: stop the native service while working on this project, or remap the container's port in `docker-compose.yml` and update `MONGODB_URI`. To check which one you're actually talking to: `docker exec analytics-mongodb mongosh analytics --quiet --eval "db.metrics.countDocuments({})"` right after a test POST.

**`npm run dev` / `npm run preview` can leave an orphaned Node process running (Windows).** `npm run dev` is never one process on Windows — `node.exe` (npm) spawns `cmd.exe /d /s /c vite`, which spawns a *second* `node.exe` that's the real server. Killing the top-level npm process doesn't cascade to that second process; Windows never auto-terminates children when a parent dies. Confirmed directly by mapping the process tree with `Get-CimInstance Win32_Process`. Fix that prevents it: invoke Vite directly, skipping the `cmd.exe` hop —
```bash
node node_modules/vite/bin/vite.js          # instead of: npm run dev
node node_modules/vite/bin/vite.js preview  # instead of: npm run preview
```
If something's still listening, kill by port rather than by PID (works regardless of how deep the orphaned tree is):
```powershell
Get-NetTCPConnection -LocalPort 5173 -State Listen | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }
```
```bash
netstat -ano | grep ":5173.*LISTENING" | awk '{print $5}' | xargs -r -I{} taskkill //F //PID {}
```
(The backend doesn't have this problem — `uvicorn` runs as a single `python.exe` process directly.)

**MongoDB Atlas network access is `0.0.0.0/0` on the deployed instance.** Required because Render's free tier has no static outbound IP to allowlist instead. Mitigated by a strong, unique database-user password; would be tightened (a specific IP range, or VPC peering) on a paid tier in a real deployment. Documented here rather than left implicit.

**Running the simulator against the deployed backend more than once without cleaning up mixes data across runs.** MongoDB Atlas is shared, persistent storage — not reset between runs. Confirmed directly: an uncleaned earlier run's leftover events silently entered a later run's rolling windows and caused a real masking-effect false negative (see Anomaly detection above). Not a bug — just a reminder this is real state, not a sandbox.

## Build log

1. **Scaffold** — repo structure, README skeleton, Docker Compose for local MongoDB, `.env.example`.
2. **Backend foundation** — FastAPI + Motor + Pydantic v2, `POST /api/metrics` (validate + store only, no detection yet), CORS, health check.
3. **Simulator** — synthetic event producer over HTTP, realistic per-metric baselines, deliberate spikes on a randomized cadence.
4. **Analytics endpoints** — `/metrics/latest`, `/metrics/stats` (aggregation pipeline), first two indexes.
5. **Anomaly detection** — z-score live on ingest with cold-start/divide-by-zero guards, `/metrics/anomalies`; Isolation Forest built as a separate offline comparison tool (5b).
6. **React dashboard** — metric cards, trend chart (added `GET /metrics/history` to support it), anomaly panel, explicit loading/error states for every fetch.
7. **Real-time polling** — 5-second interval on the same fetch hook, non-blocking refresh indicator, graceful degradation and automatic recovery through backend outages.
8. **Polish** — standardized the `/api` prefix everywhere, added the pytest suite (32 tests), found and fixed 3 real bugs during hardening (`NaN`/`Infinity` input, a validation-error response that crashed instead of erroring cleanly, an unbounded `minutes` param), added the source-aware compound index.
9. **Deployment** — Dockerized the backend, deployed to Render (backend) + Vercel (frontend) + MongoDB Atlas, multi-origin CORS, verified live end-to-end against real public URLs.
10. **Final README pass** — this document: rewritten from scratch, every claim checked against current code rather than carried over from an earlier phase's notes.

## What I built vs. what I'd add next

**Built:** a full-stack app that ingests, detects, stores, and visualizes streaming metrics in near-real-time, with a documented, evidence-based comparison between two anomaly-detection approaches, an automated test suite, and a genuinely live public deployment — not a localhost-only demo.

**What I'd add next, specifically (not generic "more tests" filler):**
- **The z-score masking effect** is real and reproducible, both locally and against live production data (see Anomaly detection). A fix would need either a secondary check that excludes already-flagged points from a window's own baseline, or a longer accumulation window traded against slower cold-start — a real design decision, not a one-line patch.
- **Isolation Forest isn't a usable alternative at this app's data scale** as currently configured (133 vs. 7 flags on the same data, mostly false positives). Making it viable would need either a much larger accumulation window before it activates, or a fundamentally different way of estimating `contamination` than scikit-learn's default heuristic.
- **No compound index for anomaly+metric together** — `GET /api/metrics/anomalies?metric=X` scans the anomaly index and filters metric in memory. Fine at this data volume; would be the next index to add if that combination became a hot path.
- **No authentication anywhere** — deliberately out of scope per CLAUDE.md's ground rules for this project, but a real gap beyond a portfolio demo.
- **No CI** — 32 tests exist and pass locally, but nothing runs them automatically on push. A GitHub Actions workflow would be the natural next step.
- **The simulator is the only data source, ever** — there is no real production traffic behind this project, and the live demo needs someone to run the simulator manually to look alive. Said plainly rather than left to be discovered.
- **Render's free-tier cold start (30–60s)** is a real, visible rough edge on the live demo. A paid tier or a scheduled keep-alive ping would fix it — not done here, deliberately, for a portfolio project's cost/complexity trade-off.
- **Atlas's `0.0.0.0/0` network access** is a real, acknowledged trade-off of the free tier's lack of a static IP — see Known issues.

## Built with Claude Code

This project was built across 10 guided phases in collaboration with Claude Code (Anthropic's CLI agent), directed one phase at a time against the plan in `CLAUDE.md`, with explicit scope per phase and independent verification before moving to the next — live `curl`/`mongosh` checks against real data and real deployed infrastructure, not just reading generated code and trusting it. That verification loop is where most of the specific findings in this README came from: a real Windows port conflict, a real async event-loop bug in the test suite, real input-validation bugs caught by testing edge cases directly (`NaN`, an oversized query param), and the z-score masking effect confirmed against genuine production data during deployment. Worth being upfront about, and a fair thing to walk through in an interview — what was asked for, what was checked, and what changed as a result of checking.
