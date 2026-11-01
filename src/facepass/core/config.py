from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from FACEPASS_* environment variables or `.env`."""

    model_config = SettingsConfigDict(env_prefix="FACEPASS_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str
    secret_key: SecretStr = Field(min_length=32)
    cors_origins: list[str] = []
    models_dir: Path = Path("models")

    # Upload limits
    max_upload_bytes: int = 8 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_image_side: int = 1280

    # Matching. Measured on LFW, see docs/evaluation.md. Recalibrate after changing the model.
    match_threshold: float = Field(default=0.40, ge=-1, le=1)
    identify_threshold: float = Field(default=0.45, ge=-1, le=1)

    # Image quality gates. See docs/evaluation.md for the measured distributions.
    min_detection_score: float = Field(default=0.80, ge=0, le=1)
    min_face_size: int = 64
    min_sharpness: float = 15.0
    min_brightness: float = 40.0
    max_brightness: float = 220.0

    # Liveness. The threshold has NOT been calibrated against real spoof attacks.
    liveness_enabled: bool = True
    liveness_threshold: float = Field(default=0.50, ge=0, le=1)

    # Inference capacity per process. Requests over capacity wait, then receive a 503.
    inference_workers: int = Field(default=2, ge=1)
    inference_wait_seconds: float = 30.0

    # Subjects and sessions
    max_faces_per_subject: int = Field(default=5, ge=1)
    session_ttl_seconds: int = Field(default=120, ge=10)
    session_max_attempts: int = Field(default=3, ge=1)

    # Abuse protection
    rate_limit_per_minute: int = Field(default=120, ge=1)
    failed_verify_limit: int = Field(default=5, ge=1)
    failed_verify_window_seconds: int = Field(default=900, ge=1)

    # Background worker
    audit_retention_days: int = Field(default=90, ge=1)
    webhook_max_attempts: int = Field(default=8, ge=1)
    webhook_timeout_seconds: float = 10.0
    worker_poll_seconds: float = 2.0

    @model_validator(mode="after")
    def _refuse_placeholder_secret_in_production(self) -> Self:
        if self.environment == "production" and "change-me" in self.secret_key.get_secret_value():
            raise ValueError("Set FACEPASS_SECRET_KEY to a random value in production")
        return self


@lru_cache
def get_settings() -> Settings:
    # Required values come from the environment.
    return Settings()
