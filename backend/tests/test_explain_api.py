"""Tests for POST /api/anomalies/{id}/explain and the GeminiExplainer
wrapper behind it. The real Gemini API is never called: routes get a
FakeExplainer (conftest.py) that records calls, and the wrapper is tested
against a fake SDK client. conftest's autouse guard makes an accidental
real client fail the test outright.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import errors

from app.llm import Explanation, GeminiExplainer, LLMRateLimited, LLMUnavailable

REFERENCE_HOUR = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def post_order(client, *, region, brand="Nike", product_id="sku-001", timestamp=None, quantity=1):
    payload = {
        "order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "product_name": f"{brand} Product",
        "category": "Apparel",
        "brand": brand,
        "quantity": quantity,
        "unit_price": 50.0,
        "region": region,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return client.post("/api/orders", json=payload)


def make_anomaly(client, admin_headers, *, brand="Nike", region="Explain Region") -> str:
    """Builds a real z-score flood for `brand` and returns one flagged
    order's anomaly id."""
    for hours_ago, count in zip(range(10, 0, -1), [3, 4] * 5):
        for _ in range(count):
            post_order(client, region=region, brand=brand, timestamp=REFERENCE_HOUR - timedelta(hours=hours_ago))
    for _ in range(20):
        post_order(client, region=region, brand=brand, timestamp=REFERENCE_HOUR)
    anomalies = client.get("/api/anomalies/business", headers=admin_headers, params={"brand": brand}).json()
    in_region = [a for a in anomalies if a["region"] == region]
    assert in_region, "scenario must produce a flagged anomaly"
    return in_region[0]["id"]


def explain(client, anomaly_id, headers):
    return client.post(f"/api/anomalies/{anomaly_id}/explain", headers=headers)


class TestExplainBasics:
    def test_requires_auth(self, api_client, fake_explainer):
        assert api_client.post("/api/anomalies/6ab5efdaec656456fa8063a1/explain").status_code == 401

    def test_malformed_id_is_404(self, api_client, admin_headers, fake_explainer):
        assert explain(api_client, "not-an-object-id", admin_headers).status_code == 404
        assert fake_explainer.calls == []

    def test_unknown_id_is_404(self, api_client, admin_headers, fake_explainer):
        assert explain(api_client, "6ab5efdaec656456fa8063a1", admin_headers).status_code == 404
        assert fake_explainer.calls == []

    def test_an_unflagged_order_has_nothing_to_explain(self, api_client, admin_headers, fake_explainer):
        post_order(api_client, region="Quiet Region")
        order_id = api_client.get("/api/orders", headers=admin_headers).json()[0]["id"]

        response = explain(api_client, order_id, admin_headers)

        assert response.status_code == 404
        assert fake_explainer.calls == []


class TestExplainGeneration:
    def test_first_call_generates_stores_and_returns_an_explanation(
        self, api_client, admin_headers, fake_explainer
    ):
        anomaly_id = make_anomaly(api_client, admin_headers)

        response = explain(api_client, anomaly_id, admin_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["anomaly_id"] == anomaly_id
        assert body["cached"] is False
        assert body["explanation"] and body["suggested_action"]
        assert body["explained_at"].endswith("Z")
        assert len(fake_explainer.calls) == 1

    def test_the_model_is_handed_the_structured_context(self, api_client, admin_headers, fake_explainer):
        api_client.post(
            "/api/inventory/seed",
            json={
                "product_id": "sku-001", "product_name": "Nike Product", "category": "Apparel",
                "brand": "Nike", "region": "Explain Region", "current_stock": 5000,
            },
        )
        anomaly_id = make_anomaly(api_client, admin_headers)

        explain(api_client, anomaly_id, admin_headers)

        context = fake_explainer.calls[0]
        assert context["anomaly"]["region"] == "Explain Region"
        assert context["anomaly"]["brand"] == "Nike"
        assert context["anomaly"]["product"] == "Nike Product"
        assert context["z_score_of_hourly_order_count"]["flagged"] is True
        assert abs(context["z_score_of_hourly_order_count"]["score"]) > 3  # under-volume hours flag with a negative z
        # No batch run yet: the batch detectors are unknown (null), not "not flagged".
        assert context["isolation_forest"] is None
        assert context["forecast_deviation"] is None
        assert context["detectors_flagged"] == "1 of 3"
        assert context["inventory"]["current_stock"] is not None
        assert "days_of_stock_remaining" in context["inventory"]
        growth = context["revenue_change_pct_vs_previous_7d"]
        assert set(growth) == {"product", "region"}
        json.dumps(context)  # must be JSON-serializable, it is sent to the model as JSON

    def test_batch_verdicts_appear_in_the_context_once_a_run_has_scored_the_record(
        self, api_client, admin_headers, fake_explainer
    ):
        # Recent (not REFERENCE_HOUR) so the batch window covers it.
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for hours_back, count in zip(range(12, 0, -1), [2, 3] * 6):
            for i in range(count):
                post_order(api_client, region="Batch Ctx", timestamp=hour - timedelta(hours=hours_back, minutes=-5 - i))
        for i in range(20):
            post_order(api_client, region="Batch Ctx", timestamp=hour + timedelta(seconds=i), quantity=4)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)
        anomaly_id = api_client.get("/api/anomalies/business", headers=admin_headers).json()[0]["id"]

        explain(api_client, anomaly_id, admin_headers)

        context = fake_explainer.calls[0]
        assert context["isolation_forest"]["flagged"] is True
        assert context["forecast_deviation"]["flagged"] is True
        assert context["forecast_deviation"]["actual_orders_in_that_hour"] == 20.0
        assert context["detectors_flagged"] in ("2 of 3", "3 of 3")


