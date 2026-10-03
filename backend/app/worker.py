"""Postgres-backed job queue worker.

Jobs live in the `jobs` table. A worker claims one atomically with
`SELECT ... FOR UPDATE SKIP LOCKED`, so any number of workers (threads or
separate processes/machines) can run without double-processing. While a job
runs, a heartbeat column is refreshed; if a worker dies, its job's heartbeat
goes stale and another worker reclaims it. Retryable failures are rescheduled
with exponential backoff via `run_after`.

Run standalone:  python -m app.worker
Or in-process:   RUN_WORKER_IN_PROCESS=true (started by the API on boot)
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
from datetime import timedelta

from sqlalchemy import text, update as sql_update

from .config import get_settings
from .db import Job, Recording, init_db, session_scope, utcnow
from .errors import PipelineError, RecordingGone
from .pipeline import event, process_recording, run_summary, update

log = logging.getLogger("worker")

CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', attempts = attempts + 1,
                    locked_by = :worker, heartbeat_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE (status = 'queued' AND run_after <= now())
           OR (status = 'running' AND heartbeat_at < now() - make_interval(secs => :stale))
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, recording_id, kind, attempts, max_attempts
    """
)


def enqueue(recording_id: uuid.UUID, kind: str = "process", delay_s: float = 0) -> None:
    s = get_settings()
    with session_scope() as sess:
        # Only one active job per recording and kind.
        sess.execute(
            sql_update(Job)
            .where(Job.recording_id == recording_id, Job.kind == kind, Job.status.in_(["queued", "running"]))
            .values(status="failed", last_error="superseded")
        )
        sess.add(Job(recording_id=recording_id, kind=kind, max_attempts=s.job_max_attempts,
                     run_after=utcnow() + timedelta(seconds=delay_s)))


class Worker:
    def __init__(self, concurrency: int | None = None):
        s = get_settings()
        self.settings = s
        self.concurrency = concurrency or s.worker_concurrency
        self.name = f"{socket.gethostname()}-{os.getpid()}"
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for i in range(self.concurrency):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)
        t = threading.Thread(target=self._janitor, name="janitor", daemon=True)
        t.start()
        self.threads.append(t)
        log.info("worker %s started with %d slot(s)", self.name, self.concurrency)

    def shutdown(self) -> None:
        self.stop.set()

    # ------------------------------------------------------------------ loops
    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                job = self._claim()
            except Exception:
                log.exception("claim failed")
                self.stop.wait(5)
                continue
            if job is None:
                self.stop.wait(1.5)
                continue
            self._run(*job)

    def _claim(self):
        with session_scope() as s:
            row = s.execute(CLAIM_SQL, {"worker": self.name, "stale": self.settings.stale_after_s}).first()
            return tuple(row) if row else None

    def _janitor(self) -> None:
        """Marks uploads that never completed as failed, so nothing stays 'uploading' forever."""
        while not self.stop.is_set():
            try:
                cutoff = utcnow() - timedelta(seconds=self.settings.upload_abandon_after_s)
                with session_scope() as s:
                    s.execute(
                        sql_update(Recording)
                        .where(Recording.status == "uploading", Recording.created_at < cutoff)
                        .values(status="failed", error_code="UPLOAD_ABANDONED", retryable=False,
                                error_message="The upload never completed (the page may have been closed).")
                    )
            except Exception:
                log.exception("janitor failed")
            self.stop.wait(60)

    # ------------------------------------------------------------------ one job
    def _run(self, job_id: int, rid: uuid.UUID, kind: str, attempt: int, max_attempts: int) -> None:
        beat_stop = threading.Event()

        def heartbeat() -> None:
            while not beat_stop.wait(self.settings.heartbeat_interval_s):
                try:
                    with session_scope() as s:
                        s.execute(sql_update(Job).where(Job.id == job_id).values(heartbeat_at=utcnow()))
                except Exception:
                    log.warning("heartbeat failed for job %s", job_id)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        log.info("job %s (%s) for %s, attempt %d/%d", job_id, kind, rid, attempt, max_attempts)
        try:
            if attempt > max_attempts:
                raise PipelineError("Processing was interrupted too many times.", code="WORKER_LOST", retryable=False)
            if kind == "summarize":
                update(rid, summary_status="running")
                run_summary(rid)
            else:
                process_recording(rid, attempt, max_attempts)
            self._finish(job_id, "done")
        except RecordingGone:
            log.info("recording %s deleted mid-job", rid)
            self._finish(job_id, "done", "recording deleted")
        except PipelineError as e:
            self._handle_failure(job_id, rid, kind, attempt, max_attempts, e)
        except Exception as e:  # unexpected bug or infrastructure problem
            log.error("job %s crashed:\n%s", job_id, traceback.format_exc())
            err = PipelineError(f"Unexpected error ({e.__class__.__name__}).", code="INTERNAL_ERROR")
            self._handle_failure(job_id, rid, kind, attempt, max_attempts, err)
        finally:
            beat_stop.set()

    def _finish(self, job_id: int, status: str, error: str | None = None, run_after=None) -> None:
        values = {"status": status, "last_error": error, "locked_by": None}
        if run_after is not None:
            values["run_after"] = run_after
        with session_scope() as s:
            s.execute(sql_update(Job).where(Job.id == job_id).values(**values))

    def _handle_failure(self, job_id, rid, kind, attempt, max_attempts, e: PipelineError) -> None:
        try:
            if kind == "summarize":
                update(rid, summary_status="failed", summary_error=e.message)
                self._finish(job_id, "failed", e.message)
                return
            if e.retryable and attempt < max_attempts:
                delay = 20 * 2 ** (attempt - 1)
                self._finish(job_id, "queued", e.message, run_after=utcnow() + timedelta(seconds=delay))
                update(rid, status="retrying", error_code=e.code, error_message=e.message, retryable=True,
                       stage_detail=f"Retrying automatically in {delay} s (attempt {attempt + 1} of {max_attempts})")
                event(rid, f"{e.message} Will retry automatically in {delay} s.", "warn")
            else:
                self._finish(job_id, "failed", e.message)
                update(rid, status="failed", error_code=e.code, error_message=e.message,
                       retryable=e.code not in ("INVALID_AUDIO", "UPLOAD_ABANDONED"), stage_detail=None)
                event(rid, f"Failed: {e.message}", "error")
        except RecordingGone:
            self._finish(job_id, "done", "recording deleted")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.shutdown())
    signal.signal(signal.SIGINT, lambda *_: w.shutdown())
    w.start()
    while not w.stop.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
