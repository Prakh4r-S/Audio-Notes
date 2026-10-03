"""The processing pipeline for one recording.

    validating  -> download from bucket, ffprobe, full decode to 16 kHz mono WAV
    preparing   -> pick an engine; for REST, detect pauses and plan ~25 s chunks
    transcribing-> REST: chunks in parallel with retries | Batch: submit + poll
    summarizing -> Groq LLM (map-reduce for long transcripts)

Every stage writes progress and a human-readable activity log to Postgres, which
the frontend polls. Work is resumable: transcribed chunks and the Gnani batch job
id are persisted, so a retry (or a worker crash) does not redo finished work.
"""

from __future__ import annotations

import logging
import random
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import delete, func, select

from . import audio
from .config import BATCH_LANGUAGES, LANGUAGES, get_settings
from .db import Chunk, Event, Recording, session_scope, utcnow
from .errors import (
    InvalidAudioError,
    PipelineError,
    ProviderAuthError,
    RecordingGone,
    SummaryError,
    TranscriptionError,
)
from .gnani import BATCH_TERMINAL, GnaniCallError, GnaniClient
from .storage import get_storage
from .summarize import Summarizer

log = logging.getLogger("pipeline")


# --------------------------------------------------------------------------- helpers
def update(rid: uuid.UUID, **fields) -> Recording:
    with session_scope() as s:
        rec = s.get(Recording, rid)
        if rec is None:
            raise RecordingGone()
        for k, v in fields.items():
            setattr(rec, k, v)
        return rec


def event(rid: uuid.UUID, message: str, level: str = "info") -> None:
    try:
        with session_scope() as s:
            if s.get(Recording, rid) is None:
                raise RecordingGone()
            s.add(Event(recording_id=rid, message=message, level=level))
    except RecordingGone:
        raise
    except Exception:  # logging must never break processing
        log.exception("could not write event")


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- entry points
def process_recording(rid: uuid.UUID, attempt: int, max_attempts: int) -> None:
    settings = get_settings()
    with session_scope() as s:
        rec = s.get(Recording, rid)
        if rec is None:
            raise RecordingGone()
        key, language, mode, filename = rec.storage_key, rec.language, rec.mode, rec.filename

    update(rid, status="processing", stage="validating", progress=2.0, error_code=None, error_message=None,
           stage_detail="Fetching the file from storage")
    event(rid, "Processing started" + (f" (attempt {attempt} of {max_attempts})" if attempt > 1 else ""))

    with tempfile.TemporaryDirectory(prefix="audionotes-") as tmp:
        src = Path(tmp) / ("source" + Path(filename).suffix.lower())
        wav = Path(tmp) / "normalized.wav"

        get_storage().download_to(key, src)
        update(rid, progress=4.0, stage_detail="Checking that the file is valid audio")
        info = audio.probe(src)
        update(rid, progress=6.0, stage_detail="Decoding and normalising audio (16 kHz mono)")
        duration = audio.normalize(src, wav)
        if duration < 0.5:
            raise InvalidAudioError(
                "The file appears to be corrupted or empty: less than half a second of audio could be decoded."
            )
        if info.duration_s > 5 and duration < 0.9 * info.duration_s:
            event(rid, f"Only {fmt_ts(duration)} of the declared {fmt_ts(info.duration_s)} could be decoded; "
                       "the file may be truncated. Continuing with the readable part.", "warn")
        if duration > settings.max_duration_min * 60:
            raise InvalidAudioError(
                f"The recording is {fmt_ts(duration)} long; the limit is {settings.max_duration_min} minutes."
            )
        update(rid, duration_s=duration, progress=8.0, stage="preparing", stage_detail="Choosing a transcription strategy")
        event(rid, f"Audio OK: {fmt_ts(duration)} ({info.codec or 'unknown codec'}, "
                   f"{info.channels or '?'} ch, {info.sample_rate or '?'} Hz)")

        engines = choose_engines(mode, language, duration)
        if mode == "batch" and engines[0] == "rest":
            why = (f"{LANGUAGES.get(language, language)} is not supported by the Batch API"
                   if language not in BATCH_LANGUAGES else "the Batch API is not available on this deployment")
            event(rid, f"Batch mode requested, but {why}; using chunked REST instead.", "warn")
        gnani = GnaniClient()
        try:
            last_error: PipelineError | None = None
            for i, engine in enumerate(engines):
                try:
                    if engine == "rest":
                        transcribe_rest(rid, wav, duration, language, gnani)
                    else:
                        transcribe_batch(rid, key, duration, language, gnani)
                    last_error = None
                    break
                except (TranscriptionError, ProviderAuthError) as e:
                    last_error = e
                    if isinstance(e, ProviderAuthError) or i == len(engines) - 1:
                        raise
                    nxt = "chunked REST" if engines[i + 1] == "rest" else "the Gnani Batch API"
                    event(rid, f"{e.message} Falling back to {nxt}.", "warn")
            if last_error:
                raise last_error
        finally:
            gnani.close()

    transcript, failed = assemble_transcript(rid)
    update(rid, transcript=transcript, progress=90.0, stage="summarizing", stage_detail="Generating summary")
    if failed:
        event(rid, f"Transcript ready, but {failed} segment(s) could not be transcribed and are marked in the text.", "warn")
    else:
        event(rid, f"Transcript ready ({len(transcript.split())} words)")

    run_summary(rid, transcript)
    update(rid, status="completed", stage="done", progress=100.0, stage_detail=None, completed_at=utcnow())
    event(rid, "Done")


