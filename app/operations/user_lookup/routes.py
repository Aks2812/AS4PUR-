"""
User Lookup - a READ-ONLY lookup by email, UPN or hostname: the user's
user-management record, their devices (client status), and which NPA
policy rules apply to each live device.

Same shape as Device Posture Validation: tenant/token step first, kept in
credential_cache (never on disk, never logged), then any number of
lookups until the cache entry expires. Nothing is ever written to the
tenant, so there is no dry-run/review/confirm step.

lookup.py / policy.py / service.py are shared with the netskope-portal
project unchanged; only client.py and this file are AS4PUR-specific.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse

from ...auth.dependencies import require_login
from ...models import User
from ...security.csrf import get_csrf_token, verify_csrf
from ...templating import templates
from ..credential_cache import FlowCollisionError, credential_cache
from ..netskope_http import NetskopeApiError, base_url
from ..validation import clean_tenant_token
from ..wizard_expired import wizard_expired_response
from .client import TenantClient
from .lookup import classify_query, suggest_text
from .service import ApiProblem, humanize_age, match_view, run_lookup, suggest

router = APIRouter(prefix="/operations/user-lookup")

FLOW = "User Lookup"
START_URL = "/operations/user-lookup"

SUGGEST_MAX = 120          # type-ahead requests per minute per tenant session (debounced in the browser)
SUGGEST_WINDOW = 60


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


def _suggest_allowed(entry) -> bool:
    """Sliding-window limit kept with the cached tenant entry, so it goes away with the token."""
    now = time.monotonic()
    hits = [t for t in entry.data.get("user_lookup_suggest_hits", []) if now - t < SUGGEST_WINDOW]
    if len(hits) >= SUGGEST_MAX:
        entry.data["user_lookup_suggest_hits"] = hits
        return False
    entry.data["user_lookup_suggest_hits"] = hits + [now]
    return True


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "unknown"


def _tenant_page(request: Request, error: str | None = None, tenant_value: str | None = None,
                 status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "operations/user_lookup/tenant.html",
        {
            "action": f"{START_URL}/tenant",
            "csrf_token": get_csrf_token(request),
            "error": error,
            "tenant_value": tenant_value,
            "back_url": "/",
        },
        status_code=status_code,
    )


def _lookup_page(request: Request, tenant: str, status_code: int = 200, **kw):
    kw.setdefault("query_text", "")
    kw.setdefault("q", None)
    kw.setdefault("error", None)
    kw.setdefault("report", None)
    return templates.TemplateResponse(
        request,
        "operations/user_lookup/lookup.html",
        {
            "action": f"{START_URL}/lookup",
            "csrf_token": get_csrf_token(request),
            "tenant": tenant,
            "back_url": START_URL,
            **kw,
        },
        status_code=status_code,
    )


@router.get("")
def start(request: Request, user: User = Depends(require_login)):
    entry = credential_cache.get(_session_key(request))
    return _tenant_page(request, tenant_value=entry.tenant if entry is not None and entry.flow == FLOW else None)


@router.post("/tenant")
def submit_tenant(
    request: Request,
    user: User = Depends(require_login),
    tenant: str = Form(default=""),
    token: str = Form(default=""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    tenant, token, field_error = clean_tenant_token(tenant, token)
    if field_error:
        return _tenant_page(request, field_error, tenant, 400)
    try:
        base_url(tenant)                        # reject a malformed tenant name now, not on the first lookup
        credential_cache.start(_session_key(request), tenant, token, flow=FLOW)
    except (NetskopeApiError, FlowCollisionError) as exc:
        return _tenant_page(request, str(exc), tenant, 400)
    return _lookup_page(request, tenant)


@router.get("/lookup")
def show_lookup(request: Request, user: User = Depends(require_login)):
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, START_URL)
    return _lookup_page(request, entry.tenant)


@router.post("/lookup")
def submit_lookup(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    q: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, START_URL)

    query = classify_query(q)
    if query.error:
        return _lookup_page(request, entry.tenant, 400, query_text=q[:320], error=query.error)

    # Rules, private apps and classification names are cached per wizard
    # entry for a few minutes (service.CACHE_TTL), so they are not re-fetched
    # on every lookup, and are dropped together with the token.
    cache = entry.data.setdefault("user_lookup_cache", {})
    client = TenantClient(entry.tenant, entry.token)
    try:
        report = run_lookup(client, query, cache)
    except ApiProblem as exc:
        return _lookup_page(request, entry.tenant, 502, query_text=query.value, q=query, error=exc.message)
    finally:
        client.close()

    return _lookup_page(
        request, entry.tenant, query_text=query.value, q=query, report=report, now=int(time.time()),
        age=humanize_age, fmt_ts=_fmt_ts, mv=lambda m: match_view(m, report.class_names),
    )


@router.post("/suggest")
def suggestions(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(default=""),
    q: str = Form(default=""),
):
    """Type-ahead for the lookup box. POST + CSRF, so typed text stays out of URLs and logs.

    Always answers JSON ({"items": [...], "error": str | None}) - the page script
    shows "error" as a note under the box and never parses anything as HTML.
    """
    verify_csrf(request, csrf_token)
    entry = credential_cache.get(_session_key(request))
    if entry is None or entry.flow != FLOW:
        return JSONResponse({"items": [], "error": "Your tenant session expired. Go back and enter the tenant and token again."},
                            status_code=410)
    if not _suggest_allowed(entry):
        return JSONResponse({"items": [], "error": "Too many suggestions requested. Keep typing and press Look up."},
                            status_code=429)
    text = suggest_text(q)
    if text is None:
        return JSONResponse({"items": [], "error": None})
    cache = entry.data.setdefault("user_lookup_cache", {})
    try:
        result = suggest(lambda: TenantClient(entry.tenant, entry.token), text, cache)
    except ApiProblem as exc:
        return JSONResponse({"items": [], "error": exc.message}, status_code=502)
    return JSONResponse(result)
