"""
Netskope API calls for Private App Import. Shared HTTP plumbing (base URL,
headers, error wrapping, list-envelope unwrapping) lives in
app/operations/netskope_http.py - reused by Phase 2 (RTP Creation) too.

Reimplements the same logic as reference_scripts/netskope_fetch_existing.py,
netskope_bulk_import.sh's import_app(), and netskope_verify_success.py's
fetch_all_apps() - preserving every documented gotcha from CLAUDE.md
Section 9 (HTTP 200 does not mean success; publisher_id as int; the
pagination behavior confirmed against the real API) - just via requests
instead of curl subprocesses.
"""
from __future__ import annotations

from ..netskope_http import NetskopeApiError, base_url, body_ok, parse_json_body, request, unwrap_list

FETCH_LIMIT = 1000  # matches reference_scripts/netskope_fetch_existing.py -
                     # offset-based multi-page pagination is confirmed NOT
                     # to work as documented conventions elsewhere suggest;
                     # one large page is the proven-working approach
MAX_PAGES = 20

__all__ = [
    "NetskopeApiError",
    "fetch_publishers",
    "normalize_app_name",
    "parse_port_range",
    "port_ranges_overlap",
    "fetch_existing_apps",
    "PortClaims",
    "existing_port_claims_and_names",
    "create_private_app",
]


def fetch_publishers(tenant: str, token: str) -> list[dict]:
    """
    Confirmed real response shape (live-verified against a production tenant,
    2026-08 - not a guess): `{"data": {"publishers": [...]}, "status":
    "success", "total": N}`. Parsed explicitly for that confirmed shape,
    with unwrap_list as a defensive fallback only, in case some other
    tenant's API version ever differs.
    """
    url = f"{base_url(tenant)}/api/v2/infrastructure/publishers"
    resp = request("GET", url, token, "fetching publishers", params={"fields": "publisher_id,publisher_name,status"})

    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError("Token was accepted but lacks the required scope (HTTP 403) to list publishers.")
    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching publishers (HTTP {resp.status_code}).")

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Publisher list response wasn't valid JSON.") from exc

    envelope = parsed.get("data", {}) if isinstance(parsed, dict) else {}
    items = envelope.get("publishers") if isinstance(envelope, dict) else None
    if items is None:
        # Confirmed 2026-09-27: against the confirmed real production response shape
        # (`data.publishers` always present), `items` above is never None, so
        # this branch is currently UNREACHABLE in production - it only exists
        # as a defensive fallback for a hypothetical future tenant/API
        # version whose `data` object doesn't nest a "publishers" key.
        # unwrap_list() only unwraps one level (see its own docstring,
        # netskope_http.py) - fine here since `envelope` is already
        # pre-stripped one level below `parsed`, not the raw double-nested
        # body.
        items = unwrap_list(envelope)

    return [
        {
            "publisher_id": item.get("publisher_id"),
            "publisher_name": item.get("publisher_name", "(unnamed)"),
            "status": item.get("status", "unknown"),
        }
        for item in items
    ]


def normalize_app_name(name: str) -> str:
    """Netskope wraps every stored Private App name in [...] on storage,
    regardless of what was submitted - always normalize both sides before
    comparing submitted vs. stored names (CLAUDE.md Section 9)."""
    return name.strip().lstrip("[").rstrip("]").strip()


def parse_port_range(port_spec: str) -> tuple[int, int]:
    """
    Parses a Netskope port field - a single port ("3389") or a range
    ("1-65535") - into an inclusive (start, end) integer pair. Confirmed
    real shape on both sides: the write-side normalizer already produces
    exactly this (CLAUDE.md Section 9), and a live GET against the real
    lab tenant (2026-08) confirmed the same shape reading back, including
    a genuine full-range app ("1-65535").
    """
    if "-" in port_spec:
        start_s, end_s = port_spec.split("-", 1)
        return int(start_s), int(end_s)
    port = int(port_spec)
    return port, port


