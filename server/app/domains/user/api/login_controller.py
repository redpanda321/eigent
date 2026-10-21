# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========

"""v1 Login - 1h access token, refresh token, rate limit."""

import time

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi_babel import _
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.core import code
from app.core.database import session
from app.core.encrypt import password_verify
from app.core.environment import env
from app.model.user.user import LoginByPasswordIn, LoginResponse, Status, User
from app.shared.auth import create_access_token, create_refresh_token
from app.shared.auth.token_blacklist import blacklist_token
from app.shared.auth.user_auth import decode_refresh_token
from app.shared.auth.user_auth import V1UserAuth, auth_must
from app.domains.user.service.clerk_session import (
    ClerkHandoffUnavailable,
    ClerkEmailUnavailable,
    ClerkNotConfigured,
    ClerkProfileUnavailable,
    ClerkSessionInvalid,
    redeemed_subject,
    verified_primary_email,
    verified_subject,
)
from app.shared.exception import TokenException, UserException
from app.shared.middleware.rate_limit import login_rate_limiter

router = APIRouter(prefix="/user", tags=["V1 Login"])


class ClerkSessionIn(BaseModel):
    session_token: str = Field(min_length=1, max_length=8192)


class ClerkHandoffIn(BaseModel):
    code: str = Field(min_length=22, max_length=128)
    verifier: str = Field(min_length=43, max_length=128)


def _resolve_clerk(call, *args) -> str:
    try:
        return call(*args)
    except ClerkNotConfigured:
        raise HTTPException(status_code=503, detail="Clerk login is not configured") from None
    except ClerkHandoffUnavailable:
        raise HTTPException(status_code=503, detail="Clerk handoff is unavailable") from None
    except ClerkProfileUnavailable:
        raise HTTPException(status_code=503, detail="Clerk user lookup is unavailable") from None
    except ClerkEmailUnavailable:
        raise HTTPException(status_code=403, detail="A verified Clerk email is required") from None
    except ClerkSessionInvalid:
        raise HTTPException(status_code=401, detail="Invalid Clerk session") from None


def _clerk_subject(token: str) -> str:
    return _resolve_clerk(verified_subject, token)


def _link_clerk_subject(subject: str, db_session: Session, auth: V1UserAuth) -> dict:
    user = auth.user
    if user.status == Status.Block or user.deleted_at is not None:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    if user.clerk_id not in (None, subject):
        raise HTTPException(status_code=409, detail="Account is already linked to another Clerk user")
    linked = db_session.exec(select(User).where(User.clerk_id == subject)).one_or_none()
    if linked is not None and linked.id != user.id:
        raise HTTPException(status_code=409, detail="Clerk user is already linked to another account")
    if user.clerk_id is None:
        user.clerk_id = subject
        db_session.add(user)
        try:
            db_session.commit()
        except IntegrityError:
            db_session.rollback()
            raise HTTPException(status_code=409, detail="Clerk user is already linked") from None
    return {"linked": True, "user_id": user.id}


def _login_clerk_subject(subject: str, db_session: Session) -> LoginResponse:
    user = db_session.exec(select(User).where(User.clerk_id == subject)).one_or_none()
    if user is None:
        email = _resolve_clerk(verified_primary_email, subject)
        if db_session.exec(select(User).where(func.lower(User.email) == email.lower())).first():
            raise HTTPException(status_code=409, detail="Existing Eigent account must be linked first")
        user = User(email=email, clerk_id=subject)
        db_session.add(user)
        try:
            db_session.commit()
            db_session.refresh(user)
        except IntegrityError:
            db_session.rollback()
            user = db_session.exec(select(User).where(User.clerk_id == subject)).one_or_none()
            if user is None:
                raise HTTPException(status_code=409, detail="Existing Eigent account must be linked first") from None
    if user.status == Status.Block or user.deleted_at is not None:
        raise HTTPException(status_code=403, detail="Clerk account is unavailable")
    return LoginResponse(token=create_access_token(user.id), email=user.email, user_id=user.id)


@router.post("/clerk/link", name="link Clerk sign-in to current account", dependencies=[login_rate_limiter])
def link_clerk_account(data: ClerkSessionIn, db_session: Session = Depends(session),
                       auth: V1UserAuth = Depends(auth_must)) -> dict:
    """Require both the existing Eigent session and a Clerk session to link."""
    return _link_clerk_subject(_clerk_subject(data.session_token), db_session, auth)


