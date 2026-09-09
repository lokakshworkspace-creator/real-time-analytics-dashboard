"""FastAPI application entrypoint.

Run with: uvicorn app.main:app --reload (from the backend/ directory).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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

# Single allowed origin (the Vite dev server) rather than "*": this API
# is never meant to be called from arbitrary origins, and allow_origins
# must be an explicit list (not "*") for allow_credentials=True to be
# valid per the CORS spec.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(metrics.router)
app.include_router(analytics.router)


@app.get("/health", tags=["health"], response_model=None)
async def health_check() -> dict | JSONResponse:
    """Confirms both the API process and the MongoDB connection are alive."""
    db = get_database()
    try:
        await db.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "disconnected"},
        )
