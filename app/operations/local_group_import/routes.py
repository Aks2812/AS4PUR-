"""
Local Group/User Import (Operation 3) - built against CLAUDE.md Section
7's confirmed 9-step workflow:

  1. tenant + token
  2. fetch + display existing SCIM groups (live pre-flight, same
     discipline as Operations 1 and 2)
  3. required acknowledgment ("not managed via Entra/SCIM sync")
  4. group choice: no group / existing group / create a new group
  (steps 2-4 combined onto one page - "existing group" needs step 2's
  list to choose from, and the acknowledgment gates the whole page, so
  splitting them into separate round-trips would add clicks without
  adding information.)
  5. upload CSV or XLSX (header-name-driven "Email" column)
  6-7. validation pass + review page, same explicit-review-before-write
     gate as every other operation
  8. confirm -> batched background job, progress visible
  9. audit report
"""
from __future__ import annotations

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
from ..validation import clean_tenant_token
from ..wizard_expired import wizard_expired_response
from . import service
from .netskope_client import NetskopeApiError, fetch_groups
from .template_file import build_template_workbook_bytes

router = APIRouter(prefix="/operations/local-group-import")

FLOW = "Local Group/User Import"

ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".xlsx"}


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


@router.get("")
def start(request: Request, user: User = Depends(require_login)):
    # See private_app_import/routes.py's own start() for why this checks
    # entry.flow before prefilling - credential_cache's one slot could
    # currently hold a DIFFERENT flow's entry entirely.
    entry = credential_cache.get(_session_key(request))
    tenant_value = entry.tenant if entry is not None and entry.flow == FLOW else None
    return templates.TemplateResponse(
        request,
        "operations/local_group_import/tenant.html",
        {
            "action": "/operations/local-group-import/tenant",
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
        headers={"Content-Disposition": 'attachment; filename="AS4PUR_local_group_import_template.xlsx"'},
    )


# --- Step 1: tenant + token, step 2: SCIM groups pre-flight ---------------


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
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/tenant.html",
            {
                "action": "/operations/local-group-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": field_error,
                "tenant_value": tenant,
            },
            status_code=400,
        )

    try:
        groups = fetch_groups(tenant, token)
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/tenant.html",
            {
                "action": "/operations/local-group-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
            },
            status_code=400,
        )

    session_key = _session_key(request)
    try:
        credential_cache.start(session_key, tenant, token, flow=FLOW)
    except FlowCollisionError as exc:
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/tenant.html",
            {
                "action": "/operations/local-group-import/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
            },
            status_code=400,
        )
    credential_cache.get(session_key).data["groups"] = groups

    return templates.TemplateResponse(
        request,
        "operations/local_group_import/group_setup.html",
        {
            "action": "/operations/local-group-import/group-setup",
            "csrf_token": get_csrf_token(request),
            "tenant": tenant,
            "groups": groups,
            "back_url": "/operations/local-group-import",
            "selected_group_choice": None,
            "new_group_name_value": "",
            "error": None,
        },
    )


# --- Steps 3-4: acknowledgment + group choice -----------------------------


def _selected_group_choice(entry_data: dict) -> str | None:
    """
    Best-effort reconstruction of the radio value the operator originally
    picked (`"none"`, `"new"`, or `"existing_<id>"`), for pre-selecting on
    Back. entry.data["group_mode"] holds service.resolve_group_choice()'s
    EFFECTIVE mode ("none" | "existing" | "create"), not the raw radio
    value - "create" maps back to the "new" radio here, which is faithful
    for the normal case. Not perfectly faithful in one narrow, rare case:
    if "create a new group" collided with an already-existing group of
    that name, the effective mode is "existing" instead of "create" - Back
    would then show "existing group" pre-selected instead of "new", which
    is a reasonable reflection of what actually happened but not a
    pixel-perfect replay of the original click. Flagged, not fixed: fixing
    it would mean caching the raw, pre-resolution form value separately
    for no real behavioral benefit.
    """
    mode = entry_data.get("group_mode")
    if mode is None:
        return None
    if mode == "existing":
        return f"existing_{entry_data.get('group_id')}"
    if mode == "create":
        return "new"
    return mode  # "none"


