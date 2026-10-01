"""
Business logic for Local Group/User Import (Operation 3), built against
CLAUDE.md Section 7's confirmed 9-step workflow.

File parsing (step 5) is header-name-driven, NOT the reference script's
original "Column A" positional convention (CLAUDE.md Section 9, decision
made 2026-09-06) - matching Operations 1 and 2's own parsing architecture:
`headers.index()` lookup against row 1's literal text, order-independent,
tolerant of extra columns. Required header: `Email`.

Outcome vocabulary reused, not invented, per Section 7's own instruction:
WILL_CREATE / SKIPPED_EXISTS / VALIDATION_EXCLUDED for the dry-run
(Operation 1's vocabulary), SUCCESS / SKIPPED_EXISTS / FAILED /
VALIDATION_EXCLUDED for the job's per-row outcomes.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from reference_scripts.netskope_scim_manager import validate_email

from .netskope_client import NetskopeApiError, add_user_to_group, create_group, create_user, fetch_group_by_name, fetch_user_by_email

__all__ = [
    "NetskopeApiError",
    "NormalizerError",
    "EmailRow",
    "ValidationSummary",
    "REQUIRED_COLUMNS",
    "parse_email_file",
    "mark_intra_file_duplicates",
    "validate_email_formats",
    "check_existing_users",
    "build_validation_summary",
    "resolve_group_choice",
    "run_import_job",
]

REQUIRED_COLUMNS = ["Email"]

# Section 7 step 8: "Starting hypothesis, not a decided value" - Operation
# 1 paces at 2s/call (PACE_SECONDS in private_app_import/service.py);
# Operation 3 can cost up to ~3 calls per row here (create-user, then
# add-to-group's PATCH - the group itself is resolved ONCE per job, not
# re-looked-up per row, see resolve_group_choice/run_import_job below, so
# this is already less than the "4 calls" ceiling Section 7 estimated
# assuming a naive per-row re-lookup). NOT VALIDATED AGAINST A REAL TENANT -
# do not treat this as tested just because the code runs without errors.
# Must be confirmed empirically during Phase 3's real-tenant pass, same
# "test small, confirm, then scale" discipline as every pacing constant in
# this project.
PACE_SECONDS = 5.0


class NormalizerError(Exception):
    """A fatal problem with the uploaded file itself (missing column,
    unreadable/empty workbook or CSV) - mirrors every other operation's
    own fatal-exit cases."""


@dataclass
class EmailRow:
    row_no: int
    email: str
    # WILL_CREATE | SKIPPED_EXISTS | VALIDATION_EXCLUDED (Operation 1's
    # vocabulary, reused per CLAUDE.md Section 7's explicit instruction)
    status: str = "PENDING"
    exclusion_reason: str | None = None
    # Set only when status == SKIPPED_EXISTS because the user already
    # existed in the tenant - carried through to run_import_job so it can
    # still add an already-existing user to the target group without a
    # second lookup.
    existing_user_id: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.exclusion_reason is None


@dataclass
class ValidationSummary:
    rows: list[EmailRow]
    will_create: int
    skipped_exists: int
    validation_excluded: int


def parse_email_file(path: Path, original_filename: str) -> list[EmailRow]:
    """Dispatches on the uploaded file's own extension - CSV or XLSX,
    both accepted (CLAUDE.md Section 7 step 5). Header-name-driven in
    both cases: the "Email" column is located by its literal header text
    in row 1, not by position."""
    ext = Path(original_filename).suffix.lower()
    if ext == ".csv":
        return _parse_csv(path)
    return _parse_xlsx(path)


def _missing_columns_error(headers: list) -> NormalizerError | None:
    missing = [h for h in REQUIRED_COLUMNS if h not in headers]
    if not missing:
        return None
    found = ", ".join(str(h) for h in headers if h not in (None, ""))
    return NormalizerError(f"Missing required column(s): {', '.join(missing)}. Found columns: {found}")


def _parse_csv(path: Path) -> list[EmailRow]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            try:
                headers = next(reader)
            except StopIteration:
                raise NormalizerError("The uploaded file is empty.")

            error = _missing_columns_error(headers)
            if error:
                raise error
            email_col = headers.index("Email")

            rows: list[EmailRow] = []
            for row_no, raw_row in enumerate(reader, start=2):
                value = raw_row[email_col] if email_col < len(raw_row) else None
                if value is None or not str(value).strip():
                    continue  # blank trailing row
                rows.append(EmailRow(row_no=row_no, email=str(value).strip()))
            return rows
    except NormalizerError:
        raise
    except OSError as exc:
        # WSTG-CONF path-disclosure fix (2026-09-08): str(exc) on an
        # OSError (e.g. FileNotFoundError) includes the full internal
        # server path (e.g. "...\data\uploads_tmp\<uuid>_name.csv") -
        # confirmed via a direct test against this exact function before
        # this fix. Generic message only; no safe server-side-only
        # logging path exists yet in this app (confirmed during the
        # original WSTG audit: zero logging infrastructure anywhere
        # except one traceback.print_exc() in jobs/manager.py), so the
        # real path simply isn't recorded anywhere right now rather than
        # inventing a new logging subsystem for this one case.
        raise NormalizerError("Could not read the uploaded file.") from exc


def _parse_xlsx(path: Path) -> list[EmailRow]:
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
    except OSError as exc:
        # Same path-disclosure fix as _parse_csv's own OSError branch -
        # openpyxl.load_workbook() opens the file itself, so a missing
        # file surfaces here the identical way (FileNotFoundError's
        # str() includes the full server path). Caught separately from
        # the broader except below, which handles openpyxl's own
        # corrupted-file exceptions (e.g. BadZipFile) - confirmed via a
        # direct test that THOSE messages don't include a path, so
        # they're still safe to show as-is.
        raise NormalizerError("Could not read the uploaded file.") from exc
    except Exception as exc:
        raise NormalizerError(f"Could not open the uploaded file: {exc}") from exc

    headers = [c.value for c in ws[1]]
    error = _missing_columns_error(headers)
    if error:
        raise error
    email_col = headers.index("Email")

    rows: list[EmailRow] = []
    for row_no, raw_row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        value = raw_row[email_col] if email_col < len(raw_row) else None
        if value is None or not str(value).strip():
            continue  # blank trailing row
        rows.append(EmailRow(row_no=row_no, email=str(value).strip()))
    return rows


def mark_intra_file_duplicates(rows: list[EmailRow]) -> None:
    """Two rows with the same email (case-insensitive) within the same
    uploaded file - only the first occurrence survives; later ones are
    excluded, citing the first row's number. Same VALIDATION_EXCLUDED
    treatment and reasoning as Operation 1's intra-file duplicate checks
    (CLAUDE.md Section 9) - the tenant-side already-exists check alone
    can't catch this, since the tenant doesn't have this run's user yet
    at the time both rows would be checked."""
    first_seen: dict[str, int] = {}
    for row in rows:
        key = row.email.strip().lower()
        if key not in first_seen:
            first_seen[key] = row.row_no
        else:
            row.exclusion_reason = f"email '{row.email}' is also used by row {first_seen[key]} earlier in this same file"


def validate_email_formats(rows: list[EmailRow]) -> None:
    """Reuses reference_scripts/netskope_scim_manager.py's own
    validate_email() regex - a pure function, no SSL/token-file baggage,
    same "wrap the proven logic" treatment as Operation 1 reusing
    netskope_xlsx_normalize.py's row-level normalize functions."""
    for row in rows:
        if not row.is_valid:
            continue
        if not validate_email(row.email):
            row.exclusion_reason = f"'{row.email}' is not a validly formatted email address"


