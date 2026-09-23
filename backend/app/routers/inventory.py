"""Inventory endpoints: POST /api/inventory/seed, GET /api/inventory,
GET /api/inventory/risk.

`inventory` is a mutable current-state doc per product+region (one
document, updated in place by every order — see routers/orders.py's
decrement-on-write), unlike `orders`, which is an append-only event log.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..models import InventoryItem, InventoryRiskItem, InventorySeedIn, inventory_document_to_item

router = APIRouter(prefix="/api", tags=["inventory"])

DEFAULT_RISK_WINDOW_MINUTES = 60 * 24  # 24 hours — "recent demand" for a stock-risk read


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
async def get_inventory(db: AsyncIOMotorDatabase = Depends(get_database)) -> list[InventoryItem]:
    """Current stock list, ordered by product then region for a stable,
    scannable table — this collection is small (product x region
    combinations, not an event log), so a plain unfiltered scan sorted
    in Python-adjacent Mongo sort is cheap.
    """
    cursor = db.inventory.find().sort([("product_name", 1), ("region", 1)])
    documents = [doc async for doc in cursor]
    return [inventory_document_to_item(doc) for doc in documents]


@router.get("/inventory/risk", response_model=list[InventoryRiskItem])
async def get_inventory_risk(
    minutes: int = Query(
        default=DEFAULT_RISK_WINDOW_MINUTES,
        gt=0,
        description="Trailing window, in minutes, over which recent demand is measured.",
    ),
    low_stock_threshold: int = Query(
        default=20, ge=0, description="Stock at or below this level is considered low."
    ),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[InventoryRiskItem]:
    """Joins recent order demand per product+region against current
    stock and classifies each as HIGH/MEDIUM/LOW risk:
      - HIGH:   stock <= threshold AND recent demand >= stock
                (likely to sell out before restocking)
      - MEDIUM: stock <= threshold (low, but demand hasn't caught up yet)
      - LOW:    everything else

    Demand is aggregated in one pass across all product+region pairs
    (backed by the `product_id_1_region_1_timestamp_-1` index) rather
    than one query per inventory row, then joined against the inventory
    list in Python — the inventory collection is small enough that this
    two-query join is simpler to read than a $lookup pipeline, and
    doesn't require the demand aggregation and the stock list to be the
    same shape.

    Sorted HIGH first, then MEDIUM, then LOW; within a tier, lowest
    stock first — the items most worth a human's attention lead the
    table.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    demand_pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
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

    inventory_cursor = db.inventory.find()
    inventory_docs = [doc async for doc in inventory_cursor]

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

        items.append(
            InventoryRiskItem(
                product_id=doc["product_id"],
                product_name=doc["product_name"],
                region=doc["region"],
                current_stock=stock,
                recent_demand=demand,
                risk=risk,
            )
        )

    items.sort(key=lambda item: (risk_rank[item.risk], item.current_stock))
    return items
