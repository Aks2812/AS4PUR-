#!/bin/sh
# Container start: apply migrations, then run the single uvicorn process.
set -eu

# README "Database": migrate BEFORE the first start, or init_db() creates the
# tables without migration history and a later upgrade fails. Safe on every
# start: at head it does nothing.
python -m alembic upgrade head

# One worker, never --workers / --reload (README, "Run").
# The port is reachable only on the compose network, from the nginx container,
# whose address is not fixed - so forwarded headers are trusted from any peer.
# Do not publish port 8000 on the host. (Read by uvicorn from the environment.)
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
