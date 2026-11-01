import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError

from facepass.api.deps import (
    ActorDep,
    DbDep,
    FaceServiceDep,
    SessionTokenDep,
    SettingsDep,
    problem_responses,
    read_upload,
)
from facepass.core.errors import ProblemError, not_found
from facepass.core.security import utcnow
from facepass.db.models import FaceTemplate, Subject, VerificationSession
from facepass.schemas import (
    FaceList,
    FaceOut,
    IdentifyResult,
    SessionCreate,
    SessionCreated,
    SessionOut,
    SessionSubmitted,
    SubjectCreate,
    SubjectOut,
    SubjectPage,
    VerifyResult,
)
from facepass.services import audit, sessions
from facepass.services.audit import Actor

router = APIRouter(prefix="/v1", responses=problem_responses(401, 429))

ImageUpload = Annotated[UploadFile, File(description="JPEG, PNG or WebP with one visible face.")]
_UPLOAD_ERRORS = (400, 413, 415, 422, 503)


def _get_subject(db: DbDep, actor: Actor, subject_id: uuid.UUID) -> Subject:
    subject = db.scalar(
        select(Subject).where(
            Subject.id == subject_id, Subject.application_id == actor.application_id
        )
    )
    if subject is None:
        raise not_found("subject")
    return subject


