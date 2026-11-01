from datetime import timedelta

from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.core.security import utcnow
from facepass.db.models import AuditEvent, RateLimitCounter, VerificationSession, WebhookDelivery
from facepass.services import webhooks

_FINISHED_RECORD_RETENTION = timedelta(days=7)


def expire_sessions(session_factory: sessionmaker[Session], batch_size: int = 100) -> int:
    """Close sessions nobody finished, telling the application through a webhook."""
    with session_factory() as db:
        now = utcnow()
        sessions = db.scalars(
            select(VerificationSession)
            .where(VerificationSession.status == "pending", VerificationSession.expires_at <= now)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        for session in sessions:
            session.status = "expired"
            session.finished_at = now
            webhooks.enqueue_session_finished(db, session)
        db.commit()
        return len(sessions)


def purge_old_records(session_factory: sessionmaker[Session], settings: Settings) -> None:
    """Enforce retention limits: sensitive records must not be kept forever by default."""
    now = utcnow()
    with session_factory() as db:
        db.execute(
            delete(AuditEvent).where(
                AuditEvent.created_at < now - timedelta(days=settings.audit_retention_days)
            )
        )
        db.execute(
            delete(VerificationSession).where(
                VerificationSession.finished_at < now - _FINISHED_RECORD_RETENTION
            )
        )
        db.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.finished_at < now - _FINISHED_RECORD_RETENTION
            )
        )
        window_end = RateLimitCounter.window_start + func.make_interval(
            0, 0, 0, 0, 0, 0, RateLimitCounter.window_seconds
        )
        db.execute(delete(RateLimitCounter).where(window_end < now))
        db.commit()


def try_lock(db: Session, lock_id: int) -> bool:
    """Take a transaction-scoped advisory lock, so only one worker runs a periodic job."""
    statement = text("SELECT pg_try_advisory_xact_lock(:lock_id)").bindparams(
        bindparam("lock_id", lock_id)
    )
    return bool(db.scalar(statement))
