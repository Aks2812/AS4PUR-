"""
Small validation helpers shared across every operation's routes, kept
here (like netskope_http.py and credential_cache.py) specifically to
avoid the two copies of this logic drifting apart - which already
happened once: both operations' tenant/token checks were written
identically, then both needed the identical fix for the same reported
bug (a combined "both are required" message even when only one field
was actually blank).
"""
from __future__ import annotations

__all__ = ["tenant_token_error_message"]


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
