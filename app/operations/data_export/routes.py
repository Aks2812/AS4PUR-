"""
Data Export (fifth operation) - a READ-ONLY export of tenant data to CSV: private
apps (complete, with protocol/port) and users & groups. Like Device Posture
Validation it makes no write to Netskope, so the dry-run/review-gate pattern
(CLAUDE.md Section 6) doesn't apply; what it does have instead is a result
summary shown before any download, and a completeness check that refuses to
produce a partial file.

Flow: tenant + token (the shared credential_cache flow, Section 3) -> choose an
exporter -> the export runs -> summary page -> download links.

The blocking work (Netskope calls with pacing, retries and file building) runs
in a worker thread so the event loop stays free for every other operator.

"Nothing kept on the server" and "show a summary before download" are reconciled
like this: the finished files are held IN MEMORY ONLY, on this session's
credential_cache entry, for at most RESULT_TTL_SECONDS. They are never written to
disk, are scoped to the session that ran the export (another session cannot see
them), and are dropped on logout, on a new run, or by expiry. The alternative -
re-running the whole export for every download - would multiply the calls to a
rate-limited API and could hand back a file that differs from the summary shown.

Audit: one `data_export` entry per run (who, tenant, type, options, row counts,
warnings - or the failure and its kind) and one `data_export_download` per
download (file kind, filename, row count). File contents never enter the audit
log. Exports contain user identities and internal topology, so every response
that carries or announces one is Cache-Control: no-store.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session as DBSession

from ...audit.service import log_event
from ...auth.dependencies import require_login
from ...db import get_db
from ...models import User
from ...security.csrf import get_csrf_token, verify_csrf
from ...templating import templates
from ..credential_cache import FlowCollisionError, credential_cache
from ..netskope_http import NetskopeApiError
from ..validation import clean_tenant_token
from ..wizard_expired import wizard_expired_response
from . import service
from .netskope_client import ExportIncompleteError

router = APIRouter(prefix="/operations/data-export")

FLOW = "Data Export"
ROOT = "/operations/data-export"

# How long a finished export stays downloadable (in memory only).
RESULT_TTL_SECONDS = 600

_DOWNLOAD_KINDS = ("private_apps", "users", "groups", "memberships", "users_groups_zip")
_DOWNLOAD_LABELS = {
    "private_apps": "Private apps (CSV)",
    "users": "Users (CSV)",
    "groups": "Groups (CSV)",
    "memberships": "Memberships (CSV)",
    "users_groups_zip": "All three users & groups files (ZIP)",
}
_TRUE_VALUES = {"on", "true", "1", "yes"}


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _tenant_page(request: Request, *, error: str | None = None, tenant_value: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "operations/data_export/tenant.html",
        {
            "action": f"{ROOT}/tenant",
            "csrf_token": get_csrf_token(request),
            "error": error,
            "tenant_value": tenant_value,
            "back_url": "/",
        },
        status_code=status_code,
    )


def _exporters_page(request: Request, tenant: str, *, error: str | None = None, include_deleted: bool = False, status_code: int = 200):
    return _no_store(templates.TemplateResponse(
        request,
        "operations/data_export/exporters.html",
        {
            "tenant": tenant,
            "csrf_token": get_csrf_token(request),
            "error": error,
            "include_deleted": include_deleted,
            "back_url": ROOT,
        },
        status_code=status_code,
    ))


def _gone_page(request: Request):
    return _no_store(templates.TemplateResponse(
        request, "operations/data_export/gone.html", {"restart_url": ROOT, "ttl_minutes": RESULT_TTL_SECONDS // 60}, status_code=410
    ))


def _held_result(entry) -> service.ExportResult | None:
    """The finished export for this session, if it is still within its in-memory window."""
    result = entry.data.get("export_result") if entry is not None else None
    if result is None:
        return None
    if time.monotonic() - entry.data.get("export_result_at", 0.0) >= RESULT_TTL_SECONDS:
        entry.data.pop("export_result", None)
        entry.data.pop("export_result_at", None)
        return None
    return result


def _audit(db: DBSession, request: Request, user: User, action: str, outcome: str, tenant: str, payload: dict) -> None:
    log_event(
        db, action=action, outcome=outcome, actor_username=user.username, tenant=tenant,
        detail=json.dumps(payload, sort_keys=True), source_ip=_client_ip(request),
    )


def _downloads(result: service.ExportResult) -> list[dict]:
    return [
        {"label": _DOWNLOAD_LABELS[kind], "href": f"{ROOT}/download/{kind}", "filename": f.filename, "rows": f.rows}
        for kind, f in result.files.items()
    ]


# --------------------------------------------------------------------------
# Start and tenant
# --------------------------------------------------------------------------

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
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    tenant, token, field_error = clean_tenant_token(tenant, token)
    if field_error:
        return _tenant_page(request, error=field_error, tenant_value=tenant, status_code=400)

    try:
        credential_cache.start(_session_key(request), tenant, token, flow=FLOW)
    except FlowCollisionError as exc:
        return _tenant_page(request, error=str(exc), tenant_value=tenant, status_code=400)

    return _exporters_page(request, tenant)


@router.get("/exporters")
def show_exporters(request: Request, user: User = Depends(require_login)):
    """Back-navigation target from the result page."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, ROOT)
    return _exporters_page(request, entry.tenant)


