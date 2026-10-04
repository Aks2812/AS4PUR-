"""
Device Posture Validation (Operation 4) - a READ-ONLY diagnostic lookup,
not a bulk-write operation. Deliberately does NOT use the dry-run/
review-gate/confirm pattern the other three operations use (CLAUDE.md
Section 6) - that pattern exists to add deliberate friction before a
write, and this feature never writes anything. Input goes straight to
output.

Still reuses the shared tenant/token entry pattern and credential_cache
(CLAUDE.md Section 3 - never on disk, never in the DB, never logged) so
an operator can look up more than one user's devices in one sitting
without re-entering the tenant/token every time - the cached entry is
only ever cleared by credential_cache's own existing paths (TTL, logout,
starting over with a different tenant), never popped here, since there's
no job to hand ownership off to.

Internal-only: no sidebar/dashboard copy or route here mentions this on
the public landing page, and no wordmark/tagline/acronym text is touched.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ...auth.dependencies import require_login
from ...config import settings
from ...models import User
from ...security.csrf import get_csrf_token, verify_csrf
from ...templating import templates
from ...uploads import UploadRejected, delete_upload, format_size, save_upload
from ..credential_cache import FlowCollisionError, credential_cache
from ..validation import clean_tenant_token, normalise_credentials, tenant_token_error_message
from ..wizard_expired import wizard_expired_response
from . import service
from .netskope_client import NetskopeApiError, fetch_classification_rules, fetch_client_status, render_condition_tree, rules_for_os

router = APIRouter(prefix="/operations/device-posture")

FLOW = "Device Posture Validation"

# nsdebug.log is a plain-text agent log - real Info-level samples already
# run up to ~9MB (nsdebug_parser.py's own module docstring) and Debug-level
# ones larger, so this feature has its own limit
# (config.nsdebug_upload_max_bytes, 50MB) rather than sharing the 10MB one
# the Excel/CSV operations use. ".txt" is accepted alongside ".log" since
# it's common for an operator to rename/export the file that way when
# pulling it off a device.
ALLOWED_LOG_EXTENSIONS = {".log", ".txt"}


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


def _build_candidates(rule: dict) -> dict:
    conditions = rule.get("conditions") or {}
    return {
        "rule_id": rule.get("_id") or rule.get("rule_id") or rule.get("id") or "(unknown id)",
        "name": rule.get("name") or rule.get("rule_name") or "(unnamed rule)",
        "rendered_conditions": render_condition_tree(conditions),
    }


@router.get("")
def start(request: Request, user: User = Depends(require_login)):
    entry = credential_cache.get(_session_key(request))
    tenant_value = entry.tenant if entry is not None and entry.flow == FLOW else None
    return templates.TemplateResponse(
        request,
        "operations/device_posture/tenant.html",
        {
            "action": "/operations/device-posture/tenant",
            "csrf_token": get_csrf_token(request),
            "error": None,
            "tenant_value": tenant_value,
            "back_url": "/",
        },
    )


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
            "operations/device_posture/tenant.html",
            {
                "action": "/operations/device-posture/tenant",
                "csrf_token": get_csrf_token(request),
                "error": field_error,
                "tenant_value": tenant,
                "back_url": "/",
            },
            status_code=400,
        )

    try:
        credential_cache.start(_session_key(request), tenant, token, flow=FLOW)
    except FlowCollisionError as exc:
        return templates.TemplateResponse(
            request,
            "operations/device_posture/tenant.html",
            {
                "action": "/operations/device-posture/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
                "back_url": "/",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "operations/device_posture/email.html",
        {
            "action": "/operations/device-posture/lookup",
            "csrf_token": get_csrf_token(request),
            "tenant": tenant,
            "email_value": "",
            "back_url": "/operations/device-posture",
            "error": None,
        },
    )


@router.get("/lookup")
def show_lookup(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for the email step, and the "look up another
    user" link from the results page."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, "/operations/device-posture")

    return templates.TemplateResponse(
        request,
        "operations/device_posture/email.html",
        {
            "action": "/operations/device-posture/lookup",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "email_value": "",
            "back_url": "/operations/device-posture",
            "error": None,
        },
    )


@router.post("/lookup")
def submit_lookup(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    email: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or entry.flow != FLOW:
        return wizard_expired_response(request, session_key, "/operations/device-posture")

    email = email.strip()
    if not email:
        return templates.TemplateResponse(
            request,
            "operations/device_posture/email.html",
            {
                "action": "/operations/device-posture/lookup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "email_value": email,
                "back_url": "/operations/device-posture",
                "error": "Enter a user email to look up.",
            },
            status_code=400,
        )

    try:
        devices = fetch_client_status(entry.tenant, entry.token, email)
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/device_posture/email.html",
            {
                "action": "/operations/device-posture/lookup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "email_value": email,
                "back_url": "/operations/device-posture",
                "error": str(exc),
            },
            status_code=400,
        )

    if not devices:
        return templates.TemplateResponse(
            request,
            "operations/device_posture/results.html",
            {
                "tenant": entry.tenant,
                "email": email,
                "devices": [],
                "no_devices_message": f"No devices found for {email} in this tenant.",
                "fetch_error": None,
                "back_url": "/operations/device-posture/lookup",
            },
        )

    # One classification-rules fetch, filtered client-side per device OS -
    # devices sharing an OS reuse the same fetch rather than re-fetching
    # the whole rule set once per device.
    fetch_error = None
    try:
        all_rules = fetch_classification_rules(entry.tenant, entry.token)
    except NetskopeApiError as exc:
        all_rules = []
        fetch_error = str(exc)

    device_views = []
    for device in devices:
        matching_rules = rules_for_os(all_rules, device["os"]) if not fetch_error else []
        device_views.append(
            {
                **device,
                "candidate_rules": [_build_candidates(r) for r in matching_rules],
            }
        )

    return templates.TemplateResponse(
        request,
        "operations/device_posture/results.html",
        {
            "tenant": entry.tenant,
            "email": email,
            "devices": device_views,
            "no_devices_message": None,
            "fetch_error": fetch_error,
            "back_url": "/operations/device-posture/lookup",
        },
    )


def _upload_page(request: Request, error: str | None = None, status_code: int = 200):
    """The upload form, optionally with an inline error banner. The limit is
    injected for the page's own size pre-check (static/js/nsdebug-upload.js),
    so an oversize file is caught before the upload starts - the server-side
    check below stays the one that actually enforces it."""
    limit = settings.nsdebug_upload_max_bytes
    return templates.TemplateResponse(
        request,
        "operations/device_posture/nsdebug_upload.html",
        {
            "action": "/operations/device-posture/nsdebug-log",
            "csrf_token": get_csrf_token(request),
            "error": error,
            "max_upload_bytes": limit,
            "max_upload_label": format_size(limit),
            "back_url": "/operations/device-posture",
        },
        status_code=status_code,
    )


def _upload_rejection_message(exc: UploadRejected) -> str:
    if exc.reason == "too_large":
        return (
            f"This file is {format_size(exc.size)}, which is over the {format_size(exc.limit)} upload limit. "
            "Collect a fresh log, or trim it to the relevant time window, and try again."
        )
    if exc.reason == "empty":
        return "The file you chose is empty (0 bytes). Choose the device's nsdebug.log and try again."
    if exc.reason == "bad_type":
        return (
            f"Unsupported file type '{exc.extension or '(none)'}'. "
            "Upload the device's nsdebug log as a .log or .txt file."
        )
    return str(exc.detail)


def _analyse_upload(upload_path, filename: str, tenant: str, token: str) -> tuple[dict, dict | None]:
    """Everything the upload route does after the file is saved that is not
    `await`-able: parse, build the report, and (optionally) cross-reference
    the live tenant rule. Runs in a worker thread (see the route)."""
    try:
        parsed = service.parse_uploaded_log(upload_path, filename)
    finally:
        # No further use for the raw file once parsed - nothing here is
        # ever written back to it, and it never needs to survive past
        # this one request (CLAUDE.md Section 5: clean up temp files).
        delete_upload(upload_path)

    report = service.build_report_view(parsed)

    # Optional live-rule cross-reference (CLAUDE.md 2026-09-27 nsdebug
    # follow-up) - tenant/token are both blank by default, matching this
    # route's existing no-tenant-required behavior exactly. A failure here
    # is never fatal to the primary log-only report built above - it's
    # rendered as its own side panel, `report` itself is never touched.
    rule_cross_reference = None
    tenant, token, field_error = normalise_credentials(tenant, token)         # strips; refuses a control character
    if field_error or tenant or token:
        field_error = field_error or tenant_token_error_message(tenant, token)
        if field_error:
            rule_cross_reference = {
                "attempted": True,
                "error": field_error,
                "rule": None,
                "no_match_reason": None,
                "required_but_not_observed": [],
            }
        else:
            rule_cross_reference = service.cross_reference_rule(report, tenant, token)
    return report, rule_cross_reference


@router.get("/nsdebug-log")
def show_nsdebug_upload(request: Request, user: User = Depends(require_login)):
    """No tenant/token step here on purpose (unlike the rest of this
    operation, above) - parsing an already-collected log file makes no
    Netskope API call at all, so there's nothing to authenticate."""
    return _upload_page(request)


