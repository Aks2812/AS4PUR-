from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DBSession

from ..audit.service import log_event
from ..config import settings
from ..db import get_db
from ..models import User
from ..operations.credential_cache import credential_cache
from ..security.csrf import LOGIN_CSRF_COOKIE, new_login_csrf_value, verify_login_csrf, verify_csrf
from ..security.passwords import verify_password
from ..security.rate_limit import login_rate_limiter
from ..security.sessions import COOKIE_NAME, cookie_kwargs, create_session, revoke_session
from ..templating import templates
from ..timeutil import utcnow

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _render_login(request: Request, error: str | None, status_code: int = 200):
    csrf_value = new_login_csrf_value()
    response = templates.TemplateResponse(
        request, "login.html", {"error": error, "csrf_token": csrf_value}, status_code=status_code
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf_value,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=600,
        path="/login",
    )
    return response


@router.get("/login")
def login_form(request: Request):
    if request.state.session is not None:
        return RedirectResponse(url="/", status_code=303)
    error = None
    # Set by require_login's AccountDeactivated redirect (Part E,
    # 2026-09-09) - safe to show plainly here (unlike login_submit's
    # deliberately generic "invalid username or password" for a bad
    # login attempt): the visitor already had a real, authenticated
    # session a moment ago, so this isn't an account-enumeration risk,
    # just telling them why they were just bounced.
    if request.query_params.get("reason") == "deactivated":
        error = "This account has been deactivated. Contact an administrator."
    return _render_login(request, error=error)


@router.post("/login")
def login_submit(
    request: Request,
    db: DBSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_login_csrf(request, csrf_token)
    ip = _client_ip(request)

    if login_rate_limiter.is_locked_out(ip, username):
        log_event(
            db, action="login_failed", outcome="failure", actor_username=username,
            detail="rate limited - too many recent attempts", source_ip=ip,
        )
        return _render_login(request, error="Too many failed attempts. Please wait a few minutes and try again.", status_code=429)

    user = db.query(User).filter(User.username == username.strip()).one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        login_rate_limiter.record_failure(ip, username)
        log_event(db, action="login_failed", outcome="failure", actor_username=username, detail="invalid credentials", source_ip=ip)
        return _render_login(request, error="Invalid username or password.", status_code=401)

    login_rate_limiter.record_success(ip, username)
    raw_token, _session = create_session(db, user)
    user.last_login_at = utcnow()
    db.commit()
    log_event(db, action="login_success", outcome="success", actor_username=user.username, source_ip=ip)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(COOKIE_NAME, raw_token, **cookie_kwargs())
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return response


@router.post("/logout")
def logout(request: Request, db: DBSession = Depends(get_db), csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    raw_token = request.cookies.get(COOKIE_NAME)
    session = request.state.session
    username = session.user.username if session else None
    if session is not None:
        # Any Netskope tenant/token an in-progress operation wizard was
        # holding in memory for this session dies with the session itself
        # (CLAUDE.md Section 3) - not just the AS4PUR login.
        credential_cache.discard(session.token_hash)
    revoke_session(db, raw_token)
    if username:
        log_event(db, action="logout", outcome="success", actor_username=username, source_ip=_client_ip(request))

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
