"""Motor (async MongoDB driver) connection lifecycle.

The client is created once at app startup and reused for the process
lifetime (Motor's client is designed to be shared, not reopened per
request — it manages its own connection pool). Connect/close are wired
into FastAPI's lifespan context in main.py rather than opened lazily on
first request, so a bad MONGODB_URI fails fast at startup instead of on
the first user-facing request.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import settings


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


def get_database() -> AsyncIOMotorDatabase:
    """FastAPI dependency: yields the shared database handle.

    Raises if called before connect_to_mongo() has run (i.e. outside the
    app's lifespan), which should only happen in a misconfigured test.
    """
    if _connection.db is None:
        raise RuntimeError("Database not initialized — connect_to_mongo() has not run yet.")
    return _connection.db
