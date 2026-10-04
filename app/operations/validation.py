"""
Small validation helpers shared across every operation's routes, kept
here (like netskope_http.py and credential_cache.py) specifically to
avoid the two copies of this logic drifting apart - which already
happened once: both operations' tenant/token checks were written
identically, then both needed the identical fix for the same reported
bug (a combined "both are required" message even when only one field
was actually blank).

It happened again with the token itself (hotfix 2026-10-04): only two of the
nine places a token is typed stripped it, so a stray space or line break from
a copy-paste reached the HTTP header, and the HTTP library's error text - which
contains the header VALUE - was stored and shown. `normalise_credentials()` is
now the one place that cleans what the operator typed; every entry point calls
it (through `clean_tenant_token()`), and nothing else strips these fields.
"""
from __future__ import annotations

import unicodedata

__all__ = ["tenant_token_error_message", "normalise_credentials", "clean_tenant_token"]

# Short, and deliberately free of the value: they are shown on screen and the value may be the secret itself.
TOKEN_CHARACTER_MESSAGE = (
    "The API token contains a line break or another character that cannot be part of a token. "
    "Paste it again from the Netskope console, copying only the token itself."
)
TENANT_CHARACTER_MESSAGE = (
    "The tenant name contains a line break or another character that cannot be part of a tenant name. "
    "Check it and type it again."
)


def tenant_token_error_message(tenant: str, token: str) -> str | None:
    """Field-specific required-field message for the tenant/token entry
    step (CLAUDE.md Section 11's Form(...) empty-string sweep). Returns
    None when both are present - callers still own the actual `if not
    tenant or not token:` decision of whether to reject, this only
    decides what to say once they have."""
    if tenant and token:
        return None
    if not tenant and not token:
        return "Both the tenant name and the API token are required."
    if not tenant:
        return "Tenant name is required."
    return "API token is required."


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _cannot_go_in_a_header(value: str) -> bool:
    """A token is sent as an HTTP header value: a control character (line break, tab, NUL, DEL) is refused by the
    HTTP library, and anything outside Latin-1 cannot be encoded at all."""
    return _has_control_character(value) or any(ord(ch) > 0xFF for ch in value)


def normalise_credentials(tenant: str | None, token: str | None) -> tuple[str, str, str | None]:
    """Strips surrounding whitespace (spaces, tabs, line breaks) from the tenant name and the API token, then refuses
    any control character left inside either.

    Returns `(tenant, token, problem)`. On a problem the values come back EMPTY and `problem` is a short message that
    does not repeat them, so a page that re-displays the returned values cannot echo what was typed."""
    tenant = (tenant or "").strip()
    token = (token or "").strip()
    if _has_control_character(tenant):
        return "", "", TENANT_CHARACTER_MESSAGE
    if _cannot_go_in_a_header(token):
        return "", "", TOKEN_CHARACTER_MESSAGE
    return tenant, token, None


def clean_tenant_token(tenant: str | None, token: str | None) -> tuple[str, str, str | None]:
    """What a tenant/token entry route calls: `normalise_credentials()` plus the required-field messages. Returns the
    cleaned `(tenant, token, error_message)`; `error_message` is None when both are present and clean."""
    tenant, token, problem = normalise_credentials(tenant, token)
    if problem:
        return tenant, token, problem
    return tenant, token, tenant_token_error_message(tenant, token)
