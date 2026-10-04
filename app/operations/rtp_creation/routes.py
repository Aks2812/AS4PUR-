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
from ..private_app_import.netskope_client import fetch_existing_apps, normalize_app_name
from ..validation import clean_tenant_token
from ..wizard_expired import wizard_expired_response
from . import service
from .netskope_client import NetskopeApiError, fetch_policy_groups, fetch_rule_by_id, fetch_rules, summarize_rule
from .template_file import build_template_workbook_bytes

router = APIRouter(prefix="/operations/rtp-creation")

ALLOWED_UPLOAD_EXTENSIONS = {".xlsx"}

# Two distinct flows share this one router/operation - Op2's "create a new
# rule" and "add users to an existing rule" paths are NOT the same flow
# for credential_cache's collision-guard purposes, despite both living
# under /operations/rtp-creation.
FLOW_NEW_RULE = "RTP Creation (new rule)"
FLOW_EXISTING_RULE = "RTP Creation (add users to existing rule)"


def _session_key(request: Request) -> str:
    return request.state.session.token_hash


def _identity_inputs_back_url(flow: str) -> str:
    """
    identity_inputs.html is the one screen genuinely shared, byte-for-byte,
    between both Op2 flows (see the module comment above
    submit_existing_rule_tenant) - but the step immediately BEFORE it is
    different for each: the create-new path's own tenant screen, vs. the
    existing-rule path's rule-confirmation screen. Every render site of
    identity_inputs.html (both POST handlers that land on it, its own GET
    redisplay, and the validation-error redisplay in
    submit_identity_inputs) calls this rather than hardcoding one.
    """
    if flow == FLOW_EXISTING_RULE:
        return "/operations/rtp-creation/existing-rule/proceed"
    return "/operations/rtp-creation/new"


@router.get("")
def choose_mode(request: Request, user: User = Depends(require_login)):
    """
    Mode selection, at the literal start of the wizard (this is the URL
    dashboard.html's "Start" link points to): "Create new rule" is the
    original, unchanged flow (now at GET /new); "Add users to an existing
    rule" is the new PATCH-based path. No CSRF token needed here - this
    page only links elsewhere, it doesn't submit a form. This is genuinely
    step 0 of both flows (their own tenant screens' Back links point back
    here), so it needs no Back link of its own.
    """
    return templates.TemplateResponse(request, "operations/rtp_creation/mode.html", {})


@router.get("/new")
def start(request: Request, user: User = Depends(require_login)):
    # See private_app_import/routes.py's start() for why this checks
    # entry.flow before prefilling - credential_cache's one slot could
    # currently hold a DIFFERENT flow's entry (including Op2's OWN other
    # flow, /existing-rule).
    entry = credential_cache.get(_session_key(request))
    tenant_value = entry.tenant if entry is not None and entry.flow == FLOW_NEW_RULE else None
    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/tenant.html",
        {
            "action": "/operations/rtp-creation/tenant",
            "csrf_token": get_csrf_token(request),
            "error": None,
            "tenant_value": tenant_value,
            "back_url": "/operations/rtp-creation",
        },
    )


@router.get("/template.xlsx")
def download_template(user: User = Depends(require_login)):
    content = build_template_workbook_bytes()
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="AS4PUR_rtp_creation_template.xlsx"'},
    )


