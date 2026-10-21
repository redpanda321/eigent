"""Clerk sign-in may only reach an explicitly linked Eigent account."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("database_url", "sqlite:///test.db")
os.environ.setdefault("secret_key", "test-secret-with-at-least-thirty-two-bytes")

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine

from app.domains.user.api import login_controller
from app.domains.user.service.clerk_session import ClerkNotConfigured, ClerkSessionInvalid, verify_clerk_session
from app.model.user.user import User
from app.shared.auth.user_auth import V1UserAuth

ISSUER = "https://clerk.example.test"
ORIGIN = "https://app.example.test"


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


def test_clerk_login_requires_explicit_binding(db, monkeypatch):
    monkeypatch.setattr(login_controller, "_clerk_subject", lambda _token: "user_abc123")
    data = login_controller.ClerkSessionIn(session_token="verified-token")
    with pytest.raises(HTTPException) as error:
        login_controller.login_with_clerk(data, db)
    assert error.value.status_code == 403

    first = db.get(User, 1)
    login_controller.link_clerk_account(data, db, SimpleNamespace(user=first))
    response = login_controller.login_with_clerk(data, db)
    assert response.user_id == 1
    assert V1UserAuth.decode_token(response.token).id == 1


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
