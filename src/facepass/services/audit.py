import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from facepass.core.logging import request_id_var
from facepass.db.models import AuditEvent


@dataclass(frozen=True)
class Actor:
    """The authenticated caller: an API key, or a session token from a web or mobile client."""

    application_id: uuid.UUID
    type: str
    id: uuid.UUID


def record(
    db: Session,
    actor: Actor,
    action: str,
    outcome: str,
    subject_id: uuid.UUID | None = None,
) -> None:
    """Add an audit row to the current transaction. The caller decides when to commit."""
    db.add(
        AuditEvent(
            application_id=actor.application_id,
            action=action,
            outcome=outcome,
            actor_type=actor.type,
            actor_id=actor.id,
            subject_id=subject_id,
            request_id=request_id_var.get(),
        )
    )
