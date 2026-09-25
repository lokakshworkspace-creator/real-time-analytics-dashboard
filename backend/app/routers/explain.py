"""POST /api/anomalies/{anomaly_id}/explain — the on-demand LLM
explanation of one flagged anomaly (see llm.py for what the model is and
isn't trusted with).

Order of operations is deliberate:
  1. Find the record, brand-scoped. Anything the caller can't see is a
     plain 404 — a business account can't tell "doesn't exist" from
     "another brand's", so this can't be used to probe other brands'
     anomaly ids.
  2. If it already has an explanation AND the detector verdicts that
     explanation was written against still match the record's current
     ones (see _verdict_basis), return it. No context gathering, no
     explainer call — a repeat click is free, and (because this runs
     before the explainer is used) it still works with no API key set.
     If the verdicts have changed since (a batch run scored the record
     later), the stored text is stale and falls through to step 3.
  3. Otherwise gather structured context, call the model once, store the
     result on the record, return it.

POST rather than GET: the first call has a side effect (it spends quota
and writes to the record), which is what POST means.
"""

from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..llm import Explainer, LLMRateLimited, LLMUnavailable, get_explainer
from ..models import ExplanationResponse, UserOut, order_document_to_anomaly
from ..security import brand_match_stage, get_current_user
from .inventory import get_inventory_risk
from .orders import RANGE_DAYS, compute_change_pct

router = APIRouter(prefix="/api", tags=["anomalies"])

# Same trailing window (and vocabulary) as GET /api/alerts' decline
# detection and the dashboard's default range, so "recent growth" means
# the same thing everywhere.
GROWTH_RANGE = "7d"

# Free-tier rate-limit responses tell the client when to retry; this is
# a conservative fixed hint, not read from the provider.
RETRY_AFTER_SECONDS = 30


def _verdict_basis(document: dict) -> dict:
    """A fingerprint of the detector verdicts an explanation is written
    against: the z-score's flag, the Isolation Forest's flag, and the
    forecast verdict including the two numbers its text quotes (expected
    and actual orders that hour). None means "that detector hasn't scored
    this record".

    Stored beside the explanation, and the cache only counts as a hit if
    the record's CURRENT fingerprint still equals it. Without this, an
    explanation generated while only the z-score had scored a record
    ("flagged by 1 of 3 detectors") survived forever, contradicting the
    same row's badges once a batch run added the other two verdicts — a
    real bug found while comparing explanations across scenarios. The same
    check also catches a later batch run over a still-filling hour, where
    the forecast's `actual` moves (the text said 14 orders; it's now 22).
    Re-running the batch over unchanged data leaves the fingerprint
    identical, so it costs no extra model calls. A record explained
    before this existed has no stored fingerprint, so it's regenerated
    once — unknown provenance isn't trusted.

    Isolation Forest's score is deliberately excluded: it shifts slightly
    whenever the pooled window changes, and the text doesn't quote it.
    """
    forecast = None
    if document.get("is_anomaly_forecast") is not None:
        forecast = [
            bool(document["is_anomaly_forecast"]),
            round(document["forecast_expected"], 2),
            round(document["forecast_actual"], 2),
        ]
    return {
        "z_score": bool(document.get("anomaly")),
        "isolation_forest": document.get("is_anomaly_if"),
        "forecast": forecast,
    }


