from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session as DBSession

from ...auth.dependencies import require_login
from ...db import get_db
from ...jobs.manager import job_manager
from ...jobs.service import create_job
from ...models import User
from ...security.csrf import get_csrf_token, verify_csrf
from ...templating import templates
from ...uploads import delete_upload, save_upload
from ..credential_cache import FlowCollisionError, credential_cache
from ..private_app_import.netskope_client import fetch_existing_apps, normalize_app_name
from ..validation import clean_tenant_token
from ..wizard_expired import wizard_expired_response
from . import service
from .netskope_client import NetskopeApiError, fetch_publishers
from .template_file import build_template_workbook_bytes

router = APIRouter(prefix="/operations/private-app-import")

FLOW = "Private App Import"

ALLOWED_UPLOAD_EXTENSIONS = {".xlsx"}


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


@router.get("")
def start(request: Request, user: User = Depends(require_login)):
    # Prefill tenant_value from cache ONLY if the cached entry actually
    # belongs to THIS flow - this landing page is also a real Back target
    # (see publishers.html's back link), but it's also the plain "start
    # Op1 from the dashboard" URL, and credential_cache's one slot could
    # currently hold a DIFFERENT flow's entry (e.g. an Op2 wizard still
    # in progress in another tab) - never echo another flow's tenant name
    # onto this screen.
    entry = credential_cache.get(_session_key(request))
    tenant_value = entry.tenant if entry is not None and entry.flow == FLOW else None
    return templates.TemplateResponse(
        request,
        "operations/private_app_import/tenant.html",
        {
            "action": "/operations/private-app-import/tenant",
            "csrf_token": get_csrf_token(request),
            "error": None,
            "tenant_value": tenant_value,
        },
    )


@router.get("/template.xlsx")
def download_template(user: User = Depends(require_login)):
    content = build_template_workbook_bytes()
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="AS4PUR_private_app_import_template.xlsx"'},
    )


@router.post("/tenant")
def submit_tenant(
    request: Request,
    user: User = Depends(require_login),
    # default="" not Form(...): FastAPI/python-multipart treats a
    # genuinely empty submitted value as a MISSING field (its own generic
    # 422), never reaching this route's own validation below at all -
    # normally masked by the input's HTML5 `required` attribute, but real
    # for any client that bypasses it (curl, JS disabled). See CLAUDE.md
    # Section 11. No check for this existed before this field was
    # switched - fetch_publishers() below is a live API call, so an
    # empty value must be rejected here, not left to reach it.
    tenant: str = Form(default=""),
    token: str = Form(default=""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    tenant, token, field_error = clean_tenant_token(tenant, token)
    if field_error:
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/tenant.html",
            {
                "action": "/operations/private-app-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": field_error,
                "tenant_value": tenant,
            },
            status_code=400,
        )

    try:
        publishers = fetch_publishers(tenant, token)
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/tenant.html",
            {
                "action": "/operations/private-app-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
            },
            status_code=400,
        )

    if not publishers:
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/tenant.html",
            {
                "action": "/operations/private-app-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": f"'{tenant}' has no publishers configured - at least one is required before apps can be assigned.",
                "tenant_value": tenant,
            },
            status_code=400,
        )

    try:
        credential_cache.start(_session_key(request), tenant, token, flow=FLOW)
    except FlowCollisionError as exc:
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/tenant.html",
            {
                "action": "/operations/private-app-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/publishers.html",
        {
            "action": "/operations/private-app-import/publishers",
            "csrf_token": get_csrf_token(request),
            "publishers": publishers,
            "tenant": tenant,
            "selected_publisher_ids": [],
            "back_url": "/operations/private-app-import",
            "error": None,
        },
    )


# --- Step 2: publisher(s) ---------------------------------------------------


@router.get("/publishers")
def show_publishers(request: Request, user: User = Depends(require_login)):
    """
    Back-navigation target for Step 3 (upload). Re-fetches the publisher
    list live (it's never cached in entry.data, same as the initial
    /tenant submit) and pre-checks whatever was already selected last
    time through this step, so revising a selection doesn't mean starting
    from zero checkboxes.
    """
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    try:
        publishers = fetch_publishers(entry.tenant, entry.token)
        error = None
    except NetskopeApiError as exc:
        publishers = []
        error = str(exc)

    # A publisher previously selected here could genuinely be gone from
    # the tenant by the time the operator comes Back to this step (removed,
    # renamed to a new ID, connectivity dropped it from the list) - the
    # `in` check below already handles this safely (a plain list
    # membership test, never a crash), but silently doesn't re-render it
    # as checked. Surface that explicitly rather than letting the operator
    # believe their original full selection is still intact.
    selected_publisher_ids = entry.data.get("publisher_ids", [])
    live_ids = {p.get("publisher_id") for p in publishers}
    vanished_publisher_ids = [pid for pid in selected_publisher_ids if pid not in live_ids]

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/publishers.html",
        {
            "action": "/operations/private-app-import/publishers",
            "csrf_token": get_csrf_token(request),
            "publishers": publishers,
            "tenant": entry.tenant,
            "selected_publisher_ids": selected_publisher_ids,
            "vanished_publisher_ids": vanished_publisher_ids,
            "back_url": "/operations/private-app-import",
            "error": error,
        },
    )


