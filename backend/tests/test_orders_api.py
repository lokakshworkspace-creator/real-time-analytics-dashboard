"""Integration tests against the real FastAPI app, via TestClient, but
wired to a dedicated test database (see conftest.py's `api_client`
fixture) — never the real `analytics` database.

Covers: POST /api/orders validation + computed fields + inventory
decrement, GET /api/orders pagination, the aggregation endpoints
(kpis/regions/products/inventory/risk), and the region-hour order-volume
anomaly detector end-to-end via GET /api/anomalies/business.
"""

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
    "product_name": "Wireless Earbuds",
    "category": "Electronics",
    "quantity": 1,
    "unit_price": 50.0,
    "region": "North America",
}


def post_order(client, *, timestamp: datetime | None = None, **overrides):
    payload = {"order_id": str(uuid.uuid4()), **DEFAULT_ORDER, **overrides}
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return client.post("/api/orders", json=payload)


def seed_inventory(client, *, product_id="sku-001", region="North America", current_stock=300):
    return client.post(
        "/api/inventory/seed",
        json={
            "product_id": product_id,
            "product_name": DEFAULT_ORDER["product_name"],
            "category": DEFAULT_ORDER["category"],
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

    def test_empty_region_returns_422(self, api_client):
        assert post_order(api_client, region="").status_code == 422

    def test_explicit_payment_status_is_honored(self, api_client):
        response = post_order(api_client, payment_status="failed")

        assert response.status_code == 201
        assert response.json()["payment_status"] == "failed"

    def test_invalid_payment_status_returns_422(self, api_client):
        assert post_order(api_client, payment_status="pending").status_code == 422


class TestInventoryDecrement:
    def test_posting_an_order_decrements_matching_inventory(self, api_client):
        seed_inventory(api_client, current_stock=300)
        post_order(api_client, quantity=5)

        inventory = api_client.get("/api/inventory").json()
        assert len(inventory) == 1
        assert inventory[0]["current_stock"] == 295

    def test_order_without_matching_inventory_still_succeeds(self, api_client):
        # No seed call first — the order should still be recorded (see
        # routers/orders.py's warning-and-continue behavior).
        response = post_order(api_client, product_id="sku-999", region="Unseeded Region")

        assert response.status_code == 201

    def test_order_exceeding_stock_is_accepted_and_stock_clamps_at_zero(self, api_client):
        # A stockout doesn't block the sale (a deliberate scope
        # decision — see routers/orders.py) and current_stock must
        # never go negative, unlike a plain $inc.
        seed_inventory(api_client, current_stock=3)
        response = post_order(api_client, quantity=10)

        assert response.status_code == 201

        inventory = api_client.get("/api/inventory").json()
        assert inventory[0]["current_stock"] == 0

    def test_further_orders_after_stockout_keep_stock_at_zero(self, api_client):
        seed_inventory(api_client, current_stock=2)
        post_order(api_client, quantity=5)  # already clamps to 0
        post_order(api_client, quantity=1)  # would go to -1 with a plain $inc

        inventory = api_client.get("/api/inventory").json()
        assert inventory[0]["current_stock"] == 0


class TestGetOrders:
    def test_returns_newest_first(self, api_client):
        post_order(api_client, timestamp=REFERENCE_HOUR, order_id="first")
        post_order(api_client, timestamp=REFERENCE_HOUR + timedelta(minutes=5), order_id="second")

        response = api_client.get("/api/orders")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["order_id"] == "second"
        assert body[1]["order_id"] == "first"

    def test_limit_and_skip_paginate(self, api_client):
        for i in range(5):
            post_order(api_client, timestamp=REFERENCE_HOUR + timedelta(minutes=i), order_id=str(i))

        response = api_client.get("/api/orders", params={"limit": 2, "skip": 1})

        assert [o["order_id"] for o in response.json()] == ["3", "2"]

    def test_empty_database_returns_empty_list(self, api_client):
        response = api_client.get("/api/orders")

        assert response.status_code == 200
        assert response.json() == []


class TestKpis:
    def test_empty_window_returns_zeros_not_404(self, api_client):
        response = api_client.get("/api/orders/kpis")

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "minutes": 60,
            "total_orders": 0,
            "revenue": 0.0,
            "units_sold": 0,
            "avg_order_value": 0.0,
        }

    def test_kpis_aggregate_posted_orders(self, api_client):
        post_order(api_client, quantity=2, unit_price=10.0)  # total_value 20
        post_order(api_client, quantity=1, unit_price=30.0)  # total_value 30

        response = api_client.get("/api/orders/kpis")

        assert response.status_code == 200
        body = response.json()
        assert body["total_orders"] == 2
        assert body["revenue"] == 50.0
        assert body["units_sold"] == 3
        assert body["avg_order_value"] == 25.0


class TestRegionStats:
    def test_grouped_by_region_sorted_by_revenue_desc(self, api_client):
        post_order(api_client, region="Europe", quantity=1, unit_price=10.0)
        post_order(api_client, region="Asia Pacific", quantity=1, unit_price=100.0)

        response = api_client.get("/api/orders/regions")

        assert response.status_code == 200
        body = response.json()
        assert body[0]["region"] == "Asia Pacific"
        assert body[0]["revenue"] == 100.0
        assert body[1]["region"] == "Europe"


class TestProductStats:
    def test_top_order_sorts_revenue_descending(self, api_client):
        post_order(api_client, product_id="sku-a", product_name="A", quantity=1, unit_price=5.0)
        post_order(api_client, product_id="sku-b", product_name="B", quantity=1, unit_price=500.0)

        response = api_client.get("/api/orders/products", params={"order": "top"})

        body = response.json()
        assert body[0]["product_id"] == "sku-b"

    def test_bottom_order_sorts_revenue_ascending(self, api_client):
        post_order(api_client, product_id="sku-a", product_name="A", quantity=1, unit_price=5.0)
        post_order(api_client, product_id="sku-b", product_name="B", quantity=1, unit_price=500.0)

        response = api_client.get("/api/orders/products", params={"order": "bottom"})

        body = response.json()
        assert body[0]["product_id"] == "sku-a"

    def test_invalid_order_param_returns_422(self, api_client):
        response = api_client.get("/api/orders/products", params={"order": "sideways"})

        assert response.status_code == 422


class TestInventorySeedAndList:
    def test_seed_then_list(self, api_client):
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=150)

        response = api_client.get("/api/inventory")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["current_stock"] == 150

    def test_reseeding_the_same_product_region_upserts_not_duplicates(self, api_client):
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=150)
        seed_inventory(api_client, product_id="sku-001", region="Europe", current_stock=200)

        body = api_client.get("/api/inventory").json()
        assert len(body) == 1
        assert body[0]["current_stock"] == 200


