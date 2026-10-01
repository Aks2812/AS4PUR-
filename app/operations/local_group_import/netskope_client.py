"""
SCIM API calls for Local Group/User Import (Operation 3). Reimplements
reference_scripts/netskope_scim_manager.py's proven request shapes
(create_user, create_group, add_user_to_group, the SCIM filter-query
convention for lookups) via `requests` instead of `urllib`, through the
project's own shared HTTP plumbing (app/operations/netskope_http.py) -
same discipline as Operations 1 and 2.

Two required fixes applied per CLAUDE.md Section 9, NOT carried over from
the reference script:
  1. TLS is always verified - the reference script's `ScimClient.verify_ssl:
     bool = False` default (disabling certificate verification) is never
     reproduced here. netskope_http.request() uses `requests` with its
     default verification and nothing in this module ever passes
     verify=False.
  2. No token file. The reference script optionally writes the SCIM token
     to a local `.scim_token` file for reuse across sessions - this
     module never touches disk with the token; it stays in-memory only
     for the run, exactly like Operations 1 and 2 (CLAUDE.md Section 3).

Auth is `Authorization: Bearer <token>` (netskope_http.bearer_headers),
NOT `Netskope-API-Token` - confirmed real, different from every other
endpoint this project has touched. Base URL still follows AS4PUR's own
tenant-input convention (bare tenant name, e.g. "my-tenant", not a full
domain) via netskope_http.base_url() - the reference script's own
`normalize_tenant()` expected a full domain (e.g. "jakarta.goskope.com")
as input, which would be inconsistent with how every other operation in
this app takes the tenant name.
"""
from __future__ import annotations

from urllib.parse import quote

from reference_scripts.netskope_scim_manager import slugify

from ..netskope_http import NetskopeApiError, base_url, bearer_headers, request

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

__all__ = [
    "NetskopeApiError",
    "fetch_groups",
    "fetch_group_by_name",
    "fetch_user_by_email",
    "create_group",
    "create_user",
    "add_user_to_group",
]


def _scim_url(tenant: str, path: str) -> str:
    return f"{base_url(tenant)}/api/v2/scim{path}"


def _scim_request(method: str, tenant: str, token: str, path: str, action: str, **kwargs):
    url = _scim_url(tenant, path)
    return request(method, url, token, action, headers_override=bearer_headers(token), **kwargs)


def _parse_json(resp, action: str) -> dict:
    """For a response this function TRUSTS should be well-formed JSON
    (a 2xx from a GET) - raises, matching every other operation's own
    fetch-function convention (e.g. rtp_creation's fetch_policy_groups)."""
    try:
        return resp.json()
    except ValueError as exc:
        raise NetskopeApiError(f"Response wasn't valid JSON while {action}.") from exc


def _safe_body(resp) -> object:
    """For an error-status response body being read only to report detail
    - never raises, falls back to raw text, matching rtp_creation's
    create_rtp_rule convention. A malformed error body shouldn't crash the
    job; the caller already knows this call failed from the status code."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text[:300]


def fetch_groups(tenant: str, token: str, count: int = 100) -> list[dict]:
    """GET /Groups - the pre-flight step (CLAUDE.md Section 7 step 2:
    real visibility into current state, avoids accidental name collisions)
    and the source list for the "add to an existing group" choice (step
    4). Confirmed real shape (reference_scripts/netskope_scim_manager.py):
    SCIM's own standard envelope, `{"Resources": [...]}` - a fourth,
    different envelope convention from the three this project has already
    met (flat under `data`, nested under `data.publishers`, `data` with
    the page total under `counts.totalResults`)."""
    resp = _scim_request("GET", tenant, token, f"/Groups?count={count}&startIndex=1", "fetching SCIM groups")
    if resp.status_code >= 400:
        raise NetskopeApiError(f"Could not fetch SCIM groups (HTTP {resp.status_code}).")
    data = _parse_json(resp, "fetching SCIM groups")
    resources = data.get("Resources", []) if isinstance(data, dict) else []
    return [
        {"group_id": g.get("id"), "group_name": g.get("displayName")}
        for g in resources
        if isinstance(g, dict) and g.get("id")
    ]


def fetch_group_by_name(tenant: str, token: str, group_name: str) -> dict | None:
    """Confirmed real query convention (reference_scripts/netskope_scim_manager.py):
    `GET /Groups?filter=displayName eq "<name>"` (URL-encoded). Used both
    for the "does this name already exist" pre-check (Section 7 step 6)
    and internally by create_group's own non-fatal already-exists check."""
    query = quote(f'displayName eq "{group_name}"')
    resp = _scim_request("GET", tenant, token, f"/Groups?filter={query}", "looking up a SCIM group by name")
    if resp.status_code >= 400:
        raise NetskopeApiError(f"Could not search SCIM groups (HTTP {resp.status_code}).")
    data = _parse_json(resp, "looking up a SCIM group by name")
    resources = data.get("Resources", []) if isinstance(data, dict) else []
    if not resources or not isinstance(resources[0], dict):
        return None
    g = resources[0]
    return {"group_id": g.get("id"), "group_name": g.get("displayName")}


