"""Integration tests for auth (POST /api/auth/register, POST
/api/auth/login, GET /api/auth/me) and — critically — brand-scoping
isolation: a business account's request must never return another
brand's data, no matter which endpoint it hits. Kept in its own file
so a scoping regression is unambiguous in the test output, not buried
inside test_orders_api.py's functional coverage.
"""

import uuid


def register(client, **overrides):
    payload = {
        "email": f"{uuid.uuid4()}@test.example.com",
        "password": "a-test-password",
        "role": "admin",
        **overrides,
    }
    return client.post("/api/auth/register", json=payload)


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


class TestBootstrap:
    def test_first_registration_succeeds_without_auth(self, api_client):
        response = register(api_client, email="first@test.example.com", password="firstpassword", role="admin")

        assert response.status_code == 201
        assert response.json()["role"] == "admin"

    def test_bootstrap_forces_admin_role_regardless_of_requested_role(self, api_client):
        # Even a bootstrap request asking for role="business" becomes
        # admin — see routers/auth.py's module docstring for why.
        response = register(
            api_client,
            email="sneaky-bootstrap@test.example.com",
            password="sneakypassword",
            role="business",
            business_name="Whatever",
            owned_brands=["Whatever"],
        )

        assert response.status_code == 201
        assert response.json()["role"] == "admin"

    def test_registration_after_bootstrap_requires_admin_token(self, api_client):
        register(api_client, email="admin@test.example.com", password="adminpassword", role="admin")

        # No Authorization header at all — treated as an honestly
        # anonymous request, not a failed authentication: the collection
        # is no longer empty, so the bootstrap free pass is gone and
        # they're simply denied by role (403), the same as a valid,
        # correctly-authenticated non-admin would get — see
        # security.py's get_current_user_optional docstring for why
        # this is deliberately distinct from a *present* invalid token
        # (401, see the garbage-token test below).
        response = register(api_client, email="second@test.example.com", password="secondpassword", role="admin")

        assert response.status_code == 403
        assert response.status_code != 201

    def test_registration_after_bootstrap_rejects_a_garbage_token_too(self, api_client):
        register(api_client, email="admin2@test.example.com", password="adminpassword", role="admin")

        response = api_client.post(
            "/api/auth/register",
            headers={"Authorization": "Bearer not-a-real-jwt"},
            json={"email": "sneaky@test.example.com", "password": "sneakypassword", "role": "admin"},
        )

        # A *present* token that fails to decode is a failed-
        # authentication case (401), distinct from an honestly
        # anonymous request during bootstrap or a valid-but-non-admin
        # token (both 403 — see security.py's get_current_user_optional
        # docstring for the reasoning). Was 403 before that distinction
        # was added; this assertion was updated to match the fix.
        assert response.status_code == 401
        assert response.status_code != 201

    def test_registration_after_bootstrap_never_succeeds_unauthenticated_even_for_business_role(
        self, api_client
    ):
        # Same guarantee, but the payload asks for role="business" this
        # time — makes sure the 403 isn't accidentally specific to
        # role="admin" requests.
        register(api_client, email="admin3@test.example.com", password="adminpassword", role="admin")

        response = register(
            api_client,
            email="sneaky-business@test.example.com",
            password="sneakypassword",
            role="business",
            business_name="Sneaky Co",
            owned_brands=["Nike"],
        )

        assert response.status_code == 403

    def test_business_account_cannot_register_new_users(self, api_client, admin_headers, business_headers_factory):
        business_headers = business_headers_factory("nike-registrar@test.example.com", ["Nike"])

        response = api_client.post(
            "/api/auth/register",
            headers=business_headers,
            json={"email": "another@test.example.com", "password": "anotherpassword", "role": "admin"},
        )

        assert response.status_code == 403


