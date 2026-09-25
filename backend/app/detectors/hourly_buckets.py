"""Hourly (region, brand) order buckets — the shared input to the two
batch detectors (detectors/isolation_forest.py and
detectors/forecast_deviation.py).

The z-score detector (detectors/zscore.py) scores one region's order
count per hour, per order, on write. The batch detectors instead look
at a whole trailing window at once, and at more than one dimension of
each bucket, so both need the same thing: one row per (region, brand,
hour) that had at least one order, with its order count and revenue.
Fetching that once and handing the same rows to both keeps the two
detectors scoring identical data, so a disagreement between them is a
real disagreement about the data, not a difference in how each sliced it.

Hours with zero orders have no row: they have no orders to attach a
verdict to, and their average order value is undefined (0/0). The
forecast detector zero-fills them itself where a gap genuinely means
"no demand this hour" (see forecast_deviation.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class HourlyBucket:
    region: str
    brand: str
    # Naive-but-UTC, matching what MongoDB's $dateTrunc hands back (see
    # zscore.py's _hour_start for why this codebase keeps every bucket
    # key naive).
    hour: datetime
    order_count: int
    revenue: float

    @property
    def avg_order_value(self) -> float:
        return self.revenue / self.order_count

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.region, self.brand, self.hour)


async def fetch_hourly_buckets(
    db: AsyncIOMotorDatabase, *, window_start: datetime, window_end: datetime
) -> list[HourlyBucket]:
    """Every (region, brand, hour) bucket with at least one order in
    [window_start, window_end). Both bounds must be naive-UTC.

    Deliberately NOT brand-scoped: the only caller is the admin-only
    batch endpoint, which runs across every brand by design (see
    routers/detectors.py). Anything that later exposes bucket data to a
    business account must add security.brand_match_stage here.
    """
    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start, "$lt": window_end}}},
        {
            "$group": {
                "_id": {
                    "region": "$region",
                    "brand": "$brand",
                    "hour": {"$dateTrunc": {"date": "$timestamp", "unit": "hour"}},
                },
                "order_count": {"$sum": 1},
                "revenue": {"$sum": "$total_value"},
            }
        },
    ]
    rows = await db.orders.aggregate(pipeline).to_list(length=None)
    return [
        HourlyBucket(
            region=row["_id"]["region"],
            brand=row["_id"]["brand"],
            hour=row["_id"]["hour"],
            order_count=row["order_count"],
            revenue=row["revenue"],
        )
        for row in rows
    ]
