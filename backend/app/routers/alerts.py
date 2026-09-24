"""Alerts/notification center: GET /api/alerts.

Consolidates three otherwise-separate signals this app already computes
— business anomalies, HIGH-risk inventory, and declining regions/
products — into one ranked feed, rather than a dashboard viewer having
to check three panels separately to answer "what needs my attention
right now?" Reuses the existing endpoint functions directly wherever
their contract genuinely matches (get_business_anomalies,
get_inventory_risk, get_region_stats all return exactly what's needed,
already brand-scoped) instead of re-querying the same collections a
second time. The one place that isn't reused as-is is per-product
decline detection — get_product_stats's top/bottom overlap-fix caps
each direction to half the catalog (see routers/orders.py), which is
the right behavior for a top-sellers table but wrong here: a genuinely
declining product sitting in the "top" half by raw revenue would be
silently excluded. A small dedicated aggregation below (still reusing
compute_change_pct) covers every product instead.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..models import Alert, UserOut
from ..security import brand_match_stage, get_current_user
from .inventory import get_inventory_risk
from .orders import RANGE_DAYS, compute_change_pct, get_business_anomalies, get_region_stats

router = APIRouter(prefix="/api", tags=["alerts"])

# A region or product whose current-period revenue fell by more than
# this many percent vs. the previous period of equal length generates
# a "decline" alert. Negative because change_pct is signed (a decline
# is a negative percent change) — -20% is a deliberately noticeable
# drop, not routine week-to-week noise, without being so strict
# (e.g. -50%) that only a near-total collapse gets flagged.
DECLINE_ALERT_THRESHOLD_PCT = -20.0

# Severity tiers for a decline alert, by how far past the threshold the
# actual drop is — mirrors the spirit of detectors/zscore.py's severity
# tiers (multiples of a base threshold) rather than inventing an
# unrelated scale.
DECLINE_MODERATE_THRESHOLD_PCT = -35.0
DECLINE_SEVERE_THRESHOLD_PCT = -50.0

# GET /api/orders/regions/products default to range="7d" — alerts use
# the same default so "what's declining" means the same trailing
# window a dashboard viewer would already be looking at.
DEFAULT_ALERT_RANGE = "7d"


def _decline_severity(change_pct: float) -> str:
    if change_pct <= DECLINE_SEVERE_THRESHOLD_PCT:
        return "severe"
    if change_pct <= DECLINE_MODERATE_THRESHOLD_PCT:
        return "moderate"
    return "mild"


async def _get_declining_products(
    db: AsyncIOMotorDatabase, current_user: UserOut, range_: str
) -> list[dict]:
    """Every product (not capped to a top/bottom half — see module
    docstring) whose current-period revenue fell past
    DECLINE_ALERT_THRESHOLD_PCT vs. the previous period.
    """
    days = RANGE_DAYS[range_]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    brand_filter = brand_match_stage(current_user) or {}

    group_stage = {
        "$group": {
            "_id": "$product_id",
            "product_name": {"$first": "$product_name"},
            "revenue": {"$sum": "$total_value"},
        }
    }
    pipeline = [
        {"$match": {**brand_filter, "timestamp": {"$gte": previous_start}}},
        {
            "$facet": {
                "current": [{"$match": {"timestamp": {"$gte": current_start}}}, group_stage],
                "previous": [{"$match": {"timestamp": {"$lt": current_start}}}, group_stage],
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)
    facets = result[0] if result else {"current": [], "previous": []}
    previous_by_product = {r["_id"]: r["revenue"] for r in facets.get("previous", [])}

    declining = []
    for row in facets.get("current", []):
        previous_revenue = previous_by_product.get(row["_id"], 0.0)
        change_pct = compute_change_pct(row["revenue"], previous_revenue)
        if change_pct is not None and change_pct <= DECLINE_ALERT_THRESHOLD_PCT:
            declining.append(
                {"product_id": row["_id"], "product_name": row["product_name"], "change_pct": change_pct}
            )
    return declining


@router.get("/alerts", response_model=list[Alert])
async def get_alerts(
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[Alert]:
    """One ranked feed over anomalies + HIGH-risk inventory + declining
    regions/products, sorted severe-first, then most-recent-first
    within a severity tier. Brand-scoped exactly like every other
    endpoint (each underlying source call already applies
    security.brand_match_stage — a business account never sees another
    brand's alerts of any type).
    """
    # Naive-but-UTC, matching every other timestamp in this app (Motor
    # hands BSON dates back naive — see models.py's _format_utc_z) —
    # deliberate, not an oversight: anomaly alerts carry a real event
    # timestamp straight from a stored order document (naive), while
    # low_stock/decline alerts get "right now" as their timestamp
    # (nothing more specific to attach). Mixing a timezone-AWARE "now"
    # with those naive event timestamps in the sort key below would
    # have `.timestamp()` interpret the naive ones in the *local*
    # system timezone while the aware one resolves in UTC — a real,
    # silent skew in "most recent first" ordering whenever the host
    # isn't running in UTC. Keeping everything naive here means every
    # timestamp gets the same (mis)interpretation, so relative
    # ordering stays correct regardless of the host's local timezone.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alerts: list[Alert] = []

    anomalies = await get_business_anomalies(limit=50, brand=None, current_user=current_user, db=db)
    for event in anomalies:
        alerts.append(
            Alert(
                type="anomaly",
                severity=event.severity or "mild",
                message=(
                    f"Unusual order volume in {event.region} — {event.brand} "
                    f"({event.product_name}), z={event.z_score:.2f}"
                    if event.z_score is not None
                    else f"Unusual order volume in {event.region} — {event.brand}"
                ),
                timestamp=event.timestamp,
                related_entity={
                    "order_id": event.order_id,
                    "region": event.region,
                    "brand": event.brand,
                    "product_id": event.product_id,
                },
            )
        )

    risk_items = await get_inventory_risk(
        minutes=60 * 24, low_stock_threshold=20, format="json", brand=None,
        current_user=current_user, db=db,
    )
    for item in risk_items:
        if item.risk != "HIGH":
            continue
        alerts.append(
            Alert(
                type="low_stock",
                severity="severe",
                message=(
                    f"{item.product_name} in {item.region} is critically low: "
                    f"{item.current_stock} units left, recent demand {item.recent_demand}"
                ),
                timestamp=now,
                related_entity={
                    "product_id": item.product_id,
                    "region": item.region,
                    "brand": item.brand,
                },
            )
        )

    declining_regions = await get_region_stats(
        range=DEFAULT_ALERT_RANGE, brand=None, current_user=current_user, db=db
    )
    for region in declining_regions:
        change_pct = region.change_pct.revenue
        if change_pct is not None and change_pct <= DECLINE_ALERT_THRESHOLD_PCT:
            alerts.append(
                Alert(
                    type="decline",
                    severity=_decline_severity(change_pct),
                    message=(
                        f"{region.region} revenue down {abs(change_pct):.1f}% vs. the previous "
                        f"{DEFAULT_ALERT_RANGE}"
                    ),
                    timestamp=now,
                    related_entity={"region": region.region},
                )
            )

    declining_products = await _get_declining_products(db, current_user, DEFAULT_ALERT_RANGE)
    for product in declining_products:
        change_pct = product["change_pct"]
        alerts.append(
            Alert(
                type="decline",
                severity=_decline_severity(change_pct),
                message=(
                    f"{product['product_name']} revenue down {abs(change_pct):.1f}% vs. the "
                    f"previous {DEFAULT_ALERT_RANGE}"
                ),
                timestamp=now,
                related_entity={"product_id": product["product_id"]},
            )
        )

    severity_rank = {"severe": 0, "moderate": 1, "mild": 2}
    alerts.sort(key=lambda a: (severity_rank[a.severity], -a.timestamp.timestamp()))
    return alerts
