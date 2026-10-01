from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .db import SessionLocal
from .security.sessions import COOKIE_NAME, get_session


class SessionLookupMiddleware(BaseHTTPMiddleware):
    """
    Resolves the current UserSession (if any) once per request and stashes
    it on request.state.session, so every route and template can rely on
    it being present (None when not logged in) without repeating the
    lookup. This is also what makes `request.state.session` safe to render
    in base.html on every page, including the login page itself.
    """

    async def dispatch(self, request: Request, call_next):
        raw_token = request.cookies.get(COOKIE_NAME)
        with SessionLocal() as db:
            request.state.session = get_session(db, raw_token)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security headers to every response (WSTG-CONF finding, fixed
    2026-09-08 - previously set in neither this app nor
    deploy/nginx_as4pur.conf, a confirmed total gap).

    Set here, in application middleware, rather than in the Nginx config,
    deliberately:
    - This project's entire test suite drives every request through
      FastAPI's own TestClient, never through Nginx - headers set only in
      nginx_as4pur.conf would be completely unverified by this project's
      established "test small, confirm" discipline (CLAUDE.md Section 9).
    - Headers set in application code travel with the app regardless of
      how it's fronted - a future infra change, or someone editing the
      checked-in Nginx config without realizing these lines matter, can't
      silently regress this the way it could if the headers lived only in
      deploy/nginx_as4pur.conf.

    HSTS is included unconditionally, not conditioned on the request's own
    scheme: confirmed via deploy/nginx_as4pur.conf (TLS terminates there;
    plain HTTP on :80 redirects to :443) and deploy/as4pur.service
    (`--host 127.0.0.1` - Uvicorn is never reachable from the LAN
    directly, only through Nginx's reverse proxy) that the real
    deployment is HTTPS-always. Setting HSTS unconditionally would be
    actively wrong for a plain-HTTP-only deployment; that is not this one.

    CSP has no 'unsafe-inline' anywhere - confirmed via a full templates-
    tree grep that this app has zero inline <script>/<style> blocks and
    zero inline style=/onclick= attributes; every script tag is an
    external same-origin /static/js/*.js file.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response
