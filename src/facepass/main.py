import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from facepass import __version__
from facepass.api.deps import DbDep, problem_responses
from facepass.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from facepass.api.v1 import router as v1_router
from facepass.core.config import Settings, get_settings
from facepass.core.errors import ProblemError
from facepass.core.logging import configure_logging, request_id_var
from facepass.db.session import build_engine, build_session_factory
from facepass.engine.base import FaceEngine
from facepass.schemas import Health
from facepass.services.faces import FaceService

logger = logging.getLogger(__name__)


def _problem(
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    content = {
        "type": f"urn:facepass:problem:{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id_var.get(),
        **extra,
    }
    if detail is not None:
        content["detail"] = detail
    return JSONResponse(
        content, status_code=status, media_type="application/problem+json", headers=headers
    )


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def handle_problem(_: Request, exc: ProblemError) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.title, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field locations and messages: the rejected input could be biometric data.
        errors = [
            {"loc": [str(part) for part in error["loc"]], "message": error["msg"]}
            for error in exc.errors()
        ]
        return _problem(422, "validation_error", "Invalid request", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            exc.status_code, f"http_{exc.status_code}", str(exc.detail), headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error", exc_info=exc)
        return _problem(500, "internal_error", "Internal server error")


def _load_engine(settings: Settings) -> FaceEngine:
    # Imported here so tests with a fake engine don't need the model files.
    from facepass.engine.opencv import OpenCVFaceEngine

    return OpenCVFaceEngine(settings)


def create_app(settings: Settings | None = None, engine: FaceEngine | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = build_engine(settings.database_url)
        face_engine = engine or _load_engine(settings)
        app.state.settings = settings
        app.state.session_factory = build_session_factory(database)
        app.state.face_service = FaceService(face_engine, settings)
        yield
        database.dispose()

    app = FastAPI(
        title="FacePass API",
        version=__version__,
        description=(
            "Face enrollment, verification and identification for web and mobile applications."
        ),
        lifespan=lifespan,
    )
    _register_exception_handlers(app)

    # Added last-to-first: the request context wraps everything, so all responses get its headers.
    app.add_middleware(BodySizeLimitMiddleware, max_upload_bytes=settings.max_upload_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(v1_router)

    @app.get("/healthz", tags=["Operations"])
    def healthz() -> Health:
        """Report that the process is running."""
        return Health(status="ok", version=__version__)

    @app.get("/readyz", tags=["Operations"], responses=problem_responses(503))
    def readyz(db: DbDep) -> Health:
        """Report that the service can handle traffic. Models load before startup completes."""
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Readiness check failed")
            raise ProblemError(503, "database_unavailable", "Database unavailable") from None
        return Health(status="ready", version=__version__)

    return app
