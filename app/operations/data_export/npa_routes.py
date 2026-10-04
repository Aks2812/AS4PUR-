"""
"Users per NPA policy" - the HTTP layer of this Data Export sub-feature (service: npa_service.py,
network: npa_client.py). Read-only against Netskope.

Flow: tenant + token (the Data Export step, held by credential_cache) -> POST policies (one call to
read the NPA policies; shown as a searchable multi-select) -> POST preview (what the run will do,
before any heavy call) -> POST start (a background job, `app/jobs`, with visible progress) -> run page
(progress, then summary, warnings and the download) -> download.

SENSITIVE DATA / TODO (RBAC): this export lists the email address of every user a policy applies to.
The gate is `require_login` only, exactly like every other operation, because role enforcement is
still deferred app-wide (CLAUDE.md Section 11). The role gate from the backlog is REQUIRED before any
account other than the current few operators is invited to this app.

State, all in process memory on the session's credential_cache entry (the app runs ONE worker):
  npa_rules          the policy list read at step 1, so preview/start use exactly what was shown
                     (expires after RULES_TTL_SECONDS)
  npa_result         the finished CSV (expires after RESULT_TTL_SECONDS; replaced by a new run;
                     gone with the entry on logout)
  npa_running_job    set while a run is in flight (one export at a time per session)
Nothing is written to disk. The audit entry is a Job row (type `data_export_npa_users`, the tenant name in
jobs.tenant exactly as the other operations record it, item keys like `group-2`, no names, no emails, no
token) plus one `data_export_npa_users_download` audit_log row per download (also with the tenant). The
tenant name is in those two columns and nowhere else, and never in the file name.

Error text: nothing Netskope wrote is stored, logged or shown. A failure becomes an `ExportFailure` whose
message is built from a phase, a short category and an HTTP status code only (npa_client), so it is safe in
jobs.error_message and on screen - the detailed upstream text is not shown at all, because the only scrub
available (token, tenant, the selected users/groups/OUs) cannot catch an unrelated email or group name that
Netskope mentions. That scrub still runs over the fixed message as defence in depth.

Logging: the only log statements here record an exception TYPE, never its text. The job manager prints the
traceback of a failed job to stderr, so the job's exception is raised `from None` with the fixed message.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session as DBSession

from ...audit.service import log_event
from ...auth.dependencies import require_login
from ...db import get_db
from ...jobs.manager import job_manager
from ...jobs.service import create_job
from ...models import Job, User
from ...security.csrf import get_csrf_token, verify_csrf
from ...templating import templates
from ..credential_cache import credential_cache
from ..netskope_http import NetskopeApiError
from ..wizard_expired import wizard_expired_response
from . import npa_client, npa_service
from .netskope_client import ExportIncompleteError
from .npa_client import ExportLimitError
from .routes import FLOW, RESULT_TTL_SECONDS, ROOT, _client_ip, _exporters_page, _gone_page, _no_store, _session_key, router

logger = logging.getLogger(__name__)

NPA = f"{ROOT}/npa-users"
_PATH = "/npa-users"
RULES_TTL_SECONDS = 600


class ExportFailed(Exception):
    """What the job thread raises so the job manager records a clean, already-scrubbed message."""


# --------------------------------------------------------------------------
# State held in memory
# --------------------------------------------------------------------------

def _entry(request: Request):
    key = _session_key(request)
    entry = credential_cache.get(key)
    if entry is None or entry.flow != FLOW:
        return None, wizard_expired_response(request, key, ROOT)
    return entry, None


def _held_rules(entry) -> dict | None:
    held = entry.data.get("npa_rules")
    if held is None:
        return None
    if time.monotonic() - held["at"] >= RULES_TTL_SECONDS:
        entry.data.pop("npa_rules", None)
        return None
    return held


def _held_result(entry, job_id: str) -> dict | None:
    held = entry.data.get("npa_result") if entry is not None else None
    if held is None:
        return None
    if time.monotonic() - held["at"] >= RESULT_TTL_SECONDS:
        entry.data.pop("npa_result", None)
        return None
    return held if held["job_id"] == job_id else None


def _clean(entry, text) -> str:
    return npa_service.scrub(text, entry.token if entry else None, entry.tenant if entry else None)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def _select_page(request, held, tenant, *, selected=(), error=None, status_code=200):
    views = sorted(held["views"], key=lambda v: (v.name.lower(), v.rule_id))
    return _no_store(templates.TemplateResponse(
        request, "operations/data_export/npa_select.html",
        {
            "tenant": tenant, "action": f"{NPA}/preview", "csrf_token": get_csrf_token(request), "views": views,
            "declared_total": held["declared_total"], "selected": set(selected), "error": error, "back_url": f"{ROOT}/exporters",
        },
        status_code=status_code,
    ))


def _expired_list_page(request, entry):
    return _exporters_page(request, entry.tenant, error="The policy list expired (it is only kept for a few minutes). Choose \"Users per NPA policy\" again to load it again.", status_code=410)


def _selection(held, ids: list[str]):
    if not ids:
        return None, "Select at least one policy."
    known = {v.rule_id: v for v in held["views"]}
    ordered: list[str] = []
    for rid in ids:
        if rid not in known:
            return None, "One or more of the selected policies is not in the list that was loaded. Go back to the list and choose again."
        if rid not in ordered:
            ordered.append(rid)
    return [known[rid] for rid in ordered], None


# --------------------------------------------------------------------------
# 1. The policy list
# --------------------------------------------------------------------------

@router.post(f"{_PATH}/policies")
def npa_load_policies(request: Request, user: User = Depends(require_login), csrf_token: str = Form(default="")):
    verify_csrf(request, csrf_token)
    entry, expired = _entry(request)
    if expired is not None:
        return expired
    try:
        fetched = npa_client.fetch_npa_rules(entry.tenant, entry.token)
    except (ExportLimitError, ExportIncompleteError, NetskopeApiError) as exc:
        failure = npa_client.failure_for(exc, npa_client.PHASE_POLICIES)            # classified by structure; Netskope's text is dropped
        return _exporters_page(
            request, entry.tenant, error=_clean(entry, failure),
            status_code=502 if failure.category == npa_client.MISMATCH else 400,
        )
    except Exception as exc:
        logger.error("Users-per-NPA-policy policy list failed unexpectedly (%s)", type(exc).__name__)     # the type only, never its text
        return _exporters_page(
            request, entry.tenant, error="Something unexpected went wrong while loading the policies. Nothing was exported.",
            status_code=500,
        )
    held = {"views": npa_service.policy_views(fetched.rules), "declared_total": fetched.declared_total, "at": time.monotonic()}
    entry.data["npa_rules"] = held
    return _select_page(request, held, entry.tenant)


@router.get(f"{_PATH}/policies")
def npa_show_policies(request: Request, user: User = Depends(require_login)):
    """Back-navigation target: the list read earlier, from memory, with no new call."""
    entry, expired = _entry(request)
    if expired is not None:
        return expired
    held = _held_rules(entry)
    if held is None:
        return _expired_list_page(request, entry)
    return _select_page(request, held, entry.tenant)


# --------------------------------------------------------------------------
# 2. Preview, before the heavy work
# --------------------------------------------------------------------------

@router.post(f"{_PATH}/preview")
def npa_preview(request: Request, user: User = Depends(require_login), csrf_token: str = Form(default=""), rule_id: list[str] = Form(default=[])):
    verify_csrf(request, csrf_token)
    entry, expired = _entry(request)
    if expired is not None:
        return expired
    held = _held_rules(entry)
    if held is None:
        return _expired_list_page(request, entry)
    selected, error = _selection(held, rule_id)
    if error:
        return _select_page(request, held, entry.tenant, selected=rule_id, error=error, status_code=400)
    return _no_store(templates.TemplateResponse(
        request, "operations/data_export/npa_preview.html",
        {
            "tenant": entry.tenant, "action": f"{NPA}/start", "csrf_token": get_csrf_token(request), "selected": selected,
            "preview": npa_service.preview(selected), "max_calls": npa_client.MAX_API_CALLS, "max_rows": npa_service.MAX_ROWS,
            "back_url": f"{NPA}/policies",
        },
    ))


# --------------------------------------------------------------------------
# 3. The run (a background job with visible progress)
# --------------------------------------------------------------------------

def _npa_target(progress, tenant: str, token: str, views, data: dict) -> None:
    """The job thread's work. Whatever goes wrong becomes ExportFailed with a scrubbed message raised
    `from None`, so the job manager's traceback print can show neither the token nor the tenant. The message
    is stored in the database, so it is scrubbed of the selected policies' users, groups and organizational
    units as well: Netskope's error text can echo a group name back."""
    try:
        result = npa_service.run_npa_users_export(tenant, token, views, progress)
        data["npa_result"] = {"job_id": progress.job_id, "result": result, "at": time.monotonic()}
    except npa_client.ExportFailure as failure:
        # Fixed text from known parts (phase, category, status). The scrub is defence in depth.
        raise ExportFailed(npa_service.scrub(failure, token, tenant, *npa_service.user_derived_strings(views))) from None
    except Exception as exc:
        logger.error("Users-per-NPA-policy export failed unexpectedly (%s)", type(exc).__name__)
        raise ExportFailed("Something unexpected went wrong while building the export. Nothing was produced.") from None
    finally:
        data.pop("npa_running_job", None)


