from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

Embedding = NDArray[np.float32]
ImageArray = NDArray[np.uint8]


@dataclass(frozen=True)
class FaceAnalysis:
    embedding: Embedding
    detection_score: float
    sharpness: float
    liveness_score: float | None


class FaceEngine(Protocol):
    """The seam for swapping recognition models or moving to a hosted service."""

    @property
    def model_id(self) -> str:
        """Identifies the embedding space. Embeddings with different ids are not comparable."""
        ...

    def analyze(self, image: ImageArray, *, check_liveness: bool) -> FaceAnalysis:
        """Analyze a BGR image containing one face.

        Raises FaceRejectedError when the image is unusable, for example with no clear face.
        Embeddings are L2-normalized.
        """
        ...