class TestRegisterValidation:
    def test_business_role_without_owned_brands_returns_422(self, api_client, admin_headers):
        response = api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={
                "email": "broken@test.example.com",
                "password": "brokenpassword",
                "role": "business",
                "business_name": "Broken Co",
            },
        )

        assert response.status_code == 422

    def test_business_role_without_business_name_returns_422(self, api_client, admin_headers):
        response = api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={
                "email": "broken2@test.example.com",
                "password": "brokenpassword",
                "role": "business",
                "owned_brands": ["Nike"],
            },
        )

        assert response.status_code == 422

    def test_duplicate_email_returns_409(self, api_client, admin_headers):
        api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={"email": "dupe@test.example.com", "password": "dupepassword", "role": "admin"},
        )
        response = api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={"email": "dupe@test.example.com", "password": "differentpassword", "role": "admin"},
        )

        assert response.status_code == 409

    def test_password_too_short_returns_422(self, api_client, admin_headers):
        response = api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={"email": "short@test.example.com", "password": "short", "role": "admin"},
        )

        assert response.status_code == 422

    def test_invalid_email_returns_422(self, api_client, admin_headers):
        response = api_client.post(
            "/api/auth/register",
            headers=admin_headers,
            json={"email": "not-an-email", "password": "validpassword", "role": "admin"},
        )

        assert response.status_code == 422


class TestLogin:
    def test_wrong_password_returns_401(self, api_client):
        register(api_client, email="loginuser@test.example.com", password="correctpassword", role="admin")

        response = login(api_client, "loginuser@test.example.com", "wrongpassword")

        assert response.status_code == 401

    def test_unknown_email_returns_401(self, api_client):
        response = login(api_client, "nobody@test.example.com", "whatever123")

        assert response.status_code == 401

    def test_successful_login_returns_bearer_token(self, api_client):
        register(api_client, email="tokentest@test.example.com", password="tokenpassword", role="admin")

        response = login(api_client, "tokentest@test.example.com", "tokenpassword")

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert len(body["access_token"]) > 20


