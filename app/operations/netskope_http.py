"""
Shared low-level Netskope API plumbing, used by every operation - via
`requests` directly, never subprocess+curl the way the reference scripts
do, and never with certificate verification disabled (CLAUDE.md Section
5's "always verify certificates properly" - `requests` verifies by
default and nothing here ever passes verify=False).

Extracted out of app/operations/private_app_import/netskope_client.py
once Phase 2 (RTP Creation) needed the exact same HTTP-calling
infrastructure (base URL construction, headers, connection/timeout/SSL
error wrapping, list-envelope unwrapping) - duplicating this across two
operations would mean a bug fix in one silently not applying to the
other. Constants that are genuinely per-endpoint (page size conventions
differ - the private-apps list uses limit=1000, the getusers endpoint
uses limit=200, confirmed separately for each) stay local to whichever
operation's client module actually uses them, not here.

Security fix (WSTG-INPV-19, SSRF), shipped 2026-09-08: `base_url()`
previously interpolated the operator-supplied tenant string into the
outbound hostname with ZERO validation - a tenant value containing `?`,
`#`, or `/` breaks out of the intended `*.goskope.com` host entirely
(confirmed empirically: `requests.Request(...).prepare().url` on a
tenant like `evil.local?x=` resolves to a request actually aimed at
`evil.local`, not `<tenant>.goskope.com`). Fixed HERE, in the one
function every operation's every outbound call already funnels through
(confirmed: grepped the whole app, every `f"{base_url(tenant)}/..."` URL
construction anywhere in this project goes through this exact function,
no operation ever builds a Netskope URL any other way) - so the fix
can't be silently bypassed by a route or client module that forgets to
validate separately.
"""
from __future__ import annotations

import re

import requests

TIMEOUT = 30

# Hostname-label pattern (RFC 1035-style): lowercase alphanumeric and
# hyphens only, no leading/trailing hyphen, 1-63 characters. Applied to
# the tenant value AFTER lowercasing, since `*.goskope.com` labels are
# case-insensitive by DNS spec anyway - a tenant typed in mixed case is a
# harmless typo, not something to reject.
_TENANT_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class NetskopeApiError(Exception):
    """Raised for any Netskope API call that failed outright (network
    error, non-2xx, or an unparseable response). Never constructed with
    the token in its message - every caller builds these from the tenant
    name and response, never from request headers."""


def base_url(tenant: str) -> str:
    """
    Raises NetskopeApiError for anything that doesn't look like a single
    valid DNS hostname label - rejecting BEFORE any outbound request is
    attempted, not after. This is the actual SSRF fix: a tenant value
    containing `?`, `#`, `/`, `@`, or anything else outside
    [a-z0-9-] never reaches `requests.request()` at all.
    """
    normalized = tenant.strip().lower()
    if not _TENANT_PATTERN.match(normalized):
        raise NetskopeApiError(
            "Invalid tenant name - tenant names may only contain letters, numbers, and hyphens "
            "(not at the start or end), 1-63 characters. Check for a typo or a stray character."
        )
    return f"https://{normalized}.goskope.com"


def headers(token: str) -> dict:
    return {"Netskope-API-Token": token, "Content-Type": "application/json"}


def bearer_headers(token: str) -> dict:
    """SCIM's own auth convention - `Authorization: Bearer <token>`, not
    `Netskope-API-Token` - confirmed via reference_scripts/
    netskope_scim_manager.py. A genuinely different header from every
    other endpoint this project has touched so far; Local Group/User
    Import (Operation 3) is the only caller."""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def parse_json_body(resp: requests.Response) -> dict:
    """Best-effort JSON body parse - returns {} rather than raising on a
    non-JSON body, so every caller can safely call body.get(...) either
    way."""
    try:
        parsed = resp.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def body_ok(resp: requests.Response, body: dict, *, ok_codes: tuple[int, ...] = (200, 201)) -> bool:
    """CLAUDE.md Section 9: "HTTP 200 does not mean success" - Netskope can
    return a 200/201 with an error in the response body, so the body's own
    `status` field is the only thing trusted, never the HTTP status code
    alone. This is the exact check every write call in this project already
    used inline (private_app_import.create_private_app, rtp_creation's
    create_rtp_rule/patch_rule_users/fetch_rule_by_id) - centralized here,
    2026-09-27, so a new caller (device_posture's read-only GET endpoints,
    the first to check this on a list/read call rather than a write) reuses
    it instead of writing a slightly different version."""
    return resp.status_code in ok_codes and body.get("status") == "success"


def execution_ok(resp: requests.Response, body: dict, *, ok_codes: tuple[int, ...] = (200, 201)) -> bool:
    """A second, DIFFERENT "was this actually a success" convention from
    body_ok() above - do not conflate them. Confirmed real, 2026-09-27, via
    a raw `clientstatus` response pasted directly into this project (see
    CLAUDE.md Section 9): here `status` is an OBJECT
    (`{"execution": "SUCCESS"|other, "status_code": N, "message": "...",
    "count": N}`), not a top-level string - so the error text on failure
    lives at `body["status"]["message"]`, not `body["message"]`. This is
    device_posture's `fetch_client_status()`'s own confirmed convention
    only - it must NOT replace body_ok() for the three write endpoints
    that already use it (private_app_import.create_private_app,
    rtp_creation's create_rtp_rule/patch_rule_users/fetch_rule_by_id),
    whose real, separately-confirmed convention is body_ok()'s top-level
    string. Two honest functions for two genuinely different real API
    conventions."""
    status = body.get("status")
    status = status if isinstance(status, dict) else {}
    return resp.status_code in ok_codes and status.get("execution") == "SUCCESS"


