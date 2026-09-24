"""Inventory endpoints: POST /api/inventory/seed, GET /api/inventory,
GET /api/inventory/risk.

`inventory` is a mutable current-state doc per product+region (one
document, updated in place by every order — see routers/orders.py's
decrement-on-write), unlike `orders`, which is an append-only event log.

GET /api/inventory and GET /api/inventory/risk require a valid JWT and
brand-scope their results for role="business" accounts, same as every
GET under /api/orders/* — see security.brand_match_stage.
POST /api/inventory/seed stays open (no auth), same reasoning as
POST /api/orders — see that router's docstring.
"""

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..csv_export import csv_streaming_response
from ..database import get_database
from ..models import (
    InventoryItem,
    InventoryRiskItem,
    InventorySeedIn,
    ReorderSuggestion,
    UserOut,
    inventory_document_to_item,
)
from ..security import brand_match_stage, get_current_user

router = APIRouter(prefix="/api", tags=["inventory"])

DEFAULT_RISK_WINDOW_MINUTES = 60 * 24  # 24 hours — "recent demand" for a stock-risk read

# How many days of *projected* future demand a reorder should cover.
# 14 days is a deliberately simple, explainable stand-in for a real
# supplier lead time (how long a restock actually takes to arrive) —
# there's no real supplier integration in this app, so this is a single
# named constant standing in for "restock takes about two weeks",
# easy to justify or swap for a per-product value later.
LEAD_TIME_BUFFER_DAYS = 14

# Once a product's projected runway (days_of_stock_remaining) drops
# below this many days, a reorder is suggested "by" the date that
# crossing would happen — reordering at exactly zero days remaining
# would already be too late given any real lead time, so this gives a
# buffer before the shelf is actually empty. Deliberately the same
# order of magnitude as, but not coupled to, the inventory-risk
# low_stock_threshold query param (a stock-count threshold) — this one
# is a time threshold, answering "by when", not "how low."
REORDER_URGENCY_THRESHOLD_DAYS = 7


