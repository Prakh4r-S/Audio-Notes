"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { UploadPanel } from "@/components/UploadPanel";
import { StatusBadge } from "@/components/StatusBadge";
import {
  api,
  fmtBytes,
  fmtTime,
  fmtWhen,
  isActive,
  type PublicConfig,
  type RecordingSummary,
} from "@/lib/api";

export default function Home() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [items, setItems] = useState<RecordingSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { items } = await api.list();
      setItems(items);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load your recordings.");
    }
  }, []);

  useEffect(() => {
    api.config().then(setConfig).catch(() => undefined);
    load();
  }, [load]);

  // Poll quickly while anything is in flight, slowly otherwise.
  const anyActive = items?.some((r) => isActive(r.status)) ?? false;
  useEffect(() => {
    const t = setInterval(load, anyActive ? 2500 : 15000);
    return () => clearInterval(t);
  }, [anyActive, load]);

  return (
    <>
      <UploadPanel config={config} onCreated={load} />

      <section aria-labelledby="history-title">
        <div className="list-head">
          <h2 id="history-title">Your recordings</h2>
          {items && items.length > 0 && <span>{items.length} in this browser</span>}
        </div>

        {error && (
          <div className="notice fail" role="alert" style={{ marginBottom: 12 }}>
            <div>
              <strong>Couldn&apos;t load your recordings</strong>
              {error}
              <div className="actions"><button className="btn small" onClick={load}>Reload list</button></div>
            </div>
          </div>
        )}

        <div className="rows">
          {items === null && !error && <div className="empty">Loading…</div>}
          {items?.length === 0 && (
            <div className="empty">Nothing here yet. Recordings you upload from this browser will be listed here.</div>
          )}
          {items?.map((r) => (
            <Link key={r.id} href={`/recordings/${r.id}`} className="row">
              <div style={{ minWidth: 0 }}>
                <div className="title">{r.filename}</div>
                <div className="sub">
                  {r.duration_s ? fmtTime(r.duration_s) : fmtBytes(r.size_bytes)}
                  {", "}
                  {config?.languages.find((l) => l.code === r.language)?.name ?? r.language}
                </div>
                {r.status === "completed" && r.preview && (
                  <div className="preview">{r.preview.replace(/[#*_>-]+/g, " ").replace(/\s+/g, " ").replace(/^ ?Overview ?/, "")}</div>
                )}
                {r.status === "failed" && r.error_message && (
                  <div className="preview" style={{ color: "var(--fail)" }}>{r.error_message}</div>
                )}
              </div>
              <div>
                <StatusBadge status={r.status} />
                {isActive(r.status) && (
                  <>
                    <div className="mini"><div style={{ width: `${r.progress}%` }} /></div>
                    <div className="sub">{r.stage_detail ?? ""}</div>
                  </>
                )}
                {r.status === "completed" && r.summary_status === "failed" && (
                  <div className="sub" style={{ color: "var(--warn)" }}>Summary missing</div>
                )}
              </div>
              <div className="when">{fmtWhen(r.created_at)}</div>
            </Link>
          ))}
        </div>
      </section>
    </>
  );
}
