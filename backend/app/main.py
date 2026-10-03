"""HTTP API. Everything slow happens in the worker; every endpoint here returns in milliseconds."""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update as sql_update

from .config import ALLOWED_EXTENSIONS, BATCH_LANGUAGES, LANGUAGES, get_settings
from .db import Chunk, Event, Job, Recording, init_db, session_scope
from .storage import LocalStorage, get_storage
from .worker import Worker, enqueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()
_worker: Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker
    init_db()
    get_storage()
    if settings.run_worker_in_process:
        _worker = Worker()
        _worker.start()
    yield
    if _worker:
        _worker.shutdown()


app = FastAPI(title="Audio Notes API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- auth-lite
def owner(x_client_id: str = Header(..., alias="X-Client-Id")) -> str:
    """Each browser gets a random id (kept in localStorage). It scopes the history list.
    This is not authentication; see the architecture page."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", x_client_id):
        raise HTTPException(400, "Invalid client id")
    return x_client_id


def get_owned(s, rid: uuid.UUID, owner_key: str) -> Recording:
    rec = s.get(Recording, rid)
    if rec is None or rec.owner_key != owner_key:
        raise HTTPException(404, "Recording not found")
    return rec


# --------------------------------------------------------------------------- schemas
class CreateRecording(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    content_type: str | None = None
    language: str = "en-IN"
    mode: Literal["auto", "chunked", "batch"] = "auto"


class UploadFailed(BaseModel):
    message: str = Field(default="Upload failed", max_length=500)


def summary_view(r: Recording) -> dict:
    return {
        "id": str(r.id),
        "filename": r.filename,
        "size_bytes": r.size_bytes,
        "language": r.language,
        "mode": r.mode,
        "status": r.status,
        "stage": r.stage,
        "stage_detail": r.stage_detail,
        "progress": round(r.progress or 0, 1),
        "duration_s": r.duration_s,
        "engine": r.engine,
        "chunks_total": r.chunks_total,
        "chunks_done": r.chunks_done,
        "chunks_failed": r.chunks_failed,
        "summary_status": r.summary_status,
        "error_code": r.error_code,
        "error_message": r.error_message,
        "retryable": r.retryable,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "preview": (r.summary or r.transcript or "")[:220] or None,
    }


# --------------------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    with session_scope() as s:
        s.execute(text("select 1"))
    return {"ok": True}


@app.get("/api/config")
def public_config():
    return {
        "max_upload_mb": settings.max_upload_mb,
        "max_duration_min": settings.max_duration_min,
        "languages": [{"code": k, "name": v, "batch": k in BATCH_LANGUAGES} for k, v in LANGUAGES.items()],
        "batch_available": settings.batch_available,
        "extensions": sorted(ALLOWED_EXTENSIONS),
        "llm_models": settings.groq_model_list,
        "worker_mode": "in-process" if settings.run_worker_in_process else "separate",
    }


@app.post("/api/recordings", status_code=201)
def create_recording(body: CreateRecording, owner_key: str = Depends(owner)):
    ext = Path(body.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, f"Unsupported file type '{ext or 'none'}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if body.size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File is larger than the {settings.max_upload_mb} MB limit.")
    if body.language not in LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    rid = uuid.uuid4()
    key = f"{owner_key}/{rid}/source{ext}"
    target = get_storage().create_upload_target(key, body.content_type or "application/octet-stream")
    with session_scope() as s:
        rec = Recording(id=rid, owner_key=owner_key, filename=body.filename[:255], content_type=body.content_type,
                        size_bytes=body.size_bytes, storage_key=key, language=body.language, mode=body.mode,
                        status="uploading", stage="upload", stage_detail="Uploading to storage")
        s.add(rec)
        s.flush()
        s.add(Event(recording_id=rid, message=f"Upload started: {body.filename} ({body.size_bytes / 1e6:.1f} MB)"))
    return {"recording": {"id": str(rid)}, "upload": target}


@app.post("/api/recordings/{rid}/uploaded")
def mark_uploaded(rid: uuid.UUID, owner_key: str = Depends(owner)):
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        if rec.status != "uploading":
            return summary_view(rec)
        key, expected = rec.storage_key, rec.size_bytes
    size = get_storage().object_size(key)
    if size is None:
        raise HTTPException(409, "The file has not arrived in storage. Please upload it again.")
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        if size and size != expected:
            rec.status, rec.error_code, rec.retryable = "failed", "UPLOAD_INCOMPLETE", False
            rec.error_message = f"Only {size} of {expected} bytes arrived. Please upload the file again."
            s.add(Event(recording_id=rid, level="error", message=rec.error_message))
            return summary_view(rec)
        rec.status, rec.stage, rec.progress, rec.stage_detail = "queued", "validating", 1.0, "Waiting for a worker"
        s.add(Event(recording_id=rid, message="Upload complete; queued for processing"))
        view = summary_view(rec)
    enqueue(rid)
    return view


@app.post("/api/recordings/{rid}/upload-failed")
def mark_upload_failed(rid: uuid.UUID, body: UploadFailed, owner_key: str = Depends(owner)):
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        if rec.status == "uploading":
            rec.status, rec.error_code, rec.retryable = "failed", "UPLOAD_FAILED", False
            rec.error_message = f"Upload failed: {body.message}"
            s.add(Event(recording_id=rid, level="error", message=rec.error_message))
        return summary_view(rec)


@app.get("/api/recordings")
def list_recordings(owner_key: str = Depends(owner), limit: int = Query(50, le=200)):
    with session_scope() as s:
        rows = s.scalars(
            select(Recording).where(Recording.owner_key == owner_key).order_by(Recording.created_at.desc()).limit(limit)
        ).all()
        return {"items": [summary_view(r) for r in rows]}


@app.get("/api/recordings/{rid}")
def get_recording(rid: uuid.UUID, owner_key: str = Depends(owner)):
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        chunks = s.scalars(select(Chunk).where(Chunk.recording_id == rid).order_by(Chunk.idx)).all()
        events = s.scalars(select(Event).where(Event.recording_id == rid).order_by(Event.id.desc()).limit(60)).all()
        view = summary_view(rec)
        view.update(
            transcript=rec.transcript,
            summary=rec.summary,
            summary_error=rec.summary_error,
            summary_model=rec.summary_model,
            segments=[
                {"idx": c.idx, "start": c.start_s, "end": c.end_s, "status": c.status, "text": c.text,
                 "attempts": c.attempts, "error": c.error}
                for c in chunks
            ],
            events=[{"level": e.level, "message": e.message, "at": e.created_at.isoformat()} for e in reversed(events)],
        )
        key, has_audio = rec.storage_key, rec.status != "uploading"
    view["audio_url"] = get_storage().signed_download_url(key) if has_audio else None
    return view


@app.post("/api/recordings/{rid}/retry")
def retry(rid: uuid.UUID, owner_key: str = Depends(owner)):
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        if rec.status == "failed":
            if rec.retryable is False:
                raise HTTPException(409, "This failure cannot be fixed by retrying; please upload the file again.")
            rec.status, rec.error_code, rec.error_message, rec.stage_detail = "queued", None, None, "Waiting for a worker"
            s.add(Event(recording_id=rid, message="Retry requested"))
            kind = "process"
        elif rec.status == "completed" and rec.summary_status == "failed":
            rec.summary_status, rec.summary_error = "pending", None
            s.add(Event(recording_id=rid, message="Summary retry requested"))
            kind = "summarize"
        else:
            raise HTTPException(409, "Nothing to retry")
    enqueue(rid, kind)
    return {"ok": True}


@app.delete("/api/recordings/{rid}", status_code=204)
def delete_recording(rid: uuid.UUID, owner_key: str = Depends(owner)):
    with session_scope() as s:
        rec = get_owned(s, rid, owner_key)
        prefix = rec.storage_key.rsplit("/", 1)[0]
        s.execute(delete(Recording).where(Recording.id == rid))
    try:
        get_storage().delete_prefix(prefix)
    except Exception:
        logging.getLogger("api").warning("could not delete storage for %s", rid)


@app.get("/api/queue")
def queue_stats():
    """Small operational view used on the architecture page."""
    with session_scope() as s:
        rows = s.execute(select(Job.status, text("count(*)")).group_by(Job.status)).all()
    return {status: n for status, n in rows}


# --------------------------------------------------------------------------- local storage routes (dev only)
@app.put("/api/local-upload/{key:path}")
async def local_upload(key: str, request: Request, expires: int, token: str):
    st = get_storage()
    if not isinstance(st, LocalStorage) or not st.verify(key, "put", expires, token):
        raise HTTPException(403, "Invalid or expired upload URL")
    path = st.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        async for block in request.stream():
            f.write(block)
    return {"ok": True}


@app.get("/api/local-files/{key:path}")
def local_file(key: str, expires: int, token: str):
    st = get_storage()
    if not isinstance(st, LocalStorage) or not st.verify(key, "get", expires, token):
        raise HTTPException(403, "Invalid or expired link")
    return FileResponse(st.path_for(key))
