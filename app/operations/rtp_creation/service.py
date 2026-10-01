"""
Business logic for RTP Creation, split into Stage A (identity resolution)
and Stage B (rule creation) matching CLAUDE.md Section 7's confirmed
two-gate workflow. Stage A is a synchronous dry-run (same shape as
Private App Import's upload-time reconciliation - fast enough not to need
a background job); Stage B's actual create+verify is the one thing that
becomes a background Job, since it's the one thing that writes anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from .netskope_client import (
    IdentityDirectory,
    NetskopeApiError,
    create_rtp_rule,
    fetch_group_members,
    fetch_rule_by_id,
    patch_rule_users,
)

__all__ = [
    "NetskopeApiError",
    "IdentityDirectory",
    "IdentityRow",
    "IdentityResolution",
    "NormalizerError",
    "parse_identity_workbook",
    "resolve_identities",
    "notable_identity_rows",
    "build_rule_payload",
    "run_create_rule_job",
    "MergePreview",
    "build_merge_preview",
    "run_add_users_to_rule_job",
]

REQUIRED_COLUMNS = ["SAM Account Name", "Display Name"]


class NormalizerError(Exception):
    """A fatal problem with the uploaded file itself (missing columns,
    unreadable workbook) - mirrors reference_scripts/netskope_email_reconcile.py's
    own fatal-exit cases."""


@dataclass
class IdentityRow:
    upn: str
    display_name: str | None
    email: str | None = None
    # MATCHED | UNMATCHED | AMBIGUOUS_MATCH - MATCHED/UNMATCHED reuse
    # reference_scripts/netskope_email_reconcile.py's own vocabulary (its
    # matched/unmatched output lists); AMBIGUOUS_MATCH is a new AS4PUR
    # addition (not present in the reference script, which only ever
    # checked userName) for the case where the same raw input value
    # independently matches two DIFFERENT real accounts - one via
    # userName, a different one via email - and neither can be picked
    # automatically.
    status: str = "PENDING"
    # "username" | "email" | None - which index actually resolved the
    # match (None for UNMATCHED/AMBIGUOUS_MATCH). Not shown on Review Gate
    # 1's table itself (that stays exactly as it was) - surfaced instead
    # in the job's row-by-row audit trail (see notable_identity_rows /
    # run_create_rule_job) so an "matched via email fallback" case stays
    # traceable later if something looks wrong.
    match_source: str | None = None
    # The matched account's `provisioner` value (e.g. "SCIM" or "Local"),
    # None for UNMATCHED/AMBIGUOUS_MATCH. CLAUDE.md Section 9, decision
    # made 2026-09-07: the full-directory search no longer excludes
    # Local-provisioned accounts, so this is recorded for every match now
    # (not just an edge case) - same low-visibility treatment as
    # match_source: job detail/audit trail only, never Review Gate 1.
    provisioner: str | None = None


@dataclass
class IdentityResolution:
    rows: list[IdentityRow]
    matched_count: int
    unmatched_count: int
    # Rows where the raw input value matched two different real accounts
    # at once (a userName hit on one, an email hit on another) - excluded
    # from matched_count/the rule entirely, and from unmatched_count too
    # (this isn't "no match", it's "too many matches to trust"). Its own
    # bucket so Review Gate 1 can surface it explicitly rather than
    # silently guessing which account was meant.
    ambiguous_count: int = 0
    # None/"" means this run searched the full tenant directory rather
    # than one SCIM group - Review Gate 1 must say which happened, not
    # just report matched/unmatched counts, so an operator reviewing the
    # results understands what population was actually searched.
    scim_group: str | None = None
    # Size of the searched candidate pool (distinct usernames in the
    # group, or tenant-wide SCIM-provisioned accounts when scim_group is
    # empty) - CLAUDE.md Section 9: full-directory search is confirmed to
    # exclude locally-created admin/service accounts, this number
    # reflects that exclusion.
    population_size: int = 0
    # The IdentityDirectory built during this resolution - kept here
    # (rather than discarded once matching is done) so the "add users to
    # an existing rule" path can reuse the exact same by_username/by_email
    # population to proactively check the TARGET RULE's existing users for
    # staleness, with zero extra API calls (see build_merge_preview).
    # Optional/defaulted because IdentityResolution predates this need and
    # some older test construction sites don't care about it.
    directory: "IdentityDirectory | None" = None


def parse_identity_workbook(xlsx_path: Path) -> list[IdentityRow]:
    """Reuses reference_scripts/netskope_email_reconcile.py's exact
    expected columns (SAM Account Name, Display Name) - XLSX only,
    matching the reference script (it never accepted CSV)."""
    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
    except Exception as exc:
        raise NormalizerError(f"Could not open the uploaded file: {exc}") from exc

    headers = [c.value for c in ws[1]]
    missing = [h for h in REQUIRED_COLUMNS if h not in headers]
    if missing:
        found = ", ".join(str(h) for h in headers if h is not None)
        raise NormalizerError(f"Missing required column(s): {', '.join(missing)}. Found columns: {found}")
    upn_col = headers.index("SAM Account Name")
    display_col = headers.index("Display Name")

    rows: list[IdentityRow] = []
    for raw_row in ws.iter_rows(min_row=2, values_only=True):
        upn = raw_row[upn_col]
        if upn is None or not str(upn).strip():
            continue  # blank trailing row
        display_name = raw_row[display_col]
        rows.append(IdentityRow(upn=str(upn).strip(), display_name=str(display_name).strip() if display_name else None))

    return rows


def resolve_identities(rows: list[IdentityRow], tenant: str, token: str, scim_group: str | None, email_domain: str) -> IdentityResolution:
    """
    Mutates each row's `email`/`status`/`match_source` in place. Base
    matching logic matches reference_scripts/netskope_email_reconcile.py's
    exact behavior - case-insensitive match against the live group's
    accounts[].userName - plus the CLAUDE.md Section 9 domain-append
    defensive fix, which is NOT present in the reference script itself: a
    bare SAM Account Name with no "@" at all has caused a silent 0% match
    rate twice on real customer files, so a missing domain is defensively
    appended before the lookup, using whatever domain the operator
    supplied for this tenant (Section 10 - tenant-specific, never
    assumed).

    Extended beyond the reference script (real customer case, sample-file.xlsx):
    a userName miss now falls back to a lookup against the same
    population's real email addresses (IdentityDirectory.by_email) -
    deliberately NOT a "detect whether this file uses UPN or email"
    classification step, since the same real file demonstrably mixes both
    conventions per-row. Both indices are always checked for every row
    (not just on a username miss) so a rare case where the raw value
    independently matches two DIFFERENT real accounts - one via userName,
    a different one via email - is caught as AMBIGUOUS_MATCH rather than
    silently resolved to whichever index happened to be checked first.

    `scim_group` blank/None searches the full tenant directory instead of
    one group (CLAUDE.md Section 9 - confirmed real-tenant behavior,
    verified 2026-09-06 against a real tenant: this is a "search the full employee
    directory" feature, not a fix for the historical groupless-SCIM-user
    incident described elsewhere in Section 9, which real-tenant testing
    did not reproduce). Decision made 2026-09-07: this path no longer
    excludes Local-provisioned accounts either - a match is MATCHED
    regardless of provisioner, full stop (see fetch_group_members' own
    docstring for the reasoning). The matched account's provisioner is
    still recorded per row (see IdentityRow.provisioner) for later
    traceability, never used here to change matching outcomes.
    """
    scim_group = (scim_group or "").strip() or None
    directory = fetch_group_members(tenant, token, scim_group)
    domain = email_domain.strip()
    if domain and not domain.startswith("@"):
        domain = f"@{domain}"

    matched = unmatched = ambiguous = 0
    for row in rows:
        key = row.upn.strip().lower()
        if "@" not in key and domain:
            key = f"{key}{domain.lower()}"

        username_hit = directory.by_username.get(key)
        email_hit = directory.by_email.get(key)

        if username_hit and email_hit and username_hit != email_hit:
            # Same raw value, two different real accounts - do not guess.
            row.email = None
            row.match_source = None
            row.provisioner = None
            row.status = "AMBIGUOUS_MATCH"
            ambiguous += 1
        elif username_hit:
            row.email = username_hit
            row.match_source = "username"
            row.provisioner = directory.provisioner_by_username.get(key)
            row.status = "MATCHED"
            matched += 1
        elif email_hit:
            row.email = email_hit
            row.match_source = "email"
            row.provisioner = directory.provisioner_by_email.get(key)
            row.status = "MATCHED"
            matched += 1
        else:
            row.status = "UNMATCHED"
            row.match_source = None
            row.provisioner = None
            unmatched += 1

    return IdentityResolution(
        rows=rows, matched_count=matched, unmatched_count=unmatched,
        ambiguous_count=ambiguous, scim_group=scim_group,
        population_size=len(directory.by_username),
        directory=directory,
    )


def notable_identity_rows(rows: list[IdentityRow]) -> list[tuple[str, str, str | None, str | None]]:
    """
    The rows worth calling out individually in the job's row-by-row audit
    trail (run_create_rule_job / run_add_users_to_rule_job): every MATCHED
    row (whichever index resolved it) plus every AMBIGUOUS_MATCH row.
    UNMATCHED rows are excluded - already fully covered by the aggregate
    count, nothing more to say about them.

    Widened from "email-fallback + ambiguous only" to "every matched row"
    on 2026-09-07, the same day the full-directory search stopped
    excluding Local-provisioned accounts (see fetch_group_members):
    provisioner traceability is the actual point now, and it only works
    if every match carries it, not just the unusual fallback/ambiguous
    cases - "purely for later traceability if a specific match is ever
    questioned" (any match, since the population now includes accounts
    that used to be filtered out entirely).

    Returns (upn, status, match_source, provisioner) tuples. Still never
    shown on Review Gate 1's table itself - job detail/audit trail only.
    """
    return [
        (row.upn, row.status, row.match_source, row.provisioner)
        for row in rows
        if row.status != "UNMATCHED"
    ]


def _record_identity_resolution_notes(
    progress,
    matched_count: int,
    unmatched_count: int,
    ambiguous_count: int,
    notable_rows: list[tuple[str, str, str | None, str | None]] | None,
) -> None:
    """
    Shared between run_create_rule_job and run_add_users_to_rule_job - one
    aggregate summary line, plus one note per notable_identity_rows() row
    (every MATCHED row plus every AMBIGUOUS_MATCH row), so both job types
    record identity resolution to the audit trail identically.
    """
    summary_line = f"{matched_count} matched, {unmatched_count} unmatched"
    if ambiguous_count:
        summary_line += f", {ambiguous_count} ambiguous (excluded, needs manual review)"
    summary_line += " from the uploaded file (Stage A)."
    progress.record_note("identity_resolution", "INFO", summary_line)

    for upn, status, match_source, provisioner in notable_rows or []:
        if status == "AMBIGUOUS_MATCH":
            progress.record_note(
                "identity_resolution", "AMBIGUOUS_MATCH",
                f"{upn}: matched two different real accounts at once (one via userName, "
                f"a different one via email) - excluded from this rule, needs manual review.",
            )
        elif match_source == "email":
            progress.record_note(
                "identity_resolution", "INFO",
                f"{upn}: matched via email fallback (no userName match; the value itself "
                f"is a known account's real email address) (provisioner: {provisioner or 'unknown'}).",
            )
        elif match_source == "username":
            progress.record_note(
                "identity_resolution", "INFO",
                f"{upn}: matched via username (provisioner: {provisioner or 'unknown'}).",
            )


def build_rule_payload(rule_name: str, group_id: str, access_method: str, private_apps: list[str], emails: list[str]) -> dict:
    """Confirmed real NPA rules create payload shape (CLAUDE.md Section 9),
    matching reference_scripts/netskope_rtp_create_policy.py exactly.
    `enabled` is ALWAYS "0" (string, not boolean) - never settable to
    anything else here, on purpose (see run_create_rule_job's docstring)."""
    return {
        "rule_name": rule_name,
        "enabled": "0",
        "group_id": group_id,
        "policy_type": "private-app",
        "rule_data": {
            "policy_type": "private-app",
            "access_method": [access_method],
            "userType": "user",
            "users": emails,
            "privateApps": private_apps,
            "match_criteria_action": {"action_name": "allow"},
            "external_dlp": False,
            "json_version": 3,
            "show_dlp_profile_action_table": False,
        },
    }


def run_create_rule_job(
    progress,
    tenant: str,
    token: str,
    rule_name: str,
    group_id: str,
    access_method: str,
    private_apps: list[str],
    emails: list[str],
    matched_count: int,
    unmatched_count: int,
    ambiguous_count: int = 0,
    notable_rows: list[tuple[str, str, str | None]] | None = None,
) -> None:
    """
    One rule per run (CLAUDE.md Section 7: one user-set + one app-set = one
    rule, confirmed scope boundary - no auto-splitting). `progress.set_totals(1)`
    reflects that this job accomplishes exactly one thing (create the
    rule); the identity-resolution summary and verification outcome are
    recorded as supplementary notes (record_note), not counted against
    that total, the same pattern Private App Import's post-creation
    verification pass already uses.

    `notable_rows` (from `notable_identity_rows()`) adds one audit-trail
    note per email-fallback match or AMBIGUOUS_MATCH row, on top of the
    single aggregate summary line below - this is the "row-by-row audit
    detail" a "matched via email fallback" or ambiguous case stays
    traceable in later, without touching Review Gate 1's own table.
    `ambiguous_count` defaults to 0 and `notable_rows` to None so existing
    callers that predate this feature keep working unchanged.
    """
    progress.set_totals(1)
    _record_identity_resolution_notes(progress, matched_count, unmatched_count, ambiguous_count, notable_rows)

    payload = build_rule_payload(rule_name, group_id, access_method, private_apps, emails)
    try:
        ok, detail, rule_id = create_rtp_rule(tenant, token, payload)
    except NetskopeApiError as exc:
        progress.record_item("rule", "FAILED", str(exc))
        raise

    if not ok:
        progress.record_item("rule", "FAILED", detail)
        return

    progress.record_item("rule", "SUCCESS", f"{detail} (rule_id={rule_id}, created DISABLED)")

    # Independent verification (reference_scripts/netskope_rtp_create_policy.py's
    # own check): confirm the full user list actually landed, not
    # truncated, before this is ever treated as trustworthy - never just
    # trust the create response alone.
    try:
        rule = fetch_rule_by_id(tenant, token, rule_id)
    except NetskopeApiError as exc:
        progress.set_summary(
            f"Independent verification could not run: {exc}. The create response above is the only "
            f"confirmation available - do not enable this rule until it's been checked manually."
        )
        return

    stored_users = rule.get("rule_data", {}).get("users", [])
    expected = {e.lower() for e in emails}
    stored = {u.lower() for u in stored_users}
    missing = expected - stored

    if len(stored_users) != len(emails) or missing:
        detail_msg = f"Stored user count {len(stored_users)} vs. expected {len(emails)}."
        if missing:
            sample = ", ".join(sorted(missing)[:10])
            detail_msg += f" Missing from the stored rule: {sample}" + (" (+more)" if len(missing) > 10 else "") + "."
        progress.record_note("rule", "MISMATCH", detail_msg)
        progress.set_summary("Independent verification found a mismatch - see the note above. Do NOT enable this rule until investigated.")
    else:
        progress.record_note("rule", "VERIFIED", f"Confirmed: all {len(emails)} users are present in the stored rule.")
        progress.set_summary(
            f"Rule created and verified (rule_id={rule_id}), currently DISABLED. Enabling it is a separate, "
            f"deliberate step - review it visually in the Netskope UI under the target policy group first."
        )


# --- "Add users to an existing rule" - reuses Stage A unchanged, replaces
# --- Stage B's create-a-new-rule path with a PATCH-and-merge path -------


@dataclass
class MergePreview:
    """
    The Review Gate 2 comparison for the "add users to an existing rule"
    path: what the target rule's `rule_data.users` looks like today
    versus what Stage A resolved, split into the three buckets the
    operator needs to see explicitly (never silently skip the
    already-present ones - CLAUDE.md Section 6's own review-before-write
    discipline applies here exactly as everywhere else in this app).
    """
    current_users: list[str]
    matched_emails: list[str]
    already_present: list[str]
    new_users: list[str]
    # Layer 1 of the stale-user hardening (2026-09-08): existing-rule
    # users that DON'T resolve against the same directory population
    # Stage A already fetched - a normal, expected condition on a real
    # long-lived rule (someone left the company, an account got renamed),
    # not an edge case. These are excluded from `merged_users` below
    # PROACTIVELY, before any PATCH is attempted, because Netskope rejects
    # the entire `users` array if even one value is stale (confirmed
    # error: "Invalid values from users, userGroups or organization_units:
    # {...}") - submitting a known-bad value and hoping is not an option.
    # Shown to the operator as its own explicit category on Review Gate 2
    # (existing_rule_review.html), same visual weight as the enabled-rule
    # warning - never folded silently into the general confirm checkbox.
    stale_existing_users: list[str]
    # The COMPLETE array to PATCH with - the SURVIVING current users
    # (current_users minus stale_existing_users) plus new_users appended.
    # CLAUDE.md Section 9's confirmed PATCH replace semantics (verified
    # 2026-09-06, a lab tenant rule_id=130) is exactly why this must already be
    # the full target list before patch_rule_users() is ever called -
    # PATCHing with only new_users would silently drop every user already
    # on the rule.
    merged_users: list[str]


def build_merge_preview(current_users: list[str], matched_emails: list[str], directory: IdentityDirectory) -> MergePreview:
    """
    Case-insensitive comparison, per the confirmed design. `current_users`
    entries are kept verbatim (their existing stored casing is never
    rewritten) - only genuinely new emails are appended, and only once
    each (deduped against each other too, in case Stage A's matched list
    contains the same real email twice via two different uploaded rows -
    e.g. one row matched by userName, another by email fallback, for the
    same underlying person under two different raw identifiers).

    `directory` is the SAME IdentityDirectory Stage A already fetched for
    this run (see IdentityResolution.directory) - reused here, zero extra
    API calls, to proactively check whether each of the target rule's
    EXISTING users still resolves to a real, active account. Checked
    against BOTH by_email and by_username (case-insensitive), the same
    dual-index convention resolve_identities() itself uses for uploaded
    rows - a rule's stored `users[]` entries are always real email
    addresses in the confirmed schema, so by_email is the primary check,
    but by_username is checked too defensively (in case a bare UPN-shaped
    string ever ended up stored some other way, e.g. typed directly into
    the Netskope UI, bypassing this app entirely).

    A stale existing user is dropped from the comparison entirely before
    the already-present/new split runs, so it can never accidentally
    reappear in `merged_users` and can never be double-counted against
    `matched_emails` (which, by construction, can only ever contain
    values that DO resolve against this exact directory - see
    resolve_identities - so a value can never be simultaneously "stale"
    here and "matched" there).
    """
    surviving_current: list[str] = []
    stale_existing_users: list[str] = []
    for u in current_users:
        key = u.strip().lower()
        if key in directory.by_email or key in directory.by_username:
            surviving_current.append(u)
        else:
            stale_existing_users.append(u)

    current_lower = {u.lower() for u in surviving_current}
    already_present: list[str] = []
    new_users: list[str] = []
    seen_new_lower: set[str] = set()

    for email in matched_emails:
        key = email.lower()
        if key in current_lower:
            already_present.append(email)
        elif key not in seen_new_lower:
            new_users.append(email)
            seen_new_lower.add(key)

    return MergePreview(
        current_users=list(current_users),
        matched_emails=list(matched_emails),
        already_present=already_present,
        new_users=new_users,
        stale_existing_users=stale_existing_users,
        merged_users=surviving_current + new_users,
    )


# Layer 2 (reactive) hardening constants (2026-09-08). See
# _parse_invalid_values_error and run_add_users_to_rule_job.
MAX_PATCH_ATTEMPTS = 5
MAX_TOTAL_EXCLUDED_USERS = 15
MAX_EXCLUDED_FRACTION = 0.25

# Confirmed real Netskope PATCH error format for a stale users/userGroups/
# organization_units value: "Invalid values from users, userGroups or
# organization_units: {...}" with the exact rejected raw values inside the
# braces. Anchored on the fixed phrase, tolerant of whatever's inside the
# braces - matched by _parse_invalid_values_error, never guessed at.
_INVALID_VALUES_ERROR_RE = re.compile(
    r"Invalid values from users, userGroups or organization_units:\s*\{(.*)\}", re.DOTALL
)


def _parse_invalid_values_error(message: str) -> list[str] | None:
    """
    Extracts the exact rejected values from Netskope's confirmed PATCH
    error message for a stale users/userGroups/organization_units entry.
    Returns None - not an empty list, not a best-effort guess - for
    anything that doesn't match this EXACT confirmed pattern: written
    defensively against the error message format ever changing, per the
    design brief's explicit instruction to fail clean rather than
    guess-parse. A None return tells the caller to treat this as an
    ordinary, unparseable API failure (record it and stop), not a
    stale-user condition it can retry around.
    """
    match = _INVALID_VALUES_ERROR_RE.search(message)
    if not match:
        return None
    inner = match.group(1).strip()
    if not inner:
        return None

    values: list[str] = []
    for token in inner.split(","):
        token = token.strip()
        # Tolerate a Python-repr-style set/list ('x', "x") as well as bare,
        # unquoted comma-separated values - strip one matching layer of
        # quotes either way, never more.
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        if token:
            values.append(token)
    return values or None


def run_add_users_to_rule_job(
    progress,
    tenant: str,
    token: str,
    rule_id: str,
    rule_name: str,
    merged_users: list[str],
    already_present_count: int,
    new_count: int,
    matched_count: int,
    unmatched_count: int,
    ambiguous_count: int = 0,
    notable_rows: list[tuple[str, str, str | None]] | None = None,
    stale_existing_users: list[str] | None = None,
    original_rule_user_count: int = 0,
) -> None:
    """
    Adds users to an EXISTING rule via PATCH, instead of creating a new
    one. `merged_users` must already be the full target array (see
    MergePreview/build_merge_preview) - this function does no merging of
    its own, on purpose.

    Two layers of defense against stale users on the target rule (2026-
    09-08), a normal condition on a real long-lived rule, not an edge
    case:

    Layer 1 (proactive) already happened before this job ever started -
    `merged_users` has already had build_merge_preview()'s
    stale_existing_users excluded, and the operator already saw that
    category explicitly on Review Gate 2. `stale_existing_users` /
    `original_rule_user_count` are passed through here purely so this
    job's own audit trail can distinguish "excluded before you confirmed"
    from Layer 2's "excluded during the write itself, a surprise" below -
    recorded as an INFO note only, since the operator already reviewed
    and accepted this before confirming.

    Layer 2 (reactive) handles whatever Layer 1's directory snapshot
    missed - the directory was fetched once, earlier in this same run;
    a real-world race (an account deactivated between then and the PATCH
    actually running) or any other stale value Layer 1's check didn't
    catch is still possible. If patch_rule_users() comes back with the
    confirmed "Invalid values from users, userGroups or
    organization_units: {...}" pattern, the exact rejected values are
    parsed out (_parse_invalid_values_error), removed from the payload,
    and the PATCH is retried - capped at MAX_PATCH_ATTEMPTS total
    attempts, and stopping entirely (failing the job, not the rule) if
    the combined proactive+reactive exclusion count would exceed
    MAX_TOTAL_EXCLUDED_USERS or MAX_EXCLUDED_FRACTION of the rule's
    original user count. An error that doesn't match the confirmed
    pattern is never guess-parsed - it fails clean immediately, exactly
    as it would have before this feature existed.

    The independent post-PATCH verification below matters MORE here than
    run_create_rule_job's equivalent check on a freshly-created rule:
    there, a mismatch means some new users didn't make it into a rule
    that's created DISABLED and not yet live either way. Here, because
    PATCH replaces the whole `users` array, a mismatch could mean an
    EXISTING user's access was silently dropped from a rule that may
    already be ENABLED and live right now - a materially worse failure
    mode. Design decision, deliberately going further than
    run_create_rule_job's own "note + keep job SUCCESS" precedent for
    that reason: a verification mismatch (or a verification call that
    couldn't even run) here is recorded as a SECOND `record_item` FAILED
    outcome for "rule" (on top of the first SUCCESS item for the PATCH
    call itself) - this pushes the job to PARTIAL_SUCCESS, not a quiet
    SUCCESS, so it's visible on the plain jobs list (/jobs) as something
    other than a clean green badge, not just in a note buried in job
    detail. Only a fully clean verification leaves the job as SUCCESS. A
    successful PATCH that still needed Layer 2's reactive stripping gets
    the exact same PARTIAL_SUCCESS treatment, for the same reason: a
    surprise (something Layer 1 didn't catch, never shown to the
    operator) happened during the write, and a quiet green SUCCESS badge
    would hide that from the plain jobs list.
    """
    progress.set_totals(1)
    _record_identity_resolution_notes(progress, matched_count, unmatched_count, ambiguous_count, notable_rows)
    progress.record_note(
        "identity_resolution", "INFO",
        f"Of the matched users, {already_present_count} were already on rule {rule_id} (no change) "
        f"and {new_count} are new additions.",
    )

    stale_existing_users = stale_existing_users or []
    if stale_existing_users:
        progress.record_note(
            "stale_users", "INFO",
            f"PROACTIVE: {len(stale_existing_users)} existing user(s) on rule {rule_id} no longer resolved in this "
            f"tenant's directory and were excluded before this write - already shown to, and confirmed by, "
            f"the operator on the review page: " + ", ".join(stale_existing_users) + ".",
        )

    payload = list(merged_users)
    reactively_excluded: list[str] = []
    proactive_count = len(stale_existing_users)
    attempt = 0
    ok = False
    detail = ""

    while attempt < MAX_PATCH_ATTEMPTS:
        attempt += 1
        try:
            ok, detail = patch_rule_users(tenant, token, rule_id, payload)
        except NetskopeApiError as exc:
            progress.record_item("rule", "FAILED", str(exc))
            raise

        if ok:
            break

        rejected = _parse_invalid_values_error(detail)
        if rejected is None:
            # Doesn't match the one confirmed parseable pattern - an
            # ordinary failure, exactly as before this feature existed.
            # Never guess-parse a message that doesn't match.
            progress.record_item("rule", "FAILED", detail)
            return

        payload_lower = {u.lower(): u for u in payload}
        newly_excluded = [payload_lower[r.lower()] for r in rejected if r.lower() in payload_lower]
        if not newly_excluded:
            # Parsed cleanly but named nothing actually in this payload -
            # retrying unchanged would just repeat the same rejection
            # forever. Fail clean rather than loop pointlessly.
            progress.record_item("rule", "FAILED", detail)
            return

        total_excluded = proactive_count + len(reactively_excluded) + len(newly_excluded)
        fraction = (total_excluded / original_rule_user_count) if original_rule_user_count else 1.0
        if total_excluded > MAX_TOTAL_EXCLUDED_USERS or fraction > MAX_EXCLUDED_FRACTION:
            progress.record_item(
                "rule", "FAILED",
                f"PATCH attempt {attempt} rejected {len(newly_excluded)} more stale value(s) as invalid "
                f"({', '.join(newly_excluded)}), bringing the total excluded (proactive + reactive) to "
                f"{total_excluded} - over the safety threshold ({MAX_TOTAL_EXCLUDED_USERS} total, or "
                f"{int(MAX_EXCLUDED_FRACTION * 100)}% of the rule's original {original_rule_user_count} "
                f"user(s)). Stopping here rather than continuing to auto-strip - this needs manual review.",
            )
            return

        reactively_excluded.extend(newly_excluded)
        newly_excluded_lower = {v.lower() for v in newly_excluded}
        payload = [u for u in payload if u.lower() not in newly_excluded_lower]
        progress.record_note(
            "stale_users", "INFO",
            f"REACTIVE: PATCH attempt {attempt} of {MAX_PATCH_ATTEMPTS} rejected as invalid - NOT caught by the "
            f"proactive directory check shown on the review page: " + ", ".join(newly_excluded) +
            ". Retrying without them.",
        )

    if not ok:
        progress.record_item(
            "rule", "FAILED",
            f"Still rejected as invalid after {MAX_PATCH_ATTEMPTS} attempt(s) - giving up rather than "
            f"retrying further. Last error: {detail}",
        )
        return

    success_detail = f"{detail} (rule_id={rule_id}, {new_count} user(s) added"
    if reactively_excluded:
        success_detail += f", {len(reactively_excluded)} stale user(s) removed reactively during the write"
    success_detail += ")"
    progress.record_item("rule", "SUCCESS", success_detail)

    if reactively_excluded:
        # The surprising category (CLAUDE.md-worthy distinction from the
        # proactive note above): discovered only during the write itself,
        # never shown to the operator before they confirmed. A second
        # record_item (not just a note) so this pushes the job to
        # PARTIAL_SUCCESS and is visible on the plain /jobs list, not
        # buried in job detail - same treatment as a verification
        # mismatch below.
        progress.record_item(
            "stale_users_reactive", "FAILED",
            f"{len(reactively_excluded)} existing user(s) were removed from this rule during the write "
            f"itself, NOT shown on the review page beforehand: " + ", ".join(reactively_excluded) + ". "
            f"This means Netskope rejected them as invalid at write time even though they resolved (or "
            f"appeared to) against the directory snapshot taken earlier in this run - review whether this "
            f"is expected.",
        )

    # Independent verification - see docstring above for why a mismatch
    # here is a hard FAILED item, not just a supplementary note. Compares
    # against `payload` (the FINAL array actually submitted, after any
    # Layer 2 stripping), not the original `merged_users`.
    try:
        rule = fetch_rule_by_id(tenant, token, rule_id)
    except NetskopeApiError as exc:
        progress.record_item(
            "rule", "FAILED",
            f"Independent verification could not run: {exc}. The PATCH above reported success, but this "
            f"could not be confirmed - check the rule manually before assuming it's correct.",
        )
        progress.set_summary("Independent verification could not run after the PATCH - see the note above. Check this rule manually.")
        return

    stored_users = rule.get("rule_data", {}).get("users", [])
    expected = {u.lower() for u in payload}
    stored = {u.lower() for u in stored_users}
    missing = expected - stored
    unexpected = stored - expected

    if len(stored_users) != len(payload) or missing or unexpected:
        detail_msg = f"Stored user count {len(stored_users)} vs. expected {len(payload)}."
        if missing:
            sample = ", ".join(sorted(missing)[:10])
            detail_msg += f" MISSING from the stored rule (dropped?): {sample}" + (" (+more)" if len(missing) > 10 else "") + "."
        if unexpected:
            sample = ", ".join(sorted(unexpected)[:10])
            detail_msg += f" UNEXPECTED in the stored rule: {sample}" + (" (+more)" if len(unexpected) > 10 else "") + "."
        progress.record_item("rule", "FAILED", detail_msg)
        progress.set_summary(
            "Independent verification found a mismatch after adding users - see the note above. This may mean "
            "an existing user's access was dropped. Check this rule in the Netskope UI now, do not assume it's correct."
        )
    else:
        progress.record_note("rule", "VERIFIED", f"Confirmed: all {len(payload)} expected users are present in the stored rule, no others.")
        summary = (
            f"Rule {rule_id} ({rule_name}) updated and verified: {new_count} user(s) added, "
            f"{already_present_count} were already present. {len(payload)} total user(s) now on the rule."
        )
        if stale_existing_users:
            summary += f" {len(stale_existing_users)} stale existing user(s) were removed (shown on the review page)."
        if reactively_excluded:
            summary += f" {len(reactively_excluded)} more were removed reactively during the write - see the callout above."
        progress.set_summary(summary)
