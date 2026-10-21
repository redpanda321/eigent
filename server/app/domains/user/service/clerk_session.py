"""Verify Clerk session JWTs before account binding or login.

An Eigent access token still owns the existing numeric account. Clerk's
subject only identifies a separately verified sign-in and is never inferred
from email or from an unverified JWT payload.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import jwt
import requests

from app.core.environment import env


class ClerkNotConfigured(Exception):
    pass


class ClerkSessionInvalid(Exception):
    pass


class ClerkHandoffUnavailable(Exception):
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


def redeemed_subject(code: str, verifier: str) -> str:
    """Redeem a one-time code through the fixed staging origin, then verify its JWT locally."""
    origin = env("EIGENT_CLERK_HANDOFF_ORIGIN", "")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or parsed.netloc != parsed.hostname):
        raise ClerkNotConfigured("Clerk handoff origin is not configured")
    if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22,128}", code)
            or not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier)):
        raise ClerkSessionInvalid("invalid Clerk handoff")
    try:
        response = requests.post(
            origin + "/api/eigent/redeem", json={"code": code, "verifier": verifier},
            timeout=5, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ClerkHandoffUnavailable("Clerk handoff unavailable") from exc
    if response.status_code != 200:
        raise ClerkSessionInvalid("invalid or expired Clerk handoff")
    try:
        token = response.json()["session_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ClerkHandoffUnavailable("invalid Clerk handoff response") from exc
    return verified_subject(token)
