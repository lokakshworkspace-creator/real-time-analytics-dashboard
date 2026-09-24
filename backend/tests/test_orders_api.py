"""Integration tests against the real FastAPI app, via TestClient, but
wired to a dedicated test database (see conftest.py's `api_client`
fixture) — never the real `analytics` database.

Covers: POST /api/orders validation + computed fields + inventory
decrement, GET /api/orders pagination + CSV export, the aggregation
endpoints (kpis/regions/products/trend/inventory/risk), and the
region-hour order-volume anomaly detector end-to-end via
GET /api/anomalies/business. Every GET endpoint here is exercised with
`admin_headers` (see conftest.py) unless the test is specifically about
role scoping — that's test_auth_api.py's job, kept separate so a
brand-scoping regression fails loudly under its own name rather than
buried among these functional tests.
"""

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

# A fixed reference hour rather than datetime.now(): the anomaly
# detector buckets by wall-clock hour (see detectors/zscore.py), so
# building a deterministic 24h-trailing-window test around real "now"
# would flake if the suite happened to run within a few seconds of an
# hour boundary. Every timestamp below is expressed relative to this.
REFERENCE_HOUR = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

DEFAULT_ORDER = {
    "product_id": "sku-001",
    "product_name": "Nike Running Shoes",
    "category": "Apparel",
    "brand": "Nike",
    "quantity": 1,
    "unit_price": 50.0,
    "region": "North America",
}


def post_order(client, *, timestamp: datetime | None = None, **overrides):
    payload = {"order_id": str(uuid.uuid4()), **DEFAULT_ORDER, **overrides}
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return client.post("/api/orders", json=payload)


def seed_inventory(client, *, product_id="sku-001", region="North America", current_stock=300, brand="Nike"):
    return client.post(
        "/api/inventory/seed",
        json={
            "product_id": product_id,
            "product_name": DEFAULT_ORDER["product_name"],
            "category": DEFAULT_ORDER["category"],
            "brand": brand,
            "region": region,
            "current_stock": current_stock,
        },
    )


class TestPostOrdersValidation:
    def test_valid_payload_returns_201_with_computed_total_value(self, api_client):
        response = post_order(api_client, quantity=3, unit_price=10.0)

        assert response.status_code == 201
        body = response.json()
        assert body["total_value"] == 30.0
        assert body["brand"] == "Nike"
        assert body["anomaly"] is False  # cold start — no prior hourly history yet
        assert body["payment_status"] == "success"  # default
        assert body["timestamp"].endswith("Z")
        assert "id" in body and body["id"]

    def test_zero_quantity_returns_422(self, api_client):
        assert post_order(api_client, quantity=0).status_code == 422

    def test_negative_unit_price_returns_422(self, api_client):
        assert post_order(api_client, unit_price=-5.0).status_code == 422

    def test_missing_required_field_returns_422(self, api_client):
        payload = {**DEFAULT_ORDER, "order_id": str(uuid.uuid4())}
        del payload["region"]
        response = api_client.post("/api/orders", json=payload)

        assert response.status_code == 422

    def test_missing_brand_returns_422(self, api_client):
        payload = {**DEFAULT_ORDER, "order_id": str(uuid.uuid4())}
        del payload["brand"]
        response = api_client.post("/api/orders", json=payload)

        assert response.status_code == 422

    def test_empty_region_returns_422(self, api_client):
        assert post_order(api_client, region="").status_code == 422

    def test_explicit_payment_status_is_honored(self, api_client):
        response = post_order(api_client, payment_status="failed")

        assert response.status_code == 201
        assert response.json()["payment_status"] == "failed"

    def test_invalid_payment_status_returns_422(self, api_client):
        assert post_order(api_client, payment_status="pending").status_code == 422

    def test_post_orders_requires_no_auth(self, api_client):
        # Deliberate: the simulator/order-intake path stays open — see
        # routers/orders.py's create_order docstring. Confirming this
        # explicitly so it can't silently regress into requiring a
        # token (which would break the simulator) without a test
        # noticing.
        response = api_client.post("/api/orders", json={"order_id": "no-auth-check", **DEFAULT_ORDER})

        assert response.status_code == 201

    def test_a_backdated_order_older_than_the_regions_existing_history_does_not_crash(
        self, api_client
    ):
        # Regression test for a real bug found while building Feature 2:
        # score_order_volume() computed
        # available_hours = (this order's hour) - (region's earliest
        # order's hour), which goes NEGATIVE when a new order's
        # timestamp is older than every order already stored for that
        # region (e.g. a backdated/historical order, or building
        # period-over-period test fixtures) — Motor's to_list() then
        # raised "ValueError: length must be non-negative", a 500 on
        # what should be an ordinary POST. Fixed by flooring
        # available_hours at 0 (see detectors/zscore.py).
        post_order(api_client, region="Backdate Test Region", timestamp=REFERENCE_HOUR)

        older_timestamp = REFERENCE_HOUR - timedelta(days=10)
        response = post_order(api_client, region="Backdate Test Region", timestamp=older_timestamp)

        assert response.status_code == 201


