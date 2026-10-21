"""Verify Clerk session JWTs before account binding or login.

An Eigent access token still owns the existing numeric account. Clerk's
subject only identifies a separately verified sign-in and is never inferred
from email or from an unverified JWT payload.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import jwt

from app.core.environment import env


class ClerkNotConfigured(Exception):
    pass


class ClerkSessionInvalid(Exception):
    pass


def verify_clerk_session(token: str, *, issuer: str, public_key: str,
                         allowed_origins: frozenset[str]) -> str:
    parsed = urlsplit(issuer)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query
            or parsed.fragment or not public_key or not allowed_origins):
        raise ClerkNotConfigured("Clerk verification is not configured")
    if not isinstance(token, str) or not 1 <= len(token) <= 8192:
        raise ClerkSessionInvalid("invalid Clerk session")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise ClerkSessionInvalid("invalid Clerk session")
        claims = jwt.decode(
            token, public_key, algorithms=["RS256"], issuer=issuer,
            leeway=5, options={"verify_aud": False,
                               "require": ["sub", "sid", "iss", "iat", "nbf", "exp", "azp"]},
        )
    except jwt.InvalidTokenError as exc:
        raise ClerkSessionInvalid("invalid Clerk session") from exc
    subject, session_id, origin = claims["sub"], claims["sid"], claims["azp"]
    if (not isinstance(subject, str) or not re.fullmatch(r"user_[A-Za-z0-9]+", subject)
            or not isinstance(session_id, str)
            or not re.fullmatch(r"sess_[A-Za-z0-9]+", session_id)
            or not isinstance(origin, str) or origin not in allowed_origins):
        raise ClerkSessionInvalid("invalid Clerk session")
    return subject


def verified_subject(token: str) -> str:
    issuer = env("EIGENT_CLERK_ISSUER", "")
    public_key = env("EIGENT_CLERK_JWT_KEY", "")
    allowed = frozenset(origin.strip() for origin in
                        env("EIGENT_CLERK_ALLOWED_ORIGINS", "").split(",") if origin.strip())
    return verify_clerk_session(token, issuer=issuer, public_key=public_key,
                                allowed_origins=allowed)
