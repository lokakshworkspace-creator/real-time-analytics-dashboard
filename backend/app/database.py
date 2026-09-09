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

# The two indexes CLAUDE.md specifies for the `metrics` collection:
#   - metric_1_timestamp_-1: the dominant query pattern — recent data
#     for one metric. Backs GET /metrics/latest (equality on metric,
#     sorted by timestamp) and GET /metrics/stats ($match on metric +
#     a timestamp range).
#   - timestamp_-1: queries across all metrics regardless of which one
#     — e.g. a $sort/$group scan over the whole collection.
# Explicit names (rather than letting PyMongo auto-name them) make
# re-running create_index() on every startup predictable: the same
# name always maps to the same key spec, so db.metrics.getIndexes()
# reads the same list of indexes as this constant, no matter how many
# times the app has restarted.
METRICS_INDEXES: list[tuple[list[tuple[str, int]], str]] = [
    ([("metric", 1), ("timestamp", -1)], "metric_1_timestamp_-1"),
    ([("timestamp", -1)], "timestamp_-1"),
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
    """Creates the `metrics` indexes, safe to call on every startup.

    create_index() is already idempotent in the normal case — MongoDB
    no-ops if an index with the same name *and* the same key spec
    already exists, it doesn't error or duplicate it. The explicit
    try/except below exists for the one case that isn't a silent no-op:
    if a given name ever pointed to a *different* key spec (e.g. this
    list changes in a later phase, or a stale index survived from an
    earlier version of this codebase), create_index() raises
    OperationFailure instead of just updating it in place. That should
    be loud, but it shouldn't take the whole API down at startup, so we
    log it and continue rather than letting it propagate.
    """
    for keys, name in METRICS_INDEXES:
        try:
            await db.metrics.create_index(keys, name=name)
        except OperationFailure as exc:
            print(f"WARNING: could not create index '{name}' on metrics: {exc}")


def get_database() -> AsyncIOMotorDatabase:
    """FastAPI dependency: yields the shared database handle.

    Raises if called before connect_to_mongo() has run (i.e. outside the
    app's lifespan), which should only happen in a misconfigured test.
    """
    if _connection.db is None:
        raise RuntimeError("Database not initialized — connect_to_mongo() has not run yet.")
    return _connection.db