class TestInventoryDecrement:
    def test_posting_an_order_decrements_matching_inventory(self, api_client, admin_headers):
        seed_inventory(api_client, current_stock=300)
        post_order(api_client, quantity=5)

        inventory = api_client.get("/api/inventory", headers=admin_headers).json()
        assert len(inventory) == 1
        assert inventory[0]["current_stock"] == 295

    def test_order_without_matching_inventory_still_succeeds(self, api_client):
        # No seed call first — the order should still be recorded (see
        # routers/orders.py's warning-and-continue behavior).
        response = post_order(api_client, product_id="sku-999", region="Unseeded Region")

        assert response.status_code == 201

    def test_order_exceeding_stock_is_accepted_and_stock_clamps_at_zero(self, api_client, admin_headers):
        # A stockout doesn't block the sale (a deliberate scope
        # decision — see routers/orders.py) and current_stock must
        # never go negative, unlike a plain $inc.
        seed_inventory(api_client, current_stock=3)
        response = post_order(api_client, quantity=10)

        assert response.status_code == 201

        inventory = api_client.get("/api/inventory", headers=admin_headers).json()
        assert inventory[0]["current_stock"] == 0

    def test_further_orders_after_stockout_keep_stock_at_zero(self, api_client, admin_headers):
        seed_inventory(api_client, current_stock=2)
        post_order(api_client, quantity=5)  # already clamps to 0
        post_order(api_client, quantity=1)  # would go to -1 with a plain $inc

        inventory = api_client.get("/api/inventory", headers=admin_headers).json()
        assert inventory[0]["current_stock"] == 0


