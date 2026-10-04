"""
Read-only Netskope calls for "Users per NPA policy" (a Data Export sub-feature).

Everything goes through Data Export's own `_call` (tenant validation, retry of 429 and 5xx with
Retry-After / RateLimit-Reset / exponential backoff, pacing) and the shared `netskope_http`
plumbing - there is no HTTP client of its own here. What this module adds:

  * the NPA policy list, read in one call and refused if it does not add up
  * SCIM group lookup BY NAME with a properly quoted filter value, paged with startIndex+count
    and checked against totalResults (the Operation 3 helpers it replaces for this purpose read a
    single page of 100 and never look at totalResults, and take the first of several matches)
  * a group's members (SCIM ids - NOT emails)
  * the scimId -> email directory, taken from the existing, guarded getusers paging (its account
    objects carry `scimId`), so no per-member `Users/{id}` call is ever made
  * a hard ceiling on API calls per export (CallBudget / BudgetPacer)

Facts this relies on, and where they were seen:
  - GET /api/v2/policy/npa/rules returns every rule when no `limit` is sent, as {"data": [...]}
    (user_lookup/service.py). No total has ever been seen on it, so completeness can only be
    checked when the response happens to carry one.
  - SCIM uses `Authorization: Bearer` (netskope_http.bearer_headers), the REST endpoints use
    `Netskope-API-Token`; one token typed per run has to carry both scopes.
  - SCIM list responses carry totalResults / startIndex / itemsPerPage / Resources; a group
    resource fetched by id returns `members` as [{"value": <scim user id>}].
UNVERIFIED against a live tenant: the exact shape of a rule's `userGroups` entries, and whether
SCIM returns `members` for non-SCIM (Local / AD) groups. Both degrade to visible UNRESOLVED rows.

Calls are counted when they are made (one per logical call; the retries inside `_call` are not
counted separately).

Upstream text is never kept. Whatever Netskope (or the HTTP library) says in an error - which can echo
the request, and so a group name, or name an unrelated user - is classified by STRUCTURE (the HTTP status
code attached to the error, the type of the exception, the type of its cause) into an `ExportFailure`
whose message is built from known parts only: a phase, a short category and a status code. That is the
only error text this export stores, logs or shows. Separately, the HTTP library logs every request line,
query string included, at DEBUG; the SCIM group filter carries a group name, so those two library loggers
are told to cut the query string off - for this export's own calls only (see _redacting_urls).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from dataclasses import dataclass, field

import requests

from ..netskope_http import NetskopeApiError, bearer_headers
from .netskope_client import (
    PACE_SECONDS, ExportIncompleteError, Pacer, _call, _is_count, _raise_for_status, fetch_users,
)

MAX_API_CALLS = 2000           # per export; every call, every endpoint
SCIM_PAGE = 100                # count= per SCIM page
SCIM_PACE_SECONDS = 0.05       # SCIM allows far more than the 4/s endpoints; this is a courtesy gap
MAX_SCIM_PAGES = 100           # runaway-loop guard for one name lookup

_RULES_PATH = "/api/v2/policy/npa/rules"
_SCIM_GROUPS_PATH = "/api/v2/scim/Groups"
# A SCIM group id goes into a URL path: UUID-like text only, never "." / ".." or anything with a separator.
_SAFE_ID = re.compile(r"^(?!\.+$)[A-Za-z0-9._~-]{1,128}$")

__all__ = [
    "ExportLimitError", "ExportFailure", "UnexpectedReply", "UpstreamRefused", "failure_for", "CATEGORIES",
    "UnsafeGroupId", "CallBudget", "BudgetPacer", "NpaRules", "GroupMembers", "UserDirectory",
    "fetch_npa_rules", "scim_filter_value", "find_scim_groups", "fetch_group_members", "load_user_directory",
    "PACE_SECONDS", "SCIM_PACE_SECONDS", "SCIM_PAGE", "MAX_API_CALLS",
]


class ExportLimitError(Exception):
    """A hard ceiling (API calls or result rows) was reached. `kind` ("api_calls" | "rows") and `limit` (an int)
    are what the user-facing message is built from; the text given here is for developers."""

    def __init__(self, message: str, *, kind: str | None = None, limit: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.limit = limit


class UnsafeGroupId(NetskopeApiError):
    """A group id that is not safe to put in a URL path. Only this group is affected."""


class UnexpectedReply(NetskopeApiError):
    """Netskope answered, but not in a form this export can use (not JSON, the wrong shape, an entry without an
    id). Never carries text taken from the reply."""


class UpstreamRefused(NetskopeApiError):
    """HTTP 200 with an error in the body (CLAUDE.md Section 9: a 200 does not mean success). Never carries the
    body's own message."""

    status_code = 200