class TestMe:
    def test_returns_profile_for_a_valid_token(self, api_client, admin_headers):
        response = api_client.get("/api/auth/me", headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    def test_missing_token_returns_401(self, api_client):
        assert api_client.get("/api/auth/me").status_code == 401

    def test_malformed_token_returns_401(self, api_client):
        response = api_client.get(
            "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
        )

        assert response.status_code == 401

    def test_business_account_profile_includes_owned_brands(self, api_client, business_headers_factory):
        headers = business_headers_factory("nike-me@test.example.com", ["Nike"])

        response = api_client.get("/api/auth/me", headers=headers)

        body = response.json()
        assert body["role"] == "business"
        assert body["owned_brands"] == ["Nike"]


class TestBrandScopingIsolation:
    """The critical guarantee the whole auth feature exists for: a
    business account's request must never return another brand's data,
    regardless of which endpoint it hits. Each test here posts orders
    for TWO different brands, then asserts the scoped account's
    response contains only its own.
    """

    def _seed_two_brand_orders(self, api_client, admin_headers):
        for brand, product_id, region in [
            ("Nike", "sku-nike", "North America"),
            ("Adidas", "sku-adidas", "Europe"),
        ]:
            api_client.post(
                "/api/inventory/seed",
                json={
                    "product_id": product_id,
                    "product_name": f"{brand} Shoes",
                    "category": "Apparel",
                    "brand": brand,
                    "region": region,
                    "current_stock": 100,
                },
            )
            api_client.post(
                "/api/orders",
                json={
                    "order_id": str(uuid.uuid4()),
                    "product_id": product_id,
                    "product_name": f"{brand} Shoes",
                    "category": "Apparel",
                    "brand": brand,
                    "quantity": 2,
                    "unit_price": 50.0,
                    "region": region,
                },
            )

    def test_orders_endpoint_never_returns_another_brands_data(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-orders@test.example.com", ["Nike"])

        response = api_client.get("/api/orders", params={"limit": 100}, headers=nike_headers)

        brands_seen = {order["brand"] for order in response.json()}
        assert brands_seen == {"Nike"}
        assert "Adidas" not in brands_seen

    def test_kpis_endpoint_only_counts_owned_brand(self, api_client, admin_headers, business_headers_factory):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-kpis@test.example.com", ["Nike"])

        response = api_client.get("/api/orders/kpis", headers=nike_headers)

        # Each seeded order is qty=2 @ 50.0 = 100 revenue; only Nike's
        # should count for a Nike-scoped account.
        assert response.json()["current"]["total_orders"] == 1
        assert response.json()["current"]["revenue"] == 100.0

    def test_products_endpoint_never_returns_another_brands_products(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-products@test.example.com", ["Nike"])

        response = api_client.get("/api/orders/products", headers=nike_headers)

        product_ids = {p["product_id"] for p in response.json()["products"]}
        assert product_ids == {"sku-nike"}

    def test_regions_endpoint_never_returns_another_brands_regions(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-regions@test.example.com", ["Nike"])

        response = api_client.get("/api/orders/regions", headers=nike_headers)

        regions = {r["region"] for r in response.json()}
        assert regions == {"North America"}  # Adidas's Europe order excluded

    def test_inventory_endpoint_never_returns_another_brands_stock(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-inventory@test.example.com", ["Nike"])

        response = api_client.get("/api/inventory", headers=nike_headers)

        brands_seen = {item["brand"] for item in response.json()}
        assert brands_seen == {"Nike"}

    def test_inventory_risk_endpoint_never_returns_another_brands_stock(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        nike_headers = business_headers_factory("nike-risk@test.example.com", ["Nike"])

        response = api_client.get("/api/inventory/risk", headers=nike_headers)

        brands_seen = {item["brand"] for item in response.json()}
        assert brands_seen == {"Nike"}

    def test_anomalies_endpoint_never_returns_another_brands_anomalies(
        self, api_client, admin_headers, business_headers_factory
    ):
        # Build a real flagged anomaly for Adidas specifically, then
        # confirm a Nike-scoped account sees none of it.
        from datetime import datetime, timedelta, timezone

        reference_hour = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        for hours_ago, count in zip(range(10, 0, -1), [3, 4, 3, 4, 3, 4, 3, 4, 3, 4]):
            bucket_time = reference_hour - timedelta(hours=hours_ago)
            for _ in range(count):
                api_client.post(
                    "/api/orders",
                    json={
                        "order_id": str(uuid.uuid4()),
                        "product_id": "sku-adidas",
                        "product_name": "Adidas Shoes",
                        "category": "Apparel",
                        "brand": "Adidas",
                        "quantity": 1,
                        "unit_price": 50.0,
                        "region": "Adidas Anomaly Region",
                        "timestamp": bucket_time.isoformat(),
                    },
                )
        for _ in range(20):
            api_client.post(
                "/api/orders",
                json={
                    "order_id": str(uuid.uuid4()),
                    "product_id": "sku-adidas",
                    "product_name": "Adidas Shoes",
                    "category": "Apparel",
                    "brand": "Adidas",
                    "quantity": 1,
                    "unit_price": 50.0,
                    "region": "Adidas Anomaly Region",
                    "timestamp": reference_hour.isoformat(),
                },
            )

        admin_anomalies = api_client.get("/api/anomalies/business", headers=admin_headers).json()
        assert len(admin_anomalies) > 0  # sanity: the anomaly really was flagged

        nike_headers = business_headers_factory("nike-anomalies@test.example.com", ["Nike"])
        nike_anomalies = api_client.get("/api/anomalies/business", headers=nike_headers).json()
        assert nike_anomalies == []

    def test_business_account_owning_multiple_brands_sees_both(
        self, api_client, admin_headers, business_headers_factory
    ):
        self._seed_two_brand_orders(api_client, admin_headers)
        multi_headers = business_headers_factory("multi-brand@test.example.com", ["Nike", "Adidas"])

        response = api_client.get("/api/orders", params={"limit": 100}, headers=multi_headers)

        brands_seen = {order["brand"] for order in response.json()}
        assert brands_seen == {"Nike", "Adidas"}

    def test_admin_sees_every_brand(self, api_client, admin_headers):
        self._seed_two_brand_orders(api_client, admin_headers)

        response = api_client.get("/api/orders", params={"limit": 100}, headers=admin_headers)

        brands_seen = {order["brand"] for order in response.json()}
        assert brands_seen == {"Nike", "Adidas"}


class TestBrandParamCannotOverrideBusinessScope:
    """The specific attack the admin brand-switcher's `?brand=` param
    could open up if it weren't guarded: a business account explicitly
    passing another brand's name on the query string.

    security.brand_match_stage() ignores `requested_brand` entirely for
    role="business" — a business account is always scoped to its own
    owned_brands regardless of what it asks for. That's the design
    choice made here (over rejecting the param outright with a 403/422):
    it's less code (one guard clause, already there, vs. a second
    branch that has to distinguish "business user passed brand=" from
    every other case), and it fails safe by construction — there's no
    additional check that could be forgotten on a new endpoint, because
    every endpoint already calls brand_match_stage() to get scoped at
    all. Each test below logs in as a Nike-only business account,
    explicitly requests ?brand=Sony, and asserts the response is still
    Nike-only (200) — proving the param is silently ignored, not that
    it happens to 403.
    """

    def _seed_nike_and_sony(self, api_client):
        for brand, product_id, region in [
            ("Nike", "sku-nike-escape", "North America"),
            ("Sony", "sku-sony-escape", "Asia Pacific"),
        ]:
            api_client.post(
                "/api/inventory/seed",
                json={
                    "product_id": product_id,
                    "product_name": f"{brand} Item",
                    "category": "Test",
                    "brand": brand,
                    "region": region,
                    "current_stock": 100,
                },
            )
            api_client.post(
                "/api/orders",
                json={
                    "order_id": str(uuid.uuid4()),
                    "product_id": product_id,
                    "product_name": f"{brand} Item",
                    "category": "Test",
                    "brand": brand,
                    "quantity": 3,
                    "unit_price": 40.0,
                    "region": region,
                },
            )

    def _nike_headers(self, api_client, business_headers_factory):
        return business_headers_factory("nike-escape@test.example.com", ["Nike"])

    def test_orders_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/orders", params={"limit": 100, "brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        brands_seen = {order["brand"] for order in response.json()}
        assert brands_seen == {"Nike"}
        assert "Sony" not in brands_seen

    def test_kpis_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/orders/kpis", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        # Nike's one seeded order: qty=3 @ 40.0 = 120 revenue. If Sony's
        # data leaked through, this would be 2 orders / 240 revenue.
        assert response.json()["current"]["total_orders"] == 1
        assert response.json()["current"]["revenue"] == 120.0

    def test_trend_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/orders/trend", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["orders"] == 1  # only Nike's order, not both

    def test_regions_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/orders/regions", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        regions = {r["region"] for r in response.json()}
        assert regions == {"North America"}  # Nike's region, never Sony's Asia Pacific

    def test_products_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/orders/products", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        product_ids = {p["product_id"] for p in response.json()["products"]}
        assert product_ids == {"sku-nike-escape"}

    def test_inventory_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/inventory", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        brands_seen = {item["brand"] for item in response.json()}
        assert brands_seen == {"Nike"}

    def test_inventory_risk_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/inventory/risk", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        brands_seen = {item["brand"] for item in response.json()}
        assert brands_seen == {"Nike"}

    def test_anomalies_endpoint(self, api_client, admin_headers, business_headers_factory):
        self._seed_nike_and_sony(api_client)
        nike_headers = self._nike_headers(api_client, business_headers_factory)

        response = api_client.get(
            "/api/anomalies/business", params={"brand": "Sony"}, headers=nike_headers
        )

        assert response.status_code == 200
        # No anomalies flagged in this small seed (cold start), but the
        # real assertion is that this 200s without ever touching Sony's
        # data — any event present must be Nike's.
        assert all(event["brand"] == "Nike" for event in response.json())