# --- Stage A: identity resolution ---------------------------------------


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
    # switched - an empty tenant/token would otherwise get cached and
    # only surface as a broken request one step later, at upload.
    tenant: str = Form(default=""),
    token: str = Form(default=""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    tenant, token, field_error = clean_tenant_token(tenant, token)
    if field_error:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/tenant.html",
            {
                "action": "/operations/rtp-creation/tenant",
                "csrf_token": get_csrf_token(request),
                "error": field_error,
                "tenant_value": tenant,
                "back_url": "/operations/rtp-creation",
            },
            status_code=400,
        )

    # No live call here (unlike Private App Import's publisher fetch) -
    # Stage A has nothing to fetch-and-display until identity resolution
    # itself, which needs the SCIM group name from the next step first.
    # The first real API call, and so the first real check of whether
    # this tenant/token actually work, happens at the upload step.
    try:
        credential_cache.start(_session_key(request), tenant, token, flow=FLOW_NEW_RULE)
    except FlowCollisionError as exc:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/tenant.html",
            {
                "action": "/operations/rtp-creation/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
                "back_url": "/operations/rtp-creation",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/identity_inputs.html",
        {
            "action": "/operations/rtp-creation/identity-inputs",
            "csrf_token": get_csrf_token(request),
            "tenant": tenant,
            "scim_group_value": "",
            "email_domain_value": "",
            "back_url": _identity_inputs_back_url(FLOW_NEW_RULE),
            "error": None,
        },
    )


@router.get("/identity-inputs")
def show_identity_inputs(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 3 (upload) - shared, unmodified
    screen for both Op2 flows; only the Back link's own destination
    differs (see _identity_inputs_back_url). Pre-fills the SCIM group and
    email domain if this session already passed through here once."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/identity_inputs.html",
        {
            "action": "/operations/rtp-creation/identity-inputs",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "scim_group_value": entry.data.get("scim_group", ""),
            "email_domain_value": entry.data.get("email_domain", ""),
            "back_url": _identity_inputs_back_url(entry.flow),
            "error": None,
        },
    )


@router.post("/identity-inputs")
def submit_identity_inputs(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    scim_group: str = Form(default=""),
    # default="" not Form(...): FastAPI/python-multipart treats a
    # genuinely empty submitted value as a MISSING field (its own generic
    # 422), never reaching the friendly required-field check below at
    # all - normally masked by the input's HTML5 `required` attribute,
    # but real for any client that bypasses it (curl, JS disabled).
    email_domain: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    # SCIM sync group is optional (CLAUDE.md Section 7/9): some real
    # clients have users who genuinely refuse assignment to any group.
    # Blank triggers fetch_group_members' full-directory search path
    # instead - email domain stays mandatory either way, it's needed for
    # the domain-append fix regardless of which population is searched.
    scim_group = scim_group.strip()
    email_domain = email_domain.strip()
    if not email_domain:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/identity_inputs.html",
            {
                "action": "/operations/rtp-creation/identity-inputs",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "scim_group_value": scim_group,
                "email_domain_value": email_domain,
                "back_url": _identity_inputs_back_url(entry.flow),
                "error": "The email domain is required.",
            },
            status_code=400,
        )

    entry.data["scim_group"] = scim_group
    entry.data["email_domain"] = email_domain

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/upload.html",
        {
            "action": "/operations/rtp-creation/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "back_url": "/operations/rtp-creation/identity-inputs",
            "previously_processed": None,
            "continue_url": "/operations/rtp-creation/confirm-identities",
            "error": None,
        },
    )


@router.get("/upload")
def show_upload(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 4 (Review Gate 1). Same
    already-processed treatment as the other two operations' upload steps
    - see private_app_import/routes.py's show_upload for the rationale."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "scim_group" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    previously_processed = None
    if "resolution" in entry.data:
        resolution = entry.data["resolution"]
        previously_processed = {
            "filename": entry.data.get("input_filename"),
            "matched_count": resolution.matched_count,
            "unmatched_count": resolution.unmatched_count,
            "ambiguous_count": resolution.ambiguous_count,
        }

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/upload.html",
        {
            "action": "/operations/rtp-creation/upload",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "back_url": "/operations/rtp-creation/identity-inputs",
            "previously_processed": previously_processed,
            "continue_url": "/operations/rtp-creation/confirm-identities",
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
    if entry is None or "scim_group" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    upload_path = await save_upload(file, ALLOWED_UPLOAD_EXTENSIONS)
    entry.data["upload_path"] = str(upload_path)
    entry.data["input_filename"] = file.filename

    try:
        rows = service.parse_identity_workbook(upload_path)
        resolution = service.resolve_identities(rows, entry.tenant, entry.token, entry.data["scim_group"], entry.data["email_domain"])
    except (service.NormalizerError, service.NetskopeApiError) as exc:
        delete_upload(upload_path)
        entry.data.pop("upload_path", None)
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/upload.html",
            {
                "action": "/operations/rtp-creation/upload",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "back_url": "/operations/rtp-creation/identity-inputs",
                "previously_processed": None,
                "continue_url": "/operations/rtp-creation/confirm-identities",
                "error": str(exc),
            },
            status_code=400,
        )

    # The file has been fully read into `resolution` - nothing downstream
    # needs it anymore.
    delete_upload(upload_path)
    entry.data.pop("upload_path", None)
    entry.data["resolution"] = resolution

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/review_identities.html",
        {
            "action": "/operations/rtp-creation/confirm-identities",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "resolution": resolution,
            "filename": file.filename,
            "back_url": "/operations/rtp-creation/upload",
            "error": None,
        },
    )


