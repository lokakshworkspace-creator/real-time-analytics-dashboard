"""Order ingest and read endpoints: POST /api/orders, GET /api/orders,
GET /api/orders/kpis, GET /api/orders/regions, GET /api/orders/products,
GET /api/orders/trend, and GET /api/anomalies/business.

Detection runs ON WRITE in the POST handler — see
detectors/zscore.py's score_order_volume for the guarded region-hour
rolling-window implementation and the reasoning behind that trade-off.

Every GET endpoint requires a valid JWT (Depends(get_current_user)) and
brand-scopes its query via security.brand_match_stage: a business
account always sees only its own owned_brands; an admin sees everything
by default, or can pass `?brand=X` to view as if scoped to one brand
(powers the frontend's admin brand-switcher). POST /api/orders and
POST /api/inventory/seed deliberately stay open (no auth) — see their
own docstrings for why.
"""

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from ..csv_export import csv_streaming_response
from ..database import get_database
from ..detectors import zscore
from ..models import (
    BrandBenchmark,
    BrandBenchmarkResponse,
    BusinessAnomalyEvent,
    ForecastPoint,
    ForecastResponse,
    KpiChangePct,
    KpiValues,
    OrderIn,
    OrderKpis,
    OrderOut,
    ProductChangePct,
    ProductPeriodValues,
    ProductStats,
    ProductStatsResponse,
    RegionChangePct,
    RegionPeriodValues,
    RegionStats,
    TrendPoint,
    UserOut,
    order_document_to_anomaly,
    order_document_to_out,
)
from ..security import brand_match_stage, get_current_user, require_admin

router = APIRouter(prefix="/api", tags=["orders"])

# --- range/granularity vocabulary shared by /orders/trend and every
# current/previous/change_pct endpoint (kpis, regions, products) ---------

RANGE_DAYS = {"7d": 7, "30d": 30, "1y": 365}


def compute_change_pct(current_val: float, previous_val: float) -> float | None:
    """Percent change, current vs. previous — the one piece of math
    shared by GET /api/orders/kpis, /api/orders/regions, and
    /api/orders/products, extracted here instead of reimplemented as a
    local closure in each (it started as one, inside get_order_kpis,
    before /regions and /products needed the identical logic).

    Returns None (not 0, not infinity) when the previous period had
    nothing to compare against — "up from zero" has no defined
    percentage. See KpiChangePct/RegionChangePct/ProductChangePct's
    docstrings for why every caller treats None as "New" rather than a
    0% change.
    """
    if previous_val == 0:
        return None
    return ((current_val - previous_val) / previous_val) * 100