def unwrap_list(data, extra_keys: tuple[str, ...] = ()) -> list:
    """Netskope's list endpoints have been observed nesting the actual
    array under a few different keys depending on endpoint - defensive
    unwrapping matches reference_scripts' own handling of this exact
    ambiguity. "publishers" is confirmed real behavior (live-verified
    against a production tenant, 2026-08), not a guess: the publishers endpoint
    responds `{"data": {"publishers": [...]}, "status": "success", "total": N}`.
    `extra_keys` lets a caller add endpoint-specific keys (checked first)
    without every caller needing to know every other endpoint's key.

    Only unwraps ONE level, not recursively - confirmed 2026-09-27
    (tests/test_netskope_http.py) that feeding this the raw, un-stripped
    `fetch_publishers()` response shape (`{"data": {"publishers": [...]},
    ...}`) returns [] rather than the real list, because it stops after
    matching "data" and never re-checks the result for a further-nested
    key. Not a live bug: every current caller
    (private_app_import/netskope_client.py) already pre-strips one level
    itself (`parsed.get("data", ...)`) before calling this - but a future
    caller passing a genuinely raw, multi-level-nested body would hit this
    silently (empty list, no exception) rather than get an error."""
    if isinstance(data, dict):
        for key in (*extra_keys, "private_apps", "apps", "app_list", "publishers", "data"):
            if key in data:
                data = data[key]
                break
    return data if isinstance(data, list) else []


# Fixed on purpose. When a header value is not allowed (a leading space, a line break, a character outside Latin-1)
# the HTTP library's own error text IS the header value - here, the API token - and this wrapper used to copy that
# text into NetskopeApiError, which the operations then stored (jobs.error_message, job_items.message) and showed
# (hotfix 2026-10-04). So that text is never copied, and never chained to the new error (`from None`), because the
# job manager prints the full traceback, chain included. Errors whose text is worth keeping (a certificate problem
# names the certificate) are copied with any credential removed, and chained to a scrubbed copy (_scrubbed_copy).
INVALID_TOKEN_MESSAGE = (
    "The API token could not be sent to Netskope: it contains a line break, a leading space or another character "
    "that is not allowed in an HTTP header. Enter it again, copying only the token itself."
)


def _secret_forms(token: str, sent: dict) -> list[str]:
    """Everything that must never be copied into an error message, longest first: the token as typed and stripped,
    every credential header value that was sent (and the part after `Bearer `), each also the way `repr()` writes it
    (a line break becomes backslash-n)."""
    raw = {token, token.strip()}
    for name, value in sent.items():
        if isinstance(value, str) and name.lower() not in ("content-type", "accept"):
            raw.update((value, value.strip()))
            if value.lower().startswith("bearer "):
                raw.add(value[7:].strip())
    forms = set()
    for secret in raw:
        if len(secret) >= 4:
            forms.update((secret, repr(secret)[1:-1]))
    return sorted(forms, key=len, reverse=True)


def _without_secrets(exc: Exception, token: str, sent: dict) -> str:
    """The exception's text with any credential in it removed - for the library errors whose text is kept because it
    is useful (a certificate problem names the certificate) but that could still carry a header."""
    text = str(exc)
    for form in _secret_forms(token, sent):
        text = text.replace(form, "[removed]")
    return text


def _scrubbed_copy(exc: Exception, token: str, sent: dict) -> Exception:
    """The exception rebuilt with credentials removed from its text. It is what the new error is chained to: the SAME
    class, because the NPA export classifies a failure by the type of its cause (timeout, certificate, other), but
    nothing a printed traceback chain shows can carry a header."""
    text = _without_secrets(exc, token, sent)
    try:
        return type(exc)(text)
    except Exception:
        return requests.exceptions.RequestException(text)


def request(
    method: str, url: str, token: str, action: str, *, headers_override: dict | None = None, **kwargs
) -> requests.Response:
    sent = headers_override or headers(token)
    try:
        return requests.request(method, url, headers=sent, timeout=TIMEOUT, **kwargs)
    except requests.exceptions.InvalidHeader:                       # before RequestException: it is one, and so is a ValueError
        raise NetskopeApiError(INVALID_TOKEN_MESSAGE) from None
    except requests.exceptions.SSLError as exc:
        raise NetskopeApiError(f"TLS certificate problem while {action}: {_without_secrets(exc, token, sent)}") from _scrubbed_copy(exc, token, sent)
    except requests.exceptions.ConnectionError as exc:
        raise NetskopeApiError(
            f"Could not reach the tenant while {action} - check the tenant name and network connectivity."
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise NetskopeApiError(f"Timed out while {action} - the tenant may be unreachable.") from exc
    except requests.exceptions.RequestException as exc:
        raise NetskopeApiError(f"Request failed while {action}: {_without_secrets(exc, token, sent)}") from _scrubbed_copy(exc, token, sent)
    except ValueError:                                              # not a `requests` error: http.client or the codec refusing a header
        raise NetskopeApiError(INVALID_TOKEN_MESSAGE) from None
