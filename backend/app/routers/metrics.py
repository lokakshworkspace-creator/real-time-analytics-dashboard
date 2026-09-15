"""Ingest endpoint: POST /api/metrics.

Validates, runs z-score anomaly detection, and stores. Detection runs
ON WRITE — see detectors/zscore.py for the guarded rolling-window
implementation and the reasoning behind that trade-off (cheap reads,
at the cost of needing reprocessing if detection logic ever changes).
Isolation Forest (Phase 5b) deliberately does NOT run here — see
detectors/isolation_forest.py for why keeping it out of the live write
path is the point, not an oversight.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..detectors import zscore
from ..models import MetricIn, MetricOut, metric_document_to_out

router = APIRouter(prefix="/api", tags=["metrics"])


@router.post("/metrics", response_model=MetricOut, status_code=status.HTTP_201_CREATED)
async def create_metric(
    payload: MetricIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> MetricOut:
    document = payload.model_dump()
    if document["timestamp"] is None:
        document["timestamp"] = datetime.now(timezone.utc)

    # Score against this metric+source's existing history BEFORE
    # inserting — the rolling window must never include the point it's
    # currently scoring.
    result = await zscore.score(
        db, metric=document["metric"], source=document["source"], value=document["value"]
    )
    document["anomaly"] = result.is_anomaly
    # Persisted (not just used transiently) so the exact number behind
    # every flag is inspectable later — directly useful for honestly
    # explaining "why was this flagged" rather than just "it was".
    # Deliberately not added to MetricOut/LatestMetric's response shape
    # (out of scope for this phase — see GET /metrics/anomalies, whose
    # entire purpose is surfacing this).
    document["z_score"] = result.z_score

    insert_result = await db.metrics.insert_one(document)
    created = await db.metrics.find_one({"_id": insert_result.inserted_id})
    return metric_document_to_out(created)
