from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DBSession

from ..db import get_db
from ..models import Role, User
from ..operations.credential_cache import credential_cache
from ..security.sessions import COOKIE_NAME, revoke_session


class NotAuthenticated(Exception):
    """Raised by require_login; app/main.py turns this into a 303 redirect
    to /login rather than a bare 401, since every route here is a browser
    page, not a JSON API."""


class AccountDeactivated(NotAuthenticated):
    """Raised by require_login specifically when an otherwise-valid
    session's user has been deactivated (Part E of the RBAC build,
    2026-09-09) - a subclass of NotAuthenticated, not a sibling, so it's
    still caught by app/main.py's existing single exception_handler
    (Starlette walks the exception's MRO looking for a registered
    handler), but distinguishable there so the redirect can carry a clear
    "this account has been deactivated" reason instead of the generic
    "please log in" bounce a plain missing/expired session gets."""


class PasswordChangeRequired(NotAuthenticated):
    """Raised by require_login specifically when the session's user has
    must_change_password=True (Part D, 2026-09-09) - same MRO-based
    catch-by-app/main.py's-single-handler trick as AccountDeactivated
    above, distinguished there so the redirect goes to the mandatory
    set-new-password page instead of /login.

    Deliberately does NOT touch the session the way AccountDeactivated
    does (no revoke_session, no credential_cache.discard, no clearing
    request.state.session) - Part D's spec is explicit that flagging
    must_change_password leaves "current password... current session (if
    any)... untouched." The session stays exactly as valid as it was;
    this exception only ever redirects it to one specific page instead of
    everywhere else, on every request, until that page is completed."""


def _require_active_session(request: Request, db: DBSession) -> User:
    """Shared by require_login and
    require_login_allow_pending_password_change below - session
    existence + the is_active re-check only. Split out so the
    must_change_password check (require_login only) doesn't have to be
    duplicated, or skipped, in the one dependency that must NOT enforce
    it (the mandatory set-password page itself)."""
    session = request.state.session
    if session is None:
        raise NotAuthenticated()
    if not session.user.is_active:
        # CLAUDE.md's explicit Part E requirement: is_active must be
        # re-checked on every authenticated request, not just at login
        # (login_submit, app/auth/routes.py, already checked it there -
        # this closes the other half: an already-open session surviving
        # deactivation until it naturally expires). Revoke the session
        # outright rather than just refusing this one request - the same
        # "session dies, everything scoped to it dies with it" principle
        # logout() already follows (credential_cache.discard alongside
        # revoke_session), applied here to a forced deactivation instead
        # of a voluntary logout.
        revoke_session(db, request.cookies.get(COOKIE_NAME))
        credential_cache.discard(session.token_hash)
        request.state.session = None
        raise AccountDeactivated()
    return session.user


def require_login(request: Request, db: DBSession = Depends(get_db)) -> User:
    user = _require_active_session(request, db)
    if user.must_change_password:
        raise PasswordChangeRequired()
    return user


def require_login_allow_pending_password_change(request: Request, db: DBSession = Depends(get_db)) -> User:
    """Identical to require_login except it does NOT redirect away for
    must_change_password=True - the ONLY dependency app/auth/
    password_change.py's mandatory set-new-password route may use, since
    that page must stay reachable precisely because that flag is set.
    Every other authenticated route in this app must keep using plain
    require_login, never this."""
    return _require_active_session(request, db)


def require_role(*roles: Role):
    """
    Not applied to any route yet in v1 (operator's explicit decision to
    build the role column now, defer enforcement) - see User.role in
    models.py. Fully functional so a later phase can attach it to a route
    the same way require_login is used, without revisiting this file.
    """

    def _dependency(user: User = Depends(require_login)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have permission to perform this action.")
        return user

    return _dependency


# First real caller of require_role (2026-09-08, Part B of the RBAC build)
# - a plain module-level dependency, same usage shape as require_login
# (`Depends(require_admin)`, no factory call needed at each route), rather
# than every admin-only route in Parts C-F writing its own
# `Depends(require_role(Role.ADMIN))`. Reuses require_role's existing
# enforcement/error-handling exactly (same 403, same message, same
# require_login-first ordering) instead of a second, slightly-different
# admin-check pattern living alongside it.
require_admin = require_role(Role.ADMIN)
