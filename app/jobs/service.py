from __future__ import annotations

from sqlalchemy.orm import Session as DBSession

from ..models import Job, JobStatus


def create_job(db: DBSession, *, job_type: str, tenant: str, created_by_username: str, input_filename: str | None = None) -> Job:
    job = Job(
        job_type=job_type,
        tenant=tenant,
        created_by_username=created_by_username,
        input_filename=input_filename,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