class TestExplainCaching:
    def test_second_call_returns_the_stored_explanation_with_zero_model_calls(
        self, api_client, admin_headers, fake_explainer
    ):
        anomaly_id = make_anomaly(api_client, admin_headers)

        first = explain(api_client, anomaly_id, admin_headers).json()
        assert len(fake_explainer.calls) == 1

        second = explain(api_client, anomaly_id, admin_headers).json()
        third = explain(api_client, anomaly_id, admin_headers).json()

        assert len(fake_explainer.calls) == 1, "repeat calls must not reach the model"
        assert first["cached"] is False
        assert second["cached"] is True and third["cached"] is True
        assert second["explanation"] == first["explanation"]
        assert second["suggested_action"] == first["suggested_action"]
        assert second["explained_at"] == first["explained_at"]

    def test_a_cached_explanation_is_served_even_if_the_model_is_now_failing(
        self, api_client, admin_headers, fake_explainer
    ):
        anomaly_id = make_anomaly(api_client, admin_headers)
        explain(api_client, anomaly_id, admin_headers)
        fake_explainer.error = LLMUnavailable("Explanations are unavailable: GEMINI_API_KEY is not configured.")

        response = explain(api_client, anomaly_id, admin_headers)

        assert response.status_code == 200
        assert response.json()["cached"] is True

    def test_caching_is_per_anomaly(self, api_client, admin_headers, fake_explainer):
        first_id = make_anomaly(api_client, admin_headers, region="Region A")
        second_id = make_anomaly(api_client, admin_headers, region="Region B")

        explain(api_client, first_id, admin_headers)
        explain(api_client, second_id, admin_headers)

        assert len(fake_explainer.calls) == 2


def make_recent_flood(client, region="Fresh Flood"):
    """A z-score-flagged flood inside the batch detectors' window (unlike
    make_anomaly, whose fixed 2026-01-01 hour is far outside it)."""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for hours_back, count in zip(range(12, 0, -1), [2, 3] * 6):
        for i in range(count):
            post_order(client, region=region, timestamp=hour - timedelta(hours=hours_back) + timedelta(minutes=5 + i))
    for i in range(20):
        post_order(client, region=region, timestamp=hour + timedelta(seconds=i), quantity=4)


