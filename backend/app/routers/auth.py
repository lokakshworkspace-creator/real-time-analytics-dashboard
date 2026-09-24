"""Auth endpoints: POST /api/auth/register, POST /api/auth/login,
GET /api/auth/me.

Registration has a one-time bootstrap: while the `users` collection is
empty, POST /api/auth/register succeeds without a token and always
creates an admin, regardless of the `role` in the request body —
otherwise there would be no way to create the very first admin account
(every other registration requires an existing admin's token). Once at
least one user exists, that door closes permanently: every further
registration requires a valid admin token, and honors the requested
role.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from ..database import get_database
from ..models import LoginIn, TokenOut, UserIn, UserOut, user_document_to_out
from ..security import (
    create_access_token,
    get_current_user,
    get_current_user_optional,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: UserOut | None = Depends(get_current_user_optional),
) -> UserOut:
    is_bootstrap = await db.users.count_documents({}) == 0

    if is_bootstrap:
        # First-ever account: no auth required, forced to admin
        # regardless of what the request asked for — see module
        # docstring. A stray/malicious bootstrap call can only ever
        # create an admin, never a business account with no admin
        # having approved it.
        role = "admin"
    else:
        if current_user is None or current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to register new users",
            )
        role = payload.role

    # Checked explicitly rather than relying solely on the unique
    # email_1 index (see database.py) to reject a duplicate: the index
    # is created once, at app startup, not guaranteed to exist the
    # instant this handler runs in every environment (e.g. a test
    # database whose lifespan never ran — confirmed directly, this
    # exact gap caused test_duplicate_email_returns_409 to fail against
    # an unindexed test DB before this check was added). The index stays
    # in place too, as the actual belt-and-suspenders: it's what makes
    # the DuplicateKeyError catch below reachable at all, for the race
    # between two concurrent registrations for the same email that this
    # single pre-check can't fully close on its own.
    if await db.users.find_one({"email": payload.email}) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email {payload.email!r} already exists",
        )

    document = {
        "email": payload.email,
        "password_hash": hash_password(payload.password),
        "role": role,
        "business_name": payload.business_name if role == "business" else None,
        "owned_brands": payload.owned_brands if role == "business" else [],
        "created_at": datetime.now(timezone.utc),
    }

    try:
        result = await db.users.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email {payload.email!r} already exists",
        ) from exc

    created = await db.users.find_one({"_id": result.inserted_id})
    return user_document_to_out(created)


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, db: AsyncIOMotorDatabase = Depends(get_database)) -> TokenOut:
    user = await db.users.find_one({"email": payload.email})
    # Same 401 + message whether the email doesn't exist or the
    # password is wrong — distinguishing them would let a caller
    # enumerate registered email addresses one guess at a time.
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )

    token, expires_in = create_access_token(
        user_id=str(user["_id"]), role=user["role"], owned_brands=user.get("owned_brands", [])
    )
    return TokenOut(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    return current_user
