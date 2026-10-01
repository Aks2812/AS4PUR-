"""
Central application configuration.

Nothing tenant-specific or credential-specific lives here (CLAUDE.md
Section 3) - this only holds *application* configuration: where the
database lives, session/cookie behavior, upload limits, login rate
limiting. Tenant names and API tokens are always operator-supplied at the
point of use, never read from settings/env here.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AS4PUR_", extra="ignore")

    app_name: str = "AS4PUR"

    database_path: str = "./data/as4pur.db"

    # Idle-timeout session lifetime. Sliding: extended on every authenticated
    # request, so an active operator never gets logged out mid-task, but an
    # abandoned tab expires for real (CLAUDE.md Section 5 - "no indefinite
    # sessions").
    session_lifetime_minutes: int = 30

    # Must be True in any real deployment - Nginx terminates TLS in front of
    # this app (CLAUDE.md Section 5), and the session cookie must never be
    # sent over a plaintext channel. False is only for local development
    # over plain http://127.0.0.1, where a browser will refuse a `Secure`
    # cookie entirely.
    secure_cookies: bool = True

    login_rate_limit_attempts: int = 5
    login_rate_limit_window_minutes: int = 15

    upload_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    # nsdebug.log uploads only (Device Posture Validation) - kept separate
    # from upload_max_bytes above on purpose: real Info-level logs are
    # already ~9MB and Debug-level ones run larger, but the Excel/CSV
    # operations have no reason to accept 50MB files (openpyxl loads a
    # whole workbook into memory), so their limit is not loosened to match.
    # Deploy: nginx's client_max_body_size must stay a little above this.
    nsdebug_upload_max_bytes: int = 50 * 1024 * 1024  # 50 MB
    upload_temp_dir: str = "./data/uploads_tmp"

    # Data Export (fifth operation): rows per getusers/getgroups request.
    # 200 is the only page size ever proven against a real tenant (Operation 2
    # runs on it; a limit of 2000 once produced a non-JSON error), so the
    # ceiling is 200 - lower it to force small pages in tests or a live check.
    data_export_page_size: int = Field(default=200, ge=1, le=200)

    # FastAPI's interactive docs (/docs, /redoc) and /openapi.json list every route and need no
    # login, so they are off unless AS4PUR_ENABLE_API_DOCS=1 (development only).
    enable_api_docs: bool = False

    # Email domains an admin may invite (comma-separated, case-insensitive), e.g.
    # "example.com,example.org". Empty - the default - refuses every invite until
    # an operator sets it (fail closed). See app/admin/routes.py.
    invite_allowed_domains: str = ""

    @property
    def database_url(self) -> str:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.database_path}"


settings = Settings()
