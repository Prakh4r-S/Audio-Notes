"""Local stand-in for Gnani (REST + Batch), Groq and Supabase Storage, used for testing.

Behaviour is switchable at runtime via POST /_control, e.g.
  {"rest_fail_rate": 0.3, "rest_fail_status": 503}   flaky ASR
  {"rest_fail_rate": 1.0, "rest_fail_status": 503}   ASR down -> forces Batch fallback
  {"groq_bad_models": ["llama-3.3-70b-versatile"]}   model decommissioned -> next model
  {"groq_down": true}                                summary failure path
Run: uvicorn tests.mock_providers:app --port 9100
"""

from __future__ import annotations

import io
import random
import threading
import time
import uuid
import wave

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONTROL: dict = {
    "rest_fail_rate": 0.0, "rest_fail_status": 503, "rest_latency": 0.3,
    "batch_seconds": 6.0, "batch_fail": False,
    "groq_bad_models": [], "groq_down": False,
}
STATS = {"rest_calls": 0, "rest_failures": 0, "batch_jobs": 0, "groq_calls": 0}
OBJECTS: dict[str, bytes] = {}
JOBS: dict[str, dict] = {}
lock = threading.Lock()

WORDS = ("the team reviewed the quarterly plan and agreed to ship the new onboarding flow by friday "
         "priya will draft the announcement while rahul checks the analytics numbers for the pilot cities").split()


@app.post("/_control")
async def control(req: Request):
    CONTROL.update(await req.json())
    return {"control": CONTROL, "stats": STATS}


@app.get("/_stats")
def stats():
    return STATS


# ----------------------------------------------------------------------------- Gnani REST
@app.post("/stt/v3")
async def stt(audio_file: UploadFile = File(...), language_code: str = Form(...), format: str = Form("verbatim"),
              request: Request = None):
    if request.headers.get("X-API-Key-ID") != "test-gnani":
        return JSONResponse({"success": False, "error": {"type": "FORBIDDEN", "message": "bad key"}}, 403)
    data = await audio_file.read()
    with lock:
        STATS["rest_calls"] += 1
    time.sleep(CONTROL["rest_latency"])
    try:
        with wave.open(io.BytesIO(data)) as w:
            dur = w.getnframes() / w.getframerate()
    except Exception:
        return JSONResponse({"success": False, "error": {"type": "INVALID_REQUEST_ERROR", "message": "bad audio"}}, 400)
    if dur > 60:
        return JSONResponse({"success": False, "error": {"type": "INVALID_REQUEST_ERROR",
                             "message": "Audio duration exceeds maximum limit of 60 seconds"}}, 400)
    if random.random() < CONTROL["rest_fail_rate"]:
        with lock:
            STATS["rest_failures"] += 1
        s = CONTROL["rest_fail_status"]
        return JSONResponse({"success": False, "error": {"type": "SERVICE_UNAVAILABLE", "message": "temporarily down"}}, s)
    n = max(1, int(dur * 2.2))
    start = random.randint(0, len(WORDS) - 1)
    text = " ".join(WORDS[(start + i) % len(WORDS)] for i in range(n))
    return {"success": True, "request_id": "req_" + uuid.uuid4().hex[:8], "timestamp": "x", "transcript": text}


# ----------------------------------------------------------------------------- Gnani Batch
@app.post("/stt/v3/batch/jobs", status_code=201)
async def batch_create(req: Request):
    body = await req.json()
    jid = str(uuid.uuid4())
    JOBS[jid] = {"status": "CREATED", "paths": body["source"]["paths"], "lang": body["config"]["language_code"]}
    STATS["batch_jobs"] += 1
    return {"job_id": jid, "status": "CREATED", "total_files_accepted": None}


@app.post("/stt/v3/batch/jobs/{jid}/start", status_code=202)
def batch_start(jid: str):
    job = JOBS[jid]
    job.update(status="STARTING", started=time.time())
    import httpx
    try:  # Fetch the audio like Gnani would.
        r = httpx.get(job["paths"][0], timeout=30)
        r.raise_for_status()
        job["bytes"] = len(r.content)
    except Exception as e:
        job.update(status="START_FAILED", reason=f"All provided paths were invalid ({e.__class__.__name__})")
    return {"job_id": jid, "status": job["status"]}


@app.get("/stt/v3/batch/jobs/{jid}")
def batch_status(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] not in ("START_FAILED", "COMPLETED", "FAILED"):
        el = time.time() - job["started"]
        if el > CONTROL["batch_seconds"]:
            job["status"] = "FAILED" if CONTROL["batch_fail"] else "COMPLETED"
        elif el > 2:
            job["status"] = "IN_PROGRESS"
        elif el > 1:
            job["status"] = "QUEUED"
    return {"job_id": jid, "status": job["status"], "cancel_reason": job.get("reason"),
            "progress": {"total_files": 1, "completed_files": int(job["status"] == "COMPLETED")}}


