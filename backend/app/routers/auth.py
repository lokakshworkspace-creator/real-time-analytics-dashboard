"""Auth endpoints: POST /api/auth/register, POST /api/auth/login,
GET /api/auth/me, PATCH /api/auth/me, POST /api/auth/change-password.

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

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from ..database import get_database
from ..models import (
    LoginIn,
    MessageOut,
    PasswordChangeIn,
    ProfileUpdateIn,
    TokenOut,
    UserIn,
    UserOut,
    user_document_to_out,
)
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


@router.patch("/me", response_model=UserOut)
async def update_me(
    payload: ProfileUpdateIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: UserOut = Depends(get_current_user),
) -> UserOut:
    """Self-service profile edit. Today that means exactly one thing: a
    business account renaming itself (`business_name`).

    An admin has no business name — the field is null by design (see
    `register`) — so there is nothing for them to edit, and this is a 403
    (their role isn't allowed the operation), not a silent no-op.
    Everything else — role, owned_brands, email — is rejected before this
    body is ever handled: ProfileUpdateIn forbids extra fields, so a
    request naming them gets a 422 and changes nothing (not even a
    business_name sent alongside them: the whole request is refused).
    Brand ownership in particular is deliberately not self-editable: it
    is what every scoped query in this app keys on (security.py's
    brand_match_stage), so letting an account widen it would defeat the
    whole role model.
    """
    if current_user.role != "business":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator accounts have no business name to edit",
        )

    await db.users.update_one(
        {"_id": ObjectId(current_user.user_id)},
        {"$set": {"business_name": payload.business_name}},
    )
    updated = await db.users.find_one({"_id": ObjectId(current_user.user_id)})
    return user_document_to_out(updated)


@router.post("/change-password", response_model=MessageOut)
async def change_password(
    payload: PasswordChangeIn,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: UserOut = Depends(get_current_user),
) -> MessageOut:
    """Changes the caller's own password, after re-proving they know the
    current one — a stolen or left-open session alone can't take over
    the account.

    A wrong `current_password` is a 401 with its own message. (The
    frontend treats 401 from most endpoints as "your session expired"
    and signs you out; this call opts out of that — see api/client.js —
    since here it means "that password is wrong", not "you're logged
    out".)

    The response carries no password or hash, only an acknowledgement.

    What this does NOT do: revoke existing tokens. Sessions are stateless
    JWTs with a 24h expiry and no server-side session list, so a token
    minted before the change keeps working until it expires. Changing a
    password therefore protects future logins, not sessions already
    open elsewhere — see the README's Auth section.
    """
    document = await db.users.find_one({"_id": ObjectId(current_user.user_id)})
    if document is None or not verify_password(payload.current_password, document["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect"
        )

    await db.users.update_one(
        {"_id": document["_id"]},
        {"$set": {"password_hash": hash_password(payload.new_password)}},
    )
    return MessageOut(message="Password updated")
