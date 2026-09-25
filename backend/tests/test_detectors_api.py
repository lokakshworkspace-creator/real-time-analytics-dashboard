"""Integration tests for the batch detectors end to end: POST
/api/detectors/run-batch (auth, stamping), the combined-verdict shape of
GET /api/anomalies/business, and how detector agreement feeds
GET /api/alerts. Scenarios are built relative to the real clock because
the batch always runs "now"; they go through the real POST /api/orders,
so the z-score verdict comes from the real ingest path too.
"""

import uuid
from datetime import datetime, timedelta, timezone


def current_hour() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def post_order(client, *, timestamp, region, brand="Nike", quantity=1, unit_price=50.0):
    return client.post(
        "/api/orders",
        json={
            "order_id": str(uuid.uuid4()),
            "timestamp": timestamp.isoformat(),
            "product_id": "sku-001",
            "product_name": "Nike Running Shoes",
            "category": "Apparel",
            "brand": brand,
            "quantity": quantity,
            "unit_price": unit_price,
            "region": region,
        },
    )


def seed_flood_scenario(client, *, region="Flood Region", brand="Nike"):
    """12 hours of 2-3 orders/hour, then a 20-order flood in the current
    hour: an unmistakable spike for all three detectors."""
    hour = current_hour()
    for hours_back, count in zip(range(12, 0, -1), [2, 3] * 6):
        for i in range(count):
            post_order(
                client, timestamp=hour - timedelta(hours=hours_back) + timedelta(minutes=5 + i),
                region=region, brand=brand,
            )
    for i in range(20):
        post_order(client, timestamp=hour + timedelta(seconds=i), region=region, brand=brand, quantity=4)


class TestRunBatchAuth:
    def test_requires_auth(self, api_client):
        assert api_client.post("/api/detectors/run-batch").status_code == 401

    def test_business_account_is_forbidden(self, api_client, business_headers_factory):
        headers = business_headers_factory("nike-batch@test.example.com", ["Nike"])

        response = api_client.post("/api/detectors/run-batch", headers=headers)

        assert response.status_code == 403

    def test_rejects_out_of_range_params(self, api_client, admin_headers):
        for params in ({"window_days": 0}, {"window_days": 31}, {"recent_hours": 0}):
            response = api_client.post("/api/detectors/run-batch", params=params, headers=admin_headers)
            assert response.status_code == 422