@router.get("/confirm-identities")
def show_review_identities(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Stage B's first screen in both flows
    (group.html for create-new, existing_rule_review.html for
    existing-rule) - Review Gate 1 is identical either way, so both point
    here. Redisplays from the cached resolution, no re-parse."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "resolution" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/review_identities.html",
        {
            "action": "/operations/rtp-creation/confirm-identities",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "resolution": entry.data["resolution"],
            "filename": entry.data.get("input_filename"),
            "back_url": "/operations/rtp-creation/upload",
            "error": None,
        },
    )


@router.post("/confirm-identities")
def confirm_identities(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    reviewed: str | None = Form(default=None),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "resolution" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    if reviewed != "yes":
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/review_identities.html",
            {
                "action": "/operations/rtp-creation/confirm-identities",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "resolution": entry.data["resolution"],
                "filename": entry.data.get("input_filename"),
                "back_url": "/operations/rtp-creation/upload",
                "error": "Please check the review box to confirm before proceeding.",
            },
            status_code=400,
        )

    # --- "Add users to an existing rule" mode branches off here: Stage A
    # (everything above this point) is identical for both modes - this is
    # the one place the two paths necessarily diverge, since Stage B is
    # genuinely different work (PATCH an existing rule vs. create a new
    # one). entry.data["target_rule"] is only ever set by the
    # existing-rule lookup step, never by the create-new-rule path.
    if "target_rule" in entry.data:
        return _render_existing_rule_review(request, entry)

    # --- Stage B begins: the group_id pre-flight, required the same way
    # it is for Operations 1 and 3, no exception (CLAUDE.md Section 7).
    try:
        groups = fetch_policy_groups(entry.tenant, entry.token)
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/review_identities.html",
            {
                "action": "/operations/rtp-creation/confirm-identities",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "resolution": entry.data["resolution"],
                "filename": entry.data.get("input_filename"),
                "back_url": "/operations/rtp-creation/upload",
                "error": str(exc),
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/group.html",
        {
            "action": "/operations/rtp-creation/group",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "groups": groups,
            "selected_group_id": entry.data.get("group_id"),
            "back_url": "/operations/rtp-creation/confirm-identities",
            "error": None,
        },
    )


# --- Stage B: rule creation ----------------------------------------------


@router.get("/group")
def show_group(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 6 (rule-details). Live re-fetch
    (the group list is never cached), pre-selecting whatever was already
    chosen last time through this step."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "resolution" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    try:
        groups = fetch_policy_groups(entry.tenant, entry.token)
        error = None
    except NetskopeApiError as exc:
        groups = []
        error = str(exc)

    # The previously-selected group could genuinely be gone from the
    # tenant by the time the operator comes Back here (deleted, or the
    # fetch simply came back different) - `selected_group_id == g.group_id`
    # in the template is a plain string comparison inside a loop, never a
    # crash either way, but silently renders with nothing checked. Surface
    # that rather than letting the operator believe their earlier choice
    # is still selected.
    selected_group_id = entry.data.get("group_id")
    selected_group_vanished = selected_group_id is not None and selected_group_id not in {g.get("group_id") for g in groups}

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/group.html",
        {
            "action": "/operations/rtp-creation/group",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "groups": groups,
            "selected_group_id": selected_group_id,
            "selected_group_vanished": selected_group_vanished,
            "back_url": "/operations/rtp-creation/confirm-identities",
            "error": error,
        },
    )


