"""Thin clients for Gnani's speech-to-text APIs (docs.gnani.ai).

* REST  - POST /stt/v3, synchronous, <= 60 s of audio per request.
* Batch - /stt/v3/batch/jobs, asynchronous, up to 4 h per file, pulls audio from a URL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from .config import get_settings
from .errors import ProviderAuthError, TranscriptionError


@dataclass
class GnaniCallError(Exception):
    """A failed call, classified for retry decisions."""

    status: int | None
    message: str
    transient: bool
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return (r.text or "")[:200] or f"HTTP {r.status_code}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("type") or json.dumps(err)[:200]
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("message") or json.dumps(detail)[:200]
        return body.get("message") or (str(err) if err else json.dumps(body)[:200])
    return str(body)[:200]


def _classify(r: httpx.Response) -> GnaniCallError:
    msg = _error_message(r)
    if r.status_code in (401, 403):
        raise ProviderAuthError(
            f"Gnani rejected the request ({r.status_code}): {msg}. Check the API key and remaining credits."
        )
    retry_after = None
    if ra := r.headers.get("retry-after"):
        try:
            retry_after = float(ra)
        except ValueError:
            pass
    transient = r.status_code in (408, 409, 425, 429) or r.status_code >= 500
    return GnaniCallError(r.status_code, f"Gnani returned {r.status_code}: {msg}", transient, retry_after)


class GnaniClient:
    def __init__(self) -> None:
        s = get_settings()
        if not s.gnani_api_key:
            raise ProviderAuthError("Transcription is not configured (GNANI_API_KEY is missing).")
        self.s = s
        self.http = httpx.Client(
            base_url=s.gnani_base_url,
            headers={"X-API-Key-ID": s.gnani_api_key},
            timeout=httpx.Timeout(s.gnani_rest_timeout_s, connect=15.0),
        )

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ REST
    def transcribe_clip(self, wav_bytes: bytes, language: str) -> str:
        try:
            r = self.http.post(
                "/stt/v3",
                files={"audio_file": ("chunk.wav", wav_bytes, "audio/wav")},
                data={"language_code": language, "format": "transcribe"},
            )
        except httpx.TimeoutException as e:
            raise GnaniCallError(None, "Gnani did not respond in time", transient=True) from e
        except httpx.HTTPError as e:
            raise GnaniCallError(None, f"Network error reaching Gnani ({e.__class__.__name__})", True) from e
        if r.status_code != 200:
            raise _classify(r)
        body = r.json()
        if not body.get("success", True):
            raise GnaniCallError(200, f"Gnani reported a failure: {_error_message(r)}", transient=True)
        return (body.get("transcript") or "").strip()

    # ----------------------------------------------------------------- Batch
    def _call(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.http.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise GnaniCallError(None, f"Network error reaching Gnani Batch ({e.__class__.__name__})", True) from e
        if r.status_code >= 300:
            raise _classify(r)
        return r.json()

    def batch_create(self, audio_url: str, language: str) -> str:
        body = {
            "config": {"model": self.s.gnani_batch_model, "language_code": language, "mode": "transcribe"},
            "source": {"type": "cloud_storage", "auth": {"mode": "public"}, "paths": [audio_url]},
        }
        return self._call("POST", "/stt/v3/batch/jobs", json=body)["job_id"]

    def batch_start(self, job_id: str) -> None:
        self._call("POST", f"/stt/v3/batch/jobs/{job_id}/start")

    def batch_status(self, job_id: str) -> dict:
        return self._call("GET", f"/stt/v3/batch/jobs/{job_id}")

    def batch_files(self, job_id: str) -> list[dict]:
        return self._call("GET", f"/stt/v3/batch/jobs/{job_id}/files", params={"limit": 100}).get("data", [])

    def batch_cancel(self, job_id: str) -> None:
        try:
            self._call("POST", f"/stt/v3/batch/jobs/{job_id}/cancel")
        except Exception:  # best effort
            pass

    def fetch_transcript(self, url: str) -> dict:
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
        except httpx.HTTPError as e:
            raise TranscriptionError(f"Could not download the batch transcript ({e.__class__.__name__}).") from e
        if r.status_code != 200:
            raise TranscriptionError(f"Could not download the batch transcript (HTTP {r.status_code}).")
        return r.json()


BATCH_TERMINAL = {"COMPLETED", "PARTIAL_FAILURE", "FAILED", "START_FAILED", "CANCELLED"}
