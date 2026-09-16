"""FastAPI application entrypoint.

Run with: uvicorn app.main:app --reload (from the backend/ directory).
"""

import math
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from .config import settings
from .database import close_mongo_connection, connect_to_mongo, ensure_indexes, get_database
from .routers import analytics, metrics


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    await ensure_indexes(get_database())
    yield
    await close_mongo_connection()


app = FastAPI(title="Real-Time Data Analytics Dashboard API", lifespan=lifespan)

# An explicit allowlist (never "*") — this API is never meant to be
# called from arbitrary origins, and allow_origins must be an explicit
# list for allow_credentials=True to be valid per the CORS spec.
# settings.frontend_origins supports more than one entry (comma-
# separated in FRONTEND_ORIGIN) specifically so local dev and a
# deployed frontend can both be allowed at once — see config.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(metrics.router)
app.include_router(analytics.router)


# Phase 8 hardening: MongoDB going briefly unreachable mid-request (a
# restart, a network blip, connection-pool exhaustion) previously meant
# every route would let the raw driver exception propagate and become a
# generic, unhelpful 500. Registered once here — for every route, not
# wrapped per-endpoint — so a Mongo-layer failure anywhere in the app
# consistently comes back as a 503 the client can sensibly act on
# ("try again shortly"), instead of 5 near-duplicate try/except blocks
# scattered across routers/metrics.py and routers/analytics.py.
# PyMongoError is the base class for every pymongo/Motor exception
# (ServerSelectionTimeoutError, AutoReconnect, etc.), so this catches
# the whole family, not just one specific failure mode.
@app.exception_handler(PyMongoError)
async def handle_mongo_error(request: Request, exc: PyMongoError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please try again shortly."},
    )


def _sanitize_for_json(value):
    """Recursively replaces non-finite floats (NaN/Infinity/-Infinity)
    with their string form. Starlette's JSONResponse renders with
    allow_nan=False (correctly — those tokens aren't valid JSON), so
    any raw NaN/Infinity anywhere in a response body makes json.dumps
    raise ValueError deep inside response encoding, past the point
    anything can turn it into a clean error for the client.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    return value


# Phase 8 hardening: a body like {"value": NaN, ...} correctly fails
# MetricIn's allow_inf_nan=False validation — but FastAPI's *default*
# 422 handler echoes the rejected value back in each error's `input`
# field, and that raw NaN then hits the allow_nan=False wall above
# while building the *error response itself*, turning what should be a
# clean 422 into an opaque 500. Confirmed live via curl before this
# handler existed. Sanitizing the encoded error body fixes the
# response without changing what MetricIn accepts or rejects.
@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_sanitize_for_json(jsonable_encoder({"detail": exc.errors()})),
    )


@app.get("/health", tags=["health"], response_model=None)
async def health_check(db: AsyncIOMotorDatabase = Depends(get_database)) -> dict | JSONResponse:
    """Confirms both the API process and the MongoDB connection are alive.

    Uses Depends(get_database) like every other route (Phase 8) rather
    than calling get_database() directly in the handler body — the
    direct-call version bypassed FastAPI's dependency_overrides, which
    meant this one endpoint couldn't be pointed at a test database like
    every other route can be in backend/tests/.
    """
    try:
        await db.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "disconnected"},
        )