class TestGetOrders:
    def test_requires_auth(self, api_client):
        response = api_client.get("/api/orders")

        assert response.status_code == 401

    def test_returns_newest_first(self, api_client, admin_headers):
        post_order(api_client, timestamp=REFERENCE_HOUR, order_id="first")
        post_order(api_client, timestamp=REFERENCE_HOUR + timedelta(minutes=5), order_id="second")

        response = api_client.get("/api/orders", headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["order_id"] == "second"
        assert body[1]["order_id"] == "first"

    def test_limit_and_skip_paginate(self, api_client, admin_headers):
        for i in range(5):
            post_order(api_client, timestamp=REFERENCE_HOUR + timedelta(minutes=i), order_id=str(i))

        response = api_client.get("/api/orders", params={"limit": 2, "skip": 1}, headers=admin_headers)

        assert [o["order_id"] for o in response.json()] == ["3", "2"]

    def test_empty_database_returns_empty_list(self, api_client, admin_headers):
        response = api_client.get("/api/orders", headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == []

    def test_csv_format_returns_csv_content_type_and_header_row(self, api_client, admin_headers):
        post_order(api_client, order_id="csv-test")

        response = api_client.get("/api/orders", params={"format": "csv"}, headers=admin_headers)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert 'attachment; filename="orders.csv"' in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].split(",")[:3] == ["id", "order_id", "timestamp"]
        assert "csv-test" in response.text


class TestKpis:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/orders/kpis").status_code == 401

    def test_empty_window_returns_zeros_not_404(self, api_client, admin_headers):
        response = api_client.get("/api/orders/kpis", params={"range": "7d"}, headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["range"] == "7d"
        assert body["current"] == {
            "total_orders": 0, "revenue": 0.0, "units_sold": 0, "avg_order_value": 0.0,
        }
        assert body["previous"] == body["current"]
        assert all(v is None for v in body["change_pct"].values())

    def test_kpis_aggregate_posted_orders_into_current_period(self, api_client, admin_headers):
        post_order(api_client, quantity=2, unit_price=10.0)  # total_value 20
        post_order(api_client, quantity=1, unit_price=30.0)  # total_value 30

        response = api_client.get("/api/orders/kpis", params={"range": "7d"}, headers=admin_headers)

        body = response.json()["current"]
        assert body["total_orders"] == 2
        assert body["revenue"] == 50.0
        assert body["units_sold"] == 3
        assert body["avg_order_value"] == 25.0

    def test_change_pct_computed_against_previous_period(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        # Previous 7d window: one order worth 100.
        post_order(api_client, quantity=1, unit_price=100.0, timestamp=now - timedelta(days=10))
        # Current 7d window: one order worth 150 -> +50%.
        post_order(api_client, quantity=1, unit_price=150.0, timestamp=now - timedelta(days=1))

        response = api_client.get("/api/orders/kpis", params={"range": "7d"}, headers=admin_headers)

        body = response.json()
        assert body["previous"]["revenue"] == 100.0
        assert body["current"]["revenue"] == 150.0
        assert body["change_pct"]["revenue"] == 50.0

    def test_invalid_range_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/kpis", params={"range": "3d"}, headers=admin_headers)

        assert response.status_code == 422


class TestRegionStats:
    def test_grouped_by_region_sorted_by_current_revenue_desc(self, api_client, admin_headers):
        post_order(api_client, region="Europe", quantity=1, unit_price=10.0)
        post_order(api_client, region="Asia Pacific", quantity=1, unit_price=100.0)

        response = api_client.get("/api/orders/regions", params={"range": "7d"}, headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert body[0]["region"] == "Asia Pacific"
        assert body[0]["current"]["revenue"] == 100.0
        assert body[1]["region"] == "Europe"

    def test_requires_auth(self, api_client):
        assert api_client.get("/api/orders/regions").status_code == 401

    def test_invalid_range_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/regions", params={"range": "3d"}, headers=admin_headers)

        assert response.status_code == 422

    def test_change_pct_computed_against_previous_period(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        # Previous 7d window: one Europe order worth 100.
        post_order(api_client, region="Europe", quantity=1, unit_price=100.0, timestamp=now - timedelta(days=10))
        # Current 7d window: one Europe order worth 150 -> +50%.
        post_order(api_client, region="Europe", quantity=1, unit_price=150.0, timestamp=now - timedelta(days=1))

        response = api_client.get("/api/orders/regions", params={"range": "7d"}, headers=admin_headers)

        europe = next(r for r in response.json() if r["region"] == "Europe")
        assert europe["previous"]["revenue"] == 100.0
        assert europe["current"]["revenue"] == 150.0
        assert europe["change_pct"]["revenue"] == 50.0

    def test_change_pct_is_null_when_previous_period_had_no_orders(self, api_client, admin_headers):
        post_order(api_client, region="Europe", quantity=1, unit_price=100.0)

        response = api_client.get("/api/orders/regions", params={"range": "7d"}, headers=admin_headers)

        europe = next(r for r in response.json() if r["region"] == "Europe")
        assert europe["previous"]["revenue"] == 0.0
        assert europe["change_pct"]["revenue"] is None


class TestProductStats:
    def test_top_order_sorts_revenue_descending(self, api_client, admin_headers):
        post_order(api_client, product_id="sku-a", product_name="A", brand="BrandA", quantity=1, unit_price=5.0)
        post_order(api_client, product_id="sku-b", product_name="B", brand="BrandB", quantity=1, unit_price=500.0)

        response = api_client.get("/api/orders/products", params={"order": "top"}, headers=admin_headers)

        products = response.json()["products"]
        assert products[0]["product_id"] == "sku-b"
        assert products[0]["brand"] == "BrandB"

    def test_bottom_order_sorts_revenue_ascending(self, api_client, admin_headers):
        post_order(api_client, product_id="sku-a", product_name="A", brand="BrandA", quantity=1, unit_price=5.0)
        post_order(api_client, product_id="sku-b", product_name="B", brand="BrandB", quantity=1, unit_price=500.0)

        response = api_client.get("/api/orders/products", params={"order": "bottom"}, headers=admin_headers)

        products = response.json()["products"]
        assert products[0]["product_id"] == "sku-a"

    def test_invalid_order_param_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/products", params={"order": "sideways"}, headers=admin_headers)

        assert response.status_code == 422

    def test_csv_format(self, api_client, admin_headers):
        post_order(api_client, product_id="sku-a", product_name="A", brand="BrandA")

        response = api_client.get(
            "/api/orders/products", params={"format": "csv"}, headers=admin_headers
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "sku-a" in response.text

    def test_requires_auth(self, api_client):
        assert api_client.get("/api/orders/products").status_code == 401

    def test_invalid_range_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/products", params={"range": "3d"}, headers=admin_headers)

        assert response.status_code == 422

    def test_change_pct_computed_against_previous_period(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        post_order(
            api_client, product_id="sku-a", product_name="A", brand="BrandA",
            quantity=1, unit_price=100.0, timestamp=now - timedelta(days=10),
        )
        post_order(
            api_client, product_id="sku-a", product_name="A", brand="BrandA",
            quantity=1, unit_price=200.0, timestamp=now - timedelta(days=1),
        )

        response = api_client.get(
            "/api/orders/products", params={"range": "7d"}, headers=admin_headers
        )

        sku_a = next(p for p in response.json()["products"] if p["product_id"] == "sku-a")
        assert sku_a["previous"]["revenue"] == 100.0
        assert sku_a["current"]["revenue"] == 200.0
        assert sku_a["change_pct"]["revenue"] == 100.0

    def test_change_pct_is_null_when_previous_period_had_no_orders(self, api_client, admin_headers):
        post_order(api_client, product_id="sku-a", product_name="A", brand="BrandA", unit_price=100.0)

        response = api_client.get(
            "/api/orders/products", params={"range": "7d"}, headers=admin_headers
        )

        sku_a = next(p for p in response.json()["products"] if p["product_id"] == "sku-a")
        assert sku_a["previous"]["revenue"] == 0.0
        assert sku_a["change_pct"]["revenue"] is None


class TestProductStatsTopBottomOverlapFix:
    """The bug: with a small catalog, GET .../products?order=top and
    ?order=bottom used to both just $sort+$limit independently, so a
    5-product catalog with limit=5 on each returned the SAME 5 products
    under both directions — merely reordered, not actually distinct
    "top" and "bottom" performers. Fixed by fetching the full sorted
    list once and capping each direction to its own non-overlapping
    half — see get_product_stats's docstring for the exact split.
    """

    def _seed_tiny_catalog(self, api_client):
        # 3 distinct products, deliberately small enough that
        # requesting limit=3 on both top and bottom (as a real caller
        # plausibly would, mirroring PRODUCT_LIMIT in the frontend)
        # would overlap under the old (buggy) implementation.
        for product_id, price in [("sku-1", 10.0), ("sku-2", 50.0), ("sku-3", 100.0)]:
            post_order(
                api_client,
                product_id=product_id,
                product_name=product_id,
                brand="TinyBrand",
                quantity=1,
                unit_price=price,
            )

    def test_top_and_bottom_never_share_a_product_id_on_a_tiny_catalog(self, api_client, admin_headers):
        self._seed_tiny_catalog(api_client)

        top = api_client.get(
            "/api/orders/products", params={"limit": 3, "order": "top"}, headers=admin_headers
        ).json()["products"]
        bottom = api_client.get(
            "/api/orders/products", params={"limit": 3, "order": "bottom"}, headers=admin_headers
        ).json()["products"]

        top_ids = {p["product_id"] for p in top}
        bottom_ids = {p["product_id"] for p in bottom}
        assert top_ids.isdisjoint(bottom_ids)
        # 3 products -> ceil(3/2)=2 top, floor(3/2)=1 bottom (the exact
        # split, not just "no overlap" — confirms the halves, not a
        # looser property that would also pass on an accidental fix).
        assert top_ids == {"sku-3", "sku-2"}  # highest, then second-highest revenue
        assert bottom_ids == {"sku-1"}  # lowest revenue

    def test_note_explains_the_cap_when_it_actually_happens(self, api_client, admin_headers):
        self._seed_tiny_catalog(api_client)

        response = api_client.get(
            "/api/orders/products", params={"limit": 3, "order": "top"}, headers=admin_headers
        )

        body = response.json()
        assert body["note"] is not None
        assert "3" in body["note"]  # total distinct product count mentioned

    def test_note_is_null_when_the_catalog_is_big_enough_for_the_requested_limit(
        self, api_client, admin_headers
    ):
        self._seed_tiny_catalog(api_client)

        # limit=1 fits well within ceil(3/2)=2 — no capping needed.
        response = api_client.get(
            "/api/orders/products", params={"limit": 1, "order": "top"}, headers=admin_headers
        )

        body = response.json()
        assert body["note"] is None
        assert len(body["products"]) == 1

    def test_large_catalog_is_unaffected_by_the_cap(self, api_client, admin_headers):
        # 10 products, limit=3 each direction -> nowhere near needing a
        # cap (ceil(10/2)=5, floor(10/2)=5) — behaves exactly as before.
        for i in range(10):
            post_order(
                api_client,
                product_id=f"sku-many-{i}",
                product_name=f"Product {i}",
                brand="BigBrand",
                quantity=1,
                unit_price=float(i + 1),
            )

        top = api_client.get(
            "/api/orders/products", params={"limit": 3, "order": "top"}, headers=admin_headers
        ).json()
        bottom = api_client.get(
            "/api/orders/products", params={"limit": 3, "order": "bottom"}, headers=admin_headers
        ).json()

        assert top["note"] is None
        assert bottom["note"] is None
        assert len(top["products"]) == 3
        assert len(bottom["products"]) == 3
        top_ids = {p["product_id"] for p in top["products"]}
        bottom_ids = {p["product_id"] for p in bottom["products"]}
        assert top_ids.isdisjoint(bottom_ids)


class TestTrend:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/orders/trend").status_code == 401

    def test_default_range_and_granularity(self, api_client, admin_headers):
        post_order(api_client)

        response = api_client.get("/api/orders/trend", headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["orders"] == 1
        assert body[0]["period"]  # a "YYYY-MM-DD" string

    def test_nonsensical_combo_returns_422(self, api_client, admin_headers):
        response = api_client.get(
            "/api/orders/trend", params={"range": "7d", "granularity": "month"}, headers=admin_headers
        )

        assert response.status_code == 422

    def test_allowed_combo_succeeds(self, api_client, admin_headers):
        response = api_client.get(
            "/api/orders/trend", params={"range": "1y", "granularity": "week"}, headers=admin_headers
        )

        assert response.status_code == 200

    def test_invalid_range_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/trend", params={"range": "3d"}, headers=admin_headers)

        assert response.status_code == 422


class TestForecast:
    def test_requires_auth(self, api_client):
        response = api_client.get("/api/orders/forecast", params={"product_id": "sku-001"})

        assert response.status_code == 401

    def test_missing_product_id_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/orders/forecast", headers=admin_headers)

        assert response.status_code == 422

    def test_horizon_out_of_range_returns_422(self, api_client, admin_headers):
        assert api_client.get(
            "/api/orders/forecast", params={"product_id": "sku-001", "horizon": 0}, headers=admin_headers
        ).status_code == 422
        assert api_client.get(
            "/api/orders/forecast", params={"product_id": "sku-001", "horizon": 31}, headers=admin_headers
        ).status_code == 422

    def test_no_history_falls_back_to_zero_projection(self, api_client, admin_headers):
        response = api_client.get(
            "/api/orders/forecast",
            params={"product_id": "sku-never-ordered", "horizon": 5},
            headers=admin_headers,
        )

        body = response.json()
        assert body["method"] == "insufficient_history_zero_projection"
        assert len(body["forecast"]) == 5
        for point in body["forecast"]:
            assert point["projected_orders"] == 0.0
            assert point["projected_revenue"] == 0.0

    def test_single_days_history_falls_back_to_flat_projection(self, api_client, admin_headers):
        # "Today" is deliberately excluded from the history window (an
        # in-progress day's count is artificially low, not a real daily
        # total — see get_order_forecast's window bound) — so this uses
        # yesterday, not the default "now" timestamp, to land inside
        # the counted history.
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        post_order(
            api_client, product_id="sku-flat-test", quantity=3, unit_price=10.0, timestamp=yesterday,
        )  # total_value 30, one distinct history day

        response = api_client.get(
            "/api/orders/forecast", params={"product_id": "sku-flat-test", "horizon": 3}, headers=admin_headers
        )

        body = response.json()
        assert body["method"] == "insufficient_history_flat_projection"
        # Every projected day repeats that single day's actual totals.
        for point in body["forecast"]:
            assert point["projected_orders"] == 1.0
            assert point["projected_revenue"] == 30.0

    def test_multi_day_history_uses_linear_regression_and_produces_sane_numbers(
        self, api_client, admin_headers
    ):
        now = datetime.now(timezone.utc)
        # A clear, deliberate upward trend: 2 orders/day 10 days ago,
        # ramping to 20 orders/day yesterday.
        for days_ago in range(10, 0, -1):
            count = round(2 + (20 - 2) * (10 - days_ago) / 9)
            ts = now - timedelta(days=days_ago, hours=1)  # stay clear of "today"
            for _ in range(count):
                post_order(
                    api_client, product_id="sku-trend-test", quantity=1, unit_price=10.0, timestamp=ts
                )

        response = api_client.get(
            "/api/orders/forecast", params={"product_id": "sku-trend-test", "horizon": 7}, headers=admin_headers
        )

        body = response.json()
        assert body["method"] == "linear_regression_last_42_days"
        forecast = body["forecast"]
        assert len(forecast) == 7

        # Sanity, not exact-value assertions (per the brief): every
        # projection is a finite, non-negative number, and a clear
        # upward-trending history projects a non-decreasing near-term
        # future, not a random or wildly-swinging series.
        for point in forecast:
            assert point["projected_orders"] >= 0
            assert point["projected_revenue"] >= 0
            assert point["projected_orders"] == point["projected_orders"]  # NaN != NaN
        assert forecast[-1]["projected_orders"] >= forecast[0]["projected_orders"]

    def test_projection_never_goes_negative_on_a_downward_trend(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        # Sharp downward trend: 30 orders/day 10 days ago, down to 1
        # order/day yesterday — naive extrapolation would go negative
        # well within a 30-day horizon.
        for days_ago in range(10, 0, -1):
            count = max(1, round(30 - (30 - 1) * (10 - days_ago) / 9))
            ts = now - timedelta(days=days_ago, hours=1)
            for _ in range(count):
                post_order(
                    api_client, product_id="sku-decline-test", quantity=1, unit_price=5.0, timestamp=ts
                )

        response = api_client.get(
            "/api/orders/forecast",
            params={"product_id": "sku-decline-test", "horizon": 30},
            headers=admin_headers,
        )

        for point in response.json()["forecast"]:
            assert point["projected_orders"] >= 0.0
            assert point["projected_revenue"] >= 0.0

    def test_business_account_forecasting_another_brands_product_sees_no_history(
        self, api_client, admin_headers, business_headers_factory
    ):
        post_order(api_client, product_id="sku-other-brand", brand="OtherBrand", quantity=5)
        nike_headers = business_headers_factory("nike-forecast@test.example.com", ["Nike"])

        response = api_client.get(
            "/api/orders/forecast", params={"product_id": "sku-other-brand"}, headers=nike_headers
        )

        # Not a 403 — brand scoping just means a Nike account never
        # sees OtherBrand's order history, so this looks identical to
        # "this product has never been ordered" (see get_order_forecast's
        # docstring for why that's the deliberate, simpler behavior).
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "insufficient_history_zero_projection"


class TestInventorySeedAndList:
    def test_seed_then_list(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=150)

        response = api_client.get("/api/inventory", headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["current_stock"] == 150
        assert body[0]["brand"] == "Nike"

    def test_reseeding_the_same_product_region_upserts_not_duplicates(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=150)
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=200)

        body = api_client.get("/api/inventory", headers=admin_headers).json()
        assert len(body) == 1
        assert body[0]["current_stock"] == 200

    def test_get_inventory_requires_auth(self, api_client):
        assert api_client.get("/api/inventory").status_code == 401


class TestInventoryRisk:
    def test_high_risk_when_stock_low_and_demand_meets_or_exceeds_it(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=5)
        post_order(api_client, product_id="sku-001", region="North America", quantity=10)

        response = api_client.get("/api/inventory/risk", headers=admin_headers)

        body = response.json()
        assert len(body) == 1
        assert body[0]["risk"] == "HIGH"
        assert body[0]["recent_demand"] == 10
        assert body[0]["brand"] == "Nike"

    def test_medium_risk_when_stock_low_but_demand_hasnt_caught_up(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=15)
        post_order(api_client, product_id="sku-001", region="North America", quantity=1)

        response = api_client.get(
            "/api/inventory/risk", params={"low_stock_threshold": 20}, headers=admin_headers
        )

        body = response.json()
        assert body[0]["risk"] == "MEDIUM"

    def test_low_risk_when_stock_is_healthy(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=300)

        response = api_client.get("/api/inventory/risk", headers=admin_headers)

        body = response.json()
        assert body[0]["risk"] == "LOW"
        assert body[0]["recent_demand"] == 0

    def test_csv_format(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=300)

        response = api_client.get(
            "/api/inventory/risk", params={"format": "csv"}, headers=admin_headers
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "sku-001" in response.text


class TestInventoryRiskReorderSuggestion:
    """Feature: days_of_stock_remaining + reorder_suggestion on GET
    /api/inventory/risk (routers/inventory.py). Each test computes its
    expected numbers independently (not by re-deriving the same
    formula the endpoint uses) so these actually catch a wrong formula,
    not just confirm the code does what it does.
    """

    def test_daily_demand_rate_and_days_remaining(self, api_client, admin_headers):
        # 1440-minute (1-day) window, demand=10 -> daily_demand_rate=10.
        # Seeded stock is 250, but posting the order decrements it (see
        # routers/orders.py's decrement-on-write) to 240 before this
        # read happens -> days_remaining = 240 / 10 = 24.0 exactly.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=250)
        post_order(api_client, product_id="sku-001", region="North America", quantity=10)

        response = api_client.get(
            "/api/inventory/risk", params={"minutes": 1440}, headers=admin_headers
        )

        body = response.json()[0]
        assert body["daily_demand_rate"] == 10.0
        assert body["current_stock"] == 240
        assert body["days_of_stock_remaining"] == 24.0

    def test_days_remaining_is_none_when_no_recent_demand(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=300)

        response = api_client.get("/api/inventory/risk", headers=admin_headers)

        body = response.json()[0]
        assert body["daily_demand_rate"] == 0.0
        assert body["days_of_stock_remaining"] is None
        assert body["reorder_suggestion"] is None

    def test_reorder_suggestion_quantity_covers_lead_time_buffer(self, api_client, admin_headers):
        # stock=0, daily_demand_rate=12 (demand=12 over a 1-day window)
        # -> LEAD_TIME_BUFFER_DAYS=14 * 12 - 0 = 168, ceil'd (already
        # a whole number) -> suggested_quantity == 168.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=0)
        post_order(api_client, product_id="sku-001", region="North America", quantity=12)

        response = api_client.get(
            "/api/inventory/risk", params={"minutes": 1440}, headers=admin_headers
        )

        body = response.json()[0]
        assert body["reorder_suggestion"]["suggested_quantity"] == 168

    def test_reorder_suggestion_quantity_floored_at_zero_when_overstocked(self, api_client, admin_headers):
        # Plenty of stock relative to demand: projected 14-day demand
        # (5 * 14 = 70) is far below current_stock (5000) -> the naive
        # (projected - stock) would be deeply negative; must floor at 0.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=5000)
        post_order(api_client, product_id="sku-001", region="North America", quantity=5)

        response = api_client.get(
            "/api/inventory/risk", params={"minutes": 1440}, headers=admin_headers
        )

        body = response.json()[0]
        assert body["reorder_suggestion"]["suggested_quantity"] == 0

    def test_reorder_suggested_by_date_is_today_when_already_past_urgency_threshold(
        self, api_client, admin_headers
    ):
        # days_of_stock_remaining=0 (zero stock) is already below
        # REORDER_URGENCY_THRESHOLD_DAYS=7 -> "by when" can't be in the
        # past, so it must floor at today, not a negative offset.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=0)
        post_order(api_client, product_id="sku-001", region="North America", quantity=3)

        response = api_client.get(
            "/api/inventory/risk", params={"minutes": 1440}, headers=admin_headers
        )

        body = response.json()[0]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert body["reorder_suggestion"]["suggested_by_date"] == today

    def test_never_crashes_on_a_very_small_window(self, api_client, admin_headers):
        # Sanity check for the divide-by-zero guard: an extreme but
        # valid (minutes > 0) window shouldn't blow up the endpoint.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=10)
        post_order(api_client, product_id="sku-001", region="North America", quantity=1)

        response = api_client.get(
            "/api/inventory/risk", params={"minutes": 1}, headers=admin_headers
        )

        assert response.status_code == 200
        body = response.json()[0]
        assert body["daily_demand_rate"] > 0  # a huge rate, but a finite, sane number
        assert body["days_of_stock_remaining"] is not None

    def test_reorder_suggestion_omitted_from_low_risk_items_when_no_demand(
        self, api_client, admin_headers
    ):
        # LOW risk (healthy stock, no demand signal) genuinely has
        # nothing to suggest — confirms the API-level contract the
        # frontend's "skip for LOW risk" display rule depends on.
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=500)

        response = api_client.get("/api/inventory/risk", headers=admin_headers)

        body = response.json()[0]
        assert body["risk"] == "LOW"
        assert body["reorder_suggestion"] is None


class TestCsvEscaping:
    """csv_export.py builds every CSV via csv.DictWriter, which quotes/
    escapes RFC-4180-style by default (QUOTE_MINIMAL: a field gets
    wrapped in double quotes, with any literal double quote inside it
    doubled, whenever the field contains the delimiter, the quote
    character, or a newline). These tests exist to prove that's
    actually happening end-to-end through the real HTTP response, not
    just to assert csv.DictWriter behaves the way its own docs say it
    does — a naive `",".join(...)` implementation would corrupt every
    one of these rows into extra/misaligned columns, which is exactly
    what these tests would catch.
    """

    TRICKY_NAME = 'Nike, Air Max "Special" Edition\nSecond Line'

    def test_orders_csv_round_trips_a_name_with_comma_quote_and_newline(self, api_client, admin_headers):
        post_order(api_client, order_id="tricky-1", product_name=self.TRICKY_NAME)

        response = api_client.get("/api/orders", params={"format": "csv"}, headers=admin_headers)

        rows = list(csv.DictReader(io.StringIO(response.text)))
        matching = [r for r in rows if r["order_id"] == "tricky-1"]
        assert len(matching) == 1
        # The exact original string survives the round trip — not
        # split across extra columns, not missing its comma/quote/
        # newline, not merged with a neighboring field.
        assert matching[0]["product_name"] == self.TRICKY_NAME
        assert matching[0]["brand"] == "Nike"  # the field right after product_name in the header

    def test_products_csv_round_trips_the_same_tricky_name(self, api_client, admin_headers):
        post_order(api_client, product_id="sku-tricky", product_name=self.TRICKY_NAME)

        response = api_client.get(
            "/api/orders/products", params={"format": "csv"}, headers=admin_headers
        )

        rows = list(csv.DictReader(io.StringIO(response.text)))
        matching = [r for r in rows if r["product_id"] == "sku-tricky"]
        assert len(matching) == 1
        assert matching[0]["product_name"] == self.TRICKY_NAME

    def test_inventory_risk_csv_round_trips_the_same_tricky_name(self, api_client, admin_headers):
        seed_inventory(api_client, product_id="sku-tricky-risk", current_stock=10)
        api_client.post(
            "/api/inventory/seed",
            json={
                "product_id": "sku-tricky-risk",
                "product_name": self.TRICKY_NAME,
                "category": DEFAULT_ORDER["category"],
                "brand": "Nike",
                "region": "North America",
                "current_stock": 10,
            },
        )

        response = api_client.get(
            "/api/inventory/risk", params={"format": "csv"}, headers=admin_headers
        )

        rows = list(csv.DictReader(io.StringIO(response.text)))
        matching = [r for r in rows if r["product_id"] == "sku-tricky-risk"]
        assert len(matching) == 1
        assert matching[0]["product_name"] == self.TRICKY_NAME

    def test_csv_row_count_matches_json_row_count_for_tricky_data(self, api_client, admin_headers):
        # The clearest possible symptom of broken escaping: a comma or
        # newline inside a field silently creates extra CSV rows/columns.
        # Posting several tricky orders and comparing row counts against
        # the equivalent JSON response catches that directly.
        for i in range(3):
            post_order(api_client, order_id=f"tricky-count-{i}", product_name=f'{self.TRICKY_NAME} #{i}')

        json_response = api_client.get("/api/orders", params={"limit": 100}, headers=admin_headers)
        csv_response = api_client.get(
            "/api/orders", params={"limit": 100, "format": "csv"}, headers=admin_headers
        )

        json_count = len(json_response.json())
        csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        assert len(csv_rows) == json_count


class TestBrands:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/brands").status_code == 401

    def test_admin_sees_distinct_brands_from_orders(self, api_client, admin_headers):
        post_order(api_client, brand="Nike")
        post_order(api_client, brand="Adidas")
        post_order(api_client, brand="Nike")

        response = api_client.get("/api/brands", headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == ["Adidas", "Nike"]

    def test_business_account_sees_only_its_own_owned_brands(self, api_client, business_headers_factory):
        # Deliberate design choice (see routers/orders.py's get_brands
        # docstring): a business account never learns the full universe
        # of brands that exist in the system, even though "Adidas" has
        # real orders sitting right there in the same database — its
        # brand list is exactly its own owned_brands, sourced from the
        # JWT-backed user profile rather than a DB scan. Consistent
        # with every other scoped endpoint: a business account's view
        # of the world stops at its own brand(s).
        post_order(api_client, brand="Nike")
        post_order(api_client, brand="Adidas")
        nike_headers = business_headers_factory("nike-brands@test.example.com", ["Nike"])

        response = api_client.get("/api/brands", headers=nike_headers)

        assert response.json() == ["Nike"]

    def test_multi_brand_business_account_sees_exactly_its_own_set_not_more_not_less(
        self, api_client, business_headers_factory
    ):
        post_order(api_client, brand="Nike")
        post_order(api_client, brand="Adidas")
        post_order(api_client, brand="Puma")  # a third brand this account doesn't own
        multi_headers = business_headers_factory("multi-brands@test.example.com", ["Adidas", "Nike"])

        response = api_client.get("/api/brands", headers=multi_headers)

        assert response.json() == ["Adidas", "Nike"]  # sorted, both owned brands, no Puma


class TestBrandsBenchmark:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/brands/benchmark").status_code == 401

    def test_business_account_gets_403_not_scoped_data(
        self, api_client, admin_headers, business_headers_factory
    ):
        post_order(api_client, brand="Nike")
        nike_headers = business_headers_factory("nike-benchmark@test.example.com", ["Nike"])

        response = api_client.get("/api/brands/benchmark", headers=nike_headers)

        # Not brand-scoped down to just Nike's own figures — outright
        # denied. A business account should never see even its own row
        # of a cross-brand comparison, since that implies competitors
        # exist and roughly how they're performing (see
        # get_brands_benchmark's docstring).
        assert response.status_code == 403

    def test_average_revenue_is_the_plain_mean_of_each_brands_revenue(self, api_client, admin_headers):
        post_order(api_client, brand="Nike", quantity=1, unit_price=100.0)
        post_order(api_client, brand="Adidas", quantity=1, unit_price=200.0)

        response = api_client.get("/api/brands/benchmark", params={"range": "30d"}, headers=admin_headers)

        body = response.json()
        assert body["average_revenue"] == 150.0  # (100 + 200) / 2, not weighted by order count

    def test_revenue_vs_average_pct_computed_correctly(self, api_client, admin_headers):
        post_order(api_client, brand="Nike", quantity=1, unit_price=100.0)
        post_order(api_client, brand="Adidas", quantity=1, unit_price=300.0)
        # average = (100 + 300) / 2 = 200
        # Nike: (100 - 200) / 200 * 100 = -50%
        # Adidas: (300 - 200) / 200 * 100 = +50%

        response = api_client.get("/api/brands/benchmark", params={"range": "30d"}, headers=admin_headers)

        by_brand = {b["brand"]: b for b in response.json()["benchmarks"]}
        assert by_brand["Nike"]["revenue_vs_average_pct"] == -50.0
        assert by_brand["Adidas"]["revenue_vs_average_pct"] == 50.0

    def test_empty_window_returns_empty_benchmarks_not_an_error(self, api_client, admin_headers):
        response = api_client.get("/api/brands/benchmark", params={"range": "7d"}, headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["benchmarks"] == []
        assert body["average_revenue"] == 0.0

    def test_invalid_range_returns_422(self, api_client, admin_headers):
        response = api_client.get("/api/brands/benchmark", params={"range": "3d"}, headers=admin_headers)

        assert response.status_code == 422

    def test_sorted_by_revenue_descending(self, api_client, admin_headers):
        post_order(api_client, brand="Small", quantity=1, unit_price=10.0)
        post_order(api_client, brand="Big", quantity=1, unit_price=1000.0)

        response = api_client.get("/api/brands/benchmark", params={"range": "30d"}, headers=admin_headers)

        brands_in_order = [b["brand"] for b in response.json()["benchmarks"]]
        assert brands_in_order[0] == "Big"


class TestBusinessAnomalies:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/anomalies/business").status_code == 401

    def test_extreme_order_volume_is_flagged_after_a_real_baseline(self, api_client, admin_headers):
        # Build 10 hourly buckets of believable, slightly-varying order
        # volume for one region — the region-hour detector's cold-start
        # threshold (MIN_WINDOW_SIZE=10, see detectors/zscore.py).
        counts_per_hour = [3, 4, 3, 4, 3, 4, 3, 4, 3, 4]
        for hours_ago, count in zip(range(10, 0, -1), counts_per_hour):
            bucket_time = REFERENCE_HOUR - timedelta(hours=hours_ago)
            for _ in range(count):
                post_order(api_client, timestamp=bucket_time, region="Anomaly Test Region")

        # Now flood the current hour far past that baseline.
        last_response = None
        for _ in range(20):
            last_response = post_order(
                api_client, timestamp=REFERENCE_HOUR, region="Anomaly Test Region"
            )

        assert last_response.status_code == 201
        assert last_response.json()["anomaly"] is True

        anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()
        assert len(anomalies) >= 1
        assert all(event["region"] == "Anomaly Test Region" for event in anomalies)
        assert all(abs(event["z_score"]) > 3 for event in anomalies)
        assert all(event["severity"] in ("mild", "moderate", "severe") for event in anomalies)
        assert all(event["brand"] == "Nike" for event in anomalies)

    def test_steady_order_volume_never_flagged(self, api_client, admin_headers):
        for hours_ago in range(11, -1, -1):  # 11 prior hours + the current one
            bucket_time = REFERENCE_HOUR - timedelta(hours=hours_ago)
            for _ in range(3):
                post_order(api_client, timestamp=bucket_time, region="Steady Region")

        response = api_client.get("/api/anomalies/business", headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == []

    def test_empty_database_returns_empty_list(self, api_client, admin_headers):
        response = api_client.get("/api/anomalies/business", headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == []


class TestHealth:
    def test_health_check_reports_ok_against_the_test_database(self, api_client):
        response = api_client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
