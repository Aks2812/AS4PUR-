"""
Read-only Netskope calls for Data Export (fifth operation). Everything goes
through the shared plumbing in app/operations/netskope_http.py (base_url's
tenant validation, the token header, connection/timeout/TLS error wrapping,
body_ok, unwrap_list) - there is no HTTP client of its own here. What this
module adds is what no existing client needed:

  * pacing on the 4-per-second endpoints (users/counts, getusers, getgroups)
  * a bounded retry for 429 and 5xx, honouring Retry-After, then
    RateLimit-Reset, else exponential - retries live HERE ONLY; the other
    operations' clients are untouched
  * both error-body shapes Netskope uses (a bare JSON string, or
    {"status": "<message>"}), turned into one readable line
  * the completeness invariants: an export is never partial. A mismatch
    between what Netskope says exists and what came back raises
    ExportIncompleteError, and the caller produces no file.

Facts this is built on (recorded against a lab tenant, 2026-09-30):
  private apps  GET /api/v2/steering/apps/private
                {"data": {"private_apps": [...]}, "status": "success", "total": N}.
                limit=1000 returned all 14 apps; `offset` does NOT act as an
                item index (limit=5&offset=5 returned nothing although items
                5-9 exist), so there is no paging: ONE call, then
                len(private_apps) == total or fail.
  users/groups  POST /api/v2/users/getusers, /getgroups
                {"counts": {"totalResults", "offset", "itemsPerPage"}, "data": [...]}
                - no `status` field. Paged with Operation 2's proven body,
                {"query": {"paging": {"offset": o, "limit": n}}}. Nothing past
                page 1 had been exercised on either endpoint when this was
                written, hence the paging checks below.
  users/counts  GET /api/v2/users/counts?cached=false - a bare object, no envelope.
  rate limits   list 30/s + 1000/h; users/counts, getusers, getgroups 4/s.

Never used: /api/v2/platform/administration/scim/Users (that lists Netskope
console ADMINS, not end users), and the SCIM endpoints (not needed).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ..netskope_http import NetskopeApiError, base_url, body_ok, parse_json_body, request, unwrap_list

APPS_LIMIT = 1000        # one call returns at most this many apps; offset paging does not work on this endpoint
PACE_SECONDS = 0.3       # >= this between calls on the 4/s endpoints (250 ms is the hard floor at 4/s)
MAX_RETRIES = 3          # retries after the first attempt, for 429 and 5xx
BACKOFF_BASE_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0  # never block a worker thread longer than this on one Retry-After
MAX_PAGES = 1000         # runaway-loop guard: 1000 pages x 200 rows is far beyond any real tenant
_MAX_DETAIL_CHARS = 200

_APPS_PATH = "/api/v2/steering/apps/private"
_COUNTS_PATH = "/api/v2/users/counts"
_USERS_PATH = "/api/v2/users/getusers"
_GROUPS_PATH = "/api/v2/users/getgroups"

__all__ = [
    "NetskopeApiError",
    "ExportIncompleteError",
    "PageTrace",
    "PagedResult",
    "PrivateApps",
    "Pacer",
    "fetch_private_apps",
    "fetch_users",
    "fetch_groups",
    "fetch_user_counts",
]


class ExportIncompleteError(Exception):
    """What Netskope returned does not add up to what it said exists (a short or
    repeated page, a changing total, a duplicate id, an app count that differs
    from `total`). The message is written for the operator; the caller shows it
    and produces NO file. A paging failure also records which endpoint stalled and
    the pages read before it did, so a live check can say exactly where it stopped."""

    def __init__(self, message: str, *, endpoint: str | None = None, pages: list | None = None) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.pages = pages or []


@dataclass
class PageTrace:
    offset: int
    limit: int
    rows: int
    first_id: str


@dataclass
class PagedResult:
    rows: list[dict]
    total: int
    pages: list[PageTrace] = field(default_factory=list)


@dataclass
class PrivateApps:
    apps: list[dict]
    total: int


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Pacer:
    """A pause before every call except the first, so calls on the 4/s endpoints are
    at least `min_interval` apart. One Pacer is shared by everything one export run
    sends to those endpoints (users, groups and counts together), not one per endpoint."""

    def __init__(self, min_interval: float = PACE_SECONDS) -> None:
        self._interval = min_interval
        self._calls = 0

    def wait(self) -> None:
        if self._calls:
            _sleep(self._interval)
        self._calls += 1


# --------------------------------------------------------------------------
# Errors: both shapes, one readable line
# --------------------------------------------------------------------------

def _error_detail(resp) -> str:
    """Netskope error bodies arrive as a bare JSON string ("request body has an
    error: ...") or as an object: {"message": ..., "status": "error"} (HTTP 200 or
    not, CLAUDE.md Section 9) or {"status": "<message>"}. Anything else -
    empty, HTML, a list - has no usable text."""
    try:
        parsed = resp.json()
    except ValueError:
        return ""
    if isinstance(parsed, str):
        text = parsed
    elif isinstance(parsed, dict):
        message = parsed.get("message")
        status = parsed.get("status")
        text = message if isinstance(message, str) else status if isinstance(status, str) else ""
    else:
        text = ""
    return " ".join(text.split())[:_MAX_DETAIL_CHARS]


def _raise_for_status(resp, what: str) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code == 401:
        raise NetskopeApiError("Token was rejected (HTTP 401) - it's likely wrong or expired.")
    if resp.status_code == 403:
        raise NetskopeApiError(f"Token was accepted but lacks the required scope (HTTP 403) to read {what}.")
    detail = _error_detail(resp)
    raise NetskopeApiError(
        f"Netskope rejected the request for {what} (HTTP {resp.status_code})" + (f": {detail}" if detail else ".")
    )


def _header_seconds(resp, name: str) -> float | None:
    raw = resp.headers.get(name)
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _retry_wait(resp, attempt: int) -> float:
    for name in ("Retry-After", "RateLimit-Reset"):
        seconds = _header_seconds(resp, name)
        if seconds is not None:
            return seconds
    return BACKOFF_BASE_SECONDS * (2 ** attempt)


def _call(method: str, tenant: str, token: str, path: str, action: str, *, params=None, json_body=None, pacer: Pacer | None = None):
    url = f"{base_url(tenant)}{path}"          # validates the tenant BEFORE anything is sent
    if pacer is not None:
        pacer.wait()
    attempt = 0
    while True:
        resp = request(method, url, token, action, params=params, json=json_body)
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        detail = _error_detail(resp)
        if attempt >= MAX_RETRIES:
            raise NetskopeApiError(
                f"Netskope kept answering HTTP {resp.status_code} while {action} (gave up after {MAX_RETRIES} retries)"
                + (f": {detail}" if detail else ".")
            )
        wait = _retry_wait(resp, attempt)
        if wait > MAX_WAIT_SECONDS:
            raise NetskopeApiError(
                f"Netskope asked for a {int(wait)}-second wait (HTTP {resp.status_code}) while {action}, which is longer "
                f"than this export will wait. Try again later."
            )
        _sleep(wait)
        attempt += 1


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --------------------------------------------------------------------------
# Private apps: one call, then len == total
# --------------------------------------------------------------------------

def fetch_private_apps(tenant: str, token: str) -> PrivateApps:
    resp = _call("GET", tenant, token, _APPS_PATH, "reading private apps", params={"limit": APPS_LIMIT, "offset": 0})
    _raise_for_status(resp, "private apps")
    body = parse_json_body(resp)
    if not body_ok(resp, body):
        detail = _error_detail(resp)
        raise NetskopeApiError("Netskope did not report success reading private apps" + (f": {detail}" if detail else "."))

    data = body.get("data")
    if not (isinstance(data, dict) and isinstance(data.get("private_apps"), list)):
        raise NetskopeApiError("Netskope's private-apps response had an unexpected shape, so nothing was exported.")
    apps = unwrap_list(data, extra_keys=("private_apps",))
    if any(not isinstance(a, dict) for a in apps):
        raise NetskopeApiError("Netskope's private-apps response contained an entry that is not an app, so nothing was exported.")

    total = body.get("total")
    if not _is_count(total):
        raise ExportIncompleteError(
            "Netskope did not report a usable total number of private apps, so the export cannot be checked "
            "for completeness. No file was produced."
        )
    if len(apps) != total:
        if total > APPS_LIMIT:
            raise ExportIncompleteError(
                f"This tenant reports {total} private apps, more than the {APPS_LIMIT} that one request can return "
                f"(paging by offset does not work on this endpoint), so the export cannot be completed. No file was produced."
            )
        raise ExportIncompleteError(
            f"Netskope reports {total} private apps but returned {len(apps)}. No file was produced, so a partial "
            f"export can never be mistaken for a complete one."
        )
    return PrivateApps(apps=apps, total=total)


# --------------------------------------------------------------------------
# users/counts (the soft cross-check's source)
# --------------------------------------------------------------------------

def fetch_user_counts(tenant: str, token: str, pacer: Pacer | None = None) -> dict:
    resp = _call("GET", tenant, token, _COUNTS_PATH, "reading user counts", params={"cached": "false"}, pacer=pacer or Pacer())
    _raise_for_status(resp, "user counts")
    try:
        body = resp.json()
    except ValueError as exc:
        raise NetskopeApiError("Netskope's user-counts response wasn't valid JSON.") from exc
    if not (isinstance(body, dict) and _is_count(body.get("totalUsers"))):
        raise NetskopeApiError("Netskope's user-counts response had an unexpected shape.")
    return body


# --------------------------------------------------------------------------
# getusers / getgroups
# --------------------------------------------------------------------------

def _fetch_paged(tenant: str, token: str, path: str, what: str, page_size: int, pacer: Pacer | None) -> PagedResult:
    """Pages with {"query": {"paging": {"offset", "limit"}}}, advancing by the number of
    rows actually received (a server that returns fewer rows than asked for would lose
    rows if the offset advanced by the requested limit). Never returns a partial list:

      - the first page's totalResults is the expected count; a later page that reports a
        different total means the data moved while it was being read
      - a page whose first id equals the previous page's first id is a repeat, not progress
      - an empty page before the total is reached is a stall
      - an id seen twice is an error
      - rows read must equal totalResults exactly
    """
    rows: list[dict] = []
    seen: set[str] = set()
    pages: list[PageTrace] = []
    total: int | None = None
    previous_first: str | None = None
    offset = 0

    for _ in range(MAX_PAGES):
        body = {"query": {"paging": {"offset": offset, "limit": page_size}}}
        resp = _call("POST", tenant, token, path, f"reading {what}", json_body=body, pacer=pacer)
        _raise_for_status(resp, what)
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise NetskopeApiError(f"Netskope's {what} response wasn't valid JSON.") from exc

        counts = parsed.get("counts") if isinstance(parsed, dict) else None
        data = parsed.get("data") if isinstance(parsed, dict) else None
        page_total = counts.get("totalResults") if isinstance(counts, dict) else None
        if not (_is_count(page_total) and isinstance(data, list)):
            raise NetskopeApiError(f"Netskope's {what} response had an unexpected shape, so nothing was exported.")

        if total is None:
            total = page_total
        elif page_total != total:
            raise ExportIncompleteError(
                f"The number of {what} changed while they were being read (was {total}, now {page_total}). "
                f"Run the export again. No file was produced.",
                endpoint=what, pages=list(pages)
            )

        if not data:
            if len(rows) < total:
                raise ExportIncompleteError(
                    f"Netskope returned an empty page after {len(rows)} of {total} {what} - paging stopped advancing. "
                    f"No file was produced.",
                endpoint=what, pages=list(pages)
                )
            break

        ids: list[str] = []
        for item in data:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(item_id, str) or not item_id:
                raise NetskopeApiError(f"Netskope's {what} response contained an entry without an id, so nothing was exported.")
            ids.append(item_id)

        if previous_first is not None and ids[0] == previous_first:
            raise ExportIncompleteError(
                f"Netskope returned the same page of {what} again at offset {offset} - paging did not advance. "
                f"No file was produced.",
                endpoint=what, pages=list(pages)
            )
        for item_id in ids:
            if item_id in seen:
                raise ExportIncompleteError(
                    f"Netskope returned a duplicate entry while paging {what} (the same id twice). No file was produced.",
                endpoint=what, pages=list(pages)
                )
            seen.add(item_id)

        rows.extend(data)
        pages.append(PageTrace(offset=offset, limit=page_size, rows=len(data), first_id=ids[0]))
        previous_first = ids[0]
        offset += len(data)
        if len(rows) >= total:
            break
    else:
        raise ExportIncompleteError(
            f"Stopped after {MAX_PAGES} pages without reaching the end of the {what} list. No file was produced.",
                endpoint=what, pages=list(pages)
        )

    if len(rows) != total:
        raise ExportIncompleteError(
            f"Read {len(rows)} {what} but Netskope reports {total}. No file was produced.",
                endpoint=what, pages=list(pages)
        )
    return PagedResult(rows=rows, total=total, pages=pages)


def fetch_users(tenant: str, token: str, page_size: int, pacer: Pacer | None = None) -> PagedResult:
    return _fetch_paged(tenant, token, _USERS_PATH, "users", page_size, pacer or Pacer())


def fetch_groups(tenant: str, token: str, page_size: int, pacer: Pacer | None = None) -> PagedResult:
    return _fetch_paged(tenant, token, _GROUPS_PATH, "groups", page_size, pacer or Pacer())