@router.post("/group")
def submit_group(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    group_id: str = Form(...),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "resolution" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    group_id = group_id.strip()
    if not group_id:
        try:
            groups = fetch_policy_groups(entry.tenant, entry.token)
        except NetskopeApiError:
            groups = []
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/group.html",
            {
                "action": "/operations/rtp-creation/group",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "groups": groups,
                "selected_group_id": None,
                "back_url": "/operations/rtp-creation/confirm-identities",
                "error": "Select a policy group before continuing.",
            },
            status_code=400,
        )

    entry.data["group_id"] = group_id

    try:
        apps = fetch_existing_apps(entry.tenant, entry.token)
    except NetskopeApiError as exc:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/group.html",
            {
                "action": "/operations/rtp-creation/group",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "groups": [],
                "selected_group_id": group_id,
                "back_url": "/operations/rtp-creation/confirm-identities",
                "error": str(exc),
            },
            status_code=400,
        )

    # Checkbox value = the raw stored name (already bracket-wrapped, ready
    # to submit straight into the rule payload); label = bracket-stripped
    # for readability. CLAUDE.md Section 9: this is where Private App
    # Import's port-segmentation fix pays off - the operator can tell
    # `[172.1.1.4-SSH]` and `[172.1.1.4-LDAP]` apart at a glance.
    app_names = sorted({a["app_name"] for a in apps if a.get("app_name")})

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/rule_details.html",
        {
            "action": "/operations/rtp-creation/rule-details",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "app_names": app_names,
            "normalize_app_name": normalize_app_name,
            "selected_apps": [],
            "access_method_value": "",
            "rule_name_value": "",
            "back_url": "/operations/rtp-creation/group",
            "error": None,
        },
    )


@router.get("/rule-details")
def show_rule_details(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 7 (Review Gate 2). Live re-fetch
    (the app list is never cached), pre-checking previously-selected apps
    and pre-filling the access method and rule name if already set."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "group_id" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    try:
        apps = fetch_existing_apps(entry.tenant, entry.token)
        app_names = sorted({a["app_name"] for a in apps if a.get("app_name")})
        error = None
    except NetskopeApiError as exc:
        app_names = []
        error = str(exc)

    # Same category of drift as show_group above: a previously-checked app
    # could genuinely be gone from the tenant by the time the operator
    # comes Back here. `name in selected_apps` in the template is a plain
    # list membership test, never a crash - but silently unchecks it with
    # no explanation. Surface it, bracket-stripped for readability like
    # everywhere else this list is shown.
    selected_apps = entry.data.get("private_apps", [])
    vanished_apps = [normalize_app_name(a) for a in selected_apps if a not in app_names]

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/rule_details.html",
        {
            "action": "/operations/rtp-creation/rule-details",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "app_names": app_names,
            "normalize_app_name": normalize_app_name,
            "selected_apps": selected_apps,
            "vanished_apps": vanished_apps,
            "access_method_value": entry.data.get("access_method", ""),
            "rule_name_value": entry.data.get("rule_name", ""),
            "back_url": "/operations/rtp-creation/group",
            "error": error,
        },
    )