@router.post("/publishers")
async def submit_publishers(request: Request, user: User = Depends(require_login)):
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))

    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    publisher_ids_raw = form.getlist("publisher_id")
    if not publisher_ids_raw:
        try:
            publishers = fetch_publishers(entry.tenant, entry.token)
        except NetskopeApiError:
            publishers = []
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/publishers.html",
            {
                "action": "/operations/private-app-import/publishers",
                "csrf_token": get_csrf_token(request),
                "publishers": publishers,
                "tenant": entry.tenant,
                "selected_publisher_ids": [],
                "back_url": "/operations/private-app-import",
                "error": "Select at least one publisher before continuing.",
            },
            status_code=400,
        )

    entry.data["publisher_ids"] = [int(p) for p in publisher_ids_raw]

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/upload.html",
        {
            "action": "/operations/private-app-import/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "back_url": "/operations/private-app-import/publishers",
            "previously_processed": None,
            "continue_url": "/operations/private-app-import/confirm",
            "error": None,
        },
    )


# --- Step 3: upload ----------------------------------------------------------


@router.get("/upload")
def show_upload(request: Request, user: User = Depends(require_login)):
    """
    Back-navigation target for Step 4 (review). If a file was already
    uploaded and analyzed (entry.data["summary"] present), shows that
    fact - filename plus the will-create/skip/invalid counts - with a
    choice to continue straight to the review page or upload a different
    file below to replace those results. Never silently discards the
    cached reconciliation just because the operator looked at this step
    again.
    """
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "publisher_ids" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    previously_processed = None
    if "summary" in entry.data:
        summary = entry.data["summary"]
        previously_processed = {
            "filename": entry.data.get("input_filename"),
            "will_create": summary.will_create,
            "skipped_exists": summary.skipped_exists,
            "validation_excluded": summary.validation_excluded,
        }

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/upload.html",
        {
            "action": "/operations/private-app-import/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "back_url": "/operations/private-app-import/publishers",
            "previously_processed": previously_processed,
            "continue_url": "/operations/private-app-import/confirm",
            "error": None,
        },
    )


@router.post("/upload")
async def submit_upload(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "publisher_ids" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    upload_path = await save_upload(file, ALLOWED_UPLOAD_EXTENSIONS)
    entry.data["upload_path"] = str(upload_path)
    entry.data["input_filename"] = file.filename

    try:
        existing_claims, existing_names = service.existing_port_claims_and_names(entry.tenant, entry.token)
        summary = service.build_reconciliation(upload_path, existing_claims, existing_names)
    except (service.NormalizerError, service.NetskopeApiError) as exc:
        delete_upload(upload_path)
        entry.data.pop("upload_path", None)
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/upload.html",
            {
                "action": "/operations/private-app-import/upload",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "back_url": "/operations/private-app-import/publishers",
                "previously_processed": None,
                "continue_url": "/operations/private-app-import/confirm",
                "error": str(exc),
            },
            status_code=400,
        )

    entry.data["summary"] = summary

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/review.html",
        {
            "action": "/operations/private-app-import/confirm",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "summary": summary,
            "filename": file.filename,
            "back_url": "/operations/private-app-import/upload",
            "error": None,
        },
    )


# --- Step 4: review + confirm ------------------------------------------------


@router.get("/confirm")
def show_review(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for nothing further downstream (this is the
    last page before the job), but Step 3's "already processed, continue"
    choice links straight here rather than making the operator re-submit
    the same file."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "summary" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    return templates.TemplateResponse(
        request,
        "operations/private_app_import/review.html",
        {
            "action": "/operations/private-app-import/confirm",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "summary": entry.data["summary"],
            "filename": entry.data.get("input_filename"),
            "back_url": "/operations/private-app-import/upload",
            "error": None,
        },
    )


@router.post("/confirm")
def confirm(
    request: Request,
    user: User = Depends(require_login),
    db: DBSession = Depends(get_db),
    csrf_token: str = Form(...),
    reviewed: str | None = Form(default=None),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "summary" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/private-app-import")

    if reviewed != "yes":
        # confirm-gate.js already disables the button client-side until
        # this is checked - this is the real, server-side gate behind it
        # (CLAUDE.md Section 6: never let the UI alone be the safety net).
        return templates.TemplateResponse(
            request,
            "operations/private_app_import/review.html",
            {
                "action": "/operations/private-app-import/confirm",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "summary": entry.data["summary"],
                "filename": entry.data.get("input_filename"),
                "back_url": "/operations/private-app-import/upload",
                "error": "Please check the review box to confirm before proceeding.",
            },
            status_code=400,
        )

    entry = credential_cache.pop(session_key)  # ownership transfers to the job now
    summary = entry.data["summary"]
    publisher_ids = entry.data["publisher_ids"]

    job = create_job(
        db,
        job_type="private_app_import",
        tenant=entry.tenant,
        created_by_username=user.username,
        input_filename=entry.data.get("input_filename"),
    )
    # Recorded the moment the job exists, not after it finishes (job
    # execution is async) - see CompletedMarker's own docstring for what
    # "completed" means here.
    credential_cache.mark_completed(session_key, FLOW, job.id)

    job_manager.start(job.id, service.run_import_job, entry.tenant, entry.token, publisher_ids, summary.rows)

    # Every row's data needed for the job is already in `summary.rows` -
    # the uploaded file itself has nothing further to contribute.
    upload_path = entry.data.get("upload_path")
    if upload_path:
        delete_upload(Path(upload_path))

    return RedirectResponse(f"/jobs/{job.id}", status_code=303)
