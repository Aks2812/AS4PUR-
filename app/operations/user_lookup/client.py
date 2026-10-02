"""
The small HTTP client user_lookup's service.py expects: one object with
`request(method, path, params=, json=) -> requests.Response`, plus the
status classifier and permission labels used to word API errors.

Built on netskope_http (base_url's tenant validation, the
Netskope-API-Token header, TIMEOUT) so the SSRF fix there still covers
every call made here. Connection errors are left as `requests`
exceptions on purpose: service._call turns them into ApiProblem.
"""
from __future__ import annotations

import re

import requests

from ..netskope_http import TIMEOUT, base_url, headers

# Read permissions the lookup needs, in the words shown when one is missing.
CAPABILITY_LABELS = {
    "users": "Users: email, UPN, groups",
    "devices": "Device status (client status)",
    "device_classification": "Device classification rules",
    "npa_rules": "NPA policy rules",
    "private_apps": "Private apps and tags",
}


class TenantClient:
    def __init__(self, tenant: str, token: str):
        self.base = base_url(tenant)            # raises NetskopeApiError for a bad tenant name
        self._s = requests.Session()
        self._s.headers.update(headers(token))
        self._s.headers["Accept"] = "application/json"

    def __repr__(self) -> str:                  # never print the token
        return f"<TenantClient {self.base}>"

    def request(self, method: str, path: str, *, params=None, json=None) -> requests.Response:
        # allow_redirects=False: requests does not strip Netskope-API-Token on a cross-host redirect.
        return self._s.request(method, self.base + path, params=params, json=json,
                               timeout=TIMEOUT, allow_redirects=False)

    def close(self) -> None:
        self._s.close()


def _detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    msg = ""
    if isinstance(body, dict):
        for k in ("message", "msg", "description", "error", "detail", "errors"):
            v = body.get(k)
            if v:
                msg = v if isinstance(v, str) else str(v)
                break
    return re.sub(r"[\x00-\x1f\x7f]", " ", msg)[:160]


def _classify(resp: requests.Response) -> tuple[str, str]:
    code = resp.status_code
    detail = _detail(resp)
    low = (detail or resp.text[:300]).lower()
    if 200 <= code < 300:
        return "ok", ""
    if 300 <= code < 400:
        return "redirect", ""
    if code == 401:
        return "invalid_token", detail
    if "not enabled" in low:
        return "not_enabled", detail
    if code == 403:
        return "denied", detail
    if code == 404:
        return "not_enabled", detail
    if code == 400:
        return "rejected", detail
    if code == 429:
        return "rate_limited", detail
    return "error", detail
