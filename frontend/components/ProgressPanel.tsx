"use client";

import { useEffect, useState } from "react";
import { fmtTime, type Recording } from "@/lib/api";

const STEPS = [
  { key: "upload", label: "Upload" },
  { key: "check", label: "Check audio" },
  { key: "transcribe", label: "Transcribe" },
  { key: "summarize", label: "Summarise" },
];

function stepIndex(stage: string): number {
  switch (stage) {
    case "upload": return 0;
    case "validating":
    case "preparing": return 1;
    case "transcribing": return 2;
    case "summarizing": return 3;
    default: return 4;
  }
}

function headline(r: Recording): string {
  if (r.status === "queued") return "Waiting for a free worker";
  if (r.status === "retrying") return "Paused after a temporary problem";
  if (r.status === "failed") return "Stopped at this step";
  switch (r.stage) {
    case "validating": return "Checking the file";
    case "preparing": return "Preparing the audio";
    case "transcribing": return r.engine === "batch" ? "Transcribing with Gnani Batch" : "Transcribing with Gnani";
    case "summarizing": return "Writing the summary";
    default: return "Working";
  }
}

export function ProgressPanel({ r }: { r: Recording }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const current = stepIndex(r.stage);
  const elapsed = (now - new Date(r.created_at).getTime()) / 1000;
  const stale = r.status === "processing" && now - new Date(r.updated_at).getTime() > 120_000;

  return (
    <section className="progress-panel" aria-label="Processing progress">
      <ol className="steps">
        {STEPS.map((s, i) => {
          const state = i < current ? "done" : i === current ? (r.status === "failed" ? "failed" : "current") : "";
          return (
            <li key={s.key} className={state} aria-current={state === "current" ? "step" : undefined}>
              <span className="dot">{state === "done" ? "✓" : state === "failed" ? "!" : i + 1}</span>
              <span className="lbl">{s.label}</span>
            </li>
          );
        })}
      </ol>

      <div className="now" aria-live="polite">
        <div>
          <div className="what">{headline(r)}</div>
          <div style={{ color: "var(--ink-2)", fontSize: "0.92rem" }}>{r.stage_detail ?? ""}</div>
        </div>
        {r.status !== "failed" && <div style={{ textAlign: "right" }}>
          <div className="pct">{Math.floor(r.progress)}%</div>
          <div style={{ color: "var(--ink-3)", fontSize: "0.82rem" }}>{fmtTime(elapsed)} since upload</div>
        </div>}
      </div>

      {(r.status !== "failed" || r.segments.length > 0) && <Timeline r={r} />}

      {stale && (
        <div className="notice warn" style={{ marginTop: 16 }}>
          <div>
            No progress for over two minutes. If the worker stopped, the job will be picked up again automatically
            within a couple of minutes.
          </div>
        </div>
      )}
    </section>
  );
}

function Timeline({ r }: { r: Recording }) {
  const total = r.duration_s ?? 0;
  const restSegments = r.engine === "rest" && r.segments.length > 0;

  return (
    <div>
      <div className="timeline" aria-hidden={!restSegments}>
        {restSegments ? (
          r.segments.map((s) => {
            const retrying = s.status === "pending" && s.error;
            return (
              <div
                key={s.idx}
                className={`seg ${retrying ? "retry" : s.status}`}
                style={{ flexGrow: Math.max(0.5, s.end - s.start), flexBasis: 0 }}
                title={`${fmtTime(s.start)}–${fmtTime(s.end)}: ${
                  s.status === "done" ? "transcribed" : s.status === "failed" ? `failed: ${s.error}` : retrying ? s.error : s.status
                }${s.attempts > 1 ? ` (attempt ${s.attempts})` : ""}`}
              />
            );
          })
        ) : (
          <div className={`whole${r.status === "failed" ? "" : " indeterminate"}`}>
            <div style={{ width: `${Math.max(0, r.progress)}%` }} />
          </div>
        )}
      </div>
      <div className="timeline-axis">
        <span>0:00</span>
        {total > 0 && <span>{fmtTime(total / 2)}</span>}
        <span>{total > 0 ? fmtTime(total) : ""}</span>
      </div>
      {restSegments ? (
        <div className="timeline-legend">
          <span>
            {r.chunks_done} of {r.chunks_total} segments transcribed
            {r.chunks_failed > 0 && `, ${r.chunks_failed} failed`}
          </span>
          <span><i style={{ background: "var(--signal)" }} />Done</span>
          <span><i style={{ background: "var(--signal-soft)", outline: "1px solid var(--signal)" }} />In flight</span>
          <span><i style={{ background: "var(--warn)" }} />Retrying</span>
          <span><i style={{ background: "var(--fail)" }} />Failed</span>
          <span><i style={{ background: "var(--pending)" }} />Waiting</span>
        </div>
      ) : (
        r.engine === "batch" && (
          <div className="timeline-legend">
            <span>Gnani processes the whole file at once, so this bar is an estimate based on the recording&apos;s length.</span>
          </div>
        )
      )}
    </div>
  );
}
