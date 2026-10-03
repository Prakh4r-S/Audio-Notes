"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * The API runs on a free host that sleeps when idle; the first request can take
 * up to a minute. Instead of a page that silently hangs, say so.
 */
export function ServerStatus() {
  const [state, setState] = useState<"ok" | "waking" | "down">("ok");

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const slow = setTimeout(() => !cancelled && setState((s) => (s === "ok" ? "waking" : s)), 2500);

    const ping = async () => {
      try {
        await api.health();
        if (!cancelled) setState("ok");
        clearTimeout(slow);
      } catch {
        attempts += 1;
        if (cancelled) return;
        setState(attempts >= 8 ? "down" : "waking");
        setTimeout(ping, 5000);
      }
    };
    ping();
    return () => {
      cancelled = true;
      clearTimeout(slow);
    };
  }, []);

  if (state === "ok") return null;
  return (
    <div className={`notice ${state === "down" ? "fail" : "info"}`} role="status" style={{ marginBottom: 20 }}>
      <div>
        {state === "waking" ? (
          <>
            <strong>Starting the server</strong>
            The backend sleeps when nobody is using it. Waking it can take up to a minute; this page will continue on its own.
          </>
        ) : (
          <>
            <strong>The server is not responding</strong>
            Uploads and history are unavailable right now. Still retrying every few seconds.
          </>
        )}
      </div>
    </div>
  );
}