@router.get("/group-setup")
def show_group_setup(request: Request, user: User = Depends(require_login)):
    """
    Back-navigation target for Step 5 (upload). The groups list itself is
    already cached (entry.data["groups"], set once at /tenant) so this
    doesn't need a live re-fetch. Pre-selects the previously-chosen group
    option and pre-fills the new-group-name text field - but deliberately
    does NOT pre-check the acknowledgment checkbox even if it was already
    checked once: that box is a submission-time safety gate (CLAUDE.md
    Section 6/7), and silently re-checking it on the operator's behalf
    would quietly remove the exact friction that gate exists to provide.
    """
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "groups" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

    return templates.TemplateResponse(
        request,
        "operations/local_group_import/group_setup.html",
        {
            "action": "/operations/local-group-import/group-setup",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "groups": entry.data["groups"],
            "back_url": "/operations/local-group-import",
            "selected_group_choice": _selected_group_choice(entry.data),
            "new_group_name_value": entry.data.get("group_name") or "",
            "error": None,
        },
    )


@router.post("/group-setup")
async def submit_group_setup(request: Request, user: User = Depends(require_login)):
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))

    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "groups" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

    acknowledged = form.get("acknowledged")
    group_choice = (form.get("group_choice") or "").strip()
    new_group_name = (form.get("new_group_name") or "").strip()

    mode: str | None
    existing_group_id: str | None = None
    if group_choice == "none":
        mode = "none"
    elif group_choice == "new":
        mode = "new"
    elif group_choice.startswith("existing_"):
        mode = "existing"
        existing_group_id = group_choice[len("existing_"):]
    else:
        mode = None

    errors = []
    if acknowledged != "yes":
        errors.append(
            "You must acknowledge that these users are not managed via this tenant's Entra ID/SCIM sync before continuing."
        )
    if mode is None:
        errors.append("Choose a group option before continuing.")
    elif mode == "new" and not new_group_name:
        errors.append("Enter a name for the new group.")

    if errors:
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/group_setup.html",
            {
                "action": "/operations/local-group-import/group-setup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "groups": entry.data["groups"],
                "back_url": "/operations/local-group-import",
                "selected_group_choice": group_choice or None,
                "new_group_name_value": new_group_name,
                "error": " ".join(errors),
            },
            status_code=400,
        )

    try:
        effective_mode, group_id, group_name, note = service.resolve_group_choice(
            entry.tenant, entry.token, mode, existing_group_id, new_group_name or None
        )
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/group_setup.html",
            {
                "action": "/operations/local-group-import/group-setup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "groups": entry.data["groups"],
                "back_url": "/operations/local-group-import",
                "selected_group_choice": group_choice or None,
                "new_group_name_value": new_group_name,
                "error": str(exc),
            },
            status_code=400,
        )

    entry.data["group_mode"] = effective_mode
    entry.data["group_id"] = group_id
    entry.data["group_name"] = group_name
    entry.data["group_note"] = note

    return templates.TemplateResponse(
        request,
        "operations/local_group_import/upload.html",
        {
            "action": "/operations/local-group-import/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "group_note": note,
            "back_url": "/operations/local-group-import/group-setup",
            "previously_processed": None,
            "continue_url": "/operations/local-group-import/confirm",
            "error": None,
        },
    )


# --- Step 5-7: upload, validation pass, review ----------------------------


