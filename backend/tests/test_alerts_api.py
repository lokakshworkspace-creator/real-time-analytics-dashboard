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


class TestAlertsBrandFilter:
    """The admin's `?brand=` narrows EVERY alert source (anomalies, low
    stock, regional and product declines), not just some of them; a
    business account's `?brand=` is ignored — it stays on its own brands.
    """

    def _seed_two_brands(self, client):
        now = datetime.now(timezone.utc)
        reference_hour = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        for brand, region in (("Nike", "Nike Region"), ("Adidas", "Adidas Region")):
            # 1) HIGH-risk stock
            seed_inventory(client, product_id=f"sku-{brand}-low", brand=brand, region=region, current_stock=5)
            post_order(client, product_id=f"sku-{brand}-low", brand=brand, region=region, quantity=10)
            # 2) a declining product (-80% vs the previous 7d)
            for _ in range(10):
                post_order(client, product_id=f"sku-{brand}-decl", product_name=f"{brand} Decliner",
                           brand=brand, region=f"{region} D", timestamp=now - timedelta(days=10))
            for _ in range(2):
                post_order(client, product_id=f"sku-{brand}-decl", product_name=f"{brand} Decliner",
                           brand=brand, region=f"{region} D", timestamp=now - timedelta(days=1))
            # 3) a z-score anomaly (10 quiet hours, then a flood)
            for hours_ago, count in zip(range(10, 0, -1), [3, 4] * 5):
                for _ in range(count):
                    post_order(client, brand=brand, region=f"{region} A",
                               timestamp=reference_hour - timedelta(hours=hours_ago))
            for _ in range(20):
                post_order(client, brand=brand, region=f"{region} A", timestamp=reference_hour)

    @staticmethod
    def _brands_by_type(alerts):
        """{alert type: set of brands it mentions}, read from each type's own identifying fields."""
        seen = {"low_stock": set(), "anomaly": set(), "decline": set()}
        for a in alerts:
            entity = a["related_entity"]
            if a["type"] == "decline":
                pid = entity.get("product_id")
                if pid:
                    seen["decline"].add(pid.split("-")[1])  # sku-<Brand>-decl
                else:
                    seen["decline"].add(entity["region"].split()[0])  # "<Brand> Region D"
            else:
                seen[a["type"]].add(entity["brand"])
        return seen

    def test_admin_with_no_filter_sees_every_brand_in_every_source(self, api_client, admin_headers):
        self._seed_two_brands(api_client)

        seen = self._brands_by_type(api_client.get("/api/alerts", headers=admin_headers).json())

        assert seen["low_stock"] == {"Nike", "Adidas"}
        assert seen["anomaly"] == {"Nike", "Adidas"}
        assert seen["decline"] == {"Nike", "Adidas"}

    def test_admin_brand_filter_narrows_all_four_sources(self, api_client, admin_headers):
        self._seed_two_brands(api_client)

        response = api_client.get("/api/alerts", params={"brand": "Nike"}, headers=admin_headers)

        assert response.status_code == 200
        seen = self._brands_by_type(response.json())
        assert seen["low_stock"] == {"Nike"}
        assert seen["anomaly"] == {"Nike"}
        assert seen["decline"] == {"Nike"}  # product AND regional declines

    def test_admin_filter_for_a_brand_with_no_data_is_empty(self, api_client, admin_headers):
        self._seed_two_brands(api_client)

        response = api_client.get("/api/alerts", params={"brand": "Puma"}, headers=admin_headers)

        assert response.status_code == 200
        assert response.json() == []

    def test_business_account_cannot_widen_its_scope_with_brand(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brands(api_client)
        nike_headers = business_headers_factory("nike-alert-param@test.example.com", ["Nike"])

        without = api_client.get("/api/alerts", headers=nike_headers).json()
        other_brand = api_client.get("/api/alerts", params={"brand": "Adidas"}, headers=nike_headers).json()

        # The param changed nothing. Compared without `timestamp`: low-stock
        # and decline alerts are stamped "now" per request, so two calls
        # can never match to the microsecond (that, not scope, was the only
        # difference between the two responses).
        strip = lambda alerts: [{k: v for k, v in a.items() if k != "timestamp"} for a in alerts]
        assert strip(other_brand) == strip(without)
        seen = self._brands_by_type(other_brand)
        assert seen["low_stock"] == {"Nike"}
        assert seen["anomaly"] == {"Nike"}
        assert seen["decline"] == {"Nike"}