def _encode_cursor(subject: Subject) -> str:
    raw = f"{subject.created_at.isoformat()}|{subject.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, subject_id = base64.urlsafe_b64decode(cursor).decode().split("|")
        return datetime.fromisoformat(created_at), uuid.UUID(subject_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ProblemError(400, "invalid_cursor", "Invalid cursor") from None


@router.post(
    "/subjects",
    tags=["Subjects"],
    status_code=status.HTTP_201_CREATED,
    responses=problem_responses(409),
)
def create_subject(
    body: SubjectCreate, response: Response, db: DbDep, actor: ActorDep
) -> SubjectOut:
    """Create a person to enroll faces for."""
    subject = Subject(
        id=uuid.uuid4(),
        application_id=actor.application_id,
        external_id=body.external_id,
        created_at=utcnow(),
    )
    db.add(subject)
    audit.record(db, actor, "subject.create", "success", subject.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ProblemError(
            409,
            "external_id_conflict",
            "External id already used",
            "A subject with this external_id already exists.",
        ) from None
    response.headers["Location"] = f"/v1/subjects/{subject.id}"
    return SubjectOut.model_validate(subject)


@router.get("/subjects", tags=["Subjects"])
def list_subjects(
    db: DbDep,
    actor: ActorDep,
    external_id: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> SubjectPage:
    """List subjects, newest first, optionally finding one by your own user id."""
    query = select(Subject).where(Subject.application_id == actor.application_id)
    if external_id is not None:
        query = query.where(Subject.external_id == external_id)
    if cursor is not None:
        query = query.where(tuple_(Subject.created_at, Subject.id) < _decode_cursor(cursor))
    rows = db.scalars(
        query.order_by(Subject.created_at.desc(), Subject.id.desc()).limit(limit + 1)
    ).all()
    items = rows[:limit]
    return SubjectPage(
        items=[SubjectOut.model_validate(subject) for subject in items],
        next_cursor=_encode_cursor(items[-1]) if len(rows) > limit else None,
    )


@router.get("/subjects/{subject_id}", tags=["Subjects"], responses=problem_responses(404))
def get_subject(subject_id: uuid.UUID, db: DbDep, actor: ActorDep) -> SubjectOut:
    return SubjectOut.model_validate(_get_subject(db, actor, subject_id))


@router.delete(
    "/subjects/{subject_id}",
    tags=["Subjects"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(404),
)
def delete_subject(subject_id: uuid.UUID, db: DbDep, actor: ActorDep) -> None:
    """Permanently delete a person and all their biometric data. This can't be undone."""
    subject = _get_subject(db, actor, subject_id)
    db.delete(subject)
    audit.record(db, actor, "subject.delete", "success", subject_id)
    db.commit()


@router.post(
    "/subjects/{subject_id}/faces",
    tags=["Faces"],
    status_code=status.HTTP_201_CREATED,
    responses=problem_responses(404, 409, *_UPLOAD_ERRORS),
)
def enroll_face(
    subject_id: uuid.UUID,
    image: ImageUpload,
    consent: Annotated[
        bool, Form(description="You confirm the person agreed to biometric processing.")
    ],
    response: Response,
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
    service: FaceServiceDep,
) -> FaceOut:
    """Enroll a face. The photo is discarded, and only its embedding is stored."""
    if not consent:
        raise ProblemError(
            422,
            "consent_required",
            "Consent required",
            "Set consent to true once the person has agreed to biometric processing.",
        )
    _get_subject(db, actor, subject_id)
    db.commit()
    raw = read_upload(image, settings)
    template = service.enroll(db, actor, subject_id, raw, utcnow())
    db.commit()
    response.headers["Location"] = f"/v1/subjects/{subject_id}/faces/{template.id}"
    return FaceOut.model_validate(template)


@router.get("/subjects/{subject_id}/faces", tags=["Faces"], responses=problem_responses(404))
def list_faces(subject_id: uuid.UUID, db: DbDep, actor: ActorDep) -> FaceList:
    _get_subject(db, actor, subject_id)
    templates = db.scalars(
        select(FaceTemplate)
        .where(FaceTemplate.subject_id == subject_id)
        .order_by(FaceTemplate.created_at)
    ).all()
    return FaceList(items=[FaceOut.model_validate(template) for template in templates])


@router.delete(
    "/subjects/{subject_id}/faces/{face_id}",
    tags=["Faces"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(404),
)
def delete_face(subject_id: uuid.UUID, face_id: uuid.UUID, db: DbDep, actor: ActorDep) -> None:
    template = db.scalar(
        select(FaceTemplate).where(
            FaceTemplate.id == face_id,
            FaceTemplate.subject_id == subject_id,
            FaceTemplate.application_id == actor.application_id,
        )
    )
    if template is None:
        raise not_found("face")
    db.delete(template)
    audit.record(db, actor, "face.delete", "success", subject_id)
    db.commit()


@router.post(
    "/verify",
    tags=["Recognition"],
    responses=problem_responses(404, 409, *_UPLOAD_ERRORS),
)
def verify(
    image: ImageUpload,
    subject_id: Annotated[uuid.UUID, Form()],
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
    service: FaceServiceDep,
) -> VerifyResult:
    """Check whether a photo matches one subject (1:1)."""
    raw = read_upload(image, settings)
    result = service.verify(db, actor, subject_id, raw)
    db.commit()
    return result


@router.post("/identify", tags=["Recognition"], responses=problem_responses(*_UPLOAD_ERRORS))
def identify(
    image: ImageUpload,
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
    service: FaceServiceDep,
) -> IdentifyResult:
    """Find who a photo shows among this application's enrolled subjects (1:N)."""
    raw = read_upload(image, settings)
    result = service.identify(db, actor, raw)
    db.commit()
    return result


@router.post(
    "/sessions",
    tags=["Sessions"],
    status_code=status.HTTP_201_CREATED,
    responses=problem_responses(404, 422),
)
def create_session(
    body: SessionCreate, db: DbDep, actor: ActorDep, settings: SettingsDep
) -> SessionCreated:
    """Create a short-lived token so a web or mobile client can upload a photo directly."""
    session, token = sessions.create_session(db, actor, settings, body)
    return SessionCreated(
        id=session.id,
        token=token,
        action=session.action,
        subject_id=session.subject_id,
        expires_at=session.expires_at,
        max_attempts=settings.session_max_attempts,
    )


@router.get("/sessions/{session_id}", tags=["Sessions"], responses=problem_responses(404))
def get_session(session_id: uuid.UUID, db: DbDep, actor: ActorDep) -> SessionOut:
    """Read a session's outcome on your backend, as an alternative to webhooks."""
    session = db.scalar(
        select(VerificationSession).where(
            VerificationSession.id == session_id,
            VerificationSession.application_id == actor.application_id,
        )
    )
    if session is None:
        raise not_found("session")
    return SessionOut.model_validate(session)


@router.post(
    "/sessions/submit",
    tags=["Sessions"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_responses(409, 410, *_UPLOAD_ERRORS),
)
def submit_session(
    image: ImageUpload,
    token: SessionTokenDep,
    db: DbDep,
    settings: SettingsDep,
    service: FaceServiceDep,
) -> SessionSubmitted:
    """Upload a photo from the client. The outcome goes only to your backend.

    A rejected photo returns an error code the client can show as guidance, and the user can
    retry while attempts remain.
    """
    raw = read_upload(image, settings)
    session = sessions.submit(db, service, settings, token, raw)
    return SessionSubmitted(id=session.id, status=session.status)