class TestInventoryRisk:
    def test_high_risk_when_stock_low_and_demand_meets_or_exceeds_it(self, api_client):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=5)
        post_order(api_client, product_id="sku-001", region="North America", quantity=10)

        response = api_client.get("/api/inventory/risk")

        body = response.json()
        assert len(body) == 1
        assert body[0]["risk"] == "HIGH"
        assert body[0]["recent_demand"] == 10

    def test_medium_risk_when_stock_low_but_demand_hasnt_caught_up(self, api_client):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=15)
        post_order(api_client, product_id="sku-001", region="North America", quantity=1)

        response = api_client.get("/api/inventory/risk", params={"low_stock_threshold": 20})

        body = response.json()
        assert body[0]["risk"] == "MEDIUM"

    def test_low_risk_when_stock_is_healthy(self, api_client):
        seed_inventory(api_client, product_id="sku-001", region="North America", current_stock=300)

        response = api_client.get("/api/inventory/risk")

        body = response.json()
        assert body[0]["risk"] == "LOW"
        assert body[0]["recent_demand"] == 0


class TestBusinessAnomalies:
    def test_extreme_order_volume_is_flagged_after_a_real_baseline(self, api_client):
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

        anomalies = api_client.get("/api/anomalies/business").json()
        assert len(anomalies) >= 1
        assert all(event["region"] == "Anomaly Test Region" for event in anomalies)
        assert all(abs(event["z_score"]) > 3 for event in anomalies)

    def test_steady_order_volume_never_flagged(self, api_client):
        for hours_ago in range(11, -1, -1):  # 11 prior hours + the current one
            bucket_time = REFERENCE_HOUR - timedelta(hours=hours_ago)
            for _ in range(3):
                post_order(api_client, timestamp=bucket_time, region="Steady Region")

        response = api_client.get("/api/anomalies/business")

        assert response.status_code == 200
        assert response.json() == []

    def test_empty_database_returns_empty_list(self, api_client):
        response = api_client.get("/api/anomalies/business")

        assert response.status_code == 200
        assert response.json() == []


class TestHealth:
    def test_health_check_reports_ok_against_the_test_database(self, api_client):
        response = api_client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