def port_ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Inclusive-boundary overlap: two ranges sharing even one port number
    (e.g. "1-100" and "100-200" both claiming port 100) count as
    overlapping - a port is either claimed or it isn't, unlike a
    half-open time interval."""
    return a[0] <= b[1] and b[0] <= a[1]


def fetch_existing_apps(tenant: str, token: str) -> list[dict]:
    """
    Every existing Private App's name, destination host(s), and
    (transport, port-range) claims, tenant-wide, in one paginated fetch -
    shared by the collision pre-check (destination+port AND name -
    CLAUDE.md Section 9's port-level access segmentation, and the sibling
    name-collision case) and the independent post-creation verification
    pass. Used to be two separate near-identical paginated fetches
    (fetch_existing_destinations / fetch_all_private_apps); unified since
    every field any of them needed was already present in the same
    response, at no extra API cost - confirmed live against the real lab
    tenant (2026-08): the bulk list response already includes each app's
    full `protocols` array, no per-app detail fetch needed.
    """
    url = f"{base_url(tenant)}/api/v2/steering/apps/private"
    apps: list[dict] = []
    offset = 0
    total = None
    pages = 0

    while True:
        resp = request("GET", url, token, "fetching existing apps", params={"limit": FETCH_LIMIT, "offset": offset})
        if resp.status_code != 200:
            raise NetskopeApiError(f"Unexpected response fetching existing apps (HTTP {resp.status_code}).")
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise NetskopeApiError("Existing-apps response wasn't valid JSON.") from exc

        data = unwrap_list(parsed.get("data", []))
        if total is None:
            total = parsed.get("total")

        for app in data:
            ports: list[tuple[str, tuple[int, int]]] = []
            for entry in app.get("protocols", []) or []:
                port_spec = entry.get("port")
                transport = (entry.get("transport") or "").lower()
                if not port_spec or not transport:
                    continue
                try:
                    ports.append((transport, parse_port_range(str(port_spec))))
                except ValueError:
                    continue  # malformed port field from the API - skip rather than crash the whole fetch

            apps.append(
                {
                    "app_name": app.get("app_name", ""),
                    "hosts": [h.strip() for h in str(app.get("host", "")).split(",") if h.strip()],
                    "ports": ports,
                }
            )

        pages += 1
        if len(data) == 0:
            break
        offset += FETCH_LIMIT
        if isinstance(total, int) and offset >= total:
            break
        if pages >= MAX_PAGES:
            break

    return apps


PortClaims = dict  # dict[str, list[tuple[str, tuple[int, int]]]] - host -> [(transport, (start, end)), ...]


def existing_port_claims_and_names(tenant: str, token: str) -> tuple[PortClaims, set[str]]:
    """
    Derives both collision-check inputs from one shared fetch: a per-host
    index of (transport, port-range) claims, and every existing app's
    normalized name. CLAUDE.md Section 9 - port-level access segmentation:
    two apps at the same host only collide if their ports (same
    transport) actually overlap; a disjoint or different-transport port
    set at an already-used host is a confirmed, legitimate, wanted
    pattern (different users needing different port-scoped access to the
    same destination - e.g. one app scoped to tcp/22, another at the same
    host scoped to tcp/443), not a collision. Name collision is a sibling
    case, unaffected by ports - a name is a name regardless of what ports
    it's scoped to.

    One app's `protocols` list applies uniformly to every host in its
    host string (confirmed: there's one flat protocols array per app, not
    one per host) - each host gets the full set of that app's claims.
    """
    apps = fetch_existing_apps(tenant, token)
    claims: PortClaims = {}
    for app in apps:
        for host in app["hosts"]:
            claims.setdefault(host, []).extend(app["ports"])
    names = {normalize_app_name(app["app_name"]) for app in apps if app.get("app_name")}
    return claims, names


def create_private_app(tenant: str, token: str, payload: dict) -> tuple[bool, str]:
    """POSTs one Private App. CLAUDE.md Section 9: HTTP 200/201 does not
    mean success - Netskope can return either code with an error in the
    response body, so the body's own `status` field is the only thing
    trusted here, never the HTTP status code alone."""
    url = f"{base_url(tenant)}/api/v2/steering/apps/private"
    resp = request("POST", url, token, "creating a private app", json=payload)
    body = parse_json_body(resp)

    if body_ok(resp, body):
        return True, f"HTTP {resp.status_code}: {body.get('message', 'created')}"

    return False, f"HTTP {resp.status_code}: {body.get('message', resp.text[:300])}"