@router.post("/clerk/login", name="login with linked Clerk session", dependencies=[login_rate_limiter])
def login_with_clerk(data: ClerkSessionIn, db_session: Session = Depends(session)) -> LoginResponse:
    """Issue the existing Eigent token for an explicitly linked Clerk user."""
    return _login_clerk_subject(_clerk_subject(data.session_token), db_session)


@router.post("/clerk/redeem-link", name="link account with Clerk handoff", dependencies=[login_rate_limiter])
def link_clerk_handoff(data: ClerkHandoffIn, db_session: Session = Depends(session),
                       auth: V1UserAuth = Depends(auth_must)) -> dict:
    return _link_clerk_subject(_resolve_clerk(redeemed_subject, data.code, data.verifier), db_session, auth)


@router.post("/clerk/redeem-login", name="login with Clerk handoff", dependencies=[login_rate_limiter])
def login_clerk_handoff(data: ClerkHandoffIn, db_session: Session = Depends(session)) -> LoginResponse:
    return _login_clerk_subject(_resolve_clerk(redeemed_subject, data.code, data.verifier), db_session)


@router.post("/dev_login", name="dev login (Swagger only)", include_in_schema=True)
async def dev_login(username: str | None = Form(default=None), password: str | None = Form(default=None)):
    """Debug-only login for Swagger Authorize. Accepts OAuth2 password form."""
    if env("debug", "") != "on":
        raise HTTPException(status_code=404)
    return {"access_token": create_access_token(1), "token_type": "bearer"}


@router.post("/auto-login", name="auto login for local mode")
async def auto_login(db_session: Session = Depends(session)) -> LoginResponse:
    """Auto login for fully local mode. Returns most recently active user or creates default."""
    if env("debug", "").lower() not in {"on", "true", "1"}:
        raise HTTPException(status_code=404)
    user = User.by(
        User.status == Status.Normal,
        order_by=User.updated_at.desc(),
        limit=1,
        s=db_session,
    ).one_or_none()

    if not user:
        with db_session as s:
            try:
                user = User(
                    email="admin@local.eigent.ai",
                    username="admin",
                    nickname="Admin",
                    avatar="",
                    fullname="",
                    work_desc="",
                )
                s.add(user)
                s.commit()
                s.refresh(user)
                logger.info("Default admin user created", extra={"user_id": user.id})
            except Exception as e:
                s.rollback()
                logger.error("Failed to create default admin user", extra={"error": str(e)}, exc_info=True)
                raise UserException(code.error, _("Failed to create default user"))

    logger.info("Auto login successful", extra={"user_id": user.id, "email": user.email})
    return LoginResponse(
        token=create_access_token(user.id), email=user.email, user_id=user.id
    )


class RefreshTokenIn(BaseModel):
    refresh_token: str


@router.post("/login", name="login by email or password", dependencies=[login_rate_limiter])
async def by_password(data: LoginByPasswordIn, db_session: Session = Depends(session)) -> dict:
    """User login with email and password. Returns access_token (1h) and refresh_token (30d)."""
    user = User.by(User.email == data.email, s=db_session).one_or_none()
    if not user or not user.password or not password_verify(data.password, user.password):
        raise UserException(code.password, _("Account or password error"))
    if user.status == Status.Block:
        raise UserException(code.error, _("Your account has been blocked."))
    if user.deleted_at is not None:
        raise UserException(code.error, _("Account is unavailable."))

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "email": user.email,
    }


@router.post("/refresh", name="refresh tokens", dependencies=[login_rate_limiter])
async def refresh(data: RefreshTokenIn, db_session: Session = Depends(session)) -> dict:
    """Exchange valid refresh_token for new access_token and refresh_token."""
    if not data.refresh_token:
        raise TokenException(code.token_need, _("Refresh token required"))
    user_id, jti, exp_ts = await decode_refresh_token(data.refresh_token)
    user = db_session.get(User, user_id)
    if not user:
        raise TokenException(code.token_invalid, _("User not found"))
    if user.status == Status.Block:
        raise UserException(code.error, _("Your account has been blocked."))
    if user.deleted_at is not None:
        raise UserException(code.error, _("Account is unavailable."))
    if jti:
        ttl = max(0, exp_ts - int(time.time()))
        await blacklist_token(jti, ttl)
    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "email": user.email,
    }