def run_summary(rid: uuid.UUID, transcript: str | None = None) -> None:
    """Generate the summary. A failure here never discards the transcript."""
    if transcript is None:
        with session_scope() as s:
            rec = s.get(Recording, rid)
            if rec is None:
                raise RecordingGone()
            transcript = rec.transcript or ""
    update(rid, summary_status="running", summary_error=None)
    try:
        summarizer = Summarizer()
    except SummaryError as e:
        update(rid, summary_status="failed", summary_error=e.message)
        event(rid, e.message, "error")
        return
    try:
        def progress(done: int, total: int) -> None:
            update(rid, stage_detail=f"Summarising long transcript: section {done} of {total}",
                   progress=90.0 + 8.0 * done / max(total, 1))

        summarizer.on_status = lambda msg: update(rid, stage_detail=msg)
        summary = summarizer.summarize(transcript, on_progress=progress)
        update(rid, summary=summary, summary_status="done", summary_model=summarizer.model_used)
        event(rid, f"Summary generated with {summarizer.model_used}")
    except (SummaryError, ProviderAuthError) as e:
        update(rid, summary_status="failed", summary_error=e.message)
        event(rid, f"{e.message} The transcript is still available; use 'Retry summary' to try again.", "error")
    finally:
        summarizer.close()


# --------------------------------------------------------------------------- engine choice
def choose_engines(mode: str, language: str, duration: float) -> list[str]:
    s = get_settings()
    batch_ok = s.batch_available and language in BATCH_LANGUAGES
    if mode == "batch":
        return ["batch", "rest"] if batch_ok else ["rest"]
    if mode == "chunked":
        return ["rest"]
    # auto: chunked REST first (fine-grained progress, all languages), Batch as fallback.
    return ["rest", "batch"] if batch_ok else ["rest"]


