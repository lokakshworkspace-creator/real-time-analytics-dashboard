"""The batch pass: fetches one trailing window of hourly buckets, runs
BOTH batch detectors (Isolation Forest + forecast deviation) over it,
and stamps each verdict onto the order documents that make up the
scored bucket — the same records that already carry the z-score verdict
from ingest (see routers/orders.py's create_order), not a parallel
collection.

Field semantics on an order document (all written here, none at ingest):
  is_anomaly_if / if_score            Isolation Forest verdict for the
                                      order's (region, brand, hour) bucket
  is_anomaly_forecast /               forecast-deviation verdict for the
  forecast_expected / forecast_actual same bucket
Absent (null when read) means "no batch run has scored this order's
bucket" — either the order predates every run, or its bucket had too
little history for that detector. False means "scored, not flagged".
That distinction is why every scored bucket is stamped, not just the
flagged ones. Deliberately no retroactive backfill beyond the trailing
window: a verdict needs the window's context, and re-deriving one for a
bucket months old from data that has since aged out of the window would
be a different (and less defensible) number than the one a run at the
time would have produced.

A bucket verdict is stamped on EVERY order in the bucket (one
update_many per bucket), so each order reads as one combined record —
consistent with how the z-score detector already flags every order past
its threshold, not just the first. Consequence worth knowing: one flagged
hour appears as several rows in the anomaly list, as a z-score flood
already does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateMany

from ..models import BatchRunSummary
from . import forecast_deviation, isolation_forest
from .hourly_buckets import fetch_hourly_buckets
from .zscore import _hour_start

DEFAULT_WINDOW_DAYS = 7
DEFAULT_RECENT_HOURS = 24

_HOUR = timedelta(hours=1)


async def run_batch(
    db: AsyncIOMotorDatabase,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    recent_hours: int = DEFAULT_RECENT_HOURS,
    now: datetime | None = None,
) -> BatchRunSummary:
    """Fits/scores over the trailing `window_days` (ending at the end of
    the current hour), stamping verdicts for buckets in the last
    `recent_hours`. `now` is injectable for deterministic tests.
    """
    current_hour = _hour_start(now or datetime.now(timezone.utc))
    window_start = current_hour - timedelta(days=window_days)
    window_end = current_hour + _HOUR
    recent_from = current_hour - timedelta(hours=recent_hours - 1)

    buckets = await fetch_hourly_buckets(db, window_start=window_start, window_end=window_end)

    if_verdicts = isolation_forest.score_buckets(buckets, recent_from=recent_from)
    forecast_verdicts = forecast_deviation.score_buckets(buckets, recent_from=recent_from)

    operations = []
    for bucket in buckets:
        fields: dict = {}
        if_verdict = if_verdicts.get(bucket.key)
        if if_verdict is not None:
            fields["is_anomaly_if"] = if_verdict.flagged
            fields["if_score"] = if_verdict.score
        forecast_verdict = forecast_verdicts.get(bucket.key)
        if forecast_verdict is not None:
            fields["is_anomaly_forecast"] = forecast_verdict.flagged
            fields["forecast_expected"] = forecast_verdict.expected
            fields["forecast_actual"] = forecast_verdict.actual
        if not fields:
            continue
        operations.append(
            UpdateMany(
                {
                    "region": bucket.region,
                    "brand": bucket.brand,
                    "timestamp": {"$gte": bucket.hour, "$lt": bucket.hour + _HOUR},
                },
                {"$set": fields},
            )
        )

    orders_stamped = 0
    if operations:
        result = await db.orders.bulk_write(operations, ordered=False)
        # matched, not modified: re-running over unchanged data rewrites
        # identical values, which MongoDB reports as matched-but-not-
        # modified — "orders this run scored" is the number worth returning.
        orders_stamped = result.matched_count

    note = None
    if len(buckets) < isolation_forest.MIN_BUCKETS:
        note = (
            f"Isolation Forest skipped: {len(buckets)} hourly bucket(s) in the window, "
            f"need at least {isolation_forest.MIN_BUCKETS}."
        )

    return BatchRunSummary(
        window_days=window_days,
        recent_hours=recent_hours,
        buckets_in_window=len(buckets),
        isolation_forest_buckets_scored=len(if_verdicts),
        isolation_forest_buckets_flagged=sum(v.flagged for v in if_verdicts.values()),
        forecast_buckets_scored=len(forecast_verdicts),
        forecast_buckets_flagged=sum(v.flagged for v in forecast_verdicts.values()),
        orders_stamped=orders_stamped,
        note=note,
    )
