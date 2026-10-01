"""
Public invite-consumption routes (Part C of the RBAC build, 2026-09-08) -
the counterpart to app/admin/routes.py's invite creation/listing/
revocation. Reached by someone who does NOT have an AS4PUR account or
session yet, so this uses the same pre-session double-submit-cookie CSRF
pattern the login page itself uses (app/security/csrf.py's
new_presession_csrf_value/verify_presession_csrf), not the post-login
synchronizer token every other form in this app uses - its own cookie
name (REGISTER_CSRF_COOKIE), never shared with login's.

No email infrastructure exists to send the registration link
automatically (explicitly out of scope, Part C spec) - the admin copies
it from admin/invites.html and shares it manually.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from ..audit.service import log_event
from ..config import settings
from ..db import get_db
from ..models import Invite, User
from ..security.csrf import REGISTER_CSRF_COOKIE, new_presession_csrf_value, verify_presession_csrf
from ..security.passwords import hash_password, validate_new_password
from ..security.tokens import hash_token
from ..templating import templates
from ..timeutil import utcnow

router = APIRouter()


def _find_invite(db: DBSession, token: str) -> Invite | None:
    return db.query(Invite).filter(Invite.token_hash == hash_token(token)).one_or_none()


def _invalid_reason(invite: Invite | None) -> str:
    """Distinct reasons, not one generic "invalid link" message - same
    philosophy as wizard_already_completed.html/wizard_expired.html
    (disambiguate causes an operator/admin can actually tell apart, per
    the earlier wizard-back-navigation work)."""
    if invite is None:
        return "not_found"
    if invite.used_at is not None:
        return "used"
    return "expired"  # covers both natural expiry and an admin revoke


def _render_invalid(request: Request, reason: str):
    return templates.TemplateResponse(request, "register_invalid.html", {"reason": reason}, status_code=400)


def _render_form(request: Request, invite: Invite, token: str, error: str | None, status_code: int = 200):
    csrf_value = new_presession_csrf_value()
    response = templates.TemplateResponse(
        request,
        "register.html",
        {"email": invite.email, "token": token, "csrf_token": csrf_value, "error": error},
        status_code=status_code,
    )
    response.set_cookie(
        REGISTER_CSRF_COOKIE,
        csrf_value,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=600,
        path=f"/register/{token}",
    )
    return response


@router.get("/register/{token}")
def register_form(token: str, request: Request, db: DBSession = Depends(get_db)):
    invite = _find_invite(db, token)
    if invite is None or invite.used_at is not None or invite.expires_at <= utcnow():
        return _render_invalid(request, _invalid_reason(invite))
    return _render_form(request, invite, token, error=None)


@router.post("/register/{token}")
def register_submit(
    token: str,
    request: Request,
    db: DBSession = Depends(get_db),
    password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    verify_presession_csrf(request, REGISTER_CSRF_COOKIE, csrf_token)

    invite = _find_invite(db, token)
    if invite is None or invite.used_at is not None or invite.expires_at <= utcnow():
        return _render_invalid(request, _invalid_reason(invite))

    error = validate_new_password(password, confirm_password)

    if error:
        return _render_form(request, invite, token, error=error, status_code=400)

    # Atomic, single-use consumption: a conditional UPDATE (used_at IS
    # NULL -> now), never a SELECT-then-check-then-UPDATE - closes the
    # race where two concurrent submits for the same invite could
    # otherwise both pass the plain-Python check above. Only proceed to
    # create the account if THIS request is the one that actually flipped
    # it (rowcount == 1) - Part C's explicit "enforce single-use at the
    # database level, not just application logic."
    result = db.execute(
        update(Invite).where(Invite.id == invite.id, Invite.used_at.is_(None)).values(used_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        return _render_invalid(request, "used")

    user = User(username=invite.email, role=invite.role, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Genuinely unexpected (an account already exists under this
        # email) rather than the ordinary double-submit race handled
        # above - fail clean rather than a raw 500.
        db.rollback()
        return _render_invalid(request, "used")

    log_event(
        db, action="registration", outcome="success", actor_username=invite.email,
        detail=f"account created via invite id={invite.id}, role={invite.role.value}",
    )

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(REGISTER_CSRF_COOKIE, path=f"/register/{token}")
    return response
