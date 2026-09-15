"""Read endpoints for dashboard consumption: GET /api/metrics/latest,
GET /api/metrics/stats, GET /api/metrics/anomalies (Phase 5), and (as
of Phase 6) GET /api/metrics/history.

Phase 8 note: these routes originally had no `/api` prefix, matching
CLAUDE.md's own endpoint list verbatim (`POST /api/metrics` vs. bare
`GET /metrics/latest`) — flagged in Phase 4 as an inconsistency to
revisit, not fixed then because it wasn't in that phase's scope.
Standardized here under one shared prefix (`/api`) for every route in
the app, which is what "polish" means: consistent API surface, a
single mental model for API consumers (including the frontend's
api/client.js, updated in the same change), and one less thing to
explain away as "deliberate" when it was really just deferred.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..models import (
    METRIC_NAMES,
    AnomalyEvent,
    HistoryPoint,
    LatestMetric,
    MetricName,
    MetricStats,
    metric_document_to_anomaly,
    metric_document_to_history_point,
    metric_document_to_latest,
)

router = APIRouter(prefix="/api", tags=["analytics"])

DEFAULT_STATS_WINDOW_MINUTES = 60
DEFAULT_HISTORY_WINDOW_MINUTES = 60

# Upper bound on `minutes` for /stats and /history (Phase 8 hardening).
# Without it, an absurd value (e.g. a client typo with a dozen extra
# digits) reaches `timedelta(minutes=minutes)` and raises an uncaught
# OverflowError — confirmed directly: `timedelta(minutes=10**21)`
# raises "Python int too large to convert to C int". A generous year-
# long ceiling comfortably covers any real dashboard use case while
# turning that crash into a clean 422 instead.
MAX_WINDOW_MINUTES = 60 * 24 * 365


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
        le=MAX_WINDOW_MINUTES,
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


@router.get("/metrics/anomalies", response_model=list[AnomalyEvent])
async def get_anomalies(
    limit: int = Query(default=50, gt=0, le=500, description="Max events to return."),
    metric: MetricName | None = Query(
        default=None, description="Restrict to one metric, e.g. 'cpu_usage'."
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[AnomalyEvent]:
    """Flagged events, newest first — the z-score detector's own record
    of what it flagged and why (see detectors/zscore.py; the `z_score`
    field on each result is what actually triggered it).

    {anomaly: True} + sort by timestamp is exactly what the
    `anomaly_1_timestamp_-1` index (see database.py) is built for. When
    `metric` is also given, that filter is applied as a post-scan fetch
    filter rather than its own index range (there's no anomaly+metric
    compound index) — still fine here, since the anomaly index already
    does the expensive part: skipping every non-flagged document
    without a full collection scan.
    """
    query_filter: dict = {"anomaly": True}
    if metric:
        query_filter["metric"] = metric

    cursor = db.metrics.find(query_filter).sort("timestamp", -1).limit(limit)
    documents = [doc async for doc in cursor]
    return [metric_document_to_anomaly(doc) for doc in documents]


@router.get("/metrics/history", response_model=list[HistoryPoint])
async def get_metric_history(
    metric: MetricName,
    minutes: int = Query(
        default=DEFAULT_HISTORY_WINDOW_MINUTES,
        gt=0,
        le=MAX_WINDOW_MINUTES,
        description="Size of the trailing window, in minutes.",
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[HistoryPoint]:
    """Raw (timestamp, value) points for one metric over a trailing
    window, oldest first — added in Phase 6 specifically so the trend
    chart can render real history from a single fetch on mount, without
    needing the client-side polling loop that's reserved for Phase 7.

    $match on {metric, timestamp: {$gte: ...}} sorted by timestamp is
    the same shape /metrics/stats uses, so it's served by the same
    `metric_1_timestamp_-1` index — ascending order is still covered,
    since MongoDB can walk a descending index in reverse.

    Returns an empty list (200), not a 404, when there's no data in the
    window — unlike /metrics/stats, an empty list isn't a missing
    answer, it's a legitimate (if boring) chart with no points yet.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    cursor = db.metrics.find(
        {"metric": metric, "timestamp": {"$gte": window_start}},
        projection={"timestamp": 1, "value": 1, "_id": 0},
        sort=[("timestamp", 1)],
    )
    documents = [doc async for doc in cursor]
    return [metric_document_to_history_point(doc) for doc in documents]
