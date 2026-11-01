import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from facepass.core.config import Settings
from facepass.core.errors import LivenessFailedError, ProblemError, not_found
from facepass.core.security import utcnow
from facepass.db.models import FaceTemplate, Subject
from facepass.engine.base import FaceAnalysis, FaceEngine
from facepass.engine.image import decode_image
from facepass.schemas import IdentifyCandidate, IdentifyResult, Liveness, VerifyResult
from facepass.services import audit, ratelimit
from facepass.services.audit import Actor

_MAX_IDENTIFY_CANDIDATES = 10


def _failed_verify_key(subject_id: uuid.UUID) -> str:
    return f"failed_verify:{subject_id}"


class FaceService:
    """Enrollment and matching, shared by direct API calls and client upload sessions.

    The enroll, verify and identify methods add their changes to the caller's transaction, so
    a session can commit its outcome atomically with them. Rejections are committed here, so
    the audit trail keeps them even though the caller's work is abandoned.
    """

    def __init__(self, engine: FaceEngine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    def enroll(
        self, db: Session, actor: Actor, subject_id: uuid.UUID, raw: bytes, consent_at: datetime
    ) -> FaceTemplate:
        analysis = self._analyze(db, actor, "face.enroll", subject_id, raw)

        # The row lock serializes concurrent enrollments, keeping the face limit accurate.
        subject = db.scalar(
            select(Subject)
            .where(Subject.id == subject_id, Subject.application_id == actor.application_id)
            .with_for_update()
        )
        if subject is None:
            raise not_found("subject")

        distance = FaceTemplate.embedding.cosine_distance(analysis.embedding.tolist())
        count, min_distance = db.execute(
            select(func.count(FaceTemplate.id), func.min(distance)).where(
                FaceTemplate.subject_id == subject_id,
                FaceTemplate.model_id == self._engine.model_id,
            )
        ).one()

        if count >= self._settings.max_faces_per_subject:
            raise self._reject(
                db,
                actor,
                "face.enroll",
                subject_id,
                ProblemError(
                    409,
                    "face_limit_reached",
                    "Face limit reached",
                    f"A subject can have at most {self._settings.max_faces_per_subject} faces.",
                ),
            )
        # Stops someone with a stolen session from adding their own face to another account.
        if count > 0 and 1 - min_distance < self._settings.match_threshold:
            raise self._reject(
                db,
                actor,
                "face.enroll",
                subject_id,
                ProblemError(
                    409,
                    "face_mismatch",
                    "Face does not match subject",
                    "The new face doesn't match the faces already enrolled for this subject.",
                ),
            )

        template = FaceTemplate(
            id=uuid.uuid4(),
            subject_id=subject_id,
            application_id=actor.application_id,
            embedding=analysis.embedding.tolist(),
            model_id=self._engine.model_id,
            detection_score=analysis.detection_score,
            sharpness=analysis.sharpness,
            consent_at=consent_at,
            created_at=utcnow(),
        )
        db.add(template)
        audit.record(db, actor, "face.enroll", "success", subject_id)
        db.flush()
        return template

    def verify(self, db: Session, actor: Actor, subject_id: uuid.UUID, raw: bytes) -> VerifyResult:
        subject = db.scalar(
            select(Subject).where(
                Subject.id == subject_id, Subject.application_id == actor.application_id
            )
        )
        if subject is None:
            raise not_found("subject")

        settings = self._settings
        key = _failed_verify_key(subject_id)
        window = settings.failed_verify_window_seconds
        limit = settings.failed_verify_limit
        status = ratelimit.peek(db, key, limit, window)
        if not status.allowed:
            raise self._reject(
                db,
                actor,
                "face.verify",
                subject_id,
                ProblemError(
                    429,
                    "too_many_failed_attempts",
                    "Too many failed attempts",
                    "Verification for this subject is temporarily locked.",
                    headers={"Retry-After": str(status.reset_after_seconds)},
                ),
            )
        # Holding a database connection through inference would starve the pool under load.
        db.commit()

        try:
            analysis = self._analyze(db, actor, "face.verify", subject_id, raw)
        except LivenessFailedError:
            ratelimit.hit(db, key, limit, window)
            db.commit()
            raise

        distance = db.scalar(
            select(
                func.min(FaceTemplate.embedding.cosine_distance(analysis.embedding.tolist()))
            ).where(
                FaceTemplate.subject_id == subject_id,
                FaceTemplate.model_id == self._engine.model_id,
            )
        )
        if distance is None:
            raise self._reject(
                db,
                actor,
                "face.verify",
                subject_id,
                ProblemError(
                    409,
                    "subject_not_enrolled",
                    "Subject not enrolled",
                    "Enroll a face for this subject before verifying.",
                ),
            )

        score = 1 - float(distance)
        matched = score >= settings.match_threshold
        if matched:
            ratelimit.reset(db, key)
        else:
            ratelimit.hit(db, key, limit, window)
        audit.record(db, actor, "face.verify", "match" if matched else "no_match", subject_id)
        return VerifyResult(
            match=matched,
            subject_id=subject_id,
            score=round(score, 4),
            threshold=settings.match_threshold,
            model_id=self._engine.model_id,
            liveness=_liveness(analysis),
        )

    def identify(self, db: Session, actor: Actor, raw: bytes) -> IdentifyResult:
        # Connection released: see `verify`.
        db.commit()
        analysis = self._analyze(db, actor, "face.identify", None, raw)

        # An exact scan within the tenant. Every search is filtered by application, so an
        # approximate index would first gather neighbors from other tenants and lose recall.
        distance = FaceTemplate.embedding.cosine_distance(analysis.embedding.tolist())
        threshold = self._settings.identify_threshold
        rows = db.execute(
            select(FaceTemplate.subject_id, Subject.external_id, distance.label("distance"))
            .join(Subject, Subject.id == FaceTemplate.subject_id)
            .where(
                FaceTemplate.application_id == actor.application_id,
                FaceTemplate.model_id == self._engine.model_id,
                distance <= 1 - threshold,
            )
            .order_by(distance)
            .limit(_MAX_IDENTIFY_CANDIDATES * self._settings.max_faces_per_subject)
        ).all()

        best_by_subject: dict[uuid.UUID, IdentifyCandidate] = {}
        for subject_id, external_id, row_distance in rows:
            if subject_id not in best_by_subject:
                best_by_subject[subject_id] = IdentifyCandidate(
                    subject_id=subject_id,
                    external_id=external_id,
                    score=round(1 - float(row_distance), 4),
                )
        candidates = list(best_by_subject.values())[:_MAX_IDENTIFY_CANDIDATES]

        audit.record(db, actor, "face.identify", "match" if candidates else "no_match")
        return IdentifyResult(
            match=bool(candidates),
            candidates=candidates,
            threshold=threshold,
            model_id=self._engine.model_id,
            liveness=_liveness(analysis),
        )

    def _analyze(
        self, db: Session, actor: Actor, action: str, subject_id: uuid.UUID | None, raw: bytes
    ) -> FaceAnalysis:
        settings = self._settings
        try:
            image = decode_image(
                raw, max_pixels=settings.max_image_pixels, max_side=settings.max_image_side
            )
            analysis = self._engine.analyze(image, check_liveness=settings.liveness_enabled)
            if (
                analysis.liveness_score is not None
                and analysis.liveness_score < settings.liveness_threshold
            ):
                raise LivenessFailedError()
        except ProblemError as exc:
            raise self._reject(db, actor, action, subject_id, exc) from None
        return analysis

    @staticmethod
    def _reject(
        db: Session, actor: Actor, action: str, subject_id: uuid.UUID | None, error: ProblemError
    ) -> ProblemError:
        """Commit an audit record for a rejected request, then return the error to raise."""
        audit.record(db, actor, action, error.code, subject_id)
        db.commit()
        return error


def _liveness(analysis: FaceAnalysis) -> Liveness:
    score = analysis.liveness_score
    return Liveness(checked=score is not None, score=None if score is None else round(score, 4))
