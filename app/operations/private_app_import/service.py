from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .netskope_client import (
    NetskopeApiError,
    PortClaims,
    create_private_app,
    existing_port_claims_and_names,
    fetch_existing_apps,
    normalize_app_name,
    parse_port_range,
    port_ranges_overlap,
)
from .normalizer_adapter import ManifestRow, NormalizerError, parse_workbook

__all__ = [
    "NetskopeApiError",
    "NormalizerError",
    "ManifestRow",
    "ReconciliationSummary",
    "build_reconciliation",
    "mark_intra_file_duplicate_names",
    "mark_intra_file_duplicate_destinations",
    "run_import_job",
    "existing_port_claims_and_names",
]

# Matches reference_scripts/netskope_bulk_import.sh's SLEEP_BETWEEN_CALLS -
# rate-limit protection between actual create POSTs. Not applied between
# rows that don't need an API call (already-excluded/already-colliding),
# since there's no call to pace there.
PACE_SECONDS = 2.0


@dataclass
class ReconciliationSummary:
    rows: list[ManifestRow]
    will_create: int
    skipped_exists: int
    validation_excluded: int
    warnings: list[str]


def _row_port_ranges(row: ManifestRow) -> list[tuple[str, tuple[int, int]]]:
    """
    This row's own (transport, port-range) pairs, in the same shape as
    existing_port_claims_and_names()'s claims index, so both sides compare
    on equal footing. No defensive try/except here the way there is on
    the untrusted API-response side - an unparseable port would already
    have excluded this row upstream in normalizer_adapter.py, so by the
    time a row reaches this function its ports are guaranteed well-formed.
    """
    return [((p.get("type") or "").lower(), parse_port_range(p["port"])) for p in row.protocols if p.get("port")]


def _format_port_range(transport: str, port_range: tuple[int, int]) -> str:
    start, end = port_range
    port_part = str(start) if start == end else f"{start}-{end}"
    return f"{transport}/{port_part}"


def _find_port_collision(row: ManifestRow, existing_claims: PortClaims) -> tuple[str, str] | None:
    """
    Returns (host, formatted_existing_port_range) for the first
    destination+port overlap found against an existing app, or None if
    this row's hosts/ports are genuinely free. CLAUDE.md Section 9 -
    port-level access segmentation: only a same-transport, overlapping-
    range match at the same host is a real collision. A disjoint or
    different-transport port set at an already-used host (e.g. an
    existing app on tcp/22 there, this row wanting tcp/443, or the same
    port range but a different transport - confirmed real pattern: tcp
    and udp both claiming "1-65535" on the same app) is the confirmed,
    legitimate port-segmentation pattern, not a collision.
    """
    row_ranges = _row_port_ranges(row)
    for host in row.hosts:
        for existing_transport, existing_range in existing_claims.get(host, []):
            for row_transport, row_range in row_ranges:
                if row_transport == existing_transport and port_ranges_overlap(row_range, existing_range):
                    return host, _format_port_range(existing_transport, existing_range)
    return None


def apply_collision_check(rows: list[ManifestRow], existing_claims: PortClaims, existing_names: set[str]) -> None:
    """
    Mutates each row's `status` in place. CLAUDE.md Section 9: reusing an
    existing NAME resolves to Netskope silently merging into the
    pre-existing app - a sibling case, unaffected by ports (a name is a
    name regardless of what ports it's scoped to). Reusing an existing
    DESTINATION only collides if this row's ports (same transport)
    actually overlap an existing app's ports there - port-level access
    segmentation is a confirmed, legitimate, wanted pattern (different
    users needing different port-scoped access to the same destination),
    so a same-destination row with disjoint or different-transport ports
    is WILL_CREATE, not a collision. Same SKIPPED_EXISTS status either way
    (Section 7: reuse the established outcome vocabulary); `collision_host`
    (+ `collision_port`) vs. `collision_name` records which one actually
    fired, for the review page's explanation text.

    `existing_names` is re-normalized defensively (idempotent if the
    caller already did it, which existing_port_claims_and_names() always
    does) rather than trusted as pre-normalized - bracket-normalization
    mismatches are exactly the class of bug CLAUDE.md Section 9 keeps
    surfacing, not worth risking on a caller convention.
    """
    existing_names = {normalize_app_name(n) for n in existing_names}
    for row in rows:
        if not row.is_valid:
            row.status = "VALIDATION_EXCLUDED"
            continue

        collision = _find_port_collision(row, existing_claims)
        if collision:
            row.status = "SKIPPED_EXISTS"
            row.collision_host, row.collision_port = collision
            continue

        if normalize_app_name(row.app_name) in existing_names:
            row.status = "SKIPPED_EXISTS"
            row.collision_name = row.app_name
            continue

        row.status = "WILL_CREATE"