@router.post("/rule-details")
async def submit_rule_details(request: Request, user: User = Depends(require_login)):
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))

    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "group_id" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    private_apps = form.getlist("private_apps")
    access_method = (form.get("access_method") or "").strip()
    rule_name = (form.get("rule_name") or "").strip()

    errors = []
    if not private_apps:
        errors.append("Select at least one private app.")
    if access_method not in ("Client", "Clientless"):
        errors.append("Choose an access method.")
    if not rule_name:
        errors.append("Enter a rule name.")

    if errors:
        try:
            apps = fetch_existing_apps(entry.tenant, entry.token)
            app_names = sorted({a["app_name"] for a in apps if a.get("app_name")})
        except NetskopeApiError:
            app_names = []
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/rule_details.html",
            {
                "action": "/operations/rtp-creation/rule-details",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "app_names": app_names,
                "normalize_app_name": normalize_app_name,
                # Preserve whatever was already picked/typed on this failed
                # attempt - a validation error shouldn't mean re-doing the
                # parts that were already right.
                "selected_apps": private_apps,
                "access_method_value": access_method,
                "rule_name_value": rule_name,
                "back_url": "/operations/rtp-creation/group",
                "error": " ".join(errors),
            },
            status_code=400,
        )

    entry.data["private_apps"] = private_apps
    entry.data["access_method"] = access_method
    entry.data["rule_name"] = rule_name

    resolution = entry.data["resolution"]
    matched_emails = [row.email for row in resolution.rows if row.status == "MATCHED" and row.email]

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/review_rule.html",
        {
            "action": "/operations/rtp-creation/confirm-rule",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "rule_name": rule_name,
            "group_id": entry.data["group_id"],
            "access_method": access_method,
            "private_apps": private_apps,
            "normalize_app_name": normalize_app_name,
            "matched_count": len(matched_emails),
            "unmatched_count": resolution.unmatched_count,
            "back_url": "/operations/rtp-creation/rule-details",
            "error": None,
        },
    )


@router.post("/confirm-rule")
def confirm_rule(
    request: Request,
    user: User = Depends(require_login),
    db: DBSession = Depends(get_db),
    csrf_token: str = Form(...),
    reviewed: str | None = Form(default=None),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "rule_name" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    resolution = entry.data["resolution"]
    matched_emails = [row.email for row in resolution.rows if row.status == "MATCHED" and row.email]

    if reviewed != "yes":
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/review_rule.html",
            {
                "action": "/operations/rtp-creation/confirm-rule",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "rule_name": entry.data["rule_name"],
                "group_id": entry.data["group_id"],
                "access_method": entry.data["access_method"],
                "private_apps": entry.data["private_apps"],
                "normalize_app_name": normalize_app_name,
                "matched_count": len(matched_emails),
                "unmatched_count": resolution.unmatched_count,
                "back_url": "/operations/rtp-creation/rule-details",
                "error": "Please check the review box to confirm before proceeding.",
            },
            status_code=400,
        )

    entry = credential_cache.pop(session_key)  # ownership transfers to the job now
    resolution = entry.data["resolution"]
    matched_emails = [row.email for row in resolution.rows if row.status == "MATCHED" and row.email]

    job = create_job(
        db,
        job_type="rtp_creation",
        tenant=entry.tenant,
        created_by_username=user.username,
        input_filename=entry.data.get("input_filename"),
    )
    credential_cache.mark_completed(session_key, FLOW_NEW_RULE, job.id)

    job_manager.start(
        job.id,
        service.run_create_rule_job,
        entry.tenant,
        entry.token,
        entry.data["rule_name"],
        entry.data["group_id"],
        entry.data["access_method"],
        entry.data["private_apps"],
        matched_emails,
        resolution.matched_count,
        resolution.unmatched_count,
        resolution.ambiguous_count,
        service.notable_identity_rows(resolution.rows),
    )

    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --- "Add users to an existing rule" mode --------------------------------
# Stage A (tenant/token entry through Review Gate 1) is entirely the same
# code as the create-new-rule path above - reached via /existing-rule/proceed
# below, which just renders the SAME identity_inputs.html the create-new
# path's /tenant route renders, so /identity-inputs, /upload, and
# /confirm-identities (and their templates) never needed to change. Only
# this mode's own tenant/rule-lookup entry and its Stage B (PATCH instead
# of POST) are new.


