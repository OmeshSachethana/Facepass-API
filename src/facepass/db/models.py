import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from facepass.core.security import utcnow

EMBEDDING_DIMENSIONS = 128


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Application(Base):
    """A tenant: one integrating product. All biometric data is scoped to an application."""

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255))
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subject(Base):
    """A person known to an application. `external_id` is the application's own user id."""

    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("application_id", "external_id"),
        # Target of the composite foreign key on face_templates.
        UniqueConstraint("id", "application_id"),
        Index("ix_subjects_listing", "application_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE")
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()


class FaceTemplate(Base):
    """A stored face embedding. The source photo is discarded after processing."""

    __tablename__ = "face_templates"
    __table_args__ = (
        # Composite so a template can never point at another application's subject.
        ForeignKeyConstraint(
            ["subject_id", "application_id"],
            ["subjects.id", "subjects.application_id"],
            ondelete="CASCADE",
            name="fk_face_templates_subject_application",
        ),
        Index("ix_face_templates_subject_id", "subject_id"),
        Index("ix_face_templates_application_model", "application_id", "model_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Embeddings from different models are not comparable, so every query filters on this.
    model_id: Mapped[str] = mapped_column(String(64))
    detection_score: Mapped[float] = mapped_column(Float)
    sharpness: Mapped[float] = mapped_column(Float)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class VerificationSession(Base):
    """A short-lived grant letting a web or mobile client upload one photo directly."""

    __tablename__ = "verification_sessions"
    __table_args__ = (
        CheckConstraint("action IN ('enroll', 'verify', 'identify')", name="action"),
        CheckConstraint("status IN ('pending', 'completed', 'failed', 'expired')", name="status"),
        Index("ix_verification_sessions_status_expires", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    """Transactional outbox: written with the state change, sent later by the worker."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered', 'failed')", name="status"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Who did what, and the outcome. Never holds images, embeddings or match scores."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_application_created", "application_id", "created_at"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # Deliberately not a foreign key: the record of a deletion must outlive the subject.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class RateLimitCounter(Base):
    """Fixed-window counters, kept in Postgres so limits hold across API replicas."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_seconds: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=0)
