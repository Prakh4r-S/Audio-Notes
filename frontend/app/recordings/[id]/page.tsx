"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ProgressPanel } from "@/components/ProgressPanel";
import { StatusBadge } from "@/components/StatusBadge";
import { ApiError, api, fmtBytes, fmtTime, isActive, type Recording } from "@/lib/api";

const LANG: Record<string, string> = {
  "en-IN": "English (India)", "hi-IN": "Hindi", "bn-IN": "Bengali", "gu-IN": "Gujarati", "kn-IN": "Kannada",
  "ml-IN": "Malayalam", "mr-IN": "Marathi", "pa-IN": "Punjabi", "ta-IN": "Tamil", "te-IN": "Telugu",
};

export default function RecordingPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [r, setR] = useState<Recording | null>(null);
  const [loadError, setLoadError] = useState<{ msg: string; gone: boolean } | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get(id);
      setR(data);
      setLoadError(null);
      // Signed URLs change on every fetch; keep the first so playback isn't interrupted.
      setAudioUrl((cur) => cur ?? data.audio_url);
    } catch (e) {
      const gone = e instanceof ApiError && e.status === 404;
      setLoadError({ msg: e instanceof Error ? e.message : "Couldn't load this recording.", gone });
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const active = r ? isActive(r.status) || r.summary_status === "running" : true;
  useEffect(() => {
    if (!active || loadError?.gone) return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [active, load, loadError?.gone]);

  async function retry() {
    setBusy(true);
    setActionError(null);
    try {
      await api.retry(id);
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Retry failed.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Delete this recording, its audio, transcript and summary? This can't be undone.")) return;
    try {
      await api.remove(id);
      router.push("/");
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Delete failed.");
    }
  }

  function seek(t: number) {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = t;
    a.play().catch(() => undefined);
  }

  if (loadError && (loadError.gone || !r)) {
    return (
      <>
        <Link href="/" className="back">Back to recordings</Link>
        <div className={`notice ${loadError.gone ? "warn" : "fail"}`} role="alert">
          <div>
            <strong>{loadError.gone ? "Recording not found" : "Couldn't load this recording"}</strong>
            {loadError.gone
              ? "It may have been deleted, or it was uploaded from a different browser."
              : `${loadError.msg} Retrying automatically.`}
          </div>
        </div>
      </>
    );
  }
  if (!r) return <p className="waiting">Loading…</p>;

  const failedSegments = r.segments.filter((s) => s.status === "failed").length;

  return (
    <>
      <Link href="/" className="back">Back to recordings</Link>

      <div className="detail-head">
        <div style={{ minWidth: 0 }}>
          <h1>{r.filename}</h1>
          <div className="facts">
            <StatusBadge status={r.status} />
            {r.duration_s != null && <span><b>{fmtTime(r.duration_s)}</b> long</span>}
            <span>{fmtBytes(r.size_bytes)}</span>
            <span>{LANG[r.language] ?? r.language}</span>
            {r.engine && (
              <span>
                {r.status === "completed" ? "Transcribed" : "Transcribing"} via{" "}
                {r.engine === "batch" ? "Gnani Batch API" : `Gnani REST in ${r.chunks_total} segments`}
              </span>
            )}
          </div>
        </div>
        <button className="btn danger small" onClick={remove}>Delete</button>
      </div>

      {loadError && (
        <div className="notice warn" style={{ marginBottom: 16 }}>
          <div>Lost contact with the server ({loadError.msg}). Showing the last known state; reconnecting.</div>
        </div>
      )}

      {r.status === "failed" && (
        <div className="notice fail" role="alert" style={{ marginBottom: 20 }}>
          <div>
            <strong>{failureTitle(r.error_code)}</strong>
            {r.error_message}
            <div className="actions">
              {r.retryable ? (
                <button className="btn small" onClick={retry} disabled={busy}>
                  {busy ? "Retrying…" : "Retry processing"}
                </button>
              ) : (
                <Link className="btn small" href="/">Upload a different file</Link>
              )}
            </div>
          </div>
        </div>
      )}

      {r.status === "retrying" && (
        <div className="notice warn" role="status" style={{ marginBottom: 20 }}>
          <div>
            <strong>Hit a temporary problem; will retry on its own</strong>
            {r.error_message} Segments already transcribed are kept.
          </div>
        </div>
      )}

      {actionError && (
        <div className="notice fail" role="alert" style={{ marginBottom: 20 }}><div>{actionError}</div></div>
      )}

      {r.status !== "completed" && <ProgressPanel r={r} />}

      {r.status === "completed" && failedSegments > 0 && (
        <div className="notice warn" style={{ marginBottom: 20 }}>
          <div>
            {failedSegments} segment(s) could not be transcribed. They are marked in the transcript; the rest is complete.
          </div>
        </div>
      )}

      {audioUrl && (
        <div className="player">
          <audio ref={audioRef} controls preload="metadata" src={audioUrl} />
        </div>
      )}

      <div className="columns">
        <section className="pane summary-pane" aria-labelledby="sum-title">
          <div className="pane-head">
            <h2 id="sum-title">Summary</h2>
            {r.summary && <CopyButton text={r.summary} />}
          </div>
          <div className="pane-body">
            <SummaryBody r={r} onRetry={retry} busy={busy} />
          </div>
        </section>

        <TranscriptPane r={r} onSeek={seek} canSeek={!!audioUrl} />
      </div>

      <details className="activity" open={r.status === "failed" || r.status === "retrying"}>
        <summary>Activity log ({r.events.length})</summary>
        <ol>
          {r.events.map((e, i) => (
            <li key={i} className={e.level}>
              <time dateTime={e.at}>{new Date(e.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</time>
              <span>{e.message}</span>
            </li>
          ))}
        </ol>
      </details>
    </>
  );
}

function failureTitle(code: string | null): string {
  switch (code) {
    case "INVALID_AUDIO": return "This file can't be transcribed";
    case "UPLOAD_FAILED":
    case "UPLOAD_INCOMPLETE":
    case "UPLOAD_ABANDONED": return "The upload didn't finish";
    case "PROVIDER_AUTH": return "The transcription service refused the request";
    case "TRANSCRIPTION_FAILED": return "Transcription failed";
    case "STORAGE_ERROR": return "Couldn't read the file from storage";
    default: return "Processing failed";
  }
}

function SummaryBody({ r, onRetry, busy }: { r: Recording; onRetry: () => void; busy: boolean }) {
  if (r.summary_status === "done" && r.summary) {
    return (
      <>
        <div className="prose"><ReactMarkdown>{r.summary.replace(/<!--[\s\S]*?-->/g, "")}</ReactMarkdown></div>
        {r.summary_model && <p className="model-note">Written by {r.summary_model} on Groq from the transcript.</p>}
      </>
    );
  }
  if (r.summary_status === "failed") {
    return (
      <div className="notice warn">
        <div>
          <strong>No summary yet</strong>
          {r.summary_error} The transcript is unaffected.
          {r.status === "completed" && (
            <div className="actions">
              <button className="btn small" onClick={onRetry} disabled={busy}>{busy ? "Retrying…" : "Retry summary"}</button>
            </div>
          )}
        </div>
      </div>
    );
  }
  if (r.status === "failed") return <p className="waiting">No summary, because the recording could not be transcribed.</p>;
  return (
    <div className="waiting">
      {r.summary_status === "running" ? "Writing the summary…" : "The summary is written once the transcript is complete."}
      <div className="lines" aria-hidden="true">
        <span style={{ width: "92%" }} /><span style={{ width: "78%" }} /><span style={{ width: "85%" }} /><span style={{ width: "60%" }} />
      </div>
    </div>
  );
}

function TranscriptPane({ r, onSeek, canSeek }: { r: Recording; onSeek: (t: number) => void; canSeek: boolean }) {
  const [view, setView] = useState<"timed" | "plain">("timed");
  const hasText = r.segments.some((s) => s.text);
  const plain = r.transcript ?? r.segments.filter((s) => s.text).map((s) => s.text).join(" ");

  function download() {
    const body = r.segments.length
      ? r.segments.map((s) => `[${fmtTime(s.start)}] ${s.status === "done" ? s.text ?? "" : "(not transcribed)"}`).join("\n")
      : plain;
    const blob = new Blob([body], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = r.filename.replace(/\.[^.]+$/, "") + "-transcript.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <section className="pane" aria-labelledby="tr-title">
      <div className="pane-head">
        <h2 id="tr-title">Transcript</h2>
        {hasText && (
          <div className="tools">
            <div className="seg-control" style={{ height: 30 }} role="group" aria-label="Transcript view">
              <button aria-pressed={view === "timed"} onClick={() => setView("timed")}>Timed</button>
              <button aria-pressed={view === "plain"} onClick={() => setView("plain")}>Plain</button>
            </div>
            <CopyButton text={plain} />
            <button className="btn quiet small" onClick={download}>Download .txt</button>
          </div>
        )}
      </div>
      <div className="pane-body">
        {!hasText && r.segments.length === 0 && (
          <p className="waiting">
            {r.status === "failed"
              ? "No transcript was produced."
              : r.engine === "batch"
                ? "Gnani returns the batch transcript in one piece when the job finishes."
                : "Text appears here segment by segment as Gnani returns it."}
          </p>
        )}
        {view === "plain" && hasText ? (
          <div className="plain">{plain}</div>
        ) : (
          <div className="segments">
            {r.segments.map((s) => (
              <div key={s.idx} className={`segline ${s.status}`}>
                <button className="ts" onClick={() => onSeek(s.start)} disabled={!canSeek} title={canSeek ? "Play from here" : undefined}>
                  {fmtTime(s.start)}
                </button>
                <div className="txt">
                  {s.status === "done"
                    ? s.text || <em style={{ color: "var(--ink-3)" }}>(no speech detected)</em>
                    : s.status === "failed"
                      ? `Could not transcribe ${fmtTime(s.start)}–${fmtTime(s.end)}: ${s.error ?? "unknown error"}`
                      : s.status === "running"
                        ? `Transcribing${s.attempts > 1 ? ` (attempt ${s.attempts})` : ""}…`
                        : s.error
                          ? s.error
                          : "Waiting"}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn quiet small"
      onClick={() =>
        navigator.clipboard.writeText(text).then(
          () => { setDone(true); setTimeout(() => setDone(false), 1500); },
          () => undefined,
        )
      }
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}
