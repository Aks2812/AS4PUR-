"""
Shared "your wizard progress isn't here anymore" response, used by every
operation everywhere a route currently does `credential_cache.get(...)`
and finds nothing (or finds an entry missing an expected `entry.data`
key from an earlier step). Replaces the old bare
`RedirectResponse(start_url, status_code=303)` - which silently dropped
the operator back at step 1 with zero explanation - with a real page.

As of 2026-09-08, this is no longer a single one-size-fits-all message:
before falling back to the generic "expired" page, it checks
credential_cache's CompletedMarker (see credential_cache.py) for this
session. If the session's last wizard run was recently handed off to a
job, that's a meaningfully different, better situation than expiry or
never-having-started - the operator (very plausibly navigating Back
after a run they already finished, now that real Back links exist
across the app) shouldn't be told to "start again" as if their work was
lost. That case gets its own page pointing straight at the job.

Deliberate simplification that remains, even after the above: the
generic path still doesn't distinguish true TTL expiry from "never
started" - credential_cache doesn't track that distinction, and (as
before) the actual next step ("start again") is the same regardless of
which of those two actually happened.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from ..templating import templates
from .credential_cache import credential_cache


def wizard_expired_response(request: Request, session_key: str, restart_url: str) -> Response:
    marker = credential_cache.get_completed(session_key)
    if marker is not None:
        return templates.TemplateResponse(
            request,
            "wizard_already_completed.html",
            {"flow": marker.flow, "job_url": f"/jobs/{marker.job_id}"},
        )
    return templates.TemplateResponse(request, "wizard_expired.html", {"restart_url": restart_url})