@router.post("/inventory/seed", response_model=InventoryItem)
async def seed_inventory(
    payload: InventorySeedIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> InventoryItem:
    """Upserts a starting stock record for one product+region.

    Upserted rather than a plain insert: the simulator calls this once
    per product+region on startup, and re-running it (a restarted
    simulator, a second demo session) should reset that record's stock
    back to the given level, not error on a duplicate key or silently
    create a second conflicting document — backed by the unique
    `product_id_1_region_1` index (see database.py).
    """
    now = datetime.now(timezone.utc)
    document = payload.model_dump()
    document["last_updated"] = now

    await db.inventory.update_one(
        {"product_id": payload.product_id, "region": payload.region},
        {"$set": document},
        upsert=True,
    )
    created = await db.inventory.find_one(
        {"product_id": payload.product_id, "region": payload.region}
    )
    return inventory_document_to_item(created)


@router.get("/inventory", response_model=list[InventoryItem])
async def get_inventory(
    brand: str | None = Query(default=None, description="Admin only — see routers/orders.py's get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[InventoryItem]:
    """Current stock list, ordered by product then region for a stable,
    scannable table — this collection is small (product x region
    combinations, not an event log), so a plain unfiltered scan sorted
    in Python-adjacent Mongo sort is cheap. The brand filter is applied
    directly to this find() rather than via an aggregation pipeline,
    for the same reason — no index dedicated to it, matching this
    collection's existing "small enough for a full scan" rationale.
    """
    query_filter: dict = brand_match_stage(current_user, brand) or {}
    cursor = db.inventory.find(query_filter).sort([("product_name", 1), ("region", 1)])
    documents = [doc async for doc in cursor]
    return [inventory_document_to_item(doc) for doc in documents]


@router.get("/inventory/risk", response_model=None)
async def get_inventory_risk(
    minutes: int = Query(
        default=DEFAULT_RISK_WINDOW_MINUTES,
        gt=0,
        description="Trailing window, in minutes, over which recent demand is measured.",
    ),
    low_stock_threshold: int = Query(
        default=20, ge=0, description="Stock at or below this level is considered low."
    ),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    brand: str | None = Query(default=None, description="Admin only — see routers/orders.py's get_orders."),
    current_user: UserOut = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Joins recent order demand per product+region against current
    stock and classifies each as HIGH/MEDIUM/LOW risk:
      - HIGH:   stock <= threshold AND recent demand >= stock
                (likely to sell out before restocking)
      - MEDIUM: stock <= threshold (low, but demand hasn't caught up yet)
      - LOW:    everything else

    Demand is aggregated in one pass across all product+region pairs
    (backed by the `product_id_1_region_1_timestamp_-1` index, or
    `brand_1_timestamp_-1` once a business account's brand condition is
    merged into the demand aggregation's $match) rather than one query
    per inventory row, then joined against the (also brand-filtered)
    inventory list in Python — the inventory collection is small enough
    that this two-query join is simpler to read than a $lookup
    pipeline, and doesn't require the demand aggregation and the stock
    list to be the same shape.

    Sorted HIGH first, then MEDIUM, then LOW; within a tier, lowest
    stock first — the items most worth a human's attention lead the
    table. See routers/orders.py's get_orders for why this route
    declares response_model=None (format=csv support).
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    brand_filter = brand_match_stage(current_user, brand) or {}

    # Days in the lookback window, as a float — drives daily_demand_rate
    # below. `minutes` is constrained gt=0 by the Query declaration
    # above, so this can't actually be 0, but the guard stays explicit
    # (rather than trusting that constraint alone) since a ZeroDivisionError
    # here would take down the whole endpoint for every caller, not just
    # a malformed one.
    window_days = minutes / (60 * 24)
    if window_days <= 0:
        window_days = 1.0

    demand_pipeline = [
        {"$match": {**brand_filter, "timestamp": {"$gte": window_start}}},
        {
            "$group": {
                "_id": {"product_id": "$product_id", "region": "$region"},
                "demand": {"$sum": "$quantity"},
            }
        },
    ]
    demand_results = await db.orders.aggregate(demand_pipeline).to_list(length=None)
    demand_by_key = {
        (r["_id"]["product_id"], r["_id"]["region"]): r["demand"] for r in demand_results
    }

    inventory_cursor = db.inventory.find(brand_filter)
    inventory_docs = [doc async for doc in inventory_cursor]

    now = datetime.now(timezone.utc)
    risk_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items: list[InventoryRiskItem] = []
    for doc in inventory_docs:
        stock = doc["current_stock"]
        demand = demand_by_key.get((doc["product_id"], doc["region"]), 0)

        if stock <= low_stock_threshold and demand >= stock:
            risk = "HIGH"
        elif stock <= low_stock_threshold:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        daily_demand_rate = demand / window_days

        days_remaining = None
        reorder_suggestion = None
        if daily_demand_rate > 0:
            days_remaining = stock / daily_demand_rate

            # Floored at 0 per the brief: never suggest a negative
            # reorder quantity for a product that's already
            # over-stocked relative to the lead-time buffer.
            projected_demand = daily_demand_rate * LEAD_TIME_BUFFER_DAYS
            suggested_quantity = math.ceil(max(0.0, projected_demand - stock))

            # "By when" — days until runway would cross the urgency
            # threshold, floored at 0 (today) rather than a past date
            # for a product that's already below it.
            days_until_urgent = max(0.0, days_remaining - REORDER_URGENCY_THRESHOLD_DAYS)
            suggested_by_date = (now + timedelta(days=days_until_urgent)).strftime("%Y-%m-%d")

            reorder_suggestion = ReorderSuggestion(
                suggested_quantity=suggested_quantity, suggested_by_date=suggested_by_date
            )

        items.append(
            InventoryRiskItem(
                product_id=doc["product_id"],
                product_name=doc["product_name"],
                brand=doc["brand"],
                region=doc["region"],
                current_stock=stock,
                recent_demand=demand,
                risk=risk,
                daily_demand_rate=daily_demand_rate,
                days_of_stock_remaining=days_remaining,
                reorder_suggestion=reorder_suggestion,
            )
        )

    items.sort(key=lambda item: (risk_rank[item.risk], item.current_stock))

    if format == "csv":
        # reorder_suggestion is a nested object — flattened into two
        # plain columns here rather than left for csv.DictWriter to
        # stringify a dict into one cell (see csv_export.py).
        rows = []
        for item in items:
            row = item.model_dump()
            suggestion = row.pop("reorder_suggestion")
            row["reorder_suggested_quantity"] = suggestion["suggested_quantity"] if suggestion else ""
            row["reorder_suggested_by_date"] = suggestion["suggested_by_date"] if suggestion else ""
            rows.append(row)
        return csv_streaming_response(
            rows,
            fieldnames=[
                "product_id", "product_name", "brand", "region", "current_stock", "recent_demand",
                "risk", "daily_demand_rate", "days_of_stock_remaining",
                "reorder_suggested_quantity", "reorder_suggested_by_date",
            ],
            filename="inventory_risk.csv",
        )
    return items