async def _revenue_growth_pct(
    db: AsyncIOMotorDatabase, brand_filter: dict, field: str, value: str
) -> float | None:
    """Current-vs-previous-period revenue change for one product or
    region (`field` is "product_id" or "region") — the same
    current/previous/change_pct pattern as /api/orders/regions and
    /products, via the shared compute_change_pct. None when the previous
    period had no revenue to compare against.
    """
    days = RANGE_DAYS[GROWTH_RANGE]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    group = {"$group": {"_id": None, "revenue": {"$sum": "$total_value"}}}
    pipeline = [
        {"$match": {**brand_filter, field: value, "timestamp": {"$gte": previous_start}}},
        {
            "$facet": {
                "current": [{"$match": {"timestamp": {"$gte": current_start}}}, group],
                "previous": [{"$match": {"timestamp": {"$lt": current_start}}}, group],
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)
    facets = result[0] if result else {"current": [], "previous": []}
    current = facets["current"][0]["revenue"] if facets["current"] else 0.0
    previous = facets["previous"][0]["revenue"] if facets["previous"] else 0.0
    return compute_change_pct(current, previous)


async def _gather_context(
    db: AsyncIOMotorDatabase, current_user: UserOut, document: dict
) -> dict:
    event = order_document_to_anomaly(document)
    brand_filter = brand_match_stage(current_user) or {}

    risk_items = await get_inventory_risk(
        minutes=60 * 24, low_stock_threshold=20, format="json", brand=None,
        current_user=current_user, db=db,
    )
    inventory = next(
        (i for i in risk_items if i.product_id == event.product_id and i.region == event.region), None
    )

    return {
        "anomaly": {
            "product": event.product_name,
            "brand": event.brand,
            "region": event.region,
            "order_timestamp": event.timestamp.isoformat(),
            "order_quantity": event.quantity,
            "order_value": event.total_value,
        },
        "detectors_flagged": f"{event.detector_agreement} of 3",
        # Field names carry their units on purpose. The bare names these
        # verdicts have in the API (`expected`, `actual`, `score`) are
        # ambiguous once they sit in a JSON blob next to `order_quantity`
        # (units in ONE order): the first live run had the model describe
        # the forecast's 14 orders-that-hour as an "order quantity of 14".
        # A null verdict means that detector has not scored this record.
        "z_score_of_hourly_order_count": event.z_score.model_dump(),
        "isolation_forest": (
            {"flagged": event.isolation_forest.flagged, "anomaly_score_0_to_1": event.isolation_forest.score}
            if event.isolation_forest
            else None
        ),
        "forecast_deviation": (
            {
                "flagged": event.forecast.flagged,
                "expected_orders_in_that_hour": event.forecast.expected,
                "actual_orders_in_that_hour": event.forecast.actual,
            }
            if event.forecast
            else None
        ),
        "inventory": {
            "current_stock": inventory.current_stock if inventory else None,
            "days_of_stock_remaining": inventory.days_of_stock_remaining if inventory else None,
        },
        f"revenue_change_pct_vs_previous_{GROWTH_RANGE}": {
            "product": await _revenue_growth_pct(db, brand_filter, "product_id", event.product_id),
            "region": await _revenue_growth_pct(db, brand_filter, "region", event.region),
        },
    }


@router.post("/anomalies/{anomaly_id}/explain", response_model=ExplanationResponse)
async def explain_anomaly(
    anomaly_id: str,
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
    explainer: Explainer = Depends(get_explainer),
) -> ExplanationResponse:
    try:
        object_id = ObjectId(anomaly_id)
    except (InvalidId, TypeError) as exc:
        raise HTTPException(status_code=404, detail="Anomaly not found") from exc

    # Must be flagged by at least one detector — an ordinary order has
    # nothing to explain — and inside the caller's brand scope.
    document = await db.orders.find_one(
        {
            "_id": object_id,
            "$or": [{"anomaly": True}, {"is_anomaly_if": True}, {"is_anomaly_forecast": True}],
            **(brand_match_stage(current_user) or {}),
        }
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")

    basis = _verdict_basis(document)
    if document.get("explanation") and document.get("explanation_basis") == basis:
        return ExplanationResponse(
            anomaly_id=anomaly_id,
            explanation=document["explanation"],
            suggested_action=document["suggested_action"],
            explained_at=document["explained_at"],
            cached=True,
        )

    context = await _gather_context(db, current_user, document)
    try:
        result = await explainer.explain(context)
    except LLMRateLimited:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The explanation service is busy right now. Please try again shortly.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    except LLMUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    # Truncated to milliseconds: BSON dates only hold millisecond
    # precision, so a microsecond timestamp returned here would differ
    # from the one a later (cached) read gets back from MongoDB, and the
    # same explanation would report two different times.
    now = datetime.now(timezone.utc)
    explained_at = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    await db.orders.update_one(
        {"_id": object_id},
        {
            "$set": {
                "explanation": result.text,
                "suggested_action": result.suggested_action,
                "explained_at": explained_at,
                "explanation_basis": basis,
            }
        },
    )
    return ExplanationResponse(
        anomaly_id=anomaly_id,
        explanation=result.text,
        suggested_action=result.suggested_action,
        explained_at=explained_at,
        cached=False,
    )
