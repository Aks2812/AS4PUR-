"""
Generic file-upload handling shared by every operation's upload step
(CLAUDE.md Section 5). Not wired to any concrete operation yet - Phase 1
(Private App Import) is the first to call this.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .config import settings

_MB = 1024 * 1024

# Starlette renamed status.HTTP_413_REQUEST_ENTITY_TOO_LARGE to
# ..._CONTENT_TOO_LARGE (RFC 9110) and deprecates the old name, but
# requirements.txt allows fastapi versions on either side of that rename -
# the literal is valid on both.
_HTTP_413 = 413

# Deliberately restrictive: basename only (path components stripped by
# os.path.basename before this even runs), then collapse anything outside
# this allowlist into "_". A run of only "." and "_" characters (which is
# all a pure directory-traversal payload like "../../x" collapses to,
# since ".." survives the allowlist but "/" does not) gets stripped
# entirely by the trailing .strip("._") below, rather than left dangling
# as a leading run of dots.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(original_name: str) -> str:
    base = os.path.basename(original_name or "upload")
    cleaned = _SAFE_CHARS.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


class UploadRejected(HTTPException):
    """
    An upload save_upload() refused. Deliberately still an HTTPException: an
    operation that just lets it propagate keeps its existing JSON response,
    unchanged. A route that wants a friendly page instead catches it and
    reads `reason` ("bad_type" | "too_large" | "empty") plus the numbers
    behind it, rather than parsing the message text.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        reason: str,
        size: int | None = None,
        limit: int | None = None,
        extension: str | None = None,
    ):
        super().__init__(status_code, detail)
        self.reason = reason
        self.size = size
        self.limit = limit
        self.extension = extension


def format_size(num_bytes: int) -> str:
    """Human-readable size for operator-facing messages: "13.1 MB", "50 MB"."""
    if num_bytes >= _MB:
        value, unit = num_bytes / _MB, "MB"
    elif num_bytes >= 1024:
        value, unit = num_bytes / 1024, "KB"
    else:
        return f"{num_bytes} bytes"
    return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"


async def save_upload(
    upload: UploadFile,
    allowed_extensions: set[str],
    max_bytes: int | None = None,
    reject_empty: bool = False,
) -> Path:
    """
    Validates extension and size, streams the upload to a uniquely-named
    file under upload_temp_dir in 1MB chunks (never the whole file in
    memory), and returns its path. The caller owns deleting it via
    delete_upload() once processing is done - on every path, including
    failure, so temp files never accumulate. This function itself never
    leaves a partial file behind when it raises.

    `max_bytes` defaults to the shared upload_max_bytes; a feature with a
    legitimately different ceiling passes its own. Rejections raise
    UploadRejected (an HTTPException - see its docstring).
    """
    limit = settings.upload_max_bytes if max_bytes is None else max_bytes
    filename = sanitize_filename(upload.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in allowed_extensions:
        raise UploadRejected(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported file type '{ext or '(none)'}'. Allowed: {', '.join(sorted(allowed_extensions))}",
            reason="bad_type",
            extension=ext,
        )

    too_large_detail = f"File exceeds the {limit // _MB}MB limit."

    # By the time a route runs, Starlette has already received the whole
    # body and spooled it (to disk beyond 1MB), so its size is exact -
    # reject on it without copying an oversize file just to throw it away.
    # The chunked copy below still enforces the limit on its own, for an
    # upload object that doesn't carry a size.
    if upload.size is not None and upload.size > limit:
        raise UploadRejected(
            _HTTP_413, too_large_detail,
            reason="too_large", size=upload.size, limit=limit,
        )

    dest_dir = Path(settings.upload_temp_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{uuid.uuid4().hex}_{filename}"

    size = 0
    try:
        with open(dest_path, "wb") as out:
            while chunk := await upload.read(_MB):
                size += len(chunk)
                if size > limit:
                    # Count (don't write) the rest so the message can state
                    # the file's real size, not just "more than the limit".
                    while rest := await upload.read(_MB):
                        size += len(rest)
                    raise UploadRejected(
                        _HTTP_413, too_large_detail,
                        reason="too_large", size=size, limit=limit,
                    )
                out.write(chunk)
        if reject_empty and size == 0:
            raise UploadRejected(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.", reason="empty", size=0)
    except BaseException:
        # `out` is closed by now (the with-block has exited), so this is safe
        # on Windows too.
        dest_path.unlink(missing_ok=True)
        raise

    return dest_path


def delete_upload(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
