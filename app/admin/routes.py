"""
Admin-only capability surface (Parts B-F of the RBAC build, 2026-09-08/09).
Every route here requires Depends(require_admin) - see
app/auth/dependencies.py. Explicit scope boundary (Part C spec, confirmed
in chat): nothing in this router touches Op1/2/3 usage, job visibility, or
audit-trail access - those stay exactly as they are today, unrestricted by
role, for every user regardless of this router's existence.

Part C: invite creation/listing/revocation. Part E (reordered ahead of
Part D, 2026-09-09): the "Manage users" list page and deactivate/
reactivate actions - see _user_rows/list_users/deactivate_user/
reactivate_user below. The live is_active re-check itself lives in
app/auth/dependencies.py's require_login, not here - this router only
provides the admin-facing controls that flip the flag.

Part F (2026-09-09): the minimum-one-admin guardrail and a (new, minimal
- none existed before) role-change action, both of which give deactivate_
user/change_role their own pre-check (would_leave_zero_active_admins,
below) so a normal admin sees a clean 400 rather than the raw 409 the
ORM-level guard (app/security/admin_guard.py) produces if that check is
ever bypassed - see that module's docstring for why the REAL enforcement
lives there, not here. Also: an admin can never deactivate their OWN
account, unconditionally - a separate rule from the zero-admin guardrail
(see deactivate_user's own comment for why).

Part D (2026-09-09): the two admin-triggered password-reset actions -
force_password_reset (flag only) and reset_password (temp password +
flag + kill every session the target currently holds). No self-targeting
restriction on either, unlike Part F's self-deactivation block - neither
action removes the acting admin's own admin capability, so neither
carries that same irreversibility risk (an admin CAN force-reset or
reset-password their own account; nothing here stops them).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session as DBSession

from ..audit.service import log_event
from ..auth.dependencies import require_admin
from ..config import settings
from ..db import get_db
from ..models import Invite, Role, User
from ..operations.credential_cache import credential_cache
from ..security.csrf import get_csrf_token, verify_csrf
from ..security.passwords import generate_temporary_password, hash_password
from ..security.sessions import revoke_all_sessions_for_user
from ..security.tokens import generate_token, hash_token
from ..templating import templates
from ..timeutil import utcnow

router = APIRouter(prefix="/admin")

# Input-guard only, not the security control itself (Part C spec) - real
# access control is the invite mechanism (a token only an admin generated
# and shared can be consumed), not this domain check.
INVITE_LIFETIME_HOURS = 72

INVITE_DOMAINS_SETTING = "AS4PUR_INVITE_ALLOWED_DOMAINS"
INVITE_DOMAINS_MISSING_MESSAGE = (
    f"Invites are turned off: no allowed email domain is configured. Set {INVITE_DOMAINS_SETTING} "
    "in the server's .env (comma-separated, for example example.com) and restart the app."
)


def allowed_invite_domains() -> list[str]:
    """The domains an invite may be issued for, from the AS4PUR_INVITE_ALLOWED_DOMAINS
    setting: comma-separated, case-insensitive, whitespace and a leading "@" tolerated.
    Empty (the default) means invites are refused - fail closed, never "any domain"."""
    domains = []
    for part in settings.invite_allowed_domains.split(","):
        domain = part.strip().lstrip("@").strip().lower()
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def _invite_domain_context() -> dict:
    domains = allowed_invite_domains()
    return {"invite_domains": domains, "invite_domains_missing": not domains}


def _invite_state(invite: Invite, now) -> str:
    """Pending / used / expired - deliberately no separate "revoked"
    state: revoke (see revoke_invite below) just sets expires_at into the
    past, so it's indistinguishable from natural expiry here, which is
    the correct behavior per Part C's own spec ("sets it expired
    immediately")."""
    if invite.used_at is not None:
        return "used"
    if invite.expires_at <= now:
        return "expired"
    return "pending"


def _invite_rows(db: DBSession) -> list[dict]:
    now = utcnow()
    invites = db.query(Invite).order_by(Invite.created_at.desc()).all()
    return [{"invite": inv, "state": _invite_state(inv, now)} for inv in invites]


def _user_rows(db: DBSession) -> list[User]:
    return db.query(User).order_by(User.username).all()


def _is_active_admin(target: User) -> bool:
    return target.role == Role.ADMIN and target.is_active


def _would_leave_zero_active_admins(db: DBSession, target: User) -> bool:
    """True only if `target` is CURRENTLY an active admin and is the ONLY
    one - i.e. removing target's active-admin status right now would
    leave zero. Route-level courtesy check (Part F, 2026-09-09) - the
    real, unconditional enforcement is app/security/admin_guard.py's
    before_flush event; this exists purely so the normal path returns a
    clean 400 instead of that guard's 409."""
    if not _is_active_admin(target):
        return False
    active_admin_count = db.query(User).filter(User.role == Role.ADMIN, User.is_active.is_(True)).count()
    return active_admin_count <= 1


