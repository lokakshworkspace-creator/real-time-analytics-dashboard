"""Shared pytest fixtures for the integration test suite.

The core guarantee these fixtures exist to provide: tests NEVER touch
the real `analytics` database configured in the repo-root `.env`, even
if it's sitting right there with real data in it. `test_db` points at
a dedicated `<real-db-name>_test` database instead, and `api_client`
wires the FastAPI app to use it via dependency_overrides — the app's
real startup/shutdown lifespan (which would connect using the real
settings) never runs in these tests at all, so there's no path by
which a test could reach the production collection.
"""

import pymongo
import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.database import get_database
from app.main import app

TEST_DB_NAME = f"{settings.mongodb_db_name}_test"


@pytest.fixture
def test_db():
    """A Motor database pointed at the dedicated test database.

    Motor's client construction is lazy (no network I/O happens until
    an actual operation is awaited), so creating it here in a plain
    sync fixture is safe — no event loop is needed until FastAPI's own
    request handling later awaits an operation on it.

    Cleanup uses a separate, plain PyMongo (sync) client to drop the
    whole test database after each test — sidesteps needing
    pytest-asyncio just for fixture teardown, and guarantees each test
    starts from a genuinely empty collection, not whatever the previous
    test happened to leave behind.
    """
    motor_client = AsyncIOMotorClient(settings.mongodb_uri)
    db = motor_client[TEST_DB_NAME]
    yield db
    motor_client.close()
    with pymongo.MongoClient(settings.mongodb_uri) as sync_client:
        sync_client.drop_database(TEST_DB_NAME)


@pytest.fixture
def api_client(test_db):
    """A TestClient wired to `test_db` via FastAPI's dependency_overrides.

    Used as `with TestClient(app) as c:` (not bare `TestClient(app)`) —
    that form keeps one consistent event loop alive for every request
    made through this client. Without it, Starlette opens a fresh event
    loop per individual call; `test_db`'s Motor client persists across
    a whole test's multiple requests and gets bound to whichever loop
    handled its first operation, so any second call in the same test
    fails with "Event loop is closed". Confirmed by hitting exactly
    that error before adding `with` here.

    Using `with` does run the app's real lifespan (connect_to_mongo +
    ensure_indexes against the *real* settings.mongodb_uri/db_name from
    .env) — but every route resolves its database via
    Depends(get_database), overridden below, so real *data* is never
    touched either way; the only side effect is idempotent index
    creation against the real `analytics` collection (harmless, and
    already exists from actually running the app). Running this suite
    does mean MongoDB must be reachable — same requirement as running
    the app itself, e.g. `docker compose up -d` from the repo root.
    """
    app.dependency_overrides[get_database] = lambda: test_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers(api_client):
    """Registers (via the bootstrap path — `test_db` is always empty at
    this point) and logs in an admin, returning ready-to-use
    Authorization headers.

    Goes through the real /api/auth/register + /api/auth/login
    endpoints rather than inserting a user document directly — the
    point of an integration suite is exercising the same path a real
    client hits, including password hashing and JWT issuance, not just
    getting a token into a test as cheaply as possible.
    """
    api_client.post(
        "/api/auth/register",
        json={"email": "admin@test.example.com", "password": "admin-test-password", "role": "admin"},
    )
    login = api_client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "admin-test-password"},
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def business_headers_factory(api_client, admin_headers):
    """Returns a factory: business_headers_factory(email, brands) ->
    Authorization headers for a fresh business account admin-registers
    on the spot, scoped to `brands` (a list — e.g. ["Nike"]).

    A factory, not a single fixed business fixture, because brand-
    scoping tests need more than one business account (at minimum, one
    to prove *does* see its own brand and one to prove *doesn't* see
    another's) with different owned_brands per test.
    """

    def _make(email: str, brands: list[str]) -> dict:
        api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={
                "email": email,
                "password": "business-test-password",
                "role": "business",
                "business_name": brands[0],
                "owned_brands": brands,
            },
        )
        login = api_client.post(
            "/api/auth/login", json={"email": email, "password": "business-test-password"}
        )
        token = login.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _make
