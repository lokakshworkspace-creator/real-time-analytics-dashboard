"""Integration tests against the real FastAPI app, via TestClient, but
wired to a dedicated test database (see conftest.py's `api_client`
fixture) — never the real `analytics` database.

Covers: POST /api/metrics validation (valid + invalid payloads),
GET /api/metrics/latest, GET /api/metrics/anomalies, plus a couple of
Phase 8-specific regression tests for the /api prefix change and the
new hardening (non-finite floats, oversized `minutes`).
"""

DEFAULT_PAYLOAD = {"metric": "cpu_usage", "value": 50.0, "source": "test-source"}


def post_metric(client, **overrides):
    payload = {**DEFAULT_PAYLOAD, **overrides}
    return client.post("/api/metrics", json=payload)


class TestPostMetricsValidation:
    def test_valid_payload_returns_201_with_the_stored_document(self, api_client):
        response = post_metric(api_client, metric="cpu_usage", value=55.5, source="server-1")

        assert response.status_code == 201
        body = response.json()
        assert body["metric"] == "cpu_usage"
        assert body["value"] == 55.5
        assert body["source"] == "server-1"
        assert body["anomaly"] is False  # cold start — no prior window yet
        assert body["timestamp"].endswith("Z")
        assert "id" in body and body["id"]

    def test_unknown_metric_name_returns_422(self, api_client):
        response = post_metric(api_client, metric="not_a_real_metric")

        assert response.status_code == 422

    def test_missing_required_field_returns_422(self, api_client):
        response = api_client.post("/api/metrics", json={"metric": "orders", "value": 10})  # no source

        assert response.status_code == 422

    def test_non_numeric_value_returns_422(self, api_client):
        response = post_metric(api_client, value="not-a-number")

        assert response.status_code == 422

    def test_empty_source_returns_422(self, api_client):
        response = post_metric(api_client, source="")  # min_length=1 on MetricIn.source

        assert response.status_code == 422

    def test_zero_and_negative_values_are_accepted(self, api_client):
        # No positivity constraint on `value` — a drop to 0 orders, or a
        # negative delta-style metric, is a legitimate real event, not
        # invalid input.
        assert post_metric(api_client, metric="orders", value=0).status_code == 201
        assert post_metric(api_client, metric="orders", value=-5).status_code == 201

    def test_nan_value_returns_422(self, api_client):
        # Phase 8 hardening: NaN previously sailed through validation
        # and could poison a rolling window's mean/stdev silently (see
        # models.py's MetricIn.value comment).
        #
        # httpx's `json=` convenience parameter refuses to even encode
        # NaN/Infinity client-side (strict JSON spec) — the request
        # would never leave the test process, let alone reach the
        # server, so this sends the raw request body bytes instead,
        # the way a real non-httpx client (curl, a misbehaving
        # producer) actually could.
        response = api_client.post(
            "/api/metrics",
            content=b'{"metric": "cpu_usage", "value": NaN, "source": "x"}',
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422

    def test_infinity_value_returns_422(self, api_client):
        response = api_client.post(
            "/api/metrics",
            content=b'{"metric": "cpu_usage", "value": Infinity, "source": "x"}',
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422


class TestLatestMetrics:
    def test_returns_one_entry_per_posted_metric(self, api_client):
        post_metric(api_client, metric="cpu_usage", source="server-1", value=50)
        post_metric(api_client, metric="orders", source="server-1", value=10)

        response = api_client.get("/api/metrics/latest")

        assert response.status_code == 200
        metrics_present = {entry["metric"] for entry in response.json()}
        assert metrics_present == {"cpu_usage", "orders"}

    def test_omits_metrics_with_no_data_rather_than_padding(self, api_client):
        post_metric(api_client, metric="cpu_usage")

        response = api_client.get("/api/metrics/latest")

        assert len(response.json()) == 1

    def test_source_filter_returns_only_that_source(self, api_client):
        post_metric(api_client, metric="cpu_usage", source="server-1", value=10)
        post_metric(api_client, metric="cpu_usage", source="server-2", value=20)

        response = api_client.get("/api/metrics/latest", params={"source": "server-2"})

        body = response.json()
        assert len(body) == 1
        assert body[0]["source"] == "server-2"
        assert body[0]["value"] == 20

    def test_empty_database_returns_empty_list_not_an_error(self, api_client):
        response = api_client.get("/api/metrics/latest")

        assert response.status_code == 200
        assert response.json() == []


class TestAnomalies:
    def test_extreme_value_is_flagged_after_a_real_baseline_and_appears_in_anomalies(self, api_client):
        # Build a real rolling window through the actual ingest+detect
        # path (not a direct DB insert) — this exercises detection
        # end-to-end, not just the /anomalies read side.
        for v in [50, 51, 49, 50, 52, 48, 50, 51, 49, 50]:
            post_metric(api_client, metric="cpu_usage", source="anomaly-test", value=v)

        response = post_metric(api_client, metric="cpu_usage", source="anomaly-test", value=500)
        assert response.status_code == 201
        assert response.json()["anomaly"] is True

        anomalies = api_client.get("/api/metrics/anomalies").json()
        assert len(anomalies) == 1
        assert anomalies[0]["value"] == 500
        assert anomalies[0]["z_score"] is not None
        assert abs(anomalies[0]["z_score"]) > 3

    def test_normal_values_never_appear_in_anomalies(self, api_client):
        for v in [50, 51, 49, 50, 52, 48, 50, 51, 49, 50, 50, 51]:
            post_metric(api_client, metric="cpu_usage", source="normal-test", value=v)

        response = api_client.get("/api/metrics/anomalies")

        assert response.status_code == 200
        assert response.json() == []

    def test_metric_filter_excludes_other_metrics(self, api_client):
        for v in [50, 51, 49, 50, 52, 48, 50, 51, 49, 50]:
            post_metric(api_client, metric="cpu_usage", source="s", value=v)
        post_metric(api_client, metric="cpu_usage", source="s", value=500)  # flags cpu_usage

        for v in [10, 11, 9, 10, 12, 8, 10, 11, 9, 10]:
            post_metric(api_client, metric="orders", source="s", value=v)
        # orders never gets an extreme value -> never flagged

        response = api_client.get("/api/metrics/anomalies", params={"metric": "orders"})

        assert response.json() == []


class TestApiPrefixConsistency:
    """Phase 8: every route now lives under /api. These lock that change
    in so it can't silently regress back to the old mixed prefixing.
    """

    def test_old_unprefixed_paths_no_longer_exist(self, api_client):
        for path in ["/metrics/latest", "/metrics/stats", "/metrics/anomalies", "/metrics/history"]:
            response = api_client.get(path)
            assert response.status_code == 404, f"{path} should 404 now that it moved under /api"

    def test_every_endpoint_reachable_under_the_api_prefix(self, api_client):
        post_metric(api_client, metric="cpu_usage", value=50)

        assert api_client.get("/api/metrics/latest").status_code == 200
        assert api_client.get("/api/metrics/anomalies").status_code == 200
        assert api_client.get("/api/metrics/history", params={"metric": "cpu_usage"}).status_code == 200
        assert api_client.get("/api/metrics/stats", params={"metric": "cpu_usage"}).status_code == 200


class TestStatsAndHistory:
    def test_stats_404s_when_the_window_has_no_data(self, api_client):
        response = api_client.get("/api/metrics/stats", params={"metric": "cpu_usage"})

        assert response.status_code == 404

    def test_stats_aggregation_matches_posted_values(self, api_client):
        for v in [10, 20, 30]:
            post_metric(api_client, metric="cpu_usage", source="s", value=v)

        response = api_client.get("/api/metrics/stats", params={"metric": "cpu_usage"})

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 3
        assert body["avg"] == 20.0
        assert body["min"] == 10.0
        assert body["max"] == 30.0

    def test_history_returns_empty_list_not_404_when_no_data(self, api_client):
        response = api_client.get("/api/metrics/history", params={"metric": "cpu_usage"})

        assert response.status_code == 200
        assert response.json() == []

    def test_oversized_minutes_returns_422_not_a_crash(self, api_client):
        # Phase 8 hardening: previously an absurd `minutes` value
        # reached timedelta(minutes=...) uncaught and raised
        # OverflowError -> a raw 500. See MAX_WINDOW_MINUTES in
        # routers/analytics.py.
        response = api_client.get(
            "/api/metrics/stats", params={"metric": "cpu_usage", "minutes": 10**21}
        )

        assert response.status_code == 422


class TestHealth:
    def test_health_check_reports_ok_against_the_test_database(self, api_client):
        response = api_client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
