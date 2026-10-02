from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session as DBSession

from .admin.routes import router as admin_router
from .auth.dependencies import AccountDeactivated, NotAuthenticated, PasswordChangeRequired, require_login
from .auth.password_change import router as password_change_router
from .auth.registration import router as registration_router
from .auth.routes import router as auth_router
from .config import settings
from .db import get_db, init_db
from .i18n import LANDING_TRANSLATIONS, LANG_COOKIE_NAME, resolve_lang
from .jobs.routes import router as jobs_router
from .middleware import SecurityHeadersMiddleware, SessionLookupMiddleware
from .models import AuditLog, Invite, Job, JobStatus, User
from .security.admin_guard import MinimumAdminViolation
from .operations.data_export import router as data_export_router
from .operations.device_posture import router as device_posture_router
from .operations.local_group_import import router as local_group_import_router
from .operations.private_app_import import router as private_app_import_router
from .operations.rtp_creation import router as rtp_creation_router
from .operations.user_lookup import router as user_lookup_router
from .templating import templates
from .timeutil import utcnow
from .worker_guard import WorkerConfigError, ensure_single_worker

_APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    ensure_single_worker(os.environ)
    docs_urls = {} if settings.enable_api_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title=settings.app_name, lifespan=lifespan, **docs_urls)

    app.add_middleware(SessionLookupMiddleware)
    # Added last so it wraps outermost - applies to every response,
    # including redirects and error responses, not just normal 200 pages.
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")

    app.include_router(auth_router)
    app.include_router(registration_router)
    app.include_router(password_change_router)
    app.include_router(admin_router)
    app.include_router(jobs_router)
    app.include_router(private_app_import_router)
    app.include_router(rtp_creation_router)
    app.include_router(local_group_import_router)
    app.include_router(device_posture_router)
    app.include_router(data_export_router)
    app.include_router(user_lookup_router)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if isinstance(exc, AccountDeactivated):
            return RedirectResponse(url="/login?reason=deactivated", status_code=303)
        if isinstance(exc, PasswordChangeRequired):
            return RedirectResponse(url="/set-password", status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    @app.exception_handler(MinimumAdminViolation)
    async def _minimum_admin_violation(request: Request, exc: MinimumAdminViolation):
        # Belt and braces (Part F, 2026-09-09): app/admin/routes.py's own
        # pre-checks should always catch this first and return a clean
        # 400 from the route itself - this handler only fires if some
        # future write path ever reaches the ORM-level guard
        # (app/security/admin_guard.py) without checking first. 409
        # Conflict, not 400/500: the request was well-formed, it just
        # conflicts with a standing invariant.
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/")
    def home(request: Request, db: DBSession = Depends(get_db)):
        # 2026-09-16 landing-page addition: "/" now branches on session
        # presence rather than unconditionally requiring login. An
        # anonymous visitor gets the new public landing.html (marketing
        # copy + a "Go to Login" CTA - nothing operational, safe to show
        # without authentication); an authenticated one gets the exact
        # same dashboard.html as before, byte-for-byte unchanged below.
        # Deliberately checking request.state.session directly instead of
        # using `user: User = Depends(require_login)` as a parameter -
        # that dependency raises NotAuthenticated (→ a 303 to /login)
        # before this function body ever runs, which would make an
        # anonymous "/" always redirect and never reach the landing-page
        # branch. require_login is still called explicitly below, once a
        # session is confirmed to exist, so must_change_password/
        # deactivated-account handling is completely unchanged.
        if request.state.session is None:
            # 2026-09-16 landing-page language toggle: server-side
            # rendered from LANDING_TRANSLATIONS, never client-side JS
            # string-swapping. ?lang= (sent by the header <select>'s
            # onchange navigation, static/js/landing.js) wins when it's a
            # recognized value; otherwise the as4pur_lang cookie from a
            # prior visit; otherwise English. The cookie is (re)written
            # only when a valid ?lang= was actually present on THIS
            # request - a plain repeat visit with no query param neither
            # needs nor should reset it.
            query_lang = request.query_params.get("lang")
            active_lang = resolve_lang(query_lang, request.cookies.get(LANG_COOKIE_NAME))
            response = templates.TemplateResponse(
                request, "landing.html", {"t": LANDING_TRANSLATIONS[active_lang], "active_lang": active_lang}
            )
            if query_lang in LANDING_TRANSLATIONS:
                response.set_cookie(
                    LANG_COOKIE_NAME,
                    active_lang,
                    httponly=True,
                    secure=settings.secure_cookies,
                    samesite="lax",
                    max_age=60 * 60 * 24 * 365,
                    path="/",
                )
            return response
        user = require_login(request, db)

        # Four cheap COUNT queries for the dashboard's stat cards
        # (2026-09-13 UI/UX pass, corrected 2026-09-13 - the original
        # "Total operations run"/"Successful runs"/"Active users" set was
        # replaced per direct instruction; "Active users" already lives on
        # the Admin users page and isn't duplicated here). `now` uses
        # timeutil.utcnow() throughout, never datetime.now() directly -
        # see that module's own docstring on naive-vs-aware datetimes.
        now = utcnow()
        stats = {
            "in_progress_jobs": db.query(Job)
            .filter(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
            .count(),
            # Same "pending" definition as app/admin/routes.py's own
            # _invite_state() (used_at is None AND not yet expired) -
            # reused, not reinvented.
            "pending_invites": db.query(Invite)
            .filter(Invite.used_at.is_(None), Invite.expires_at > now)
            .count(),
            "failed_logins_24h": db.query(AuditLog)
            .filter(AuditLog.action == "login_failed", AuditLog.timestamp >= now - timedelta(hours=24))
            .count(),
            "jobs_this_week": db.query(Job).filter(Job.created_at >= now - timedelta(days=7)).count(),
        }
        # Per-operation "has recent activity" flag (2026-09-13 correction)
        # for the operation cards' left-border accent: at least one Job
        # row for that operation reached a TERMINAL state (SUCCESS,
        # PARTIAL_SUCCESS, or FAILED) - still-PENDING/RUNNING doesn't
        # count as "completed" yet. Scoped to exactly the job_type each
        # card's own wizard writes (app/operations/*/routes.py) - RTP
        # creation's separate "add users to an existing rule" job_type
        # (rtp_add_users_to_rule) is intentionally excluded, since that's
        # not the wizard this card launches.
        op_activity = {
            job_type: db.query(Job)
            .filter(Job.job_type == job_type, Job.status.notin_([JobStatus.PENDING, JobStatus.RUNNING]))
            .first()
            is not None
            for job_type in ("private_app_import", "rtp_creation", "local_group_import")
        }
        return templates.TemplateResponse(
            request, "dashboard.html", {"user": user, "stats": stats, "op_activity": op_activity}
        )

    @app.get("/help")
    def help_page(request: Request, user: User = Depends(require_login)):
        return templates.TemplateResponse(request, "help.html", {})

    return app


try:
    app = create_app()
except WorkerConfigError as exc:
    # A clean one-line refusal instead of a stack trace when started by uvicorn/gunicorn.
    raise SystemExit(f"AS4PUR refuses to start: {exc}") from None
