# Real-Time Data Analytics Dashboard

**[Source](https://github.com/lokakshworkspace-creator/real-time-analytics-dashboard)**

A synthetic, real-time-feeling **e-commerce business analytics** dashboard: a FastAPI backend ingests streaming order events into MongoDB, decrements inventory on write, flags unusual order volume with three independent statistical/ML detectors (a live z-score, plus a batch Isolation Forest and a forecast-deviation pass) whose agreement drives alert ranking, lets an LLM explain a flagged anomaly in plain English on demand, and a React frontend polls for updates every 5 seconds — KPI cards, a revenue-by-region chart, product performance tables with period-over-period growth, an inventory risk table with reorder suggestions, a demand forecast overlay, an anomaly panel, a consolidated alerts feed, and (admin-only) cross-brand benchmarking, backed by data that's actually live end-to-end, not mocked. Orders, revenue, regional demand, product performance, and inventory risk — not server metrics. Originally built as a 10-phase portfolio project prioritizing defensible technical choices over feature count, then rewritten from system-metrics (CPU/memory/response-time) to this business-analytics domain while keeping the same architecture and engineering standards.

Run locally — see Setup below.

## Architecture

```
Simulator/Producer ──POST /api/orders──▶ FastAPI ──▶ MongoDB (orders, inventory, users)
                                            │              │
                                            │      inventory decremented
                                            │      on every order write
                                            ▼
                                   Anomaly Detection
              z-score + severity tier — on write, per order (region-hour)
              Isolation Forest + forecast deviation — batch pass over a
              trailing window (region, brand, hour buckets), admin-triggered
              (all three verdicts live on the same order record)
                                            │
                                            ▼
                    REST endpoints, JWT-authenticated + role/brand-scoped
                 (/api/auth/*, /api/orders/*, /api/inventory/*,
                  /api/anomalies/business, /api/alerts, /api/brands,
                  POST /api/detectors/run-batch, POST /api/anomalies/{id}/explain)
                                            │                    │
                                            │                    └──▶ Gemini (on demand,
                                            ▼                         cached on the record)
                     React app — 5 routed pages behind a navbar and a
                     login guard; each page polls (5s) only while shown;
                     admin brand-switcher / business auto-scope
```
Deployed as: React static build (Vercel) → FastAPI in Docker (Render) → MongoDB (Atlas, free M0). Locally: the same React dev server → the same FastAPI app under `uvicorn` → MongoDB via Docker Compose. Same code, same container image, both places — see Setup below. (The frontend is a client-routed single-page app, so a static host needs a rewrite of every path to `index.html` or reloading `/overview` 404s — `frontend/vercel.json` does this.)

### Tech stack, and why

- **FastAPI + Motor (async MongoDB driver).** Async end-to-end matters here specifically because detection runs synchronously inside the ingest request (`POST /api/orders` queries the region's hourly order-count history, scores it, then writes, then decrements inventory) — a sync driver would block the event loop on every single write. Pydantic v2 gives request validation and response serialization for free, which is most of what this API's routes actually do.
- **MongoDB.** `orders` is an append-only event log (one document per order); `inventory` is a mutable current-state document per product+region, updated in place by every order; `users` holds one document per account (role, hashed password, owned brands). No relational structure to model, and nothing here ever joins across collections in the database itself — the one place this app does combine two (`GET /api/inventory/risk`) does it as two independent queries joined in Python, simpler to read than a `$lookup` pipeline at this data volume. The aggregation pipeline is what `/api/orders/kpis`, `/api/orders/regions`, `/api/orders/products`, `/api/orders/trend`, and `/api/inventory/risk`'s demand calculation actually need: computing sums/averages/counts *in the database*, not by pulling documents into Python.
- **React (Vite, JavaScript — not TypeScript).** No strong reason to deviate from CLAUDE.md's plain-JS default for a project this size. Plain `fetch` + `useState`/`useEffect` (via one small shared hook, `useApiData`) instead of a data-fetching library or Redux. Auth state lives in a small `AuthContext` (React state, not localStorage — see Auth below), not a full state-management library.
- **Polling every 5s, not WebSockets/SSE.** Keeps the backend stateless — no per-client connection state to share if it ever ran on more than one instance — and 5s is frequent enough for this app's actual update cadence. The real engineering cost of polling isn't the fetch itself, it's *not* blanking the UI on every refresh; the shared `useApiData` hook is built and tested specifically for that (see Known issues).
- **z-score as the live detector.** A rolling baseline (here: a region's own hourly order-count history) with a fixed `|z| > 3` threshold is simple enough to explain and defend line-by-line, and cheap enough to run synchronously on every write — which detection-on-write requires. See Anomaly detection below for exactly how it's scoped, its severity tiers, and its known limitations.
- **scikit-learn's Isolation Forest, as a batch detector — not per order.** A forest has to be *fitted* on a window before it can score anything, so it can't be updated one write at a time the way a mean/stdev can; running it in the ingest path would mean rebuilding 100 trees per order. It runs as an admin-triggered pass over a trailing window instead (`POST /api/detectors/run-batch`), and earns its place by scoring three dimensions at once (order count, revenue, average order value), which the univariate z-score can't. A second batch detector compares each hour to a linear trend fitted on that series' earlier hours (reusing the exact regression behind `GET /api/orders/forecast`). Manual trigger rather than a scheduler on purpose: it exercises the identical code path, is trivially demoable and testable, and a scheduler is a small addition on top later, not a rewrite.
- **Gemini as an explanation layer, not a detector.** The LLM never decides what is anomalous — three statistical/ML detectors do that. On a click it is handed the numbers those detectors already produced (plus stock runway and recent growth) as structured JSON and asked for a 2-3 sentence plain-English summary and a one-line suggested action, told to use only those facts. One call per anomaly, ever: the result is cached on the record, so cost scales with human clicks, not data volume, and the free tier's rate limit is a non-issue.
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
                   (orders: region+timestamp, anomaly+timestamp, is_anomaly_if+timestamp,
                   is_anomaly_forecast+timestamp, product_id+region+timestamp, brand+timestamp;
                   inventory: unique product_id+region; users: unique email)
    llm.py        The Gemini explanation layer: GeminiExplainer (structured-JSON output,
                   429 → LLMRateLimited, everything else → LLMUnavailable with a client-safe
                   message) behind a get_explainer dependency so tests swap in a fake
    models.py     OrderIn/OrderOut/BusinessAnomalyEvent (z_score/isolation_forest/forecast
                   verdicts + detector_agreement), ExplanationResponse, BatchRunSummary,
                   OrderKpis (current/previous/change_pct)
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
      hourly_buckets.py       The (region, brand, hour) bucket rows both batch detectors score
      isolation_forest.py     Batch, multivariate (order_count, revenue, avg_order_value);
                               pure score_buckets(), deterministic via a fixed seed
      forecast_deviation.py   Batch: hour vs. a linear trend fitted on its series' earlier
                               hours; owns fit_linear_trend(), shared with /orders/forecast
      batch.py                Fetches one window, runs both batch detectors, stamps verdicts
                               onto the order records (no parallel collection)
    routers/
      detectors.py  POST /api/detectors/run-batch (admin-only, deliberately not brand-scoped)
      explain.py    POST /api/anomalies/{id}/explain — brand-scoped lookup, cache-first,
                     then one model call and the result stored on the record
      auth.py       POST /api/auth/register (admin-only, one-time unauthenticated
                     bootstrap for the first account), POST /api/auth/login,
                     GET /api/auth/me, PATCH /api/auth/me (a business account renames
                     itself; nothing else is self-editable), POST /api/auth/change-password
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
                   Seeds inventory (8 products x 5 regions — Nike has four, every other
                   brand one) on startup, then posts orders with weighted region/product
                   choice and occasional demand spikes. PRODUCTS here is the catalog's
                   single source of truth; seed_users.py and seed_history.py read it.
  seed_history.py Backfills a realistic 14-day order history for one brand (default Nike)
                   through the real POST /api/orders path, in chronological order, with
                   per-product/per-region demand, weekday/time-of-day shape, week-over-week
                   trends, two demand events, and varied final stock. Not idempotent.
  seed_users.py   One-time setup script — also an HTTP client of the API (same
                   philosophy as simulator.py) — creates the first admin account and one
                   business account per brand, so role scoping can be exercised
                   immediately. Reads credentials from .env, never hardcodes them.
  requirements.txt
/frontend
  src/
    App.jsx                     The route table: /login (public), then behind RequireAuth an
                                 AppLayout (persistent navbar) holding /overview, /products,
                                 /anomalies, /profile, and — behind a second guard,
                                 RequireAdmin — /benchmark. "/" and unknown paths -> /overview
    pages/                      One component per route: OverviewPage (KPIs, trend + forecast,
                                 regions, recent orders), ProductsPage (product performance,
                                 inventory risk), AnomaliesPage (AnomalyPanel + AlertsCenter,
                                 side by side, deliberately not merged), BenchmarkPage (admin),
                                 ProfilePage (account details, rename, change password)
    api/client.js                fetch() wrapper, /api prefix applied once, attaches
                                 `Authorization: Bearer` from the in-memory token, calls
                                 a registered 401 handler (AuthContext's logout) on any
                                 401 — except changePassword(), where a 401 means "wrong
                                 current password", not "session expired" — plus
                                 updateProfile()/changePassword() and downloadCsv()
    context/
      AuthContext.jsx            user/token/login()/logout()/updateUser() — token in React
                                 state ONLY, never localStorage (see Auth below); also
                                 carries why the user is on the login screen (a password-
                                 change or expired-session notice) and whether to resume
                                 the page they were on
      BrandFilterContext.jsx     the admin's selected brand (or "All brands"), shared by
                                 every data-fetching section so BrandSwitcher re-scopes
                                 the whole dashboard at once; its provider lives in
                                 AppLayout, so the selection survives page navigation
    constants.js                 POLL_INTERVAL_MS = 5000, DEFAULT_RANGE = '7d',
                                 INVENTORY_RISK_WINDOW_MINUTES = 1440 — named constants
    hooks/useApiData.js         Fetch-on-mount + optional polling → {data, loading, error,
                                 isRefreshing, pollError, lastUpdated}
    components/
      LoginPage.jsx                          Email + password form, calls AuthContext.login,
                                              returns you to the page you were headed to
      Navbar.jsx                             Persistent top bar: page links (active one
                                              highlighted; Benchmark hidden from business
                                              accounts), BrandSwitcher or "Viewing: X", Sign out
      AppLayout.jsx                          Navbar + <Outlet/> + BrandFilterProvider
      RequireAuth.jsx / RequireAdmin.jsx     Route guards: signed out -> /login; non-admin on
                                              an admin route -> an in-place "Not available" page
      BrandSwitcher.jsx                      Admin-only brand dropdown (now in the navbar), from
                                              GET /api/brands
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
                                              mild/moderate/severe severity badge, a row of
                                              badges for which detectors agreed, and an
                                              Explain button; newest first
      AlertsCenter.jsx                       Consolidated feed (GET /api/alerts) — anomalies +
                                              low stock + declining regions/products ranked
                                              together (anomalies by detector agreement, with
                                              the same badges + Explain); additive alongside
                                              AnomalyPanel, not a replacement (AnomalyPanel
                                              stays the detailed per-event view)
      DetectorBadges.jsx                     Which of the 3 detectors flagged an anomaly, "N of 3"
      ExplainButton.jsx                      Click → loading → the LLM explanation inline, tagged
                                              "Just generated" (a model call was spent) or
                                              "Saved" (served from the record, free); state is
                                              per row, so lists are keyed by anomaly id, not
                                              array index, to survive the 5s poll re-sort
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

The one thing that needs a key is the anomaly **Explain** button: set `GEMINI_API_KEY` in `.env` (a free key from [Google AI Studio](https://aistudio.google.com/apikey), no card required). Without it everything else works and only that endpoint answers `503` with a clear message. `GEMINI_MODEL` is a setting because hosted model names get retired — see Known issues. **Put keys in `.env` only, never in `.env.example`:** the example file is committed.

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
**265 tests, all passing** (verified for this README, not quoted from an earlier phase — see below). Needs MongoDB reachable (step 1), but never touches the real `analytics` database — see Testing below.

### 4. Simulator + seed data

```bash
python seed_users.py    # creates the first admin + one business account per brand
python simulator.py     # generates synthetic orders
```
The z-score verdict is computed as each order arrives; the Isolation Forest and forecast-deviation verdicts appear after an admin runs the batch pass (there is deliberately no scheduler — see Anomaly detection):
```bash
curl -X POST http://localhost:8000/api/detectors/run-batch -H "Authorization: Bearer $ADMIN_TOKEN"
```
`seed_users.py` reads `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`/`SEED_BUSINESS_PASSWORD` from `.env` (see `.env.example`) — never hardcoded — and is idempotent (re-running it logs in rather than failing on accounts that already exist).

`simulator.py` seeds a starting stock level for 8 products (Nike has four — running shoes, a Dri-FIT t-shirt, a duffel bag, a heritage cap — and each other brand one) across 5 regions via `POST /api/inventory/seed`, then posts one order per tick to `POST /api/orders` over HTTP — a client of the API, not something that writes to MongoDB directly. Runs until Ctrl+C. Useful flags: `--interval 1` (seconds between events, default 1.5), `--duration 60` (stop automatically, default: run forever), `--url http://localhost:8000` (target a different backend, including a deployed one — `seed_users.py` takes the same `--url` flag).

**A browsable history in one command.** The simulator produces a live stream, but a business account's dashboard needs *history* to be worth looking at (a previous period to compare against, a trend with shape, regions and products that genuinely differ). `python seed_history.py` backfills 14 days for Nike (~2,300 orders, roughly 150/day) via the real ingest path: shoes sell well everywhere, the duffel bag does best in Europe and the Middle East, the cap does best in Asia Pacific; volume peaks in each region's local evening and lifts on weekends; shoes and bags are growing week over week while the cap is declining; and two real-looking demand events are built in (a t-shirt promotion in Europe, a one-hour bulk buy of duffel bags in the Middle East, both within the last day, so the batch's default 24-hour scoring window scores them and the anomaly panel's newest-50 view shows them). Final stock levels are sized off each product+region's actual demand and vary on purpose, with one HIGH-risk and one MEDIUM-risk item. Then run the detector batch (`POST /api/detectors/run-batch?window_days=14`). It is **not idempotent** (re-running stacks another history on top), and note that **`simulator.py` re-seeds every product+region's stock to ~300 on startup**, overwriting those varied levels.

Regions and products are weighted unevenly (some naturally busier than others, so KPIs/charts have real differences to show), quantity is usually 1–3 units, and roughly 2% of orders are a deliberate demand spike (15–40 units in one order) — enough to occasionally push a region's hourly order *count* past its own baseline, which is what the anomaly detector actually watches (see Anomaly detection below). This is entirely synthetic data generated for demo purposes; product catalog, regions, and spike magnitudes were chosen to *look* plausible, not derived from any real system.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev
```
Opens at `http://localhost:5173` (confirmed empirically, not assumed — see Known issues if you automate starting/stopping this). `FRONTEND_ORIGIN` in `.env` must include whatever origin the frontend is actually served from. Reads `VITE_API_BASE_URL` from the **repo-root** `.env` (`vite.config.js` points `envDir` up one level), so no separate `frontend/.env` is needed.

Opens on a login screen (see Auth below) — sign in with an account `seed_users.py` created, e.g. the admin, or one of the per-brand business accounts. Once signed in you land on **Overview**, and the app is five pages behind a navbar (see Frontend pages and navigation below); each page fetches on load and then every 5 seconds, *only while it is on screen*. First load shows a real loading spinner or a red error box; every refresh after that leaves existing data on screen and shows only a small "Refreshing…" pulse — a failed poll shows "⚠ Trouble refreshing" without disturbing the last-known-good data, and clears itself automatically on the next successful poll. A 401 from any endpoint (an expired token, most commonly) signs the whole dashboard out and drops back to the login screen.

## Frontend pages and navigation

The dashboard is a routed multi-page app (React Router), not one long scrolling page. A persistent navbar sits above whichever page is showing.

| Route | Page | Who | What's on it |
|---|---|---|---|
| `/login` | Sign in | anyone | email + password; shows a notice when you arrive after a password change or an expired session |
| `/overview` | Overview (landing page after login) | all | KPI cards, Revenue Trend (with the forecast toggle), Revenue by Region, Recent Orders |
| `/products` | Products & Inventory | all | Product Performance (top/bottom, growth deltas) and Inventory Risk (days of stock left, reorder suggestions); the CSV export buttons stay on their tables |
| `/anomalies` | Anomalies & Alerts | all | the anomaly panel (detector badges, severity, Explain) and the Alerts Center, side by side — both kept, not merged |
| `/benchmark` | Cross-Brand Benchmark | **admin only** | the benchmark table |
| `/profile` | Profile & Settings | all | account details, rename (business accounts), change password |

**Guards.** `/login` is public; everything else renders inside `RequireAuth` (signed out -> `/login`, remembering where you were headed, so a typed deep link or an expired session resumes there after you sign in). `/benchmark` sits behind a *second* guard, `RequireAdmin`, in addition to being hidden from business accounts' navbar: hiding a link protects nothing, since anyone can type the URL, and the API's own 403 stops the data but not an empty page shell. A business account that lands on `/benchmark` — typed URL, bookmark, or a post-login redirect — gets a plain "Not available" page, and the benchmark component (and therefore its API request) never mounts. Verified in a real browser both ways: in-app navigation to the URL, and a full typed load that goes through login and then to `/benchmark`; neither made a request to `/api/brands/benchmark`.

**Polling follows the page.** Each page renders only the components whose data it shows, and every component owns its own polling (`useApiData`), which stops when the component unmounts. So leaving a page stops its polling and returning to it starts again from a fresh load — measured live: zero Overview requests during 11.5 s (two full poll cycles) on the Profile page, and polling resumed within one tick on return. The trade-off: coming back to a page shows its loading state again rather than the stale data from when you left.

**The brand filter is shared, and applies to every data panel on every page.** `BrandFilterProvider` lives in `AppLayout`, above the navbar and the page, so an admin's brand selection (set in the navbar's switcher) persists as they move between pages, and every panel that shows brand-scoped data — KPIs, trend and forecast, regions, recent orders, the anomaly panel, the Alerts Center, product performance, inventory risk — passes it to its API call and refetches when it changes. Two things are deliberately *not* filtered: the **Cross-Brand Benchmark**, because a comparison of brands against each other and against their cross-brand average is meaningless narrowed to one brand (its endpoint has no `brand` parameter by design), and a business account, which never sees the switcher and is always scoped to its own brands server-side. The filter starts at "All brands" every session: the provider lives inside the signed-in layout, so signing out unmounts it and the next sign-in starts fresh — an admin is never left on a filtered view they didn't choose this session (verified in a browser).

**Sessions are still in memory.** Routing doesn't change the earlier decision (see Auth): the JWT lives in React state only, so *any full page load* — including typing a URL or pressing refresh — signs you out and comes back through `/login`, then returns you to the page you asked for.

**Filter and range changes refetch immediately (a second `useApiData` bug).** The hook only re-ran its effect when `intervalMs` changed, so a *changed query* — a new brand, a 7D/30D toggle — waited for the next 5-second poll tick. Measured in a browser before the fix: **3.2 s** for the brand switcher and **5.0 s** for a range toggle to reach the KPI cards; after: **169 ms** and **232 ms**. Worse than slow, it was inconsistent: every panel polls on its own offset timer, so after picking a brand the panels caught up at different moments, each showing the *previous* brand's numbers under the new brand's label until its turn. That read as "the filter only works on some pages". `fetchFn` is now an effect dependency (callers memoize it on the values the query depends on), so a change refetches at once, shows the loading state instead of passing the old data off as the new scope, and restarts the poll clock.

**A pre-existing bug this surfaced.** `BrandSwitcher` never loaded its brand list in dev. React's StrictMode mounts, cleans up and re-mounts every effect, and `useApiData` guarded overlapping requests with a "fetch in flight" ref shared across those runs: the first run's fetch (cancelled by the cleanup, its result discarded) left the flag set, so the re-run skipped its own fetch. Polling components hid it (the next 5-second tick fetched); the switcher doesn't poll, so it stayed on "All brands". The flag is now local to each effect run.

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

8 simulated products (`sku-001`–`sku-008`: five brands — Nike/Sony/Hydro Flask/Logitech/Lululemon — where Nike has four (running shoes, Dri-FIT t-shirt, duffel bag, heritage cap) and the others one each — spanning Apparel/Electronics/Home & Kitchen/Sporting Goods/Accessories) across 5 regions (North America, Europe, Asia Pacific, Latin America, Middle East). Timestamps are always UTC ISO-8601 with an explicit `Z` and fixed-width microseconds — `dt.isoformat()` silently drops the fractional-seconds field whenever it's exactly zero, which produced inconsistent output shapes until this was found and fixed (`strftime` instead, unconditionally).

## API

Every route lives under one consistent `/api/` prefix. Every example response below is real output, captured against this codebase for this document — not invented.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process + MongoDB connectivity check |
| POST | `/api/auth/register` | Create a user. Admin-only — except a one-time unauthenticated bootstrap while zero users exist (see Auth) |
| POST | `/api/auth/login` | Exchange email+password for a JWT |
| GET | `/api/auth/me` | The current token's user profile |
| PATCH | `/api/auth/me` | Self-service rename: body `{"business_name": "..."}` (trimmed, 1-100 chars). **Business accounts only** — `403` for an admin, who has no business name. Any other field (`owned_brands`, `role`, `email`, …) is a `422` naming the field and changes nothing. Returns the updated profile |
| POST | `/api/auth/change-password` | Body `{"current_password", "new_password"}`. Verifies the current password (`401` `Current password is incorrect` if wrong, nothing changes); the new one follows the registration rules (8-72 chars) and must differ from the current one. Returns only `{"message": "Password updated"}` — never a password or hash. Does **not** revoke existing tokens (see Auth) |
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
| GET | `/api/anomalies/business?limit=50&brand=` | Orders flagged by **any** of the three detectors, newest first. Each carries all three verdicts side by side — `z_score` `{flagged, score, severity}`, `isolation_forest` `{flagged, score}` and `forecast` `{flagged, expected, actual}` (both `null` until a batch run has scored the record) — and `detector_agreement` (0-3) |
| POST | `/api/detectors/run-batch?window_days=7&recent_hours=24` | **Admin-only** (403 for `role="business"`, deliberately not brand-scoped: it fits one forest across every brand). Runs Isolation Forest + forecast deviation over the trailing window and stamps the verdicts onto the order records; returns a summary of what it did. Idempotent |
| POST | `/api/anomalies/{anomaly_id}/explain` | On-demand LLM explanation of one flagged anomaly (2-3 sentences + a suggested action). Brand-scoped (another brand's id is a plain 404). Cached on the record: the first call makes one Gemini call and later calls are free — until a batch run changes the record's detector verdicts, which makes the stored text stale and regenerates it on the next call. A provider rate limit is a clean `503` "try again shortly", not a raw error |
| GET | `/api/alerts?brand=` | Consolidated, ranked feed (admin-only `brand` narrows all four sources — anomalies, low stock, regional and product declines — and is ignored for business accounts, like every other endpoint): business anomalies + HIGH-risk inventory + regions/products declining past a threshold, one shared `severity`/`type`/`message` shape. Anomaly alerts also carry `detectors` and `detector_agreement`, which raise severity and rank |
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

curl -X POST "http://localhost:8000/api/detectors/run-batch" -H "Authorization: Bearer $ADMIN_TOKEN"
# {"window_days":7,"recent_hours":24,"buckets_in_window":50,"isolation_forest_buckets_scored":25,
#  "isolation_forest_buckets_flagged":3,"forecast_buckets_scored":5,"forecast_buckets_flagged":3,
#  "orders_stamped":90,"note":null}
# (scored < buckets_in_window is normal — a detector skips a bucket with too little history behind
#  it, e.g. forecast deviation needs 10 earlier hours of that region+brand. Re-running is idempotent.)
curl -X POST "http://localhost:8000/api/detectors/run-batch" -H "Authorization: Bearer $BUSINESS_TOKEN"
# 403 {"detail":"Admin role required"}

curl "http://localhost:8000/api/anomalies/business?limit=500" -H "Authorization: Bearer $TOKEN"
# Two records from the same flooded hour (engineered scenario: a 12-hour baseline of 2-3 orders/hour
# in a dedicated region, then a 14-order flood in the current hour), after a batch run.
# 1) an order all three detectors flagged:
# {"id":"6ab5efdaec656456fa8063a9","order_id":"readme-det-flood-14","timestamp":"2026-09-25T03:02:00.000000Z",
#  "product_id":"sku-001","product_name":"Nike Running Shoes","brand":"Nike","region":"readme-detector-demo",
#  "quantity":4,"total_value":359.96,
#  "z_score":{"flagged":true,"score":22.02,"severity":"severe"},
#  "isolation_forest":{"flagged":true,"score":0.811},
#  "forecast":{"flagged":true,"expected":2.64,"actual":14.0},
#  "detector_agreement":3}
# 2) an earlier order in the SAME hour that the z-score passed (it arrived while the hour's count was
#    still ordinary) but the batch detectors flagged, because they score the whole hour:
# {"id":"6ab5efdaec656456fa80639e","order_id":"readme-det-flood-3","timestamp":"2026-09-25T03:03:00.000000Z",
#  ...,"quantity":6,"total_value":539.94,
#  "z_score":{"flagged":false,"score":0.957,"severity":null},
#  "isolation_forest":{"flagged":true,"score":0.811},
#  "forecast":{"flagged":true,"expected":2.64,"actual":14.0},
#  "detector_agreement":2}
# (isolation_forest / forecast are null — not flagged:false — on any record no batch run has scored)

curl "http://localhost:8000/api/alerts" -H "Authorization: Bearer $TOKEN"
# [{"type":"anomaly","severity":"severe",
#   "message":"Unusual order volume in readme-anomaly-demo — Nike (Nike Running Shoes), z=23.72 — flagged by 3 of 3 detectors",
#   "timestamp":"2026-09-25T04:15:00.000000Z",
#   "related_entity":{"anomaly_id":"6ab5f4c924fccd58c5e07d77","order_id":"readme-anomaly-flood-15",
#     "region":"readme-anomaly-demo","brand":"Nike","product_id":"sku-001"},
#   "detectors":["z_score","isolation_forest","forecast"],"detector_agreement":3},
#  ... 20 more 3-detector anomaly alerts ...,
#  {"type":"low_stock","severity":"severe",
#   "message":"Lululemon Yoga Mat in Middle East is critically low: 0 units left, recent demand 6",
#   "timestamp":"2026-09-25T04:12:59.430994Z",
#   "related_entity":{"product_id":"sku-005","region":"Middle East","brand":"Lululemon"},
#   "detectors":null,"detector_agreement":null},
#  {"type":"anomaly","severity":"moderate",
#   "message":"Unusual order volume in readme-anomaly-demo — Nike (Nike Running Shoes) — flagged by 2 of 3 detectors",
#   "timestamp":"2026-09-25T04:04:00.000000Z",
#   "related_entity":{"anomaly_id":"6ab5f4c924fccd58c5e07d6c","order_id":"readme-anomaly-flood-4",
#     "region":"readme-anomaly-demo","brand":"Nike","product_id":"sku-001"},
#   "detectors":["isolation_forest","forecast"],"detector_agreement":2},
#  ... 13 more 2-detector anomaly alerts ...,
#  {"type":"decline","severity":"moderate",
#   "message":"Sony Wireless Earbuds revenue down 40.4% vs. the previous 7d",
#   "timestamp":"2026-09-25T04:12:59.430994Z","related_entity":{"product_id":"sku-002"},
#   "detectors":null,"detector_agreement":null}]
# (37 alerts: 35 anomaly, 1 low_stock, 1 decline. Ranked severity first, then detector agreement, then
#  recency — so every 3-detector anomaly outranks every 2-detector one, and a 2-detector one outranks a
#  same-severity lone flag. A 2-detector anomaly whose z-score didn't flag has no z tier to borrow, so it
#  starts at "mild" and each extra agreeing detector raises it one tier: mild + 1 extra = "moderate".)

curl -X POST "http://localhost:8000/api/anomalies/6ab5efdaec656456fa8063a9/explain" -H "Authorization: Bearer $TOKEN"
# First call — one real Gemini call (gemini-3.5-flash-lite), 3.1s:
# {"anomaly_id":"6ab5efdaec656456fa8063a9",
#  "explanation":"Three of three detectors flagged an anomaly for Nike Running Shoes in the readme-detector-demo region on September 25, 2026, at 03:02, where 14 orders occurred in that hour against an expected 2.64. This spike matters because it leaves only 1.2 days of stock remaining with current inventory at 120 units.",
#  "suggested_action":"Review inventory levels immediately for Nike Running Shoes in the readme-detector-demo region.",
#  "explained_at":"2026-09-25T04:07:12.761000Z","cached":false}
# Second call — the identical stored text, no model call, 0.2s, same explained_at:
# {... same body ...,"explained_at":"2026-09-25T04:07:12.761000Z","cached":true}
# Everything in that text traces to a number in the record or its context (14 vs 2.64 is the forecast
# verdict; 120 units and 1.2 days come from the seeded inventory and the last 24h of demand).
# Failure modes, all real responses except the last:
#   no GEMINI_API_KEY   -> 503 {"detail":"Explanations are unavailable: GEMINI_API_KEY is not configured."}
#   another brand's id  -> 404 {"detail":"Anomaly not found"}   (as a business account; never reaches the model)
#   provider 429        -> 503 "The explanation service is busy right now. Please try again shortly."
#                          + Retry-After: 30 (covered by a mocked-provider test — not triggered live)

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

curl -X PATCH http://localhost:8000/api/auth/me \
  -H "Content-Type: application/json" -H "Authorization: Bearer $BUSINESS_TOKEN" \
  -d '{"business_name":"  Temp Renamed Inc  "}'
# 200 {"user_id":"6ab604a2324bb3a33f2bbec7","email":"tmp-profile@example.com","role":"business",
#      "business_name":"Temp Renamed Inc","owned_brands":["Nike"]}      (whitespace trimmed)
curl -X PATCH http://localhost:8000/api/auth/me -H "Content-Type: application/json" -H "Authorization: Bearer $BUSINESS_TOKEN" \
  -d '{"business_name":"Sneaky","owned_brands":["Sony"]}'
# 422 {"detail":[{"type":"extra_forbidden","loc":["body","owned_brands"],"msg":"Extra inputs are not permitted"}]}
# (the whole request is refused — the valid business_name sent alongside was NOT applied)
curl -X PATCH http://localhost:8000/api/auth/me -H "Content-Type: application/json" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"business_name":"Admin Co"}'
# 403 {"detail":"Administrator accounts have no business name to edit"}

curl -X POST http://localhost:8000/api/auth/change-password -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"current_password":"WRONG-password","new_password":"newpass-67890"}'
# 401 {"detail":"Current password is incorrect"}     (the old password still logs in; the new one doesn't)
curl -X POST http://localhost:8000/api/auth/change-password -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"current_password":"oldpass-12345","new_password":"oldpass-12345"}'
# 422 {"detail":[{"type":"value_error","loc":["body"],"msg":"Value error, new_password must be different from current_password",...}]}
# (note what is absent: no "input" echoing either password back — see Auth)
curl -X POST http://localhost:8000/api/auth/change-password -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"current_password":"oldpass-12345","new_password":"newpass-67890"}'
# 200 {"message":"Password updated"}     (then: old password -> 401 at login, new password -> 200)
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

**Profile self-service (`PATCH /api/auth/me`, `POST /api/auth/change-password`).** A business account can rename itself and any account can change its own password; nothing else about an account is self-editable. The rename endpoint's request model forbids extra fields, so a body naming `role`, `owned_brands` or `email` is a `422` and changes *nothing* — not even a valid `business_name` sent alongside it — because brand ownership is what every scoped query keys on, and letting an account widen it would defeat the whole role model. An admin gets a `403` (an admin has no business name; it isn't a silent no-op). Changing a password re-verifies the current one first (a stolen or left-open session alone can't take the account over), and applies the same 8-72 character rules as registration from one shared definition, so the two can't drift apart. **After a successful change the frontend signs the user out and returns them to the login screen** (with a note): they prove the new password works. That is a UX choice, not a security control — see the next paragraph.

**Changing a password does not revoke sessions.** Sessions are stateless JWTs with a 24-hour expiry and no server-side session list, so a token minted before the change keeps working until it expires (a test pins this so a future change is deliberate). The password change protects *future logins*, not a session already open elsewhere; real revocation would need a token version or a denylist. A wrong current password is a `401`, which every other endpoint uses to mean "your session expired", so the client opts this one call out of its automatic sign-out (otherwise a typo would log you out).

**Auth error bodies never echo what you typed.** FastAPI's default 422 body includes each rejected value in `input`, and a cross-field validator's `input` is the *whole request body* — so "new password must differ from the current one" echoed both passwords straight back, and so did registration's length errors. Response bodies end up in logs and proxies, so on every `/api/auth/*` route that field is dropped (you still get which field failed and why). Found by reading the curl output while verifying the new endpoints, not by a test.

**What this auth system is not**: no password *reset* (a signed-in user can change their password, but there is no forgot-password flow), no email verification, no rate limiting on login attempts, no refresh tokens (a token simply stops working after 24h — sign in again), no audit log of who registered whom. All out of scope for a portfolio project's auth layer, same spirit as CLAUDE.md's original "no auth at all" line, which this directly supersedes — see What I built vs. what I'd add next.

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

### Two more detectors, run as a batch (Isolation Forest + forecast deviation)

The z-score above is the only detector that runs on write. Two more run as an admin-triggered **batch pass** over a trailing window (`POST /api/detectors/run-batch`, defaults: 7-day window, verdicts for the last 24 hours) and are deliberately different in what they look at:

| | Z-score | Isolation Forest | Forecast deviation |
|---|---|---|---|
| **Runs** | on write, per order | batch | batch |
| **Scores** | a region's orders this hour vs. its last 24 hours | one (region, brand, hour) bucket's `order_count`, `revenue`, `avg_order_value` *together*, vs. the window | a bucket's order count vs. a linear trend fitted on that series' earlier hours |
| **Catches** | sudden volume spikes or drops | an unusual *combination* (normal count, abnormal value) | a departure from trend — so steady growth isn't mistaken for an anomaly |
| **Needs** | 10 hours of region history | 10 buckets in the window | 10 earlier hours of that region+brand |
| **Code** | `detectors/zscore.py` | `detectors/isolation_forest.py` | `detectors/forecast_deviation.py` |

**Why batch, not per order (Isolation Forest).** A forest must be *fitted* on a window before it can score anything; there's no cheap incremental update the way a mean/stdev has. Fitting 100 trees inside `POST /api/orders` would put that cost on every write, for a verdict about an hour that isn't even over yet. The z-score's opposite shape — cheap to recompute — is exactly why detection-on-write works for it. It is fitted with a fixed seed (a verdict that changed between two runs over identical data couldn't be demoed or defended) and needs no feature scaling (tree splits are per-feature, so revenue in the thousands doesn't drown out order count in single digits).

**Forecast deviation reuses the forecast endpoint's regression.** `fit_linear_trend` is the same least-squares fit behind `GET /api/orders/forecast` (moved out of the router into `detectors/forecast_deviation.py` so a detector doesn't import from a router; the endpoint imports it from there, so there is still one implementation). Only the resolution differs: daily per product there, hourly per (region, brand) here. A bucket never trains the trend it is judged against, and quiet hours inside a series' span count as zero demand, not missing. The threshold is a named constant (`DEVIATION_THRESHOLD = 0.5`: an hour more than 50% above or below its trend), with the denominator floored at one order so a quiet series doesn't read "1 vs 0.2 expected" as 400%.

**All three verdicts live on the same order record — no parallel collection.** The batch stamps `is_anomaly_if`/`if_score` and `is_anomaly_forecast`/`forecast_expected`/`forecast_actual` onto the orders in each scored bucket, alongside the `anomaly`/`z_score`/`severity` the z-score wrote at ingest. Three consequences worth knowing:
- **`null` means "not scored", `false` means "scored, not flagged".** Every scored bucket is stamped, not just flagged ones, and there is deliberately no retroactive backfill beyond the trailing window — a verdict needs the window's context, and a bucket that has aged out of it can't be re-derived faithfully.
- **A bucket verdict lands on every order in the bucket** (one `update_many` per bucket), consistent with the z-score flagging every order past its threshold. So one flagged hour shows as several rows, as a z-score flood already did.
- **Agreement can differ within one incident.** The z-score is scored as each order *arrives*, so the first orders of a flood hour pass it while later ones flag; the batch verdicts cover the whole hour. In the captured example above, early flood orders have `detector_agreement: 2` and later ones `3`.

**Detector agreement.** `detector_agreement` (0-3) counts how many detectors flagged a record; a detector that hasn't scored it (`null`) is not agreement. `GET /api/alerts` uses it twice: it raises severity — an anomaly starts at its z-score tier (or `mild` if the z-score didn't flag, having no tier to lend) and each *additional* agreeing detector raises it one tier, capped at `severe` — and it breaks ties in the sort (severity, then agreement, then recency). The result: a 2-3 detector anomaly outranks a lone flag of the same base severity. One deliberate limit: severity still dominates agreement, so a 2-detector anomaly that only reaches `moderate` ranks *below* a lone `severe` z-score flag.

**What agreement does and doesn't prove.** The three detectors are different *methods*, not independent *data sources*: all read the same hourly order counts (Isolation Forest also revenue and order value), so a flood trips all three by construction. Agreement is corroboration that the anomaly isn't an artifact of one method's assumptions, not proof of a real-world cause. This is why the UI shows *which* detectors agreed rather than a single confidence number.

**A real bug the tests caught in the Isolation Forest.** Given a perfectly constant window (every hourly bucket identical), no tree can split anything, every score comes out at exactly 0.5 ("nothing is more isolated than anything else"), and scikit-learn's `predict()` labelled **all** of them outliers — every steady order listed as an anomaly. It is the forest's version of the z-score's `stdev == 0` guard. Fixed by flagging on the score itself (must exceed 0.5 by more than float noise) rather than trusting `predict()`; a regression test pins it.

**A complete outage is invisible to all three detectors.** A bucket exists only for an hour that had at least one order, so an hour with **zero** orders produces no bucket at all: there is no order record to carry a verdict, and the z-score only ever runs when an order arrives. Every detector here is order-driven, so "orders stopped entirely" — arguably the loudest anomaly a shop can have — is exactly the case none of them can see. (A *partial* drop, where some orders still arrive, is flagged: verified live, a drop from ~10 to 3 orders an hour tripped all three.) Fixing it is a design change, not a tweak: it needs a scheduled job that zero-fills the expected hours and raises a different kind of alert that isn't stamped on an order. Related: the batch also scores the *in-progress* hour on its partial count, so a "drop" is only trustworthy for a completed hour.

**The batch detectors are too noisy on low-count data — measured, not guessed.** On ~2,300 organic Nike orders over 14 days (roughly 150/day, so a region-hour averages about two orders), scoring the full last 7 days flagged **113 of 601 Isolation Forest buckets (19%)** and **201 of 580 forecast-deviation buckets (35%)**, on top of 67 z-score flags raised at ingest, and the anomaly API returned 500+ rows across 157 distinct hours — with no anomalies injected beyond two deliberate events. The causes are arithmetic, not bugs: at an expected count of 2, a 50% relative threshold is *one order* of ordinary Poisson noise, and a 3σ z-score on a tiny, sparse window fires on roughly 1% of hours. The default 24-hour scoring window limits how much of that reaches the UI (16 of 74 IF and 33 of 73 forecast buckets in the seeded run) but not the ratio. The fixes are calibration decisions rather than one-liners — a minimum *absolute* deviation for the forecast detector, count-aware (Poisson) thresholds, a per-series forest, or a minimum volume before the batch detectors run — so it is documented here, not silently retuned. It also means the anomaly panel's "newest 50 rows, unranked" view can be dominated by noise, and why the demand events were placed in the most recent hours.

**Limitations, stated plainly.** One forest is fitted across all (region, brand) buckets pooled together, so a naturally busy series is judged against quiet ones — a forest per series is the better design but needs far more history than a 7-day window of a simulated catalog gives. Forecast deviation's relative threshold is noisy at tiny counts (2 vs 4 orders is a 100% deviation). And there is no scheduler: a run happens when an admin triggers it, so batch verdicts lag the z-score until then (an order posted after the last run reads `null` for both).

### The explanation layer (Gemini) — narration, not detection

`POST /api/anomalies/{id}/explain` turns one already-flagged anomaly into 2-3 plain-English sentences and a one-line suggested action. **It does not detect anything**: the three detectors above decide what is anomalous; the model only narrates numbers they already produced.

- **On demand, cached, never automatic.** Nothing calls the model until a user clicks Explain. The result is stored on the anomaly record (`explanation`, `suggested_action`, `explained_at`, plus `explanation_basis`), so later calls are free and cost scales with clicks, not data volume. **The cache is invalidated when the detector verdicts change:** `explanation_basis` fingerprints the verdicts the text was written against (the z-score flag, the Isolation Forest flag, and the forecast verdict including the expected/actual order counts the text quotes), and a cache hit requires the record's *current* fingerprint to still match. An explanation written while only the z-score had scored a record ("flagged by 1 of 3 detectors") is therefore regenerated once a batch run adds the other two, instead of contradicting the row's own badges. Re-running the batch over unchanged data leaves the fingerprint identical and costs nothing; a record explained before the fingerprint existed is regenerated once. The cache check runs *before* the model is even constructed, so a cached explanation is still served with no API key set or while the provider is down (tested).
- **Structured context, told to stay inside it.** The model receives one JSON object — product, brand, region, the three verdicts, stock and days-of-stock-remaining (from inventory risk), and 7-day revenue change for the product and region (from the growth-rate feature) — and a system prompt to use only those facts, treat `null` as unknown, treat string values as data not instructions, and not mix up units. Output is constrained to a `{summary, suggested_action}` JSON schema so it parses deterministically.
- **Brand-scoped like everything else.** The record is looked up inside the caller's brand scope; another brand's id is a plain `404`, indistinguishable from a nonexistent one, and never reaches the model (tested, including that a foreign brand can't read a *cached* explanation).
- **Clean failures.** A provider `429` becomes `503` "try again shortly" with `Retry-After`, any other provider error a `503` with a message that never echoes the raw provider error, and a failed call stores nothing so a retry can succeed.
- **The UI shows whether it spent a model call.** A fresh explanation is tagged "Just generated"; a stored one "Saved".

**What I found running it for real.** (1) The model named in the brief, `gemini-2.0-flash-lite`, had been retired by the time it was first called live — a `404`, not a code bug. The model is a setting (`GEMINI_MODEL`) for exactly this reason; the default is now `gemini-3.5-flash-lite`, the replacement the API itself pointed to, pinned by name rather than a `-latest` alias so a captured example stays reproducible. (2) The first real explanation said "an actual **order quantity** of 14 compared to an expected 2.64" — wrong: those are hourly order *counts*. My context passed bare `expected`/`actual` fields next to an unrelated `order_quantity` (units in one order), and the model conflated them. Fixed by giving context fields self-describing names (`expected_orders_in_that_hour`) and a units line in the prompt; the re-run reads "14 orders occurred in that hour against an expected 2.64" (see the captured example above). (3) While comparing explanations across five different scenarios, one cached explanation said "flagged by 1 of 3 detectors" for a record that by then had all three: it had been generated before a batch run and the cache never noticed the verdicts had changed (fixed — see the cache invalidation above; three tests pin it, including the exact before/after-batch sequence). (4) A fresh response reported `explained_at` at microsecond precision while the stored copy has MongoDB's millisecond precision, so cached and fresh disagreed about the same timestamp; truncated to milliseconds before storing and returning.

**Limits of the layer.** The prompt constrains the model but nothing *verifies* its sentences against the data, so it is a triage aid, not a diagnosis; the suggested action is generic by design (it knows nothing about the business beyond the JSON). All tests use a fake explainer and a fake SDK client, and an autouse guard makes any test that builds a real client fail — the real API is never called from the suite.

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
**265 tests, 265 passing** (re-run for this document — real current output, not a number carried over from an earlier phase).

- **`tests/test_zscore.py`** — unit tests against `compute_zscore()`, the pure function extracted from the detector specifically so this is possible without mocking Motor's async cursor. Deterministic input → known output: normal values, extreme values in both directions once past the minimum window, the exact `MIN_WINDOW_SIZE - 1` boundary, the divide-by-zero guard, a test that independently recomputes the textbook z-score formula to confirm it's applied correctly, and a full pass over every severity-tier boundary (just under/at/just-under-the-next-tier for mild→moderate→severe, plus the negative-z and cold-start/non-anomalous cases).
- **`tests/test_orders_api.py`** — integration tests via FastAPI's `TestClient`, wired through `dependency_overrides` to a disposable `analytics_test` database (`tests/conftest.py`) — MongoDB must be reachable, but the real `analytics` database is never touched, and the test database is dropped after every test. Covers order validation (valid/invalid quantity/price/payment_status/brand), inventory decrement on order (including the no-matching-inventory case and floor-clamping at 0), pagination, CSV export headers/content for all export-capable endpoints, KPI/region/product period-over-period deltas (including the null-when-previous-is-zero guard), trend bucketing and its range/granularity validation, `/api/brands`, the business anomaly detector end-to-end (building a real 10-hour region baseline via explicit past timestamps, then flooding the current hour to trigger a real flag with the correct severity), `TestProductStatsTopBottomOverlapFix` (the bug fix above, against a deliberately tiny 3-product catalog), reorder-suggestion math (`TestInventoryRiskReorderSuggestion`), the forecast endpoint's fallback methods and sanity bounds (`TestForecast` — no NaN, no negative projections), and admin-only enforcement for the benchmark endpoint (`TestBrandsBenchmark` — 403 for a business account, correct `revenue_vs_average_pct` math).
- **`tests/test_auth_api.py`** — register/login/me, the bootstrap rule (first account free, forced to admin; every account after that needs an existing admin's token), registration validation (business role requires business_name + owned_brands together, duplicate email, password length, malformed email), and `TestBrandScopingIsolation` — the test class that exists specifically to prove a business account's request never returns another brand's data, exercised directly against every scoped endpoint (`/api/orders`, `/api/orders/kpis`, `/api/orders/products`, `/api/orders/regions`, `/api/inventory`, `/api/inventory/risk`, `/api/anomalies/business`), plus the inverse case (an admin sees every brand, a multi-brand business account sees exactly its own set).
- **`tests/test_profile_api.py`** — `PATCH /api/auth/me`: a business account renames itself (response and persisted value, whitespace trimmed, blank/overlong rejected), an admin gets 403, `owned_brands` / `role` / `email` are each rejected with a 422 that changes nothing, a valid name smuggled alongside a forbidden field is *not* applied, and the error names the offending field. `POST /api/auth/change-password`: correct current password succeeds and the new one logs in while the old is rejected; the response carries no password or hash; a wrong current password is 401 and the old password still works; length rules (7 rejected, 8 accepted, 73 rejected); new-must-differ; admins can change theirs too; validation errors never echo either password (and neither does a malformed login); and a pre-change token keeps working (the documented no-revocation limit).
- **`tests/test_alerts_api.py`** — `GET /api/alerts` end to end: each of the three signal sources surfaces correctly on its own (an anomaly, a HIGH-risk product, a declining product in isolation from region-level noise), the combined list sorts by severity then recency, and brand-scoping holds for a business account exactly the same way it does everywhere else in this app; and `TestAlertsBrandFilter` — the admin's `?brand=` narrows all four sources (low stock, anomalies, product and regional declines), an unfiltered admin sees every brand, an unknown brand is empty, and a business account passing another brand's name gets an identical feed.
- **`tests/test_batch_detectors.py`** (43, pure — no DB, no network) — Isolation Forest runs on a small synthetic window, flags an unmistakable outlier and nothing else, is deterministic, only scores recent buckets, and returns no verdicts below the cold-start size; the constant-window regression (see Anomaly detection); forecast-deviation math on known input (a deliberately way-off value is flagged, a normal one isn't, the threshold boundary is exclusive, a steady *trend* is followed rather than flagged, a zero expectation doesn't divide by zero, too little history gives no verdict); `detector_agreement` across every combination of flagged/unflagged/`null` verdicts; and the alert severity-bump and sort-key rules.
- **`tests/test_detectors_api.py`** — `POST /api/detectors/run-batch`: 401/403/422, an empty database, scoring a real flood scenario through the real ingest path, idempotent re-runs; the combined anomaly record (batch verdicts are `null` before a run and all three appear after one; batch flags list orders the z-score passed, with agreement 2; steady traffic is never listed even after a run); a business account sees only its own brand's verdicts; and alerts carrying `detectors`/`detector_agreement` with agreement-escalated severity and agreement-first ranking.
- **`tests/test_explain_api.py`** — `POST /api/anomalies/{id}/explain` with the Gemini client mocked throughout: 401 / malformed / unknown / unflagged ids are 404 and never call the model; the first call generates and stores, **the second and third make zero model calls** and return the identical stored text; a cached explanation is still served when the model is failing; a business account can't explain (or read a cached explanation of) another brand's anomaly and the model is never called for it; the context handed to the model is structured and JSON-serializable; a provider 429 is a clean 503 with `Retry-After` and nothing stored, so a retry can succeed; and the `GeminiExplainer` wrapper's error mapping (429 vs other API errors vs network failure vs unreadable reply, no leaking provider detail) against a fake SDK client. An autouse fixture in `conftest.py` makes any test that tries to build a real Gemini client fail outright, since a real key can sit in the dev `.env`.

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
14. **Multi-detector anomaly layer + on-demand LLM explanations** — added two batch detectors beside the live z-score: an Isolation Forest (scikit-learn; order_count/revenue/avg_order_value together, per region+brand+hour) and a forecast-deviation detector (reusing the forecast endpoint's regression at hourly resolution), both run by an admin-only `POST /api/detectors/run-batch` (manual trigger by design, no scheduler) and stamped onto the same order records as the z-score verdict. `GET /api/anomalies/business` now returns all three verdicts plus `detector_agreement`, which raises alert severity and ranks the feed; `AnomalyPanel`/`AlertsCenter` show which detectors agreed. On top of that, `POST /api/anomalies/{id}/explain` has Gemini narrate a flagged anomaly from the numbers the detectors produced (on demand, cached on the record, brand-scoped, rate limits are a clean 503). Built backend-first in four verified steps (each checked with real `curl` against seeded data before the next), then the frontend, then verified in a real browser as admin and business accounts, including a real Gemini call. Four real problems found and fixed along the way: the Isolation Forest flagged *every* bucket of a perfectly constant window as an outlier (caught by a test; see Anomaly detection); the model named in the brief (`gemini-2.0-flash-lite`) had been retired, so the model is now a setting with a working default; the first live explanation mislabelled hourly order counts as an "order quantity" because my context fields were ambiguous (fixed by unit-bearing field names); and fresh vs. cached `explained_at` disagreed at the millisecond because BSON dates truncate microseconds. 84 new tests (`test_batch_detectors.py`, `test_detectors_api.py`, `test_explain_api.py`) — 237 total, all passing.
15. **Explanation-cache invalidation, a multi-product Nike catalog, and a browsable demo history** — fixed the stale-explanation bug found while comparing five scenarios (the cache now stores a fingerprint of the detector verdicts the text was written against and regenerates when they change; 3 new tests), and documented the two limitations that were not quick fixes (a zero-order hour creates no bucket so an outage is invisible to the detectors; and a measured 19% / 35% flag rate from the batch detectors on low-count organic data). Nike went from one product to four (`sku-006` Dri-FIT t-shirt, `sku-007` duffel bag, `sku-008` heritage cap, added to `simulator.PRODUCTS`, the single source of truth that `seed_users.py` and the new `seed_history.py` read). `seed_history.py` backfills 14 days of Nike history (~2,350 orders) through the real ingest path with per-product, per-region demand, weekday and time-of-day shape, week-over-week trends in both directions, two real-looking demand events, and varied final stock including one HIGH- and one MEDIUM-risk item; the data was seeded into the dev database and deliberately left there. Two things I got wrong first and fixed before handing it over: the first seeding put the demand events two days back, where the batch's default 24-hour window never scored them and the newest-50 panel buried them (re-seeded with events in the last few hours), and running the batch over the full week surfaced the calibration finding above. Tests: 3 new (verdict-fingerprint invalidation), 240 total, all passing.
16. **Multi-page app with routing, and a profile page** — the single scrolling dashboard became five routed pages (`/overview`, `/products`, `/anomalies`, an admin-only `/benchmark`, `/profile`) behind a persistent navbar (active-route highlighting, the brand switcher moved into it) with two route guards: `RequireAuth` (signed out -> `/login`, resuming the requested page afterwards) and `RequireAdmin` (a business account that reaches `/benchmark` by any route sees a "Not available" page and the component never mounts, so no request is made). Backend first, verified with `curl` including every reject case: `PATCH /api/auth/me` (a business account renames itself; `owned_brands`/`role`/`email` are a 422 that changes nothing; admin is a 403) and `POST /api/auth/change-password` (wrong current password 401, same rules as registration, no password or hash in the response). Then the routing skeleton, verified in a real browser as admin and business (41 checks: redirects, both ways of reaching the blocked route, active highlighting, brand filter persisting across pages, and polling measured to stop while off a page and resume on return), then the profile forms last (30 checks, on a throwaway account so the demo logins were never touched). Decisions made with you: sign the user out after a password change, 403 for admin and 422 for unsupported fields. Real problems found and fixed on the way: (1) the 422 for "new password must differ" echoed *both passwords* back in the response body (FastAPI's default `input` field) — and registration had the same leak — so `input` is now dropped on every `/api/auth/*` route; (2) `BrandSwitcher` never loaded its brands in dev, a pre-existing `useApiData` bug exposed by StrictMode's double-mount (see Frontend pages and navigation); (3) a wrong current password is a 401, which the client treats as "session expired" and would have signed the user out over a typo, so that one call opts out; (4) my first design passed the "password changed" notice through router state and lost a race with the route guard's own redirect, and then resetting a flag in `login()` sent the user back to the old page — both caught by the browser checks, both fixed by carrying that state in `AuthContext`. Also added `frontend/vercel.json` (a client-routed app 404s on reload without a rewrite to `index.html`) and removed the old header CSS. Not verified in a browser: the "session expired" notice path (a 401 mid-session). Tests: 21 new (`test_profile_api.py`), 261 total, all passing.
17. **Consistent admin brand filter across every page** — audited every API call on `/overview` and `/anomalies`: all but one already passed the brand, and the exception was the Alerts Center, whose endpoint (`GET /api/alerts`) had no `?brand=` at all — so the README's claim that it did was wrong. Added the optional admin-only `brand` to it (same `brand_match_stage` pattern, threaded through all four alert sources) and wired `AlertsCenter` to the filter. The bigger cause of the "works on some pages" impression was not missing wiring: `useApiData` only refetched on its next poll tick after a query change (measured 3.2 s for a brand change, 5.0 s for a range toggle, with panels catching up at different moments), fixed by making the fetch function an effect dependency (169 ms / 232 ms after) — which also removed a now-redundant ref and a lint warning. Benchmark is unchanged on purpose (a cross-brand comparison can't be narrowed to one brand). Verified live as admin on every page, comparing what each panel *displays* to what the API returns for that brand (Nike and, as the discriminating case, Sony): Overview's KPIs, trend, regions and recent orders, the anomaly panel and the Alerts Center all narrow to the brand and return to everything on "All brands"; Products and Benchmark still behave as before; the filter defaults back to "All brands" after sign-out/sign-in (30 checks), and the earlier 41 navigation/guard/polling checks still pass. Tests: 4 new (`TestAlertsBrandFilter`), 265 total, all passing.

## What I built vs. what I'd add next

**Built:** a full-stack app that ingests, detects, stores, and visualizes streaming order events in near-real-time — KPIs with period-over-period deltas, a revenue trend chart with an optional demand-forecast overlay, regional revenue and product performance (both with period-over-period growth rates), inventory risk with days-of-stock-remaining and reorder suggestions, a statistical/ML anomaly-detection layer (a live z-score, plus batch Isolation Forest and forecast-deviation detectors whose agreement drives alert severity and ranking) with an on-demand LLM explanation layer on top — the model narrates what the detectors flagged, it does not detect anything — a consolidated cross-signal alerts feed, and admin-only cross-brand benchmarking — all behind JWT-based role/brand-scoped auth with CSV export — as a routed five-page app (Overview, Products & Inventory, Anomalies & Alerts, an admin-only Cross-Brand Benchmark behind its own route guard, and a Profile page where a business account can rename itself and anyone can change their password) with an automated 265-test suite, run locally (see Setup below); not currently deployed anywhere public.

**What I'd add next, specifically (not generic "more tests" filler):**
- **The z-score masking effect** is a known property of any rolling-window z-score detector (see Anomaly detection) — not specifically reproduced against the current hourly-bucket design, but worth fixing regardless. A fix would need either a secondary check that excludes already-flagged points from a window's own baseline, or a longer accumulation window traded against slower cold-start — a real design decision, not a one-line patch.
- **Per-series Isolation Forests and a scheduler.** The forest is fitted on all (region, brand) buckets pooled, so a busy series is judged against quiet ones; one forest per series is the better design but needs far more history than a simulated 7-day window has. And the batch pass is manual: a scheduler (APScheduler, calling the same `run_batch`) is the natural next step once someone wants verdicts without clicking.
- **Detect the absence of orders.** An hour with zero orders creates no bucket, so a full outage is invisible to all three detectors (see Anomaly detection). It needs a scheduled job that zero-fills expected hours and a separate alert type; it can't be a verdict stamped on an order record.
- **Calibrate the batch detectors for low-count data.** 19% of Isolation Forest buckets and 35% of forecast buckets flagged on organic data (numbers in Anomaly detection). A minimum absolute deviation on the forecast detector and a minimum bucket volume are the cheapest first steps; a rank/severity ordering in the anomaly panel (it currently shows the newest 50, unranked) would stop noise burying real events.
- **One incident is many rows.** Verdicts are stamped on every order in a flagged bucket, so a single flood hour appears as dozens of anomaly/alert rows (37 alerts in the captured example, from one engineered flood). Collapsing an incident to one row on read is the obvious UX fix; it was left out because it changes what the existing z-score list returns.
- **Nothing verifies the LLM's sentences against the data.** The prompt constrains it and the context is structured, but a wrong sentence (as the units mix-up above showed is possible) is only caught by a human reading it. A cheap grounding check — every number in the explanation must appear in the context — would catch that class of error mechanically.
- **The three detectors are not independent evidence.** They are three methods over the same hourly order counts, so agreement means "robust to the choice of method", not "confirmed by a second source". A detector reading something else (payment failures, inventory movements) would make agreement mean more.
- **No refresh tokens, password reset, or rate limiting on login** — see Auth's "what this auth system is not." A real product needs at least the first two; none were worth the added complexity for a portfolio auth layer whose main point is demonstrating role/brand-scoped authorization correctly.
- **The test-database index gap** (see Known issues) is patched at the endpoint level for the one case it actually broke, but the harness-level cause — no test environment ever runs `ensure_indexes()` against the disposable test database — is still there, waiting for the next feature that assumes a database-level constraint.
- **No CI** — 265 tests exist and pass locally, but nothing runs them automatically on push. A GitHub Actions workflow would be the natural next step.
- **The simulator is the only data source, ever** — there is no real production traffic behind this project; a deployed instance would need someone to run the simulator manually to look alive. Said plainly rather than left to be discovered.
- **Render's free-tier cold start (30–60s)** would be a real, visible rough edge on a deployed instance. A paid tier or a scheduled keep-alive ping would fix it — not a concern while this only runs locally.
- **Atlas's `0.0.0.0/0` network access** is a real, acknowledged trade-off of the free tier's lack of a static IP — see Known issues.
- **The forecast is deliberately simple** (moving/linear trend over ~42 days, no seasonality, no external signals) — chosen so it's explainable line-by-line in an interview, not because it's the best available technique. A real forecasting feature would want at minimum a seasonality check (day-of-week effects are very real for order volume) before this is trusted for anything beyond a rough overlay.
- **The alert thresholds (decline %, reorder lead-time buffer, urgency margin) are fixed constants**, not per-brand configurable — fine for a single demo dataset, a real multi-tenant version would want these as settings per brand, since a 20% week-over-week swing means something very different for a high-volume vs. a low-volume product line.

## Built with Claude Code

This project was built across 10 guided phases in collaboration with Claude Code (Anthropic's CLI agent), directed one phase at a time against the plan in `CLAUDE.md`, then later fully rewritten from system-metrics to this e-commerce business-analytics domain, then extended again with role-based auth, trend/deltas/severity, and CSV export, then once more with a bug fix and five business-value features (Build log's phases 11–13) — in every case with explicit scope per step and independent verification before moving on: live `curl`/`mongosh`/pytest checks against real data, and — for the auth/frontend work specifically — real browser screenshots confirming brand isolation held in the actual UI, not just reading generated code and trusting it. That verification loop is where most of the specific findings in this README came from: a real Windows port conflict, a real async event-loop bug in the test suite, real input-validation bugs caught by testing edge cases directly (`NaN`, an oversized query param), a real z-score masking effect confirmed against genuine production data during the original deployment, a naive/aware datetime bug and a cold-start-defeating zero-padding bug during the business-analytics rewrite, a `passlib`/`bcrypt` incompatibility and a test-database index gap during the auth build, and — during the bug-fix-and-features phase — a negative-length crash in the z-score detector found by deliberately backdating a test order, and a second naive/aware datetime inconsistency in the new alerts feed caught by consistency-checking against the rest of the codebase before it ever shipped, and — during the detector/LLM phase — an Isolation Forest that flagged a perfectly constant window in its entirety (caught by a test), a retired hosted model found only by calling it live, and an LLM explanation that mislabelled a count as a quantity because of ambiguous field names in the prompt context. Worth being upfront about, and a fair thing to walk through in an interview — what was asked for, what was checked, and what changed as a result of checking.
