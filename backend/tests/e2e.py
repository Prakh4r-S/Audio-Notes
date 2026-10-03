"""Drive the API exactly as the browser does: create -> PUT to signed URL -> mark uploaded -> poll.

Usage: python tests/e2e.py <file> [--mode auto|chunked|batch] [--lang en-IN] [--api http://localhost:8000]
"""

import argparse
import mimetypes
import sys
import time
import uuid
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("file")
p.add_argument("--mode", default="auto")
p.add_argument("--lang", default="en-IN")
p.add_argument("--api", default="http://localhost:8000")
p.add_argument("--client", default="e2e-" + uuid.uuid4().hex[:12])
p.add_argument("--timeout", type=float, default=300)
a = p.parse_args()

path = Path(a.file)
H = {"X-Client-Id": a.client}
api = httpx.Client(base_url=a.api, headers=H, timeout=30)

r = api.post("/api/recordings", json={
    "filename": path.name, "size_bytes": path.stat().st_size,
    "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    "language": a.lang, "mode": a.mode})
if r.status_code != 201:
    print("CREATE REJECTED", r.status_code, r.text)
    sys.exit(0)
rid = r.json()["recording"]["id"]
up = r.json()["upload"]
url = up["url"] if up["url"].startswith("http") else a.api + up["url"]
u = httpx.request(up["method"], url, content=path.read_bytes(), headers=up["headers"], timeout=120)
print("upload", u.status_code)
print("uploaded ->", api.post(f"/api/recordings/{rid}/uploaded").json()["status"])

last = None
t0 = time.time()
while time.time() - t0 < a.timeout:
    d = api.get(f"/api/recordings/{rid}").json()
    line = f"{d['status']:<10} {d['stage']:<12} {d['progress']:5.1f}%  {d['stage_detail'] or ''}"
    if line != last:
        print(f"[{time.time() - t0:5.1f}s] {line}")
        last = line
    if d["status"] in ("completed", "failed"):
        break
    time.sleep(0.7)

print("\n--- events")
for e in d["events"]:
    print(f"  {e['level']:<5} {e['message']}")
print("--- engine:", d["engine"], "| segments:", len(d["segments"]), "| summary:", d["summary_status"], d["summary_model"])
if d["transcript"]:
    print("--- transcript:", d["transcript"][:160], "...")
if d["error_message"]:
    print("--- ERROR:", d["error_code"], d["error_message"], "| retryable:", d["retryable"])
print("RID", rid, "CLIENT", a.client)