def mark_intra_file_duplicate_names(rows: list[ManifestRow]) -> None:
    """
    Two rows sharing an app name WITHIN THE SAME UPLOADED FILE hit the
    exact same silent-merge behavior as a tenant-side name collision
    (CLAUDE.md Section 9) - whichever one actually POSTs first creates the
    app, and every later row sharing that name would merge into it rather
    than create a separate app, even though neither one collides with
    anything already in the tenant. The tenant-collision check alone can't
    catch this (the tenant doesn't have either app yet at the time both
    are checked), so this runs first, on the file's own rows.

    Only the first occurrence (by row order) is left alone; every later
    row sharing that name is excluded, citing the first row's number -
    matching the real mechanics (the first one to actually POST is the
    one that would genuinely create something; the rest would merge into
    it). Sets `exclusion_reason` and relies on apply_collision_check()'s
    existing `not row.is_valid` branch to turn that into
    VALIDATION_EXCLUDED - no separate status needed (CLAUDE.md Section 7:
    reuse the established outcome vocabulary).

    Rows already excluded for their own reason (bad host/port, etc.) are
    skipped entirely, in both directions: they don't count as a
    "first occurrence" worth protecting (they were never going to be
    created anyway), and they aren't re-flagged for someone else's
    duplicate name on top of their own problem.
    """
    first_occurrence_row_no: dict[str, int] = {}
    for row in rows:
        if not row.is_valid:
            continue
        key = normalize_app_name(row.app_name)
        if key not in first_occurrence_row_no:
            first_occurrence_row_no[key] = row.row_no
        else:
            row.exclusion_reason = (
                f"app name '{row.app_name}' is also used by row {first_occurrence_row_no[key]} earlier in "
                f"this same file - only that first occurrence can be created; this row would silently merge "
                f"into it rather than create a separate app"
            )


def mark_intra_file_duplicate_destinations(rows: list[ManifestRow]) -> None:
    """
    Two rows sharing an overlapping destination+port WITHIN THE SAME
    UPLOADED FILE hit the exact same silent-merge behavior as a
    tenant-side destination collision (CLAUDE.md Section 9) - whichever
    one actually POSTs first creates the app, and any later row whose
    ports overlap it at that host would merge into it rather than create
    a separate app, even though neither collides with anything already in
    the tenant. The tenant-side check alone can't catch this (the tenant
    has neither app yet when both are checked).

    Deliberately port-aware, reusing the exact same overlap logic as the
    tenant-side check (port_ranges_overlap + same-transport comparison) -
    CLAUDE.md Section 9's port-level access segmentation is a confirmed,
    legitimate, wanted pattern, and one file is exactly how an operator
    would actually author it in practice (e.g. one row for `[IP-SSH]`
    tcp/22, another row for `[IP-LDAP]` tcp/389, same destination, same
    upload). Treating any shared destination as a duplicate regardless of
    ports would silently reintroduce the exact bug the port-aware fix
    exists to prevent - just one step earlier in the pipeline.

    Only the first occurrence of a given destination+port (by row order)
    is left alone; a later row is excluded only if its ports genuinely
    overlap (same transport, overlapping range) an earlier valid row's
    ports at a shared host - citing that row's number, same reasoning and
    same VALIDATION_EXCLUDED treatment as mark_intra_file_duplicate_names().
    An excluded row's own ports are never registered as claims for later
    rows to collide against - it was never going to be created either, so
    it isn't a real "first occurrence" to protect.
    """
    claimed: dict[str, list[tuple[str, tuple[int, int], int]]] = {}
    for row in rows:
        if not row.is_valid:
            continue

        row_ranges = _row_port_ranges(row)
        collision: tuple[str, str, tuple[int, int], int] | None = None
        for host in row.hosts:
            for existing_transport, existing_range, first_row_no in claimed.get(host, []):
                if collision:
                    break
                for row_transport, row_range in row_ranges:
                    if row_transport == existing_transport and port_ranges_overlap(row_range, existing_range):
                        collision = (host, existing_transport, existing_range, first_row_no)
                        break
            if collision:
                break

        if collision:
            host, transport, port_range, first_row_no = collision
            row.exclusion_reason = (
                f"destination '{host}' port {_format_port_range(transport, port_range)} is also used by row "
                f"{first_row_no} earlier in this same file - only that first occurrence can be created; this "
                f"row would silently merge into it rather than create a separate app"
            )
        else:
            for host in row.hosts:
                claimed.setdefault(host, []).extend((transport, port_range, row.row_no) for transport, port_range in row_ranges)


def build_reconciliation(xlsx_path: Path, existing_claims: PortClaims, existing_names: set[str]) -> ReconciliationSummary:
    rows = parse_workbook(xlsx_path)
    mark_intra_file_duplicate_names(rows)
    mark_intra_file_duplicate_destinations(rows)
    apply_collision_check(rows, existing_claims, existing_names)
    warnings = [w for row in rows for w in row.warnings]
    return ReconciliationSummary(
        rows=rows,
        will_create=sum(1 for r in rows if r.status == "WILL_CREATE"),
        skipped_exists=sum(1 for r in rows if r.status == "SKIPPED_EXISTS"),
        validation_excluded=sum(1 for r in rows if r.status == "VALIDATION_EXCLUDED"),
        warnings=warnings,
    )


