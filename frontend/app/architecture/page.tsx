import type { Metadata } from "next";

export const metadata: Metadata = { title: "Architecture | Audio Notes" };

const REPO = process.env.NEXT_PUBLIC_GITHUB_URL || "https://github.com/";

export default function Architecture() {
  return (
    <article className="doc">
      <h1>How Audio Notes works</h1>
      <p className="lede">
        A recording goes from the browser straight into a storage bucket, a background worker turns it into a transcript
        with Gnani and a summary with an LLM, and the page follows along by polling. This page explains each step and
        the decisions behind it.
      </p>

      <div className="repo">
        <svg width="20" height="20" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
        </svg>
        <div>
          Source code: <a href={REPO}>{REPO.replace(/^https?:\/\//, "")}</a>
        </div>
      </div>

      <figure className="diagram" aria-label="System diagram">
        <SystemDiagram />
      </figure>

      <h2>The stack</h2>
      <table>
        <tbody>
          <tr><td>Frontend</td><td>Next.js on Vercel. It also proxies <code>/api/*</code> to the backend, so the browser talks to one origin and there is no CORS to manage.</td></tr>
          <tr><td>API</td><td>FastAPI on Render, in a Docker image that includes ffmpeg.</td></tr>
          <tr><td>Database</td><td>Postgres on Supabase: recordings, segments, an activity log, and the job queue itself.</td></tr>
          <tr><td>Files</td><td>A private Supabase Storage bucket.</td></tr>
          <tr><td>Background jobs</td><td>A worker that pulls jobs from a Postgres table with <code>FOR UPDATE SKIP LOCKED</code>.</td></tr>
          <tr><td>Speech to text</td><td>Gnani: the synchronous REST endpoint on pre-split chunks, with the Batch API as an alternative and fallback.</td></tr>
          <tr><td>Summary</td><td>An LLM on Groq (Llama 3.3 70B, with automatic fallback to another Groq model if that one is unavailable).</td></tr>
        </tbody>
      </table>

      <h2>From upload to transcript</h2>
      <ol>
        <li>
          The browser asks the API to create a recording. The API validates the extension and size, inserts a row with
          status <code>uploading</code>, and returns a short-lived signed upload URL for the bucket.
        </li>
        <li>
          The browser <code>PUT</code>s the file directly to the bucket with <code>XMLHttpRequest</code>, which gives
          byte-level upload progress. The audio never passes through the API server, so request size limits and timeouts
          on the hosting platform don&apos;t apply to it.
        </li>
        <li>
          The browser tells the API the upload finished. The API checks the object actually exists with the expected
          size, marks the recording <code>queued</code>, and inserts a job row. This request returns immediately.
        </li>
        <li>
          A worker claims the job, downloads the file, runs <code>ffprobe</code>, and fully decodes it to 16 kHz mono WAV.
          Decoding the whole file up front is the corruption check: a damaged or non-audio file fails here with a clear
          message instead of half-way through transcription.
        </li>
        <li>The worker transcribes the audio (see below), stores every segment with its timestamps, and assembles the transcript.</li>
        <li>The transcript is sent to the LLM for a summary, and the recording is marked <code>completed</code>.</li>
      </ol>
      <p>
        At every step the worker writes the current stage, a percentage, a one-line description, and an activity-log
        entry to Postgres. The recording page polls every 1.5 seconds while work is in progress, so a long file shows
        segments filling in rather than a frozen page, and transcript text appears segment by segment before the whole
        job is done.
      </p>

      <h2>Where files live</h2>
      <p>
        The original upload is stored once, at <code>&lt;browser-id&gt;/&lt;recording-id&gt;/source.&lt;ext&gt;</code>{" "}
        in a private bucket. The browser gets time-limited signed URLs to upload it and later to play it back. The
        normalised WAV and the chunks exist only in the worker&apos;s temporary directory and are deleted when the job
        ends, so storage holds exactly one copy of each recording. Transcripts, segments and summaries are rows in
        Postgres. Deleting a recording removes the rows and the stored file.
      </p>

      <h2>Handling long audio</h2>
      <p>
        Gnani&apos;s REST endpoint accepts at most 60 seconds per request and recommends 30 or less, so a two-minute
        recording already needs splitting. I chose to split the audio myself rather than send everything to the Batch
        API by default, because it gives real progress, parallelism, per-piece retries and support for all ten
        languages (Batch lacks Gujarati and Punjabi).
      </p>
      <ul>
        <li>
          <strong>Cutting at pauses.</strong> ffmpeg&apos;s <code>silencedetect</code> finds pauses; the planner aims
          for about 25-second pieces and, within a 12–29 second window, cuts at the pause closest to the target,
          preferring longer pauses. Only if a window has no pause at all does it make a hard cut. This avoids slicing
          words in half, which is the main way chunked transcription loses accuracy.
        </li>
        <li>
          <strong>Parallel, with retries.</strong> Four segments are transcribed at once. Each retries up to four times
          on timeouts, 429s and 5xx responses with exponential backoff and jitter (honouring <code>Retry-After</code>).
          A 401 or 403 stops immediately, since a bad key or exhausted credits won&apos;t fix itself.
        </li>
        <li>
          <strong>Resumable.</strong> Each segment&apos;s text is saved as soon as it returns. If the job is retried or
          the worker crashes, finished segments are skipped, so a failure at minute 40 doesn&apos;t cost the first 39
          minutes again, or the credits.
        </li>
        <li>
          <strong>Batch as the second engine.</strong> In Automatic mode, if segments still fail after their retries,
          the worker submits the whole file to Gnani&apos;s Batch API using a signed URL to the bucket, then polls it every
          10 seconds. Choosing Batch mode reverses the order: Batch first, chunked REST as the fallback. The Batch job
          id is stored, so a restarted worker resumes polling instead of submitting (and paying for) a second job.
        </li>
        <li>
          <strong>Long transcripts for the LLM.</strong> Transcripts under about 24,000 characters are summarised in
          one call. Longer ones are summarised map-reduce style: each section becomes dense notes, and the notes become
          the final summary.
        </li>
      </ul>

      <h2>What runs synchronously and what runs in the background</h2>
      <table>
        <thead>
          <tr><th>Synchronous (milliseconds)</th><th>Background worker</th></tr>
        </thead>
        <tbody>
          <tr>
            <td style={{ whiteSpace: "normal", fontWeight: 400 }}>
              Creating a recording and its signed upload URL; confirming the upload and enqueuing the job; listing and
              reading recordings; retry and delete. The file upload itself runs in the browser, directly against storage.
            </td>
            <td>
              Downloading, probing and decoding the audio; pause detection and chunking; every Gnani call; Batch polling;
              the LLM summary; marking abandoned uploads as failed.
            </td>
          </tr>
        </tbody>
      </table>
      <p>
        The queue is a Postgres table rather than Redis and Celery. A worker claims the oldest runnable job with{" "}
        <code>SELECT … FOR UPDATE SKIP LOCKED</code>, so any number of workers can run without double-processing, and
        jobs are as durable as the data they describe. A running job updates a heartbeat every 10 seconds; if a worker
        dies, the heartbeat goes stale and another worker reclaims the job after 90 seconds. Retryable failures are
        rescheduled with backoff (20 s, then 40 s) up to three attempts.
      </p>
      <p>
        On the free hosting tier the worker runs as threads inside the API process. The same code runs as a separate
        process with <code>python -m app.worker</code> by setting <code>RUN_WORKER_IN_PROCESS=false</code>, which is
        how it should run with paid infrastructure. One consequence of the free tier: the host sleeps after about 15
        minutes without web traffic. While a recording page is open its polling keeps the server awake; if everyone
        closes the tab mid-job and the server sleeps, the job is not lost. It is reclaimed through the stale-heartbeat
        rule on the next wake-up and resumes from its last finished segment.
      </p>

      <h2>Making failure visible</h2>
      <ul>
        <li>Wrong type, empty or oversized files are rejected in the browser before uploading, and again by the API.</li>
        <li>Upload errors (dropped connection, storage rejection, cancel) are shown with a retry button and recorded, so the failed upload appears in the history list instead of vanishing.</li>
        <li>Corrupt or non-audio files fail at the decode step as &ldquo;This file can&apos;t be transcribed&rdquo;, with the reason; truncated files are transcribed as far as they decode, with a warning.</li>
        <li>Temporary provider problems show an amber &ldquo;retrying&rdquo; state with the exact error and the countdown. Permanent ones show what happened and whether retrying can help.</li>
        <li>If only the summary fails, the transcript is still shown, with a &ldquo;Retry summary&rdquo; button.</li>
        <li>Each segment that can&apos;t be transcribed is marked in place in the transcript with its time range, rather than silently dropped.</li>
        <li>Every recording has an activity log of what the system did and when.</li>
        <li>A banner explains when the free-tier server is waking up, which can take up to a minute.</li>
      </ul>

      <h2>Trade-offs and what I&apos;d do differently with more time</h2>
      <ul>
        <li>
          <strong>Accounts.</strong> &ldquo;Your recordings&rdquo; are scoped by a random id stored in the browser. That
          keeps one reviewer&apos;s uploads separate from another&apos;s without sign-up, but it isn&apos;t
          authentication. I would add real auth (Supabase Auth) and row-level security.
        </li>
        <li>
          <strong>Push instead of polling.</strong> Polling every 1.5 s is simple and survives proxies and sleeping
          hosts. Server-sent events fed by Postgres <code>LISTEN/NOTIFY</code> would be more efficient.
        </li>
        <li>
          <strong>Resumable uploads.</strong> A single <code>PUT</code> must restart from zero if the connection drops.
          Supabase supports the TUS protocol for resumable uploads. The free tier also caps objects at 50 MB, which is
          about 50 minutes of typical MP3; I would raise that on a paid plan or transcode to Opus in the browser first.
        </li>
        <li>
          <strong>Better boundaries.</strong> Hard cuts (when a 29-second window has no pause) can split a word. I would
          add a short overlap at hard cuts and de-duplicate the joined words, and use a voice-activity model instead of
          an energy threshold to find pauses in noisy audio.
        </li>
        <li>
          <strong>Speaker labels.</strong> The Batch API supports diarisation for up to two speakers; exposing it would
          make meeting transcripts much more readable.
        </li>
        <li>
          <strong>Operations.</strong> Alembic migrations instead of create-on-boot, a dedicated worker service, rate
          limiting per browser to protect ASR credits, structured logs and alerting on failed jobs, and automated tests
          in CI against the mock providers that already exist in the repository.
        </li>
      </ul>
    </article>
  );
}

function SystemDiagram() {
  const box = (x: number, y: number, w: number, h: number, title: string, sub: string, accent = false) => (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={6} fill="var(--surface)" stroke={accent ? "var(--signal)" : "var(--line)"} strokeWidth={accent ? 1.5 : 1} />
      <text x={x + w / 2} y={y + h / 2 - 4} textAnchor="middle" fontSize={14} fontWeight={600} fill="var(--ink)">{title}</text>
      <text x={x + w / 2} y={y + h / 2 + 14} textAnchor="middle" fontSize={11.5} fill="var(--ink-3)">{sub}</text>
    </g>
  );
  const arrow = (d: string, label?: string, lx?: number, ly?: number, dashed = false) => (
    <g>
      <path d={d} fill="none" stroke="var(--ink-3)" strokeWidth={1.2} strokeDasharray={dashed ? "4 4" : undefined} markerEnd="url(#ah)" />
      {label && <text x={lx} y={ly} fontSize={11.5} fill="var(--ink-2)" textAnchor="middle">{label}</text>}
    </g>
  );
  return (
    <svg viewBox="0 0 760 400" role="img" aria-label="Browser uploads to storage and calls the API; the API writes jobs to Postgres; the worker claims jobs, reads the file, calls Gnani and Groq, and writes results back.">
      <defs>
        <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--ink-3)" />
        </marker>
      </defs>

      {box(20, 30, 170, 60, "Browser", "Next.js on Vercel", true)}
      {box(300, 30, 170, 60, "FastAPI", "Render, Docker + ffmpeg")}
      {box(570, 30, 170, 60, "Storage bucket", "Supabase, private")}
      {box(300, 170, 170, 60, "Postgres", "recordings, segments, jobs")}
      {box(300, 310, 170, 60, "Worker", "claims jobs, heartbeats", true)}
      {box(570, 250, 170, 50, "Gnani ASR", "REST chunks or Batch")}
      {box(570, 330, 170, 50, "Groq LLM", "summary")}

      {arrow("M190 52 L298 52", "create, poll", 244, 44)}
      {arrow("M105 30 C105 0, 650 0, 650 28", "PUT file to signed URL", 380, 12)}
      {arrow("M385 90 L385 168", "insert job", 420, 134)}
      {arrow("M385 308 L385 232")}
      <text x={377} y={268} fontSize={11.5} fill="var(--ink-2)" textAnchor="end">claim job (SKIP LOCKED),</text>
      <text x={377} y={283} fontSize={11.5} fill="var(--ink-2)" textAnchor="end">write progress</text>
      {arrow("M470 318 C520 318, 515 130, 598 92", undefined, 0, 0, true)}
      <text x={482} y={200} fontSize={11.5} fill="var(--ink-2)">download</text>
      {arrow("M470 335 L568 280")}
      {arrow("M470 352 L568 355")}
      {arrow("M690 250 L690 94", undefined, 0, 0, true)}
      <text x={698} y={175} fontSize={11.5} fill="var(--ink-2)">Batch fetches</text>
      <text x={698} y={190} fontSize={11.5} fill="var(--ink-2)">signed URL</text>
    </svg>
  );
}
