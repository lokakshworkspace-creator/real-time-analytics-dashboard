"""Read endpoints for dashboard consumption: GET /metrics/latest and
GET /metrics/stats.

Phase 4 scope only — no anomaly data exists yet (that's Phase 5), so
there is deliberately no GET /metrics/anomalies here: an endpoint that
can only ever return an empty list isn't worth shipping until it has
something to return.

No `/api` prefix on these two routes, matching CLAUDE.md's own endpoint
list verbatim (`POST /api/metrics` vs. `GET /metrics/latest` — the
brief documents that split, not an oversight here).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..models import (
    METRIC_NAMES,
    LatestMetric,
    MetricName,
    MetricStats,
    metric_document_to_latest,
)

router = APIRouter(tags=["analytics"])

DEFAULT_STATS_WINDOW_MINUTES = 60


@router.get("/metrics/latest", response_model=list[LatestMetric])
async def get_latest_metrics(
    source: str | None = Query(
        default=None, description="Restrict to one source, e.g. 'server-2'."
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[LatestMetric]:
    """Most recent document for each of the 5 known metrics.

    Deliberately five independent find_one() calls (one per metric),
    not a single $group aggregation over the whole collection: each
    query is `{metric: X, ...}` sorted by timestamp descending — exactly
    what the `metric_1_timestamp_-1` index (see database.py) is built
    for, an equality match plus an already-sorted scan, no in-memory
    sort and no full collection scan. Run concurrently via
    asyncio.gather so the wall-clock cost is one round trip, not five
    sequential ones.

    A metric with zero events ever recorded is omitted from the
    response rather than padded with a placeholder value — there's no
    real "latest" to report for something that's never happened, and
    inventing one (e.g. zeros) would be misleading to a dashboard
    rendering these as cards. Response is ordered to match METRIC_NAMES
    (the same fixed order every time), convenient for a dashboard with
    fixed card positions.
    """
    base_filter: dict = {"source": source} if source else {}

    async def latest_for(metric_name: str) -> dict | None:
        return await db.metrics.find_one(
            {"metric": metric_name, **base_filter},
            sort=[("timestamp", -1)],
        )

    documents = await asyncio.gather(*(latest_for(name) for name in METRIC_NAMES))
    return [metric_document_to_latest(doc) for doc in documents if doc is not None]


@router.get("/metrics/stats", response_model=MetricStats)
async def get_metric_stats(
    metric: MetricName,
    minutes: int = Query(
        default=DEFAULT_STATS_WINDOW_MINUTES,
        gt=0,
        description="Size of the trailing window, in minutes.",
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> MetricStats:
    """Aggregate stats (count/avg/min/max) for one metric over a
    trailing time window, computed entirely in MongoDB via
    $match -> $group — not by pulling documents into Python and
    computing with statistics/pandas, per CLAUDE.md. $match on
    {metric, timestamp: {$gte: ...}} is exactly the
    `metric_1_timestamp_-1` index's use case (equality + range).

    404s if there's no data for that metric in the window, rather than
    returning zeros/nulls — a window with no data isn't "stats of
    nothing", it's the absence of an answer, and a 200 with all-zero
    numbers would be indistinguishable from a real (if boring) result.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    pipeline = [
        {"$match": {"metric": metric, "timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": None,
                "count": {"$sum": 1},
                "avg": {"$avg": "$value"},
                "min": {"$min": "$value"},
                "max": {"$max": "$value"},
            }
        },
    ]
    result = await db.metrics.aggregate(pipeline).to_list(length=1)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No '{metric}' data in the last {minutes} minute(s).",
        )

    stats = result[0]
    return MetricStats(
        metric=metric,
        minutes=minutes,
        count=stats["count"],
        avg=stats["avg"],
        min=stats["min"],
        max=stats["max"],
    )