def fetch_user_by_email(tenant: str, token: str, email: str) -> dict | None:
    """Confirmed real query convention: `GET /Users?filter=userName eq
    "<email>"`. This tenant's SCIM `userName` is the email address itself
    (confirmed in create_user's own payload below) - the "already exists
    in the tenant" check for Section 7 step 6, reusing Operation 1's
    SKIPPED_EXISTS treatment, not a new outcome category."""
    query = quote(f'userName eq "{email}"')
    resp = _scim_request("GET", tenant, token, f"/Users?filter={query}", "looking up a SCIM user by email")
    if resp.status_code >= 400:
        raise NetskopeApiError(f"Could not search SCIM users (HTTP {resp.status_code}).")
    data = _parse_json(resp, "looking up a SCIM user by email")
    resources = data.get("Resources", []) if isinstance(data, dict) else []
    if not resources or not isinstance(resources[0], dict):
        return None
    u = resources[0]
    return {"user_id": u.get("id"), "email": u.get("userName")}


def create_group(tenant: str, token: str, group_name: str) -> tuple[bool, str, str | None]:
    """POST /Groups. Returns (ok, detail, group_id). Does NOT duplicate the
    reference script's own already-exists pre-check internally - the
    caller (service.resolve_group_choice) already resolves an
    already-taken name to "use the existing group" before this is ever
    called, so by the time this runs it's always meant to genuinely
    create something new."""
    body = {
        "schemas": [SCIM_GROUP_SCHEMA],
        "externalId": slugify(group_name),
        "displayName": group_name,
        "members": [],
        "meta": {"resourceType": "Group"},
    }
    resp = _scim_request("POST", tenant, token, "/Groups", "creating a SCIM group", json=body)
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {_safe_body(resp)}", None
    body_data = _safe_body(resp)
    data = body_data if isinstance(body_data, dict) else {}
    return True, f"Group created successfully. ID: {data.get('id', 'unknown')}", data.get("id")


def create_user(tenant: str, token: str, email: str) -> tuple[bool, str, str | None]:
    """POST /Users. Returns (ok, detail, user_id). Confirmed real payload
    shape (reference_scripts/netskope_scim_manager.py) - `userName` and
    `emails[0].value` are both the email address; display/given/family
    name are derived from the local-part of the email, same convention."""
    local_name = email.split("@")[0]
    display_name = local_name.replace(".", " ").replace("_", " ").title() or email
    name_parts = display_name.split(maxsplit=1)
    given_name = name_parts[0]
    family_name = name_parts[1] if len(name_parts) > 1 else "User"

    body = {
        "schemas": [SCIM_USER_SCHEMA, SCIM_ENTERPRISE_USER_SCHEMA],
        "externalId": email,
        "userName": email,
        "active": True,
        "displayName": display_name,
        "emails": [{"primary": True, "value": email, "type": "work"}],
        "name": {"formatted": display_name, "familyName": family_name, "givenName": given_name},
        "meta": {"resourceType": "User"},
    }
    resp = _scim_request("POST", tenant, token, "/Users", "creating a SCIM user", json=body)
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {_safe_body(resp)}", None
    body_data = _safe_body(resp)
    data = body_data if isinstance(body_data, dict) else {}
    return True, f"User created successfully. ID: {data.get('id', 'unknown')}", data.get("id")


def add_user_to_group(tenant: str, token: str, group_id: str, user_id: str) -> tuple[bool, str]:
    """PATCH /Groups/{id} - standard SCIM PatchOp `add` on the `members`
    path. Takes IDs directly rather than re-resolving group/user by name
    the way the reference script's own add_user_to_group does on every
    call - the group_id is already known once per job (resolved at group
    setup, or from create_group's own response), and the user_id is
    already known from create_user's response or the pre-flight
    already-exists lookup, so re-deriving both by name per row would be
    pure waste, not fidelity to the reference script."""
    body = {"schemas": [SCIM_PATCH_SCHEMA], "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}]}
    resp = _scim_request("PATCH", tenant, token, f"/Groups/{group_id}", "adding a user to a SCIM group", json=body)
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {_safe_body(resp)}"
    return True, "added"
