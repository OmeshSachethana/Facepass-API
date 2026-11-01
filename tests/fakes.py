"""A deterministic engine, so API tests can choose each photo's outcome through its color."""

import io

import numpy as np
from PIL import Image

from facepass.core.errors import FaceRejectedError
from facepass.engine.base import FaceAnalysis, ImageArray

_AXES = 128


def _unit(axis: int, secondary: int | None = None, weight: float = 0.0) -> np.ndarray:
    vector = np.zeros(_AXES, dtype=np.float32)
    vector[axis] = 1.0
    if secondary is not None:
        vector[secondary] = weight
    normalized: np.ndarray = vector / np.linalg.norm(vector)
    return normalized


def photo(red: int, green: int = 0, blue: int = 0, image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (red, green, blue)).save(buffer, format=image_format)
    return buffer.getvalue()


def person(index: int) -> bytes:
    """A photo of a person. Different indexes produce orthogonal embeddings."""
    return photo(index)


def similar_person(index: int) -> bytes:
    """A second photo of a person, close to `person(index)` but not identical."""
    return photo(index, 1)


def no_face() -> bytes:
    return photo(0, 0, 200)


def spoof(index: int) -> bytes:
    """A photo that matches `person(index)` but fails the liveness check."""
    return photo(index, 0, 100)


class FakeFaceEngine:
    model_id = "fake-1"

    def analyze(self, image: ImageArray, *, check_liveness: bool) -> FaceAnalysis:
        blue, green, red = (int(value) for value in image[0, 0])
        if blue == 200:
            raise FaceRejectedError("no_face_detected", "No face detected", "No face.")
        embedding = _unit(red, red + 1, 0.2) if green else _unit(red)
        liveness = None
        if check_liveness:
            liveness = 0.05 if blue == 100 else 0.99
        return FaceAnalysis(embedding, 0.95, 100.0, liveness)
