"""Database models and session management."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from .config_loader import get_config

Base = declarative_base()


class Video(Base):
    """Original uploaded videos."""

    __tablename__ = "videos"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String(500), nullable=False)
    original_filename = Column(String(500), nullable=False)
    s3_key = Column(String(1000), nullable=False, unique=True)
    s3_bucket = Column(String(255), nullable=False, default="per-aspera-brain")
    file_size_bytes = Column(BigInteger)
    duration_seconds = Column(Numeric(10, 2))
    resolution = Column(String(50))
    format = Column(String(20))
    status = Column(String(50), default="uploaded")
    uploaded_by = Column(String(255))
    extra_data = Column("metadata", JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Metadata fields
    speaker = Column(String(255))
    event_name = Column(String(255))
    event_date = Column(Date)
    description = Column(Text)

    # Relationships
    transcripts = relationship("Transcript", back_populates="video", cascade="all, delete-orphan")
    clips = relationship("Clip", back_populates="source_video", cascade="all, delete-orphan")


class Transcript(Base):
    """Transcriptions of videos."""

    __tablename__ = "transcripts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id = Column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    s3_key = Column(String(1000), nullable=False, unique=True)
    provider = Column(String(50), default="aws")
    language = Column(String(20), default="en-US")
    full_text = Column(Text)
    word_count = Column(Integer)
    status = Column(String(50), default="pending")
    error_message = Column(Text)
    extra_data = Column("metadata", JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    video = relationship("Video", back_populates="transcripts")
    segments = relationship("TranscriptSegment", back_populates="transcript", cascade="all, delete-orphan")


class TranscriptSegment(Base):
    """Individual timestamped segments of transcripts."""

    __tablename__ = "transcript_segments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transcript_id = Column(UUID(as_uuid=True), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False)
    segment_index = Column(Integer, nullable=False)
    start_time = Column(Numeric(10, 3), nullable=False)
    end_time = Column(Numeric(10, 3), nullable=False)
    text = Column(Text, nullable=False)
    confidence = Column(Numeric(5, 4))
    speaker = Column(String(100))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationships
    transcript = relationship("Transcript", back_populates="segments")


class Clip(Base):
    """Segments cut from source videos."""

    __tablename__ = "clips"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_video_id = Column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    clip_name = Column(String(500), nullable=False)
    s3_key = Column(String(1000), unique=True)
    start_time = Column(Numeric(10, 3), nullable=False)
    end_time = Column(Numeric(10, 3), nullable=False)
    status = Column(String(50), default="pending")
    file_size_bytes = Column(BigInteger)
    notes = Column(Text)
    created_by = Column(String(255))
    extra_data = Column("metadata", JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    source_video = relationship("Video", back_populates="clips")
    compiled_video_clips = relationship("CompiledVideoClip", back_populates="clip")

    @property
    def duration_seconds(self) -> Decimal:
        return self.end_time - self.start_time


class CompiledVideo(Base):
    """Final videos assembled from clips."""

    __tablename__ = "compiled_videos"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    description = Column(Text)
    s3_key = Column(String(1000), unique=True)
    total_duration_seconds = Column(Numeric(10, 2))
    file_size_bytes = Column(BigInteger)
    resolution = Column(String(50))
    status = Column(String(50), default="pending")
    created_by = Column(String(255))
    extra_data = Column("metadata", JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    compiled_video_clips = relationship(
        "CompiledVideoClip", back_populates="compiled_video", cascade="all, delete-orphan"
    )


class CompiledVideoClip(Base):
    """Junction table: clips in compiled videos with ordering."""

    __tablename__ = "compiled_video_clips"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    compiled_video_id = Column(
        UUID(as_uuid=True), ForeignKey("compiled_videos.id", ondelete="CASCADE"), nullable=False
    )
    clip_id = Column(UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False)
    sequence_order = Column(Integer, nullable=False)
    transition_type = Column(String(50), default="cut")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("compiled_video_id", "sequence_order"),)

    # Relationships
    compiled_video = relationship("CompiledVideo", back_populates="compiled_video_clips")
    clip = relationship("Clip", back_populates="compiled_video_clips")


class ProcessingJob(Base):
    """Track async processing operations."""

    __tablename__ = "processing_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type = Column(String(50), nullable=False)
    reference_id = Column(UUID(as_uuid=True), nullable=False)
    reference_type = Column(String(50), nullable=False)
    aws_job_id = Column(String(500))
    status = Column(String(50), default="queued")
    progress = Column(Integer, default=0)
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


# Database session management
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the database engine."""
    global _engine
    if _engine is None:
        config = get_config()
        _engine = create_engine(config.db_connection_string, pool_pre_ping=True)
    return _engine


def get_session_factory():
    """Get the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def get_session() -> Session:
    """Get a new database session."""
    SessionLocal = get_session_factory()
    return SessionLocal()


class DatabaseSession:
    """Context manager for database sessions."""

    def __init__(self):
        self.session: Optional[Session] = None

    def __enter__(self) -> Session:
        self.session = get_session()
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            if exc_type:
                self.session.rollback()
            else:
                self.session.commit()
            self.session.close()


def init_db():
    """Initialize database tables (use SQL script for production)."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
