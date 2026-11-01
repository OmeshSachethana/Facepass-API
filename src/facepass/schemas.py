import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Problem(BaseModel):
    """RFC 9457 problem details. Branch on `code`, which is stable across releases."""

    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    request_id: str | None = None


class SubjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Your own user id. Unique per application. Avoid names and emails.",
    )


class SubjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str | None
    created_at: datetime


class SubjectPage(BaseModel):
    items: list[SubjectOut]
    next_cursor: str | None


class FaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    model_id: str
    detection_score: float
    sharpness: float
    consent_at: datetime
    created_at: datetime


class FaceList(BaseModel):
    items: list[FaceOut]


class Liveness(BaseModel):
    checked: bool
    score: float | None = Field(description="From 0 to 1. Null when liveness is disabled.")


class VerifyResult(BaseModel):
    match: bool
    subject_id: uuid.UUID
    score: float = Field(description="Cosine similarity with the subject's closest stored face.")
    threshold: float
    model_id: str
    liveness: Liveness


class IdentifyCandidate(BaseModel):
    subject_id: uuid.UUID
    external_id: str | None
    score: float


class IdentifyResult(BaseModel):
    match: bool
    candidates: list[IdentifyCandidate] = Field(
        description="Subjects scoring at or above the threshold, best first."
    )
    threshold: float
    model_id: str
    liveness: Liveness


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["enroll", "verify", "identify"]
    subject_id: uuid.UUID | None = Field(
        default=None, description="Required for enroll and verify. Not allowed for identify."
    )
    consent: bool = Field(
        default=False,
        description="Required for enroll: you confirm the person agreed to biometric processing.",
    )

    @model_validator(mode="after")
    def _check_subject(self) -> Self:
        if self.action == "identify" and self.subject_id is not None:
            raise ValueError("subject_id is not allowed for identify")
        if self.action != "identify" and self.subject_id is None:
            raise ValueError(f"subject_id is required for {self.action}")
        return self


class SessionCreated(BaseModel):
    id: uuid.UUID
    token: str = Field(description="Give this to the client. It is shown only once.")
    action: str
    subject_id: uuid.UUID | None
    expires_at: datetime
    max_attempts: int


class SessionOut(BaseModel):
    """The session's outcome. Read it on your backend and never trust a result sent by a client."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    status: str
    subject_id: uuid.UUID | None
    attempts: int
    result: dict[str, Any] | None
    created_at: datetime
    expires_at: datetime
    finished_at: datetime | None


class SessionSubmitted(BaseModel):
    """Tells the client the upload was accepted. The outcome goes only to your backend."""

    id: uuid.UUID
    status: str


class Health(BaseModel):
    status: str
    version: str
