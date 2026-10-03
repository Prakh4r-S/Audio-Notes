"""Transcript summarisation via Groq's OpenAI-compatible chat completions API.

Short transcripts are summarised in one call. Long ones use map-reduce: each
~24k-character section is condensed into notes, then the notes are combined
into the final summary, so length is bounded by time rather than context size.
"""

from __future__ import annotations

import random
import time

import httpx

from .config import get_settings
from .errors import ProviderAuthError, SummaryError

SYSTEM = (
    "You summarise transcripts of audio recordings produced by automatic speech recognition. "
    "The transcript may contain recognition errors, missing punctuation or mixed Hindi and English; "
    "infer the intended meaning but never invent facts that are not supported by the text. "
    "Always write the summary in English, even when the transcript is in another language."
)

FINAL_INSTRUCTIONS = """Write a summary of the recording in Markdown with exactly these sections:

## Overview
Two to four sentences: what the recording is and its main point.

## Key points
Five to eight concise bullet points, in the order they occur.

## Action items & decisions
Bullets for any tasks, decisions, dates or numbers worth acting on. If there are none, write "None mentioned."

Do not add any other sections or any preamble."""

MAP_INSTRUCTIONS = (
    "This is one section of a longer transcript. Write dense notes (8-15 bullets) capturing every "
    "topic, claim, name, number, decision and action item in this section. No preamble."
)


class Summarizer:
    def __init__(self) -> None:
        s = get_settings()
        if not s.groq_api_key:
            raise SummaryError("Summaries are not configured (GROQ_API_KEY is missing).", retryable=False)
        self.s = s
        self.models = s.groq_model_list
        self.model_used: str | None = None
        self.on_status = None  # optional callback(str) for user-visible retry messages
        self.http = httpx.Client(
            base_url=s.groq_base_url,
            headers={"Authorization": f"Bearer {s.groq_api_key}"},
            timeout=httpx.Timeout(s.groq_timeout_s, connect=15.0),
        )

    def close(self) -> None:
        self.http.close()

    def _complete(self, user: str, max_tokens: int = 1200) -> str:
        last_error = "unknown error"
        models = [self.model_used] if self.model_used else self.models
        for model in models:
            for attempt in range(4):
                try:
                    r = self.http.post(
                        "/chat/completions",
                        json={
                            "model": model,
                            "temperature": 0.2,
                            "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                        },
                    )
                except httpx.HTTPError as e:
                    last_error = f"network error ({e.__class__.__name__})"
                    self._wait(min(20, 2 ** attempt + random.random()), last_error)
                    continue
                if r.status_code == 200:
                    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
                    if text:
                        self.model_used = model
                        return text
                    last_error = "the model returned an empty response"
                    continue
                if r.status_code == 401:
                    raise ProviderAuthError("Groq rejected the API key (401).")
                if r.status_code in (400, 403, 404):
                    # Model decommissioned / not enabled for this account: try the next one.
                    last_error = f"model {model} unavailable ({r.status_code})"
                    break
                last_error = f"Groq returned {r.status_code}"
                wait = r.headers.get("retry-after")
                self._wait(min(30, float(wait)) if wait and wait.replace(".", "").isdigit()
                           else min(20, 2 ** attempt + random.random()), last_error)
        raise SummaryError(f"The summary could not be generated: {last_error}.")

    def _wait(self, seconds: float, reason: str) -> None:
        if self.on_status:
            self.on_status(f"Summary service problem ({reason}); retrying in {seconds:.0f} s")
        time.sleep(seconds)

    def summarize(self, transcript: str, on_progress=None) -> str:
        text = transcript.strip()
        if len(text) < 20:
            return "## Overview\nThe recording contains little or no recognisable speech, so there is nothing to summarise."
        size = self.s.summary_chunk_chars
        if len(text) <= size:
            return self._complete(f"{FINAL_INSTRUCTIONS}\n\nTranscript:\n\"\"\"\n{text}\n\"\"\"")

        sections = _split(text, size)
        notes = []
        for i, sec in enumerate(sections, 1):
            if on_progress:
                on_progress(i - 1, len(sections))
            notes.append(self._complete(f"{MAP_INSTRUCTIONS}\n\nSection {i} of {len(sections)}:\n\"\"\"\n{sec}\n\"\"\"", 900))
        if on_progress:
            on_progress(len(sections), len(sections))
        joined = "\n\n".join(f"Notes on section {i}:\n{n}" for i, n in enumerate(notes, 1))
        return self._complete(
            f"{FINAL_INSTRUCTIONS}\n\nThe recording was long, so it was first condensed into section notes. "
            f"Summarise the whole recording from these notes:\n\"\"\"\n{joined}\n\"\"\""
        )


def _split(text: str, size: int) -> list[str]:
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind(". ", start, end), text.rfind("। ", start, end), text.rfind(" ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        parts.append(text[start:end].strip())
        start = end
    return [p for p in parts if p]