# --------------------------------------------------------------------------
# Failures: a fixed message from known parts, never the upstream text
# --------------------------------------------------------------------------

PHASE_POLICIES = "loading policies"
PHASE_USERS = "resolving users"
PHASE_GROUPS = "expanding groups"
PHASE_FILE = "building the file"
PHASES = (PHASE_POLICIES, PHASE_USERS, PHASE_GROUPS, PHASE_FILE)

AUTH, SCOPE, RATE_LIMIT, SERVER_ERROR = "auth", "scope", "rate limit", "server error"
TIMEOUT, NETWORK, TLS, REJECTED = "timeout", "network", "tls", "rejected"
UNEXPECTED_REPLY, MISMATCH, LIMIT, INTERNAL = "unexpected reply", "truncation/mismatch", "limit", "internal error"

# category -> the sentence shown to the user. Fixed text only (no quotes or apostrophes, so it renders as is).
# `{what}` is filled from _WHAT below, by phase; `{limit}` is an int.
CATEGORIES = {
    AUTH: "the token was rejected - it is probably wrong or has expired.",
    SCOPE: "the token is valid but lacks the permission to read {what}.",
    RATE_LIMIT: "Netskope kept limiting the request rate even after waiting and retrying. Try again later.",
    SERVER_ERROR: "Netskope answered with a server error even after retrying. Try again later.",
    TIMEOUT: "the request to Netskope timed out - the tenant may be unreachable.",
    NETWORK: "the tenant could not be reached - check the tenant name and network connectivity.",
    TLS: "a TLS certificate problem prevented the connection to the tenant.",
    REJECTED: "Netskope refused the request or reported an error in its reply.",
    UNEXPECTED_REPLY: "Netskope sent a reply in a form this export cannot use (unexpected response).",
    MISMATCH: "what Netskope returned did not add up to what it reported, so the list may be incomplete (truncation or mismatch).",
    LIMIT: "a safety limit of this export was reached. Select fewer policies and try again.",
    INTERNAL: "an unexpected internal error occurred.",
}
_WHAT = {PHASE_POLICIES: "NPA policies", PHASE_USERS: "users", PHASE_GROUPS: "SCIM groups", PHASE_FILE: "this data"}
_LIMIT_SENTENCES = {
    "api_calls": "this export needs more than the {limit} API calls allowed for a single export. Select fewer policies and try again.",
    "rows": "this export would produce more than the {limit} rows allowed for a single export. Select fewer policies and try again.",
}


