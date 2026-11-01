import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from facepass.db.models import AuditEvent, FaceTemplate
from tests.conftest import Tenant
from tests.fakes import no_face, person, photo, similar_person, spoof
from tests.helpers import create_subject, enroll, enrolled_subject, upload, verify


def test_enroll_requires_consent(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = client.post(
        f"/v1/subjects/{subject_id}/faces",
        files=upload(person(1)),
        data={"consent": "false"},
        headers=tenant.headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "consent_required"


def test_enroll_returns_face_without_embedding(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = enroll(client, tenant, subject_id)
    assert response.status_code == 201
    body = response.json()
    assert body["subject_id"] == subject_id
    assert body["model_id"] == "fake-1"
    assert "embedding" not in body
    assert response.headers["Location"].endswith(body["id"])


def test_enroll_rejects_face_that_differs_from_enrolled_faces(
    client: TestClient, tenant: Tenant
) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    response = enroll(client, tenant, subject_id, person(2))
    assert response.status_code == 409
    assert response.json()["code"] == "face_mismatch"


def test_enroll_enforces_face_limit(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    assert enroll(client, tenant, subject_id, similar_person(1)).status_code == 201
    response = enroll(client, tenant, subject_id, person(1))
    assert response.status_code == 409
    assert response.json()["code"] == "face_limit_reached"


def test_enroll_rejects_spoof(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = enroll(client, tenant, subject_id, spoof(1))
    assert response.status_code == 422
    assert response.json()["code"] == "liveness_failed"


def test_verify_matches_same_person(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    response = verify(client, tenant, subject_id, similar_person(1))
    assert response.status_code == 200
    body = response.json()
    assert body["match"] is True
    assert 0.9 < body["score"] <= 1
    assert body["threshold"] == 0.40
    assert body["liveness"] == {"checked": True, "score": 0.99}


def test_verify_rejects_different_person(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    response = verify(client, tenant, subject_id, person(2))
    assert response.status_code == 200
    assert response.json()["match"] is False


def test_verify_without_enrollment_is_a_conflict(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = verify(client, tenant, subject_id, person(1))
    assert response.status_code == 409
    assert response.json()["code"] == "subject_not_enrolled"


def test_verify_locks_after_repeated_failures(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    for _ in range(3):
        assert verify(client, tenant, subject_id, person(2)).json()["match"] is False

    # Locked even for the real person, because an attacker may have been probing.
    response = verify(client, tenant, subject_id, person(1))
    assert response.status_code == 429
    assert response.json()["code"] == "too_many_failed_attempts"
    assert int(response.headers["Retry-After"]) > 0


def test_liveness_failures_count_towards_lockout(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    for _ in range(3):
        response = verify(client, tenant, subject_id, spoof(1))
        assert response.json()["code"] == "liveness_failed"
    assert verify(client, tenant, subject_id, person(1)).status_code == 429


def test_successful_verify_clears_failure_count(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    for _ in range(2):
        verify(client, tenant, subject_id, person(2))
    assert verify(client, tenant, subject_id, person(1)).json()["match"] is True
    for _ in range(2):
        assert verify(client, tenant, subject_id, person(2)).status_code == 200


def test_rejected_photos_do_not_count_towards_lockout(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    for _ in range(5):
        assert verify(client, tenant, subject_id, no_face()).status_code == 422
    assert verify(client, tenant, subject_id, person(1)).json()["match"] is True


def test_identify_finds_the_right_subject(client: TestClient, tenant: Tenant) -> None:
    create_subject(client, tenant, "someone-else")
    first = enrolled_subject(client, tenant, person(1))
    enrolled_subject(client, tenant, person(2))

    response = client.post("/v1/identify", files=upload(person(1)), headers=tenant.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["match"] is True
    assert [candidate["subject_id"] for candidate in body["candidates"]] == [first]


def test_identify_reports_no_match_for_unknown_face(client: TestClient, tenant: Tenant) -> None:
    enrolled_subject(client, tenant, person(1))
    response = client.post("/v1/identify", files=upload(person(9)), headers=tenant.headers)
    assert response.json() == {
        "match": False,
        "candidates": [],
        "threshold": 0.45,
        "model_id": "fake-1",
        "liveness": {"checked": True, "score": 0.99},
    }


def test_identify_returns_each_subject_once(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    assert enroll(client, tenant, subject_id, similar_person(1)).status_code == 201
    response = client.post("/v1/identify", files=upload(person(1)), headers=tenant.headers)
    assert [c["subject_id"] for c in response.json()["candidates"]] == [subject_id]


def test_tenants_cannot_see_or_match_each_others_data(
    client: TestClient, tenant: Tenant, make_tenant: Callable[..., Tenant]
) -> None:
    other = make_tenant()
    subject_id = enrolled_subject(client, tenant, person(1))
    face_id = client.get(f"/v1/subjects/{subject_id}/faces", headers=tenant.headers).json()[
        "items"
    ][0]["id"]

    assert client.get(f"/v1/subjects/{subject_id}", headers=other.headers).status_code == 404
    assert client.get("/v1/subjects", headers=other.headers).json()["items"] == []
    assert client.delete(f"/v1/subjects/{subject_id}", headers=other.headers).status_code == 404
    assert (
        client.delete(
            f"/v1/subjects/{subject_id}/faces/{face_id}", headers=other.headers
        ).status_code
        == 404
    )
    assert enroll(client, other, subject_id).status_code == 404
    assert verify(client, other, subject_id, person(1)).status_code == 404
    response = client.post("/v1/identify", files=upload(person(1)), headers=other.headers)
    assert response.json()["match"] is False


def test_delete_subject_erases_biometric_data_and_keeps_audit(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))

    assert client.delete(f"/v1/subjects/{subject_id}", headers=tenant.headers).status_code == 204

    assert db.scalars(select(FaceTemplate)).all() == []
    assert client.get(f"/v1/subjects/{subject_id}", headers=tenant.headers).status_code == 404
    actions = db.scalars(
        select(AuditEvent.action).where(AuditEvent.subject_id == uuid.UUID(subject_id))
    ).all()
    assert "subject.delete" in actions


def test_delete_single_face(client: TestClient, tenant: Tenant) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    face_id = enroll(client, tenant, subject_id, similar_person(1)).json()["id"]
    path = f"/v1/subjects/{subject_id}/faces"

    assert client.delete(f"{path}/{face_id}", headers=tenant.headers).status_code == 204

    assert len(client.get(path, headers=tenant.headers).json()["items"]) == 1
    assert client.delete(f"{path}/{face_id}", headers=tenant.headers).status_code == 404


def test_audit_records_rejections_with_no_scores(
    client: TestClient, tenant: Tenant, db: Session
) -> None:
    subject_id = enrolled_subject(client, tenant, person(1))
    verify(client, tenant, subject_id, no_face())
    verify(client, tenant, subject_id, person(2))

    outcomes = db.scalars(
        select(AuditEvent.outcome).where(AuditEvent.action == "face.verify")
    ).all()
    assert sorted(outcomes) == ["no_face_detected", "no_match"]


def test_jpeg_upload_is_accepted(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant)
    response = client.post(
        f"/v1/subjects/{subject_id}/faces",
        files={"image": ("photo.jpg", photo(10, image_format="JPEG"), "image/jpeg")},
        data={"consent": "true"},
        headers=tenant.headers,
    )
    assert response.status_code == 201
