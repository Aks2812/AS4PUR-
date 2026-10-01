"""
Part F of the RBAC build (2026-09-09): the minimum-one-admin guardrail.

Structural, not just a per-route check - registered here, once, as a
SQLAlchemy `before_flush` event on the base Session class, so it applies
to every write made through the ORM regardless of which route (or some
future code path nobody has written yet) makes it. app/models.py imports
this module at its bottom purely for this side effect (the event
registration) - the same "importing a module registers something global"
pattern app/db.py's own init_db() already relies on for Base.metadata.

Two layers, deliberately, not one:
1. This module - the actual, unconditional enforcement. Fires at flush
   time no matter which code wrote the change, including a hypothetical
   future route that forgets to check first, or a raw ORM manipulation
   in a test/shell.
2. app/admin/routes.py's own pre-checks (`would_leave_zero_active_admins`)
   - a courtesy that produces a clean 400 error page from the actual
   route BEFORE ever attempting the write. That layer exists purely for
   a better user-facing message; THIS layer is what actually guarantees
   the invariant can never be violated through the ORM, period.

Known, accepted limitation - no real code path in this app exercises it
today: a single flush that simultaneously promotes one user to admin AND
demotes/deactivates the previously-last admin could be incorrectly
refused, because a query issued from inside before_flush only sees
already-flushed state - the promoted user would still look "not yet
admin" to that query. Every admin action in this app changes exactly one
User row per request/commit today; if a future feature ever needs to
change two users' admin status in the same transaction, revisit this
before relying on it.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session as ORMSession
from sqlalchemy.orm import attributes


class MinimumAdminViolation(Exception):
    """Raised when a pending flush would leave zero active
    (role=ADMIN, is_active=True) User rows. app/main.py turns this into a
    clean 409 response rather than a raw 500 - belt and braces, in case
    some future write path ever reaches this without going through
    admin/routes.py's own friendly pre-check first."""


@event.listens_for(ORMSession, "before_flush")
def _guard_minimum_one_admin(session, flush_context, instances) -> None:
    # Deferred import, deliberately: this module is imported FROM
    # app/models.py (at its bottom) purely to register this event, so
    # importing models.py's own classes back at THIS module's top level
    # would be a real circular import. Safe here because this function
    # only runs at flush time, long after both modules are fully loaded.
    from ..models import Role, User

    def is_active_admin(role, is_active) -> bool:
        return role == Role.ADMIN and bool(is_active)

    losing_admin_status_ids: set[int] = set()

    for obj in session.dirty:
        if not isinstance(obj, User) or not session.is_modified(obj, include_collections=False):
            continue
        role_hist = attributes.get_history(obj, "role")
        active_hist = attributes.get_history(obj, "is_active")
        # get_history().deleted holds the OLD value only when that
        # specific attribute actually changed - fall back to the
        # current value (unchanged) otherwise.
        old_role = role_hist.deleted[0] if role_hist.deleted else obj.role
        old_active = active_hist.deleted[0] if active_hist.deleted else obj.is_active
        if is_active_admin(old_role, old_active) and not is_active_admin(obj.role, obj.is_active):
            losing_admin_status_ids.add(obj.id)

    for obj in session.deleted:
        if isinstance(obj, User) and is_active_admin(obj.role, obj.is_active):
            losing_admin_status_ids.add(obj.id)

    if not losing_admin_status_ids:
        return

    # Autoflush is automatically suspended for the duration of a
    # before_flush handler (SQLAlchemy's own documented guarantee, to
    # prevent recursive flushes) - this query is the standard, safe way
    # to read already-committed state from inside one. Every OTHER User
    # row not in losing_admin_status_ids is, by definition, not being
    # touched by this flush, so its currently-persisted state is exactly
    # its state after this flush too.
    remaining = (
        session.query(User)
        .filter(User.role == Role.ADMIN, User.is_active.is_(True), ~User.id.in_(losing_admin_status_ids))
        .count()
    )
    if remaining == 0:
        raise MinimumAdminViolation(
            "This change would leave zero active admin accounts - promote another account to admin first."
        )