def _valid_status(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


class ExportFailure(Exception):
    """Why an export stopped. The message is assembled ONLY from `phase` and `category` (both from closed sets,
    anything else is a ValueError), an HTTP status code (an int, or dropped) and a ceiling (an int): nothing
    Netskope wrote can get into it, so it is safe to store in jobs.error_message and to show."""

    def __init__(self, phase: str, category: str, status=None, *, limit_kind: str | None = None, limit: int | None = None) -> None:
        if phase not in PHASES:
            raise ValueError("unknown phase")
        if category not in CATEGORIES:
            raise ValueError("unknown category")
        self.phase = phase
        self.category = category
        self.status = _valid_status(status)
        sentence = CATEGORIES[category].format(what=_WHAT[phase])
        if category == LIMIT and limit_kind in _LIMIT_SENTENCES and isinstance(limit, int) and not isinstance(limit, bool):
            sentence = _LIMIT_SENTENCES[limit_kind].format(limit=limit)
        label = category + (f", HTTP {self.status}" if self.status else "")
        super().__init__(f"Export stopped while {phase} ({label}): {sentence} No file was produced.")


def _category_for_status(status: int) -> str:
    if status == 401:
        return AUTH
    if status == 403:
        return SCOPE
    if status == 429:
        return RATE_LIMIT
    if status >= 500:
        return SERVER_ERROR
    return REJECTED


def failure_for(exc: BaseException, phase: str) -> ExportFailure:
    """Classifies by structure - the status code attached to the error, its type, the type of its cause - and
    never reads the error's text."""
    if isinstance(exc, ExportFailure):
        return exc
    if isinstance(exc, ExportLimitError):
        return ExportFailure(phase, LIMIT, limit_kind=exc.kind, limit=exc.limit)
    if isinstance(exc, ExportIncompleteError):
        return ExportFailure(phase, MISMATCH)
    if isinstance(exc, UnexpectedReply):
        return ExportFailure(phase, UNEXPECTED_REPLY)
    if isinstance(exc, NetskopeApiError):
        status = _valid_status(getattr(exc, "status_code", None))
        if status is not None:
            return ExportFailure(phase, _category_for_status(status), status)
        cause = exc.__cause__
        if isinstance(cause, requests.exceptions.Timeout):
            return ExportFailure(phase, TIMEOUT)
        if isinstance(cause, requests.exceptions.SSLError):
            return ExportFailure(phase, TLS)
        if isinstance(cause, requests.exceptions.RequestException):
            return ExportFailure(phase, NETWORK)
        return ExportFailure(phase, UNEXPECTED_REPLY)
    return ExportFailure(phase, INTERNAL)


# --------------------------------------------------------------------------
# The HTTP library's DEBUG log line carries the request URL, query string included
# --------------------------------------------------------------------------

_IN_NPA_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar("npa_export_call", default=False)
_QUERY_STRING = re.compile(r"\?[^\s\"']*")


class _CutQueryStrings(logging.Filter):
    """urllib3 logs `"GET /api/v2/scim/Groups?filter=displayName eq <group name> ..."` at DEBUG. While this export
    is making one of its own calls (_IN_NPA_CALL), the query string is cut out of that record; every other
    caller of the library is untouched."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _IN_NPA_CALL.get():
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            record.msg = _QUERY_STRING.sub("?[query removed]", message)
            record.args = ()
        return True


for _name in ("urllib3.connectionpool", "urllib3.util.retry"):
    logging.getLogger(_name).addFilter(_CutQueryStrings())


@contextlib.contextmanager
def _redacting_urls():
    token = _IN_NPA_CALL.set(True)
    try:
        yield
    finally:
        _IN_NPA_CALL.reset(token)


class CallBudget:
    """One shared counter for every API call of one export."""

    def __init__(self, max_calls: int | None = None) -> None:
        self.max_calls = MAX_API_CALLS if max_calls is None else max_calls
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.max_calls:
            raise ExportLimitError(
                f"This export needs more than the {self.max_calls} API calls allowed for a single export, so it was "
                f"stopped. Select fewer policies and try again. No file was produced.",
                kind="api_calls", limit=self.max_calls,
            )
        self.used += 1


class BudgetPacer(Pacer):
    """Data Export's Pacer, plus a call counter shared through `budget`. The call that would exceed
    the ceiling raises BEFORE anything is sent."""

    def __init__(self, budget: CallBudget, min_interval: float) -> None:
        super().__init__(min_interval)
        self.budget = budget

    def wait(self) -> None:
        self.budget.spend()
        super().wait()


# --------------------------------------------------------------------------
# The policy list
# --------------------------------------------------------------------------

@dataclass
class NpaRules:
    rules: list[dict]
    declared_total: int | None      # None: the response carried no total, completeness could not be checked


def _declared_total(body) -> int | None:
    if not isinstance(body, dict):
        return None
    candidates = [body.get("total"), body.get("totalResults")]
    counts = body.get("counts")
    if isinstance(counts, dict):
        candidates.append(counts.get("totalResults"))
    for value in candidates:
        if _is_count(value):
            return value
    return None


def _rule_id(item: dict) -> str:
    value = item.get("rule_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise UnexpectedReply("Netskope's policy list contained a policy without an id, so nothing was exported.")
    return str(value).strip()


def fetch_npa_rules(tenant: str, token: str, pacer: Pacer | None = None) -> NpaRules:
    with _redacting_urls():
        resp = _call("GET", tenant, token, _RULES_PATH, "reading NPA policies", pacer=pacer)
    _raise_for_status(resp, "NPA policies")
    try:
        parsed = resp.json()
    except ValueError as exc:
        raise UnexpectedReply("Netskope's policy list wasn't valid JSON.") from exc

    if isinstance(parsed, dict):
        status = parsed.get("status")
        if isinstance(status, str) and status.lower() != "success":
            # The body's own message is deliberately not kept: it is upstream text.
            raise UpstreamRefused("Netskope did not report success reading NPA policies.")
        items = parsed.get("data")
    else:
        items = parsed
    if not isinstance(items, list):
        raise UnexpectedReply("Netskope's policy list had an unexpected shape, so nothing was exported.")
    if any(not isinstance(item, dict) for item in items):
        raise UnexpectedReply("Netskope's policy list contained an entry that is not a policy, so nothing was exported.")

    seen: set[str] = set()
    for item in items:
        rid = _rule_id(item)
        if rid in seen:
            raise ExportIncompleteError("Netskope returned the same policy twice in its policy list. No file was produced.")
        seen.add(rid)

    total = _declared_total(parsed)
    if total is not None and total != len(items):
        raise ExportIncompleteError(
            f"Netskope reports {total} policies but returned {len(items)}. The list is incomplete, so nothing was "
            f"exported. Try again."
        )
    return NpaRules(rules=items, declared_total=total)


# --------------------------------------------------------------------------
# SCIM
# --------------------------------------------------------------------------

def scim_filter_value(name: str) -> str:
    """The value of `displayName eq <value>`: SCIM filter strings use JSON string syntax, so a JSON
    encoder quotes and escapes (quotes, backslashes, control characters) exactly as required."""
    return json.dumps(name, ensure_ascii=False)


def _scim_get(tenant: str, token: str, path: str, what: str, params: dict, pacer: Pacer, *, allow_404: bool = False) -> dict | None:
    with _redacting_urls():
        resp = _call(
            "GET", tenant, token, f"{_SCIM_GROUPS_PATH}{path}", f"reading {what}",
            params=params, pacer=pacer, headers_override=bearer_headers(token),
        )
    if allow_404 and resp.status_code == 404:
        return None
    _raise_for_status(resp, what)
    try:
        body = resp.json()
    except ValueError as exc:
        raise UnexpectedReply(f"Netskope's {what} response wasn't valid JSON.") from exc
    if not isinstance(body, dict):
        raise UnexpectedReply(f"Netskope's {what} response had an unexpected shape, so nothing was exported.")
    return body


def find_scim_groups(tenant: str, token: str, name: str, pacer: Pacer) -> list[dict]:
    """Every SCIM group whose displayName equals `name`, read page by page (startIndex + count) until
    totalResults is reached. Never returns a partial list: a missing or shifting total, a repeated
    page, a repeated id, more results than the total, or an empty page before the total all raise."""
    what = "SCIM groups"
    flt = f"displayName eq {scim_filter_value(name)}"
    found: list[dict] = []
    seen: set[str] = set()
    total: int | None = None
    previous_first: str | None = None
    for _ in range(MAX_SCIM_PAGES):
        body = _scim_get(tenant, token, "", what, {"filter": flt, "startIndex": 1 + len(found), "count": SCIM_PAGE}, pacer)
        page_total = body.get("totalResults")
        if not _is_count(page_total):
            raise ExportIncompleteError(
                "Netskope's SCIM group search did not report totalResults, so it cannot be checked for completeness. "
                "No file was produced."
            )
        if total is None:
            total = page_total
        elif page_total != total:
            raise ExportIncompleteError("The SCIM group search result changed while it was being read. Run the export again. No file was produced.")
        resources = body.get("Resources")
        if resources is None:
            resources = []
        if not isinstance(resources, list) or any(not isinstance(r, dict) for r in resources):
            raise UnexpectedReply("Netskope's SCIM group search had an unexpected shape, so nothing was exported.")
        if not resources:
            if len(found) < total:
                raise ExportIncompleteError(
                    f"Netskope returned an empty page after {len(found)} of {total} SCIM groups - paging stopped advancing. "
                    f"No file was produced."
                )
            break
        ids = []
        for r in resources:
            gid = r.get("id")
            if not isinstance(gid, str) or not gid:
                raise UnexpectedReply("Netskope's SCIM group search contained a group without an id, so nothing was exported.")
            ids.append(gid)
        if previous_first is not None and ids[0] == previous_first:
            raise ExportIncompleteError("Netskope returned the same page of SCIM groups again - paging did not advance. No file was produced.")
        for gid in ids:
            if gid in seen:
                raise ExportIncompleteError("Netskope returned the same SCIM group twice while paging. No file was produced.")
            seen.add(gid)
        found.extend({"id": r["id"], "displayName": r.get("displayName")} for r in resources)
        previous_first = ids[0]
        if len(found) > total:
            raise ExportIncompleteError(f"Netskope returned more SCIM groups ({len(found)}) than it reports ({total}). No file was produced.")
        if len(found) == total:
            break
    else:
        raise ExportIncompleteError(f"Stopped after {MAX_SCIM_PAGES} pages of one SCIM group search. No file was produced.")
    return found


@dataclass
class GroupMembers:
    ids: list[str] | None           # None: the group resource carried no `members` attribute at all
    unreadable: int = 0             # member entries that had no usable `value`


def fetch_group_members(tenant: str, token: str, group_id: str, pacer: Pacer) -> GroupMembers | None:
    """The SCIM ids of a group's members, or None if the group no longer exists (404)."""
    if not isinstance(group_id, str) or not _SAFE_ID.fullmatch(group_id):
        raise UnsafeGroupId("Netskope returned a SCIM group id in an unexpected format, so it was not used.")
    body = _scim_get(tenant, token, f"/{group_id}", "a SCIM group's members", {"attributes": "members"}, pacer, allow_404=True)
    if body is None:
        return None
    if body.get("id") != group_id:
        raise ExportIncompleteError("Netskope answered for a different SCIM group than the one asked for. No file was produced.")
    members = body.get("members")
    if not isinstance(members, list):
        return GroupMembers(ids=None)
    ids: list[str] = []
    unreadable = 0
    for entry in members:
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
        else:
            unreadable += 1
    return GroupMembers(ids=ids, unreadable=unreadable)


# --------------------------------------------------------------------------
# The scimId -> email directory (once per run)
# --------------------------------------------------------------------------

def _key(value) -> str:
    return str(value).strip().lower()


@dataclass
class UserDirectory:
    users_total: int
    _emails: dict[str, str] = field(default_factory=dict)
    _ambiguous: set[str] = field(default_factory=set)
    _group_members: dict[str, set[str]] = field(default_factory=dict)

    def email_for(self, scim_id: str) -> str | None:
        key = _key(scim_id)
        return None if key in self._ambiguous else self._emails.get(key)

    def is_ambiguous(self, scim_id: str) -> bool:
        return _key(scim_id) in self._ambiguous

    def group_count(self, name: str) -> int:
        """Users with a live account whose parentGroups names this group (case-insensitive)."""
        return len(self._group_members.get(_key(name), ()))


def _primary_email(row: dict, account: dict) -> str | None:
    emails = row.get("emails")
    if isinstance(emails, list):
        for value in emails:
            if isinstance(value, str) and value.strip():
                return value.strip()
    for value in (row.get("id"), account.get("userName")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_user_directory(tenant: str, token: str, page_size: int, pacer: Pacer) -> UserDirectory:
    with _redacting_urls():
        users = fetch_users(tenant, token, page_size, pacer=pacer)      # paged to completion, every guard of Data Export
    directory = UserDirectory(users_total=users.total)
    for row in users.rows:
        accounts = row.get("accounts")
        for account in accounts if isinstance(accounts, list) else []:
            if not isinstance(account, dict):
                continue
            who = _primary_email(row, account)
            sid = account.get("scimId")
            if who and isinstance(sid, str) and sid.strip():
                key = _key(sid)
                if key in directory._emails and directory._emails[key].lower() != who.lower():
                    directory._ambiguous.add(key)
                directory._emails.setdefault(key, who)
            if account.get("deleted") is not True and who:
                groups = account.get("parentGroups")
                for group in groups if isinstance(groups, list) else []:
                    if isinstance(group, str) and group.strip():
                        directory._group_members.setdefault(_key(group), set()).add(str(row.get("id") or who).lower())
    return directory
