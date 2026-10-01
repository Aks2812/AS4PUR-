"""
Schema shared by every operation (CLAUDE.md Section 6 / 7 / 11).

Two tables never contain a Netskope API token or SCIM bearer token, by
design, under any column - AuditLog and Job/JobItem. That rule is the
whole point of these tables existing (CLAUDE.md Section 3 / 5): they are
the persistent, exportable audit record, and the credential used to do the
work is never part of that record.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import utcnow


class Role(str, enum.Enum):
    OPERATOR = "operator"
    VIEWER = "viewer"
    # Added 2026-09-08 (Part B of the RBAC build) - a real admin-capability
    # surface (invites, password resets, activation/deactivation - Parts
    # C-F) now exists and needs a role to gate it. Deliberately additive:
    # does NOT retroactively restrict Op1/2/3 usage or job/audit
    # visibility by role - every existing user's access is unchanged by
    # this value's mere existence (see app/auth/dependencies.py's
    # require_admin, and the migration that introduces this - both are
    # inert until something Depends() on require_admin).
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Enforcement deferred to a later phase (operator's explicit decision,
    # 2026-08) - the column exists now precisely so it doesn't need a
    # migration later. Every user is effectively unrestricted in v1
    # regardless of this value.
    role: Mapped[Role] = mapped_column(SAEnum(Role), nullable=False, default=Role.OPERATOR)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Part D of the RBAC build (2026-09-09) - the ONLY mechanism anywhere
    # in this app that can force a password change. Nothing scheduled,
    # cron-based, or time-based ever sets this; calendar-based password
    # expiry was deliberately considered and rejected earlier this
    # project. Only two admin actions (app/admin/routes.py's
    # force_password_reset/reset_password) and this column's own default
    # ever touch it - and the user's own completion of app/auth/
    # password_change.py's mandatory page is the only thing that clears
    # it.
    # server_default matters here, unlike the other boolean columns above -
    # this one's migration ADDS a NOT NULL column to a table that already
    # has real rows (confirmed the hard way against a scratch copy of the
    # real file: SQLite refuses "ADD COLUMN ... NOT NULL" with no default
    # when the table isn't empty). Kept here too, not just in the
    # migration, so a future autogenerate sees the model and the DB agree
    # and reports no drift.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    """
    A server-side session record. The cookie handed to the browser only
    ever holds an opaque random token; this table stores the SHA-256 hash
    of that token, never the token itself, so reading the database doesn't
    hand out a usable session.
    """

    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped["User"] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Login/logout and other events not already captured by a Job row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)  # success | failure | info
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class Job(Base):
    """
    One row per operation run (Private App Import / RTP Creation / Local
    Group Import) - the persistent, exportable audit record CLAUDE.md
    Section 6 requires, replacing the CLI version's state.csv/run logs.
    `job_type` is a plain string rather than an Enum deliberately: which
    operations exist is a Phase 1/2/3 concern, not a Phase 0 schema
    decision.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), nullable=False, default=JobStatus.PENDING, index=True)
    created_by_username: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    input_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["JobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobItem.id"
    )


class Invite(Base):
    """
    Invite-based registration (Part C of the RBAC build, 2026-09-08).
    `token_hash` follows the exact same discipline as UserSession's own
    `token_hash` (see that model's docstring): the raw, single-use
    registration token is generated once, shown to the issuing admin, and
    only its SHA-256 hash is ever persisted - a database read alone never
    yields a usable, unused invite link.

    Single-use is enforced at the database level via an atomic conditional
    UPDATE at consumption time (`used_at IS NULL -> now`, checking the
    affected row count), not a SELECT-then-check-then-UPDATE - see
    app/auth/registration.py. `User.username` already being `unique=True`
    is a second, independent backstop: even in a race that somehow got
    past the conditional UPDATE, a second INSERT for the same email (the
    invite's email IS the resulting username) would still fail on that
    existing constraint rather than silently create a duplicate account.
    """

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[Role] = mapped_column(SAEnum(Role), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobItem(Base):
    """
    One row per item processed within a job (a spreadsheet row, a user
    email, ...). `status` is a free-form string, not an Enum, deliberately:
    CLAUDE.md Section 7 asks that operations reuse Operation 1's
    established outcome vocabulary (SUCCESS / SKIPPED_EXISTS / FAILED /
    VALIDATION_EXCLUDED / NEVER_PROCESSED) rather than invent new
    categories per operation, but a future operation may still need one
    more - a plain string needs no migration for that, an Enum would.
    """

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="items")


# Registers the minimum-one-admin guardrail's before_flush event (Part F
# of the RBAC build, 2026-09-09) - imported here, at the bottom, purely
# for that side effect, once every class above is defined. See that
# module's own docstring for why this belongs at the ORM/Session level
# rather than only inside app/admin/routes.py.
from .security import admin_guard  # noqa: E402,F401
