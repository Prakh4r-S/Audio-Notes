"""Object storage. Browsers upload straight to the bucket via short-lived signed URLs,
so large files never pass through the API server.

Backends:
  * SupabaseStorage - production (Supabase Storage bucket, private).
  * LocalStorage    - development/tests; files on disk, served through the API.
"""

from __future__ import annotations

import hashlib
import hmac
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import get_settings
from .errors import StorageError


class UploadTarget(dict):
    """{"url": ..., "method": "PUT", "headers": {...}} handed to the browser."""


class Storage:
    def create_upload_target(self, key: str, content_type: str) -> UploadTarget: ...
    def object_size(self, key: str) -> int | None: ...
    def download_to(self, key: str, dest: Path) -> None: ...
    def signed_download_url(self, key: str, expires_s: int = 3600) -> str | None: ...
    def public_fetch_url(self, key: str, expires_s: int = 7200) -> str | None:
        """An HTTPS URL a third party (Gnani Batch) can fetch, or None if impossible."""
        return None
    def delete_prefix(self, prefix: str) -> None: ...


# ---------------------------------------------------------------------------
class LocalStorage(Storage):
    def __init__(self, root: str, secret: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode()

    def path_for(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise StorageError("Invalid storage key", retryable=False)
        return p

    def sign(self, key: str, action: str, expires: int) -> str:
        msg = f"{action}:{key}:{expires}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def verify(self, key: str, action: str, expires: int, token: str) -> bool:
        return expires > time.time() and hmac.compare_digest(self.sign(key, action, expires), token)

    def _url(self, key: str, action: str, expires_s: int) -> str:
        exp = int(time.time()) + expires_s
        route = "local-upload" if action == "put" else "local-files"
        return f"/api/{route}/{quote(key)}?expires={exp}&token={self.sign(key, action, exp)}"

    def create_upload_target(self, key, content_type):
        return UploadTarget(url=self._url(key, "put", 3600), method="PUT", headers={"Content-Type": content_type})

    def object_size(self, key):
        p = self.path_for(key)
        return p.stat().st_size if p.exists() else None

    def download_to(self, key, dest):
        p = self.path_for(key)
        if not p.exists():
            raise StorageError("The uploaded file is missing from storage.", retryable=False)
        shutil.copyfile(p, dest)

    def signed_download_url(self, key, expires_s=3600):
        return self._url(key, "get", expires_s)

    def delete_prefix(self, prefix):
        shutil.rmtree(self.path_for(prefix), ignore_errors=True)


# ---------------------------------------------------------------------------
class SupabaseStorage(Storage):
    def __init__(self, url: str, service_key: str, bucket: str):
        if not url or not service_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for supabase storage")
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def _client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(headers=self.headers, timeout=timeout)

    def _path(self, key: str) -> str:
        return f"{self.bucket}/{quote(key)}"

    def create_upload_target(self, key, content_type):
        with self._client() as c:
            r = c.post(f"{self.base}/object/upload/sign/{self._path(key)}", json={}, headers={"x-upsert": "true"})
        if r.status_code >= 300:
            raise StorageError(f"Could not create an upload URL (storage returned {r.status_code}).")
        return UploadTarget(
            url=self.base + r.json()["url"],
            method="PUT",
            headers={"Content-Type": content_type or "application/octet-stream", "x-upsert": "true"},
        )

    def object_size(self, key):
        with self._client() as c:
            r = c.get(f"{self.base}/object/info/{self._path(key)}")
        if r.status_code == 200:
            data = r.json()
            size = data.get("size") or (data.get("metadata") or {}).get("size")
            return int(size) if size is not None else 0
        if r.status_code in (400, 404):
            return None
        raise StorageError(f"Storage check failed ({r.status_code}).")

    def download_to(self, key, dest):
        try:
            with self._client(timeout=600) as c, c.stream("GET", f"{self.base}/object/{self._path(key)}") as r:
                if r.status_code in (400, 404):
                    raise StorageError("The uploaded file is missing from storage.", retryable=False)
                if r.status_code >= 300:
                    raise StorageError(f"Could not read the file from storage ({r.status_code}).")
                with open(dest, "wb") as f:
                    for block in r.iter_bytes(1 << 20):
                        f.write(block)
        except httpx.HTTPError as e:
            raise StorageError(f"Network error while reading from storage: {e.__class__.__name__}") from e

    def signed_download_url(self, key, expires_s=3600):
        with self._client() as c:
            r = c.post(f"{self.base}/object/sign/{self._path(key)}", json={"expiresIn": expires_s})
        if r.status_code >= 300:
            return None
        return self.base + r.json()["signedURL"]

    def public_fetch_url(self, key, expires_s=7200):
        return self.signed_download_url(key, expires_s)

    def delete_prefix(self, prefix):
        with self._client() as c:
            r = c.post(f"{self.base}/object/list/{self.bucket}", json={"prefix": prefix, "limit": 100})
            names = [f"{prefix.rstrip('/')}/{o['name']}" for o in (r.json() if r.status_code == 200 else [])]
            if names:
                c.request("DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": names})


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "supabase":
            _storage = SupabaseStorage(s.supabase_url, s.supabase_service_key, s.supabase_bucket)
        else:
            _storage = LocalStorage(s.local_storage_dir, s.secret_key)
    return _storage
