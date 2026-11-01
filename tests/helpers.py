from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import Tenant
from tests.fakes import person


def upload(image: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {"image": ("photo.png", image, "image/png")}


def create_subject(client: TestClient, tenant: Tenant, external_id: str | None = None) -> str:
    response = client.post(
        "/v1/subjects", json={"external_id": external_id}, headers=tenant.headers
    )
    assert response.status_code == 201, response.text
    subject_id: str = response.json()["id"]
    return subject_id


def enroll(client: TestClient, tenant: Tenant, subject_id: str, image: bytes | None = None) -> Any:
    return client.post(
        f"/v1/subjects/{subject_id}/faces",
        files=upload(image if image is not None else person(1)),
        data={"consent": "true"},
        headers=tenant.headers,
    )


def enrolled_subject(client: TestClient, tenant: Tenant, image: bytes | None = None) -> str:
    subject_id = create_subject(client, tenant)
    response = enroll(client, tenant, subject_id, image)
    assert response.status_code == 201, response.text
    return subject_id


def verify(client: TestClient, tenant: Tenant, subject_id: str, image: bytes) -> Any:
    return client.post(
        "/v1/verify", files=upload(image), data={"subject_id": subject_id}, headers=tenant.headers
    )
