"""Database models and session handling (SQLAlchemy 2.0, psycopg 3)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_key: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(10))
    mode: Mapped[str] = mapped_column(String(16))  # auto | chunked | batch

    # uploading -> queued -> processing -> completed | failed
    status: Mapped[str] = mapped_column(String(16), default="uploading", index=True)
    # upload | validating | preparing | transcribing | summarizing | done
    stage: Mapped[str] = mapped_column(String(16), default="upload")
    stage_detail: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100
    duration_s: Mapped[float | None] = mapped_column(Float)
    engine: Mapped[str | None] = mapped_column(String(16))  # rest | batch
    gnani_batch_job_id: Mapped[str | None] = mapped_column(String(64))
    chunks_total: Mapped[int] = mapped_column(Integer, default=0)
    chunks_done: Mapped[int] = mapped_column(Integer, default=0)
    chunks_failed: Mapped[int] = mapped_column(Integer, default=0)

    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|done|failed
    summary_error: Mapped[str | None] = mapped_column(Text)
    summary_model: Mapped[str | None] = mapped_column(String(64))

    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool | None] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column()


class Job(Base):
    """A unit of background work. Claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # process | summarize
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)


class Chunk(Base):
    """A transcribed slice of audio (REST chunk, or a segment returned by the Batch API)."""

    __tablename__ = "chunks"

    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|done|failed
    text: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    """Human-readable activity log shown in the UI."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warn | error
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    # prepare_threshold=None keeps psycopg compatible with PgBouncer-style poolers.
    connect_args={"prepare_threshold": None} if _settings.database_url.startswith("postgresql") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def add_event(recording_id: uuid.UUID, message: str, level: str = "info") -> None:
    with session_scope() as s:
        s.add(Event(recording_id=recording_id, message=message, level=level))
