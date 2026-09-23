"""Motor (async MongoDB driver) connection lifecycle.

The client is created once at app startup and reused for the process
lifetime (Motor's client is designed to be shared, not reopened per
request — it manages its own connection pool). Connect/close are wired
into FastAPI's lifespan context in main.py rather than opened lazily on
first request, so a bad MONGODB_URI fails fast at startup instead of on
the first user-facing request.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from .config import settings

# Indexes on the `orders` collection (an append-only event log):
#   - region_1_timestamp_-1: backs GET /api/orders/regions ($match on a
#     region-scoped window isn't needed today, but the region-hour
#     order-volume detector in detectors/zscore.py runs this exact
#     shape — {region, timestamp range} — on every single POST
#     /api/orders, so it's the hottest, most frequent query in the app.
#   - anomaly_1_timestamp_-1: backs GET /api/anomalies/business —
#     equality on anomaly=True, sorted by timestamp, same shape as the
#     old metrics anomaly index.
#   - product_id_1_region_1_timestamp_-1: backs GET /api/inventory/risk's
#     recent-demand aggregation ({product_id, region} equality + a
#     timestamp range) and is a superset of the plain
#     {product_id, region} shape the inventory decrement lookup uses.
# Indexes on the `inventory` collection (a mutable current-state doc per
# product+region, not an event log):
#   - product_id_1_region_1 (unique): one document per product+region.
#     Backs the seed upsert, the per-order stock decrement, and the
#     inventory-risk lookup. Unique so a seed re-run or a race between
#     two orders for the same product+region can never fork into two
#     stock records that silently disagree with each other.
# Explicit names (rather than letting PyMongo auto-name them) make
# re-running create_index() on every startup predictable: the same name
# always maps to the same key spec.
ORDERS_INDEXES: list[tuple[list[tuple[str, int]], str, dict]] = [
    ([("region", 1), ("timestamp", -1)], "region_1_timestamp_-1", {}),
    ([("anomaly", 1), ("timestamp", -1)], "anomaly_1_timestamp_-1", {}),
    (
        [("product_id", 1), ("region", 1), ("timestamp", -1)],
        "product_id_1_region_1_timestamp_-1",
        {},
    ),
]

INVENTORY_INDEXES: list[tuple[list[tuple[str, int]], str, dict]] = [
    ([("product_id", 1), ("region", 1)], "product_id_1_region_1", {"unique": True}),
]


class _MongoConnection:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


_connection = _MongoConnection()


async def connect_to_mongo() -> None:
    _connection.client = AsyncIOMotorClient(settings.mongodb_uri)
    _connection.db = _connection.client[settings.mongodb_db_name]


async def close_mongo_connection() -> None:
    if _connection.client is not None:
        _connection.client.close()


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Creates the `orders` and `inventory` indexes, safe to call on
    every startup.

    create_index() is already idempotent in the normal case — MongoDB
    no-ops if an index with the same name *and* the same key spec/
    options already exists. The explicit try/except below exists for
    the one case that isn't a silent no-op: if a given name ever pointed
    to a *different* spec (e.g. this list changes in a later revision,
    or a stale index survived from an earlier version of this codebase),
    create_index() raises OperationFailure instead of updating it in
    place. That should be loud, but it shouldn't take the whole API down
    at startup, so we log it and continue rather than letting it
    propagate.
    """
    for keys, name, options in ORDERS_INDEXES:
        try:
            await db.orders.create_index(keys, name=name, **options)
        except OperationFailure as exc:
            print(f"WARNING: could not create index '{name}' on orders: {exc}")

    for keys, name, options in INVENTORY_INDEXES:
        try:
            await db.inventory.create_index(keys, name=name, **options)
        except OperationFailure as exc:
            print(f"WARNING: could not create index '{name}' on inventory: {exc}")


def get_database() -> AsyncIOMotorDatabase:
    """FastAPI dependency: yields the shared database handle.

    Raises if called before connect_to_mongo() has run (i.e. outside the
    app's lifespan), which should only happen in a misconfigured test.
    """
    if _connection.db is None:
        raise RuntimeError("Database not initialized — connect_to_mongo() has not run yet.")
    return _connection.db
