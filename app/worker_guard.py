"""
Single-worker guard.

Operator tokens, in-progress wizards, running background jobs, finished Data Export files and the
login rate limiter all live in the memory of ONE process. A second worker process would not see
any of them: a download could 410 on a different worker than the one that built the file, and a
job could not be polled. uvicorn and gunicorn both read WEB_CONCURRENCY as their default worker
count, so a value other than 1 there is refused at startup rather than failing in confusing ways
later.

Under uvicorn each worker process raises this at import and uvicorn's supervisor respawns it, so
the launcher keeps running and repeats the message rather than exiting (observed, uvicorn 0.54).

This only looks at WEB_CONCURRENCY. It cannot see `--workers N` on the command line,
UVICORN_WORKERS, `gunicorn -w N`, or several separate instances (see the README).
"""
from __future__ import annotations

from collections.abc import Mapping


class WorkerConfigError(RuntimeError):
    """The process was configured to run more than one worker."""


def ensure_single_worker(environ: Mapping[str, str]) -> None:
    raw = environ.get("WEB_CONCURRENCY")
    if raw is None or not raw.strip():
        return
    try:
        workers = int(raw.strip())
    except ValueError:
        workers = None
    if workers == 1:
        return
    raise WorkerConfigError(
        f"WEB_CONCURRENCY is set to {raw!r}, but AS4PUR must run as a single process. "
        "Data Export results, operator tokens, wizard state, running jobs and the login limiter are held "
        "in this process's memory, so a second worker would not see them. "
        "Unset WEB_CONCURRENCY (or set it to 1) in the service environment and start the app again."
    )
