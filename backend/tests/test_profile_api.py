"""Integration tests for the profile self-service endpoints:
PATCH /api/auth/me (rename a business account) and
POST /api/auth/change-password.

Fixtures (conftest.py): `admin_headers` logs in admin@test.example.com /
"admin-test-password"; `business_headers_factory(email, brands)` registers
a business account with password "business-test-password" and returns
its headers.
"""

ADMIN_EMAIL, ADMIN_PASSWORD = "admin@test.example.com", "admin-test-password"
BUSINESS_PASSWORD = "business-test-password"


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def patch_me(client, headers, body):
    return client.patch("/api/auth/me", headers=headers, json=body)


def change_password(client, headers, current, new):
    return client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": current, "new_password": new},
    )


class TestUpdateBusinessName:
    def test_requires_auth(self, api_client):
        assert api_client.patch("/api/auth/me", json={"business_name": "X"}).status_code == 401

    def test_business_account_can_rename_itself(self, api_client, business_headers_factory):
        headers = business_headers_factory("nike-rename@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"business_name": "Nike Global"})

        assert response.status_code == 200
        body = response.json()
        assert body["business_name"] == "Nike Global"
        assert body["email"] == "nike-rename@test.example.com"
        assert body["role"] == "business"
        assert body["owned_brands"] == ["Nike"]
        assert "password" not in body and "password_hash" not in body

    def test_the_new_name_persists(self, api_client, business_headers_factory):
        headers = business_headers_factory("persist@test.example.com", ["Nike"])
        patch_me(api_client, headers, {"business_name": "Renamed Co"})

        assert api_client.get("/api/auth/me", headers=headers).json()["business_name"] == "Renamed Co"

    def test_surrounding_whitespace_is_trimmed(self, api_client, business_headers_factory):
        headers = business_headers_factory("trim@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"business_name": "   Padded Name  "})

        assert response.json()["business_name"] == "Padded Name"

    def test_blank_and_overlong_names_are_rejected(self, api_client, business_headers_factory):
        headers = business_headers_factory("blank@test.example.com", ["Nike"])

        assert patch_me(api_client, headers, {"business_name": "   "}).status_code == 422
        assert patch_me(api_client, headers, {"business_name": ""}).status_code == 422
        assert patch_me(api_client, headers, {"business_name": "x" * 101}).status_code == 422
        assert api_client.get("/api/auth/me", headers=headers).json()["business_name"] == "Nike"

    def test_admin_is_rejected_with_403(self, api_client, admin_headers):
        response = patch_me(api_client, admin_headers, {"business_name": "Admin Co"})

        assert response.status_code == 403
        assert api_client.get("/api/auth/me", headers=admin_headers).json()["business_name"] is None


class TestProfileCannotEditPrivilegedFields:
    def test_owned_brands_cannot_be_changed_through_this_endpoint(self, api_client, business_headers_factory):
        headers = business_headers_factory("brands@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"owned_brands": ["Sony", "Nike"]})

        assert response.status_code == 422
        assert api_client.get("/api/auth/me", headers=headers).json()["owned_brands"] == ["Nike"]

    def test_role_cannot_be_changed_through_this_endpoint(self, api_client, business_headers_factory):
        headers = business_headers_factory("role@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"role": "admin"})

        assert response.status_code == 422
        assert api_client.get("/api/auth/me", headers=headers).json()["role"] == "business"

    def test_email_cannot_be_changed_through_this_endpoint(self, api_client, business_headers_factory):
        headers = business_headers_factory("email@test.example.com", ["Nike"])

        assert patch_me(api_client, headers, {"email": "someone-else@test.example.com"}).status_code == 422
        assert api_client.get("/api/auth/me", headers=headers).json()["email"] == "email@test.example.com"

    def test_a_valid_name_smuggled_alongside_a_forbidden_field_changes_nothing(
        self, api_client, business_headers_factory
    ):
        # The whole request is refused — a partial apply would let an
        # attacker's escalation attempt look like it half-worked.
        headers = business_headers_factory("smuggle@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"business_name": "Sneaky", "owned_brands": ["Sony"]})

        assert response.status_code == 422
        me = api_client.get("/api/auth/me", headers=headers).json()
        assert me["business_name"] == "Nike"
        assert me["owned_brands"] == ["Nike"]

    def test_the_error_names_the_offending_field(self, api_client, business_headers_factory):
        headers = business_headers_factory("named@test.example.com", ["Nike"])

        response = patch_me(api_client, headers, {"business_name": "Ok", "owned_brands": ["Sony"]})

        locs = [error["loc"] for error in response.json()["detail"]]
        assert ["body", "owned_brands"] in locs


class TestChangePassword:
    def test_requires_auth(self, api_client):
        response = api_client.post(
            "/api/auth/change-password",
            json={"current_password": "whatever-123", "new_password": "another-pass-123"},
        )
        assert response.status_code == 401

    def test_correct_current_password_succeeds_and_the_new_password_logs_in(
        self, api_client, business_headers_factory
    ):
        email = "changepw@test.example.com"
        headers = business_headers_factory(email, ["Nike"])

        response = change_password(api_client, headers, BUSINESS_PASSWORD, "brand-new-password-1")

        assert response.status_code == 200
        assert login(api_client, email, "brand-new-password-1").status_code == 200
        assert login(api_client, email, BUSINESS_PASSWORD).status_code == 401

    def test_the_response_carries_no_password_or_hash(self, api_client, business_headers_factory):
        headers = business_headers_factory("nohash@test.example.com", ["Nike"])

        response = change_password(api_client, headers, BUSINESS_PASSWORD, "brand-new-password-2")

        assert response.json() == {"message": "Password updated"}
        assert "brand-new-password-2" not in response.text
        assert "$2b$" not in response.text  # a bcrypt hash prefix

    def test_wrong_current_password_is_401_and_changes_nothing(self, api_client, business_headers_factory):
        email = "wrongcur@test.example.com"
        headers = business_headers_factory(email, ["Nike"])

        response = change_password(api_client, headers, "definitely-not-it", "brand-new-password-3")

        assert response.status_code == 401
        assert response.json()["detail"] == "Current password is incorrect"
        assert login(api_client, email, BUSINESS_PASSWORD).status_code == 200  # old still works
        assert login(api_client, email, "brand-new-password-3").status_code == 401  # new does not

    def test_admin_can_change_their_own_password_too(self, api_client, admin_headers):
        response = change_password(api_client, admin_headers, ADMIN_PASSWORD, "new-admin-password-1")

        assert response.status_code == 200
        assert login(api_client, ADMIN_EMAIL, "new-admin-password-1").status_code == 200
        assert login(api_client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 401

    def test_new_password_follows_the_registration_rules(self, api_client, business_headers_factory):
        headers = business_headers_factory("rules@test.example.com", ["Nike"])

        too_short = change_password(api_client, headers, BUSINESS_PASSWORD, "short")
        too_long = change_password(api_client, headers, BUSINESS_PASSWORD, "a" * 73)
        just_right_min = change_password(api_client, headers, BUSINESS_PASSWORD, "12345678")

        assert too_short.status_code == 422
        assert too_long.status_code == 422
        assert just_right_min.status_code == 200  # exactly 8 is allowed, same as registration

    def test_new_password_must_differ_from_the_current_one(self, api_client, business_headers_factory):
        headers = business_headers_factory("same@test.example.com", ["Nike"])

        response = change_password(api_client, headers, BUSINESS_PASSWORD, BUSINESS_PASSWORD)

        assert response.status_code == 422

    def test_validation_errors_never_echo_either_password_back(self, api_client, business_headers_factory):
        headers = business_headers_factory("echo@test.example.com", ["Nike"])

        same = change_password(api_client, headers, BUSINESS_PASSWORD, BUSINESS_PASSWORD)
        too_long = change_password(api_client, headers, BUSINESS_PASSWORD, "z" * 73)

        assert BUSINESS_PASSWORD not in same.text
        assert "z" * 73 not in too_long.text

    def test_login_errors_do_not_echo_the_password_either(self, api_client):
        response = api_client.post(
            "/api/auth/login", json={"email": "not-an-email", "password": "my-secret-password"}
        )

        assert response.status_code == 422
        assert "my-secret-password" not in response.text

    def test_an_existing_token_keeps_working_after_a_password_change(self, api_client, business_headers_factory):
        # Documented limitation, pinned so a future change is deliberate:
        # sessions are stateless JWTs with no server-side revocation, so a
        # password change protects future logins, not open sessions.
        headers = business_headers_factory("stale-token@test.example.com", ["Nike"])
        change_password(api_client, headers, BUSINESS_PASSWORD, "brand-new-password-4")

        assert api_client.get("/api/auth/me", headers=headers).status_code == 200