# --------------------------------------------------------------------------- REST path
def transcribe_rest(rid: uuid.UUID, wav: Path, duration: float, language: str, gnani: GnaniClient) -> None:
    s = get_settings()
    with session_scope() as sess:
        rec = sess.get(Recording, rid)
        existing = sess.scalars(select(Chunk).where(Chunk.recording_id == rid).order_by(Chunk.idx)).all()
        reuse = rec.engine == "rest" and existing
        rows = [(c.idx, c.start_s, c.end_s, c.status) for c in existing] if reuse else []

    if not reuse:
        update(rid, stage_detail="Finding pauses to split the audio at natural breaks")
        silences = audio.detect_silences(wav)
        plan = audio.plan_chunks(duration, silences, s.chunk_target_s, s.chunk_min_s, s.chunk_max_s)
        with session_scope() as sess:
            sess.execute(delete(Chunk).where(Chunk.recording_id == rid))
            for i, (a, b) in enumerate(plan):
                sess.add(Chunk(recording_id=rid, idx=i, start_s=a, end_s=b, status="pending"))
        rows = [(i, a, b, "pending") for i, (a, b) in enumerate(plan)]
        event(rid, f"Split into {len(plan)} segment(s) of up to {int(s.chunk_max_s)} s"
                   + (f", cutting at pauses ({len(silences)} found)" if len(plan) > 1 else ""))
    else:
        done_already = sum(1 for r in rows if r[3] == "done")
        event(rid, f"Resuming: {done_already} of {len(rows)} segments were already transcribed")

    total = len(rows)
    update(rid, engine="rest", gnani_batch_job_id=None, chunks_total=total, stage="transcribing")
    todo = [r for r in rows if r[3] != "done"]
    lock = threading.Lock()
    cancelled = threading.Event()

    def refresh_progress() -> None:
        with lock, session_scope() as sess:
            done = sess.scalar(select(func.count()).where(Chunk.recording_id == rid, Chunk.status == "done"))
            failed = sess.scalar(select(func.count()).where(Chunk.recording_id == rid, Chunk.status == "failed"))
            rec = sess.get(Recording, rid)
            if rec is None:
                cancelled.set()
                raise RecordingGone()
            rec.chunks_done, rec.chunks_failed = done, failed
            rec.progress = 10.0 + 78.0 * done / max(total, 1)
            rec.stage_detail = f"Transcribing segment {min(done + failed + 1, total)} of {total} with Gnani"

    def set_chunk(idx: int, **fields) -> None:
        with session_scope() as sess:
            c = sess.get(Chunk, (rid, idx))
            if c is None:
                cancelled.set()
                raise RecordingGone()
            for k, v in fields.items():
                setattr(c, k, v)

    def work(idx: int, a: float, b: float) -> str:
        """Returns 'done', 'permanent' or 'transient'."""
        clip = audio.slice_wav(wav, a, b)
        last = ""
        for attempt in range(1, s.chunk_max_attempts + 1):
            if cancelled.is_set():
                return "cancelled"
            set_chunk(idx, status="running", attempts=attempt)
            try:
                text = gnani.transcribe_clip(clip, language)
                set_chunk(idx, status="done", text=text, error=None)
                return "done"
            except GnaniCallError as e:
                last = e.message
                if not e.transient:
                    set_chunk(idx, status="failed", error=last)
                    return "permanent"
                if attempt < s.chunk_max_attempts:
                    wait = e.retry_after or min(30.0, 1.5 * 2 ** attempt) + random.random()
                    set_chunk(idx, status="pending", error=f"{last} - retrying in {wait:.0f} s")
                    time.sleep(wait)
        set_chunk(idx, status="failed", error=last)
        return "transient"

    refresh_progress()
    results: list[str] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=s.chunk_concurrency) as pool:
        futures = {pool.submit(work, i, a, b): i for i, a, b, _ in todo}
        try:
            for fut in as_completed(futures):
                results.append(fut.result())
                refresh_progress()
        except BaseException:
            cancelled.set()
            raise

    transient = results.count("transient")
    permanent = results.count("permanent")
    if transient:
        with session_scope() as sess:
            errs = sess.scalars(select(Chunk.error).where(Chunk.recording_id == rid, Chunk.status == "failed")).all()
        errors = [e for e in errs if e]
        raise TranscriptionError(
            f"{transient} of {total} segment(s) failed after {s.chunk_max_attempts} attempts "
            f"(last error: {errors[-1] if errors else 'unknown'})."
        )
    if permanent and permanent == total:
        raise TranscriptionError("Gnani rejected every segment of this recording.", retryable=False)


# --------------------------------------------------------------------------- Batch path
BATCH_STATUS_TEXT = {
    "CREATED": "Batch job created",
    "STARTING": "Gnani is validating the file",
    "QUEUED": "Waiting in Gnani's batch queue",
    "IN_PROGRESS": "Gnani is transcribing the full recording",
}