def check_existing_users(rows: list[EmailRow], tenant: str, token: str) -> None:
    """Already-exists-in-tenant check per user (CLAUDE.md Section 7 step
    6) - reuses Operation 1's SKIPPED_EXISTS treatment, not a new outcome
    category, matching netskope_scim_manager.py's own create_user()
    behavior (a non-fatal "already exists" outcome). Only runs against
    rows that already passed format/dedup validation - no reason to spend
    a live lookup on something that's already going to be excluded.
    One lookup per still-valid row; SCIM has no documented bulk-existence
    endpoint to batch this against."""
    for row in rows:
        if not row.is_valid:
            continue
        existing = fetch_user_by_email(tenant, token, row.email)
        if existing:
            row.status = "SKIPPED_EXISTS"
            row.existing_user_id = existing.get("user_id")
        else:
            row.status = "WILL_CREATE"


def build_validation_summary(rows: list[EmailRow]) -> ValidationSummary:
    for row in rows:
        if not row.is_valid:
            row.status = "VALIDATION_EXCLUDED"
    return ValidationSummary(
        rows=rows,
        will_create=sum(1 for r in rows if r.status == "WILL_CREATE"),
        skipped_exists=sum(1 for r in rows if r.status == "SKIPPED_EXISTS"),
        validation_excluded=sum(1 for r in rows if r.status == "VALIDATION_EXCLUDED"),
    )


