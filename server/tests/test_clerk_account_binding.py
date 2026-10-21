"""Clerk sign-in may only reach an explicitly linked Eigent account."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("database_url", "sqlite:///test.db")
os.environ.setdefault("secret_key", "test-secret-with-at-least-thirty-two-bytes")

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine

from app.domains.user.api import login_controller
from app.core.database import session as database_session
from app.domains.user.service.clerk_session import (
    ClerkEmailUnavailable, ClerkNotConfigured, ClerkSessionInvalid,
    redeemed_subject, verified_primary_email, verify_clerk_session,
)
from app.model.user.user import User
from app.shared.auth.user_auth import V1UserAuth
from app.shared.auth import user_auth
from app.shared.exception import TokenException
from app.shared.middleware.rate_limit import login_rate_limiter
from app.shared.middleware.cors import get_cors_middleware

ISSUER = "https://clerk.example.test"
ORIGIN = "https://app.example.test"


def test_cloud_auto_login_is_unavailable(monkeypatch):
    monkeypatch.setenv("debug", "false")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(login_controller.auto_login(db_session=object()))
    assert exc.value.status_code == 404


@pytest.fixture(scope="module")
def key_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public_pem


def clerk_token(private, **overrides):
    now = datetime.now(UTC)
    claims = {
        "sub": "user_abc123",
        "sid": "sess_abc123",
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "azp": ORIGIN,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256")


def test_verifies_real_clerk_session_signature_and_claims(key_pair):
    private, public_pem = key_pair
    assert verify_clerk_session(
        clerk_token(private), issuer=ISSUER, public_key=public_pem,
        allowed_origins=frozenset({ORIGIN}),
    ) == "user_abc123"


@pytest.mark.parametrize("override", [
    {"iss": "https://other.example.test"},
    {"azp": "https://evil.example.test"},
    {"sub": "not-a-clerk-user"},
    {"sid": "not-a-clerk-session"},
    {"exp": datetime(2020, 1, 1, tzinfo=UTC)},
])
def test_rejects_wrong_clerk_claims(key_pair, override):
    private, public_pem = key_pair
    with pytest.raises(ClerkSessionInvalid):
        verify_clerk_session(
            clerk_token(private, **override), issuer=ISSUER, public_key=public_pem,
            allowed_origins=frozenset({ORIGIN}),
        )


def test_rejects_wrong_key_algorithm_and_missing_configuration(key_pair):
    private, public_pem = key_pair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (clerk_token(other), jwt.encode({"sub": "user_abc123"}, "long-enough-hmac-test-secret-for-signing", algorithm="HS256")):
        with pytest.raises(ClerkSessionInvalid):
            verify_clerk_session(token, issuer=ISSUER, public_key=public_pem,
                                 allowed_origins=frozenset({ORIGIN}))
    with pytest.raises(ClerkNotConfigured):
        verify_clerk_session(clerk_token(private), issuer="http://clerk.example.test",
                             public_key=public_pem, allowed_origins=frozenset({ORIGIN}))


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'users.sqlite'}")
    User.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([User(id=1, email="one@example.com"), User(id=2, email="two@example.com")])
        session.commit()
        yield session
    engine.dispose()


def test_clerk_login_creates_new_account_and_reuses_binding(db, monkeypatch):
    monkeypatch.setattr(login_controller, "_clerk_subject", lambda _token: "user_abc123")
    monkeypatch.setattr(login_controller, "verified_primary_email", lambda _subject: "new@example.com")
    data = login_controller.ClerkSessionIn(session_token="verified-token")
    response = login_controller.login_with_clerk(data, db)
    assert response.email == "new@example.com"
    assert response.user_id not in (1, 2)
    assert V1UserAuth.decode_token(response.token).id == response.user_id
    assert login_controller.login_with_clerk(data, db).user_id == response.user_id


def test_clerk_handoff_http_routes_keep_eigent_auth_boundary(db, monkeypatch):
    async def not_blacklisted(_jti):
        return False

    monkeypatch.setattr(user_auth, "is_blacklisted", not_blacklisted)
    monkeypatch.setattr(login_controller, "redeemed_subject", lambda _code, _verifier: "user_abc123")

    def database():
        with Session(db.get_bind()) as request_db:
            yield request_db

    app = FastAPI()
    app.include_router(login_controller.router, prefix="/api/v1")
    app.dependency_overrides[database_session] = database
    app.dependency_overrides[login_rate_limiter.dependency] = lambda: None

    @app.exception_handler(TokenException)
    async def rejected(_request, _exception):
        return JSONResponse(status_code=401, content={"error": "authentication required"})

    handoff = {"code": "a" * 43, "verifier": "b" * 43}
    with TestClient(app) as client:
        assert client.post("/api/v1/user/clerk/redeem-link", json=handoff).status_code == 401
        token = user_auth.create_access_token(1)
        response = client.post(
            "/api/v1/user/clerk/redeem-link", json=handoff,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"linked": True, "user_id": 1}
        response = client.post("/api/v1/user/clerk/redeem-login", json=handoff)
        assert response.status_code == 200
        assert response.json()["user_id"] == 1
        assert V1UserAuth.decode_token(response.json()["token"]).id == 1


def test_packaged_desktop_cors_requires_explicit_null_origin(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "null,https://eigent.example.test")
    app = FastAPI()
    app.add_middleware(CORSMiddleware, **get_cors_middleware())
    with TestClient(app) as client:
        headers = {
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        }
        response = client.options("/api/v1/user/clerk/redeem-login", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "null"
        response = client.options(
            "/api/v1/user/clerk/redeem-login",
            headers={**headers, "Origin": "https://other.example.test"},
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers


def test_matching_legacy_email_requires_explicit_binding(db, monkeypatch):
    monkeypatch.setattr(login_controller, "_clerk_subject", lambda _token: "user_abc123")
    monkeypatch.setattr(login_controller, "verified_primary_email", lambda _subject: "One@Example.com")
    data = login_controller.ClerkSessionIn(session_token="verified-token")
    with pytest.raises(HTTPException) as error:
        login_controller.login_with_clerk(data, db)
    assert error.value.status_code == 409
    first = db.get(User, 1)
    login_controller.link_clerk_account(data, db, SimpleNamespace(user=first))
    assert login_controller.login_with_clerk(data, db).user_id == 1


def test_clerk_link_is_one_to_one_and_cannot_be_reassigned(db, monkeypatch):
    subject = "user_abc123"
    monkeypatch.setattr(login_controller, "_clerk_subject", lambda _token: subject)
    data = login_controller.ClerkSessionIn(session_token="verified-token")
    first, second = db.get(User, 1), db.get(User, 2)
    login_controller.link_clerk_account(data, db, SimpleNamespace(user=first))
    with pytest.raises(HTTPException) as duplicate:
        login_controller.link_clerk_account(data, db, SimpleNamespace(user=second))
    assert duplicate.value.status_code == 409
    subject = "user_other123"
    with pytest.raises(HTTPException) as reassigned:
        login_controller.link_clerk_account(data, db, SimpleNamespace(user=first))
    assert reassigned.value.status_code == 409


def test_blocked_user_cannot_use_clerk(db, monkeypatch):
    monkeypatch.setattr(login_controller, "_clerk_subject", lambda _token: "user_abc123")
    data = login_controller.ClerkSessionIn(session_token="verified-token")
    first = db.get(User, 1)
    first.status = -1
    db.add(first)
    db.commit()
    with pytest.raises(HTTPException) as error:
        login_controller.link_clerk_account(data, db, SimpleNamespace(user=first))
    assert error.value.status_code == 403


def test_database_enforces_unique_clerk_binding(db):
    first, second = db.get(User, 1), db.get(User, 2)
    first.clerk_id = "user_abc123"
    db.add(first)
    db.commit()
    second.clerk_id = "user_abc123"
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_handoff_redeems_code_at_fixed_origin_and_verifies_jwt(key_pair, monkeypatch):
    private, public_pem = key_pair
    token = clerk_token(private)
    monkeypatch.setenv("EIGENT_CLERK_HANDOFF_ORIGIN", "https://robotstaging.hangent.com")
    monkeypatch.setenv("EIGENT_CLERK_ISSUER", ISSUER)
    monkeypatch.setenv("EIGENT_CLERK_JWT_KEY", public_pem)
    monkeypatch.setenv("EIGENT_CLERK_ALLOWED_ORIGINS", ORIGIN)
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"session_token": token}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("app.domains.user.service.clerk_session.requests.post", post)
    assert redeemed_subject("a" * 43, "b" * 43) == "user_abc123"
    assert calls == [("https://robotstaging.hangent.com/api/eigent/redeem", {
        "json": {"code": "a" * 43, "verifier": "b" * 43}, "timeout": 5, "allow_redirects": False,
    })]


def test_first_login_uses_clerk_verified_primary_email(monkeypatch):
    monkeypatch.setenv("EIGENT_CLERK_SECRET_KEY", "sk_test_private")
    profile = {
        "id": "user_abc123",
        "primary_email_address_id": "id_primary",
        "email_addresses": [
            {"id": "id_other", "email_address": "other@example.com",
             "verification": {"status": "verified"}},
            {"id": "id_primary", "email_address": "primary@example.com",
             "verification": {"status": "verified"}},
        ],
    }
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return profile

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("app.domains.user.service.clerk_session.requests.get", get)
    assert verified_primary_email("user_abc123") == "primary@example.com"
    assert calls == [("https://api.clerk.com/v1/users/user_abc123", {
        "headers": {"Authorization": "Bearer sk_test_private"},
        "timeout": 5, "allow_redirects": False,
    })]
    profile["email_addresses"][1]["verification"]["status"] = "unverified"
    with pytest.raises(ClerkEmailUnavailable):
        verified_primary_email("user_abc123")
