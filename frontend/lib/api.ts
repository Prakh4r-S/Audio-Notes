// Typed client for the FastAPI backend (proxied at /api by next.config.mjs).

export type Status = "uploading" | "queued" | "processing" | "retrying" | "completed" | "failed";

export interface RecordingSummary {
  id: string;
  filename: string;
  size_bytes: number;
  language: string;
  mode: "auto" | "chunked" | "batch";
  status: Status;
  stage: string;
  stage_detail: string | null;
  progress: number;
  duration_s: number | null;
  engine: "rest" | "batch" | null;
  chunks_total: number;
  chunks_done: number;
  chunks_failed: number;
  summary_status: "pending" | "running" | "done" | "failed";
  error_code: string | null;
  error_message: string | null;
  retryable: boolean | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  preview: string | null;
}

export interface Segment {
  idx: number;
  start: number;
  end: number;
  status: "pending" | "running" | "done" | "failed";
  text: string | null;
  attempts: number;
  error: string | null;
}

export interface Recording extends RecordingSummary {
  transcript: string | null;
  summary: string | null;
  summary_error: string | null;
  summary_model: string | null;
  segments: Segment[];
  events: { level: "info" | "warn" | "error"; message: string; at: string }[];
  audio_url: string | null;
}

export interface PublicConfig {
  max_upload_mb: number;
  max_duration_min: number;
  languages: { code: string; name: string; batch: boolean }[];
  batch_available: boolean;
  extensions: string[];
  llm_models: string[];
}

export interface UploadTarget {
  url: string;
  method: string;
  headers: Record<string, string>;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

const CLIENT_KEY = "audio-notes-client-id";

/** A random id per browser; scopes "your recordings". Not authentication. */
export function clientId(): string {
  let id = "";
  try {
    id = localStorage.getItem(CLIENT_KEY) || "";
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(CLIENT_KEY, id);
    }
  } catch {
    id = (globalThis as { __cid?: string }).__cid ||= crypto.randomUUID();
  }
  return id;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", "X-Client-Id": clientId(), ...(init.headers || {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError("Can't reach the server. Check your connection; the page will keep trying.", 0);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON, e.g. a proxy error page */
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : res.status >= 500
          ? "The server hit a problem. Please try again in a moment."
          : `Request failed (${res.status}).`;
    throw new ApiError(msg, res.status);
  }
  return body as T;
}

export const api = {
  config: () => request<PublicConfig>("/api/config"),
  health: () => request<{ ok: boolean }>("/api/health"),
  list: () => request<{ items: RecordingSummary[] }>("/api/recordings"),
  get: (id: string) => request<Recording>(`/api/recordings/${id}`),
  create: (body: { filename: string; size_bytes: number; content_type: string; language: string; mode: string }) =>
    request<{ recording: { id: string }; upload: UploadTarget }>("/api/recordings", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  uploaded: (id: string) => request<RecordingSummary>(`/api/recordings/${id}/uploaded`, { method: "POST" }),
  uploadFailed: (id: string, message: string) =>
    request<RecordingSummary>(`/api/recordings/${id}/upload-failed`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  retry: (id: string) => request<{ ok: boolean }>(`/api/recordings/${id}/retry`, { method: "POST" }),
  remove: (id: string) => request<void>(`/api/recordings/${id}`, { method: "DELETE" }),
};

/** PUT a file to a signed URL with byte-level progress. Resolves when stored. */
export function putFile(
  target: UploadTarget,
  file: File,
  onProgress: (loaded: number, total: number) => void,
): { promise: Promise<void>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<void>((resolve, reject) => {
    xhr.open(target.method, target.url);
    for (const [k, v] of Object.entries(target.headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded, e.total);
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`Storage rejected the upload (HTTP ${xhr.status}).`));
    xhr.onerror = () => reject(new Error("The connection dropped during the upload."));
    xhr.ontimeout = () => reject(new Error("The upload timed out."));
    xhr.onabort = () => reject(new Error("Upload cancelled."));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}

// ---------------------------------------------------------------- formatting
export function fmtTime(sec: number | null | undefined): string {
  if (sec == null || !isFinite(sec)) return "–";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

export function fmtBytes(n: number): string {
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtWhen(iso: string): string {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export const isActive = (s: Status) => s === "uploading" || s === "queued" || s === "processing" || s === "retrying";

export const STATUS_LABEL: Record<Status, string> = {
  uploading: "Uploading",
  queued: "Queued",
  processing: "Processing",
  retrying: "Retrying",
  completed: "Ready",
  failed: "Failed",
};
