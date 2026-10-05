"""
Netskope API calls for Device Posture Validation (Operation 4) - shared
HTTP plumbing lives in app/operations/netskope_http.py, same as every
other operation.

This is a READ-ONLY, lookup/diagnostic feature (CLAUDE.md Section 7-style
pre-flight discipline doesn't apply here the way it does to the three
write operations - there's no dry-run/review-gate/confirm pattern because
nothing is ever created or changed). Two endpoints, both confirmed
reachable with just the standard `Netskope-Api-Token` header
(CLAUDE.md's 2026-09-22 device-classification-troubleshooting note):

- `GET /api/v2/events/datasearch/clientstatus` - real per-device posture
  status for a user, filterable by a JQL-style `query` string.
- `GET /api/v2/deviceclassification/rules` - the tenant's classification
  rules, each carrying a boolean-logic `conditions` tree.

One thing below is deliberately NOT claimed as independently confirmed
by this project, and is called out rather than silently assumed:

1. Whether `deviceclassification/rules` supports a server-side OS filter
   query parameter - CLAUDE.md's note does not confirm one exists, only
   that "how a specific device/user gets assigned to a specific
   classification-rule label is NOT yet understood." This fetches the
   full rule list once per lookup and filters client-side by each rule's
   own OS field instead, which works regardless of whether a server-side
   filter parameter exists.

`clientstatus`'s response envelope and per-record field nesting IS now
confirmed, 2026-09-27 - a real raw response was pasted directly into
this project (CLAUDE.md Section 9), not inferred: the envelope is
`{"result": [...], "status": {"execution": "SUCCESS"|other,
"status_code": N, "message": "...", "count": N}}` (`status` is an
object, a genuinely different convention from the write endpoints'
top-level string - see netskope_http.py's `execution_ok()`), and
per-record, `hostname` sits at the top level, `os` sits under
`host_info.os` (no top-level `os`/`platform` field in the real
response), and `device_classification_status`/
`device_classification_custom_status` sit under `user_info` - NOT under
`host_info`, and NOT top-level. `_extract_device()` below reflects this
confirmed shape.

`deviceclassification/rules`'s response envelope is ALSO now confirmed,
2026-09-27, via a real raw response pasted directly into this project -
and it's a THIRD, again genuinely different convention from either of
the two above: a bare top-level JSON array on success, no
`status`/`data`/`result` wrapper of any kind, with per-object fields
(`id`, `name`, `label`, `os`, `conditions`, `modifiedBy`,
`modifiedTime`) all top-level - `os` is NOT nested the way
`clientstatus`'s is. `body_ok()`/`execution_ok()` do not apply here at
all (both assume a dict envelope); `fetch_classification_rules()` below
checks "is the parsed body a list" directly instead. This endpoint's
error/failure shape remains genuinely unconfirmed - no real error
response has been captured yet - so an HTTP-200 body that ISN'T a list
is deliberately reported as a generic "unexpected response shape"
failure rather than guessing at a message field path nobody has
actually seen from this specific endpoint.

How a specific rule gets ASSIGNED to a specific device (as opposed to
merely matching that device's OS) is explicitly NOT determined here -
see render_candidate_rules()'s own docstring and the note this feature
prints on its results page. Every OS-matching rule is shown as a
candidate, not a determination.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from ...timeutil import utcnow
from ..netskope_http import NetskopeApiError, base_url, execution_ok, parse_json_body, request, unwrap_list

__all__ = [
    "NetskopeApiError",
    "fetch_client_status",
    "fetch_classification_rules",
    "rules_for_os",
    "render_condition_tree",
    "required_check_types",
    "find_check_nodes",
]

_DAYS_WINDOW = 30

# Confirmed real check types (CLAUDE.md, 2026-09-22 note, production customer
# data) - every other check type falls through to the generic renderer
# below rather than raising, since a tenant may have check types this
# project hasn't seen yet.
_PLAIN_ENGLISH_RENDERERS = {}


def _check_renderer(name):
    def _register(fn):
        _PLAIN_ENGLISH_RENDERERS[name] = fn
        return fn
    return _register


@_check_renderer("process_check")
def _render_process_check(params: dict) -> str:
    # Real confirmed key (CLAUDE.md, real GET deviceclassification/rules
    # pull, 2026-09-27) is "process", e.g. {"process_check": {"process":
    # "com.crowdstrike.falcon.Agent"}} - "name"/"process_name" were this
    # project's own earlier guesses before the real shape was seen, kept
    # as defensive fallbacks only.
    name = params.get("process") or params.get("name") or params.get("process_name") or "(unspecified process)"
    return f"process is running: {name}"


@_check_renderer("min_os_version_check")
def _render_min_os_version_check(params: dict) -> str:
    version = params.get("version") or params.get("min_os_version") or "(unspecified version)"
    edition = params.get("edition")
    text = f"OS version is at least {version}"
    if edition:
        text += f" (edition: {edition})"
    return text


@_check_renderer("mdm_check")
def _render_mdm_check(params: dict) -> str:
    return "device is enrolled in MDM"


@_check_renderer("passcode_required_check")
def _render_passcode_required_check(params: dict) -> str:
    return "device passcode/screen lock is required"


@_check_renderer("device_not_compromised_check")
def _render_device_not_compromised_check(params: dict) -> str:
    return "device is not jailbroken or rooted"


@_check_renderer("primary_storage_encrypted_check")
def _render_primary_storage_encrypted_check(params: dict) -> str:
    return "primary storage is encrypted"


@_check_renderer("domain_check")
def _render_domain_check(params: dict) -> str:
    domains = params.get("domains") or params.get("domain") or []
    if isinstance(domains, str):
        domains = [domains]
    if not domains:
        return "device domain is joined to an approved domain"
    return "device domain is one of: " + ", ".join(str(d) for d in domains)


@_check_renderer("disk_enc_check")
def _render_disk_enc_check(params: dict) -> str:
    product = params.get("product") or "(unspecified product)"
    return f"disk encryption product is: {product}"


@_check_renderer("reg_check")
def _render_reg_check(params: dict) -> str:
    path = params.get("path") or params.get("registry_path") or "(unspecified path)"
    value = params.get("value") or params.get("registry_value") or "(unspecified value)"
    expected = params.get("expected") or params.get("expected_data") or params.get("data") or "(unspecified data)"
    return f"registry {path}\\{value} equals {expected}"


@_check_renderer("av_check")
def _render_av_check(params: dict) -> str:
    product = params.get("product") or "(unspecified product)"
    requires_signature = params.get("signature_required") or params.get("require_signature")
    text = f"antivirus product is: {product}"
    if requires_signature:
        text += " (up-to-date signatures required)"
    return text


@_check_renderer("file_check")
def _render_file_check(params: dict) -> str:
    path = params.get("path") or params.get("file_path") or "(unspecified file)"
    return f"file is present: {path}"


_AND_KEYS = ("$and", "and", "AND")
_OR_KEYS = ("$or", "or", "OR")


def render_condition_tree(node) -> str:
    """
    Recursively renders a deviceclassification rule's boolean-logic
    `conditions` tree into plain English - "$and" becomes "all of: [...]",
    "$or" becomes "at least one of: [...]", nested arbitrarily. A leaf is a
    single-key dict whose key is a check type (process_check,
    min_os_version_check, ...) and whose value is that check's own
    parameters. Never raises on an unrecognized shape - falls back to a
    literal "<check_type>: <params>" rendering rather than crashing the
    whole results page over one unfamiliar check type or a malformed node.
    """
    if not isinstance(node, dict) or not node:
        return "(empty condition)"

    for key in _AND_KEYS:
        if key in node:
            children = node[key] if isinstance(node[key], list) else [node[key]]
            return "all of: [" + "; ".join(render_condition_tree(c) for c in children) + "]"
    for key in _OR_KEYS:
        if key in node:
            children = node[key] if isinstance(node[key], list) else [node[key]]
            return "at least one of: [" + "; ".join(render_condition_tree(c) for c in children) + "]"

    check_type, params = next(iter(node.items()))
    params = params if isinstance(params, dict) else {}
    renderer = _PLAIN_ENGLISH_RENDERERS.get(check_type)
    if renderer is not None:
        return renderer(params)
    return f"{check_type}: {params}"


def required_check_types(node) -> set[str]:
    """
    Flattens a deviceclassification rule's boolean-logic `conditions` tree
    into the flat set of check_type leaf keys it references - e.g.
    {"$and": [{"min_os_version_check": {...}}, {"process_check": {...}}]}
    -> {"min_os_version_check", "process_check"}.

    Used for the optional live-rule cross-reference (CLAUDE.md 2026-09-27
    nsdebug follow-up): a check type the LIVE RULE requires but which
    nsdebug_parser.py's summary_table() shows as APPLICABLE_NOT_OBSERVED
    (no local verdict logged at all) is a real, confirmed gap - not a
    hypothetical one, see the MacOS-Posturing-COD/CrowdStrike case in
    CLAUDE.md - worth flagging distinctly rather than leaving it as an
    unexplained dead end. Mirrors render_condition_tree()'s own
    $and/$or-or-leaf traversal, just collecting keys instead of rendering
    text; never raises on an unrecognized shape, same as that function.
    """
    if not isinstance(node, dict) or not node:
        return set()

    for key in (*_AND_KEYS, *_OR_KEYS):
        if key in node:
            children = node[key] if isinstance(node[key], list) else [node[key]]
            types: set[str] = set()
            for child in children:
                types |= required_check_types(child)
            return types

    check_type = next(iter(node.keys()), None)
    return {check_type} if check_type else set()


def find_check_nodes(node, check_type: str) -> list[dict]:
    """
    Collects every leaf node's own parameter dict for one specific
    check_type, anywhere in a rule's boolean-logic `conditions` tree
    ($and/$or, arbitrarily nested) - unlike required_check_types() (which
    only reports THAT a check type is present), this returns each
    occurrence's own params, since a rule can OR the SAME check type
    against multiple alternatives (confirmed real shape, CLAUDE.md
    2026-09-27: a Windows min_os_version_check rule OR's a
    "Windows 10 All" edition node against a "Windows 11 All" one - two
    separate leaf nodes for the one check type, both needed, not just the
    first found). Mirrors required_check_types()'s own $and/$or-or-leaf
    traversal; never raises on an unrecognized shape.
    """
    if not isinstance(node, dict) or not node:
        return []

    for key in (*_AND_KEYS, *_OR_KEYS):
        if key in node:
            children = node[key] if isinstance(node[key], list) else [node[key]]
            found: list[dict] = []
            for child in children:
                found.extend(find_check_nodes(child, check_type))
            return found

    leaf_type, params = next(iter(node.items()))
    if leaf_type == check_type:
        return [params if isinstance(params, dict) else {}]
    return []


def split_condition_node(node):
    """
    One level of a rule's `conditions` tree, using the same $and/$or-or-leaf
    shape rules as render_condition_tree()/required_check_types() above:
    ("and", children), ("or", children), ("leaf", (check_type, params)), or
    (None, None) for an empty/malformed node - never raises. Lets the
    three-valued evaluator in service.py walk the tree without
    re-deriving (and possibly drifting from) those shape rules.
    """
    if not isinstance(node, dict) or not node:
        return None, None

    for key in _AND_KEYS:
        if key in node:
            return "and", node[key] if isinstance(node[key], list) else [node[key]]
    for key in _OR_KEYS:
        if key in node:
            return "or", node[key] if isinstance(node[key], list) else [node[key]]

    check_type, params = next(iter(node.items()))
    return "leaf", (check_type, params if isinstance(params, dict) else {})


def iter_leaf_nodes(node):
    """Every leaf of a rule's `conditions` tree as (check_type, params), in
    document order - the order the Debug-level device info summary's
    per-type entry lists were confirmed to follow on a real Windows log
    (27/27 process names, see nsdebug_parser.mask())."""
    kind, payload = split_condition_node(node)
    if kind == "leaf":
        yield payload
    elif kind is not None:
        for child in payload:
            yield from iter_leaf_nodes(child)


def _extract_device(record: dict) -> dict:
    """Field extraction against clientstatus's real confirmed shape (see
    module docstring) - a real raw response pasted directly into this
    project, 2026-09-27: `hostname` is top-level, `os` is under
    `host_info.os`, and the classification fields are under `user_info`
    (previously assumed top-level, which real data never actually
    populates - that was a real gap, not just an unconfirmed guess)."""
    host_info = record.get("host_info") if isinstance(record.get("host_info"), dict) else {}
    user_info = record.get("user_info") if isinstance(record.get("user_info"), dict) else {}
    return {
        "hostname": record.get("hostname") or host_info.get("hostname") or "(unknown hostname)",
        "os": record.get("os") or record.get("platform") or host_info.get("os") or "(unknown OS)",
        "device_classification_status": (
            user_info.get("device_classification_status")
            or record.get("device_classification_status")
            or "(unknown)"
        ),
        "device_classification_custom_status": (
            user_info.get("device_classification_custom_status")
            or record.get("device_classification_custom_status")
            or ""
        ),
    }


def fetch_client_status(tenant: str, token: str, email: str) -> list[dict]:
    """
    Real devices for one user, last 30 days, via a JQL-style `query`
    filter (confirmed working, CLAUDE.md 2026-09-22: `username eq
    'user@example.com'`). `starttime`/`endtime` (epoch seconds) are this project's
    own reasonable choice of parameter names for "a reasonable time
    window" - not independently confirmed as this specific endpoint's own
    time-window parameter names, since CLAUDE.md's note didn't specify
    them. If a real tenant call shows a different parameter name is
    required, fix it here - this is the one place that would need to
    change.

    `utcnow()` returns a naive datetime (timeutil.py's own convention,
    correct for DB storage) - but `.timestamp()` on a naive value assumes
    the SERVER's local system timezone, not UTC, so calling it directly
    here would silently send the wrong epoch on any host not itself set to
    UTC. Re-attaching `tzinfo=utc` first (the values already represent UTC
    wall-clock time, so this doesn't change what moment they mean) fixes
    it. Confirmed the hard way, 2026-09-27 - see CLAUDE.md Section 9.
    """
    now = utcnow()
    since = now - timedelta(days=_DAYS_WINDOW)
    url = f"{base_url(tenant)}/api/v2/events/datasearch/clientstatus"
    params = {
        "query": f"username eq '{email}'",
        "starttime": int(since.replace(tzinfo=timezone.utc).timestamp()),
        "endtime": int(now.replace(tzinfo=timezone.utc).timestamp()),
    }
    resp = request("GET", url, token, "fetching device client status", params=params)

    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError("Token was accepted but lacks the required scope (HTTP 403) to query device status.")
    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching device status (HTTP {resp.status_code}).")

    body = parse_json_body(resp)
    if not execution_ok(resp, body, ok_codes=(200,)):
        # CLAUDE.md: "HTTP 200 does not mean success" - without this check,
        # an HTTP-200-wrapped error body (bad query, revoked scope, etc.)
        # was indistinguishable from a genuine zero-device result. Confirmed
        # 2026-09-27. Uses execution_ok(), not body_ok() - this endpoint's
        # real confirmed convention is a `status` OBJECT with an
        # `execution` field, not a top-level `status` string, so the error
        # text on failure lives at status.message, not a top-level message.
        status = body.get("status") if isinstance(body.get("status"), dict) else {}
        raise NetskopeApiError(
            status.get("message") or "Netskope reported an error fetching device status, with no further detail in the response."
        )

    records = body.get("result", [])
    return [_extract_device(r) for r in records if isinstance(r, dict)]


def fetch_classification_rules(tenant: str, token: str) -> list[dict]:
    """
    The tenant's full device classification rule set. See module
    docstring - fetched in full and filtered client-side by OS
    (rules_for_os), since no server-side OS filter is confirmed to exist
    for this endpoint.

    Confirmed real success shape, 2026-09-27 (CLAUDE.md Section 9): a
    BARE top-level JSON array - no `status`/`data`/`result` wrapper at
    all, a third convention distinct from both body_ok() and
    execution_ok(). Neither of those helpers applies here (both assume a
    dict envelope) - success is simply "the parsed body is a list".
    Deliberately does NOT route through parse_json_body(), which would
    silently coerce a bare list into `{}` (it only ever returns a dict),
    hiding the real successful response behind a generic failure - this
    was a real, confirmed-by-tracing bug in the previous body_ok()/`data`
    version of this function (never caught because this code path has
    never actually run against a real tenant - see
    tests/test_device_posture.py's own header docstring), NOT a crash
    the way a naive body["status"] read on a list would suggest.
    """
    url = f"{base_url(tenant)}/api/v2/deviceclassification/rules"
    resp = request("GET", url, token, "fetching device classification rules")

    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError("Token was accepted but lacks the required scope (HTTP 403) to list classification rules.")
    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching classification rules (HTTP {resp.status_code}).")

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Classification rules response wasn't valid JSON.") from exc

    if not isinstance(parsed, list):
        # This endpoint's failure/error shape is NOT confirmed - no real
        # error response has ever been captured for it - so this is
        # deliberately a generic, clearly-labeled outcome rather than a
        # guess at a message field path (body["message"] or
        # body["status"]["message"]) that has never actually been seen
        # from this specific endpoint.
        raise NetskopeApiError(
            "Netskope returned an unexpected response shape fetching classification rules "
            "(expected a list of rules)."
        )

    items = unwrap_list(parsed)
    return [item for item in items if isinstance(item, dict)]


def rules_for_os(rules: list[dict], os_name: str) -> list[dict]:
    """
    OS-matching candidates for one device - case-insensitive substring
    match against whichever of a rule's own `os`/`platform`/`operating_system`
    fields is present (field name not independently confirmed by this
    project - checks all three plausible names defensively). Does NOT
    attempt to determine which single rule actually applies (CLAUDE.md:
    "NOT yet understood") - every OS-matching rule is a candidate.
    """
    os_name = (os_name or "").strip().lower()
    if not os_name or os_name == "(unknown os)":
        return []
    matches = []
    for rule in rules:
        rule_os = str(rule.get("os") or rule.get("platform") or rule.get("operating_system") or "").strip().lower()
        if rule_os and (rule_os in os_name or os_name in rule_os):
            matches.append(rule)
    return matches
