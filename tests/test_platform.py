import base64
import io
import uuid
from collections.abc import Callable

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from facepass.core.config import Settings
from facepass.core.errors import ProblemError
from facepass.core.security import utcnow
from facepass.db.models import ApiKey
from facepass.engine.image import decode_image
from facepass.main import create_app
from tests.conftest import Tenant
from tests.fakes import FakeFaceEngine, person
from tests.helpers import create_subject, upload


def _assert_problem(response: object, status: int, code: str) -> None:
    assert response.status_code == status, response.text  # type: ignore[attr-defined]
    assert response.headers["content-type"] == "application/problem+json"  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"urn:facepass:problem:{code}"
    assert body["request_id"]


def test_production_refuses_placeholder_secret() -> None:
    with pytest.raises(ValueError, match="FACEPASS_SECRET_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="unused",
            secret_key="change-me-to-at-least-32-random-characters",
        )


def test_missing_api_key_is_unauthorized(client: TestClient) -> None:
    response = client.get("/v1/subjects")
    _assert_problem(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_api_key_is_unauthorized(client: TestClient) -> None:
    response = client.get("/v1/subjects", headers={"Authorization": "Bearer fp_key_wrong"})
    _assert_problem(response, 401, "unauthorized")


def test_session_token_is_not_an_api_key(client: TestClient) -> None:
    response = client.get("/v1/subjects", headers={"Authorization": "Bearer fp_ses_wrong"})
    _assert_problem(response, 401, "unauthorized")


def test_revoked_api_key_is_unauthorized(client: TestClient, tenant: Tenant, db: Session) -> None:
    db.get_one(ApiKey, tenant.api_key_id).revoked_at = utcnow()
    db.commit()
    _assert_problem(client.get("/v1/subjects", headers=tenant.headers), 401, "unauthorized")


def test_rate_limit_returns_standard_headers(
    settings: Settings, tenant: Tenant, session_factory: object
) -> None:
    limited = settings.model_copy(update={"rate_limit_per_minute": 2})
    with TestClient(create_app(limited, FakeFaceEngine())) as client:
        first = client.get("/v1/subjects", headers=tenant.headers)
        assert first.headers["RateLimit-Limit"] == "2"
        assert first.headers["RateLimit-Remaining"] == "1"
        client.get("/v1/subjects", headers=tenant.headers)
        response = client.get("/v1/subjects", headers=tenant.headers)
    _assert_problem(response, 429, "rate_limited")
    assert int(response.headers["Retry-After"]) > 0


def test_duplicate_external_id_is_a_conflict(client: TestClient, tenant: Tenant) -> None:
    create_subject(client, tenant, "user-1")
    response = client.post("/v1/subjects", json={"external_id": "user-1"}, headers=tenant.headers)
    _assert_problem(response, 409, "external_id_conflict")


def test_same_external_id_is_allowed_in_another_tenant(
    client: TestClient, tenant: Tenant, make_tenant: Callable[..., Tenant]
) -> None:
    create_subject(client, tenant, "user-1")
    create_subject(client, make_tenant(), "user-1")


def test_subject_pagination_visits_everything_once(client: TestClient, tenant: Tenant) -> None:
    created = {create_subject(client, tenant, f"user-{index}") for index in range(5)}
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/subjects", params=params, headers=tenant.headers).json()
        seen += [subject["id"] for subject in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(created)


def test_find_subject_by_external_id(client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(client, tenant, "user-1")
    create_subject(client, tenant, "user-2")
    page = client.get("/v1/subjects", params={"external_id": "user-1"}, headers=tenant.headers)
    assert [subject["id"] for subject in page.json()["items"]] == [subject_id]


def test_invalid_cursor_is_a_bad_request(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/v1/subjects", params={"cursor": "!!!"}, headers=tenant.headers)
    _assert_problem(response, 400, "invalid_cursor")


def test_validation_errors_use_problem_format_and_omit_input(
    client: TestClient, tenant: Tenant
) -> None:
    response = client.post("/v1/subjects", json={"external_id": "x" * 300}, headers=tenant.headers)
    _assert_problem(response, 422, "validation_error")
    assert "xxx" not in response.text


def test_unknown_route_uses_problem_format(client: TestClient) -> None:
    _assert_problem(client.get("/v1/nothing"), 404, "http_404")


def test_oversized_body_is_rejected(client: TestClient, tenant: Tenant) -> None:
    oversized = b"0" * (8 * 1024 * 1024 + 100 * 1024)
    response = client.post("/v1/identify", files=upload(oversized), headers=tenant.headers)
    assert response.status_code == 413
    assert response.json()["code"] == "image_too_large"


def test_file_just_over_limit_is_rejected(client: TestClient, tenant: Tenant) -> None:
    response = client.post(
        "/v1/identify", files=upload(b"0" * (8 * 1024 * 1024 + 1)), headers=tenant.headers
    )
    _assert_problem(response, 413, "image_too_large")


def test_empty_file_is_rejected(client: TestClient, tenant: Tenant) -> None:
    response = client.post("/v1/identify", files=upload(b""), headers=tenant.headers)
    assert response.json()["code"] in ("empty_image", "validation_error")
    assert response.status_code in (400, 422)


def test_non_image_is_rejected(client: TestClient, tenant: Tenant) -> None:
    response = client.post(
        "/v1/identify", files=upload(b"<svg onload=alert(1)>"), headers=tenant.headers
    )
    _assert_problem(response, 415, "unsupported_image")


def test_truncated_image_is_rejected(client: TestClient, tenant: Tenant) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, "JPEG")
    response = client.post(
        "/v1/identify", files=upload(buffer.getvalue()[:60]), headers=tenant.headers
    )
    _assert_problem(response, 415, "unsupported_image")


def test_decode_rejects_too_many_pixels_from_header_alone() -> None:
    buffer = io.BytesIO()
    Image.new("L", (3000, 3000)).save(buffer, "PNG")
    with pytest.raises(ProblemError) as caught:
        decode_image(buffer.getvalue(), max_pixels=1000, max_side=1280)
    assert caught.value.code == "image_too_large"


def test_decode_applies_exif_rotation() -> None:
    image = Image.new("RGB", (40, 20), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 degrees clockwise to display.
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    decoded = decode_image(buffer.getvalue(), max_pixels=10_000, max_side=1280)
    assert decoded.shape[:2] == (40, 20)


def test_decode_downscales_and_returns_bgr() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (4000, 2000), (255, 0, 0)).save(buffer, "PNG")
    decoded = decode_image(buffer.getvalue(), max_pixels=40_000_000, max_side=1000)
    assert decoded.shape[:2] == (500, 1000)
    assert decoded.dtype == np.uint8
    assert tuple(int(value) for value in decoded[0, 0]) == (0, 0, 255)


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    def preflight(origin: str) -> str | None:
        response = client.options(
            "/v1/sessions/submit",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        allowed: str | None = response.headers.get("access-control-allow-origin")
        return allowed

    assert preflight("https://app.example.com") == "https://app.example.com"
    assert preflight("https://evil.example.com") is None


def test_request_id_is_echoed_or_replaced_when_unsafe(client: TestClient) -> None:
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"]
        == "abc-123"
    )
    replaced = client.get("/healthz", headers={"X-Request-ID": "bad id\twith junk"})
    assert replaced.headers["X-Request-ID"] != "bad id\twith junk"


def test_security_headers_are_set(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"


def test_openapi_documents_problem_errors(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    verify_errors = spec["paths"]["/v1/verify"]["post"]["responses"]
    assert "application/problem+json" in verify_errors["401"]["content"]
    assert "ApiKey" in spec["components"]["securitySchemes"]
    assert "SessionToken" in spec["components"]["securitySchemes"]


def test_unknown_subject_uuid_is_not_found(client: TestClient, tenant: Tenant) -> None:
    response = client.post(
        "/v1/verify",
        files=upload(person(1)),
        data={"subject_id": str(uuid.uuid4())},
        headers=tenant.headers,
    )
    _assert_problem(response, 404, "subject_not_found")


def test_cursor_is_opaque_base64(client: TestClient, tenant: Tenant) -> None:
    for index in range(2):
        create_subject(client, tenant, f"user-{index}")
    page = client.get("/v1/subjects", params={"limit": 1}, headers=tenant.headers).json()
    assert b"|" in base64.urlsafe_b64decode(page["next_cursor"])