def _fetch_rules_safely(tenant: str, token: str) -> tuple[list[dict], str | None]:
    """
    Best-effort fetch for the rule picker (Step 2's default view) - a
    failure here degrades gracefully to the "Enter a rule ID manually"
    form being shown open by default, rather than blocking the wizard
    entirely on a picker that couldn't load.
    """
    try:
        return fetch_rules(tenant, token), None
    except NetskopeApiError as exc:
        return [], str(exc)


def _rule_picker_context(entry, rules: list[dict]) -> dict:
    """
    Added 2026-09-08: unlike the other three live-refetched pickers
    (publishers, policy groups, apps), this one had NO pre-selection at
    all until now - a gap exposed while checking what happens across all
    of them when a previously-picked item is no longer in the freshly
    fetched list. Pre-selects entry.data["target_rule"]["rule_id"] (set
    the moment a lookup succeeds) if this session already looked one up,
    and flags it as vanished if that ID isn't in the current live list -
    same "confirmed safe, but was silent" treatment as the other three
    (see show_group's own comment in this file for the fuller rationale).
    A vanished rule here doesn't necessarily mean it was deleted - manual
    entry by ID below still works regardless of whether it appears in
    this list.

    Compares as strings deliberately: a looked-up rule_id is always a
    plain str (it's whatever the operator typed into the manual-entry
    form - see summarize_rule()'s own signature), but fetch_rules()'s own
    docstring flags the LIST endpoint's per-item shape as NOT
    independently confirmed by this project, unlike the single-GET shape.
    If the real API ever returns rule_id as an int in the list (unlike
    the str this project has only confirmed for the single-GET path), a
    bare `==`/`in` would falsely report a still-present rule as vanished
    over a type mismatch alone, not an actual absence.
    """
    selected_rule_id = (entry.data.get("target_rule") or {}).get("rule_id")
    live_ids = {str(r.get("rule_id")) for r in rules if r.get("rule_id") is not None}
    return {
        "selected_rule_id": selected_rule_id,
        "selected_rule_vanished": selected_rule_id is not None and str(selected_rule_id) not in live_ids,
    }


@router.get("/existing-rule")
def existing_rule_start(request: Request, user: User = Depends(require_login)):
    entry = credential_cache.get(_session_key(request))
    tenant_value = entry.tenant if entry is not None and entry.flow == FLOW_EXISTING_RULE else None
    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_tenant.html",
        {
            "action": "/operations/rtp-creation/existing-rule/tenant",
            "csrf_token": get_csrf_token(request),
            "error": None,
            "tenant_value": tenant_value,
            "back_url": "/operations/rtp-creation",
        },
    )


@router.post("/existing-rule/tenant")
def submit_existing_rule_tenant(
    request: Request,
    user: User = Depends(require_login),
    tenant: str = Form(default=""),  # default="" not Form(...): see submit_tenant's
    token: str = Form(default=""),   # own comment above - CLAUDE.md Section 11.
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    tenant, token, field_error = clean_tenant_token(tenant, token)
    if field_error:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/existing_rule_tenant.html",
            {
                "action": "/operations/rtp-creation/existing-rule/tenant",
                "csrf_token": get_csrf_token(request),
                "error": field_error,
                "tenant_value": tenant,
                "back_url": "/operations/rtp-creation",
            },
            status_code=400,
        )

    try:
        entry = credential_cache.start(_session_key(request), tenant, token, flow=FLOW_EXISTING_RULE)
    except FlowCollisionError as exc:
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/existing_rule_tenant.html",
            {
                "action": "/operations/rtp-creation/existing-rule/tenant",
                "csrf_token": get_csrf_token(request),
                "error": str(exc),
                "tenant_value": tenant,
                "back_url": "/operations/rtp-creation",
            },
            status_code=400,
        )

    rules, rules_error = _fetch_rules_safely(entry.tenant, entry.token)

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_lookup.html",
        {
            "action": "/operations/rtp-creation/existing-rule/lookup",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "rules": rules,
            "rules_error": rules_error,
            "back_url": "/operations/rtp-creation/existing-rule",
            "error": None,
            **_rule_picker_context(entry, rules),
        },
    )


