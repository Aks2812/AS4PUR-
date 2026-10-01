from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """
    WAL mode lets the request-handling thread and a running background
    job's thread read/write concurrently without locking each other out;
    busy_timeout makes a transient lock retry briefly instead of raising
    immediately. foreign_keys is off by default in SQLite unless set per
    connection.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def get_db():
    """FastAPI dependency: one DB session per request, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    """
    First-ever setup ONLY: creates every table via Base.metadata.create_all()
    when the target database is genuinely empty - no tables of any kind,
    including no alembic_version. Once ANY schema already exists, Alembic
    (`alembic upgrade head`) is the only thing that may change it from here
    on - this function stops touching schema entirely and just checks
    whether the live schema matches the latest known migration, logging a
    clear warning rather than silently papering over a mismatch.

    `bind` defaults to this module's own `engine` (every real call site -
    app/main.py's lifespan, tests/conftest.py's per-test reset - calls this
    with no arguments) - the parameter exists so a test can point this at
    an isolated throwaway engine instead, without touching the process-wide
    `engine`/`SessionLocal` singletons.

    Why this changed (2026-09-09, confirmed the hard way - see CLAUDE.md's
    Migrations section): this function used to call create_all()
    unconditionally on every process startup. create_all() reads directly
    off Base.metadata (i.e. whatever app/models.py currently defines) and
    creates anything missing - it has no concept of Alembic's own
    bookkeeping. That silently created the `invites` table (added to
    models.py for Part C of the RBAC build) on the real data/as4pur.db the
    moment the app was started for a live walkthrough - WITHOUT ever
    recording that in alembic_version, which stayed on Part B's revision.
    The result: alembic_version claimed a schema state that no longer
    matched reality, and the first real `alembic upgrade head` afterward
    crashed outright with "table invites already exists" - reproduced
    directly against a scratch copy before this fix. This function must
    never again create a table that Alembic doesn't already know about.
    """
    from . import models  # noqa: F401 - import registers models on Base.metadata

    target = bind if bind is not None else engine
    existing_tables = set(inspect(target).get_table_names())

    if not existing_tables:
        # Genuinely first-ever setup - nothing here at all, not even
        # alembic_version - so there is no Alembic bookkeeping to
        # contradict yet. A real deployment should still run
        # `alembic stamp head` right after this (deploy/README_DEPLOY.md)
        # so the NEXT migration applies cleanly; this function only
        # decides whether create_all() may run, it doesn't stamp anything
        # itself.
        Base.metadata.create_all(bind=target)
        return

    _warn_if_schema_behind_migrations(target)


def _warn_if_schema_behind_migrations(bind) -> None:
    """
    Read-only health check: compares the database's own recorded Alembic
    revision(s) against the latest migration file(s) on disk, and logs a
    warning on any mismatch - including "no alembic_version at all despite
    existing tables" (a schema that predates Alembic and has never been
    stamped). Never modifies schema or alembic_version itself - the fix for
    a real mismatch is a deliberate `alembic upgrade`/`alembic stamp`
    (CLAUDE.md's explicit-revision-only discipline), never something this
    function does on its own during a plain app startup.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    head_revisions = set(script.get_heads())

    with bind.connect() as connection:
        current_revisions = set(MigrationContext.configure(connection).get_current_heads())

    if current_revisions != head_revisions:
        logger.warning(
            "Database schema may be out of date: alembic_version reports %s, "
            "but the latest migration(s) on disk are %s. Verify against a "
            "scratch copy, then run 'alembic upgrade <explicit revision>' to "
            "bring it up to date - or 'alembic stamp <explicit revision>' if "
            "the schema already matches by other means (e.g. a prior "
            "create_all() run). Never assume this warning means create_all() "
            "should run again - it won't, and it must not.",
            current_revisions or "<none - schema predates Alembic>",
            head_revisions,
        )
