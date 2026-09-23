"""Order ingest and read endpoints: POST /api/orders, GET /api/orders,
GET /api/orders/kpis, GET /api/orders/regions, GET /api/orders/products,
and GET /api/anomalies/business.

Detection runs ON WRITE in the POST handler — see
detectors/zscore.py's score_order_volume for the guarded region-hour
rolling-window implementation and the reasoning behind that trade-off.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from ..database import get_database
from ..detectors import zscore
from ..models import (
    BusinessAnomalyEvent,
    OrderIn,
    OrderKpis,
    OrderOut,
    ProductStats,
    RegionStats,
    order_document_to_anomaly,
    order_document_to_out,
)

router = APIRouter(prefix="/api", tags=["orders"])

DEFAULT_KPI_WINDOW_MINUTES = 60
MAX_WINDOW_MINUTES = 60 * 24 * 365  # a year — same overflow guard as the old /metrics/stats


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> OrderOut:
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


@router.get("/orders", response_model=list[OrderOut])
async def get_orders(
    limit: int = Query(default=50, gt=0, le=500, description="Max orders to return."),
    skip: int = Query(default=0, ge=0, description="Number of most-recent orders to skip."),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[OrderOut]:
    """Recent orders, newest first, paginated via skip/limit."""
    cursor = db.orders.find().sort("timestamp", -1).skip(skip).limit(limit)
    documents = [doc async for doc in cursor]
    return [order_document_to_out(doc) for doc in documents]


@router.get("/orders/kpis", response_model=OrderKpis)
async def get_order_kpis(
    minutes: int = Query(
        default=DEFAULT_KPI_WINDOW_MINUTES,
        gt=0,
        le=MAX_WINDOW_MINUTES,
        description="Size of the trailing window, in minutes.",
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> OrderKpis:
    """Headline KPI numbers over a trailing window, computed entirely in
    MongoDB via $match -> $group, not pulled into Python.

    Returns zeroed-out numbers (200), not a 404, when the window has no
    orders — see OrderKpis's docstring for why this deliberately departs
    from the old /metrics/stats convention.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": None,
                "total_orders": {"$sum": 1},
                "revenue": {"$sum": "$total_value"},
                "units_sold": {"$sum": "$quantity"},
                "avg_order_value": {"$avg": "$total_value"},
            }
        },
    ]
    result = await db.orders.aggregate(pipeline).to_list(length=1)

    if not result:
        return OrderKpis(minutes=minutes, total_orders=0, revenue=0.0, units_sold=0, avg_order_value=0.0)

    stats = result[0]
    return OrderKpis(
        minutes=minutes,
        total_orders=stats["total_orders"],
        revenue=stats["revenue"],
        units_sold=stats["units_sold"],
        avg_order_value=stats["avg_order_value"],
    )


@router.get("/orders/regions", response_model=list[RegionStats])
async def get_region_stats(
    minutes: int = Query(
        default=DEFAULT_KPI_WINDOW_MINUTES, gt=0, le=MAX_WINDOW_MINUTES,
        description="Size of the trailing window, in minutes.",
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[RegionStats]:
    """Orders + revenue grouped by region, sorted by revenue descending —
    backed by the `region_1_timestamp_-1` index for the initial $match.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
        {"$group": {"_id": "$region", "orders": {"$sum": 1}, "revenue": {"$sum": "$total_value"}}},
        {"$sort": {"revenue": -1}},
    ]
    results = await db.orders.aggregate(pipeline).to_list(length=None)
    return [RegionStats(region=r["_id"], orders=r["orders"], revenue=r["revenue"]) for r in results]


@router.get("/orders/products", response_model=list[ProductStats])
async def get_product_stats(
    minutes: int = Query(
        default=DEFAULT_KPI_WINDOW_MINUTES, gt=0, le=MAX_WINDOW_MINUTES,
        description="Size of the trailing window, in minutes.",
    ),
    limit: int = Query(default=10, gt=0, le=100, description="Max products to return."),
    order: str = Query(
        default="top",
        pattern="^(top|bottom)$",
        description="'top' = highest revenue first, 'bottom' = lowest revenue first.",
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[ProductStats]:
    """Units sold + revenue grouped by product. `order=top` surfaces
    best-sellers, `order=bottom` surfaces slow-moving products — the
    same aggregation, sorted in whichever direction the caller asked
    for, rather than two near-duplicate endpoints.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    sort_direction = -1 if order == "top" else 1

    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": "$product_id",
                "product_name": {"$first": "$product_name"},
                "units_sold": {"$sum": "$quantity"},
                "revenue": {"$sum": "$total_value"},
            }
        },
        {"$sort": {"revenue": sort_direction}},
        {"$limit": limit},
    ]
    results = await db.orders.aggregate(pipeline).to_list(length=limit)
    return [
        ProductStats(
            product_id=r["_id"],
            product_name=r["product_name"],
            units_sold=r["units_sold"],
            revenue=r["revenue"],
        )
        for r in results
    ]


@router.get("/anomalies/business", response_model=list[BusinessAnomalyEvent])
async def get_business_anomalies(
    limit: int = Query(default=50, gt=0, le=500, description="Max events to return."),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[BusinessAnomalyEvent]:
    """Flagged orders, newest first — the region-hour order-volume
    detector's own record of what it flagged and why (the `z_score`
    field on each result is what actually triggered it). {anomaly: True}
    + sort by timestamp is exactly what the `anomaly_1_timestamp_-1`
    index (see database.py) is built for.
    """
    cursor = db.orders.find({"anomaly": True}).sort("timestamp", -1).limit(limit)
    documents = [doc async for doc in cursor]
    return [order_document_to_anomaly(doc) for doc in documents]
