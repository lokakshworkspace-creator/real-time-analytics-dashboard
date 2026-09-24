"""Integration tests for GET /api/alerts (routers/alerts.py) — the
consolidated feed over business anomalies, HIGH-risk inventory, and
declining regions/products. Each source is tested both for "does the
right alert type appear with the right content" and, together, for
brand-scoping and severity-first sort order.
"""

import uuid
from datetime import datetime, timedelta, timezone


def post_order(client, *, timestamp: datetime | None = None, **overrides):
    payload = {
        "order_id": str(uuid.uuid4()),
        "product_id": "sku-001",
        "product_name": "Nike Running Shoes",
        "category": "Apparel",
        "brand": "Nike",
        "quantity": 1,
        "unit_price": 50.0,
        "region": "North America",
        **overrides,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return client.post("/api/orders", json=payload)


def seed_inventory(client, **overrides):
    payload = {
        "product_id": "sku-001",
        "product_name": "Nike Running Shoes",
        "category": "Apparel",
        "brand": "Nike",
        "region": "North America",
        "current_stock": 300,
        **overrides,
    }
    return client.post("/api/inventory/seed", json=payload)


class TestAlertsBasics:
    def test_requires_auth(self, api_client):
        assert api_client.get("/api/alerts").status_code == 401

    def test_empty_when_nothing_triggers(self, api_client, admin_headers):
        seed_inventory(api_client, current_stock=300)  # healthy stock, no orders

        response = api_client.get("/api/alerts", headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == []


class TestAnomalyAlerts:
    def test_a_real_flagged_anomaly_produces_an_anomaly_alert(self, api_client, admin_headers):
        reference_hour = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        for hours_ago, count in zip(range(10, 0, -1), [3, 4, 3, 4, 3, 4, 3, 4, 3, 4]):
            bucket_time = reference_hour - timedelta(hours=hours_ago)
            for _ in range(count):
                post_order(api_client, region="Alert Anomaly Region", timestamp=bucket_time)
        for _ in range(20):
            post_order(api_client, region="Alert Anomaly Region", timestamp=reference_hour)

        response = api_client.get("/api/alerts", headers=admin_headers)

        anomaly_alerts = [a for a in response.json() if a["type"] == "anomaly"]
        assert len(anomaly_alerts) >= 1
        alert = anomaly_alerts[0]
        assert alert["severity"] in ("mild", "moderate", "severe")
        assert "Alert Anomaly Region" in alert["message"]
        assert alert["related_entity"]["region"] == "Alert Anomaly Region"
        assert alert["related_entity"]["brand"] == "Nike"


class TestLowStockAlerts:
    def test_high_risk_inventory_produces_a_low_stock_alert(self, api_client, admin_headers):
        seed_inventory(api_client, current_stock=5)
        post_order(api_client, quantity=10)  # demand >= stock -> HIGH risk

        response = api_client.get("/api/alerts", headers=admin_headers)

        low_stock_alerts = [a for a in response.json() if a["type"] == "low_stock"]
        assert len(low_stock_alerts) == 1
        assert low_stock_alerts[0]["severity"] == "severe"
        assert low_stock_alerts[0]["related_entity"]["product_id"] == "sku-001"

    def test_medium_and_low_risk_inventory_never_produce_alerts(self, api_client, admin_headers):
        # MEDIUM: low stock but demand hasn't caught up.
        seed_inventory(api_client, product_id="sku-medium", current_stock=15)
        post_order(api_client, product_id="sku-medium", quantity=1)
        # LOW: healthy stock.
        seed_inventory(api_client, product_id="sku-low-risk", current_stock=500)

        response = api_client.get("/api/alerts", headers=admin_headers)

        assert [a for a in response.json() if a["type"] == "low_stock"] == []


class TestDeclineAlerts:
    def test_declining_region_produces_a_decline_alert(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        # Previous 7d: 10 orders. Current 7d: 1 order -> -90%.
        for _ in range(10):
            post_order(api_client, region="Decline Region", timestamp=now - timedelta(days=10))
        post_order(api_client, region="Decline Region", timestamp=now - timedelta(days=1))

        response = api_client.get("/api/alerts", headers=admin_headers)

        decline_alerts = [
            a for a in response.json()
            if a["type"] == "decline" and a["related_entity"].get("region") == "Decline Region"
        ]
        assert len(decline_alerts) == 1
        assert decline_alerts[0]["severity"] == "severe"  # -90% is well past the severe threshold
        assert "90.0%" in decline_alerts[0]["message"]

    def test_declining_product_produces_a_decline_alert(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        for _ in range(10):
            post_order(api_client, product_id="sku-decline", timestamp=now - timedelta(days=10))
        post_order(api_client, product_id="sku-decline", timestamp=now - timedelta(days=1))

        response = api_client.get("/api/alerts", headers=admin_headers)

        decline_alerts = [
            a for a in response.json()
            if a["type"] == "decline" and a["related_entity"].get("product_id") == "sku-decline"
        ]
        assert len(decline_alerts) == 1
        assert decline_alerts[0]["severity"] == "severe"

    def test_a_mild_dip_under_the_threshold_produces_no_alert(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        # A -10% dip is inside normal noise — below DECLINE_ALERT_THRESHOLD_PCT (-20%).
        for _ in range(10):
            post_order(api_client, region="Stable Region", timestamp=now - timedelta(days=10))
        for _ in range(9):
            post_order(api_client, region="Stable Region", timestamp=now - timedelta(days=1))

        response = api_client.get("/api/alerts", headers=admin_headers)

        assert [
            a for a in response.json()
            if a["type"] == "decline" and a["related_entity"].get("region") == "Stable Region"
        ] == []

    def test_growth_never_produces_a_decline_alert(self, api_client, admin_headers):
        now = datetime.now(timezone.utc)
        post_order(api_client, region="Growing Region", timestamp=now - timedelta(days=10))
        for _ in range(10):
            post_order(api_client, region="Growing Region", timestamp=now - timedelta(days=1))

        response = api_client.get("/api/alerts", headers=admin_headers)

        assert [
            a for a in response.json()
            if a["type"] == "decline" and a["related_entity"].get("region") == "Growing Region"
        ] == []


class TestAlertsSortOrder:
    def test_severe_alerts_sort_before_moderate_and_mild(self, api_client, admin_headers):
        # A severe low_stock alert.
        seed_inventory(api_client, product_id="sku-severe", current_stock=5)
        post_order(api_client, product_id="sku-severe", quantity=10)

        # A mild decline alert (just past the -20% threshold).
        now = datetime.now(timezone.utc)
        for _ in range(10):
            post_order(api_client, region="Mild Decline Region", timestamp=now - timedelta(days=10))
        for _ in range(7):  # -30% is still "mild" (below -20 but above -35 moderate threshold)
            post_order(api_client, region="Mild Decline Region", timestamp=now - timedelta(days=1))

        response = api_client.get("/api/alerts", headers=admin_headers)
        severities = [a["severity"] for a in response.json()]

        # Every "severe" entry appears before every "mild" entry.
        first_mild_index = severities.index("mild") if "mild" in severities else len(severities)
        last_severe_index = max(
            (i for i, s in enumerate(severities) if s == "severe"), default=-1
        )
        assert last_severe_index < first_mild_index


class TestAlertsBrandScoping:
    def test_business_account_never_sees_another_brands_alerts(
        self, api_client, admin_headers, business_headers_factory
    ):
        # A HIGH-risk item for Nike.
        seed_inventory(api_client, product_id="sku-nike-low", brand="Nike", current_stock=5)
        post_order(api_client, product_id="sku-nike-low", brand="Nike", quantity=10)

        # A HIGH-risk item for Adidas.
        seed_inventory(api_client, product_id="sku-adidas-low", brand="Adidas", region="Europe", current_stock=5)
        post_order(api_client, product_id="sku-adidas-low", brand="Adidas", region="Europe", quantity=10)

        nike_headers = business_headers_factory("nike-alerts@test.example.com", ["Nike"])
        response = api_client.get("/api/alerts", headers=nike_headers)

        brands_seen = {a["related_entity"].get("brand") for a in response.json() if a["type"] == "low_stock"}
        assert brands_seen == {"Nike"}