@router.post("/nsdebug-log")
async def submit_nsdebug_upload(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    file: UploadFile | None = File(default=None),
    tenant: str = Form(default=""),
    token: str = Form(default=""),
):
    verify_csrf(request, csrf_token)

    # `file` is optional at the framework level so that "nothing chosen"
    # (missing part, or the empty-filename part a browser sends for an
    # unfilled file input) reaches this route's own friendly page instead of
    # FastAPI's raw 422 JSON - CLAUDE.md Section 11's Form(...) empty-value
    # gap, fixed the same way.
    if file is None or not (file.filename or "").strip():
        return _upload_page(request, "Choose an nsdebug.log file to upload.", 400)

    # Every rejection (wrong type, too large, empty) comes back as this
    # page with an inline banner - never the raw JSON body that
    # save_upload()'s HTTPException would otherwise produce here.
    try:
        upload_path = await save_upload(
            file,
            ALLOWED_LOG_EXTENSIONS,
            max_bytes=settings.nsdebug_upload_max_bytes,
            reject_empty=True,
        )
    except UploadRejected as exc:
        return _upload_page(request, _upload_rejection_message(exc), exc.status_code)

    # Parsing is synchronous CPU work (a 45MB log holds the loop for over a
    # second) and the optional rule lookup is a blocking HTTP call - run
    # inline in this `async def` they would freeze every other request until
    # they finished, so both go to a worker thread. (`save_upload` above has
    # to stay on the loop: it reads the upload with `await`.)
    report, rule_cross_reference = await run_in_threadpool(
        _analyse_upload, upload_path, file.filename or "nsdebug.log", tenant, token
    )

    return templates.TemplateResponse(
        request,
        "operations/device_posture/nsdebug_report.html",
        {
            "report": report,
            "rule_cross_reference": rule_cross_reference,
            "back_url": "/operations/device-posture/nsdebug-log",
        },
    )
