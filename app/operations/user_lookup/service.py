"""User status lookup: talks to the Netskope API and builds a report.

Every field name used here was seen in a real tenant response (see the design doc):
- getusers: POST, paging {"page","pageSize"}, projection = list of separate names,
  "sw" is case-insensitive, "eq" is not. Returns data[].id (= primary email), emails[],
  accounts[].userName (UPN) / active / provisioner / parentGroups / emails.
- clientstatus: GET, `query=username eq '<email>'`, result[] rows, one per device.
- policy/npa/rules: GET without limit returns all rules, {"data": [...]}.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from .client import CAPABILITY_LABELS, _classify
from .lookup import IDENTITY_FIELDS, KIND_HOSTNAME, Query, getusers_body, pick_exact
from .policy import (
    Device, Identity, Match, groups_from_rows, latest_per_device, match_policies, parse_private_apps,
)

WINDOWS_DAYS = (7, 30)          # try a short window first (a wide unfiltered query can 504)
MAX_ROWS = 200
MAX_EMAILS = 3                  # clientstatus queries per person
MAX_HOST_USERS = 3              # people shown for one hostname
CACHE_TTL = 300                 # seconds for rules / apps / classification names
STALE_AFTER = 72 * 3600         # a device silent for longer than this is flagged


class ApiProblem(Exception):
    """A Netskope call failed in a way the user should be told about."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.message, self.kind = message, kind


# ---- HTTP helper ------------------------------------------------------------
def _call(client, method: str, path: str, what: str, capability: str, **kw):
    try:
        resp = client.request(method, path, **kw)
    except requests.exceptions.Timeout:
        raise ApiProblem(f"Netskope did not answer in time ({what}). Try again in a moment.", "timeout") from None
    except requests.exceptions.RequestException:
        raise ApiProblem("Could not reach the Netskope tenant.", "unreachable") from None
    state, detail = _classify(resp)
    if state == "ok":
        try:
            return resp.json()
        except ValueError:
            raise ApiProblem(f"Netskope returned an unreadable answer ({what}).", "bad_response") from None
    label = CAPABILITY_LABELS.get(capability, capability)
    if state == "invalid_token":
        raise ApiProblem("The API token was rejected (expired or revoked). Go back and enter a new token.", state)
    if state == "denied":
        raise ApiProblem(f"The token is not allowed to read {what}. Needs permission: {label}.", state)
    if state == "not_enabled":
        raise ApiProblem(f"{what.capitalize()} is not available on this tenant ({label}).", state)
    if state == "rate_limited":
        raise ApiProblem("Netskope is rate limiting the token. Wait a minute and try again.", state)
    if state == "rejected":
        raise ApiProblem(f"Netskope rejected the {what} request." + (f" ({detail})" if detail else ""), state)
    if resp.status_code == 504:
        raise ApiProblem(f"Netskope timed out ({what}). Try again, the query may have been too heavy.", "timeout")
    raise ApiProblem(f"Netskope returned HTTP {resp.status_code} for {what}.", state)


def _quote(value: str) -> str:
    """Value for a single-quoted Skope query. Characters that could end the quote are refused."""
    if any(c in value for c in ("'", "\\", "\n", "\r", "\x00")):
        raise ApiProblem("That value contains a character (apostrophe or backslash) that the device query "
                         "cannot search for safely yet.", "unsupported")
    return f"'{value}'"


def _cached(cache: dict, key: str, fn):
    hit = cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = fn()
    cache[key] = (now, value)
    return value


# ---- user management ---------------------------------------------------------
@dataclass(frozen=True)
class Account:
    upn: str
    active: bool | None
    provisioner: str
    groups: tuple[str, ...]
    ou: str


@dataclass(frozen=True)
class UserRecord:
    id: str
    name: str
    emails: tuple[str, ...]
    accounts: tuple[Account, ...]


def _str_list(v) -> tuple[str, ...]:
    return tuple(str(x) for x in v) if isinstance(v, list) else ()


