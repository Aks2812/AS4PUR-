"""
Part D of the RBAC build (2026-09-09): the mandatory "set a new password"
page. Reached by an already-logged-in user whose must_change_password
flag is true - require_login (app/auth/dependencies.py) redirects EVERY
other route here via PasswordChangeRequired the moment that flag is set,
so this file uses the one dependency exempted from that redirect
(require_login_allow_pending_password_change), or nothing here would ever
be reachable.

Because the user already has a real, valid session by the time they reach
this page (unlike registration, which happens before any session exists),
this uses the ordinary post-login synchronizer-token CSRF pattern
(get_csrf_token/verify_csrf, session.csrf_secret) - not registration's
pre-session double-submit cookie. See app/security/csrf.py's own
docstring for why the two mechanisms exist and when each applies.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DBSession

from ..audit.service import log_event
from ..auth.dependencies import require_login_allow_pending_password_change
from ..db import get_db
from ..models import User
from ..security.csrf import get_csrf_token, verify_csrf
from ..security.passwords import hash_password, validate_new_password
from ..templating import templates

router = APIRouter()


@router.get("/set-password")
def set_password_form(
    request: Request, user: User = Depends(require_login_allow_pending_password_change)
):
    if not user.must_change_password:
        # Nothing left to do here - send them back where every other
        # route already sends a fully-set-up user.
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "set_password.html", {"csrf_token": get_csrf_token(request), "error": None}
    )


@router.post("/set-password")
def set_password_submit(
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_login_allow_pending_password_change),
    password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    if not user.must_change_password:
        return RedirectResponse(url="/", status_code=303)

    error = validate_new_password(password, confirm_password)
    if error:
        return templates.TemplateResponse(
            request,
            "set_password.html",
            {"csrf_token": get_csrf_token(request), "error": error},
            status_code=400,
        )

    # `user` above came from request.state.session.user, which the
    # middleware loaded through its OWN, already-closed SessionLocal()
    # instance (see app/middleware.py) - detached from `db` here.
    # Mutating and committing IT directly would silently do nothing (db's
    # unit of work never tracked that object), exactly the class of bug
    # a plain assert-then-refresh test caught during Part D's own
    # verification. Every write in this app goes through an object
    # fetched via the SAME session that will commit it - re-fetch here
    # too, the same way every admin route re-fetches `target` rather than
    # writing to the acting admin's own Depends()-provided user object.
    db_user = db.get(User, user.id)
    db_user.password_hash = hash_password(password)
    # The ONLY thing anywhere in this app that clears this flag - see
    # models.py's own comment on must_change_password.
    db_user.must_change_password = False
    db.commit()

    log_event(
        db, action="password_changed", outcome="success", actor_username=db_user.username,
        detail="completed the mandatory must_change_password flow",
    )

    return RedirectResponse(url="/", status_code=303)