@router.get("/existing-rule/lookup")
def show_existing_rule_lookup(request: Request, user: User = Depends(require_login)):
    """Back-navigation target for Step 3 (existing_rule_confirm.html) -
    also the corrected destination for that screen's own "wrong rule?"
    link (previously pointed all the way back to tenant re-entry; see
    existing_rule_confirm.html's own comment). Live re-fetch of the rule
    list, same as the initial /existing-rule/tenant submit - the list
    itself is never cached."""
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation/existing-rule")

    rules, rules_error = _fetch_rules_safely(entry.tenant, entry.token)

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_lookup.html",
        {
            "action": "/operations/rtp-creation/existing-rule/lookup",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "rules": rules,
            "rules_error": rules_error,
            "back_url": "/operations/rtp-creation/existing-rule",
            "error": None,
            **_rule_picker_context(entry, rules),
        },
    )


@router.post("/existing-rule/lookup")
def submit_existing_rule_lookup(
    request: Request,
    user: User = Depends(require_login),
    csrf_token: str = Form(...),
    rule_id: str = Form(default=""),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation/existing-rule")

    rule_id = rule_id.strip()
    if not rule_id:
        rules, rules_error = _fetch_rules_safely(entry.tenant, entry.token)
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/existing_rule_lookup.html",
            {
                "action": "/operations/rtp-creation/existing-rule/lookup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "rules": rules,
                "rules_error": rules_error,
                "back_url": "/operations/rtp-creation/existing-rule",
                "error": "Enter the target rule ID, or select one from the list below.",
                **_rule_picker_context(entry, rules),
            },
            status_code=400,
        )

    # Fail clearly HERE - a bad ID or wrong tenant must not surface later,
    # deeper into the wizard, after the operator has already done the
    # identity-resolution work. Re-fetching the rule list here too (same
    # precedent as rule_details's own re-fetch-on-validation-error, see
    # test_rule_details_rejects_blank_rule_name's comment) so the picker
    # is still usable after a bad manual-entry attempt, not just blank.
    try:
        rule = fetch_rule_by_id(entry.tenant, entry.token, rule_id)
    except NetskopeApiError as exc:
        rules, rules_error = _fetch_rules_safely(entry.tenant, entry.token)
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/existing_rule_lookup.html",
            {
                "action": "/operations/rtp-creation/existing-rule/lookup",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "rules": rules,
                "rules_error": rules_error,
                "back_url": "/operations/rtp-creation/existing-rule",
                "error": str(exc),
                **_rule_picker_context(entry, rules),
            },
            status_code=400,
        )

    target_rule = summarize_rule(rule_id, rule)
    entry.data["target_rule"] = target_rule

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_confirm.html",
        {
            "action": "/operations/rtp-creation/existing-rule/proceed",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "target_rule": target_rule,
            "back_url": "/operations/rtp-creation/existing-rule/lookup",
            "back_label": "Wrong rule? Look up a different rule",
            "error": None,
        },
    )


@router.get("/existing-rule/proceed")
def show_existing_rule_confirm(request: Request, user: User = Depends(require_login)):
    """
    Back-navigation target for Step 4 (identity_inputs.html, existing-rule
    flow only - see _identity_inputs_back_url). Redisplays
    existing_rule_confirm.html straight from the cached target_rule - no
    live re-fetch needed, unlike the lookup screen, since the specific
    rule's details don't need to be re-verified just to look at them
    again.
    """
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "target_rule" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation/existing-rule")

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_confirm.html",
        {
            "action": "/operations/rtp-creation/existing-rule/proceed",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "target_rule": entry.data["target_rule"],
            "back_url": "/operations/rtp-creation/existing-rule/lookup",
            "back_label": "Wrong rule? Look up a different rule",
            "error": None,
        },
    )