class TestRunBatch:
    def test_empty_database_runs_cleanly(self, api_client, admin_headers):
        response = api_client.post("/api/detectors/run-batch", headers=admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["buckets_in_window"] == 0
        assert body["orders_stamped"] == 0
        assert "skipped" in body["note"]

    def test_scores_the_window_and_flags_only_the_flood(self, api_client, admin_headers):
        seed_flood_scenario(api_client)

        response = api_client.post("/api/detectors/run-batch", headers=admin_headers)

        body = response.json()
        assert body["buckets_in_window"] == 13
        assert body["isolation_forest_buckets_flagged"] == 1
        assert body["forecast_buckets_flagged"] == 1
        # Scored but not flagged: the steady baseline hours got verdicts too.
        assert body["isolation_forest_buckets_scored"] == 13
        assert body["forecast_buckets_scored"] >= 2
        assert body["orders_stamped"] == 30 + 20
        assert body["note"] is None

    def test_rerunning_is_idempotent(self, api_client, admin_headers):
        seed_flood_scenario(api_client)

        first = api_client.post("/api/detectors/run-batch", headers=admin_headers).json()
        second = api_client.post("/api/detectors/run-batch", headers=admin_headers).json()

        assert first == second
        anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()
        assert len(anomalies) == len({a["id"] for a in anomalies})


class TestCombinedAnomalyRecord:
    def test_before_any_batch_run_batch_verdicts_are_null(self, api_client, admin_headers):
        seed_flood_scenario(api_client)

        anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()

        assert anomalies, "the z-score flags the flood at ingest"
        for event in anomalies:
            assert event["z_score"]["flagged"] is True
            assert event["isolation_forest"] is None
            assert event["forecast"] is None
            assert event["detector_agreement"] == 1

    def test_after_a_batch_run_all_three_verdicts_appear_together(self, api_client, admin_headers):
        seed_flood_scenario(api_client)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()

        full = [a for a in anomalies if a["detector_agreement"] == 3]
        assert full, "later flood orders are flagged by all three detectors"
        event = full[0]
        assert event["z_score"]["flagged"] is True
        assert event["z_score"]["severity"] in ("mild", "moderate", "severe")
        assert event["isolation_forest"]["flagged"] is True
        assert 0.5 < event["isolation_forest"]["score"] <= 1.0
        assert event["forecast"]["flagged"] is True
        assert event["forecast"]["actual"] == 20.0
        assert event["forecast"]["expected"] < 5.0

    def test_batch_flags_bring_in_orders_the_zscore_did_not_flag(self, api_client, admin_headers):
        seed_flood_scenario(api_client)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()

        # Early flood orders arrive before the hour's count is unusual, so
        # the z-score passes them — but the bucket-level verdicts stamped
        # on them still list them, with agreement 2.
        batch_only = [a for a in anomalies if not a["z_score"]["flagged"]]
        assert batch_only
        assert all(a["detector_agreement"] == 2 for a in batch_only)
        assert all(a["z_score"]["severity"] is None for a in batch_only)

    def test_steady_traffic_is_never_listed_even_after_a_batch_run(self, api_client, admin_headers):
        hour = current_hour()
        for hours_back in range(12, -1, -1):
            for i in range(3):
                post_order(
                    api_client, timestamp=hour - timedelta(hours=hours_back) + timedelta(minutes=5 + i),
                    region="Steady Region",
                )

        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        assert api_client.get("/api/anomalies/business", headers=admin_headers).json() == []

    def test_business_account_sees_only_its_own_brands_verdicts(
        self, api_client, admin_headers, business_headers_factory
    ):
        seed_flood_scenario(api_client, region="Nike Flood", brand="Nike")
        seed_flood_scenario(api_client, region="Adidas Flood", brand="Adidas")
        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        nike_headers = business_headers_factory("nike-verdicts@test.example.com", ["Nike"])
        anomalies = api_client.get("/api/anomalies/business", headers=nike_headers).json()

        assert anomalies
        assert {a["brand"] for a in anomalies} == {"Nike"}
        assert all(a["isolation_forest"] is not None for a in anomalies)


class TestAlertsUseDetectorAgreement:
    def test_alerts_carry_detectors_and_agreement_and_escalate_severity(self, api_client, admin_headers):
        seed_flood_scenario(api_client)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        alerts = [
            a for a in api_client.get("/api/alerts", headers=admin_headers).json() if a["type"] == "anomaly"
        ]

        assert alerts
        by_agreement = {a["detector_agreement"] for a in alerts}
        assert by_agreement == {2, 3}
        for a in alerts:
            assert len(a["detectors"]) == a["detector_agreement"]
            assert a["related_entity"]["anomaly_id"]
            if a["detector_agreement"] == 3:
                assert a["detectors"] == ["z_score", "isolation_forest", "forecast"]
                assert a["severity"] == "severe"
            else:
                assert "z_score" not in a["detectors"]
                assert a["severity"] == "moderate"  # mild base + one extra detector

    def test_more_detectors_agreeing_ranks_first(self, api_client, admin_headers):
        seed_flood_scenario(api_client)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        alerts = [
            a for a in api_client.get("/api/alerts", headers=admin_headers).json() if a["type"] == "anomaly"
        ]

        agreements = [a["detector_agreement"] for a in alerts]
        assert agreements == sorted(agreements, reverse=True)

    def test_a_zscore_only_alert_has_a_single_detector(self, api_client, admin_headers):
        seed_flood_scenario(api_client)  # no batch run

        alerts = [
            a for a in api_client.get("/api/alerts", headers=admin_headers).json() if a["type"] == "anomaly"
        ]

        assert alerts
        assert all(a["detectors"] == ["z_score"] and a["detector_agreement"] == 1 for a in alerts)
