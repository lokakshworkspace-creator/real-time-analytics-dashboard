"""Ingest endpoint: POST /api/metrics.

Phase 2 scope only — validate and store. No anomaly detection here yet;
every document is written with anomaly=False. Phase 5 will replace that
hard-coded value with a real z-score check run at write time (see the
detection trade-off note in CLAUDE.md: detecting on write keeps reads
cheap, at the cost of needing a reprocessing pass if detection logic
changes later).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
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

    # Placeholder until Phase 5 wires in the real z-score detector.
    document["anomaly"] = False

    result = await db.metrics.insert_one(document)
    created = await db.metrics.find_one({"_id": result.inserted_id})
    return metric_document_to_out(created)
