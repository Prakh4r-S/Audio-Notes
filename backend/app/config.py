"""Runtime configuration, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database -----------------------------------------------------------
    # Any SQLAlchemy URL. For Supabase use the *session* pooler (port 5432),
    # e.g. postgresql+psycopg://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/audionotes"

    # --- Storage ------------------------------------------------------------
    storage_backend: str = "local"  # "supabase" | "local"
    local_storage_dir: str = "./.storage"
    supabase_url: str = ""  # https://<ref>.supabase.co
    supabase_service_key: str = ""  # service_role key (server-side only)
    supabase_bucket: str = "recordings"
    # Used to sign local upload/download URLs (local backend only).
    secret_key: str = "dev-secret-change-me"

    # --- Gnani ASR ----------------------------------------------------------
    gnani_api_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_batch_model: str = "gnani-prisma-v2.5"
    gnani_rest_timeout_s: float = 60.0
    # Chunking for the synchronous REST endpoint (hard limit 60 s, ideal <= 30 s).
    chunk_target_s: float = 25.0
    chunk_min_s: float = 12.0
    chunk_max_s: float = 29.0
    chunk_concurrency: int = 4
    chunk_max_attempts: int = 4
    # Batch API polling.
    batch_poll_interval_s: float = 10.0
    batch_min_timeout_s: float = 900.0

    # --- LLM (Groq, OpenAI-compatible) --------------------------------------
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Tried in order; the first model the account can use wins.
    groq_models: str = "llama-3.3-70b-versatile,openai/gpt-oss-120b,llama-3.1-8b-instant"
    groq_timeout_s: float = 90.0
    summary_chunk_chars: int = 24000

    # --- Limits & behaviour -------------------------------------------------
    max_upload_mb: int = 50  # Supabase free tier caps objects at 50 MB
    max_duration_min: int = 180  # protects ASR credits
    cors_origins: str = "*"
    # Run the queue worker inside the API process (for hosts without a free
    # background-worker tier). Set to false when running `python -m app.worker`.
    run_worker_in_process: bool = True
    worker_concurrency: int = 2
    job_max_attempts: int = 3
    heartbeat_interval_s: float = 10.0
    stale_after_s: float = 90.0
    upload_abandon_after_s: float = 3600.0

    @property
    def groq_model_list(self) -> list[str]:
        return [m.strip() for m in self.groq_models.split(",") if m.strip()]

    @property
    def batch_available(self) -> bool:
        # The Batch API pulls audio from a public HTTPS URL, which needs a cloud bucket.
        return self.storage_backend == "supabase" and bool(self.gnani_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Gnani language support (REST supports all ten; Batch lacks gu-IN and pa-IN).
LANGUAGES: dict[str, str] = {
    "en-IN": "English (India)",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}
BATCH_LANGUAGES = {k for k in LANGUAGES if k not in {"gu-IN", "pa-IN"}}

ALLOWED_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mp4", ".amr", ".wma", ".3gp",
}
