from collections.abc import Iterator
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Depends, Request, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from facepass.core.config import Settings
from facepass.core.errors import ProblemError, unauthorized
from facepass.core.security import API_KEY_PREFIX, SESSION_TOKEN_PREFIX, hash_token, utcnow
from facepass.db.models import ApiKey, Application
from facepass.schemas import Problem
from facepass.services import ratelimit
from facepass.services.audit import Actor
from facepass.services.faces import FaceService

_api_key_scheme = HTTPBearer(
    scheme_name="ApiKey",
    description="A secret API key. Use it from your backend only, never from an app or website.",
    auto_error=False,
)
_session_token_scheme = HTTPBearer(
    scheme_name="SessionToken",
    description="A short-lived session token from `POST /v1/sessions`. Safe to give to clients.",
    auto_error=False,
)
_LAST_USED_RESOLUTION = timedelta(minutes=1)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_face_service(request: Request) -> FaceService:
    service: FaceService = request.app.state.face_service
    return service


def get_db(request: Request) -> Iterator[Session]:
    # Leaving this block without a commit rolls back, so handlers must commit explicitly.
    with request.app.state.session_factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
FaceServiceDep = Annotated[FaceService, Depends(get_face_service)]
DbDep = Annotated[Session, Depends(get_db)]


def require_api_key(
    response: Response,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_api_key_scheme)],
    db: DbDep,
    settings: SettingsDep,
) -> Actor:
    if credentials is None or not credentials.credentials.startswith(API_KEY_PREFIX):
        raise unauthorized("Send an API key as a Bearer token.")
    row = db.execute(
        select(ApiKey, Application)
        .join(Application, Application.id == ApiKey.application_id)
        .where(
            ApiKey.key_hash == hash_token(credentials.credentials),
            ApiKey.revoked_at.is_(None),
            Application.disabled_at.is_(None),
        )
    ).first()
    if row is None:
        raise unauthorized("The API key is invalid or revoked.")
    api_key, application = row

    status = ratelimit.hit(db, f"api_key:{api_key.id}", settings.rate_limit_per_minute, 60)
    now = utcnow()
    if api_key.last_used_at is None or now - api_key.last_used_at > _LAST_USED_RESOLUTION:
        api_key.last_used_at = now
    # Commits the counter even when rejecting, and frees the connection before inference.
    db.commit()

    rate_headers = {
        "RateLimit-Limit": str(status.limit),
        "RateLimit-Remaining": str(status.remaining),
        "RateLimit-Reset": str(status.reset_after_seconds),
    }
    if not status.allowed:
        raise ProblemError(
            429,
            "rate_limited",
            "Rate limit exceeded",
            "Slow down and retry after the delay in the Retry-After header.",
            headers={**rate_headers, "Retry-After": str(status.reset_after_seconds)},
        )
    response.headers.update(rate_headers)
    return Actor(application.id, "api_key", api_key.id)


def require_session_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_session_token_scheme)],
) -> str:
    if credentials is None or not credentials.credentials.startswith(SESSION_TOKEN_PREFIX):
        raise unauthorized("Send a session token as a Bearer token.")
    return credentials.credentials


ActorDep = Annotated[Actor, Depends(require_api_key)]
SessionTokenDep = Annotated[str, Depends(require_session_token)]


def read_upload(upload: UploadFile, settings: Settings) -> bytes:
    raw = upload.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise ProblemError(
            413,
            "image_too_large",
            "Image too large",
            f"The image must be at most {settings.max_upload_bytes} bytes.",
        )
    if not raw:
        raise ProblemError(400, "empty_image", "Empty image", "The uploaded file is empty.")
    return raw


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Document error responses in OpenAPI, so generated clients know about them."""
    return {
        status: {
            "description": HTTPStatus(status).phrase,
            "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
        }
        for status in statuses
    }