def _users_page_context(
    db: DBSession, request: Request, error: str | None, new_temp_password: dict | None = None
) -> dict:
    return {
        "rows": _user_rows(db),
        "csrf_token": get_csrf_token(request),
        "error": error,
        "roles": list(Role),
        "new_temp_password": new_temp_password,
    }


@router.get("")
def admin_home(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/home.html", {})


@router.get("/invites")
def list_invites(request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    return templates.TemplateResponse(
        request,
        "admin/invites.html",
        {"rows": _invite_rows(db), "csrf_token": get_csrf_token(request), "error": None, "new_link": None, "roles": list(Role), **_invite_domain_context()},
    )


@router.post("/invites")
def create_invite(
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
    email: str = Form(default=""),
    role: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    email = email.strip().lower()
    domains = allowed_invite_domains()
    error = None
    role_value: Role | None = None
    if not domains:
        error = INVITE_DOMAINS_MISSING_MESSAGE
    elif not email:
        error = "Enter an email address."
    elif email.rpartition("@")[2] not in domains or not email.rpartition("@")[0]:
        error = f"Email must be an address at one of these domains: {', '.join(domains)}."
    else:
        try:
            role_value = Role(role)
        except ValueError:
            error = "Choose a valid role."

    if error:
        return templates.TemplateResponse(
            request,
            "admin/invites.html",
            {"rows": _invite_rows(db), "csrf_token": get_csrf_token(request), "error": error, "new_link": None, "roles": list(Role), **_invite_domain_context()},
            status_code=400,
        )

    raw_token = generate_token()
    now = utcnow()
    invite = Invite(
        email=email,
        role=role_value,
        token_hash=hash_token(raw_token),
        created_by=user.username,
        created_at=now,
        expires_at=now + timedelta(hours=INVITE_LIFETIME_HOURS),
    )
    db.add(invite)
    db.commit()

    log_event(
        db, action="invite_created", outcome="success", actor_username=user.username,
        detail=f"invited {email} as {role_value.value}, expires {invite.expires_at.isoformat()}",
    )

    # Rendered directly here, in the POST response body - deliberately
    # NOT a redirect-with-the-link-in-a-query-string. This app's own
    # Referrer-Policy is same-origin (SecurityHeadersMiddleware), so a
    # same-origin static asset load from a page whose OWN URL contained
    # the raw token would still send that URL as the Referer - avoided
    # entirely by never putting the token anywhere but this one response
    # body and the invite link itself.
    register_link = str(request.base_url).rstrip("/") + f"/register/{raw_token}"
    return templates.TemplateResponse(
        request,
        "admin/invites.html",
        {"rows": _invite_rows(db), "csrf_token": get_csrf_token(request), "error": None, "new_link": register_link, "roles": list(Role), **_invite_domain_context()},
    )


@router.post("/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    invite = db.get(Invite, invite_id)
    error = None
    if invite is None:
        error = "Invite not found."
    elif invite.used_at is not None:
        error = "This invite has already been used and can't be revoked."
    else:
        invite.expires_at = utcnow()
        db.commit()
        log_event(db, action="invite_revoked", outcome="success", actor_username=user.username, detail=f"invite id={invite_id} ({invite.email})")

    return templates.TemplateResponse(
        request,
        "admin/invites.html",
        {"rows": _invite_rows(db), "csrf_token": get_csrf_token(request), "error": error, "new_link": None, "roles": list(Role), **_invite_domain_context()},
        status_code=400 if error else 200,
    )


@router.get("/users")
def list_users(request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/users.html", _users_page_context(db, request, error=None))


@router.post("/users/{user_id}/deactivate")
def deactivate_user(
    user_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    target = db.get(User, user_id)
    error = None
    if target is None:
        error = "User not found."
    elif target.id == user.id:
        # Unconditional - independent of the zero-admin count below, even
        # with several other active admins remaining (Part F design
        # question, resolved 2026-09-09: recommended blocking this
        # entirely rather than only guarding the last-admin case, and
        # implemented that recommendation - see this turn's report for
        # the reasoning: self-deactivation immediately kills the acting
        # admin's own already-open session per Part E's is_active
        # re-check, with no self-service way to undo it - only another
        # admin could reactivate them. logout already covers "end my own
        # session right now" for anyone who wants that; deactivation is a
        # stronger, more permanent action that a different admin should
        # always be the one to apply.
        error = "You cannot deactivate your own account - ask another admin to do it."
    elif not target.is_active:
        error = "That user is already inactive."
    elif _would_leave_zero_active_admins(db, target):
        error = "Cannot deactivate the last active admin account - promote another account to admin first."
    else:
        target.is_active = False
        db.commit()
        log_event(
            db, action="user_deactivated", outcome="success", actor_username=user.username,
            detail=f"deactivated user id={user_id} ({target.username})",
        )
        # Deliberately NOT deleting target's existing UserSession row(s)
        # here directly - that would deny their very next request via the
        # generic "no session -> NotAuthenticated -> /login" path instead
        # of require_login's own is_active re-check (app/auth/
        # dependencies.py), which is what actually produces the specific
        # "this account has been deactivated" message (Part E spec) and
        # does the session revoke + credential_cache cleanup itself, the
        # moment that user's session is next used for anything.

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _users_page_context(db, request, error),
        status_code=400 if error else 200,
    )


@router.post("/users/{user_id}/reactivate")
def reactivate_user(
    user_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    target = db.get(User, user_id)
    error = None
    if target is None:
        error = "User not found."
    elif target.is_active:
        error = "That user is already active."
    else:
        target.is_active = True
        db.commit()
        log_event(
            db, action="user_reactivated", outcome="success", actor_username=user.username,
            detail=f"reactivated user id={user_id} ({target.username})",
        )

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _users_page_context(db, request, error),
        status_code=400 if error else 200,
    )


@router.post("/users/{user_id}/role")
def change_role(
    user_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
    role: str = Form(default=""),
):
    """The second real code path Part F's guardrail must hold across
    (deactivate_user above is the first) - no role-change action existed
    in this app before Part F; built here specifically to give the
    "changing a user's role away from admin" half of the spec something
    real to guard, per that spec's own wording."""
    verify_csrf(request, csrf_token)

    target = db.get(User, user_id)
    error = None
    new_role: Role | None = None
    if target is None:
        error = "User not found."
    else:
        try:
            new_role = Role(role)
        except ValueError:
            error = "Choose a valid role."

    if error is None and new_role == target.role:
        error = f"{target.username} is already {new_role.value}."
    elif (
        error is None
        and new_role != Role.ADMIN
        and _would_leave_zero_active_admins(db, target)
    ):
        error = "Cannot change the last active admin's role - promote another account to admin first."

    if error is None:
        old_role = target.role
        target.role = new_role
        db.commit()
        log_event(
            db, action="user_role_changed", outcome="success", actor_username=user.username,
            detail=f"changed user id={user_id} ({target.username}) role {old_role.value} -> {new_role.value}",
        )

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _users_page_context(db, request, error),
        status_code=400 if error else 200,
    )


@router.post("/users/{user_id}/force-password-reset")
def force_password_reset(
    user_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
):
    """"Force reset on next login" (Part D, 2026-09-09) - flag only. The
    current password stays valid and any session the target currently
    holds is left completely untouched - this is for a still-trusted
    account holder, not incident response (see reset_password below for
    that). require_login's own must_change_password re-check (app/auth/
    dependencies.py) is what actually funnels the target to
    /set-password on their very next request - this route does nothing
    beyond flipping the one column."""
    verify_csrf(request, csrf_token)

    target = db.get(User, user_id)
    error = None
    if target is None:
        error = "User not found."
    else:
        target.must_change_password = True
        db.commit()
        log_event(
            db, action="force_password_reset", outcome="success", actor_username=user.username,
            detail=f"flagged user id={user_id} ({target.username}) to change password on next use",
        )

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _users_page_context(db, request, error),
        status_code=400 if error else 200,
    )


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
    csrf_token: str = Form(default=""),
):
    """"Reset password directly" (Part D, 2026-09-09) - the actual
    incident-response tool. Generates a cryptographically random
    temporary password (never derived from the username or anything
    guessable - see generate_temporary_password's own docstring), hashes
    it immediately via the same Argon2 mechanism as every other password
    in this app, and shows the PLAINTEXT value exactly once, in this
    response body only - never logged, never put in audit_log's own
    `detail` field, never stored anywhere but this one hashed column.
    Also invalidates every session the target currently holds, reusing
    Part E's exact revoke mechanism (revoke_all_sessions_for_user, itself
    built on the same delete-the-row semantics as revoke_session) - a
    reset that leaves old sessions alive wouldn't accomplish much against
    a real compromise."""
    verify_csrf(request, csrf_token)

    target = db.get(User, user_id)
    error = None
    new_temp_password = None
    if target is None:
        error = "User not found."
    else:
        temp_password = generate_temporary_password()
        target.password_hash = hash_password(temp_password)
        target.must_change_password = True
        db.commit()

        revoked_token_hashes = revoke_all_sessions_for_user(db, target.id)
        for token_hash in revoked_token_hashes:
            credential_cache.discard(token_hash)

        log_event(
            db, action="password_reset_by_admin", outcome="success", actor_username=user.username,
            # Deliberately no trace of the plaintext password anywhere in
            # this detail string - only the count of sessions killed.
            detail=f"reset password for user id={user_id} ({target.username}); {len(revoked_token_hashes)} session(s) invalidated",
        )
        new_temp_password = {"username": target.username, "password": temp_password}

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _users_page_context(db, request, error, new_temp_password=new_temp_password),
        status_code=400 if error else 200,
    )