def transcribe_batch(rid: uuid.UUID, key: str, duration: float, language: str, gnani: GnaniClient) -> None:
    s = get_settings()
    if language not in BATCH_LANGUAGES:
        raise TranscriptionError(f"The Batch API does not support {LANGUAGES.get(language, language)}.")
    with session_scope() as sess:
        rec = sess.get(Recording, rid)
        job_id = rec.gnani_batch_job_id if rec.engine == "batch" else None

    try:
        if job_id:
            event(rid, f"Resuming existing Gnani batch job {job_id[:8]}…")
        else:
            url = get_storage().public_fetch_url(key, expires_s=6 * 3600)
            if not url:
                raise TranscriptionError("The Batch API needs a cloud storage URL, which is unavailable.")
            with session_scope() as sess:
                sess.execute(delete(Chunk).where(Chunk.recording_id == rid))
            job_id = gnani.batch_create(url, language)
            update(rid, engine="batch", gnani_batch_job_id=job_id, chunks_total=0, chunks_done=0, chunks_failed=0,
                   stage="transcribing", stage_detail="Submitting to Gnani Batch API")
            try:
                gnani.batch_start(job_id)
            except Exception:
                update(rid, gnani_batch_job_id=None)
                raise
            event(rid, f"Submitted to Gnani Batch API (job {job_id[:8]}…)")

        update(rid, engine="batch", stage="transcribing")
        started = time.monotonic()
        deadline = started + max(s.batch_min_timeout_s, duration * 2)
        expected = 30 + duration * 0.35  # rough turnaround estimate, for the progress bar only
        status = "CREATED"
        info: dict = {}
        while True:
            info = gnani.batch_status(job_id)
            status = info.get("status", "UNKNOWN")
            if status in BATCH_TERMINAL:
                break
            if time.monotonic() > deadline:
                gnani.batch_cancel(job_id)
                update(rid, gnani_batch_job_id=None)
                raise TranscriptionError(
                    f"The Gnani batch job did not finish within {int((deadline - started) / 60)} minutes."
                )
            elapsed = time.monotonic() - started
            est = min(0.95, elapsed / expected)
            update(rid, progress=10.0 + 78.0 * est,
                   stage_detail=f"{BATCH_STATUS_TEXT.get(status, status.title())} · {int(elapsed)} s elapsed")
            time.sleep(s.batch_poll_interval_s)
    except GnaniCallError as e:
        raise TranscriptionError(f"Gnani Batch API error: {e.message}.", retryable=e.transient) from e

    if status not in ("COMPLETED", "PARTIAL_FAILURE"):
        reason = info.get("cancel_reason")
        if not reason:
            try:
                files = gnani.batch_files(job_id)
                reason = next((f.get("error_message") for f in files if f.get("error_message")), None)
            except GnaniCallError:
                pass
        update(rid, gnani_batch_job_id=None)
        raise TranscriptionError(f"Gnani batch job ended with {status}: {reason or 'no reason given'}.")

    files = [f for f in gnani.batch_files(job_id) if f.get("status") == "COMPLETED" and f.get("transcript_url")]
    if not files:
        update(rid, gnani_batch_job_id=None)
        raise TranscriptionError("Gnani batch job completed but returned no transcript.")
    data = gnani.fetch_transcript(files[0]["transcript_url"])
    segments = [sg for sg in data.get("segments") or [] if (sg.get("text") or "").strip()]
    with session_scope() as sess:
        sess.execute(delete(Chunk).where(Chunk.recording_id == rid))
        if segments:
            for i, sg in enumerate(segments):
                sess.add(Chunk(recording_id=rid, idx=i, start_s=float(sg.get("start_time") or 0),
                               end_s=float(sg.get("end_time") or 0), status="done", text=sg["text"].strip()))
        else:
            sess.add(Chunk(recording_id=rid, idx=0, start_s=0.0, end_s=duration, status="done",
                           text=(data.get("full_transcript") or "").strip()))
    n = max(1, len(segments))
    update(rid, chunks_total=n, chunks_done=n, chunks_failed=0, progress=88.0)
    event(rid, f"Gnani batch job finished ({len(segments)} timed segments)")


# --------------------------------------------------------------------------- assembly
def assemble_transcript(rid: uuid.UUID) -> tuple[str, int]:
    with session_scope() as sess:
        chunks = sess.scalars(select(Chunk).where(Chunk.recording_id == rid).order_by(Chunk.idx)).all()
    parts, failed = [], 0
    for c in chunks:
        if c.status == "done":
            if c.text:
                parts.append(c.text)
        else:
            failed += 1
            parts.append(f"[{fmt_ts(c.start_s)}–{fmt_ts(c.end_s)} could not be transcribed]")
    return " ".join(parts).strip(), failed
