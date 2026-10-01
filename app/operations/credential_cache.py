"""
Holds an operator's Netskope tenant+token in server memory for the
duration of one guided operation flow (tenant entry -> publisher/group
selection -> upload -> review -> confirm) - never on disk, never in the
database, never in a cookie (CLAUDE.md Section 3). Keyed by the AS4PUR
session's own token_hash (never the Netskope token, never the raw session
cookie value), so a browser carries everything needed across the wizard's
several requests via nothing but its existing as4pur_session cookie.

One entry per AS4PUR session, not per tab or per operation - this is a
single slot, deliberately, matching how a browser session historically
only ever ran one wizard at a time. See FlowCollisionError below for what
happens now when two different flows try to share that one slot instead
of one silently clobbering the other.

Separately, this module also tracks (as of 2026-09-08) whether a
session's last wizard run actually finished - see CompletedMarker below.
This is a distinct, much smaller and shorter-scoped record than a
WizardEntry, kept specifically so app/operations/wizard_expired.py can
tell "you already completed this" apart from genuine expiry once the
WizardEntry itself is gone (which it always is, immediately, once a job
starts - see the first bullet below).

Cleared on every path deliberately, not just the happy one:
- immediately once a job actually starts - pop() hands the entry to the
  caller, who passes tenant/token into the job's background thread as a
  plain argument (see app/jobs/manager.py); the entry has no further
  reason to exist here once that handoff happens
- on logout (app/auth/routes.py calls discard() there too)
- by TTL, in case a wizard is abandoned mid-flow (tab closed, session
  left idle) without an explicit logout. Sliding, not fixed, as of
  2026-09-08 (see get()'s own docstring) - matches app/security/sessions.py's
  own login-session TTL, which has always been sliding.

An operation stashes whatever wizard-scoped scratch data it needs
(publisher selection, the parsed manifest, ...) under `entry.data` - a
plain dict, deliberately untyped here so this module stays reusable
across Phase 1/2/3 without importing any one operation's row/manifest
types. One key is a cross-operation convention rather than private to any
one operation: if a temp upload file's path is stashed under
`data["upload_path"]`, this module's own cleanup (TTL expiry, logout)
deletes it - so an operation doesn't need to be reachable from
app/auth/routes.py just to get its temp file cleaned up on those paths.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


class FlowCollisionError(Exception):
    """
    Raised by start() when this session's one wizard slot is already
    occupied by a DIFFERENT, still-active flow - added 2026-09-08 as
    foundation work for the Back-navigation UI. Before this, start() from
    a second tab/operation would silently overwrite the first tab's
    in-progress tenant/token/data with zero warning, including deleting
    its in-progress upload file - a real, previously-undiscovered
    cross-tab data-loss bug, not a hypothetical one.

    Re-submitting tenant/token for the SAME flow is unaffected by this -
    that's still the existing, deliberate "starting over" behavior
    (start()'s own comment below), not a collision.
    """

    def __init__(self, existing_flow: str, existing_tenant: str) -> None:
        self.existing_flow = existing_flow
        self.existing_tenant = existing_tenant
        super().__init__(
            f"You have an in-progress {existing_flow} run for {existing_tenant} - "
            f"finish or abandon it before starting a new one."
        )


@dataclass
class WizardEntry:
    tenant: str
    token: str
    # Identifies which wizard this entry belongs to (e.g. "Private App
    # Import", "RTP Creation (add users to existing rule)") - required at
    # construction, not defaulted, so every start() caller is explicit
    # about it (this is exactly the value FlowCollisionError compares and
    # reports). The four current flows are their own distinct values here
    # - Op2's "create a new rule" and "add users to an existing rule"
    # paths are two different flows despite sharing one operation and one
    # router, not one.
    flow: str
    # Renamed from `created_at` 2026-09-08: this is now "last touched",
    # not "created" - get() advances it on every successful read (sliding
    # TTL). Nothing outside this module ever reads this field directly
    # (confirmed via a full-app grep before renaming it), so the rename
    # itself changes no other code.
    # `lambda: time.monotonic()`, not `time.monotonic` directly: a bare
    # function reference as default_factory is captured ONCE, at class
    # definition time (module import) - a test monkeypatching time.monotonic
    # afterward would have no effect on already-bound dataclass fields.
    # The lambda defers the actual attribute lookup to construction time.
    last_touched_at: float = field(default_factory=lambda: time.monotonic())
    data: dict = field(default_factory=dict)


def _cleanup_upload(entry: WizardEntry) -> None:
    upload_path = entry.data.get("upload_path")
    if upload_path:
        try:
            Path(upload_path).unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class CompletedMarker:
    """
    A small, separate, short-lived record of "this session's wizard slot
    was just successfully handed off to a job" - added 2026-09-08 so
    wizard_expired_response() (see ../wizard_expired.py) can tell a
    genuinely-completed run apart from true TTL expiry or a
    never-started wizard, which otherwise all look identical (the entry
    is just gone) once the WizardEntry itself has been pop()'d.

    Deliberately NOT the full WizardEntry - that's already gone by
    design once ownership transfers to the job (see this module's own
    top-level docstring); this is a new, independent, much smaller
    record that outlives it. "Completed" here means "the wizard
    finished and a job now exists," not "the background job's every row
    succeeded" - job execution is async and may still be running, or
    may finish PARTIAL_SUCCESS/FAILED; that more specific outcome is
    exactly what the job detail page this marker links to already
    reports, so this marker doesn't need to duplicate it.
    """

    flow: str
    job_id: int
    completed_at: float = field(default_factory=lambda: time.monotonic())


class NetskopeCredentialCache:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, WizardEntry] = {}
        # Separate dict, separate lifecycle from _entries above - a
        # CompletedMarker is written at the exact moment an entry is
        # popped for a job, i.e. right as its WizardEntry stops existing,
        # so it can't live inside _entries.
        self._completed: dict[str, CompletedMarker] = {}
        self._lock = threading.Lock()

    def _is_expired(self, entry: WizardEntry) -> bool:
        return time.monotonic() - entry.last_touched_at >= self._ttl

    def start(self, session_key: str, tenant: str, token: str, flow: str) -> WizardEntry:
        """
        `flow` is required (see WizardEntry.flow). Raises
        FlowCollisionError instead of silently overwriting when a
        DIFFERENT, still-active flow already occupies this session's
        slot - callers should catch this and show the operator a clear
        message rather than let their new tenant/token submission
        silently destroy another in-progress run's state.
        """
        with self._lock:
            old = self._entries.get(session_key)
            if old is not None and not self._is_expired(old) and old.flow != flow:
                raise FlowCollisionError(old.flow, old.tenant)
            # Starting over discards whatever the previous attempt left
            # behind, temp file included - a fresh tenant/token submission
            # for the SAME flow means the operator is beginning it again.
            if old is not None:
                _cleanup_upload(old)
            # Also drop any stale "you already completed a run" marker for
            # this session - once the operator deliberately starts a NEW
            # wizard, a callout about a DIFFERENT, earlier-finished run is
            # no longer the most relevant thing to show them if THIS new
            # one later goes stale (same reasoning as _cleanup_upload
            # above: starting over means starting over).
            self._completed.pop(session_key, None)
            entry = WizardEntry(tenant=tenant, token=token, flow=flow)
            self._entries[session_key] = entry
            return entry

    def get(self, session_key: str) -> WizardEntry | None:
        """
        Sliding TTL as of 2026-09-08: every successful read pushes the
        30-minute window forward from now, the same way login sessions
        already do (app/security/sessions.py) - a wizard actively being
        used (polling a step, re-rendering a page after a validation
        error) shouldn't expire out from under the operator just because
        more than 30 minutes have passed since they FIRST entered their
        tenant/token. Only genuine inactivity (no get() calls at all for
        30 minutes) expires it now, not elapsed wall-clock time since
        start() regardless of use.
        """
        with self._lock:
            entry = self._entries.get(session_key)
            if entry is None:
                return None
            if self._is_expired(entry):
                _cleanup_upload(entry)
                del self._entries[session_key]
                return None
            entry.last_touched_at = time.monotonic()
            return entry

    def pop(self, session_key: str) -> WizardEntry | None:
        with self._lock:
            return self._entries.pop(session_key, None)

    def discard(self, session_key: str) -> None:
        """Used on logout: removes and cleans up, without handing the
        entry to anyone."""
        entry = self.pop(session_key)
        if entry is not None:
            _cleanup_upload(entry)

    def mark_completed(self, session_key: str, flow: str, job_id: int) -> None:
        """
        Called right after pop() + create_job() succeed, at each of the 4
        confirm routes (2026-09-08) - records that this session's wizard
        slot was just successfully handed off to a job, so
        wizard_expired_response() (app/operations/wizard_expired.py) can
        show "you already completed this" instead of the generic expired
        message if the operator later navigates Back into a now-gone step
        of THIS run. See CompletedMarker's own docstring for what
        "completed" does and doesn't mean here.
        """
        with self._lock:
            self._completed[session_key] = CompletedMarker(flow=flow, job_id=job_id)

    def get_completed(self, session_key: str) -> CompletedMarker | None:
        """
        Returns the marker only while it's "recent" - reuses the same
        30-minute window as the wizard TTL itself (no separate config
        knob for what counts as "recent"). Deliberately NOT sliding,
        unlike get() above: recency counts down from the actual
        completion time, not from the last time this was checked -
        someone repeatedly revisiting a stale link shouldn't keep the
        marker alive indefinitely. Past the window this quietly stops
        answering, falling back to the generic expired message - the
        completed job itself stays in audit history permanently
        regardless; this only controls how long the SPECIAL message
        keeps pointing at it.
        """
        with self._lock:
            marker = self._completed.get(session_key)
            if marker is None:
                return None
            if time.monotonic() - marker.completed_at >= self._ttl:
                del self._completed[session_key]
                return None
            return marker


credential_cache = NetskopeCredentialCache()