class TestExplainCacheInvalidation:
    """An explanation is only reusable while the verdicts it was written
    against still hold. Regression for a real bug: an explanation cached
    while only the z-score had scored a record ("1 of 3 detectors") kept
    being served after a batch run added the other two verdicts."""

    def _flagged_id(self, client, admin_headers) -> str:
        anomalies = client.get("/api/anomalies/business", headers=admin_headers).json()
        assert anomalies
        return anomalies[0]["id"]

    def test_a_batch_run_after_the_first_explanation_makes_the_next_call_regenerate(
        self, api_client, admin_headers, fake_explainer
    ):
        make_recent_flood(api_client)
        anomaly_id = self._flagged_id(api_client, admin_headers)

        first = explain(api_client, anomaly_id, admin_headers).json()
        assert first["cached"] is False
        assert fake_explainer.calls[0]["isolation_forest"] is None  # only the z-score has scored it
        assert fake_explainer.calls[0]["detectors_flagged"] == "1 of 3"

        assert explain(api_client, anomaly_id, admin_headers).json()["cached"] is True
        assert len(fake_explainer.calls) == 1

        api_client.post("/api/detectors/run-batch", headers=admin_headers)

        after = explain(api_client, anomaly_id, admin_headers).json()
        assert after["cached"] is False, "new verdicts arrived; the old explanation must not be served"
        assert len(fake_explainer.calls) == 2
        regenerated_context = fake_explainer.calls[1]
        assert regenerated_context["isolation_forest"]["flagged"] is True
        assert regenerated_context["forecast_deviation"]["flagged"] is True
        assert regenerated_context["detectors_flagged"] == "3 of 3"

        # The regenerated explanation is what's now stored.
        again = explain(api_client, anomaly_id, admin_headers).json()
        assert again["cached"] is True
        assert again["explained_at"] == after["explained_at"]
        assert len(fake_explainer.calls) == 2

    def test_rerunning_the_batch_over_unchanged_data_does_not_invalidate(
        self, api_client, admin_headers, fake_explainer
    ):
        make_recent_flood(api_client)
        api_client.post("/api/detectors/run-batch", headers=admin_headers)
        anomaly_id = self._flagged_id(api_client, admin_headers)
        explain(api_client, anomaly_id, admin_headers)

        api_client.post("/api/detectors/run-batch", headers=admin_headers)  # same data, same verdicts

        assert explain(api_client, anomaly_id, admin_headers).json()["cached"] is True
        assert len(fake_explainer.calls) == 1

    def test_an_explanation_with_no_stored_basis_is_regenerated_once(
        self, api_client, admin_headers, fake_explainer
    ):
        # Records explained before the fingerprint existed have nothing to
        # compare against; unknown provenance isn't trusted.
        import pymongo

        from app.config import settings
        from tests.conftest import TEST_DB_NAME

        make_recent_flood(api_client)
        anomaly_id = self._flagged_id(api_client, admin_headers)
        explain(api_client, anomaly_id, admin_headers)
        with pymongo.MongoClient(settings.mongodb_uri) as sync_client:
            sync_client[TEST_DB_NAME].orders.update_many({}, {"$unset": {"explanation_basis": ""}})

        regenerated = explain(api_client, anomaly_id, admin_headers).json()
        assert regenerated["cached"] is False
        assert len(fake_explainer.calls) == 2

        assert explain(api_client, anomaly_id, admin_headers).json()["cached"] is True
        assert len(fake_explainer.calls) == 2


class TestExplainBrandScoping:
    def test_business_account_cannot_explain_another_brands_anomaly(
        self, api_client, admin_headers, business_headers_factory, fake_explainer
    ):
        adidas_anomaly = make_anomaly(api_client, admin_headers, brand="Adidas", region="Adidas Region")
        nike_headers = business_headers_factory("nike-explain@test.example.com", ["Nike"])

        response = explain(api_client, adidas_anomaly, nike_headers)

        assert response.status_code == 404
        assert fake_explainer.calls == [], "another brand's data must never reach the model"

    def test_a_foreign_brand_cannot_read_a_cached_explanation_either(
        self, api_client, admin_headers, business_headers_factory, fake_explainer
    ):
        adidas_anomaly = make_anomaly(api_client, admin_headers, brand="Adidas", region="Adidas Region")
        explain(api_client, adidas_anomaly, admin_headers)  # now cached
        nike_headers = business_headers_factory("nike-cache@test.example.com", ["Nike"])

        response = explain(api_client, adidas_anomaly, nike_headers)

        assert response.status_code == 404
        assert "explanation" not in response.json()

    def test_business_account_can_explain_its_own_brands_anomaly(
        self, api_client, admin_headers, business_headers_factory, fake_explainer
    ):
        nike_anomaly = make_anomaly(api_client, admin_headers, brand="Nike")
        nike_headers = business_headers_factory("nike-own@test.example.com", ["Nike"])

        response = explain(api_client, nike_anomaly, nike_headers)

        assert response.status_code == 200
        assert len(fake_explainer.calls) == 1
        assert fake_explainer.calls[0]["anomaly"]["brand"] == "Nike"

    def test_admin_can_explain_any_brand(self, api_client, admin_headers, fake_explainer):
        adidas_anomaly = make_anomaly(api_client, admin_headers, brand="Adidas", region="Adidas Region")

        assert explain(api_client, adidas_anomaly, admin_headers).status_code == 200


