"""Password hashing, JWT issuance/verification, and the auth dependencies
every scoped route depends on.

`get_current_user` re-fetches the user document from MongoDB by the id
in the JWT's `sub` claim on every call, rather than trusting the role/
owned_brands the token was minted with. One extra DB read per
authenticated request is negligible at this app's scale, and in
exchange an admin revoking a business account's brand access (or
changing its role) takes effect on that account's very next request,
not whenever its existing tokens happen to expire — a real correctness
property for an authorization-critical field, not just a style choice.

Password hashing uses the `bcrypt` package directly, not passlib —
passlib is unmaintained (last release 2020) and its own internal
self-test raises against bcrypt>=4.1's stricter 72-byte enforcement,
confirmed directly (a 500 on the first real register call during manual
verification). bcrypt's actual 72-byte limit is enforced explicitly at
the Pydantic layer instead (UserIn.password's max_length, models.py) —
rejecting an over-length password at the API boundary with a clear 422
is more honest than silently truncating it, which would mean two
different long passwords silently hash identically.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from bson.errors import InvalidId

from .config import settings
from .database import get_database
from .models import UserOut, user_document_to_out


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(*, user_id: str, role: str, owned_brands: list[str]) -> tuple[str, int]:
    """Returns (token, expires_in_seconds).

    The payload carries role/owned_brands too (per the brief), even
    though get_current_user re-fetches them from Mongo rather than
    trusting these claims for authorization decisions — they're still
    useful to a client that wants to render role-aware UI (e.g. show
    the brand switcher) without waiting on a GET /api/auth/me round
    trip first.
    """
    expires_delta = timedelta(hours=settings.jwt_expiry_hours)
    expire_at = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": user_id,
        "role": role,
        "owned_brands": owned_brands,
        "exp": expire_at,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


# auto_error=False on both schemes: a missing/malformed Authorization
# header should reach our own 401 with a clear message, not FastAPI's
# generic default one — and the "optional" variant (used only by
# POST /api/auth/register's bootstrap check) must be able to return
# None instead of raising at all when there's no header.
_bearer_required = HTTPBearer(auto_error=False)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid or expired token: {exc}"
        ) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_required),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> UserOut:
    """FastAPI dependency: decodes the Authorization: Bearer JWT, loads
    the user it names, and returns their current profile. 401s on a
    missing header, a malformed/expired/tampered token, or a token
    naming a user that's since been deleted.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = _decode_token(credentials.credentials)
    try:
        user_object_id = ObjectId(payload.get("sub"))
    except (InvalidId, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        ) from exc

    document = await db.users.find_one({"_id": user_object_id})
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists"
        )
    return user_document_to_out(document)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_required),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> UserOut | None:
    """Same as get_current_user, but returns None instead of raising
    when there's no Authorization header at all.

    The one caller: POST /api/auth/register's bootstrap check, which
    must accept an anonymous request when zero users exist yet (there's
    no admin to have issued a token) but still needs to know whether an
    already-authenticated admin is calling once that's no longer true.

    Deliberately does NOT swallow a *present* token's own failures
    (expired/malformed/bad signature, or a token naming a deleted user)
    — those still raise get_current_user's 401 and propagate normally.
    Only "there was no token to even attempt" collapses to None here;
    "there was a token and it's garbage" is a failed-authentication
    case (401), not the same as an anonymous request, and conflating
    the two previously meant a client presenting a bad token got the
    same 403 ("Admin role required") an honestly-anonymous client did
    — correct for "no token", wrong for "invalid token", since a 403
    implies the caller was identified and denied by role, not that
    identification itself failed.
    """
    if credentials is None:
        return None
    return await get_current_user(credentials, db)


async def require_admin(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    """FastAPI dependency: get_current_user, then 403s unless role='admin'."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return current_user


def brand_match_stage(current_user: UserOut, requested_brand: str | None = None) -> dict | None:
    """Returns a MongoDB match condition scoping a query by brand, or
    None for an unscoped admin viewing everything.

    - role="business": ALWAYS scoped to current_user.owned_brands,
      regardless of `requested_brand` — a business account's own access
      boundary can never be widened by a query param it happens to
      pass, whether accidentally or deliberately.
    - role="admin", `requested_brand` given: scoped to exactly that one
      brand — this is what powers the frontend's admin brand-switcher
      (KpiCardsRow etc. pass the selected brand through as `?brand=`;
      see api/client.js). An admin picking "All brands" omits the
      param entirely.
    - role="admin", no `requested_brand`: None (no scoping — admins see
      everything by default, same as before the brand-switcher existed).

    Callers merge the result into their query's existing filter/first
    $match stage (see routers/orders.py, routers/inventory.py) rather
    than prepending it as a separate stage — MongoDB's aggregation
    optimizer merges adjacent $match stages anyway, and combining brand
    + the endpoint's own time-window condition into one dict is exactly
    the shape the brand_1_timestamp_-1 index (see database.py) is built
    to serve in a single index scan.
    """
    if current_user.role == "business":
        return {"brand": {"$in": current_user.owned_brands}}
    if requested_brand:
        return {"brand": requested_brand}
    return None
