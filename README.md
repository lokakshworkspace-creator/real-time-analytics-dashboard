# Real-Time Data Analytics Dashboard

**[Live demo →](https://real-time-analytics-dashboard-nine.vercel.app)** · [API docs](https://real-time-analytics-dashboard-3ewt.onrender.com/docs) · [Source](https://github.com/lokakshworkspace-creator/real-time-analytics-dashboard)

A synthetic, real-time-feeling **e-commerce business analytics** dashboard: a FastAPI backend ingests streaming order events into MongoDB, decrements inventory on write, flags unusual regional order volume with a z-score detector, and a React frontend polls for updates every 5 seconds — KPI cards, a revenue-by-region chart, product performance tables, an inventory risk table, and an anomaly panel, backed by data that's actually live end-to-end, not mocked. Orders, revenue, regional demand, product performance, and inventory risk — not server metrics. Originally built as a 10-phase portfolio project prioritizing defensible technical choices over feature count, then rewritten from system-metrics (CPU/memory/response-time) to this business-analytics domain while keeping the same architecture and engineering standards.

## Live demo

> **Not yet redeployed with the business-analytics rewrite described in this README.** The links below currently still run the earlier system-metrics version (CPU/memory/response-time, `POST /api/metrics`) — Phase 9's deployment, not this rewrite. Everything else in this document describes the current local codebase. Redeploying is a separate, deliberate step, not done as part of this documentation update.

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
> This command assumes the deployed backend has been redeployed with this rewrite. As of this README, it hasn't (see the note above) — the current local `simulator.py` posts to `POST /api/orders`, which the deployed backend doesn't have yet, so this would fail with 404s against it today. Run the simulator against a locally-running backend (Setup below) until redeployment happens.

Then reload the page. (If you run the simulator against the deployed backend more than once, clean up between runs — MongoDB is shared, persistent storage, not an ephemeral sandbox, so leftover data from an earlier run silently becomes part of the next run's anomaly-detection rolling windows — see Known issues.)

## Architecture

```
Simulator/Producer ──POST /api/orders──▶ FastAPI ──▶ MongoDB (orders, inventory)
                                            │              │
                                            │      inventory decremented
                                            │      on every order write
                                            ▼
                                   Anomaly Detection
                       (z-score over hourly order-count buckets, per region)
                                            │
                                            ▼
                          REST endpoints (/api/orders/*, /api/inventory/*,
                                          /api/anomalies/business)
                                            │
                                            ▼
                                  React Dashboard (polls every 5s)
```
Deployed as: React static build (Vercel) → FastAPI in Docker (Render) → MongoDB (Atlas, free M0). Locally: the same React dev server → the same FastAPI app under `uvicorn` → MongoDB via Docker Compose. Same code, same container image, both places — see Setup below.

### Tech stack, and why

- **FastAPI + Motor (async MongoDB driver).** Async end-to-end matters here specifically because detection runs synchronously inside the ingest request (`POST /api/orders` queries the region's hourly order-count history, scores it, then writes, then decrements inventory) — a sync driver would block the event loop on every single write. Pydantic v2 gives request validation and response serialization for free, which is most of what this API's routes actually do.
- **MongoDB.** `orders` is an append-only event log (one document per order); `inventory` is a mutable current-state document per product+region, updated in place by every order. No relational structure to model, and nothing here ever joins across collections in the database itself — the one place this app does combine the two (`GET /api/inventory/risk`) does it as two independent queries joined in Python, which is simpler to read than a `$lookup` pipeline at this data volume. The aggregation pipeline is what `/api/orders/kpis`, `/api/orders/regions`, `/api/orders/products`, and `/api/inventory/risk`'s demand calculation actually need: computing sums/averages/counts *in the database*, not by pulling documents into Python.
- **React (Vite, JavaScript — not TypeScript).** No strong reason to deviate from CLAUDE.md's plain-JS default for a project this size. Plain `fetch` + `useState`/`useEffect` (via one small shared hook, `useApiData`) instead of a data-fetching library or Redux.
- **Polling every 5s, not WebSockets/SSE.** Keeps the backend stateless — no per-client connection state to share if it ever ran on more than one instance — and 5s is frequent enough for this app's actual update cadence. The real engineering cost of polling isn't the fetch itself, it's *not* blanking the UI on every refresh; the shared `useApiData` hook is built and tested specifically for that (see Known issues).
- **z-score as the live detector.** A rolling baseline (here: a region's own hourly order-count history) with a fixed `|z| > 3` threshold is simple enough to explain and defend line-by-line, and cheap enough to run synchronously on every write — which detection-on-write requires. See Anomaly detection below for exactly how it's scoped and its known limitations.

## Repository structure

```
/backend
  app/
    main.py       FastAPI app, CORS, lifespan (Mongo connect/close + index creation),
                   global exception handlers (Mongo errors → 503, sanitized 422s), GET /health
    config.py     Settings (reads .env at repo root; FRONTEND_ORIGIN supports a comma-
                   separated list so local dev and a deployed frontend both work at once)
    database.py   Motor client lifecycle + get_database() dependency + ensure_indexes()
                   (orders: region+timestamp, anomaly+timestamp, product_id+region+timestamp;
                   inventory: unique product_id+region)
    models.py     OrderIn/OrderOut/BusinessAnomalyEvent, OrderKpis/RegionStats/ProductStats,
                   InventorySeedIn/InventoryItem/InventoryRiskItem (Pydantic v2) +
                   ObjectId→str conversion + shared UTC-Z timestamp formatting
    detectors/
      zscore.py     Live detector — compute_zscore() (pure math) + score_order_volume()
                     (Mongo I/O wrapper: builds a region's hourly order-count window)
    routers/
      orders.py     POST /api/orders (validate, run z-score detection, store, decrement
                     inventory); GET /api/orders, /api/orders/kpis, /api/orders/regions,
                     /api/orders/products, /api/anomalies/business
      inventory.py  POST /api/inventory/seed; GET /api/inventory, /api/inventory/risk
  tests/
    conftest.py         test-database fixtures — never the real `analytics` DB
    test_zscore.py       unit tests: compute_zscore(), deterministic input → known output
    test_orders_api.py   integration tests: TestClient against a disposable test DB
  Dockerfile / .dockerignore   what Render actually builds and deploys
  pytest.ini
  simulator.py    Standalone synthetic order producer — an HTTP client of the API, never
                   touches MongoDB directly (works against localhost or a deployed URL).
                   Seeds inventory (5 products x 5 regions) on startup, then posts orders
                   with weighted region/product choice and occasional demand spikes.
  requirements.txt
/frontend
  src/
    App.jsx                    Layout: header, KPI cards row, chart + anomaly panel,
                                product performance + inventory risk tables below
    api/client.js               fetch() wrapper, /api prefix applied once
    constants.js                 POLL_INTERVAL_MS = 5000, DEFAULT_WINDOW_MINUTES = 60,
                                 INVENTORY_RISK_WINDOW_MINUTES = 1440 — named constants
    hooks/useApiData.js         Fetch-on-mount + optional polling → {data, loading, error,
                                 isRefreshing, pollError, lastUpdated}
    components/
      KpiCardsRow.jsx / MetricCard.jsx      Total Orders / Revenue / Units Sold / Avg
                                             Order Value, from GET /api/orders/kpis
      RegionsChart.jsx                      Recharts bar chart — revenue by region
      ProductPerformanceTable.jsx           Top sellers + slow movers side by side
      InventoryRiskTable.jsx                Stock vs. recent demand, HIGH/MEDIUM/LOW badge
      AnomalyPanel.jsx                      Flagged orders (region-hour volume), newest first
      LoadingState.jsx / ErrorState.jsx     Shared *blocking* loading/error presentation
      RefreshIndicator.jsx                  Shared *non-blocking* refresh/trouble indicator
    utils/formatters.js         Currency/integer/relative-time formatting — presentational only
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

curl -X POST http://localhost:8000/api/inventory/seed \
  -H "Content-Type: application/json" \
  -d '{"product_id":"sku-001","product_name":"Wireless Earbuds","category":"Electronics","region":"Europe","current_stock":300}'

curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"demo-1","product_id":"sku-001","product_name":"Wireless Earbuds","category":"Electronics","quantity":2,"unit_price":59.99,"region":"Europe"}'
```

### 3. Tests

```bash
pytest -v
```
**37 tests, all passing** (verified for this README, not quoted from an earlier phase — see below). Needs MongoDB reachable (step 1), but never touches the real `analytics` database — see Testing below.

### 4. Simulator (synthetic data)

```bash
python simulator.py
```
On startup, seeds a starting stock level for 5 products across 5 regions via `POST /api/inventory/seed`, then posts one order per tick to `POST /api/orders` over HTTP — a client of the API, not something that writes to MongoDB directly. Runs until Ctrl+C. Useful flags: `--interval 1` (seconds between events, default 1.5), `--duration 60` (stop automatically, default: run forever), `--url http://localhost:8000` (target a different backend, including a deployed one).

Regions and products are weighted unevenly (some naturally busier than others, so KPIs/charts have real differences to show), quantity is usually 1–3 units, and roughly 2% of orders are a deliberate demand spike (15–40 units in one order) — enough to occasionally push a region's hourly order *count* past its own baseline, which is what the anomaly detector actually watches (see Anomaly detection below). This is entirely synthetic data generated for demo purposes; product catalog, regions, and spike magnitudes were chosen to *look* plausible, not derived from any real system.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev
```
Opens at `http://localhost:5173` (confirmed empirically, not assumed — see Known issues if you automate starting/stopping this). `FRONTEND_ORIGIN` in `.env` must include whatever origin the frontend is actually served from. Reads `VITE_API_BASE_URL` from the **repo-root** `.env` (`vite.config.js` points `envDir` up one level), so no separate `frontend/.env` is needed.

With the backend (and ideally the simulator) running, the dashboard fetches on load and then every 5 seconds. First load shows a real loading spinner or a red error box; every refresh after that leaves existing data on screen and shows only a small "Refreshing…" pulse — a failed poll shows "⚠ Trouble refreshing" without disturbing the last-known-good data, and clears itself automatically on the next successful poll.

## Data model

Two collections. `orders` is an append-only event log — one document per order, never updated after insert:

```json
{
  "order_id": "demo-1",
  "timestamp": "2026-09-23T05:31:09.982000Z",
  "product_id": "sku-001",
  "product_name": "Wireless Earbuds",
  "category": "Electronics",
  "quantity": 2,
  "unit_price": 59.99,
  "total_value": 119.98,
  "region": "Europe",
  "payment_status": "success",
  "anomaly": false,
  "z_score": null
}
```
`total_value` is server-computed (`quantity * unit_price`), never trusted from the client. `anomaly`/`z_score` reflect the region's hourly order-volume z-score at ingest time (see Anomaly detection) — `z_score` is persisted but not returned by every endpoint (only `GET /api/anomalies/business` surfaces it; see API below).

`inventory` is a mutable current-state document per product+region, decremented in place by every matching order:

```json
{
  "product_id": "sku-001",
  "product_name": "Wireless Earbuds",
  "category": "Electronics",
  "region": "Europe",
  "current_stock": 300,
  "last_updated": "2026-09-23T05:31:10.264000Z"
}
```

5 simulated products (`sku-001`–`sku-005`, spanning Electronics/Apparel/Home & Kitchen/Sporting Goods) across 5 regions (North America, Europe, Asia Pacific, Latin America, Middle East). Timestamps are always UTC ISO-8601 with an explicit `Z` and fixed-width microseconds — `dt.isoformat()` silently drops the fractional-seconds field whenever it's exactly zero, which produced inconsistent output shapes until this was found and fixed (`strftime` instead, unconditionally).

## API

Every route lives under one consistent `/api/` prefix. Every example response below is real output, captured against this codebase for this document — not invented.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process + MongoDB connectivity check |
| POST | `/api/orders` | Ingest one order: validate, compute `total_value`, run z-score detection, store, decrement matching inventory |
| POST | `/api/inventory/seed` | Upsert a starting stock record for one product+region (used by the simulator) |
| GET | `/api/orders?limit=50&skip=0` | Recent orders, newest first, paginated |
| GET | `/api/orders/kpis?minutes=60` | total_orders/revenue/units_sold/avg_order_value over a trailing window, via aggregation pipeline. Returns zeros (not 404) for an empty window — see Data model / API examples below for why that's a deliberate departure from `minutes`-windowed-404 elsewhere in this app |
| GET | `/api/orders/regions?minutes=60` | Orders + revenue grouped by region, sorted by revenue descending |
| GET | `/api/orders/products?minutes=60&limit=10&order=top\|bottom` | Units sold + revenue grouped by product. `order=top` = best sellers first, `order=bottom` = slow movers first |
| GET | `/api/inventory` | Current stock list, one document per product+region |
| GET | `/api/inventory/risk?minutes=1440&low_stock_threshold=20` | Joins recent order demand against current stock; classifies each product+region as `HIGH`/`MEDIUM`/`LOW` risk, sorted HIGH first |
| GET | `/api/anomalies/business?limit=50` | Flagged orders, newest first, with the `z_score` that triggered each one |

```bash
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"demo-1","product_id":"sku-001","product_name":"Wireless Earbuds","category":"Electronics","quantity":2,"unit_price":59.99,"region":"Europe"}'
# {"id":"6ab3641d55b66889a68f6c4d","order_id":"demo-1","timestamp":"2026-09-23T05:31:09.982000Z",
#  "product_id":"sku-001","product_name":"Wireless Earbuds","category":"Electronics","quantity":2,
#  "unit_price":59.99,"total_value":119.98,"region":"Europe","payment_status":"success","anomaly":false}

curl "http://localhost:8000/api/orders/kpis?minutes=60"
# {"minutes":60,"total_orders":126,"revenue":20031.98,"units_sold":302,"avg_order_value":158.98}

curl "http://localhost:8000/api/orders/regions?minutes=60"
# [{"region":"North America","orders":46,"revenue":9418.65},
#  {"region":"Europe","orders":40,"revenue":4504.33},
#  {"region":"Latin America","orders":13,"revenue":2884.48},
#  {"region":"Asia Pacific","orders":24,"revenue":2644.59},
#  {"region":"Middle East","orders":3,"revenue":579.93}]

curl "http://localhost:8000/api/orders/products?minutes=60&limit=3&order=top"
# [{"product_id":"sku-002","product_name":"Running Shoes","units_sold":83,"revenue":7469.17},
#  {"product_id":"sku-001","product_name":"Wireless Earbuds","units_sold":92,"revenue":5519.08},
#  {"product_id":"sku-004","product_name":"Mechanical Keyboard","units_sold":36,"revenue":4319.64}]

curl "http://localhost:8000/api/inventory/risk?low_stock_threshold=20"
# [{"product_id":"sku-002","product_name":"Running Shoes","region":"North America",
#   "current_stock":254,"recent_demand":57,"risk":"LOW"}, ...]
# (a HIGH example, from backend/tests/test_orders_api.py's seeded scenario:
#  stock=5, demand=10 -> {"risk":"HIGH", ...})

curl "http://localhost:8000/api/anomalies/business?limit=5"
# [{"id":"6ab3640c55b66889a68f6c47","order_id":"13313f99-...","timestamp":"2026-01-01T12:00:00.000000Z",
#   "product_id":"sku-002","product_name":"Running Shoes","region":"readme-demo-region",
#   "quantity":10,"total_value":899.90,"anomaly":true,"z_score":12.33}]
```

## Anomaly detection

**z-score over hourly order-volume buckets, per region (`detectors/zscore.py`).** Runs synchronously inside `POST /api/orders`, before the order is inserted. Unlike scoring a raw per-event value, this detector scores *how many orders a region has placed in the current hour* against that region's own history — `compute_zscore()` is the same pure mean/stdev/threshold function either way; only what feeds it changed.

For the incoming order's region, `score_order_volume()`:
1. Floors the order's timestamp to the start of its UTC hour.
2. Builds a window of the region's hourly order counts going back up to 24 hours — but only as far back as that region's actual first-ever order, not a fixed 24-hour horizon zero-padded on top. Zero-filling every hour back to a fixed horizon regardless of how much real history exists would make the window always the same length from a region's very first order onward, defeating the cold-start guard below (a region three hours old would look exactly as established as one three weeks old). Hours *within* that real span with zero orders are still filled with `0` — only hours *before* the region's first order are excluded.
3. Counts the current hour's orders so far, adds 1 for the order about to be inserted (it hasn't been written yet), and scores that value against the window: mean, sample stdev, flag if `|z| > 3`.
4. Below 10 real hourly buckets ("cold start") or a perfectly constant window (`stdev == 0`, e.g. exactly 5 orders every hour so far), no verdict is possible — stored as `anomaly: false` rather than guessing.

The z-score is persisted on every order document, flagged or not, so any verdict is inspectable after the fact via `z_score` — see `GET /api/anomalies/business`.

**A real bug found and fixed while building this: naive vs. aware datetime mismatch.** MongoDB/Motor hand BSON dates back as *naive* datetimes (no `tzinfo`, implicitly UTC) — including the `$dateTrunc` aggregation this detector uses to bucket orders by hour. Timestamps arriving from a client (e.g. an explicit ISO-8601 string with a `+00:00` offset) parse as *timezone-aware*. Comparing an aware hour-boundary key against naive keys from MongoDB never matches, even at the identical instant — every bucket lookup silently missed, and the detector never fired. Fixed by normalizing every timestamp to naive UTC before it's used as a bucket key. Caught directly by testing the detector's actual output against a hand-built scenario, not by code review.

**A known, inherent limitation of any rolling z-score detector: masking.** A value already sitting in the window can suppress detection of a later, similarly-extreme value — an earlier spike drags the window's mean/stdev up enough that a later, comparable spike's z-score lands back under the threshold. This wasn't specifically reproduced against the current hourly-bucket detector, but it's a property of the *method*, not an implementation detail, so it applies here too and is worth naming rather than implying the detector catches everything unconditionally.

**Demoability trade-off, worth being explicit about.** Because the window is measured in real hourly buckets, the anomaly panel legitimately stays empty until a region has accumulated 10 hours of order history — running the simulator for a few minutes populates the KPI cards, regional chart, and product tables immediately, but won't trigger the business-anomaly detector without either 10+ hours of wall-clock simulator runtime, or seeding history directly with explicit past timestamps (exactly what `backend/tests/test_orders_api.py`'s anomaly tests do, and how the example response above was generated).

## Testing

```bash
cd backend
pytest -v
```
**37 tests, 37 passing** (re-run for this document — real current output, not a number carried over from an earlier phase).

- **`tests/test_zscore.py`** — unit tests against `compute_zscore()`, the pure function extracted from the detector specifically so this is possible without mocking Motor's async cursor. Deterministic input → known output: normal values, extreme values in both directions once past the minimum window, the exact `MIN_WINDOW_SIZE - 1` boundary, the divide-by-zero guard, and one test that independently recomputes the textbook z-score formula to confirm it's applied correctly.
- **`tests/test_orders_api.py`** — integration tests via FastAPI's `TestClient`, wired through `dependency_overrides` to a disposable `analytics_test` database (`tests/conftest.py`) — MongoDB must be reachable, but the real `analytics` database is never touched, and the test database is dropped after every test. Covers order validation (valid/invalid quantity/price/payment_status), inventory decrement on order (including the no-matching-inventory case), pagination, KPIs/regions/products aggregation correctness, inventory seed/upsert, all three inventory-risk tiers, and the business anomaly detector end-to-end — including building a real 10-hour region baseline via explicit past timestamps, then flooding the current hour to trigger a real flag, exactly as described in Anomaly detection above.

## Known issues / gotchas

**Native MongoDB service can silently shadow the Docker container (Windows).** If MongoDB is also installed as a native Windows service, it binds `127.0.0.1:27017` specifically; Docker's proxy binds the wildcard address. Windows prefers the more specific binding for a loopback connection, so the app quietly talks to the *native* instance instead of the container — no error, `docker compose ps` looks fine regardless. Caught originally by a document count mismatch after a test POST. Fix: stop the native service while working on this project, or remap the container's port in `docker-compose.yml` and update `MONGODB_URI`. To check which one you're actually talking to: `docker exec analytics-mongodb mongosh analytics --quiet --eval "db.orders.countDocuments({})"` right after a test POST.

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

**Running the simulator against the deployed backend more than once without cleaning up mixes data across runs.** MongoDB Atlas is shared, persistent storage — not reset between runs. An uncleaned earlier run's leftover orders silently enter a later run's aggregation windows and rolling z-score baselines (a general property of a rolling detector — see the masking note in Anomaly detection above). Not a bug — just a reminder this is real state, not a sandbox.

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
10. **Final README pass** — that document: rewritten from scratch, every claim checked against current code rather than carried over from an earlier phase's notes.
11. **Business-analytics rewrite** — full domain replacement: system-metrics (CPU/memory/response-time) out, e-commerce orders/inventory/regional-demand in, same architecture and z-score approach carried forward and re-scoped to hourly order-volume buckets per region. `detectors/isolation_forest.py` removed (scored raw per-event values, no equivalent for hourly buckets). Backend verified via pytest (37 tests) and a manual simulator run before starting the frontend; frontend verified against real backend data via a live browser screenshot. Two real bugs found and fixed during this phase: a naive/aware datetime mismatch that silently broke every bucket lookup, and a zero-padding bug that defeated the detector's own cold-start guard (see Anomaly detection). Follow-up fix: `current_stock` now floor-clamps at 0 via an atomic aggregation-pipeline update instead of an unconditional `$inc` that could drive it negative — a stockout is logged and the order is still accepted, not rejected (2 new tests). This README pass documents that rewrite.

## What I built vs. what I'd add next

**Built:** a full-stack app that ingests, detects, stores, and visualizes streaming order events in near-real-time — KPIs, regional revenue, product performance, inventory risk, and region-hour order-volume anomaly detection — with an automated test suite and (pending redeployment — see Live demo above) a track record of running as a genuinely live public deployment, not a localhost-only demo.

**What I'd add next, specifically (not generic "more tests" filler):**
- **The z-score masking effect** is a known property of any rolling-window z-score detector (see Anomaly detection) — not specifically reproduced against the current hourly-bucket design, but worth fixing regardless. A fix would need either a secondary check that excludes already-flagged points from a window's own baseline, or a longer accumulation window traded against slower cold-start — a real design decision, not a one-line patch.
- **A second, independently-validating detector for this domain** — right now z-score is the only anomaly check, live or offline. An on-demand comparison tool (e.g. scoring the same hourly buckets a different way, or aggregating to daily buckets for a longer-horizon check) would make "is this really the right threshold" a checkable question instead of an assumption.
- **No authentication anywhere** — deliberately out of scope per CLAUDE.md's ground rules for this project, but a real gap beyond a portfolio demo.
- **No CI** — 37 tests exist and pass locally, but nothing runs them automatically on push. A GitHub Actions workflow would be the natural next step.
- **The simulator is the only data source, ever** — there is no real production traffic behind this project, and the live demo needs someone to run the simulator manually to look alive. Said plainly rather than left to be discovered.
- **Render's free-tier cold start (30–60s)** is a real, visible rough edge on the live demo. A paid tier or a scheduled keep-alive ping would fix it — not done here, deliberately, for a portfolio project's cost/complexity trade-off.
- **Atlas's `0.0.0.0/0` network access** is a real, acknowledged trade-off of the free tier's lack of a static IP — see Known issues.

## Built with Claude Code

This project was built across 10 guided phases in collaboration with Claude Code (Anthropic's CLI agent), directed one phase at a time against the plan in `CLAUDE.md`, then later fully rewritten from system-metrics to this e-commerce business-analytics domain (see Build log's phase 11) — in both cases with explicit scope per step and independent verification before moving on: live `curl`/`mongosh`/pytest checks against real data, not just reading generated code and trusting it. That verification loop is where most of the specific findings in this README came from: a real Windows port conflict, a real async event-loop bug in the test suite, real input-validation bugs caught by testing edge cases directly (`NaN`, an oversized query param), a real z-score masking effect confirmed against genuine production data during the original deployment, and — during the rewrite — a naive/aware datetime bug and a cold-start-defeating zero-padding bug, both caught by testing the new detector's actual output rather than trusting the code on read. Worth being upfront about, and a fair thing to walk through in an interview — what was asked for, what was checked, and what changed as a result of checking.
