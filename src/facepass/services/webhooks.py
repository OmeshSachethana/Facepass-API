import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.core.security import (
    decrypt_secret,
    encode_webhook_body,
    sign_webhook,
    utcnow,
)
from facepass.db.models import Application, VerificationSession, WebhookDelivery

logger = logging.getLogger(__name__)

_LEASE = timedelta(seconds=60)
_MAX_BACKOFF_SECONDS = 3600


def backoff_seconds(attempt: int) -> int:
    return int(min(30 * 2 ** (attempt - 1), _MAX_BACKOFF_SECONDS))


def enqueue_session_finished(db: Session, session: VerificationSession) -> None:
    """Add the event to the current transaction, so it exists only if the state change commits."""
    application = db.get_one(Application, session.application_id)
    if application.webhook_url is None:
        return
    delivery_id = uuid.uuid4()
    payload: dict[str, Any] = {
        "id": str(delivery_id),
        "type": f"session.{session.status}",
        "created_at": utcnow().isoformat(),
        "data": {
            "session_id": str(session.id),
            "action": session.action,
            "status": session.status,
            "subject_id": str(session.subject_id) if session.subject_id else None,
            "result": session.result,
        },
    }
    db.add(WebhookDelivery(id=delivery_id, application_id=session.application_id, payload=payload))


def _claim_due(db: Session, limit: int) -> list[uuid.UUID]:
    """Lease due deliveries. SKIP LOCKED lets several workers run without sending duplicates."""
    now = utcnow()
    due = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for delivery in due:
        delivery.attempts += 1
        # If this worker crashes mid-send, the lease expires and another worker retries.
        delivery.next_attempt_at = now + _LEASE
    ids = [delivery.id for delivery in due]
    db.commit()
    return ids


def _send(client: httpx.Client, url: str, secret: str, delivery: WebhookDelivery) -> str | None:
    """Send one delivery, returning an error description or None on success."""
    body = encode_webhook_body(delivery.payload)
    headers = {
        "Content-Type": "application/json",
        "FacePass-Event-Id": str(delivery.id),
        "FacePass-Signature": sign_webhook(secret, int(utcnow().timestamp()), body),
    }
    try:
        response = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}"
    return None if response.is_success else f"HTTP {response.status_code}"


def deliver_due(
    session_factory: sessionmaker[Session],
    client: httpx.Client,
    settings: Settings,
    batch_size: int = 20,
) -> int:
    """Send due webhooks with at-least-once delivery and return how many were attempted."""
    with session_factory() as db:
        claimed = _claim_due(db, batch_size)

    for delivery_id in claimed:
        with session_factory() as db:
            delivery = db.get_one(WebhookDelivery, delivery_id)
            application = db.get_one(Application, delivery.application_id)
            error = "Application has no webhook configured"
            if application.webhook_url and application.webhook_secret_encrypted:
                try:
                    secret = decrypt_secret(
                        application.webhook_secret_encrypted,
                        settings.secret_key.get_secret_value(),
                    )
                except InvalidToken:
                    # FACEPASS_SECRET_KEY changed. Run `facepass apps set-webhook` to fix this.
                    error = "Webhook secret can't be decrypted"
                else:
                    # Release the database connection while waiting on the receiver.
                    db.commit()
                    error = _send(client, application.webhook_url, secret, delivery) or ""

            now = utcnow()
            if not error:
                delivery.status = "delivered"
                delivery.finished_at = now
                delivery.last_error = None
            elif delivery.attempts >= settings.webhook_max_attempts:
                delivery.status = "failed"
                delivery.finished_at = now
                delivery.last_error = error
            else:
                delivery.next_attempt_at = now + timedelta(
                    seconds=backoff_seconds(delivery.attempts)
                )
                delivery.last_error = error
            db.commit()
            if error:
                logger.warning(
                    "Webhook delivery failed: %s",
                    error,
                    extra={"delivery_id": str(delivery.id), "attempt": delivery.attempts},
                )
    return len(claimed)
