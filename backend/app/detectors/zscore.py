"""z-score anomaly detector.

`compute_zscore` is the pure mathematical core carried over unchanged
from the earlier system-metrics MVP (see backend/tests/test_zscore.py's
deterministic unit tests): given a rolling window of prior values and a
new value, decide whether it's an outlier. `score_order_volume` is the
business-analytics wrapper around it — instead of scoring a raw metric
reading, it scores *how many orders a region has placed this hour*
against that region's last 24 hourly buckets. Reusing the same pure
function for a differently-shaped question (a per-hour count, not a
per-event value) is the point: the statistics don't care what the
numbers represent, only that they form a believable baseline.

Called synchronously inside POST /api/orders, before the new order is
inserted (detection-on-write, same trade-off as before): reads stay
cheap because nothing is computed at query time, at the cost of needing
reprocessing if this logic changes later, since old verdicts don't
retroactively change.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

# How many trailing hourly buckets form the rolling baseline for a
# region's order volume. Bounded to a day so the baseline reflects
# recent demand patterns (e.g. this region's usual daytime traffic)
# rather than slowly blending in weeks-old behavior.
WINDOW_HOURS = 24

# Minimum prior hourly buckets required before detection activates —
# the "cold start" problem. A mean/stdev computed from a couple of
# hours isn't a trustworthy baseline, so below this threshold every
# order is anomaly=False rather than risking false positives on
# essentially no history. Trade-off worth naming explicitly: in a short
# demo session (well under 10 hours of simulated traffic), the business
# anomaly panel will legitimately stay empty until enough wall-clock
# time has passed for 10 distinct hourly buckets to exist per region —
# this is a demoability cost of using *hourly* buckets rather than a
# per-event window, accepted here because CLAUDE.md's brief specifies
# hourly buckets for this detector.
MIN_WINDOW_SIZE = 10

# |z| beyond this is flagged. Under a normal distribution, ~99.7% of
# values fall within 3 standard deviations of the mean, so a breach is a
# genuinely rare event, not routine noise.
Z_THRESHOLD = 3.0


@dataclass(frozen=True)
class ZScoreResult:
    is_anomaly: bool
    z_score: float | None  # None = no verdict was possible (cold start, or a constant window)
    window_size: int  # how many prior points the verdict (or non-verdict) is based on


def compute_zscore(window: list[float], value: float) -> ZScoreResult:
    """Pure function: given prior values and a new value, returns the
    verdict. No I/O, no MongoDB, no async — the entire mathematical core
    of the detector, independent of what the numbers represent.
    """
    if len(window) < MIN_WINDOW_SIZE:
        return ZScoreResult(is_anomaly=False, z_score=None, window_size=len(window))

    mean = statistics.mean(window)
    # Sample stdev (N-1 denominator): window is a sample of this
    # series' behavior, not its entire population. Well-defined here
    # since len(window) >= MIN_WINDOW_SIZE >= 10, i.e. N-1 >= 9.
    stdev = statistics.stdev(window)

    if stdev == 0:
        # A perfectly constant recent window (e.g. a region placed
        # exactly 5 orders in each of the last 10 hours). Any deviation
        # from a zero-spread window is technically an infinite z-score,
        # which isn't a meaningful signal to act on — treat it as "no
        # verdict possible", not as an automatic flag.
        return ZScoreResult(is_anomaly=False, z_score=None, window_size=len(window))

    z = (value - mean) / stdev
    return ZScoreResult(is_anomaly=abs(z) > Z_THRESHOLD, z_score=z, window_size=len(window))


def _hour_start(timestamp: datetime) -> datetime:
    """Floors `timestamp` to the start of its UTC hour, as a *naive*
    datetime. Motor/PyMongo hand BSON dates back as naive-but-UTC
    (see models.py's _format_utc_z for the same fact elsewhere in this
    codebase) — the `$dateTrunc` aggregation below returns naive
    datetimes too, so this function's output is used as a dict key
    against those results. If it stayed timezone-aware, an aware key
    would never equal a naive one even at the identical instant, and
    every bucket lookup below would silently miss.
    """
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return timestamp.replace(minute=0, second=0, microsecond=0)


async def score_order_volume(
    db: AsyncIOMotorDatabase, *, region: str, timestamp: datetime
) -> ZScoreResult:
    """Scores this region's current-hour order count against its last
    WINDOW_HOURS hourly buckets, already stored in MongoDB.

    Must be called BEFORE the new order is inserted. `value` is the
    current hour's order count *including* the order about to be
    inserted (it hasn't been written yet, so we add 1 to what's already
    there) — otherwise an hour's first order would always be scored
    against a count of zero for its own hour, one order behind reality.

    The window only spans back as far as this region's *first-ever*
    order, not always a full WINDOW_HOURS: zero-filling every hour back
    to a fixed horizon regardless of how much real history exists would
    make `len(window)` always WINDOW_HOURS from the region's very first
    order onward, defeating compute_zscore's own cold-start guard (which
    decides "enough history" purely by window length) — a region three
    hours old would look exactly as well-established as one three weeks
    old, both zero-padded to the same length. Hours *within* that real
    span that had zero orders are still filled in below (the $group
    aggregation only returns buckets with at least one order) — omitting
    those would understate the region's true variance, which is a
    different failure mode from padding out history that was never real.

    Backed by the `region_1_timestamp_-1` index (see database.py):
    equality on region plus a bounded timestamp range, run on every
    single POST /api/orders.
    """
    hour_start = _hour_start(timestamp)

    earliest_order = await db.orders.find_one(
        {"region": region}, projection={"timestamp": 1}, sort=[("timestamp", 1)]
    )
    if earliest_order is None:
        available_hours = 0
    else:
        earliest_hour = _hour_start(earliest_order["timestamp"])
        available_hours = min(
            WINDOW_HOURS, int((hour_start - earliest_hour).total_seconds() // 3600)
        )

    window_start = hour_start - timedelta(hours=available_hours)

    pipeline = [
        {"$match": {"region": region, "timestamp": {"$gte": window_start, "$lt": hour_start}}},
        {
            "$group": {
                "_id": {"$dateTrunc": {"date": "$timestamp", "unit": "hour"}},
                "count": {"$sum": 1},
            }
        },
    ]
    buckets = await db.orders.aggregate(pipeline).to_list(length=available_hours)
    counts_by_hour = {bucket["_id"]: bucket["count"] for bucket in buckets}

    window = [
        counts_by_hour.get(hour_start - timedelta(hours=offset), 0)
        for offset in range(available_hours, 0, -1)
    ]

    current_hour_count = await db.orders.count_documents(
        {"region": region, "timestamp": {"$gte": hour_start, "$lt": hour_start + timedelta(hours=1)}}
    )
    value = current_hour_count + 1

    return compute_zscore(window, value)
