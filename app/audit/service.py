from __future__ import annotations

from sqlalchemy.orm import Session as DBSession

from ..models import AuditLog


def log_event(
    db: DBSession,
    *,
    action: str,
    outcome: str,
    actor_username: str | None = None,
    tenant: str | None = None,
    detail: str | None = None,
    source_ip: str | None = None,
) -> None:
    """
    Writes one audit record. Per CLAUDE.md Section 3/5: `detail` must never
    contain a credential/token - this function trusts its callers the same
    way the rest of the app does (the token itself should never reach any
    function outside the request handler / job target that uses it).
    """
    db.add(
        AuditLog(
            action=action,
            outcome=outcome,
            actor_username=actor_username,
            tenant=tenant,
            detail=detail,
            source_ip=source_ip,
        )
    )
    db.commit()
