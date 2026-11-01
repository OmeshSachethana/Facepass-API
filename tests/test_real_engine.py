"""End-to-end tests with the real models, real photos and a real database."""

import io
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.core.errors import FaceRejectedError
from facepass.engine.base import ImageArray
from facepass.engine.image import decode_image
from facepass.engine.manifest import ALL_MODELS
from facepass.engine.opencv import OpenCVFaceEngine
from facepass.main import create_app
from tests.conftest import Tenant
from tests.helpers import create_subject, enroll, upload, verify

MODELS_DIR = Path("models")
FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(
        not all((MODELS_DIR / model.filename).exists() for model in ALL_MODELS),
        reason="Run `facepass models download` first",
    ),
]


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _image(name: str) -> ImageArray:
    return decode_image(_bytes(name), max_pixels=40_000_000, max_side=1280)


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def engine(migrated_database: str) -> OpenCVFaceEngine:
    return OpenCVFaceEngine(
        Settings(
            _env_file=None,
            database_url=migrated_database,
            secret_key="x" * 32,
        )
    )


@pytest.fixture
def real_client(
    settings: Settings, session_factory: sessionmaker[Session], engine: OpenCVFaceEngine
) -> Iterator[TestClient]:
    # The liveness threshold isn't under test here. These are photos of photos, and
    # liveness behavior needs its own evaluation with real captures.
    relaxed = settings.model_copy(update={"liveness_threshold": 0.0})
    with TestClient(create_app(relaxed, engine)) as client:
        yield client


def test_same_person_scores_above_threshold(engine: OpenCVFaceEngine) -> None:
    first = engine.analyze(_image("armstrong_a.jpg"), check_liveness=False).embedding
    second = engine.analyze(_image("armstrong_b.jpg"), check_liveness=False).embedding
    assert float(first @ second) > 0.40


def test_different_people_score_below_threshold(engine: OpenCVFaceEngine) -> None:
    first = engine.analyze(_image("armstrong_a.jpg"), check_liveness=False).embedding
    second = engine.analyze(_image("ride.jpg"), check_liveness=False).embedding
    assert float(first @ second) < 0.40


def test_embedding_is_normalized_128_floats(engine: OpenCVFaceEngine) -> None:
    analysis = engine.analyze(_image("ride.jpg"), check_liveness=False)
    assert analysis.embedding.shape == (128,)
    assert np.isclose(np.linalg.norm(analysis.embedding), 1.0)
    assert analysis.liveness_score is None


def test_liveness_score_is_a_probability(engine: OpenCVFaceEngine) -> None:
    score = engine.analyze(_image("ride.jpg"), check_liveness=True).liveness_score
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_blank_image_has_no_face(engine: OpenCVFaceEngine) -> None:
    blank = np.full((480, 640, 3), 128, dtype=np.uint8)
    with pytest.raises(FaceRejectedError) as caught:
        engine.analyze(blank, check_liveness=False)
    assert caught.value.code == "no_face_detected"


def test_two_people_in_frame_are_rejected(engine: OpenCVFaceEngine) -> None:
    combined = np.hstack(
        [
            np.array(Image.fromarray(_image(name)).resize((480, 600)), dtype=np.uint8)
            for name in ("armstrong_a.jpg", "ride.jpg")
        ]
    )
    with pytest.raises(FaceRejectedError) as caught:
        engine.analyze(combined, check_liveness=False)
    assert caught.value.code == "multiple_faces_detected"


def test_tiny_face_is_rejected(engine: OpenCVFaceEngine) -> None:
    source = Image.fromarray(_image("ride.jpg"))
    # Small enough to fail the size gate, but still large enough for YuNet to find.
    width = int(source.width * 0.2)
    small = np.array(source.resize((width, int(source.height * 0.2))), dtype=np.uint8)
    canvas = np.full((800, 800, 3), 128, dtype=np.uint8)
    canvas[: small.shape[0], : small.shape[1]] = small
    with pytest.raises(FaceRejectedError) as caught:
        engine.analyze(canvas, check_liveness=False)
    assert caught.value.code == "face_too_small"


def test_blurred_face_is_rejected(engine: OpenCVFaceEngine) -> None:
    # A heavy blur keeps the face findable but removes the detail recognition depends on.
    blurred = cv2.GaussianBlur(_image("ride.jpg"), (0, 0), 7)
    with pytest.raises(FaceRejectedError) as caught:
        engine.analyze(blurred, check_liveness=False)
    assert caught.value.code == "face_too_blurry"


def test_dark_image_is_rejected(engine: OpenCVFaceEngine) -> None:
    dark = (_image("ride.jpg") * 0.12).astype(np.uint8)
    with pytest.raises(FaceRejectedError) as caught:
        engine.analyze(dark, check_liveness=False)
    assert caught.value.code == "poor_lighting"


def test_sideways_phone_photo_is_corrected_by_exif(engine: OpenCVFaceEngine) -> None:
    upright = Image.open(FIXTURES / "ride.jpg").convert("RGB")
    # Pixels stored rotated, with EXIF saying how to display them, as phones do.
    stored = upright.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    stored.save(buffer, "JPEG", quality=95, exif=exif)

    corrected = decode_image(buffer.getvalue(), max_pixels=40_000_000, max_side=1280)
    corrected_embedding = engine.analyze(corrected, check_liveness=False).embedding
    original_embedding = engine.analyze(_image("ride.jpg"), check_liveness=False).embedding

    assert float(corrected_embedding @ original_embedding) > 0.9


def test_full_flow_over_http(real_client: TestClient, tenant: Tenant) -> None:
    subject_id = create_subject(real_client, tenant, "armstrong")
    assert enroll(real_client, tenant, subject_id, _bytes("armstrong_a.jpg")).status_code == 201

    matched = verify(real_client, tenant, subject_id, _bytes("armstrong_b.jpg")).json()
    assert matched["match"] is True
    assert matched["model_id"] == "sface-2021dec"

    other = verify(real_client, tenant, subject_id, _bytes("ride.jpg")).json()
    assert other["match"] is False

    identified = real_client.post(
        "/v1/identify", files=upload(_bytes("armstrong_b.jpg")), headers=tenant.headers
    ).json()
    assert [c["external_id"] for c in identified["candidates"]] == ["armstrong"]

    mismatch = enroll(real_client, tenant, subject_id, _bytes("ride.jpg"))
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "face_mismatch"


def test_landscape_png_upload_works_over_http(real_client: TestClient, tenant: Tenant) -> None:
    source = Image.open(FIXTURES / "ride.jpg").convert("RGB")
    canvas = Image.new("RGB", (source.width * 2, source.height), (120, 120, 120))
    canvas.paste(source, (source.width // 2, 0))
    subject_id = create_subject(real_client, tenant)
    assert enroll(real_client, tenant, subject_id, _encode(canvas)).status_code == 201
