"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, fmtBytes, fmtTime, putFile, type PublicConfig } from "@/lib/api";

type Mode = "auto" | "chunked" | "batch";

const MODE_NOTES: Record<Mode, string> = {
  auto: "Splits the audio at pauses and transcribes the pieces in parallel. Switches to Gnani's Batch API if that fails.",
  chunked: "Only the pause-aligned, parallel REST path. Works for all ten languages.",
  batch: "Hands the whole file to Gnani's Batch API. Falls back to chunked transcription if the batch job fails.",
};

type Phase =
  | { kind: "idle" }
  | { kind: "uploading"; loaded: number; total: number; startedAt: number }
  | { kind: "finishing" }
  | { kind: "error"; message: string };

export function UploadPanel({ config, onCreated }: { config: PublicConfig | null; onCreated?: () => void }) {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [language, setLanguage] = useState("en-IN");
  const [mode, setMode] = useState<Mode>("auto");
  const [over, setOver] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const abortRef = useRef<(() => void) | null>(null);

  const lang = config?.languages.find((l) => l.code === language);
  const batchPossible = !!config?.batch_available && !!lang?.batch;
  const busy = phase.kind === "uploading" || phase.kind === "finishing";

  // Read the duration locally so problems show up before any bytes are sent.
  useEffect(() => {
    setDuration(null);
    if (!file) return;
    const url = URL.createObjectURL(file);
    const el = new Audio();
    el.preload = "metadata";
    el.onloadedmetadata = () => isFinite(el.duration) && setDuration(el.duration);
    el.src = url;
    return () => URL.revokeObjectURL(url);
  }, [file]);

  // Warn before leaving mid-upload.
  useEffect(() => {
    if (!busy) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [busy]);

  function validate(f: File): string | null {
    const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
    if (config && !config.extensions.includes(ext))
      return `${f.name} is not a supported audio file. Use one of: ${config.extensions.join(", ")}.`;
    if (f.size === 0) return `${f.name} is empty.`;
    if (config && f.size > config.max_upload_mb * 1024 * 1024)
      return `${f.name} is ${fmtBytes(f.size)}; the limit on this deployment is ${config.max_upload_mb} MB.`;
    return null;
  }

  function pick(f: File | undefined) {
    if (!f) return;
    const err = validate(f);
    setFile(f);
    setPhase(err ? { kind: "error", message: err } : { kind: "idle" });
  }

  async function start() {
    if (!file || validate(file)) return;
    let id: string | null = null;
    try {
      setPhase({ kind: "uploading", loaded: 0, total: file.size, startedAt: Date.now() });
      const created = await api.create({
        filename: file.name,
        size_bytes: file.size,
        content_type: file.type || "application/octet-stream",
        language,
        mode,
      });
      id = created.recording.id;
      const up = putFile(created.upload, file, (loaded, total) =>
        setPhase((p) => (p.kind === "uploading" ? { ...p, loaded, total } : p)),
      );
      abortRef.current = up.abort;
      await up.promise;
      abortRef.current = null;
      setPhase({ kind: "finishing" });
      await api.uploaded(id);
      onCreated?.();
      router.push(`/recordings/${id}`);
    } catch (e) {
      abortRef.current = null;
      const message = e instanceof Error ? e.message : "The upload failed.";
      if (id) api.uploadFailed(id, message).catch(() => undefined);
      onCreated?.();
      setPhase({ kind: "error", message });
    }
  }

  const tooLong = duration != null && config != null && duration > config.max_duration_min * 60;

  return (
    <section className="upload" aria-labelledby="upload-title">
      <div className="upload-intro">
        <h1 id="upload-title">Turn a recording into notes</h1>
        <p>Upload audio of any length. Gnani transcribes it and an LLM writes a summary. Progress is shown live, and you can leave this page while it works.</p>
      </div>

      {!file ? (
        <label
          className={`drop${over ? " over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}
        >
          <input type="file" accept={config?.extensions.join(",") || "audio/*"} onChange={(e) => pick(e.target.files?.[0])} />
          <strong>Drop an audio file here, or choose one</strong>
          <span className="hint">
            MP3, WAV, M4A, OGG, FLAC and more
            {config ? `, up to ${config.max_upload_mb} MB and ${config.max_duration_min} minutes` : ""}
          </span>
        </label>
      ) : (
        <div className="picked">
          <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true" style={{ color: "var(--signal)", flex: "none" }}>
            {[4, 9, 14, 19, 24].map((x, i) => (
              <rect key={x} x={x - 1.5} y={14 - [4, 9, 12, 7, 3][i]} width="3" height={[4, 9, 12, 7, 3][i] * 2} rx="1.5" fill="currentColor" />
            ))}
          </svg>
          <div>
            <div className="name">{file.name}</div>
            <div className="meta">
              {fmtBytes(file.size)}
              {duration != null && `, ${fmtTime(duration)} long`}
            </div>
          </div>
          {!busy && (
            <button className="btn quiet small" onClick={() => { setFile(null); setPhase({ kind: "idle" }); }}>
              Choose another
            </button>
          )}
        </div>
      )}

      <div className="options">
        <div className="field">
          <label htmlFor="lang">Spoken language</label>
          <select id="lang" value={language} onChange={(e) => setLanguage(e.target.value)} disabled={busy}>
            {(config?.languages || [{ code: "en-IN", name: "English (India)", batch: true }]).map((l) => (
              <option key={l.code} value={l.code}>{l.name}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <span className="label" id="mode-label">Transcription method</span>
          <div className="seg-control" role="group" aria-labelledby="mode-label">
            {(["auto", "chunked", "batch"] as Mode[]).map((m) => (
              <button key={m} type="button" aria-pressed={mode === m} onClick={() => setMode(m)} disabled={busy}>
                {m === "auto" ? "Automatic" : m === "chunked" ? "Chunked" : "Batch"}
              </button>
            ))}
          </div>
        </div>
        <button className="btn" onClick={start} disabled={!file || busy || phase.kind === "error" && !!validate(file)}>
          {busy ? "Uploading…" : "Upload and transcribe"}
        </button>
      </div>
      <p className="mode-note">
        {MODE_NOTES[mode]}
        {mode === "batch" && !batchPossible && " Batch isn't available for this language here, so chunked will be used."}
      </p>

      {tooLong && (
        <div className="notice warn" style={{ marginTop: 14 }}>
          <div>
            This recording is {fmtTime(duration)} long. Recordings over {config!.max_duration_min} minutes will be rejected
            after upload.
          </div>
        </div>
      )}

      {phase.kind === "uploading" && <UploadProgress {...phase} onCancel={() => abortRef.current?.()} />}
      {phase.kind === "finishing" && (
        <p className="upmeta" style={{ marginTop: 12 }}>Upload complete. Queuing for transcription…</p>
      )}
      {phase.kind === "error" && (
        <div className="notice fail" role="alert" style={{ marginTop: 14 }}>
          <div>
            <strong>Upload didn&apos;t go through</strong>
            {phase.message}
            {file && !validate(file) && (
              <div className="actions">
                <button className="btn small" onClick={start}>Try again</button>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function UploadProgress({ loaded, total, startedAt, onCancel }: { loaded: number; total: number; startedAt: number; onCancel: () => void }) {
  const pct = total ? (loaded / total) * 100 : 0;
  const secs = (Date.now() - startedAt) / 1000;
  const rate = secs > 0.5 ? loaded / secs : 0;
  const eta = rate > 0 ? (total - loaded) / rate : null;
  return (
    <div aria-live="polite">
      <div className="upbar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
        <div style={{ width: `${pct}%` }} />
      </div>
      <div className="upmeta">
        <span>
          Uploading {fmtBytes(loaded)} of {fmtBytes(total)}
          {rate > 0 && `, ${fmtBytes(rate)}/s`}
          {eta != null && pct < 100 && `, about ${fmtTime(eta)} left`}
        </span>
        <button className="btn quiet small" onClick={onCancel}>Cancel upload</button>
      </div>
    </div>
  );
}