def _verify_created_rows(progress, tenant: str, token: str, created_rows: list[ManifestRow]) -> None:
    """Independent post-creation verification (reference_scripts/
    netskope_verify_success.py's logic): confirms every row this run
    reported SUCCESS actually exists in the tenant under its own name -
    catches the "HTTP success but silently absorbed into a pre-existing
    app under a different name" failure mode. Findings are recorded as
    supplementary notes (record_note), not new record_item outcomes -
    each row already has its primary SUCCESS/FAILED outcome recorded;
    this only adds detail, it never changes the tally."""
    try:
        tenant_apps = fetch_existing_apps(tenant, token)
    except NetskopeApiError as exc:
        progress.set_summary(
            f"Independent post-creation verification could not run: {exc}. "
            f"SUCCESS rows above reflect the API's own response only."
        )
        return

    host_to_names: dict[str, list[str]] = {}
    for app in tenant_apps:
        for h in app["hosts"]:
            host_to_names.setdefault(h, []).append(app["app_name"])

    suspect = not_found = 0
    for row in created_rows:
        expected = normalize_app_name(row.app_name)
        if any(normalize_app_name(a["app_name"]) == expected for a in tenant_apps):
            continue  # CONFIRMED - exists under its own name, nothing to flag

        colliding_names = {n for h in row.hosts for n in host_to_names.get(h, [])}
        if colliding_names:
            progress.record_note(
                f"row-{row.row_no}",
                "SUSPECT_COLLISION",
                f"Created successfully, but its destination is now found under a different app name "
                f"({', '.join(sorted(colliding_names))}) - likely silently absorbed into an existing app. Verify manually.",
            )
            suspect += 1
        else:
            progress.record_note(
                f"row-{row.row_no}",
                "NOT_FOUND",
                "Created successfully (per the API's own response), but could not be found in the tenant "
                "afterward under any name. Needs manual investigation.",
            )
            not_found += 1

    if suspect or not_found:
        progress.set_summary(
            f"Independent verification flagged {suspect} SUSPECT_COLLISION and {not_found} NOT_FOUND "
            f"row(s) among this run's successful creates - see the flagged rows above."
        )


def run_import_job(progress, tenant: str, token: str, publisher_ids: list[int], rows: list[ManifestRow]) -> None:
    progress.set_totals(len(rows))

    # Re-check collisions (destination+port AND name - CLAUDE.md Section
    # 9's sibling cases) against the tenant's CURRENT state, not the
    # snapshot the review page was built from - real time passed between
    # the dry-run and this confirm, and this behavior is exactly the kind
    # of thing that must never go stale.
    try:
        existing_claims, existing_names = existing_port_claims_and_names(tenant, token)
    except NetskopeApiError as exc:
        for row in rows:
            if row.status == "VALIDATION_EXCLUDED":
                progress.record_item(f"row-{row.row_no}", "VALIDATION_EXCLUDED", row.exclusion_reason)
            else:
                progress.record_item(
                    f"row-{row.row_no}", "FAILED", f"Could not re-verify existing destinations/names before import: {exc}"
                )
        raise

    created_rows: list[ManifestRow] = []

    for row in rows:
        if row.status == "VALIDATION_EXCLUDED":
            progress.record_item(f"row-{row.row_no}", "VALIDATION_EXCLUDED", row.exclusion_reason)
            continue

        collision = _find_port_collision(row, existing_claims)
        if collision:
            collision_host, collision_port = collision
            progress.record_item(
                f"row-{row.row_no}", "SKIPPED_EXISTS",
                f"destination '{collision_host}' port {collision_port} already exists on another app",
            )
            continue

        if normalize_app_name(row.app_name) in existing_names:
            progress.record_item(
                f"row-{row.row_no}",
                "SKIPPED_EXISTS",
                f"app name '{row.app_name}' already exists in this tenant - creating it would silently merge "
                f"this row's destination into that existing app rather than create a new one, so it's skipped instead",
            )
            continue

        payload = row.build_payload(publisher_ids)
        try:
            ok, detail = create_private_app(tenant, token, payload)
        except NetskopeApiError as exc:
            ok, detail = False, str(exc)

        if ok:
            progress.record_item(f"row-{row.row_no}", "SUCCESS", detail)
            created_rows.append(row)
        else:
            progress.record_item(f"row-{row.row_no}", "FAILED", detail)

        time.sleep(PACE_SECONDS)

    if created_rows:
        _verify_created_rows(progress, tenant, token, created_rows)