def user_record(row: dict) -> UserRecord:
    accs = []
    for a in row.get("accounts") or []:
        if isinstance(a, dict):
            active = a.get("active")
            accs.append(Account(str(a.get("userName", "")), active if isinstance(active, bool) else None,
                                str(a.get("provisioner", "")), _str_list(a.get("parentGroups")), str(a.get("ou") or "")))
    name = " ".join(x for x in (str(row.get("givenName") or ""), str(row.get("familyName") or "")) if x)
    return UserRecord(str(row.get("id", "")), name, _str_list(row.get("emails")), tuple(accs))


def find_users(client, value: str) -> list[UserRecord]:
    """Search by UPN first, then by email. A field the tenant refuses to filter on is skipped."""
    for i, fld in enumerate(IDENTITY_FIELDS):
        try:
            body = _call(client, "POST", "/api/v2/users/getusers", "user management", "users",
                         json=getusers_body(value, field=fld))
        except ApiProblem as e:
            if i and e.kind == "rejected":      # e.g. "Unsupported field" on a fallback field
                continue
            raise
        rows = body.get("data") if isinstance(body, dict) else None
        hits = pick_exact([r for r in rows or [] if isinstance(r, dict)], value)
        if hits:
            return [user_record(r) for r in hits]
    return []


def identity_of(users: list[UserRecord], typed_email: str = "") -> Identity:
    emails: set[str] = {typed_email.lower()} if typed_email else set()
    groups: set[str] = set()
    ous: set[str] = set()
    for u in users:
        emails |= {u.id.lower(), *(e.lower() for e in u.emails)}
        for a in u.accounts:
            groups |= {g.lower() for g in a.groups}
            if a.ou:
                ous.add(a.ou.lower())
    emails.discard("")
    return Identity(frozenset(emails), frozenset(groups), frozenset(ous))


# ---- devices -------------------------------------------------------------------
def _rows(body) -> list[dict]:
    res = body.get("result") if isinstance(body, dict) else None
    if not isinstance(res, list):
        raise ApiProblem("Netskope returned an unexpected device answer.", "bad_response")
    return [r for r in res if isinstance(r, dict)]


def query_devices(client, expr: str, now: int) -> tuple[list[dict], int]:
    """Run a clientstatus query over a 7-day window, then a 30-day one if nothing came back."""
    rows: list[dict] = []
    days = WINDOWS_DAYS[0]
    for days in WINDOWS_DAYS:
        body = _call(client, "GET", "/api/v2/events/datasearch/clientstatus", "device status", "devices",
                     params={"query": expr, "starttime": now - days * 86400, "endtime": now, "limit": MAX_ROWS})
        rows = _rows(body)
        if rows:
            break
    return rows, days


# ---- policy data -----------------------------------------------------------------
def fetch_rules(client, cache: dict) -> list[dict]:
    def load():
        body = _call(client, "GET", "/api/v2/policy/npa/rules", "NPA policy rules", "npa_rules")
        items = body.get("data") if isinstance(body, dict) else body   # real answer: {"data": [...]}
        return [r for r in items or [] if isinstance(r, dict)]
    return _cached(cache, "npa_rules", load)


def fetch_private_apps(client, cache: dict) -> dict:
    def load():
        try:
            return parse_private_apps(_call(client, "GET", "/api/v2/steering/apps/private", "private apps", "private_apps"))
        except ApiProblem:
            return {}
    return _cached(cache, "private_apps", load)


def fetch_class_names(client, cache: dict) -> dict[str, str]:
    """Custom device classification id -> name. Best effort: rules only show the id."""
    def load():
        try:
            body = _call(client, "GET", "/api/v2/deviceclassification/rules", "device classification rules",
                         "device_classification")
        except ApiProblem:
            return {}
        items = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else [])
        return {str(i["id"]): str(i["name"]) for i in items or []
                if isinstance(i, dict) and i.get("id") is not None and i.get("name")}
    return _cached(cache, "class_names", load)


# ---- report ----------------------------------------------------------------------
@dataclass
class DeviceView:
    device: Device
    stale: bool
    searched: bool = False


@dataclass
class UserReport:
    typed: str
    users: list[UserRecord]
    identity: Identity
    devices: list[DeviceView]
    window_days: int
    policy: dict                       # match_policies() result, keyed by device
    groups: list[str] = field(default_factory=list)   # display names, original case
    notes: list[str] = field(default_factory=list)

    @property
    def live(self) -> list[DeviceView]:
        return [d for d in self.devices if not d.device.uninstalled]


