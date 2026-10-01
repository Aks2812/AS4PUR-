"""
Netskope API calls for RTP Creation. Shared HTTP plumbing lives in
app/operations/netskope_http.py (also used by Private App Import).

Reimplements reference_scripts/netskope_email_reconcile.py's
fetch_all_group_members() and reference_scripts/netskope_rtp_create_policy.py's
create+verify logic - preserving every documented gotcha from CLAUDE.md
Section 9 (the confirmed NPA rules payload shape, `enabled` as a string
not a boolean, `group_id` must come from a live fetch never a UI ordinal,
HTTP 200 does not mean success) - just via requests instead of curl
subprocesses.

`GET /api/v2/policy/npa/policygroups`'s response envelope is confirmed
real (captured from both two different tenants, different actual
groups each time, identical envelope structure both times): a flat
array directly under `data`, no nested sub-key - unlike publishers
(nested under `data.publishers`) or getusers (page total under
`counts.totalResults` instead of a top-level `total`). Every entry
carries `group_id` and `group_name` as strings.

`fetch_group_members` returns an `IdentityDirectory` (two lookup dicts,
by userName and by email), not a single flat dict - real customer file
evidence (sample-file.xlsx) confirmed a single upload can mix bare
UPN-style SAM Account Name values with ones that are already the
employee's real email address, per row, so both identifier shapes must
be checked. See IdentityDirectory's own docstring for why these stay two
separate dicts rather than being merged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..netskope_http import NetskopeApiError, base_url, body_ok, parse_json_body, request

PAGE_LIMIT = 200  # confirmed safe page size for getusers (reference_scripts/
                   # netskope_email_reconcile.py) - a limit=2000 test produced
                   # a non-JSON error response, never diagnosed further
MAX_PAGES = 100    # generous safety cap, not a per-tenant size assumption -
                    # One real customer's case was ~1526 users / 200 per page = ~8

__all__ = [
    "NetskopeApiError",
    "IdentityDirectory",
    "fetch_group_members",
    "fetch_policy_groups",
    "create_rtp_rule",
    "fetch_rule_by_id",
    "summarize_rule",
    "fetch_rules",
    "patch_rule_users",
]


@dataclass
class IdentityDirectory:
    """
    Two independent lookup indices built from the same getusers population,
    covering both real-world identifier shapes confirmed present in the
    SAME uploaded file (real customer case, sample-file.xlsx: most SAM Account
    Name values were the employee's real corporate email address, e.g.
    `first.last@example.invalid`, but a few were genuine bare UPN-style
    identifiers, e.g. `first20091@example.invalid` - a per-row mix, not a
    per-file or per-column convention).

    `by_username`: lowercased accounts[].userName -> real email (the
    original, reference-script convention).
    `by_email`: lowercased top-level emails[] entries -> real email - one
    entry per address when a user has more than one, all pointing at the
    same canonical `emails[0]` value.

    Deliberately kept as two separate dicts rather than merged into one:
    resolve_identities() checks both independently for every row, even
    after a hit, specifically to detect the case where the same raw input
    string coincidentally matches two DIFFERENT real accounts (a userName
    hit on one, an email hit on another) - a single merged dict could only
    ever return one answer and would silently hide that collision.

    `provisioner_by_username` / `provisioner_by_email`: same keys as
    `by_username`/`by_email`, mapped to that account's `provisioner` value
    (e.g. "SCIM" or "Local") instead of its email - CLAUDE.md Section 9,
    decision made 2026-09-07: the full-directory search no longer excludes
    Local-provisioned accounts, so a matched row's provisioner is recorded
    for later traceability (row-level audit note only, never surfaced in
    Review Gate 1's table) rather than used to filter anything.
    """
    by_username: dict[str, str] = field(default_factory=dict)
    by_email: dict[str, str] = field(default_factory=dict)
    provisioner_by_username: dict[str, str] = field(default_factory=dict)
    provisioner_by_email: dict[str, str] = field(default_factory=dict)


def fetch_group_members(tenant: str, token: str, group_name: str | None = None) -> IdentityDirectory:
    """
    Every member of the operator-specified group, tenant-wide, indexed two
    ways (see IdentityDirectory). Confirmed real behavior (reference_scripts/
    netskope_email_reconcile.py): the server-side group filter is accurate,
    and the page total lives at `counts.totalResults`, NOT the top-level
    `total` field the private-apps/publishers endpoints use - a genuinely
    different shape, not an oversight.

    `group_name` falsy (None or "") searches the FULL tenant directory
    instead of one group - no other filtering. CLAUDE.md Section 9,
    decision made 2026-09-07: the `accounts.provisioner: {"eq": "SCIM"}`
    pre-filter this path used to apply (excluding Local-provisioned
    accounts) has been REMOVED - a match is a match regardless of the
    underlying account's provisioner, full stop, matching the same
    reasoning already established for identity resolution generally (a
    vendor/lab/local-admin account can't spuriously match a name that only
    exists in another company's uploaded HR file, so excluding Local
    accounts up front was defense against a risk that isn't actually
    real). The provisioner value is still recorded per matched account
    (see IdentityDirectory) for later traceability, just never used to
    filter or flag anything visible during the run.
    """
    url = f"{base_url(tenant)}/api/v2/users/getusers"
    by_username: dict[str, str] = {}
    by_email: dict[str, str] = {}
    provisioner_by_username: dict[str, str] = {}
    provisioner_by_email: dict[str, str] = {}
    offset = 0
    total = None
    pages = 0

    if group_name:
        filter_clause = {
            "and": [
                {"accounts.parentGroups": {"eq": group_name}},
                {"accounts.deleted": {"eq": False}},
            ]
        }
    else:
        # Single-condition "and" array, deliberately - matches the same
        # {"and": [...]} shape the group-scoped filter above already sends
        # (confirmed working), rather than introducing an unconfirmed bare
        # single-condition filter shape of its own.
        filter_clause = {"and": [{"accounts.deleted": {"eq": False}}]}

    while True:
        body = {
            "query": {
                "paging": {"offset": offset, "limit": PAGE_LIMIT},
                "filter": filter_clause,
            }
        }
        resp = request("POST", url, token, "fetching group members", json=body)
        if resp.status_code != 200:
            raise NetskopeApiError(f"Unexpected response fetching group members (HTTP {resp.status_code}).")
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise NetskopeApiError("Group-members response wasn't valid JSON.") from exc

        if total is None:
            total = parsed.get("counts", {}).get("totalResults")

        data = parsed.get("data", []) if isinstance(parsed, dict) else []
        for user in data:
            emails_list = user.get("emails") or []
            real_email = emails_list[0] if emails_list else None
            accounts = user.get("accounts", []) or []
            active_accounts = [a for a in accounts if not a.get("deleted")]
            if not active_accounts or not real_email:
                continue  # matches the old by_username-only behavior exactly:
                          # a user with no non-deleted account, or no email
                          # on file at all, contributes nothing to either index
            for acct in active_accounts:
                uname = (acct.get("userName") or "").strip().lower()
                if uname:
                    by_username[uname] = real_email
                    provisioner_by_username[uname] = acct.get("provisioner")
            # emails[] is a user-level field, not per-account - a user with
            # more than one active account (mixed provisioner, in theory)
            # only has one email list, so the FIRST active account's
            # provisioner is used as this index's representative value,
            # the same approximation real_email already makes via emails[0].
            representative_provisioner = active_accounts[0].get("provisioner")
            for addr in emails_list:
                key = (addr or "").strip().lower()
                if key:
                    by_email[key] = real_email
                    provisioner_by_email[key] = representative_provisioner

        pages += 1
        if len(data) == 0:
            break
        offset += PAGE_LIMIT
        if isinstance(total, int) and offset >= total:
            break
        if pages >= MAX_PAGES:
            break

    return IdentityDirectory(
        by_username=by_username, by_email=by_email,
        provisioner_by_username=provisioner_by_username, provisioner_by_email=provisioner_by_email,
    )


def fetch_policy_groups(tenant: str, token: str) -> list[dict]:
    """
    Live policy groups for the `group_id` pre-flight step (CLAUDE.md
    Section 7/9 - never trust a UI-displayed ordinal as the real
    `group_id`). Confirmed real shape - see this module's docstring: a
    flat array directly under `data`, each entry carrying `group_id` and
    `group_name` as strings.
    """
    url = f"{base_url(tenant)}/api/v2/policy/npa/policygroups"
    resp = request("GET", url, token, "fetching policy groups")

    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError("Token was accepted but lacks the required scope (HTTP 403) to list policy groups.")
    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching policy groups (HTTP {resp.status_code}).")

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Policy-groups response wasn't valid JSON.") from exc

    items = parsed.get("data", []) if isinstance(parsed, dict) else []
    return [{"group_id": item.get("group_id"), "group_name": item.get("group_name", "(unnamed)")} for item in items]


def create_rtp_rule(tenant: str, token: str, payload: dict) -> tuple[bool, str, str | None]:
    """
    POSTs one NPA rule. CLAUDE.md Section 9: HTTP 200/201 does not mean
    success - the body's own `status` field is the only thing trusted
    here. Returns (ok, detail, rule_id) - rule_id is None on failure or if
    the response didn't include one.
    """
    url = f"{base_url(tenant)}/api/v2/policy/npa/rules"
    resp = request("POST", url, token, "creating an RTP rule", json=payload)
    body = parse_json_body(resp)

    if body_ok(resp, body):
        rule_id = body.get("data", {}).get("rule_id")
        return True, f"HTTP {resp.status_code}: {body.get('message', 'created')}", rule_id

    return False, f"HTTP {resp.status_code}: {body.get('message', resp.text[:300])}", None


def fetch_rule_by_id(tenant: str, token: str, rule_id: str) -> dict:
    """
    GET one NPA rule by ID. Used two ways: the independent post-creation
    verification pass (reference_scripts/netskope_rtp_create_policy.py's
    own check: confirm the full user list actually landed, not truncated,
    before ever reporting success) - where rule_id is always a
    just-created ID, guaranteed to exist - and, as of the "add users to an
    existing rule" feature, an operator-typed rule_id that might be wrong
    (bad ID, wrong tenant) and needs to fail clearly right here.

    CLAUDE.md Section 9: HTTP 200 does not mean success - a bad ID returns
    HTTP 200 with `{"message": "policy doesn't exist with id:X", "status":
    "error"}`, not a 404. Checking only `resp.status_code` (the original
    version of this function, before the existing-rule lookup use case
    existed) would have silently returned an empty/garbage result instead
    of a clear error for that case - now raises NetskopeApiError with the
    real message instead.

    Raises NetskopeApiError (never returns None) - every caller can treat
    a successful return as a real rule_data dict.
    """
    url = f"{base_url(tenant)}/api/v2/policy/npa/rules/{rule_id}"
    resp = request("GET", url, token, "fetching the RTP rule")

    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching rule {rule_id} (HTTP {resp.status_code}).")

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Rule-fetch response wasn't valid JSON.") from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(parsed, dict) or not body_ok(resp, parsed, ok_codes=(200,)) or not isinstance(data, dict):
        message = parsed.get("message") if isinstance(parsed, dict) else None
        raise NetskopeApiError(message or f"Rule {rule_id} was not found, or the response didn't include rule data.")

    return data


def summarize_rule(rule_id: str, raw: dict) -> dict:
    """
    Shared shape for a single rule, used both by the "Enter a rule ID
    manually" lookup (one raw dict, from fetch_rule_by_id) and the rule
    picker (one raw dict per entry of fetch_rules()'s list) - so both
    paths show identical fields for a rule, and Step 2's confirmation
    screen doesn't need its own separate extraction logic.
    """
    rule_data = raw.get("rule_data") or {}
    return {
        "rule_id": rule_id,
        "rule_name": raw.get("rule_name") or "(unnamed)",
        "enabled": str(raw.get("enabled")) == "1",
        "access_method": rule_data.get("access_method") or [],
        "private_apps": rule_data.get("privateApps") or [],
        "current_users": rule_data.get("users") or [],
    }


def fetch_rules(tenant: str, token: str) -> list[dict]:
    """
    Live list of every NPA rule, for the "add users to an existing rule"
    picker (Step 2) - confirmed real envelope: `GET /api/v2/policy/npa/rules`
    wraps a flat array under `data`, same convention as fetch_policy_groups
    (not a bare array). Small enough tenant-wide to fetch-all and filter
    client-side (16 rules on one real tenant) - same no-pagination approach as
    the publisher/app pickers elsewhere in this app.

    NOT YET independently confirmed by this project: whether each entry
    in this LIST response nests `rule_data` (access_method/privateApps/
    users) identically to a single GET-by-id's `data` object - this
    project has a real confirmed payload for POST (create) and GET-by-id,
    but not for this list endpoint's per-item shape specifically. Built
    here assuming the same shape via summarize_rule() (fields default to
    empty/0 rather than crashing if a real response differs) - flagged for
    real-tenant verification, same discipline as every other "confirmed
    vs. assumed" shape in this project.
    """
    url = f"{base_url(tenant)}/api/v2/policy/npa/rules"
    resp = request("GET", url, token, "fetching the list of RTP rules")

    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError("Token was accepted but lacks the required scope (HTTP 403) to list rules.")
    if resp.status_code != 200:
        raise NetskopeApiError(f"Unexpected response fetching the rule list (HTTP {resp.status_code}).")

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Rules-list response wasn't valid JSON.") from exc

    items = parsed.get("data", []) if isinstance(parsed, dict) else []
    summaries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rule_id = item.get("rule_id")
        if rule_id is None:
            continue
        summaries.append(summarize_rule(str(rule_id), item))
    return summaries


def patch_rule_users(tenant: str, token: str, rule_id: str, merged_users: list[str]) -> tuple[bool, str]:
    """
    PATCHes ONLY `rule_data.users` - nothing else in the body. CLAUDE.md
    Section 9's confirmed PATCH semantics (verified 2026-09-06, a lab tenant
    rule_id=130): any field included in `rule_data` fully REPLACES its
    current value, it does not merge. `merged_users` must therefore
    already be the COMPLETE target array (every user who should remain,
    old and new) - computed by service.build_merge_preview() before this
    is ever called. This function does no merging itself, on purpose, to
    keep that one responsibility in exactly one place. Fields left out of
    this body (access_method, privateApps, match_criteria_action, etc.)
    are confirmed left untouched by the same real-tenant test - a minimal
    body is safe here without round-tripping the rest of the rule.
    """
    url = f"{base_url(tenant)}/api/v2/policy/npa/rules/{rule_id}"
    payload = {"rule_data": {"users": merged_users}}
    resp = request("PATCH", url, token, "adding users to an existing RTP rule", json=payload)
    body = parse_json_body(resp)

    if body_ok(resp, body):
        return True, f"HTTP {resp.status_code}: {body.get('message', 'updated')}"

    return False, f"HTTP {resp.status_code}: {body.get('message', resp.text[:300])}"