class TestExplainProviderFailures:
    def test_a_rate_limit_becomes_a_clean_503_not_a_raw_error(self, api_client, admin_headers, fake_explainer):
        anomaly_id = make_anomaly(api_client, admin_headers)
        fake_explainer.error = LLMRateLimited()

        response = explain(api_client, anomaly_id, admin_headers)

        assert response.status_code == 503
        assert "try again shortly" in response.json()["detail"].lower()
        assert response.headers["retry-after"] == "30"
        assert "429" not in response.text and "RESOURCE_EXHAUSTED" not in response.text

    def test_a_failed_call_stores_nothing_so_a_retry_can_succeed(self, api_client, admin_headers, fake_explainer):
        anomaly_id = make_anomaly(api_client, admin_headers)
        fake_explainer.error = LLMRateLimited()
        assert explain(api_client, anomaly_id, admin_headers).status_code == 503

        fake_explainer.error = None
        retry = explain(api_client, anomaly_id, admin_headers)

        assert retry.status_code == 200
        assert retry.json()["cached"] is False

    def test_an_unavailable_service_is_a_503_with_its_own_message(self, api_client, admin_headers, fake_explainer):
        anomaly_id = make_anomaly(api_client, admin_headers)
        fake_explainer.error = LLMUnavailable("Explanations are unavailable: GEMINI_API_KEY is not configured.")

        response = explain(api_client, anomaly_id, admin_headers)

        assert response.status_code == 503
        assert "GEMINI_API_KEY" in response.json()["detail"]


# --- GeminiExplainer (the SDK wrapper), against a fake SDK client ------------


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return _FakeResponse(self._result)


class _FakeSdkClient:
    def __init__(self, result):
        self.aio = type("Aio", (), {})()
        self.aio.models = _FakeModels(result)


def explainer_with(result, *, api_key="test-key"):
    explainer = GeminiExplainer(api_key=api_key, model="test-model")
    client = _FakeSdkClient(result)
    explainer._get_client = lambda: client  # instance attr beats conftest's class-level guard
    return explainer, client


CONTEXT = {"anomaly": {"region": "EU"}, "z_score": {"flagged": True, "score": 5.1}}


class TestGeminiExplainer:
    def test_parses_a_structured_reply(self):
        reply = json.dumps({"summary": "  Volume spiked.  ", "suggested_action": " Check promos. "})
        explainer, client = explainer_with(reply)

        result = asyncio.run(explainer.explain(CONTEXT))

        assert result == Explanation(text="Volume spiked.", suggested_action="Check promos.")
        call = client.aio.models.calls[0]
        assert call["model"] == "test-model"
        assert json.loads(call["contents"]) == CONTEXT
        assert call["config"].response_mime_type == "application/json"
        assert "never invent" in call["config"].system_instruction.lower()

    def test_a_429_becomes_llm_rate_limited(self):
        explainer, _ = explainer_with(errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}))

        with pytest.raises(LLMRateLimited):
            asyncio.run(explainer.explain(CONTEXT))

    def test_other_api_errors_become_llm_unavailable_without_leaking_details(self):
        explainer, _ = explainer_with(errors.ServerError(500, {"error": {"message": "secret internal detail", "status": "INTERNAL"}}))

        with pytest.raises(LLMUnavailable) as excinfo:
            asyncio.run(explainer.explain(CONTEXT))

        assert "secret internal detail" not in str(excinfo.value)

    def test_a_non_429_client_error_is_not_mistaken_for_a_rate_limit(self):
        explainer, _ = explainer_with(errors.ClientError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}}))

        with pytest.raises(LLMUnavailable):
            asyncio.run(explainer.explain(CONTEXT))

    def test_a_network_failure_becomes_llm_unavailable(self):
        explainer, _ = explainer_with(TimeoutError("timed out"))

        with pytest.raises(LLMUnavailable):
            asyncio.run(explainer.explain(CONTEXT))

    @pytest.mark.parametrize("bad_reply", ["not json at all", "{}", '{"summary": "only one field"}', ""])
    def test_an_unreadable_reply_becomes_llm_unavailable(self, bad_reply):
        explainer, _ = explainer_with(bad_reply)

        with pytest.raises(LLMUnavailable):
            asyncio.run(explainer.explain(CONTEXT))

    def test_no_api_key_fails_fast_without_touching_the_sdk(self):
        explainer = GeminiExplainer(api_key="", model="test-model")  # conftest guard would blow up on any client use

        with pytest.raises(LLMUnavailable) as excinfo:
            asyncio.run(explainer.explain(CONTEXT))

        assert "GEMINI_API_KEY" in str(excinfo.value)
