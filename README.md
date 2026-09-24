# Real-Time Data Analytics Dashboard

**[Source](https://github.com/lokakshworkspace-creator/real-time-analytics-dashboard)**

A synthetic, real-time-feeling **e-commerce business analytics** dashboard: a FastAPI backend ingests streaming order events into MongoDB, decrements inventory on write, flags unusual regional order volume with a z-score detector, and a React frontend polls for updates every 5 seconds — KPI cards, a revenue-by-region chart, product performance tables with period-over-period growth, an inventory risk table with reorder suggestions, a demand forecast overlay, an anomaly panel, a consolidated alerts feed, and (admin-only) cross-brand benchmarking, backed by data that's actually live end-to-end, not mocked. Orders, revenue, regional demand, product performance, and inventory risk — not server metrics. Originally built as a 10-phase portfolio project prioritizing defensible technical choices over feature count, then rewritten from system-metrics (CPU/memory/response-time) to this business-analytics domain while keeping the same architecture and engineering standards.

Run locally — see Setup below.

## Architecture

```
Simulator/Producer ──POST /api/orders──▶ FastAPI ──▶ MongoDB (orders, inventory, users)
                                            │              │
                                            │      inventory decremented
                                            │      on every order write
                                            ▼
                                   Anomaly Detection
                     (z-score + severity tier over hourly order-count
                                  buckets, per region)
                                            │
                                            ▼
                    REST endpoints, JWT-authenticated + role/brand-scoped
                 (/api/auth/*, /api/orders/*, /api/inventory/*,
                  /api/anomalies/business, /api/brands)
                                            │
                                            ▼
                     React Dashboard (login gate, polls every 5s,
                        admin brand-switcher / business auto-scope)
```
Deployed as: React static build (Vercel) → FastAPI in Docker (Render) → MongoDB (Atlas, free M0). Locally: the same React dev server → the same FastAPI app under `uvicorn` → MongoDB via Docker Compose. Same code, same container image, both places — see Setup below.

### Tech stack, and why

- **FastAPI + Motor (async MongoDB driver).** Async end-to-end matters here specifically because detection runs synchronously inside the ingest request (`POST /api/orders` queries the region's hourly order-count history, scores it, then writes, then decrements inventory) — a sync driver would block the event loop on every single write. Pydantic v2 gives request validation and response serialization for free, which is most of what this API's routes actually do.
- **MongoDB.** `orders` is an append-only event log (one document per order); `inventory` is a mutable current-state document per product+region, updated in place by every order; `users` holds one document per account (role, hashed password, owned brands). No relational structure to model, and nothing here ever joins across collections in the database itself — the one place this app does combine two (`GET /api/inventory/risk`) does it as two independent queries joined in Python, simpler to read than a `$lookup` pipeline at this data volume. The aggregation pipeline is what `/api/orders/kpis`, `/api/orders/regions`, `/api/orders/products`, `/api/orders/trend`, and `/api/inventory/risk`'s demand calculation actually need: computing sums/averages/counts *in the database*, not by pulling documents into Python.
- **React (Vite, JavaScript — not TypeScript).** No strong reason to deviate from CLAUDE.md's plain-JS default for a project this size. Plain `fetch` + `useState`/`useEffect` (via one small shared hook, `useApiData`) instead of a data-fetching library or Redux. Auth state lives in a small `AuthContext` (React state, not localStorage — see Auth below), not a full state-management library.
- **Polling every 5s, not WebSockets/SSE.** Keeps the backend stateless — no per-client connection state to share if it ever ran on more than one instance — and 5s is frequent enough for this app's actual update cadence. The real engineering cost of polling isn't the fetch itself, it's *not* blanking the UI on every refresh; the shared `useApiData` hook is built and tested specifically for that (see Known issues).
- **z-score as the live detector.** A rolling baseline (here: a region's own hourly order-count history) with a fixed `|z| > 3` threshold is simple enough to explain and defend line-by-line, and cheap enough to run synchronously on every write — which detection-on-write requires. See Anomaly detection below for exactly how it's scoped, its severity tiers, and its known limitations.
- **JWT (python-jose) + bcrypt, not a full auth framework.** Two roles (admin/business), one claim that matters for authorization (owned brands), and no third-party login providers — a hand-rolled `Depends(get_current_user)` dependency plus a couple of Pydantic models is enough to implement and to explain line-by-line, the same bar the rest of this codebase holds itself to. See Auth below for exactly what's (and isn't) covered.

## Repository structure

```
/backend
  app/
    main.py       FastAPI app, CORS, lifespan (Mongo connect/close + index creation),
                   global exception handlers (Mongo errors → 503, sanitized 422s), GET /health
    config.py     Settings (reads .env at repo root; FRONTEND_ORIGIN supports a comma-
                   separated list so local dev and a deployed frontend both work at once)
    database.py   Motor client lifecycle + get_database() dependency + ensure_indexes()
                   (orders: region+timestamp, anomaly+timestamp, product_id+region+timestamp,
                   brand+timestamp; inventory: unique product_id+region; users: unique email)
    models.py     OrderIn/OrderOut/BusinessAnomalyEvent, OrderKpis (current/previous/change_pct)
                   /RegionStats/ProductStats(+note)/TrendPoint, ForecastPoint/ForecastResponse,
                   Alert/AlertType, BrandBenchmark/BrandBenchmarkResponse, InventorySeedIn/
                   InventoryItem/InventoryRiskItem(+daily_demand_rate/days_of_stock_remaining/
                   reorder_suggestion), UserIn/UserOut/LoginIn/TokenOut (Pydantic v2) +
                   ObjectId→str conversion + shared UTC-Z timestamp formatting
    security.py   Password hashing (bcrypt, used directly — see Auth), JWT issuance/
                   verification, get_current_user/require_admin dependencies,
                   brand_match_stage() (role + optional admin ?brand= → a Mongo filter)
    detectors/
      zscore.py     Live detector — compute_zscore() (pure math, now also classifies
                     severity: mild/moderate/severe) + score_order_volume() (Mongo I/O
                     wrapper: builds a region's hourly order-count window)
    routers/
      auth.py       POST /api/auth/register (admin-only, one-time unauthenticated
                     bootstrap for the first account), POST /api/auth/login,
                     GET /api/auth/me
      orders.py     POST /api/orders (open, no auth — see Auth); GET /api/orders,
                     /api/orders/kpis, /api/orders/regions, /api/orders/products,
                     /api/orders/trend, /api/orders/forecast, /api/anomalies/business,
                     /api/brands, /api/brands/benchmark (admin-only) — every GET requires
                     a JWT and brand-scopes via security.brand_match_stage (benchmark is
                     the one deliberate exception — see Auth); /api/orders,
                     /api/orders/products, /api/inventory/risk also accept ?format=csv.
                     compute_change_pct() is a shared helper (KPIs/regions/products/
                     benchmark all compare a current value against a baseline the same way)
      inventory.py  POST /api/inventory/seed (open, no auth); GET /api/inventory,
                     /api/inventory/risk — same auth/brand-scoping as orders.py
      alerts.py     GET /api/alerts — consolidates business anomalies, HIGH-risk inventory,
                     and declining regions/products into one ranked feed by calling the
                     other routers' functions directly (not re-querying), brand-scoped
    csv_export.py  Shared `?format=csv` → StreamingResponse helper, used by the three
                   routes above instead of duplicating CSV-writing per endpoint
  tests/
    conftest.py         test-database fixtures + admin_headers/business_headers_factory
                         (register+login through the real endpoints, never a DB insert)
    test_zscore.py       unit tests: compute_zscore()/severity tiers, deterministic
                          input → known output
    test_orders_api.py   integration tests: TestClient against a disposable test DB
    test_auth_api.py     integration tests: register/login/me, bootstrap rules, and —
                          the critical one — brand-scoping isolation, asserting directly
                          that a business account's request never returns another
                          brand's data, on every scoped endpoint
  Dockerfile / .dockerignore   what Render actually builds and deploys
  pytest.ini
  simulator.py    Standalone synthetic order producer — an HTTP client of the API, never
                   touches MongoDB directly (works against localhost or a deployed URL).
                   Seeds inventory (5 brand-prefixed products x 5 regions) on startup,
                   then posts orders with weighted region/product choice and occasional
                   demand spikes.
  seed_users.py   One-time setup script — also an HTTP client of the API (same
                   philosophy as simulator.py) — creates the first admin account and one
                   business account per brand, so role scoping can be exercised
                   immediately. Reads credentials from .env, never hardcodes them.
  requirements.txt
/frontend
  src/
    App.jsx                     Login gate (AuthProvider + AuthGate), then the dashboard:
                                 header (brand switcher or "Viewing: X"), KPI row, trend
                                 chart + anomaly panel, regions chart + product
                                 performance, recent orders + inventory risk, alerts
                                 center (+ cross-brand benchmark, admin-only)
    api/client.js                fetch() wrapper, /api prefix applied once, attaches
                                 `Authorization: Bearer` from the in-memory token, calls
                                 a registered 401 handler (AuthContext's logout) on any
                                 401, plus downloadCsv() for the export buttons
    context/
      AuthContext.jsx            user/token/login()/logout() — token in React state
                                 ONLY, never localStorage (see Auth below)
      BrandFilterContext.jsx     the admin's selected brand (or "All brands"), shared by
                                 every data-fetching section so BrandSwitcher re-scopes
                                 the whole dashboard at once
    constants.js                 POLL_INTERVAL_MS = 5000, DEFAULT_RANGE = '7d',
                                 INVENTORY_RISK_WINDOW_MINUTES = 1440 — named constants
    hooks/useApiData.js         Fetch-on-mount + optional polling → {data, loading, error,
                                 isRefreshing, pollError, lastUpdated}
    components/
      LoginPage.jsx                          Email + password form, calls AuthContext.login
      BrandSwitcher.jsx                      Admin-only brand dropdown, from GET /api/brands
      KpiCardsRow.jsx / MetricCard.jsx        Total Orders / Revenue / Units Sold / Avg
                                              Order Value + period-over-period ▲/▼ delta,
                                              range toggle (7D/30D/1Y), from the extended
                                              GET /api/orders/kpis
      RangeToggle.jsx                        Shared 7D/30D/1Y control (KpiCardsRow, TrendChart)
      TrendChart.jsx                         Recharts area chart — revenue over time, from
                                              GET /api/orders/trend, with an optional dashed
                                              forecast overlay (GET /api/orders/forecast) for
                                              whichever product currently leads revenue
      RegionsChart.jsx                       Recharts bar chart — revenue by region, +
                                              period-over-period DeltaBadge per row
      ProductPerformanceTable.jsx            Top sellers + slow movers side by side, +
                                              DeltaBadge per row and the bug-fix `note`
                                              when the catalog's too small for the requested
                                              top/bottom split (see API below)
      DeltaBadge.jsx                         Shared ▲/▼ percent-change indicator, extracted
                                              from MetricCard for reuse across KPIs/regions/
                                              products/benchmark
      RecentOrdersTable.jsx                  Latest orders, flagged ones highlighted
      InventoryRiskTable.jsx                 Stock vs. recent demand, HIGH/MEDIUM/LOW badge,
                                              days-of-stock-remaining, and a reorder
                                              suggestion ("Reorder 40 units by Oct 3") for
                                              HIGH/MEDIUM rows only
      AnomalyPanel.jsx                       Flagged orders (region-hour volume) with a
                                              mild/moderate/severe severity badge, newest first
      AlertsCenter.jsx                       Consolidated feed (GET /api/alerts) — anomalies +
                                              low stock + declining regions/products ranked
                                              together; additive alongside AnomalyPanel, not
                                              a replacement (AnomalyPanel stays the detailed
                                              per-event view)
      BenchmarkTable.jsx                     Admin-only cross-brand revenue/AOV comparison
                                              (GET /api/brands/benchmark) — not rendered at
                                              all for a business account, matching the
                                              backend's admin-only enforcement
      ExportCsvButton.jsx                    Fetches a `?format=csv` response as a blob
                                              and triggers a browser download (a plain
                                              `<a href>` can't carry the auth header)
      LoadingState.jsx / ErrorState.jsx      Shared *blocking* loading/error presentation
      RefreshIndicator.jsx                   Shared *non-blocking* refresh/trouble indicator
    utils/formatters.js         Currency/integer/relative-time formatting, RISK_LABEL,
                                 SEVERITY_LABEL — presentational only
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
  -d '{"product_id":"sku-001","product_name":"Nike Running Shoes","category":"Apparel","brand":"Nike","region":"Europe","current_stock":300}'

curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"demo-1","product_id":"sku-001","product_name":"Nike Running Shoes","category":"Apparel","brand":"Nike","quantity":2,"unit_price":89.99,"region":"Europe"}'
```

Every GET endpoint requires a JWT (see Auth below) — `python seed_users.py` (step 4) creates an admin account, or register one by hand:
```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"a-real-password","role":"admin"}'
# succeeds without a token only while zero users exist — see Auth below

curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"a-real-password"}'
# {"access_token":"eyJ...","token_type":"bearer","expires_in":86400}

curl http://localhost:8000/api/orders/kpis -H "Authorization: Bearer eyJ..."
```

### 3. Tests

```bash
pytest -v
```
**153 tests, all passing** (verified for this README, not quoted from an earlier phase — see below). Needs MongoDB reachable (step 1), but never touches the real `analytics` database — see Testing below.

### 4. Simulator + seed data

```bash
python seed_users.py    # creates the first admin + one business account per brand
python simulator.py     # generates synthetic orders
```
`seed_users.py` reads `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`/`SEED_BUSINESS_PASSWORD` from `.env` (see `.env.example`) — never hardcoded — and is idempotent (re-running it logs in rather than failing on accounts that already exist).

`simulator.py` seeds a starting stock level for 5 brand-prefixed products across 5 regions via `POST /api/inventory/seed`, then posts one order per tick to `POST /api/orders` over HTTP — a client of the API, not something that writes to MongoDB directly. Runs until Ctrl+C. Useful flags: `--interval 1` (seconds between events, default 1.5), `--duration 60` (stop automatically, default: run forever), `--url http://localhost:8000` (target a different backend, including a deployed one — `seed_users.py` takes the same `--url` flag).

Regions and products are weighted unevenly (some naturally busier than others, so KPIs/charts have real differences to show), quantity is usually 1–3 units, and roughly 2% of orders are a deliberate demand spike (15–40 units in one order) — enough to occasionally push a region's hourly order *count* past its own baseline, which is what the anomaly detector actually watches (see Anomaly detection below). This is entirely synthetic data generated for demo purposes; product catalog, regions, and spike magnitudes were chosen to *look* plausible, not derived from any real system.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev
```
Opens at `http://localhost:5173` (confirmed empirically, not assumed — see Known issues if you automate starting/stopping this). `FRONTEND_ORIGIN` in `.env` must include whatever origin the frontend is actually served from. Reads `VITE_API_BASE_URL` from the **repo-root** `.env` (`vite.config.js` points `envDir` up one level), so no separate `frontend/.env` is needed.

Opens on a login screen (see Auth below) — sign in with an account `seed_users.py` created, e.g. the admin, or one of the per-brand business accounts. Once signed in, the dashboard fetches on load and then every 5 seconds. First load shows a real loading spinner or a red error box; every refresh after that leaves existing data on screen and shows only a small "Refreshing…" pulse — a failed poll shows "⚠ Trouble refreshing" without disturbing the last-known-good data, and clears itself automatically on the next successful poll. A 401 from any endpoint (an expired token, most commonly) signs the whole dashboard out and drops back to the login screen.

## Data model

Three collections. `orders` is an append-only event log — one document per order, never updated after insert:

```json
{
  "order_id": "demo-brand-1",
  "timestamp": "2026-09-23T06:52:44.733000Z",
  "product_id": "sku-001",
  "product_name": "Nike Running Shoes",
  "category": "Apparel",
  "brand": "Nike",
  "quantity": 2,
  "unit_price": 89.99,
  "total_value": 179.98,
  "region": "North America",
  "payment_status": "success",
  "anomaly": false,
  "z_score": null,
  "severity": null
}
```
`total_value` is server-computed (`quantity * unit_price`), never trusted from the client. `anomaly`/`z_score`/`severity` reflect the region's hourly order-volume z-score at ingest time (see Anomaly detection) — `z_score`/`severity` are persisted but not returned by every endpoint (only `GET /api/anomalies/business` surfaces them; see API below). `brand` is what every role-scoping check in this app keys on — see Auth.

`inventory` is a mutable current-state document per product+region, decremented (floor-clamped at 0) in place by every matching order:

```json
{
  "product_id": "sku-001",
  "product_name": "Nike Running Shoes",
  "category": "Apparel",
  "brand": "Nike",
  "region": "Europe",
  "current_stock": 300,
  "last_updated": "2026-09-23T05:31:10.264000Z"
}
```

`users` holds one document per account:

```json
{
  "email": "nike@example.com",
  "password_hash": "$2b$12$...",
  "role": "business",
  "business_name": "Nike",
  "owned_brands": ["Nike"],
  "created_at": "2026-09-23T06:01:15.000000Z"
}
```
`password_hash` is a bcrypt hash — the plaintext password is never stored, and `password_hash` is never included in any API response (`UserOut`, see models.py, simply doesn't have that field). `role="admin"` accounts have `business_name: null` and `owned_brands: []` — the fields exist for every document but are only meaningful for `role="business"`, validated together at registration (see Auth).

5 simulated products (`sku-001`–`sku-005`, one real, recognizable brand per product — Nike/Sony/Hydro Flask/Logitech/Lululemon — spanning Apparel/Electronics/Home & Kitchen/Sporting Goods) across 5 regions (North America, Europe, Asia Pacific, Latin America, Middle East). Timestamps are always UTC ISO-8601 with an explicit `Z` and fixed-width microseconds — `dt.isoformat()` silently drops the fractional-seconds field whenever it's exactly zero, which produced inconsistent output shapes until this was found and fixed (`strftime` instead, unconditionally).

## API

Every route lives under one consistent `/api/` prefix. Every example response below is real output, captured against this codebase for this document — not invented.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process + MongoDB connectivity check |
| POST | `/api/auth/register` | Create a user. Admin-only — except a one-time unauthenticated bootstrap while zero users exist (see Auth) |
| POST | `/api/auth/login` | Exchange email+password for a JWT |
| GET | `/api/auth/me` | The current token's user profile |
| POST | `/api/orders` | Ingest one order: validate, compute `total_value`, run z-score + severity detection, store, decrement matching inventory (floor-clamped at 0). No auth required — see Auth |
| POST | `/api/inventory/seed` | Upsert a starting stock record for one product+region (used by the simulator). No auth required |
| GET | `/api/orders?limit=50&skip=0&brand=&format=json\|csv` | Recent orders, newest first, paginated |
| GET | `/api/orders/kpis?range=7d\|30d\|1y&brand=` | Current period's total_orders/revenue/units_sold/avg_order_value, the immediately preceding period of equal length, and the percent change between them |
| GET | `/api/orders/regions?range=7d\|30d\|1y&brand=` | Orders + revenue grouped by region, each with current/previous/change_pct (same period-over-period pattern as KPIs), sorted by current revenue descending |
| GET | `/api/orders/products?range=7d\|30d\|1y&limit=10&order=top\|bottom&brand=&format=json\|csv` | Units sold + revenue grouped by product, each with current/previous/change_pct; `top`/`bottom` are capped to non-overlapping halves of the catalog when there aren't `2×limit` distinct products, with a `note` explaining the cap (see Bug fix below) |
| GET | `/api/orders/trend?range=7d\|30d\|1y&granularity=&brand=` | Orders/revenue/units_sold bucketed by day/week/month, oldest first — powers the trend chart |
| GET | `/api/orders/forecast?product_id=X&horizon=7&brand=` | Simple demand forecast for one product: linear-regression or flat/zero fallback over its last ~42 days of daily order counts, projected `horizon` days forward. `method` always names which technique actually ran |
| GET | `/api/inventory?brand=` | Current stock list, one document per product+region |
| GET | `/api/inventory/risk?minutes=1440&low_stock_threshold=20&brand=&format=json\|csv` | Joins recent order demand against current stock; classifies each product+region as `HIGH`/`MEDIUM`/`LOW` risk, adds `daily_demand_rate`/`days_of_stock_remaining`/`reorder_suggestion`, sorted HIGH first |
| GET | `/api/anomalies/business?limit=50&brand=` | Flagged orders, newest first, with the `z_score`/`severity` that triggered each one |
| GET | `/api/alerts?brand=` | Consolidated, ranked feed: business anomalies + HIGH-risk inventory + regions/products declining past a threshold, one shared `severity`/`type`/`message` shape |
| GET | `/api/brands` | Distinct brands with at least one order — powers the admin brand-switcher |
| GET | `/api/brands/benchmark?range=7d\|30d\|1y` | **Admin-only** (403 for `role="business"`, deliberately not brand-scoped). Every brand's own orders/revenue/avg_order_value plus `revenue_vs_average_pct` against the plain average across all brands in the window |

`brand` on every GET above except the benchmark: ignored (and unnecessary) for `role="business"`, which is always scoped to its own `owned_brands` regardless; for `role="admin"`, an explicit `?brand=X` views the dashboard as if scoped to that one brand, omitted entirely for "all brands" — see Auth.

```bash
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"demo-brand-1","product_id":"sku-001","product_name":"Nike Running Shoes","category":"Apparel","brand":"Nike","quantity":2,"unit_price":89.99,"region":"North America"}'
# {"id":"6ab3773cc1399cf9aec0e6e6","order_id":"demo-brand-1","timestamp":"2026-09-23T06:52:44.733000Z",
#  "product_id":"sku-001","product_name":"Nike Running Shoes","category":"Apparel","brand":"Nike",
#  "quantity":2,"unit_price":89.99,"total_value":179.98,"region":"North America",
#  "payment_status":"success","anomaly":false}

curl "http://localhost:8000/api/orders/kpis?range=7d" -H "Authorization: Bearer $TOKEN"
# {"range":"7d","current":{"total_orders":147,"revenue":18787.26,"units_sold":274,"avg_order_value":127.80},
#  "previous":{"total_orders":142,"revenue":10108.58,"units_sold":142,"avg_order_value":71.19},
#  "change_pct":{"total_orders":3.52,"revenue":85.85,"units_sold":92.96,"avg_order_value":79.53}}
# (real current/previous data — see models.py's KpiChangePct docstring for why change_pct is
#  null instead of 0%/an error on the rare occasion the previous period actually had zero orders)

curl "http://localhost:8000/api/orders/trend?range=7d" -H "Authorization: Bearer $TOKEN"
# [{"period":"2026-09-23","orders":47,"revenue":9863.66,"units_sold":134}]

curl "http://localhost:8000/api/orders/products?range=7d&limit=5&order=top" -H "Authorization: Bearer $TOKEN"
# {"note":"Catalog has only 5 distinct product(s) in this window — top/bottom capped to
#   non-overlapping halves (3/2) instead of the requested 5, so the two lists never share
#   a product.",
#  "products":[
#    {"product_id":"sku-001","product_name":"Nike Running Shoes","brand":"Nike",
#     "current":{"units_sold":104,"revenue":9358.96},"previous":{"units_sold":53,"revenue":4769.47},
#     "change_pct":{"units_sold":96.23,"revenue":96.23}},
#    {"product_id":"sku-004","product_name":"Logitech Mechanical Keyboard","brand":"Logitech",
#     "current":{"units_sold":31,"revenue":3719.69},"previous":{"units_sold":0,"revenue":0.0},
#     "change_pct":{"units_sold":null,"revenue":null}},
#    {"product_id":"sku-002","product_name":"Sony Wireless Earbuds","brand":"Sony",
#     "current":{"units_sold":53,"revenue":3179.47},"previous":{"units_sold":89,"revenue":5339.11},
#     "change_pct":{"units_sold":-40.45,"revenue":-40.45}}]}
# (real captured output: the bug-fix note firing alongside real growth-rate deltas in both
#  directions — Nike up 96%, Sony down 40%, and Logitech's null showing the same "previous
#  period genuinely has zero orders" guard as KPIs, not a bug — see "Bug fix" below)

curl "http://localhost:8000/api/orders/regions?range=7d" -H "Authorization: Bearer $TOKEN"
# [{"region":"Europe","current":{"orders":29,"revenue":4259.32},
#   "previous":{"orders":35,"revenue":2579.65},"change_pct":{"orders":-17.14,"revenue":65.11}},
#  {"region":"North America","current":{"orders":25,"revenue":3879.46},
#   "previous":{"orders":41,"revenue":3029.59},"change_pct":{"orders":-39.02,"revenue":28.05}},
#  {"region":"Middle East","current":{"orders":18,"revenue":1779.69},
#   "previous":{"orders":32,"revenue":1979.68},"change_pct":{"orders":-43.75,"revenue":-10.10}}, ...]
# (orders and revenue can move in different directions in the same region — e.g. Europe's
#  order count is down 17% while its revenue is up 65%, a real higher-average-order-value
#  shift, not a display bug — each is its own independent change_pct)

curl "http://localhost:8000/api/orders/forecast?product_id=sku-001&horizon=7" -H "Authorization: Bearer $TOKEN"
# {"product_id":"sku-001","method":"insufficient_history_flat_projection",
#  "forecast":[{"date":"2026-09-25","projected_orders":29.0,"projected_revenue":5759.36}, ...]}
# (this demo catalog only has 1-2 days of order history, so the forecast honestly falls back
#  to a flat projection rather than fabricating a trend from one data point — see Feature 3
#  below for when linear_regression_last_42_days actually runs instead)

curl "http://localhost:8000/api/inventory/risk?brand=Nike" -H "Authorization: Bearer $TOKEN"
# [{"product_id":"sku-001","product_name":"Nike Running Shoes","brand":"Nike","region":"North America",
#   "current_stock":266,"recent_demand":0,"risk":"LOW","daily_demand_rate":0.0,
#   "days_of_stock_remaining":null,"reorder_suggestion":null}, ...]
# (a HIGH/reorder example, from backend/tests/test_orders_api.py's seeded scenario:
#  stock=5, demand=10 over 1 day -> {"risk":"HIGH","daily_demand_rate":10.0,
#  "days_of_stock_remaining":0.5,"reorder_suggestion":{"suggested_quantity":135,
#  "suggested_by_date":"2026-09-23"}})

curl "http://localhost:8000/api/anomalies/business?limit=1" -H "Authorization: Bearer $TOKEN"
# [{"id":"6ab4fa6c794bcb053c919ae8","order_id":"readme-anomaly-flood-15","timestamp":"2026-09-24T10:15:00.000000Z",
#   "product_id":"sku-001","product_name":"Nike Running Shoes","brand":"Nike",
#   "region":"readme-anomaly-demo","quantity":1,"total_value":89.99,
#   "anomaly":true,"z_score":23.72,"severity":"severe"}]
# (captured against an engineered scenario — a 10-hour baseline of ~2-3 orders/hour in a
#  dedicated region, then a current-hour flood — same technique described in Anomaly
#  detection above and reused for the /api/alerts capture right below)

curl "http://localhost:8000/api/alerts" -H "Authorization: Bearer $TOKEN"
# [{"type":"low_stock","severity":"severe",
#   "message":"Lululemon Yoga Mat in Middle East is critically low: 0 units left, recent demand 6",
#   "timestamp":"2026-09-24T10:24:52.976031Z",
#   "related_entity":{"product_id":"sku-005","region":"Middle East","brand":"Lululemon"}},
#  {"type":"anomaly","severity":"severe",
#   "message":"Unusual order volume in readme-anomaly-demo — Nike (Nike Running Shoes), z=23.72",
#   "timestamp":"2026-09-24T10:15:00.000000Z",
#   "related_entity":{"order_id":"readme-anomaly-flood-15","region":"readme-anomaly-demo",
#     "brand":"Nike","product_id":"sku-001"}},
#  ... 9 more anomaly entries from the same flood, z ranging 4.74-21.82 ...,
#  {"type":"decline","severity":"moderate",
#   "message":"Sony Wireless Earbuds revenue down 40.4% vs. the previous 7d",
#   "timestamp":"2026-09-24T10:24:52.976031Z","related_entity":{"product_id":"sku-002"}}]
# (13 alerts total, all three types present in one real response — captured against the same
#  engineered scenario as the anomaly example above, plus a seeded stock=5/demand=6 product
#  and the real Sony decline already shown in the products example; severity-then-recency
#  sorted, exactly as described in Feature 4 below)

curl http://localhost:8000/api/brands -H "Authorization: Bearer $TOKEN"
# ["Hydro Flask","Logitech","Lululemon","Nike","Sony"]

curl "http://localhost:8000/api/brands/benchmark?range=30d" -H "Authorization: Bearer $ADMIN_TOKEN"
# {"range":"30d","average_revenue":2995.54,
#  "benchmarks":[{"brand":"Nike","orders":29,"revenue":5759.36,"avg_order_value":198.6,
#    "revenue_vs_average_pct":92.26}, ...]}

curl "http://localhost:8000/api/brands/benchmark?range=30d" -H "Authorization: Bearer $BUSINESS_TOKEN"
# 403 {"detail":"Admin role required"}

curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"email":"test@example.com","password":"testpass123","role":"business"}'
# 422 {"detail":[{"type":"value_error","loc":["body"],
#   "msg":"Value error, business_name is required when role='business'", ...}]}
# (owned_brands is checked the same way — both required together, not independently optional)
```

## Bug fix: top/bottom performer overlap

`GET /api/orders/products?order=top` and `?order=bottom` independently queried "top N" and "bottom N" sorted by revenue — with only 5 products in the demo catalog and `limit=5` (or even `limit=3`), both queries returned the *same* products in reverse order, so "Top Sellers" and "Slow Movers" showed identical rows. Shrinking the limit would have hidden the symptom without fixing the cause (a 6-product catalog with `limit=5` still overlaps).

**Fix:** fetch the full current-period product list, sorted once, in a single query — then slice a non-overlapping top half and bottom half from opposite ends (`math.ceil(total/2)` products for top, the remainder for bottom), only when `total >= 2 * limit`. When the catalog's too small for the *requested* limit on both sides at once, the response caps both lists to those non-overlapping halves and adds a `note` field explaining exactly what happened and why — chosen over silently returning fewer than expected, or over an error, because a smaller-than-requested list is a legitimate, explainable state ("this brand only sells 5 products"), not a failure. The frontend (`ProductPerformanceTable.jsx`) renders that `note` directly rather than trying to hide or paper over the cap. Verified with a 3-product catalog test (`TestProductStatsTopBottomOverlapFix`) proving `top` and `bottom` never share a `product_id`, at `limit` values both above and below what the catalog can satisfy.

## Auth

**Two roles.** `admin` sees every brand, unscoped, by default (or one brand at a time via `?brand=`/the frontend's brand-switcher). `business` is permanently scoped to its own `owned_brands` — every GET under `/api/orders/*`, `/api/inventory/*`, and `/api/anomalies/business` adds a `{"brand": {"$in": owned_brands}}` match (merged into the endpoint's existing `$match`, backed by the `brand_1_timestamp_-1` index — see Data model/Architecture) that a business account's own request can never widen, override, or bypass. This is the property `tests/test_auth_api.py::TestBrandScopingIsolation` exists specifically to assert, endpoint by endpoint, not just describe.

**Bootstrap.** `POST /api/auth/register` normally requires an admin token (403 otherwise). The one exception: while the `users` collection is empty, it succeeds with no token at all — and always creates an admin regardless of the `role` in the request body — because there would otherwise be no way to create the first account. That door closes permanently the instant one user exists.

**JWTs**, not sessions: `python-jose`, `HS256`, a 24-hour expiry (`JWT_EXPIRY_HOURS`), payload carries `sub` (user id), `role`, and `owned_brands`. `get_current_user` decodes the token but then re-fetches the user document from MongoDB by id on every request, rather than trusting the token's own role/owned_brands claims for the actual authorization decision — one extra DB read per request, in exchange for an admin revoking a business account's brand access taking effect on that account's very next request, not whenever its already-issued tokens happen to expire.

**Passwords**: `bcrypt`, used directly (not via `passlib`) — `passlib` is unmaintained (last release 2020) and its own internal self-test raises against `bcrypt>=4.1`'s stricter 72-byte enforcement, confirmed directly (a 500 on the very first real register call during manual verification, before this was caught and switched). bcrypt's real 72-byte limit is enforced explicitly at the Pydantic layer instead (`UserIn.password`'s `max_length=72`) — rejected with a clear 422 rather than silently truncated.

**POST /api/orders and POST /api/inventory/seed stay unauthenticated**, a deliberate choice, not an oversight: the simulator (and any real order-intake system standing in for a checkout flow) is a different kind of client than a dashboard viewer, with no natural "logged-in user" of its own. Requiring a token here would mean hardcoding credentials into the simulator, or inventing a machine-account concept this app doesn't otherwise need, for no real security benefit at this app's scale — every *read* is still fully scoped by role regardless of who posted the underlying order.

**The frontend holds its JWT in React state only — never `localStorage`/`sessionStorage`.** A page refresh signs the user out (there's nothing to rehydrate from); in exchange, the token can't be read by a second tab, doesn't survive past the browser session, and isn't exposed to an XSS payload that goes looking through storage. A real product would likely want a refresh-token flow instead; for a portfolio dashboard, "stay logged in across refreshes" wasn't judged worth the larger attack surface. Said plainly in the login screen's own copy, not just here.

**What this auth system is not**: no password reset, no email verification, no rate limiting on login attempts, no refresh tokens (a token simply stops working after 24h — sign in again), no audit log of who registered whom. All out of scope for a portfolio project's auth layer, same spirit as CLAUDE.md's original "no auth at all" line, which this directly supersedes — see What I built vs. what I'd add next.

## Anomaly detection

**z-score over hourly order-volume buckets, per region (`detectors/zscore.py`).** Runs synchronously inside `POST /api/orders`, before the order is inserted. Unlike scoring a raw per-event value, this detector scores *how many orders a region has placed in the current hour* against that region's own history — `compute_zscore()` is the same pure mean/stdev/threshold function either way; only what feeds it changed.

For the incoming order's region, `score_order_volume()`:
1. Floors the order's timestamp to the start of its UTC hour.
2. Builds a window of the region's hourly order counts going back up to 24 hours — but only as far back as that region's actual first-ever order, not a fixed 24-hour horizon zero-padded on top. Zero-filling every hour back to a fixed horizon regardless of how much real history exists would make the window always the same length from a region's very first order onward, defeating the cold-start guard below (a region three hours old would look exactly as established as one three weeks old). Hours *within* that real span with zero orders are still filled with `0` — only hours *before* the region's first order are excluded.
3. Counts the current hour's orders so far, adds 1 for the order about to be inserted (it hasn't been written yet), and scores that value against the window: mean, sample stdev, flag if `|z| > 3`.
4. Below 10 real hourly buckets ("cold start") or a perfectly constant window (`stdev == 0`, e.g. exactly 5 orders every hour so far), no verdict is possible — stored as `anomaly: false` rather than guessing.

The z-score is persisted on every order document, flagged or not, so any verdict is inspectable after the fact via `z_score` — see `GET /api/anomalies/business`.

**Severity tiers.** A flagged order also gets a `severity` — `mild` (`3 < |z| < 4`), `moderate` (`4 <= |z| < 6`), or `severe` (`|z| >= 6`) — computed in `detectors/zscore.py`'s `_severity_for()` and persisted alongside `z_score`. The boundaries are multiples of the base 3σ flag threshold rather than independently chosen numbers: mild is "just past the threshold, most false-positive-prone"; moderate is "clearly anomalous by any reasonable reading"; severe is "2x the base threshold, virtually never noise." Lower edges are inclusive (`|z| == 4.0` is `moderate`, not `mild`), so every flagged order maps to exactly one tier with no gap. Tested directly against the exact boundary values, not just interior ones — see Testing.

**A real bug found and fixed while building this: naive vs. aware datetime mismatch.** MongoDB/Motor hand BSON dates back as *naive* datetimes (no `tzinfo`, implicitly UTC) — including the `$dateTrunc` aggregation this detector uses to bucket orders by hour. Timestamps arriving from a client (e.g. an explicit ISO-8601 string with a `+00:00` offset) parse as *timezone-aware*. Comparing an aware hour-boundary key against naive keys from MongoDB never matches, even at the identical instant — every bucket lookup silently missed, and the detector never fired. Fixed by normalizing every timestamp to naive UTC before it's used as a bucket key. Caught directly by testing the detector's actual output against a hand-built scenario, not by code review.

**A known, inherent limitation of any rolling z-score detector: masking.** A value already sitting in the window can suppress detection of a later, similarly-extreme value — an earlier spike drags the window's mean/stdev up enough that a later, comparable spike's z-score lands back under the threshold. This wasn't specifically reproduced against the current hourly-bucket detector, but it's a property of the *method*, not an implementation detail, so it applies here too and is worth naming rather than implying the detector catches everything unconditionally.

**Demoability trade-off, worth being explicit about.** Because the window is measured in real hourly buckets, the anomaly panel legitimately stays empty until a region has accumulated 10 hours of order history — running the simulator for a few minutes populates the KPI cards, regional chart, and product tables immediately, but won't trigger the business-anomaly detector without either 10+ hours of wall-clock simulator runtime, or seeding history directly with explicit past timestamps (exactly what `backend/tests/test_orders_api.py`'s anomaly tests do, and how the example response above was generated).

## Five business-value features

**1. Days-of-stock-remaining + reorder suggestions.** `GET /api/inventory/risk` now returns `daily_demand_rate` (recent demand ÷ the window's days, guarded against a zero-day window), `days_of_stock_remaining` (current stock ÷ that rate, `null` when there's no recent demand to project from rather than a misleading `Infinity`), and a `reorder_suggestion` — how many units to order to cover a configurable 14-day lead-time buffer, and by what date (stock's own low-threshold date, minus a 7-day urgency margin), also `null` when there's no rate to compute from. *Why it matters to a business:* a risk badge alone tells you something's wrong; "Reorder 40 units by Oct 3" tells you what to actually do about it, today, without opening a spreadsheet.

**2. Growth rate (period-over-period %) per region and product.** `GET /api/orders/regions` and `GET /api/orders/products` both moved from a plain `minutes` window to the same `range=7d|30d|1y` + current/previous/`change_pct` shape already built for `/api/orders/kpis` (extracted into a shared `compute_change_pct()` helper rather than copy-pasted). *Why it matters:* "Europe did $4,259 this week" is a number; "Europe is up 12% week-over-week while Latin America is down 9%" is a decision — it's what tells a business where to actually spend attention, not just what happened.

**3. Simple demand forecast.** `GET /api/orders/forecast?product_id=X&horizon=7` pulls a product's last ~42 days of daily order counts and projects forward with plain least-squares linear regression (hand-written, no numpy/sklearn — this is meant to be explained line by line, not treated as a black box) when there's enough history, falling back honestly to a flat or zero projection when there isn't (`method` always says which one actually ran — `linear_regression_last_42_days`, `insufficient_history_flat_projection`, or `insufficient_history_zero_projection`). Surfaced as a dashed-line toggle overlay on the existing trend chart, not a separate chart. *Why it matters:* "same as last week" is a legitimate, defensible forecasting baseline for a business this size — the point isn't sophistication, it's giving an honest, explainable answer to "how much should I expect to sell next week" instead of none at all.

**4. Alerts / notification center.** `GET /api/alerts` consolidates three existing signals — flagged anomalies, HIGH-risk inventory, and regions/products declining more than 20% period-over-period — into one ranked (severity, then recency) feed with a consistent `{type, severity, message, timestamp, related_entity}` shape, reusing the existing anomaly/inventory-risk/region-stats logic as direct function calls rather than re-querying. The frontend's `AlertsCenter` is additive next to the existing `AnomalyPanel`, not a replacement — a deliberate choice: `AnomalyPanel` is the detailed per-event z-score view someone would drill into, `AlertsCenter` is the "what needs my attention right now, across everything" summary. *Why it matters:* nobody should have to check four different panels every morning to notice a stockout coincided with a demand spike — one ranked list does that instead.

**5. Admin cross-brand benchmarking.** `GET /api/brands/benchmark?range=` — admin-only (`require_admin`, 403 for `role="business"`), and deliberately the one endpoint in this app that is *not* brand-scoped, since comparing brands against each other is the entire point. Each brand's own orders/revenue/avg_order_value plus `revenue_vs_average_pct` against the plain mean across every brand in the window. The frontend's `BenchmarkTable` doesn't just hide itself for a business account behind a disabled state — it isn't rendered at all, and never issues the request, so a business user can't infer that competitor figures exist even from a failed network call. *Why it matters:* a platform operator managing multiple brands (or franchisees, or seller accounts) needs "who's outperforming whom" as a single glance, not five separate dashboards mentally cross-referenced.

## Testing

```bash
cd backend
pytest -v
```
**153 tests, 153 passing** (re-run for this document — real current output, not a number carried over from an earlier phase).

- **`tests/test_zscore.py`** — unit tests against `compute_zscore()`, the pure function extracted from the detector specifically so this is possible without mocking Motor's async cursor. Deterministic input → known output: normal values, extreme values in both directions once past the minimum window, the exact `MIN_WINDOW_SIZE - 1` boundary, the divide-by-zero guard, a test that independently recomputes the textbook z-score formula to confirm it's applied correctly, and a full pass over every severity-tier boundary (just under/at/just-under-the-next-tier for mild→moderate→severe, plus the negative-z and cold-start/non-anomalous cases).
- **`tests/test_orders_api.py`** — integration tests via FastAPI's `TestClient`, wired through `dependency_overrides` to a disposable `analytics_test` database (`tests/conftest.py`) — MongoDB must be reachable, but the real `analytics` database is never touched, and the test database is dropped after every test. Covers order validation (valid/invalid quantity/price/payment_status/brand), inventory decrement on order (including the no-matching-inventory case and floor-clamping at 0), pagination, CSV export headers/content for all export-capable endpoints, KPI/region/product period-over-period deltas (including the null-when-previous-is-zero guard), trend bucketing and its range/granularity validation, `/api/brands`, the business anomaly detector end-to-end (building a real 10-hour region baseline via explicit past timestamps, then flooding the current hour to trigger a real flag with the correct severity), `TestProductStatsTopBottomOverlapFix` (the bug fix above, against a deliberately tiny 3-product catalog), reorder-suggestion math (`TestInventoryRiskReorderSuggestion`), the forecast endpoint's fallback methods and sanity bounds (`TestForecast` — no NaN, no negative projections), and admin-only enforcement for the benchmark endpoint (`TestBrandsBenchmark` — 403 for a business account, correct `revenue_vs_average_pct` math).
- **`tests/test_auth_api.py`** — register/login/me, the bootstrap rule (first account free, forced to admin; every account after that needs an existing admin's token), registration validation (business role requires business_name + owned_brands together, duplicate email, password length, malformed email), and `TestBrandScopingIsolation` — the test class that exists specifically to prove a business account's request never returns another brand's data, exercised directly against every scoped endpoint (`/api/orders`, `/api/orders/kpis`, `/api/orders/products`, `/api/orders/regions`, `/api/inventory`, `/api/inventory/risk`, `/api/anomalies/business`), plus the inverse case (an admin sees every brand, a multi-brand business account sees exactly its own set).
- **`tests/test_alerts_api.py`** — `GET /api/alerts` end to end: each of the three signal sources surfaces correctly on its own (an anomaly, a HIGH-risk product, a declining product in isolation from region-level noise), the combined list sorts by severity then recency, and brand-scoping holds for a business account exactly the same way it does everywhere else in this app.

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

**The test database never gets its own indexes created.** `ensure_indexes()` runs once, in the FastAPI app's startup lifespan — but `tests/conftest.py`'s `api_client` fixture overrides `get_database()` for request handling *after* that lifespan has already run against the real `settings.mongodb_uri`/`mongodb_db_name` (see `connect_to_mongo()`), so the disposable `analytics_test` database it hands requests never actually gets `create_index()` called against it. This went unnoticed until the unique `email_1` index on `users` was added: a test asserting a duplicate-email registration returns 409 passed locally (backed by the real index) but failed in a clean test run, because the *test* database's uniqueness was never actually enforced. Fixed at the endpoint level, not the test-harness level — `routers/auth.py`'s register handler now does an explicit `find_one` pre-check for a duplicate email rather than relying solely on the database catching it, which is more correct in production too (an index that hasn't finished building, or a database where an index was somehow dropped, would previously have silently let duplicate emails through). The underlying test-harness gap (no environment ever creates indexes on the test database) still exists and would bite the next feature that depends on a uniqueness constraint holding at the database layer — worth fixing in the harness itself if that happens again.

**The built-in `JWT_SECRET_KEY` default is intentionally insecure.** `config.py` ships a fixed, publicly-known string as a zero-configuration default so the backend still runs locally with no `.env` at all — but it means every JWT this app issues is forgeable by anyone who's read this source file until `JWT_SECRET_KEY` is set to something random in the environment. Fine for local dev; a hard requirement before any real deployment (see `.env.example`).

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
12. **Role-based auth, trend charts, KPI deltas, anomaly severity, CSV export** — the largest single feature addition: a `brand` field on every product (propagated through ingestion, the simulator's catalog, and a new compound index), JWT-based auth with `admin`/`business` roles (`security.py`, `routers/auth.py`), brand-scoping merged into every existing read endpoint plus a new `/api/orders/trend` and `/api/brands`, `/api/orders/kpis` extended to a period-over-period comparison, `mild`/`moderate`/`severe` severity tiers on the existing z-score detector, and `?format=csv` on three endpoints. Built and verified in the order specified: auth first (bootstrap/login/role-scoping via curl with real JWTs), then trend/deltas/severity (curl), then CSV export, then the full frontend (login gate, brand switcher, delta badges, severity badges, export buttons) — verified against a live backend via real browser screenshots for both an admin and a business account, confirming the brand isolation held in the actual UI, not just the API. Two real bugs found during this phase, both fixed: `passlib` raising against `bcrypt>=4.1`'s stricter behavior (switched to `bcrypt` directly), and a test-database index gap that let a duplicate-email test pass locally but fail in a clean run (fixed with an explicit pre-check in the endpoint, not just a test-harness patch — see Known issues). 30 new tests (`test_auth_api.py`, severity-tier boundaries, CSV/trend/deltas coverage) — 92 total, all passing.
13. **Bug fix (top/bottom performer overlap) + five business-value features** — fixed `/api/orders/products` returning overlapping top/bottom lists on a small catalog (non-overlapping-halves + explanatory `note`, see "Bug fix" above); added days-of-stock-remaining + reorder suggestions to `/api/inventory/risk`; extended `/api/orders/regions` and `/api/orders/products` to the same `range` + period-over-period `change_pct` pattern as KPIs (extracted `compute_change_pct()` as a shared helper); added a hand-written linear-regression demand forecast (`/api/orders/forecast`) surfaced as a dashed overlay on the trend chart; added a consolidated alerts feed (`/api/alerts`, new `AlertsCenter.jsx`, additive alongside the existing `AnomalyPanel`); added admin-only cross-brand benchmarking (`/api/brands/benchmark`, new `BenchmarkTable.jsx`, not rendered at all for a business account). Built backend-first, one piece at a time, each verified with real `curl` output against seeded data before moving to the next, and only then the frontend — verified with real browser screenshots for both an admin account (all seven lower-dashboard panels, including the new alerts feed and benchmark table) and a business account (confirming the benchmark table is genuinely absent, not just hidden by CSS, and every other panel stays correctly brand-scoped). Two real, previously-undiscovered bugs found and fixed as a byproduct of this verification, both unrelated to the features being added: `detectors/zscore.py`'s `score_order_volume` could compute a negative `available_hours` (and crash with `ValueError: length must be non-negative`) when an order's timestamp was older than its region's earliest known order — found by backdating a test order, not hypothetically; and `routers/alerts.py` initially mixed timezone-aware `datetime.now(timezone.utc)` timestamps for low-stock/decline alerts against the naive-UTC timestamps every other part of this app uses (see the naive/aware note in Anomaly detection above), which would have skewed the alert feed's recency sort — caught and fixed before it ever shipped, by consistency-checking against the rest of the codebase rather than by a failing test. 61 new tests (`test_alerts_api.py`, plus additions to `test_orders_api.py` for the bug fix, reorder math, forecast, and benchmark) — 153 total, all passing.

## What I built vs. what I'd add next

**Built:** a full-stack app that ingests, detects, stores, and visualizes streaming order events in near-real-time — KPIs with period-over-period deltas, a revenue trend chart with an optional demand-forecast overlay, regional revenue and product performance (both with period-over-period growth rates), inventory risk with days-of-stock-remaining and reorder suggestions, severity-tiered region-hour order-volume anomaly detection, a consolidated cross-signal alerts feed, and admin-only cross-brand benchmarking — all behind JWT-based role/brand-scoped auth with CSV export — with an automated 153-test suite, run locally (see Setup below); not currently deployed anywhere public.

**What I'd add next, specifically (not generic "more tests" filler):**
- **The z-score masking effect** is a known property of any rolling-window z-score detector (see Anomaly detection) — not specifically reproduced against the current hourly-bucket design, but worth fixing regardless. A fix would need either a secondary check that excludes already-flagged points from a window's own baseline, or a longer accumulation window traded against slower cold-start — a real design decision, not a one-line patch.
- **A second, independently-validating detector for this domain** — right now z-score is the only anomaly check, live or offline. An on-demand comparison tool (e.g. scoring the same hourly buckets a different way, or aggregating to daily buckets for a longer-horizon check) would make "is this really the right threshold" a checkable question instead of an assumption.
- **No refresh tokens, password reset, or rate limiting on login** — see Auth's "what this auth system is not." A real product needs at least the first two; none were worth the added complexity for a portfolio auth layer whose main point is demonstrating role/brand-scoped authorization correctly.
- **The test-database index gap** (see Known issues) is patched at the endpoint level for the one case it actually broke, but the harness-level cause — no test environment ever runs `ensure_indexes()` against the disposable test database — is still there, waiting for the next feature that assumes a database-level constraint.
- **No CI** — 153 tests exist and pass locally, but nothing runs them automatically on push. A GitHub Actions workflow would be the natural next step.
- **The simulator is the only data source, ever** — there is no real production traffic behind this project; a deployed instance would need someone to run the simulator manually to look alive. Said plainly rather than left to be discovered.
- **Render's free-tier cold start (30–60s)** would be a real, visible rough edge on a deployed instance. A paid tier or a scheduled keep-alive ping would fix it — not a concern while this only runs locally.
- **Atlas's `0.0.0.0/0` network access** is a real, acknowledged trade-off of the free tier's lack of a static IP — see Known issues.
- **The forecast is deliberately simple** (moving/linear trend over ~42 days, no seasonality, no external signals) — chosen so it's explainable line-by-line in an interview, not because it's the best available technique. A real forecasting feature would want at minimum a seasonality check (day-of-week effects are very real for order volume) before this is trusted for anything beyond a rough overlay.
- **The alert thresholds (decline %, reorder lead-time buffer, urgency margin) are fixed constants**, not per-brand configurable — fine for a single demo dataset, a real multi-tenant version would want these as settings per brand, since a 20% week-over-week swing means something very different for a high-volume vs. a low-volume product line.

## Built with Claude Code

This project was built across 10 guided phases in collaboration with Claude Code (Anthropic's CLI agent), directed one phase at a time against the plan in `CLAUDE.md`, then later fully rewritten from system-metrics to this e-commerce business-analytics domain, then extended again with role-based auth, trend/deltas/severity, and CSV export, then once more with a bug fix and five business-value features (Build log's phases 11–13) — in every case with explicit scope per step and independent verification before moving on: live `curl`/`mongosh`/pytest checks against real data, and — for the auth/frontend work specifically — real browser screenshots confirming brand isolation held in the actual UI, not just reading generated code and trusting it. That verification loop is where most of the specific findings in this README came from: a real Windows port conflict, a real async event-loop bug in the test suite, real input-validation bugs caught by testing edge cases directly (`NaN`, an oversized query param), a real z-score masking effect confirmed against genuine production data during the original deployment, a naive/aware datetime bug and a cold-start-defeating zero-padding bug during the business-analytics rewrite, a `passlib`/`bcrypt` incompatibility and a test-database index gap during the auth build, and — during the bug-fix-and-features phase — a negative-length crash in the z-score detector found by deliberately backdating a test order, and a second naive/aware datetime inconsistency in the new alerts feed caught by consistency-checking against the rest of the codebase before it ever shipped. Worth being upfront about, and a fair thing to walk through in an interview — what was asked for, what was checked, and what changed as a result of checking.
