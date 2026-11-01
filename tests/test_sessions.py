import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.core.security import utcnow
from facepass.db.models import AuditEvent, FaceTemplate, VerificationSession, WebhookDelivery
from facepass.services import maintenance, webhooks
from tests.conftest import WEBHOOK_SECRET, Tenant
from tests.fakes import no_face, person
from tests.helpers import create_subject, enrolled_subject, upload


def _start(client: TestClient, tenant: Tenant, **body: Any) -> dict[str, Any]:
    response = client.post("/v1/sessions", json=body, headers=tenant.headers)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _submit(client: TestClient, token: str, image: bytes) -> httpx.Response:
    response: httpx.Response = client.post(
        "/v1/sessions/submit",
        files=upload(image),
        headers={"Authorization": f"Bearer {token}"},
    )
    return response


def _result(client: TestClient, tenant: Tenant, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}", headers=tenant.headers)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_verify_session_hides_result_from_client_and_shows_it_to_backend(
    client: TestClient, tenant: Tenant
) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)

    response = _submit(client, session["token"], person(1))

    assert response.status_code == 202
    assert response.json() == {"id": session["id"], "status": "completed"}
    outcome = _result(client, tenant, session["id"])
    assert outcome["status"] == "completed"
    assert outcome["result"]["match"] is True
    assert "token" not in outcome


