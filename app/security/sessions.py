"""
Server-side session management. The cookie only ever carries an opaque
random token; everything else (who it belongs to, when it expires, the
CSRF secret bound to it) lives in the user_sessions table, keyed by the
token's SHA-256 hash rather than the token itself.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import joinedload

from ..config import settings
from ..models import User, UserSession
from ..timeutil import utcnow

COOKIE_NAME = "as4pur_session"


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(db: DBSession, user: User) -> tuple[str, UserSession]:
    raw_token = secrets.token_urlsafe(32)
    now = utcnow()
    record = UserSession(
        token_hash=_hash_token(raw_token),
        user_id=user.id,
        csrf_secret=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.session_lifetime_minutes),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    _ = record.user  # force-load now, in case the caller closes this session soon after
    return raw_token, record


def get_session(db: DBSession, raw_token: str | None) -> UserSession | None:
    if not raw_token:
        return None
    record = db.get(UserSession, _hash_token(raw_token), options=[joinedload(UserSession.user)])
    if record is None:
        return None

    now = utcnow()
    if record.expires_at < now:
        db.delete(record)
        db.commit()
        return None

    # Sliding idle timeout: every authenticated request pushes expiry
    # forward, so an active operator is never logged out mid-task.
    record.last_seen_at = now
    record.expires_at = now + timedelta(minutes=settings.session_lifetime_minutes)
    db.commit()
    return record


def revoke_session(db: DBSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    record = db.get(UserSession, _hash_token(raw_token))
    if record is not None:
        db.delete(record)
        db.commit()


def revoke_all_sessions_for_user(db: DBSession, user_id: int) -> list[str]:
    """Same delete-the-row semantics as revoke_session() above, scoped by
    user_id instead of a single raw token - for an admin action that must
    invalidate every session a DIFFERENT user currently holds (Part D's
    "reset password directly", 2026-09-09: "the actual incident-response
    tool"), where only each session's stored token_hash is available,
    never the raw token itself (that's the whole point of only ever
    storing the hash - see this module's own docstring).

    Returns the token_hash of every session revoked, so the caller can
    also clear each one's credential_cache entry - kept as a separate
    step at the call site rather than folded in here, matching every
    other revocation call site in this app (logout, require_login's
    AccountDeactivated path), so this module stays about sessions only."""
    sessions = db.query(UserSession).filter(UserSession.user_id == user_id).all()
    token_hashes = [s.token_hash for s in sessions]
    for record in sessions:
        db.delete(record)
    db.commit()
    return token_hashes


def cookie_kwargs() -> dict:
    return dict(
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_lifetime_minutes * 60,
        path="/",
    )
