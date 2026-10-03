# Audio Notes

Upload an audio file of any length and get back a transcript (Gnani ASR) and a summary (LLM on Groq). Past uploads are listed and can be reopened.

- **Frontend:** Next.js 16 (`frontend/`), deployed on Vercel
- **Backend:** FastAPI (`backend/`), deployed on Render with Docker (ffmpeg included)
- **Database:** Postgres (Supabase)
- **Files:** Supabase Storage, private bucket, browser uploads directly via signed URLs
- **Background jobs:** Postgres-backed queue (`SELECT … FOR UPDATE SKIP LOCKED`) with heartbeats, stale-job reclaim and backoff retries

The in-app `/architecture` page explains the design in full: the upload-to-transcript flow, where files live, how long audio is handled, what is synchronous vs background, and what I would change with more time.

## How long audio is handled

Gnani's REST endpoint takes at most 60 s per request (30 s ideal). The worker decodes the upload to 16 kHz mono WAV, finds pauses with ffmpeg `silencedetect`, and cuts ~25 s segments at pauses (hard cut only if a 12–29 s window has no pause). Segments are transcribed four at a time with per-segment retries, and each result is saved immediately so retries and crashes resume where they stopped. If the REST path keeps failing, Automatic mode falls back to Gnani's **Batch API** (which reads the file from a signed bucket URL); Batch mode does the reverse.

## Repository layout

```
backend/
  app/
    main.py        HTTP API (all endpoints return in milliseconds)
    worker.py      Postgres job queue: claim, heartbeat, retry, reclaim
    pipeline.py    validate -> chunk -> transcribe (REST or Batch, with fallback) -> summarise
    audio.py       ffprobe / ffmpeg decoding, pause detection, chunk planning
    gnani.py       Gnani REST + Batch clients with error classification
    summarize.py   Groq summaries, model fallback, map-reduce for long transcripts
    storage.py     Supabase Storage (prod) / local disk (dev), signed URLs
    db.py          SQLAlchemy models: recordings, chunks, jobs, events
  tests/
    test_chunking.py   unit tests for the chunk planner
    mock_providers.py  fake Gnani + Groq + Supabase Storage with failure injection
    e2e.py             drives the API exactly like the browser does
frontend/
  app/page.tsx                   upload + history
  app/recordings/[id]/page.tsx   live progress, transcript, summary, activity log
  app/architecture/page.tsx      the architecture write-up
render.yaml                      Render blueprint for the API
```

## Deploying (about 20 minutes)

You need free accounts on GitHub, Supabase, Render, Vercel, Gnani and Groq.

### 1. Push to GitHub
```bash
cd audio-notes
git remote add origin https://github.com/<you>/audio-notes.git
git push -u origin main
```

### 2. Supabase (database + bucket)
1. Create a project (region: Mumbai `ap-south-1` is closest to Gnani).
2. **Storage → New bucket**: name `recordings`, **private**. Under bucket settings, set the file size limit to 50 MB (the free-tier maximum).
3. Click **Connect** in the project's top bar and copy the **Session pooler** connection string. Change `postgresql://` to `postgresql+psycopg://`. Use the *session* pooler (port 5432): Render has no IPv6, so the direct connection does not work, and the transaction pooler is not needed.
4. **Project Settings → API**: copy the Project URL and the `service_role` key.

Tables are created automatically on first boot.

### 3. API keys
- **Gnani:** sign up at gnani.ai, create an API key in the dashboard.
- **Groq:** console.groq.com → API Keys.

### 4. Render (API + worker)
1. **New → Blueprint**, select the repo. Render reads `render.yaml`.
2. Fill in the secret env vars: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GNANI_API_KEY`, `GROQ_API_KEY`.
3. Deploy, then open `https://<service>.onrender.com/api/health`. It should return `{"ok": true}`.

### 5. Vercel (frontend)
1. **Add New → Project**, import the repo, set **Root Directory** to `frontend`.
2. Environment variables:
   - `BACKEND_URL` = `https://<service>.onrender.com`
   - `NEXT_PUBLIC_GITHUB_URL` = your repo URL (linked from `/architecture`)
3. Deploy. The Vercel URL is the one to submit.

### 6. Smoke test
Upload a 2–3 minute recording. You should see the segment timeline fill in, text appear segment by segment, then the summary. Try a renamed non-audio file to see the failure path.

## Running locally

```bash
# Postgres
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=audionotes postgres:16

# Backend (needs ffmpeg on PATH)
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set DATABASE_URL, keys; STORAGE_BACKEND=local works without Supabase
uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
BACKEND_URL=http://localhost:8000 npm run dev
```

With `STORAGE_BACKEND=local`, files are kept on disk and the Batch API is unavailable (Gnani has to fetch from a public URL), so Automatic mode uses chunked REST only.

### Testing without real API keys

`tests/mock_providers.py` imitates Gnani (REST and Batch), Groq and Supabase Storage, with switchable failures.

```bash
cd backend
uvicorn tests.mock_providers:app --port 9100 &
set -a; . ./.env.mock; set +a
uvicorn app.main:app --port 8000 &

python -m pytest tests/test_chunking.py
python tests/e2e.py path/to/audio.mp3                    # happy path
curl -XPOST localhost:9100/_control -d '{"rest_fail_rate":0.4}'   # flaky ASR: retries absorb it
curl -XPOST localhost:9100/_control -d '{"rest_fail_rate":1.0}'   # ASR down: falls back to Batch
curl -XPOST localhost:9100/_control -d '{"groq_down":true}'       # summary fails, transcript kept
```

Scenarios verified this way: 3.5-minute MP3 end to end; 40% ASR failure rate; REST outage with Batch fallback; Batch failure with REST fallback; Gujarati in Batch mode (falls back to REST, which supports it); corrupt and non-audio files; unsupported extensions; LLM model decommissioned (falls to next model); LLM outage with manual summary retry; manual retry after an outage resumes finished segments; worker killed mid-job is reclaimed and resumes; recordings are invisible to other browsers.

## Configuration reference

See `backend/.env.example`. The most relevant knobs:

| Variable | Default | Purpose |
|---|---|---|
| `MAX_UPLOAD_MB` | 50 | Upload size cap (Supabase free tier maximum) |
| `MAX_DURATION_MIN` | 180 | Duration cap, protects ASR credits |
| `CHUNK_TARGET_S` / `CHUNK_MAX_S` | 25 / 29 | Segment length for the REST path |
| `CHUNK_CONCURRENCY` | 4 | Parallel Gnani requests per recording |
| `GROQ_MODELS` | `llama-3.3-70b-versatile,openai/gpt-oss-120b,llama-3.1-8b-instant` | Tried in order |
| `RUN_WORKER_IN_PROCESS` | true | Worker threads inside the API (free hosting) |
