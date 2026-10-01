from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# CLAUDE.md's own "never hardcode, always read live config" convention
# (Section 3) applies here too: the DB URL comes from the app's own
# Settings (app/config.py), which already reads AS4PUR_DATABASE_PATH from
# .env / the environment - never a second, separately-hardcoded URL living
# in alembic.ini. This also means pointing a migration run at a COPY of
# the real database (for the safe verify-before-touching-prod workflow) is
# just a matter of setting AS4PUR_DATABASE_PATH before invoking alembic,
# exactly like the test suite already does in tests/conftest.py - no
# alembic-specific override mechanism needed.
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401 - import registers every model on Base.metadata

config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # render_as_batch=True: SQLite can't ALTER most column properties
        # in place (add/drop a column with a constraint, change a type,
        # etc.) - batch mode has Alembic recreate the table under the hood
        # instead, which is the standard/required way to do this against
        # SQLite specifically (this project's only DB backend, CLAUDE.md
        # Section 2). Every future migration in this project runs on
        # SQLite, so this is set unconditionally here rather than gated
        # per-dialect.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
