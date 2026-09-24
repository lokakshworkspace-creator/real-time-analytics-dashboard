"""One-time setup script: creates the first admin account and one
business account per brand in simulator.py's product catalog, so role
scoping can be exercised immediately without registering accounts by
hand through Swagger.

An HTTP client of the API, same philosophy as simulator.py — this
script never imports from `app/` or touches MongoDB directly, so it
exercises the exact same validation/hashing/token-issuance path a real
registration would hit, and works against a deployed backend with no
changes.

Credentials are never hardcoded here (per CLAUDE.md's "no credentials
in code" rule): SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD/
SEED_BUSINESS_PASSWORD are read from the repo-root .env (see
.env.example). Every seeded business account shares
SEED_BUSINESS_PASSWORD — fine for a local/demo setup script where the
whole point is "these are throwaway accounts for testing role
scoping," not something to reuse anywhere that matters.

Run:    python seed_users.py [--url URL]
Idempotent: if the admin (or a given business account) already exists,
login is used instead of failing the whole script on a 409.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

from simulator import PRODUCTS

REPO_ROOT_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(REPO_ROOT_ENV)


def _brand_email(brand: str) -> str:
    # "Hydro Flask" -> "hydro-flask@example.com" — deterministic, so
    # re-running this script always targets the same account per brand.
    slug = brand.lower().replace(" ", "-")
    return f"{slug}@example.com"


def register_or_login(
    session: requests.Session, base_url: str, *, email: str, password: str, token: str | None = None, **extra
) -> str:
    """Registers `email` (as admin, via `token`, if given — otherwise
    as the anonymous bootstrap call) and returns its access token. If
    the account already exists (409), logs in instead — this makes
    re-running the script safe.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    register_response = session.post(
        f"{base_url}/api/auth/register",
        json={"email": email, "password": password, **extra},
        headers=headers,
    )
    if register_response.status_code not in (201, 409):
        register_response.raise_for_status()

    login_response = session.post(
        f"{base_url}/api/auth/login", json={"email": email, "password": password}
    )
    login_response.raise_for_status()
    return login_response.json()["access_token"]


def run(base_url: str) -> None:
    admin_email = os.environ.get("SEED_ADMIN_EMAIL")
    admin_password = os.environ.get("SEED_ADMIN_PASSWORD")
    business_password = os.environ.get("SEED_BUSINESS_PASSWORD")

    missing = [
        name
        for name, value in [
            ("SEED_ADMIN_EMAIL", admin_email),
            ("SEED_ADMIN_PASSWORD", admin_password),
            ("SEED_BUSINESS_PASSWORD", business_password),
        ]
        if not value
    ]
    if missing:
        print(f"Missing required .env vars: {', '.join(missing)}. See .env.example.")
        sys.exit(1)

    session = requests.Session()

    print(f"Setting up admin account -> {admin_email}")
    admin_token = register_or_login(
        session, base_url, email=admin_email, password=admin_password, role="admin"
    )
    print("  admin ready.\n")

    brands = sorted({product.brand for product in PRODUCTS})
    print(f"Setting up {len(brands)} business account(s), one per brand: {', '.join(brands)}")
    for brand in brands:
        email = _brand_email(brand)
        register_or_login(
            session,
            base_url,
            email=email,
            password=business_password,
            token=admin_token,
            role="business",
            business_name=brand,
            owned_brands=[brand],
        )
        print(f"  {brand:<15} -> {email}")

    print(
        "\nDone. Every seeded business account shares SEED_BUSINESS_PASSWORD "
        "(see .env). Admin login: "
        f"{admin_email} / (SEED_ADMIN_PASSWORD from .env)."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--url",
        default=os.environ.get("SIMULATOR_API_URL", "http://localhost:8000"),
        help="Base URL of the backend API (default: http://localhost:8000, or $SIMULATOR_API_URL)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(args.url.rstrip("/"))


if __name__ == "__main__":
    main()