# Which granularities make sense for each range, and which one a caller
# gets if they don't specify one. Rejecting combos outside this table
# (e.g. 7d+month, which would produce 0-1 buckets from 7 days of data)
# with a 422 is cheaper to reason about than silently returning a
# near-empty or absurdly large chart.
ALLOWED_GRANULARITIES: dict[str, set[str]] = {
    "7d": {"day", "week"},
    "30d": {"day", "week"},
    "1y": {"week", "month"},
}
DEFAULT_GRANULARITY: dict[str, str] = {"7d": "day", "30d": "day", "1y": "month"}


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> OrderOut:
    """Deliberately unauthenticated: the simulator (and any real order
    producer standing in for a checkout system) is a different kind of
    client than a dashboard viewer, with no natural "logged-in user" of
    its own — the brief left this endpoint's auth status a judgment
    call, and requiring a token here would mean either hardcoding admin
    credentials into the simulator or inventing a machine-account
    concept this app doesn't otherwise have, for no real security
    benefit at this app's scale. Every read is still scoped by role; a
    business account can create orders for any brand but can only ever
    *see* its own — the same trust boundary a public order-intake API
    with server-side validation would have regardless of who's logged
    into the dashboard.
    """
    document = payload.model_dump()
    if document["timestamp"] is None:
        document["timestamp"] = datetime.now(timezone.utc)
    document["total_value"] = document["quantity"] * document["unit_price"]

    # Score against this region's existing hourly-bucket history BEFORE
    # inserting — the rolling window must never include the order
    # currently being scored.
    result = await zscore.score_order_volume(
        db, region=document["region"], timestamp=document["timestamp"]
    )
    document["anomaly"] = result.is_anomaly
    # Persisted (not just used transiently) so the exact number behind
    # every flag is inspectable later via GET /api/anomalies/business.
    document["z_score"] = result.z_score
    document["severity"] = result.severity

    insert_result = await db.orders.insert_one(document)

    # Decrement matching inventory, floor-clamped at 0 via an
    # aggregation-pipeline update ($max against the subtraction result,
    # both evaluated atomically in one call) — a plain $inc would let
    # current_stock go negative under sustained demand, which happened
    # in local testing. We still accept the order either way (this app
    # doesn't block sales on stock level, a deliberate scope decision,
    # not an oversight) — a sale past available stock is a stockout to
    # flag, not a reason to reject the order.
    #
    # Not upserted here — inventory is expected to be seeded ahead of
    # time (POST /api/inventory/seed) — so a missing product+region
    # record is logged rather than silently fabricated (an order should
    # still be recorded even if inventory tracking wasn't set up for
    # it) or allowed to error the whole request (inventory is a
    # supporting signal, not a hard dependency of "did this order
    # happen"). find_one_and_update with return_document=BEFORE gives us
    # the pre-decrement stock level atomically alongside the clamped
    # write, so "would this have gone negative" can be answered from a
    # single round trip rather than a separate read that could race
    # against another order for the same product+region.
    quantity = document["quantity"]
    previous_inventory = await db.inventory.find_one_and_update(
        {"product_id": document["product_id"], "region": document["region"]},
        [
            {
                "$set": {
                    "current_stock": {"$max": [{"$subtract": ["$current_stock", quantity]}, 0]},
                    "last_updated": document["timestamp"],
                }
            }
        ],
        return_document=ReturnDocument.BEFORE,
    )
    if previous_inventory is None:
        print(
            f"WARNING: no inventory record for product_id={document['product_id']!r} "
            f"region={document['region']!r} — order was recorded, stock was not decremented."
        )
    elif previous_inventory["current_stock"] < quantity:
        print(
            f"WARNING: stockout — product_id={document['product_id']!r} "
            f"region={document['region']!r} had {previous_inventory['current_stock']} in stock, "
            f"order requested {quantity}. Order was recorded; stock clamped at 0, not negative."
        )

    created = await db.orders.find_one({"_id": insert_result.inserted_id})
    return order_document_to_out(created)