# --------------------------------------------------------------------------
# Running an export
# --------------------------------------------------------------------------

async def _run(request: Request, user: User, db: DBSession, entry, kind: str, options: dict, runner, *args, **kwargs):
    # The previous result is dropped up front: whatever this run does, an older export
    # must not stay downloadable beside a newer (or failed) one.
    entry.data.pop("export_result", None)
    entry.data.pop("export_result_at", None)
    try:
        result = await run_in_threadpool(runner, *args, **kwargs)
    except ExportIncompleteError as exc:
        _audit(db, request, user, "data_export", "failure", entry.tenant,
               {"export": kind, "options": options, "error_kind": "incomplete", "error": str(exc)[:300]})
        return _exporters_page(request, entry.tenant, error=str(exc), include_deleted=bool(options.get("include_deleted")), status_code=502)
    except NetskopeApiError as exc:
        _audit(db, request, user, "data_export", "failure", entry.tenant,
               {"export": kind, "options": options, "error_kind": "api", "error": str(exc)[:300]})
        return _exporters_page(request, entry.tenant, error=str(exc), include_deleted=bool(options.get("include_deleted")), status_code=400)

    entry.data["export_result"] = result
    entry.data["export_result_at"] = time.monotonic()
    _audit(
        db, request, user, "data_export", "success", entry.tenant,
        {
            "export": kind,
            "options": options,
            "rows": {k: f.rows for k, f in result.files.items() if f.rows is not None},
            "summary": result.summary,
            "warnings": result.warnings,
        },
    )
    return _no_store(templates.TemplateResponse(
        request,
        "operations/data_export/result.html",
        {
            "tenant": entry.tenant,
            "result": result,
            "summary": result.summary,
            "warnings": result.warnings,
            "downloads": _downloads(result),
            "ttl_minutes": RESULT_TTL_SECONDS // 60,
            "back_url": f"{ROOT}/exporters",
        },
    ))


@router.post("/private-apps")
async def run_private_apps(
    request: Request,
    user: User = Depends(require_login),
    db: DBSession = Depends(get_db),
    csrf_token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, ROOT)
    return await _run(request, user, db, entry, "private_apps", {}, service.run_private_apps_export, entry.tenant, entry.token)


@router.post("/users-groups")
async def run_users_groups(
    request: Request,
    user: User = Depends(require_login),
    db: DBSession = Depends(get_db),
    csrf_token: str = Form(default=""),
    include_deleted: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, ROOT)
    include = include_deleted.strip().lower() in _TRUE_VALUES
    return await _run(
        request, user, db, entry, "users_groups", {"include_deleted": include},
        service.run_users_groups_export, entry.tenant, entry.token, include_deleted=include,
    )


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

@router.get("/download/{kind}")
def download(kind: str, request: Request, user: User = Depends(require_login), db: DBSession = Depends(get_db)):
    if kind not in _DOWNLOAD_KINDS:
        return PlainTextResponse("Unknown export file.", status_code=404)

    entry = credential_cache.get(_session_key(request))
    result = _held_result(entry)
    if result is None or kind not in result.files:
        return _gone_page(request)

    file = result.files[kind]
    _audit(db, request, user, "data_export_download", "success", result.tenant,
           {"file": kind, "filename": file.filename, "rows": file.rows})
    return Response(
        content=file.content,
        media_type=file.content_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"', "Cache-Control": "no-store"},
    )
