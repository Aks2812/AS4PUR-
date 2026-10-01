from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session as DBSession

from ..auth.dependencies import require_login
from ..db import get_db
from ..models import Job, User
from ..templating import templates

router = APIRouter(prefix="/jobs")


@router.get("")
def list_jobs(request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_login)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "jobs/list.html", {"jobs": jobs})


@router.get("/{job_id}")
def job_detail(job_id: str, request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_login)):
    job = db.get(Job, job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "jobs/list.html", {"jobs": [], "error": "Job not found."}, status_code=404
        )
    return templates.TemplateResponse(request, "jobs/status.html", {"job": job})


@router.get("/{job_id}/status.json")
def job_status_json(job_id: str, db: DBSession = Depends(get_db), user: User = Depends(require_login)):
    job = db.get(Job, job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status.value,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "success_count": job.success_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "error_message": job.error_message,
    }


@router.get("/{job_id}/export.csv")
def job_export_csv(job_id: str, db: DBSession = Depends(get_db), user: User = Depends(require_login)):
    job = db.get(Job, job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["item_key", "status", "detail", "created_at"])
    for item in job.items:
        writer.writerow([item.item_key, item.status, item.detail or "", item.created_at.isoformat()])
    buffer.seek(0)

    filename = f"as4pur_job_{job.id}_report.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
