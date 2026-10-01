"""
Runs a job's work function on a background thread inside THIS process -
deliberately not a separate worker process or external queue. This is what
makes the CLAUDE.md Section 3 token-handling design work: an operation's
Netskope API token is passed straight into the thread's target function as
a plain argument and never serialized anywhere - no queue payload, no DB
row, no IPC of any kind. See CLAUDE.md Section 3 and Section 11, and
deploy/README_DEPLOY.md for the operational consequence of this choice
(never run this app with more than one worker process).

Trade-off, accepted deliberately: if this process restarts mid-job, the
job dies where it stands and must be re-run with a freshly-entered token.
That's no worse than the CLI version's own resumable-by-rerun behavior
(state.csv), and it's what "the token is gone on session end" (Section 3)
actually means in the background-job case.
"""
from __future__ import annotations

import threading
import traceback
from typing import Callable

from ..db import SessionLocal
from ..models import Job, JobItem, JobStatus
from ..timeutil import utcnow


class JobProgress:
    """
    Handed to a running job's target function so it can report progress
    without holding one long-lived DB session open across an entire batch.
    Each call opens its own short session, writes, and closes - safe to
    use from a background thread while the request-handling thread (and
    SQLite's WAL mode) does its own thing concurrently.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def set_totals(self, total_items: int) -> None:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                job.total_items = total_items
                db.commit()

    def record_item(self, item_key: str, status: str, detail: str | None = None) -> None:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            db.add(JobItem(job_id=self.job_id, item_key=item_key, status=status, detail=detail))
            job.processed_items += 1
            if status == "SUCCESS":
                job.success_count += 1
            elif status in ("SKIPPED_EXISTS", "VALIDATION_EXCLUDED"):
                job.skipped_count += 1
            elif status == "FAILED":
                job.failed_count += 1
            db.commit()

    def record_note(self, item_key: str, status: str, detail: str | None = None) -> None:
        """
        Adds a supplementary JobItem that does NOT count toward
        total_items/processed_items or any of the success/skipped/failed
        tallies - for a finding discovered about a row *after* its primary
        outcome was already recorded via record_item (e.g. Private App
        Import's independent post-creation verification pass flagging a
        SUSPECT_COLLISION on a row already marked SUCCESS). Using
        record_item for this would double-count that row against totals
        set by set_totals(); this exists so operations can add that kind
        of supplementary detail without skewing the progress bar or the
        job's headline counts.
        """
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            db.add(JobItem(job_id=self.job_id, item_key=item_key, status=status, detail=detail))
            db.commit()

    def set_summary(self, text: str) -> None:
        """Sets Job.summary - a job-level note (not tied to one row), e.g.
        'independent verification could not run: <reason>'."""
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                job.summary = text
                db.commit()


class JobManager:
    def start(self, job_id: str, target: Callable[..., None], *args, **kwargs) -> None:
        thread = threading.Thread(
            target=self._run, args=(job_id, target, args, kwargs), daemon=True, name=f"job-{job_id}"
        )
        thread.start()

    def _run(self, job_id: str, target: Callable[..., None], args: tuple, kwargs: dict) -> None:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            job.status = JobStatus.RUNNING
            job.started_at = utcnow()
            db.commit()

        progress = JobProgress(job_id)
        try:
            target(progress, *args, **kwargs)
        except Exception as exc:
            # Deliberately generic: a target function is responsible for
            # never raising the token itself inside an exception message.
            # This handler only ever records the exception's type/text.
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.finished_at = utcnow()
                    job.error_message = f"{type(exc).__name__}: {exc}"
                    db.commit()
            traceback.print_exc()
            return

        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is not None:
                if job.failed_count == 0:
                    job.status = JobStatus.SUCCESS
                elif job.success_count > 0 or job.skipped_count > 0:
                    job.status = JobStatus.PARTIAL_SUCCESS
                else:
                    job.status = JobStatus.FAILED
                job.finished_at = utcnow()
                db.commit()


job_manager = JobManager()