@dataclass
class Report:
    kind: str
    value: str
    people: list[UserReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    class_names: dict[str, str] = field(default_factory=dict)


def _report_for(client, cache, typed: str, now: int, host: str = "") -> UserReport:
    users = find_users(client, typed)
    ident = identity_of(users, "" if users else typed)
    emails = sorted(e for e in ident.emails)[:MAX_EMAILS] or [typed.lower()]
    rows: list[dict] = []
    days = WINDOWS_DAYS[0]
    for e in emails:
        r, d = query_devices(client, f"username eq {_quote(e)}", now)
        rows += r
        days = max(days, d)
    ident = Identity(ident.emails, ident.groups | groups_from_rows(rows), ident.ous)
    devices = [DeviceView(d, bool(d.last_event_ts) and now - d.last_event_ts > STALE_AFTER,
                          bool(host) and d.hostname.lower() == host.lower())
               for d in latest_per_device(rows)]
    live = [v.device for v in devices if not v.device.uninstalled]
    policy = match_policies(fetch_rules(client, cache), ident, live)

    notes: list[str] = []
    if not users:
        notes.append("Not found in user management. Showing device data only, matched on the typed address.")
    elif len(users) > 1:
        notes.append(f"{len(users)} user records matched this address.")
    for u in users:
        for a in u.accounts:
            if a.active is False:
                notes.append(f"Account {a.upn} is disabled in user management.")
    if not devices:
        notes.append(f"No device reported in the last {days} days.")
    elif not live:
        notes.append("Every device of this user last reported the client as uninstalled.")
    seen: dict[str, str] = {}
    for name in [g for u in users for a in u.accounts for g in a.groups] + \
            [g for r in rows for g in (r.get("usergroup") or []) if isinstance(g, str)]:
        seen.setdefault(name.lower(), name)
    return UserReport(typed, users, ident, devices, days, policy, sorted(seen.values(), key=str.lower), notes)


def run_lookup(client, q: Query, cache: dict, now: int | None = None) -> Report:
    now = int(now if now is not None else time.time())
    report = Report(q.kind or "", q.value)
    if q.kind == KIND_HOSTNAME:
        rows, days = query_devices(client, f"hostname eq {_quote(q.value)}", now)
        if not rows:
            rows, days = query_devices(client, f"hostname like {_quote(q.value)}", now)
            if rows:
                report.notes.append("No exact hostname match; showing devices whose hostname contains the text.")
        names: list[str] = []
        for r in rows:
            n = str(r.get("username") or (r.get("user_info") or {}).get("username") or "")
            if n and n.lower() not in [x.lower() for x in names]:
                names.append(n)
        if not names:
            report.notes.append(f"No device with this hostname reported in the last {days} days.")
        if len(names) > MAX_HOST_USERS:
            report.notes.append(f"{len(names)} users reported this hostname; showing the first {MAX_HOST_USERS}.")
        for n in names[:MAX_HOST_USERS]:
            report.people.append(_report_for(client, cache, n, now, host=q.value))
    else:
        report.people.append(_report_for(client, cache, q.value, now))
    if report.people:
        report.class_names = fetch_class_names(client, cache)
    return report


# ---- presentation helpers ----------------------------------------------------------
def humanize_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def condition_text(rule, class_names: dict[str, str]) -> str:
    parts = []
    if rule.has_device_class:
        opts = sorted(c.capitalize() for c in rule.classification)
        opts += [class_names.get(i, f"custom #{i}") for i in sorted(rule.custom_ids)]
        parts.append("device classification is " + " or ".join(opts))
    if rule.os:
        parts.append("OS is " + " or ".join(sorted(o.capitalize() if o != "ios" else "iOS" for o in rule.os)))
    return " and ".join(parts)


def match_view(m: Match, class_names: dict[str, str]) -> dict:
    r = m.rule
    return {
        "id": r.rule_id, "name": r.name or f"rule {r.rule_id}", "action": r.action, "note": r.action_note,
        "grants": r.grants, "apps": list(r.apps), "tags": list(r.tags), "via": m.via, "failed": m.failed,
        "wants": condition_text(r, class_names), "methods": list(r.access_methods),
    }