@router.post(f"{_PATH}/start")
def npa_start(
    request: Request, user: User = Depends(require_login), db: DBSession = Depends(get_db),
    csrf_token: str = Form(default=""), rule_id: list[str] = Form(default=[]),
):
    verify_csrf(request, csrf_token)
    entry, expired = _entry(request)
    if expired is not None:
        return expired
    held = _held_rules(entry)
    if held is None:
        return _expired_list_page(request, entry)
    selected, error = _selection(held, rule_id)
    if error:
        return _select_page(request, held, entry.tenant, selected=rule_id, error=error, status_code=400)
    if entry.data.get("npa_running_job"):
        return _select_page(request, held, entry.tenant, selected=rule_id, error="An export is already running for this session. Wait for it to finish.", status_code=409)

    entry.data.pop("npa_result", None)                       # an older export must not stay downloadable beside a newer one
    job = create_job(db, job_type=npa_service.JOB_TYPE, tenant=entry.tenant, created_by_username=user.username)
    entry.data["npa_running_job"] = job.id
    job_manager.start(job.id, _npa_target, entry.tenant, entry.token, selected, entry.data)
    return RedirectResponse(f"{NPA}/run/{job.id}", status_code=303)


@router.get(f"{_PATH}/run/{{job_id}}")
def npa_run(job_id: str, request: Request, user: User = Depends(require_login), db: DBSession = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or job.job_type != npa_service.JOB_TYPE:
        return PlainTextResponse("Unknown export.", status_code=404)
    entry = credential_cache.get(_session_key(request))
    if entry is not None and entry.flow != FLOW:
        entry = None
    held = _held_result(entry, job_id)
    message = (job.error_message or "").partition(": ")[2] or (job.error_message or "")
    return _no_store(templates.TemplateResponse(
        request, "operations/data_export/npa_run.html",
        {
            "tenant": job.tenant,                       # the run's own tenant (the job row has always recorded it), not whatever the session holds now
            "job": job, "state": job.status.value, "held": held, "error_text": _clean(entry, message),
            "download_href": f"{NPA}/download/{job_id}", "ttl_minutes": RESULT_TTL_SECONDS // 60,
            "back_url": f"{ROOT}/exporters",
        },
    ))


# --------------------------------------------------------------------------
# 4. Download
# --------------------------------------------------------------------------

@router.get(f"{_PATH}/download/{{job_id}}")
def npa_download(job_id: str, request: Request, user: User = Depends(require_login), db: DBSession = Depends(get_db)):
    entry = credential_cache.get(_session_key(request))
    held = _held_result(entry, job_id) if entry is not None and entry.flow == FLOW else None
    if held is None:
        return _gone_page(request)
    file = held["result"].file
    log_event(
        db, action="data_export_npa_users_download", outcome="success", actor_username=user.username, tenant=entry.tenant,
        detail=json.dumps({"job_id": job_id, "rows": file.rows}, sort_keys=True), source_ip=_client_ip(request),
    )
    return Response(
        content=file.content, media_type=file.content_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"', "Cache-Control": "no-store"},
    )
