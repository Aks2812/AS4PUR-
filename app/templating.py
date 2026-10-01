from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import settings

_APP_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
templates.env.globals["app_name"] = settings.app_name

# Maps a Job row's internal `job_type` (app/operations/*/routes.py's own
# job_type=... string, e.g. "private_app_import") to the corrected,
# user-facing operation name (2026-09-13 UI/UX pass, CLAUDE.md's
# "Private app definition" / "User provision" / "RTP creation" rename).
# Deliberately a template filter, not a rename of the stored value itself
# - job_type is written into real Job rows already in data/as4pur.db, and
# is also matched on elsewhere (job export, filtering); changing what's
# actually stored is a data-migration question, not a display one.
_JOB_TYPE_LABELS = {
    "private_app_import": "Private app definition",
    "rtp_creation": "RTP creation",
    "rtp_add_users_to_rule": "RTP creation (add users to existing rule)",
    "local_group_import": "User provision",
}


def _job_type_label(job_type: str) -> str:
    return _JOB_TYPE_LABELS.get(job_type, job_type)


templates.env.filters["job_type_label"] = _job_type_label