@app.get("/stt/v3/batch/jobs/{jid}/files")
def batch_files(jid: str, request: Request):
    job = JOBS[jid]
    ok = job["status"] == "COMPLETED"
    base = str(request.base_url).rstrip("/")
    return {"job_id": jid, "data": [{
        "file_id": "f1", "status": "COMPLETED" if ok else "FAILED",
        "transcript_url": f"{base}/_transcripts/{jid}.json" if ok else None,
        "error_message": None if ok else "Empty transcript after 3 retries",
    }], "pagination": {"has_more": False}}


@app.post("/stt/v3/batch/jobs/{jid}/cancel")
def batch_cancel(jid: str):
    JOBS[jid]["status"] = "CANCELLED"
    return {"ok": True}


@app.get("/_transcripts/{jid}.json")
def transcript(jid: str):
    segs, t = [], 0.0
    for i in range(12):
        words = " ".join(WORDS[(i * 7 + k) % len(WORDS)] for k in range(10))
        segs.append({"segment_id": i, "start_time": t, "end_time": t + 4.5, "text": words})
        t += 5
    return {"full_transcript": " ".join(s["text"] for s in segs), "segments": segs, "language_code": JOBS[jid]["lang"]}


# ----------------------------------------------------------------------------- Groq
@app.post("/openai/v1/chat/completions")
async def groq(req: Request):
    if req.headers.get("authorization") != "Bearer test-groq":
        return JSONResponse({"error": {"message": "Invalid API Key"}}, 401)
    body = await req.json()
    STATS["groq_calls"] += 1
    if CONTROL["groq_down"]:
        return JSONResponse({"error": {"message": "over capacity"}}, 503)
    if body["model"] in CONTROL["groq_bad_models"]:
        return JSONResponse({"error": {"message": f"The model `{body['model']}` has been decommissioned"}}, 400)
    user = body["messages"][-1]["content"]
    if "section" in user.lower() and "notes" in user.lower() and "Summarise the whole" not in user:
        content = "- notes for a section\n- priya drafts the announcement"
    else:
        content = ("## Overview\nThe team reviewed the quarterly plan and agreed to ship the onboarding flow.\n\n"
                   "## Key points\n- Onboarding flow ships Friday\n- Analytics for pilot cities under review\n\n"
                   f"## Action items & decisions\n- Priya: draft announcement\n- Rahul: check numbers\n\n"
                   f"<!-- mock model {body['model']} -->")
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "model": body["model"]}


# ----------------------------------------------------------------------------- Supabase Storage
def _key(bucket: str, path: str) -> str:
    return f"{bucket}/{path}"


@app.post("/storage/v1/object/upload/sign/{bucket}/{path:path}")
def sign_upload(bucket: str, path: str, request: Request):
    if not request.headers.get("authorization", "").startswith("Bearer test-supa"):
        raise HTTPException(401)
    return {"url": f"/object/upload/sign/{bucket}/{path}?token=tok"}


@app.put("/storage/v1/object/upload/sign/{bucket}/{path:path}")
async def put_signed(bucket: str, path: str, token: str, request: Request):
    OBJECTS[_key(bucket, path)] = await request.body()
    return {"Key": _key(bucket, path)}


@app.get("/storage/v1/object/info/{bucket}/{path:path}")
def info(bucket: str, path: str):
    data = OBJECTS.get(_key(bucket, path))
    if data is None:
        return JSONResponse({"error": "not_found"}, 400)
    return {"size": len(data)}


@app.post("/storage/v1/object/sign/{bucket}/{path:path}")
def sign_get(bucket: str, path: str):
    return {"signedURL": f"/object/sign/{bucket}/{path}?token=tok"}


@app.get("/storage/v1/object/sign/{bucket}/{path:path}")
def get_signed(bucket: str, path: str):
    data = OBJECTS.get(_key(bucket, path))
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="application/octet-stream")


@app.get("/storage/v1/object/{bucket}/{path:path}")
def get_obj(bucket: str, path: str):
    data = OBJECTS.get(_key(bucket, path))
    if data is None:
        return JSONResponse({"error": "not_found"}, 400)
    return Response(data, media_type="application/octet-stream")


@app.post("/storage/v1/object/list/{bucket}")
async def list_obj(bucket: str, request: Request):
    prefix = (await request.json())["prefix"].rstrip("/") + "/"
    return [{"name": k.split(prefix, 1)[1]} for k in OBJECTS if k.startswith(f"{bucket}/{prefix}")]


@app.delete("/storage/v1/object/{bucket}")
async def del_obj(bucket: str, request: Request):
    for p in (await request.json())["prefixes"]:
        OBJECTS.pop(_key(bucket, p), None)
    return []
