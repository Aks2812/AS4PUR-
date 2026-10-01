"""
CSRF protection (CLAUDE.md Section 5: every state-changing form
submission). Two mechanisms, because there are two situations:

- Post-login: a synchronizer token. Each session already has a random
  `csrf_secret` (app/security/sessions.py); every form embeds it in a
  hidden field, and the server compares the submitted value against the
  session's own secret. Requires a session, so this covers everything
  after login (logout, and every operation's forms once Phase 1+ builds
  them).

- The login form itself, and (as of Part C, 2026-09-08) invite-based
  registration: there is no session yet before a successful login/
  registration, so the synchronizer pattern above doesn't apply ("login
  CSRF" - an attacker forcing a victim's browser to authenticate as the
  attacker's own account - is a real, if less obvious, variant of the
  same class of attack, and applies just as much to registration). A
  double-submit cookie covers both: the pre-session page sets a
  short-lived, httponly cookie holding a random value and renders the
  same value into a hidden field; the submit handler checks they still
  match. `new_presession_csrf_value`/`verify_presession_csrf` are the
  shared mechanism; login and registration each use their own cookie name
  (never share one) so the two flows can't interfere with each other if
  both happen to be open in different tabs.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

LOGIN_CSRF_COOKIE = "as4pur_login_csrf"
REGISTER_CSRF_COOKIE = "as4pur_register_csrf"


def get_csrf_token(request: Request) -> str:
    """Reads the CSRF token bound to the current (post-login) session, for
    embedding into a form. Requires request.state.session to be set."""
    session = getattr(request.state, "session", None)
    if session is None:
        raise RuntimeError("get_csrf_token() called with no session on the request")
    return session.csrf_secret


def verify_csrf(request: Request, submitted_token: str | None) -> None:
    session = getattr(request.state, "session", None)
    if session is None or not submitted_token or not secrets.compare_digest(submitted_token, session.csrf_secret):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your session may have expired or this form is out of date - please reload the page and try again.",
        )


def new_presession_csrf_value() -> str:
    return secrets.token_urlsafe(24)


def verify_presession_csrf(request: Request, cookie_name: str, submitted_token: str | None) -> None:
    cookie_value = request.cookies.get(cookie_name)
    if not cookie_value or not submitted_token or not secrets.compare_digest(cookie_value, submitted_token):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This form is out of date - please reload the page and try again.",
        )


def new_login_csrf_value() -> str:
    return new_presession_csrf_value()


def verify_login_csrf(request: Request, submitted_token: str | None) -> None:
    verify_presession_csrf(request, LOGIN_CSRF_COOKIE, submitted_token)