@router.get("/upload")
def show_upload(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 6 (review). Same already-processed
    treatment as the other two operations' upload steps - see
    private_app_import/routes.py's show_upload for the full rationale."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "group_mode" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

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
        "operations/local_group_import/upload.html",
        {
            "action": "/operations/local-group-import/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "group_note": entry.data.get("group_note"),
            "back_url": "/operations/local-group-import/group-setup",
            "previously_processed": previously_processed,
            "continue_url": "/operations/local-group-import/confirm",
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
    if entry is None or "group_mode" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

    upload_path = await save_upload(file, ALLOWED_UPLOAD_EXTENSIONS)
    entry.data["upload_path"] = str(upload_path)
    entry.data["input_filename"] = file.filename

    try:
        rows = service.parse_email_file(upload_path, file.filename)
        service.mark_intra_file_duplicates(rows)
        service.validate_email_formats(rows)
        service.check_existing_users(rows, entry.tenant, entry.token)
        summary = service.build_validation_summary(rows)
    except (service.NormalizerError, service.NetskopeApiError) as exc:
        delete_upload(upload_path)
        entry.data.pop("upload_path", None)
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/upload.html",
            {
                "action": "/operations/local-group-import/upload",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "group_note": entry.data.get("group_note"),
                "back_url": "/operations/local-group-import/group-setup",
                "previously_processed": None,
                "continue_url": "/operations/local-group-import/confirm",
                "error": str(exc),
            },
            status_code=400,
        )

    # The file has been fully read into `summary` - nothing downstream
    # needs it anymore.
    delete_upload(upload_path)
    entry.data.pop("upload_path", None)
    entry.data["summary"] = summary

    return templates.TemplateResponse(
        request,
        "operations/local_group_import/review.html",
        {
            "action": "/operations/local-group-import/confirm",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "summary": summary,
            "filename": file.filename,
            "group_mode": entry.data["group_mode"],
            "group_name": entry.data.get("group_name"),
            "group_note": entry.data.get("group_note"),
            "back_url": "/operations/local-group-import/upload",
            "error": None,
        },
    )


# --- Step 8-9: confirm -> job -> audit -------------------------------------


@router.get("/confirm")
def show_review(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for nothing further downstream; Step 5's
    "already processed, continue" choice links straight here."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "summary" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

    return templates.TemplateResponse(
        request,
        "operations/local_group_import/review.html",
        {
            "action": "/operations/local-group-import/confirm",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "summary": entry.data["summary"],
            "filename": entry.data.get("input_filename"),
            "group_mode": entry.data["group_mode"],
            "group_name": entry.data.get("group_name"),
            "group_note": entry.data.get("group_note"),
            "back_url": "/operations/local-group-import/upload",
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
        return wizard_expired_response(request, session_key, "/operations/local-group-import")

    if reviewed != "yes":
        # confirm-gate.js already disables the button client-side until
        # this is checked - this is the real, server-side gate behind it
        # (CLAUDE.md Section 6: never let the UI alone be the safety net).
        return templates.TemplateResponse(
            request,
            "operations/local_group_import/review.html",
            {
                "action": "/operations/local-group-import/confirm",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "summary": entry.data["summary"],
                "filename": entry.data.get("input_filename"),
                "group_mode": entry.data["group_mode"],
                "group_name": entry.data.get("group_name"),
                "group_note": entry.data.get("group_note"),
                "back_url": "/operations/local-group-import/upload",
                "error": "Please check the review box to confirm before proceeding.",
            },
            status_code=400,
        )

    entry = credential_cache.pop(session_key)  # ownership transfers to the job now
    summary = entry.data["summary"]

    job = create_job(
        db,
        job_type="local_group_import",
        tenant=entry.tenant,
        created_by_username=user.username,
        input_filename=entry.data.get("input_filename"),
    )
    credential_cache.mark_completed(session_key, FLOW, job.id)

    job_manager.start(
        job.id,
        service.run_import_job,
        entry.tenant,
        entry.token,
        entry.data["group_mode"],
        entry.data.get("group_id"),
        entry.data.get("group_name"),
        summary.rows,
    )

    return RedirectResponse(f"/jobs/{job.id}", status_code=303)