@router.get("/orders", response_model=None)
async def get_orders(
    limit: int = Query(default=50, gt=0, le=500, description="Max orders to return."),
    skip: int = Query(default=0, ge=0, description="Number of most-recent orders to skip."),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    brand: str | None = Query(
        default=None,
        description="Admin only: scope to one brand (ignored for role='business', which is "
        "always scoped to its own owned_brands regardless of this param).",
    ),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Recent orders, newest first, paginated via skip/limit.

    `response_model=None`: this route sometimes returns a StreamingResponse
    (format=csv) instead of a JSON body. FastAPI only applies response_model
    validation/serialization to a plain returned value — a Response
    subclass returned directly is sent as-is — but declaring a
    response_model here anyway would make OpenAPI advertise a JSON
    schema for the csv branch too, which is misleading. list[OrderOut]
    is still enforced by hand in the json branch via order_document_to_out.
    """
    query_filter: dict = brand_match_stage(current_user, brand) or {}
    cursor = db.orders.find(query_filter).sort("timestamp", -1).skip(skip).limit(limit)
    documents = [doc async for doc in cursor]
    orders = [order_document_to_out(doc) for doc in documents]

    if format == "csv":
        return csv_streaming_response(
            [o.model_dump() for o in orders],
            fieldnames=[
                "id", "order_id", "timestamp", "product_id", "product_name", "brand",
                "category", "quantity", "unit_price", "total_value", "region",
                "payment_status", "anomaly",
            ],
            filename="orders.csv",
        )
    return orders


@router.get("/orders/kpis", response_model=OrderKpis)
async def get_order_kpis(
    range: str = Query(default="7d", pattern="^(7d|30d|1y)$", description="7d, 30d, or 1y."),
    brand: str | None = Query(default=None, description="Admin only — see get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> OrderKpis:
    """Headline KPI numbers for the current `range`, PLUS the same
    numbers for the immediately preceding period of equal length, PLUS
    the percent change between them — computed via one aggregation
    call using $facet to branch into "current" and "previous" without
    two round trips (see models.py's OrderKpis docstring).

    Both periods always return zeroed-out numbers (not a 404) when they
    have no orders — see OrderKpis's docstring for why.
    """
    days = RANGE_DAYS[range]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)

    brand_filter = brand_match_stage(current_user, brand) or {}
    group_stage = {
        "$group": {
            "_id": None,
            "total_orders": {"$sum": 1},
            "revenue": {"$sum": "$total_value"},
            "units_sold": {"$sum": "$quantity"},
            "avg_order_value": {"$avg": "$total_value"},
        }
    }

    pipeline = [
        # Brand scope + the widest possible time bound (previous_start
        # onward) as ONE match stage — the first, and only, pre-$facet
        # stage, exactly what the brand_1_timestamp_-1 index (see
        # database.py) is built to serve in a single index scan. Each
        # $facet branch below narrows further with its own $match.
        {"$match": {**brand_filter, "timestamp": {"$gte": previous_start}}},
        {
            "$facet": {
                "current": [{"$match": {"timestamp": {"$gte": current_start}}}, group_stage],
                "previous": [
                    {"$match": {"timestamp": {"$lt": current_start}}},
                    group_stage,
                ],
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)
    facets = result[0] if result else {"current": [], "previous": []}

    def _values(rows: list[dict]) -> KpiValues:
        if not rows:
            return KpiValues(total_orders=0, revenue=0.0, units_sold=0, avg_order_value=0.0)
        row = rows[0]
        return KpiValues(
            total_orders=row["total_orders"],
            revenue=row["revenue"],
            units_sold=row["units_sold"],
            avg_order_value=row["avg_order_value"],
        )

    current = _values(facets.get("current", []))
    previous = _values(facets.get("previous", []))

    change_pct = KpiChangePct(
        total_orders=compute_change_pct(current.total_orders, previous.total_orders),
        revenue=compute_change_pct(current.revenue, previous.revenue),
        units_sold=compute_change_pct(current.units_sold, previous.units_sold),
        avg_order_value=compute_change_pct(current.avg_order_value, previous.avg_order_value),
    )

    return OrderKpis(range=range, current=current, previous=previous, change_pct=change_pct)


@router.get("/orders/regions", response_model=list[RegionStats])
async def get_region_stats(
    range: str = Query(default="7d", pattern="^(7d|30d|1y)$", description="7d, 30d, or 1y."),
    brand: str | None = Query(default=None, description="Admin only — see get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[RegionStats]:
    """Orders + revenue per region for the current `range` vs. the
    immediately preceding period of equal length, sorted by current
    revenue descending — same $facet current/previous pattern as
    GET /api/orders/kpis (see compute_change_pct), just grouped by
    region instead of collapsed into one overall total. Backed by the
    `region_1_timestamp_-1` index for the initial $match (or
    `brand_1_timestamp_-1` once a brand condition is merged in — see
    security.brand_match_stage).

    A region with orders in the previous period but none in the current
    one doesn't appear at all — this lists the current period's
    regions, with prior-period context, not a merged union of every
    region either period ever saw.
    """
    days = RANGE_DAYS[range]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    brand_filter = brand_match_stage(current_user, brand) or {}

    group_stage = {
        "$group": {"_id": "$region", "orders": {"$sum": 1}, "revenue": {"$sum": "$total_value"}}
    }
    pipeline = [
        {"$match": {**brand_filter, "timestamp": {"$gte": previous_start}}},
        {
            "$facet": {
                "current": [
                    {"$match": {"timestamp": {"$gte": current_start}}},
                    group_stage,
                    {"$sort": {"revenue": -1}},
                ],
                "previous": [{"$match": {"timestamp": {"$lt": current_start}}}, group_stage],
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)
    facets = result[0] if result else {"current": [], "previous": []}
    previous_by_region = {r["_id"]: r for r in facets.get("previous", [])}

    stats = []
    for row in facets.get("current", []):
        previous_row = previous_by_region.get(row["_id"])
        current_values = RegionPeriodValues(orders=row["orders"], revenue=row["revenue"])
        previous_values = RegionPeriodValues(
            orders=previous_row["orders"] if previous_row else 0,
            revenue=previous_row["revenue"] if previous_row else 0.0,
        )
        stats.append(
            RegionStats(
                region=row["_id"],
                current=current_values,
                previous=previous_values,
                change_pct=RegionChangePct(
                    orders=compute_change_pct(current_values.orders, previous_values.orders),
                    revenue=compute_change_pct(current_values.revenue, previous_values.revenue),
                ),
            )
        )
    return stats


@router.get("/orders/products", response_model=None)
async def get_product_stats(
    range: str = Query(default="7d", pattern="^(7d|30d|1y)$", description="7d, 30d, or 1y."),
    limit: int = Query(default=10, gt=0, le=100, description="Max products to return."),
    order: str = Query(
        default="top",
        pattern="^(top|bottom)$",
        description="'top' = highest current-period revenue first, 'bottom' = lowest first.",
    ),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    brand: str | None = Query(default=None, description="Admin only — see get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Units sold + revenue per product for the current `range` vs. the
    immediately preceding period, plus a change_pct — same $facet
    current/previous pattern as GET /api/orders/regions/kpis (see
    compute_change_pct). `order=top` surfaces best-sellers by CURRENT
    period revenue, `order=bottom` surfaces slow-moving products.

    Fetches the FULL sorted current-period product list once (no
    `$limit` in the pipeline), then slices in Python — not two
    independent `$sort` + `$limit` queries — specifically so
    `order=top` and `order=bottom` can never return overlapping
    product_ids even when called with the same `limit` against a small
    catalog (this app's simulator only ever has 5-6 SKUs). Two
    independent top-N/bottom-N queries against a 5-product catalog with
    limit=5 each would both just return all 5 products, merely
    reordered — a real bug, confirmed and fixed here, not a
    hypothetical.

    Each direction is capped to its own non-overlapping half of the
    sorted list — `ceil(total/2)` for top, `floor(total/2)` for bottom
    (top gets the extra product on an odd-sized catalog, an arbitrary
    but consistent tiebreak) — so a single call never needs to know
    what limit its "sibling" top/bottom call used; both are safe by
    construction as long as each stays within its own half. `note` is
    populated only when this capping actually reduced what the caller
    asked for, explaining why the response is shorter than `limit`
    rather than leaving that to look like a bug. See get_orders for why
    this route declares response_model=None (format=csv support).
    """
    days = RANGE_DAYS[range]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    brand_filter = brand_match_stage(current_user, brand) or {}

    group_stage = {
        "$group": {
            "_id": "$product_id",
            "product_name": {"$first": "$product_name"},
            "brand": {"$first": "$brand"},
            "units_sold": {"$sum": "$quantity"},
            "revenue": {"$sum": "$total_value"},
        }
    }
    pipeline = [
        {"$match": {**brand_filter, "timestamp": {"$gte": previous_start}}},
        {
            "$facet": {
                "current": [
                    {"$match": {"timestamp": {"$gte": current_start}}},
                    group_stage,
                    {"$sort": {"revenue": -1}},  # always DESC — direction applied by slicing below
                ],
                "previous": [{"$match": {"timestamp": {"$lt": current_start}}}, group_stage],
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)
    facets = result[0] if result else {"current": [], "previous": []}
    all_results = facets.get("current", [])
    previous_by_product = {r["_id"]: r for r in facets.get("previous", [])}
    total = len(all_results)

    top_half = math.ceil(total / 2)
    bottom_half = total // 2
    max_for_direction = top_half if order == "top" else bottom_half
    effective_limit = min(limit, max_for_direction)

    note = None
    if effective_limit < limit:
        note = (
            f"Catalog has only {total} distinct product(s) in this window — top/bottom "
            f"capped to non-overlapping halves ({top_half}/{bottom_half}) instead of the "
            f"requested {limit}, so the two lists never share a product."
        )

    if order == "top":
        selected = all_results[:effective_limit]
    else:
        # all_results is sorted DESC; the bottom performers are its tail,
        # reversed back to ascending (worst first) to match this
        # endpoint's existing order=bottom contract.
        selected = list(reversed(all_results[total - effective_limit :])) if effective_limit else []

    products = []
    for r in selected:
        previous_row = previous_by_product.get(r["_id"])
        current_values = ProductPeriodValues(units_sold=r["units_sold"], revenue=r["revenue"])
        previous_values = ProductPeriodValues(
            units_sold=previous_row["units_sold"] if previous_row else 0,
            revenue=previous_row["revenue"] if previous_row else 0.0,
        )
        products.append(
            ProductStats(
                product_id=r["_id"],
                product_name=r["product_name"],
                brand=r["brand"],
                current=current_values,
                previous=previous_values,
                change_pct=ProductChangePct(
                    units_sold=compute_change_pct(current_values.units_sold, previous_values.units_sold),
                    revenue=compute_change_pct(current_values.revenue, previous_values.revenue),
                ),
            )
        )

    if format == "csv":
        rows = []
        for p in products:
            rows.append(
                {
                    "product_id": p.product_id,
                    "product_name": p.product_name,
                    "brand": p.brand,
                    "current_units_sold": p.current.units_sold,
                    "current_revenue": p.current.revenue,
                    "previous_units_sold": p.previous.units_sold,
                    "previous_revenue": p.previous.revenue,
                    "change_pct_units_sold": p.change_pct.units_sold,
                    "change_pct_revenue": p.change_pct.revenue,
                }
            )
        return csv_streaming_response(
            rows,
            fieldnames=[
                "product_id", "product_name", "brand", "current_units_sold", "current_revenue",
                "previous_units_sold", "previous_revenue", "change_pct_units_sold", "change_pct_revenue",
            ],
            filename="product_performance.csv",
        )
    return ProductStatsResponse(note=note, products=products)


@router.get("/orders/trend", response_model=list[TrendPoint])
async def get_order_trend(
    range: str = Query(default="7d", pattern="^(7d|30d|1y)$", description="7d, 30d, or 1y."),
    granularity: str | None = Query(
        default=None,
        pattern="^(day|week|month)$",
        description="day, week, or month. Defaults per range (7d/30d -> day, 1y -> month); "
        "not every combination is allowed — see ALLOWED_GRANULARITIES.",
    ),
    brand: str | None = Query(default=None, description="Admin only — see get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[TrendPoint]:
    """Orders/revenue/units_sold bucketed by day/week/month over `range`,
    oldest first — powers the frontend's trend chart.

    granularity=None picks DEFAULT_GRANULARITY[range]; an explicit
    granularity outside ALLOWED_GRANULARITIES[range] (e.g. 7d+month) is
    a 422, not a silently-empty or single-bucket chart.
    """
    resolved_granularity = granularity or DEFAULT_GRANULARITY[range]
    if resolved_granularity not in ALLOWED_GRANULARITIES[range]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"granularity={resolved_granularity!r} doesn't make sense for range={range!r}. "
            f"Allowed: {sorted(ALLOWED_GRANULARITIES[range])}.",
        )

    window_start = datetime.now(timezone.utc) - timedelta(days=RANGE_DAYS[range])
    brand_filter = brand_match_stage(current_user, brand) or {}

    pipeline = [
        {"$match": {**brand_filter, "timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": {"$dateTrunc": {"date": "$timestamp", "unit": resolved_granularity}},
                "orders": {"$sum": 1},
                "revenue": {"$sum": "$total_value"},
                "units_sold": {"$sum": "$quantity"},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    results = await db.orders.aggregate(pipeline).to_list(length=None)
    return [
        TrendPoint(
            period=r["_id"].strftime("%Y-%m-%d"),
            orders=r["orders"],
            revenue=r["revenue"],
            units_sold=r["units_sold"],
        )
        for r in results
    ]


# --- /orders/forecast ---------------------------------------------------

# How many trailing days of daily order-count history feed the
# forecast — "4-6 weeks" per the brief; 42 days (6 weeks) gives the
# regression more points to fit without reaching so far back that a
# product's demand pattern from over a month ago still dominates the
# projected trend.
FORECAST_HISTORY_DAYS = 42

# Below this many DISTINCT days with at least one real order, there
# isn't enough signal to fit a meaningful trend line — two points is
# the mathematical minimum for a line, but the real reason this is a
# named threshold (not just "the regression works or it doesn't") is
# to make the fallback an honest, explicit product/policy decision
# rather than whatever a degenerate regression happens to compute.
MIN_DAYS_FOR_TREND = 2


def _day_start(dt: datetime) -> datetime:
    """Floors `dt` to the start of its UTC day, as a *naive* datetime —
    same reasoning as detectors/zscore.py's _hour_start: MongoDB's
    $dateTrunc returns naive-but-UTC datetimes, so this function's
    output must be naive too to compare equal as a dict key. Getting
    this wrong (an aware key against $dateTrunc's naive output) was a
    real bug caught in this exact codebase before — see zscore.py's
    _hour_start docstring.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _fit_linear_trend(y_values: list[float]) -> tuple[float, float]:
    """Ordinary least-squares fit of y = intercept + slope * x, with
    x = 0, 1, 2, ... over the given values in order. Pure Python (sums
    and a division), not a new dependency — "a basic linear regression
    over the recent daily buckets," per the brief, not anything that
    needs numpy/scikit-learn for a fit this simple.
    """
    n = len(y_values)
    x_values = range(n)
    x_mean = sum(x_values) / n
    y_mean = sum(y_values) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    slope = numerator / denominator if denominator != 0 else 0.0
    intercept = y_mean - slope * x_mean
    return intercept, slope


@router.get("/orders/forecast", response_model=ForecastResponse)
async def get_order_forecast(
    product_id: str = Query(..., min_length=1),
    horizon: int = Query(default=7, gt=0, le=30, description="Days to project forward."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> ForecastResponse:
    """Projects `product_id`'s daily orders/revenue for the next
    `horizon` days from its last FORECAST_HISTORY_DAYS of real daily
    order counts, via ordinary least-squares linear regression (see
    _fit_linear_trend) — deliberately simple and explainable, not a
    black box. `method` on the response names exactly what ran, so a
    consumer never mistakes this for something more sophisticated.

    Brand-scoped the same way as every other endpoint
    (security.brand_match_stage, no override param here — `product_id`
    already narrows to one product, and a business account's own
    owned_brands filter still applies underneath it): a business
    account forecasting a product_id outside its owned brands simply
    sees no historical orders for it, and gets back a flat
    zero-history projection — not a 403, since "no data for this
    product" and "you don't own this brand" produce the same honest
    answer either way, without this endpoint needing a separate
    ownership check to reach it.

    Below MIN_DAYS_FOR_TREND distinct days of real order history, a
    trend line isn't meaningful to fit — falls back to a flat
    projection (the single observed day's values, or zero with no
    history at all) rather than extrapolating a "trend" from
    essentially no data.
    """
    now = datetime.now(timezone.utc)
    today = _day_start(now)
    window_start = today - timedelta(days=FORECAST_HISTORY_DAYS)
    brand_filter = brand_match_stage(current_user) or {}

    pipeline = [
        {
            "$match": {
                "product_id": product_id,
                **brand_filter,
                "timestamp": {"$gte": window_start, "$lt": today},
            }
        },
        {
            "$group": {
                "_id": {"$dateTrunc": {"date": "$timestamp", "unit": "day"}},
                "orders": {"$sum": 1},
                "revenue": {"$sum": "$total_value"},
            }
        },
    ]
    results = await db.orders.aggregate(pipeline).to_list(length=FORECAST_HISTORY_DAYS)
    by_day = {r["_id"]: r for r in results}

    # Zero-filled across the FULL window (not just days with real
    # orders) — same reasoning as the anomaly detector's own hourly
    # buckets: a regression fit only on non-zero days would silently
    # ignore every quiet day and overstate the trend.
    daily_orders = [
        by_day.get(today - timedelta(days=offset), {}).get("orders", 0)
        for offset in range(FORECAST_HISTORY_DAYS, 0, -1)
    ]
    daily_revenue = [
        by_day.get(today - timedelta(days=offset), {}).get("revenue", 0.0)
        for offset in range(FORECAST_HISTORY_DAYS, 0, -1)
    ]

    if len(results) < MIN_DAYS_FOR_TREND:
        if len(results) == 0:
            flat_orders, flat_revenue = 0.0, 0.0
            method = "insufficient_history_zero_projection"
        else:
            only_day = results[0]
            flat_orders, flat_revenue = float(only_day["orders"]), float(only_day["revenue"])
            method = "insufficient_history_flat_projection"

        forecast = [
            ForecastPoint(
                date=(today + timedelta(days=i + 1)).strftime("%Y-%m-%d"),
                projected_orders=round(flat_orders, 2),
                projected_revenue=round(flat_revenue, 2),
            )
            for i in range(horizon)
        ]
    else:
        orders_intercept, orders_slope = _fit_linear_trend(daily_orders)
        revenue_intercept, revenue_slope = _fit_linear_trend(daily_revenue)
        method = f"linear_regression_last_{FORECAST_HISTORY_DAYS}_days"

        forecast = []
        for i in range(horizon):
            x = FORECAST_HISTORY_DAYS + i  # continues the fitted line's x-axis
            # Floored at 0 — a downward trend projected far enough
            # forward would otherwise go negative, and negative orders
            # aren't a meaningful projection.
            projected_orders = max(0.0, orders_intercept + orders_slope * x)
            projected_revenue = max(0.0, revenue_intercept + revenue_slope * x)
            forecast.append(
                ForecastPoint(
                    date=(today + timedelta(days=i + 1)).strftime("%Y-%m-%d"),
                    projected_orders=round(projected_orders, 2),
                    projected_revenue=round(projected_revenue, 2),
                )
            )

    return ForecastResponse(product_id=product_id, method=method, forecast=forecast)


@router.get("/brands", response_model=list[str])
async def get_brands(
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[str]:
    """Distinct brands with at least one order ever, sorted — powers the
    frontend's admin brand-switcher (there's no other way for it to
    know what brands exist to offer). A business account gets back
    only its own owned_brands, without a database round trip — it
    already knows its own scope and has no switcher to populate.
    """
    if current_user.role == "business":
        return sorted(current_user.owned_brands)
    return sorted(await db.orders.distinct("brand"))


@router.get("/brands/benchmark", response_model=BrandBenchmarkResponse)
async def get_brands_benchmark(
    range: str = Query(default="30d", pattern="^(7d|30d|1y)$", description="7d, 30d, or 1y."),
    current_user: UserOut = Depends(require_admin),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> BrandBenchmarkResponse:
    """Admin-only (Depends(require_admin), not just get_current_user —
    see security.py): every brand's orders/revenue/avg_order_value for
    `range`, and how each brand's revenue compares to the plain average
    across every brand in the same window. Never brand-scoped by
    `brand_match_stage` — there's no `?brand=` param and no business-
    account path through this endpoint at all, by design: a business
    account seeing even an aggregate "you're 23% below average" figure
    would leak that competitor brands exist and roughly how they're
    doing, which is exactly the cross-brand visibility role-scoping
    exists to prevent everywhere else in this app.
    """
    days = RANGE_DAYS[range]
    window_start = datetime.now(timezone.utc) - timedelta(days=days)

    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": "$brand",
                "orders": {"$sum": 1},
                "revenue": {"$sum": "$total_value"},
                "avg_order_value": {"$avg": "$total_value"},
            }
        },
        {"$sort": {"revenue": -1}},
    ]
    results = await db.orders.aggregate(pipeline).to_list(length=None)

    average_revenue = sum(r["revenue"] for r in results) / len(results) if results else 0.0

    benchmarks = [
        BrandBenchmark(
            brand=r["_id"],
            orders=r["orders"],
            revenue=r["revenue"],
            avg_order_value=r["avg_order_value"],
            revenue_vs_average_pct=compute_change_pct(r["revenue"], average_revenue),
        )
        for r in results
    ]
    return BrandBenchmarkResponse(range=range, average_revenue=average_revenue, benchmarks=benchmarks)


@router.get("/anomalies/business", response_model=list[BusinessAnomalyEvent])
async def get_business_anomalies(
    limit: int = Query(default=50, gt=0, le=500, description="Max events to return."),
    brand: str | None = Query(default=None, description="Admin only — see get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[BusinessAnomalyEvent]:
    """Flagged orders, newest first — the region-hour order-volume
    detector's own record of what it flagged and why (the `z_score`/
    `severity` fields are what actually triggered it, and how bad it
    was — see detectors/zscore.py). {anomaly: True} + sort by timestamp
    is exactly what the `anomaly_1_timestamp_-1` index (see database.py)
    is built for.
    """
    query_filter: dict = {"anomaly": True, **(brand_match_stage(current_user, brand) or {})}
    cursor = db.orders.find(query_filter).sort("timestamp", -1).limit(limit)
    documents = [doc async for doc in cursor]
    return [order_document_to_anomaly(doc) for doc in documents]
