import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from facepass.core.config import Settings
from facepass.core.errors import ProblemError, not_found, unauthorized
from facepass.core.security import generate_session_token, hash_token, utcnow
from facepass.db.models import Subject, VerificationSession
from facepass.schemas import SessionCreate
from facepass.services import audit, webhooks
from facepass.services.audit import Actor
from facepass.services.faces import FaceService


def create_session(
    db: Session, actor: Actor, settings: Settings, request: SessionCreate
) -> tuple[VerificationSession, str]:
    """Create a session and return it with its token, which exists only in this response."""
    if request.action == "enroll" and not request.consent:
        raise _consent_required()
    if request.subject_id is not None:
        subject = db.scalar(
            select(Subject).where(
                Subject.id == request.subject_id, Subject.application_id == actor.application_id
            )
        )
        if subject is None:
            raise not_found("subject")

    now = utcnow()
    token = generate_session_token()
    session = VerificationSession(
        id=uuid.uuid4(),
        application_id=actor.application_id,
        subject_id=request.subject_id,
        action=request.action,
        status="pending",
        token_hash=hash_token(token),
        attempts=0,
        consent_at=now if request.consent else None,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    audit.record(db, actor, "session.create", "success", request.subject_id)
    db.commit()
    return session, token


def submit(
    db: Session, service: FaceService, settings: Settings, token: str, raw: bytes
) -> VerificationSession:
    """Process a client's upload.

    A rejected photo spends one attempt and lets the user retry. Any conclusive outcome,
    including "no match", closes the session so a token can't be used to probe the matcher.
    """
    session = db.scalar(
        select(VerificationSession).where(VerificationSession.token_hash == hash_token(token))
    )
    if session is None:
        raise unauthorized("The session token is invalid.")
    if session.status != "pending":
        raise ProblemError(409, "session_closed", "Session closed", "Create a new session.")

    # Claiming atomically means parallel uploads can't exceed the attempt limit.
    now = utcnow()
    attempts = db.scalar(
        update(VerificationSession)
        .where(
            VerificationSession.id == session.id,
            VerificationSession.status == "pending",
            VerificationSession.expires_at > now,
            VerificationSession.attempts < settings.session_max_attempts,
        )
        .values(attempts=VerificationSession.attempts + 1)
        .returning(VerificationSession.attempts)
    )
    db.commit()
    if attempts is None:
        if session.expires_at <= now:
            raise ProblemError(410, "session_expired", "Session expired", "Create a new session.")
        raise ProblemError(409, "session_closed", "Session closed", "Create a new session.")

    actor = Actor(session.application_id, "session", session.id)
    try:
        result = _run_action(db, service, actor, session, raw)
    except ProblemError as exc:
        db.rollback()
        if attempts >= settings.session_max_attempts:
            _finish(db, session, "failed", {"error": exc.code})
            db.commit()
        raise

    if not _finish(db, session, "completed", result):
        # A parallel upload finished first, so this one's changes are abandoned.
        db.rollback()
        raise ProblemError(409, "session_closed", "Session closed", "Create a new session.")
    db.commit()
    return session


def _run_action(
    db: Session,
    service: FaceService,
    actor: Actor,
    session: VerificationSession,
    raw: bytes,
) -> dict[str, Any]:
    if session.action == "identify":
        return service.identify(db, actor, raw).model_dump(mode="json")

    # The API validated subject_id when the session was created.
    if session.subject_id is None:
        raise RuntimeError(f"Session {session.id} has no subject")
    if session.action == "verify":
        return service.verify(db, actor, session.subject_id, raw).model_dump(mode="json")

    if session.consent_at is None:
        raise RuntimeError(f"Enroll session {session.id} has no consent")
    template = service.enroll(db, actor, session.subject_id, raw, session.consent_at)
    return {"enrolled": True, "face_id": str(template.id), "subject_id": str(template.subject_id)}


def _finish(db: Session, session: VerificationSession, status: str, result: dict[str, Any]) -> bool:
    """Close a session once. Return False if another request has already closed it."""
    now = utcnow()
    closed = db.scalar(
        update(VerificationSession)
        .where(VerificationSession.id == session.id, VerificationSession.status == "pending")
        .values(status=status, result=result, finished_at=now)
        .returning(VerificationSession.id)
    )
    if closed is None:
        return False
    db.refresh(session)
    webhooks.enqueue_session_finished(db, session)
    return True


def _consent_required() -> ProblemError:
    return ProblemError(
        422,
        "consent_required",
        "Consent required",
        "Set consent to true once the person has agreed to biometric processing.",
    )