def resolve_group_choice(
    tenant: str, token: str, group_mode: str, existing_group_id: str | None, new_group_name: str | None
) -> tuple[str, str | None, str | None, str | None]:
    """
    CLAUDE.md Section 7 step 4/6. `group_mode` is "none" | "existing" |
    "new" (the operator's raw choice). Returns
    (effective_mode, group_id, group_name, note):
      - effective_mode is "none" | "existing" | "create" - what the job
        should actually do.
      - A "new" request for a name that already exists resolves to
        "existing" instead, with `note` explaining why - the same
        non-fatal "already exists" treatment create_group() itself uses
        (CLAUDE.md Section 7 step 6: "create_group already does this
        check too, same non-fatal treatment"). No duplicate group is ever
        attempted; members are added to the pre-existing group instead.
        Checked immediately here (at group-choice submission), not
        deferred to the file-upload review page - faster feedback for a
        decision the operator can act on right away (pick a different
        name, or switch to "use the existing group" explicitly) rather
        than discovering it only after uploading a whole file.
    """
    if group_mode == "none":
        return "none", None, None, None
    if group_mode == "existing":
        return "existing", existing_group_id, None, None

    # group_mode == "new"
    found = fetch_group_by_name(tenant, token, new_group_name)
    if found:
        note = (
            f"A group named \"{new_group_name}\" already exists (ID {found['group_id']}) - members will be "
            f"added to that existing group; no new group will be created."
        )
        return "existing", found["group_id"], found["group_name"], note
    return "create", None, new_group_name, None


def run_import_job(
    progress,
    tenant: str,
    token: str,
    group_mode: str,
    group_id: str | None,
    group_name: str | None,
    rows: list[EmailRow],
) -> None:
    """
    CLAUDE.md Section 7 steps 8-9: batched execution with visible
    progress, not a naive one-at-a-time loop ported from the reference
    script - PACE_SECONDS is still a real sleep between each row's calls
    (see its own docstring: an unvalidated starting hypothesis), but
    progress is written per row via `progress` (JobProgress), the same
    pattern Operations 1 and 2 already use, not silence until the whole
    batch finishes.

    Group creation/assignment happens once, up front, not per-row - see
    resolve_group_choice()'s docstring and netskope_client.add_user_to_group's
    for why re-deriving the group per row (the reference script's own
    per-call re-lookup-by-name pattern) is unnecessary waste here.
    """
    progress.set_totals(len(rows))

    effective_group_id = group_id
    if group_mode == "create":
        try:
            ok, detail, new_id = create_group(tenant, token, group_name)
        except NetskopeApiError as exc:
            ok, detail, new_id = False, str(exc), None
        if ok:
            effective_group_id = new_id
            progress.record_note("group", "SUCCESS", detail)
        else:
            progress.record_note("group", "FAILED", detail)
            progress.set_summary(
                f"Could not create group \"{group_name}\": {detail}. Users below were still processed, "
                f"without any group assignment."
            )
            effective_group_id = None
        time.sleep(PACE_SECONDS)
    elif group_mode == "existing":
        progress.record_note("group", "INFO", f"Using existing group (id={group_id}).")

    for row in rows:
        if row.status == "VALIDATION_EXCLUDED":
            progress.record_item(f"row-{row.row_no}", "VALIDATION_EXCLUDED", row.exclusion_reason)
            continue

        user_id = row.existing_user_id
        already_existed = row.status == "SKIPPED_EXISTS"

        if already_existed:
            detail = f"user '{row.email}' already exists in this tenant"
        else:
            try:
                ok, create_detail, user_id = create_user(tenant, token, row.email)
            except NetskopeApiError as exc:
                ok, create_detail, user_id = False, str(exc), None
            if not ok:
                progress.record_item(f"row-{row.row_no}", "FAILED", create_detail)
                time.sleep(PACE_SECONDS)
                continue
            detail = create_detail

        if effective_group_id and user_id:
            try:
                added_ok, added_detail = add_user_to_group(tenant, token, effective_group_id, user_id)
            except NetskopeApiError as exc:
                added_ok, added_detail = False, str(exc)
            if added_ok:
                detail = f"{detail}; added to group"
            else:
                progress.record_item(f"row-{row.row_no}", "FAILED", f"{detail}; FAILED to add to group: {added_detail}")
                time.sleep(PACE_SECONDS)
                continue

        progress.record_item(f"row-{row.row_no}", "SKIPPED_EXISTS" if already_existed else "SUCCESS", detail)
        time.sleep(PACE_SECONDS)