def test_enroll_session_stores_consent_and_face(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    subject_id = create_subject(client, tenant)
    session = _start(client, tenant, action="enroll", subject_id=subject_id, consent=True)

    assert _submit(client, session["token"], person(1)).status_code == 202

    template = db.scalars(select(FaceTemplate)).one()
    assert str(template.subject_id) == subject_id
    assert template.consent_at is not None
    assert _result(client, tenant, session["id"])["result"]["face_id"] == str(template.id)


def test_enroll_session_requires_consent(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = client.post(
        "/v1/sessions",
        json={"action": "enroll", "subject_id": subject_id},
        headers=tenant.headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "consent_required"


def test_identify_session(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="identify")
    assert _submit(client, session["token"], person(1)).status_code == 202
    candidates = _result(client, tenant, session["id"])["result"]["candidates"]
    assert candidates[0]["subject_id"] == subject_id


def test_subject_rules_are_validated(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    for body in (
        {"action": "verify"},
        {"action": "identify", "subject_id": subject_id},
    ):
        response = client.post("/v1/sessions", json=body, headers=tenant.headers)
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


def test_session_cannot_target_another_tenants_subject(
    client: TestClient, tenant: Tenant, make_tenant: Callable[..., Tenant]
) -> None:
    subject_id = create_subject(client, tenant)
    response = client.post(
        "/v1/sessions",
        json={"action": "verify", "subject_id": subject_id},
        headers=make_tenant().headers,
    )
    assert response.status_code == 404


def test_session_outcome_is_private_to_its_tenant(
    client: TestClient, tenant: Tenant, make_tenant: Callable[..., Tenant]
) -> None:
    session = _start(client, tenant, action="identify")
    response = client.get(f"/v1/sessions/{session['id']}", headers=make_tenant().headers)
    assert response.status_code == 404


def test_token_cannot_be_reused_after_a_result(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)
    assert _submit(client, session["token"], person(2)).status_code == 202

    # A no-match closes the session, so a stolen token can't try a series of faces.
    response = _submit(client, session["token"], person(1))
    assert response.status_code == 409
    assert response.json()["code"] == "session_closed"


def test_rejected_photo_allows_retry(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)

    rejected = _submit(client, session["token"], no_face())
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "no_face_detected"

    assert _submit(client, session["token"], person(1)).status_code == 202
    assert _result(client, tenant, session["id"])["attempts"] == 2


def test_attempt_limit_fails_the_session(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)

    for _ in range(3):
        assert _submit(client, session["token"], no_face()).status_code == 422

    outcome = _result(client, tenant, session["id"])
    assert outcome["status"] == "failed"
    assert outcome["result"] == {"error": "no_face_detected"}
    assert _submit(client, session["token"], person(1)).status_code == 409


def test_expired_session_is_rejected(client: TestClient, tenant: Tenant, db: Session) -> None:
    session = _start(client, tenant, action="identify")
    row = db.scalars(select(VerificationSession)).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    response = _submit(client, session["token"], person(1))

    assert response.status_code == 410
    assert response.json()["code"] == "session_expired"


def test_unknown_session_token_is_unauthorized(client: TestClient) -> None:
    assert _submit(client, "fp_ses_unknown", person(1)).status_code == 401


def test_api_key_cannot_submit_as_session(client: TestClient, tenant: Tenant) -> None:
    assert _submit(client, tenant.api_key, person(1)).status_code == 401


def test_session_token_is_not_stored_in_plain_text(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    session = _start(client, tenant, action="identify")
    row = db.scalars(select(VerificationSession)).one()
    assert session["token"] not in row.token_hash


def test_session_activity_is_audited_as_session_actor(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)
    _submit(client, session["token"], person(1))
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "face.verify")).one()
    assert event.actor_type == "session"
    assert str(event.actor_id) == session["id"]


def _client_recording(received: list[httpx.Request], status_code: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(status_code)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_webhook_is_signed_and_delivered(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    session_factory: sessionmaker[Session],
    settings: Settings,
    db: Session,
) -> None:
    tenant = make_tenant("https://hooks.example.com/facepass")
    subject_id = enrolled_subject(client, tenant, person(1))
    session = _start(client, tenant, action="verify", subject_id=subject_id)
    _submit(client, session["token"], person(1))

    received: list[httpx.Request] = []
    with _client_recording(received) as http:
        assert webhooks.deliver_due(session_factory, http, settings) == 1

    request = received[0]
    assert str(request.url) == "https://hooks.example.com/facepass"
    fields = dict(part.split("=") for part in request.headers["FacePass-Signature"].split(","))
    signed = fields["t"].encode() + b"." + request.content
    assert fields["v1"] == hmac.new(WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    event = json.loads(request.content)
    assert event["type"] == "session.completed"
    assert event["data"]["session_id"] == session["id"]
    assert event["data"]["result"]["match"] is True
    assert db.scalars(select(WebhookDelivery)).one().status == "delivered"


def test_webhook_failure_retries_then_gives_up(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    session_factory: sessionmaker[Session],
    settings: Settings,
    db: Session,
) -> None:
    tenant = make_tenant("https://hooks.example.com/facepass")
    session = _start(client, tenant, action="identify")
    _submit(client, session["token"], person(1))
    received: list[httpx.Request] = []
    limited = settings.model_copy(update={"webhook_max_attempts": 2})

    with _client_recording(received, status_code=500) as http:
        webhooks.deliver_due(session_factory, http, limited)
        delivery = db.scalars(select(WebhookDelivery)).one()
        assert delivery.status == "pending"
        assert delivery.next_attempt_at > utcnow()
        assert delivery.last_error == "HTTP 500"

        # Not due yet, so nothing is sent until the backoff passes.
        assert webhooks.deliver_due(session_factory, http, limited) == 0
        delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
        webhooks.deliver_due(session_factory, http, limited)

    db.refresh(delivery)
    assert delivery.status == "failed"
    assert len(received) == 2


def test_no_webhook_row_without_configured_url(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    session = _start(client, tenant, action="identify")
    _submit(client, session["token"], person(1))
    assert db.scalars(select(WebhookDelivery)).all() == []


def test_backoff_grows_and_is_capped() -> None:
    assert webhooks.backoff_seconds(1) == 30
    assert webhooks.backoff_seconds(2) == 60
    assert webhooks.backoff_seconds(30) == 3600


def test_expired_sessions_are_closed_with_webhook(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    session_factory: sessionmaker[Session],
    db: Session,
) -> None:
    tenant = make_tenant("https://hooks.example.com/facepass")
    session = _start(client, tenant, action="identify")
    row = db.scalars(select(VerificationSession)).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    assert maintenance.expire_sessions(session_factory) == 1

    assert _result(client, tenant, session["id"])["status"] == "expired"
    delivery = db.scalars(select(WebhookDelivery)).one()
    assert delivery.payload["type"] == "session.expired"


def test_retention_purges_old_records_only(
    client: TestClient,
    tenant: Tenant,
    session_factory: sessionmaker[Session],
    settings: Settings,
    db: Session,
) -> None:
    create_subject(client, tenant)
    create_subject(client, tenant)
    events = db.scalars(select(AuditEvent)).all()
    events[0].created_at = utcnow() - timedelta(days=settings.audit_retention_days + 1)
    db.commit()

    maintenance.purge_old_records(session_factory, settings)

    db.expire_all()
    assert len(db.scalars(select(AuditEvent)).all()) == 1