@router.post("/existing-rule/proceed")
def existing_rule_proceed(request: Request, user: User = Depends(require_login), csrf_token: str = Form(...)):
    """
    Hands off into Stage A - identity resolution is completely unchanged
    for this mode, so this just renders the exact same identity_inputs.html
    template (and posts to the exact same shared /identity-inputs route)
    the create-new-rule path's /tenant route renders after tenant entry.
    """
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "target_rule" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation/existing-rule")

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/identity_inputs.html",
        {
            "action": "/operations/rtp-creation/identity-inputs",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "scim_group_value": "",
            "email_domain_value": "",
            "back_url": _identity_inputs_back_url(FLOW_EXISTING_RULE),
            "error": None,
        },
    )


def _render_existing_rule_review(request: Request, entry):
    """Called from confirm_identities once Review Gate 1 is passed, only
    for the add-users-to-existing-rule mode - computes the merge preview
    and renders Review Gate 2 for this mode."""
    resolution = entry.data["resolution"]
    target_rule = entry.data["target_rule"]
    matched_emails = [row.email for row in resolution.rows if row.status == "MATCHED" and row.email]
    preview = service.build_merge_preview(target_rule["current_users"], matched_emails, resolution.directory)
    entry.data["merge_preview"] = preview

    return templates.TemplateResponse(
        request,
        "operations/rtp_creation/existing_rule_review.html",
        {
            "action": "/operations/rtp-creation/existing-rule/confirm",
            "csrf_token": get_csrf_token(request),
            "tenant": entry.tenant,
            "target_rule": target_rule,
            "preview": preview,
            "back_url": "/operations/rtp-creation/confirm-identities",
            "error": None,
        },
    )


@router.post("/existing-rule/confirm")
def confirm_add_users_to_existing_rule(
    request: Request,
    user: User = Depends(require_login),
    db: DBSession = Depends(get_db),
    csrf_token: str = Form(...),
    reviewed: str | None = Form(default=None),
):
    verify_csrf(request, csrf_token)
    session_key = _session_key(request)
    entry = credential_cache.get(session_key)
    if entry is None or "merge_preview" not in entry.data:
        return wizard_expired_response(request, session_key, "/operations/rtp-creation")

    if reviewed != "yes":
        return templates.TemplateResponse(
            request,
            "operations/rtp_creation/existing_rule_review.html",
            {
                "action": "/operations/rtp-creation/existing-rule/confirm",
                "csrf_token": get_csrf_token(request),
                "tenant": entry.tenant,
                "target_rule": entry.data["target_rule"],
                "preview": entry.data["merge_preview"],
                "back_url": "/operations/rtp-creation/confirm-identities",
                "error": "Please check the review box to confirm before proceeding.",
            },
            status_code=400,
        )

    entry = credential_cache.pop(session_key)  # ownership transfers to the job now
    resolution = entry.data["resolution"]
    target_rule = entry.data["target_rule"]
    preview = entry.data["merge_preview"]

    job = create_job(
        db,
        job_type="rtp_add_users_to_rule",
        tenant=entry.tenant,
        created_by_username=user.username,
        input_filename=entry.data.get("input_filename"),
    )
    credential_cache.mark_completed(session_key, FLOW_EXISTING_RULE, job.id)

    job_manager.start(
        job.id,
        service.run_add_users_to_rule_job,
        entry.tenant,
        entry.token,
        target_rule["rule_id"],
        target_rule["rule_name"],
        preview.merged_users,
        len(preview.already_present),
        len(preview.new_users),
        resolution.matched_count,
        resolution.unmatched_count,
        resolution.ambiguous_count,
        service.notable_identity_rows(resolution.rows),
        preview.stale_existing_users,
        len(preview.current_users),
    )

    return RedirectResponse(f"/jobs/{job.id}", status_code=303)
