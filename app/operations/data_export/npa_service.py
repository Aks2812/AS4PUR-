"""
Business logic for "Users per NPA policy" (a Data Export sub-feature): turns NPA policies, SCIM group
members and the tenant's user directory into ONE CSV of who each selected policy applies to.

Reuse: `user_lookup.policy.normalize_rule` supplies a rule's id, name and action (and copes with the real
action values, e.g. `periodic_reauth`). It is NOT used for the user / group / OU lists, because it
lower-cases them into frozensets (a SCIM `displayName eq` lookup needs the group name as written, and the
output wants the original spelling and a stable order); those three lists are read straight from
`rule_data` here. Nor for `enabled`: it reduces anything but "1"/"true" to False, i.e. it guesses. CSV writing is Data Export's own `build_csv_bytes` (UTF-8 with BOM, the
csv module, a leading quote on any cell starting with = + - @ TAB CR).

CSV columns (stable): policy_name, policy_id, action, enabled, user, via, group_name, status, reason
  enabled true | false | unknown - the rule's top-level `enabled` field, which is the string "1" or "0";
          anything else (absent, empty, a number, a boolean, other text) is `unknown`, never guessed
  via     direct | group | organization_unit | all_users
  status  OK | UNRESOLVED | EMPTY | ALL_USERS
  reason  empty unless status is UNRESOLVED or EMPTY, where it says why. Both have an EMPTY `user`.
EMPTY and UNRESOLVED are different things and are never mixed: UNRESOLVED means membership could not be
determined (group not found, ambiguous name, members not returned, a member that is not in the user list,
an unreadable entry); EMPTY means the group WAS resolved and has no members, one row with via=group,
group_name set and reason "group has no members". An organization-unit row carries the OU in `group_name`
and leaves `user` empty. A policy with no users, no groups and no OUs gets ONE row, ALL_USERS (the label
sits in `user`, `reason` is empty). Nothing is silently dropped or de-duplicated away: a user reachable
directly and through two groups appears three times, once per (via, group); two UNRESOLVED rows differ by
their reason, so both stay.

Failures: whatever goes wrong while talking to Netskope leaves `run_npa_users_export` as an
`npa_client.ExportFailure` (phase, category, status code - a fixed message, never Netskope's own text).

Nothing here touches the disk or logs; held in memory only (see npa_routes.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ...config import settings
from ...timeutil import utcnow
from ..netskope_http import NetskopeApiError
from ..user_lookup.policy import normalize_rule
from . import netskope_client as client
from . import npa_client
from .netskope_client import ExportIncompleteError
from .npa_client import ExportLimitError
from .service import CSV_CONTENT_TYPE, ExportFile, build_csv_bytes

NPA_HEADER = ["policy_name", "policy_id", "action", "enabled", "user", "via", "group_name", "status", "reason"]
STATUS_COLUMN = NPA_HEADER.index("status")
ALL_USERS_LABEL = "ALL_USERS (no user/group restriction)"
MAX_ROWS = 200_000             # per export

JOB_TYPE = "data_export_npa_users"

OK, UNRESOLVED, EMPTY, ALL_USERS = "OK", "UNRESOLVED", "EMPTY", "ALL_USERS"
EMPTY_REASON = "group has no members"
ENABLED_TRUE, ENABLED_FALSE, ENABLED_UNKNOWN = "true", "false", "unknown"


def npa_filename(now: datetime) -> str:
    return f"as4pur-npa-policy-users-{now:%Y%m%d-%H%M%S}Z.csv"


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


# --------------------------------------------------------------------------
# Scrubbing: no token and no tenant name in anything that is stored or shown
# --------------------------------------------------------------------------

def scrub(text, *secrets) -> str:
    """Removes each secret (the API token, the tenant name, and `<tenant>.goskope.com`) from `text`.
    The shared request wrapper puts `requests` exception text, which contains the tenant's host, into
    some error messages; this runs over every message this feature stores or shows. A secret shorter
    than 8 characters is only removed as a standalone token, so a short tenant name cannot mangle words."""
    out = "" if text is None else str(text)
    for secret in secrets:
        if not isinstance(secret, str) or not secret.strip():
            continue
        s = secret.strip()
        out = re.sub(re.escape(s) + r"\.goskope\.com", "[removed]", out, flags=re.IGNORECASE)
        if len(s) >= 8:
            out = re.sub(re.escape(s), "[removed]", out, flags=re.IGNORECASE)
        else:
            out = re.sub(r"(?<![A-Za-z0-9-])" + re.escape(s) + r"(?![A-Za-z0-9-])", "[removed]", out, flags=re.IGNORECASE)
    return out


def user_derived_strings(views) -> list[str]:
    """Every email, group name and organizational unit in the selected policies, longest first. A failed run's
    message is stored in the database (jobs.error_message), and Netskope's own error text can echo back what
    was asked - a SCIM filter carries a group name - so the caller scrubs these out too, alongside the token and
    the tenant name. Run only on the failure path."""
    values = {s for v in views for s in (*v.users, *v.groups, *v.ous)}
    return sorted(values, key=lambda s: (-len(s), s))


# --------------------------------------------------------------------------
# Reading the policies
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyView:
    rule_id: str
    name: str
    action: str                    # as the rule says: allow, block, periodic_reauth, ... or "unknown"
    enabled: str                   # ENABLED_TRUE | ENABLED_FALSE | ENABLED_UNKNOWN (see _enabled_state)
    users: tuple[str, ...]         # original spelling and order; exact repeats (ignoring case) dropped
    groups: tuple[str, ...]
    ous: tuple[str, ...]
    unreadable: int = 0            # entries in those three lists that are not usable text

    @property
    def restricts_nobody(self) -> bool:
        return not (self.users or self.groups or self.ous or self.unreadable)


def _entries(value, *, is_email: bool) -> tuple[tuple[str, ...], int]:
    """(entries, unreadable count). Emails are stripped and de-duplicated ignoring case; group and OU names
    are kept exactly as written (a SCIM `displayName eq` lookup needs them verbatim) and only exact repeats go."""
    if value is None:
        return (), 0
    if not isinstance(value, (list, tuple)):
        return (), 1
    out: list[str] = []
    seen: set[str] = set()
    bad = 0
    for item in value:
        if not isinstance(item, str) or not item.strip():
            bad += 1
            continue
        text = item.strip() if is_email else item
        key = text.lower() if is_email else text
        if key not in seen:
            seen.add(key)
            out.append(text)
    return tuple(out), bad


def _enabled_state(value) -> str:
    """The rule's top-level `enabled` is the STRING "1" or "0". Anything else - absent, empty, a number, a
    boolean, other text - is unknown. `normalize_rule` is not used for this: it turns every value that is not
    "1"/"true" into False, i.e. it guesses "disabled", which this export must not do."""
    if isinstance(value, str):
        if value == "1":
            return ENABLED_TRUE
        if value == "0":
            return ENABLED_FALSE
    return ENABLED_UNKNOWN


def policy_views(raw_rules: list[dict]) -> list[PolicyView]:
    views = []
    for raw in raw_rules or []:
        rule = normalize_rule(raw)
        if rule is None:
            continue
        rd = raw.get("rule_data") if isinstance(raw.get("rule_data"), dict) else {}
        # users / userGroups / organization_units are read straight from rule_data, NOT from `rule` (the
        # normalize_rule result): normalize_rule lower-cases them into frozensets, but the SCIM
        # `displayName eq "<name>"` lookup needs the group name in its ORIGINAL case, and the CSV should show
        # the original spelling in a stable order. Only id, name and action come from normalize_rule.
        users, bad_u = _entries(rd.get("users"), is_email=True)
        groups, bad_g = _entries(rd.get("userGroups"), is_email=False)
        ous, bad_o = _entries(rd.get("organization_units"), is_email=False)
        views.append(PolicyView(
            rule_id=rule.rule_id, name=rule.name, action=rule.action, enabled=_enabled_state(raw.get("enabled")),
            users=users, groups=groups, ous=ous, unreadable=bad_u + bad_g + bad_o,
        ))
    return views


@dataclass(frozen=True)
class Preview:
    policies: int
    groups_to_expand: int
    direct_users: int
    ous: int
    all_users_policies: int


def _distinct_groups(views) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for view in views:
        for name in view.groups:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def preview(views) -> Preview:
    return Preview(
        policies=len(views),
        groups_to_expand=len(_distinct_groups(views)),
        direct_users=sum(len(v.users) for v in views),
        ous=sum(len(v.ous) for v in views),
        all_users_policies=sum(1 for v in views if v.restricts_nobody),
    )


# --------------------------------------------------------------------------
# Expanding groups
# --------------------------------------------------------------------------

@dataclass
class Member:
    email: str | None
    scim_id: str
    reason: str = ""


@dataclass
class GroupExpansion:
    name: str
    status: str = OK
    reason: str = ""               # generic (never contains the group name): it is also written to the audit item
    members: list[Member] = field(default_factory=list)


def _expand_group(tenant, token, name, directory, pacer) -> GroupExpansion:
    def unresolved(reason: str) -> GroupExpansion:
        return GroupExpansion(name=name, status=UNRESOLVED, reason=reason)

    matches = npa_client.find_scim_groups(tenant, token, name, pacer)
    if not matches:
        return unresolved("group not found by name (SCIM displayName)")
    if len(matches) > 1:
        return unresolved(f"ambiguous name: {len(matches)} SCIM groups share it")
    try:
        got = npa_client.fetch_group_members(tenant, token, matches[0]["id"], pacer)
    except npa_client.UnsafeGroupId:
        return unresolved("the group's id has an unexpected format")
    if got is None:
        return unresolved("the group disappeared before its members could be read")
    if got.ids is None:
        return unresolved("members not returned by SCIM for this group")

    members: list[Member] = []
    for sid in got.ids:
        who = directory.email_for(sid)
        if who:
            members.append(Member(email=who, scim_id=sid))
        elif directory.is_ambiguous(sid):
            members.append(Member(email=None, scim_id=sid, reason=f"SCIM user id {sid} matches several users in the tenant's user list"))
        else:
            members.append(Member(email=None, scim_id=sid, reason=f"SCIM user id {sid} not found in the tenant's user list"))
    if got.unreadable:
        members.append(Member(email=None, scim_id="", reason=f"{_n(got.unreadable, 'member entry', 'member entries')} without a usable id"))
    return GroupExpansion(name=name, members=members)


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

def _policy_rows(view: PolicyView, expansions: dict[str, GroupExpansion]) -> list[list[str]]:
    rows: list[list[str]] = []
    seen: set[tuple] = set()
    base = [view.name, view.rule_id, view.action, view.enabled]

    def add(user: str, via: str, group: str, status: str, reason: str = "") -> None:
        # Only an exact repeat of the same (user, via, group, status, reason) is collapsed. The reason is part of
        # the key because an UNRESOLVED row has an empty user: two of them in one group differ only by reason.
        key = (user.lower(), via, group, status, reason)
        if key not in seen:
            seen.add(key)
            rows.append([*base, user, via, group, status, reason])

    for user in sorted(view.users, key=str.lower):
        add(user, "direct", "", OK)
    for group in sorted(view.groups, key=str.lower):
        expansion = expansions[group]
        if expansion.status == UNRESOLVED:
            add("", "group", group, UNRESOLVED, expansion.reason)
        elif not expansion.members:
            add("", "group", group, EMPTY, EMPTY_REASON)          # resolved, and genuinely has no members: not UNRESOLVED
        else:
            for member in sorted(expansion.members, key=lambda m: (m.email or m.scim_id or m.reason).lower()):
                if member.email:
                    add(member.email, "group", group, OK)
                else:
                    add("", "group", group, UNRESOLVED, member.reason)
    for ou in sorted(view.ous, key=str.lower):
        add("", "organization_unit", ou, OK)
    if view.unreadable:
        add("", "direct", "", UNRESOLVED, f"{_n(view.unreadable, 'unreadable entry', 'unreadable entries')} in this policy's user, group or OU lists")
    if view.restricts_nobody:
        add(ALL_USERS_LABEL, "all_users", "", ALL_USERS)
    return rows


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

@dataclass
class NpaResult:
    file: ExportFile
    rows: int
    summary: dict
    warnings: list[str]
    created_at: datetime


def run_npa_users_export(tenant: str, token: str, views, progress, now: datetime | None = None, page_size: int | None = None) -> NpaResult:
    """Blocking; meant for the job thread. `progress` has set_totals / record_item / set_summary (JobProgress).
    Raises npa_client.ExportFailure (and nothing that carries upstream text) when the export cannot be completed:
    the caller turns it into a failed job - no file exists in that case."""
    now = now or utcnow()
    tenant = tenant.strip().lower()
    size = page_size or settings.data_export_page_size
    views = sorted(views, key=lambda v: (v.name.lower(), v.rule_id))
    groups = _distinct_groups(views)

    budget = npa_client.CallBudget()
    users_pacer = npa_client.BudgetPacer(budget, client.PACE_SECONDS)
    scim_pacer = npa_client.BudgetPacer(budget, npa_client.SCIM_PACE_SECONDS)
    progress.set_totals((1 if groups else 0) + len(groups) + len(views))

    phase = npa_client.PHASE_USERS
    try:
        directory = None
        if groups:
            directory = npa_client.load_user_directory(tenant, token, size, users_pacer)
            progress.record_item("users-directory", "SUCCESS", None)

        phase = npa_client.PHASE_GROUPS
        expansions: dict[str, GroupExpansion] = {}
        for index, name in enumerate(groups, 1):
            expansion = _expand_group(tenant, token, name, directory, scim_pacer)
            expansions[name] = expansion
            progress.record_item(f"group-{index}", "SUCCESS" if expansion.status == OK else "UNRESOLVED", None if expansion.status == OK else expansion.reason)

        phase = npa_client.PHASE_FILE
        rows: list[list[str]] = []
        for index, view in enumerate(views, 1):
            policy_rows = _policy_rows(view, expansions)
            rows.extend(policy_rows)
            if len(rows) > MAX_ROWS:
                raise ExportLimitError(
                    f"This export would produce more than the {MAX_ROWS} rows allowed for a single export, so it was stopped. "
                    f"Select fewer policies and try again. No file was produced.",
                    kind="rows", limit=MAX_ROWS,
                )
            bad = sum(1 for r in policy_rows if r[STATUS_COLUMN] == UNRESOLVED)
            progress.record_item(f"policy-{index}", "SUCCESS" if not bad else "UNRESOLVED", None if not bad else f"{_n(bad, 'unresolved row', 'unresolved rows')}")
    except npa_client.ExportFailure:
        raise
    except (ExportLimitError, ExportIncompleteError, NetskopeApiError) as exc:
        # Classified by structure; the exception's own text (which can carry upstream text) is dropped here.
        raise npa_client.failure_for(exc, phase) from None

    unresolved_rows = sum(1 for r in rows if r[STATUS_COLUMN] == UNRESOLVED)
    empty_rows = sum(1 for r in rows if r[STATUS_COLUMN] == EMPTY)
    ok_rows = sum(1 for r in rows if r[STATUS_COLUMN] == OK)                       # display only: the result page's status tiles
    all_users_rows = sum(1 for r in rows if r[STATUS_COLUMN] == ALL_USERS)
    unresolved_groups = [e for e in expansions.values() if e.status == UNRESOLVED]
    empty_groups = [e for e in expansions.values() if e.status == OK and not e.members]
    warnings: list[str] = []
    if unresolved_groups:
        listed = "; ".join(f'"{e.name}" ({e.reason})' for e in unresolved_groups[:10])
        more = f"; and {len(unresolved_groups) - 10} more" if len(unresolved_groups) > 10 else ""
        warnings.append(f"{_n(len(unresolved_groups), 'group', 'groups')} could not be resolved and appear as UNRESOLVED rows: {listed}{more}.")
    member_problems = sum(1 for e in expansions.values() if e.status == OK for m in e.members if not m.email)
    if member_problems:
        warnings.append(f"{_n(member_problems, 'group-membership entry', 'group-membership entries')} could not be matched to a user and appear as UNRESOLVED rows.")
    unreadable = sum(v.unreadable for v in views)
    if unreadable:
        warnings.append(f"{_n(unreadable, 'entry', 'entries')} in the selected policies could not be read and appear as UNRESOLVED rows.")
    if directory is not None:
        mismatched = []
        for e in expansions.values():
            if e.status != OK:
                continue
            listed_ids = {m.scim_id for m in e.members if m.scim_id}
            seen = directory.group_count(e.name)
            if len(listed_ids) != seen:
                mismatched.append(f'"{e.name}": SCIM lists {len(listed_ids)}, the user directory shows {seen}')
        if mismatched:
            more = f"; and {len(mismatched) - 5} more" if len(mismatched) > 5 else ""
            warnings.append(
                "Group sizes differ between SCIM and the user directory (" + "; ".join(mismatched[:5]) + more + "). "
                "Either a membership list was cut short or the group names do not match between the two; compare with the console."
            )

    content = build_csv_bytes(NPA_HEADER, rows)
    file = ExportFile(kind="npa_users", filename=npa_filename(now), content=content, content_type=CSV_CONTENT_TYPE, rows=len(rows))
    summary = {
        "policies": len(views),
        "groups_expanded": len(groups),
        "groups_unresolved": len(unresolved_groups),
        "groups_empty": len(empty_groups),
        "rows": len(rows),
        "unresolved_rows": unresolved_rows,
        "empty_rows": empty_rows,
        "ok_rows": ok_rows,
        "all_users_rows": all_users_rows,
        "directory_users": directory.users_total if directory is not None else None,
        "api_calls": budget.used,
    }
    progress.set_summary(
        f"{_n(len(views), 'policy', 'policies')}, {_n(len(rows), 'row', 'rows')}, {unresolved_rows} unresolved, {empty_rows} empty, "
        f"{_n(len(groups), 'group', 'groups')} expanded, {_n(budget.used, 'API call', 'API calls')}"
    )
    return NpaResult(file=file, rows=len(rows), summary=summary, warnings=warnings, created_at=now)
