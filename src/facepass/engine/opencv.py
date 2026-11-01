import queue
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

from facepass.core.config import Settings
from facepass.core.errors import FaceRejectedError, ProblemError
from facepass.engine.base import Embedding, FaceAnalysis, ImageArray
from facepass.engine.manifest import MINIFASNET_V1SE, MINIFASNET_V2, SFACE, YUNET

_DETECTOR_MIN_SCORE = 0.6
_DETECTOR_NMS_THRESHOLD = 0.3
_DETECTOR_TOP_K = 50
# A smaller face than this, relative to the largest, is treated as a bystander in the background.
_BYSTANDER_RATIO = 0.5
_SHARPNESS_SIZE = (112, 112)
# MiniFASNet class index for a live face. The other two classes are spoof types.
_LIVE_CLASS = 1


@dataclass(frozen=True)
class DetectedFace:
    """One YuNet row: box, five landmarks and a score. `row` is what SFace aligns from."""

    row: NDArray[np.float32]

    @property
    def box(self) -> tuple[float, float, float, float]:
        x, y, width, height = self.row[:4]
        return float(x), float(y), float(width), float(height)

    @property
    def score(self) -> float:
        return float(self.row[-1])


class _LivenessModel:
    """Silent-Face MiniFASNet ensemble. Inputs are unnormalized BGR crops at a model's scale."""

    _INPUT_SIZE = (80, 80)

    def __init__(self, models_dir: Path) -> None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self._models = [
            (
                ort.InferenceSession(
                    str(models_dir / model.filename),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                ),
                scale,
            )
            for model, scale in ((MINIFASNET_V2, 2.7), (MINIFASNET_V1SE, 4.0))
        ]

    def score(self, image: ImageArray, face: DetectedFace) -> float:
        """Return the probability, from 0 to 1, that the face is live."""
        probabilities = np.zeros(3, dtype=np.float64)
        for session, scale in self._models:
            crop = self._crop(image, face, scale).astype(np.float32)
            tensor = np.transpose(crop, (2, 0, 1))[np.newaxis]
            logits = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
            exponentials = np.exp(logits - logits.max())
            probabilities += exponentials / exponentials.sum()
        return float(probabilities[_LIVE_CLASS] / len(self._models))

    def _crop(self, image: ImageArray, face: DetectedFace, scale: float) -> ImageArray:
        image_height, image_width = image.shape[:2]
        x, y, width, height = face.box
        scale = min((image_height - 1) / height, (image_width - 1) / width, scale)
        new_width, new_height = width * scale, height * scale
        center_x, center_y = x + width / 2, y + height / 2
        left = max(0, int(center_x - new_width / 2))
        top = max(0, int(center_y - new_height / 2))
        right = min(image_width - 1, int(center_x + new_width / 2))
        bottom = min(image_height - 1, int(center_y + new_height / 2))
        resized: ImageArray = cv2.resize(
            image[top : bottom + 1, left : right + 1], self._INPUT_SIZE
        )
        return resized


class _Worker:
    """OpenCV's detector and recognizer are stateful, so each worker owns its own pair."""

    def __init__(self, models_dir: Path) -> None:
        self.detector = cv2.FaceDetectorYN.create(
            str(models_dir / YUNET.filename),
            "",
            (320, 320),
            _DETECTOR_MIN_SCORE,
            _DETECTOR_NMS_THRESHOLD,
            _DETECTOR_TOP_K,
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(models_dir / SFACE.filename), "")


class OpenCVFaceEngine:
    """YuNet detection, SFace embeddings and MiniFASNet liveness, all running on the CPU."""

    model_id = "sface-2021dec"

    def __init__(self, settings: Settings) -> None:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        self._settings = settings
        self._workers: queue.Queue[_Worker] = queue.Queue()
        for _ in range(settings.inference_workers):
            self._workers.put(_Worker(settings.models_dir))
        self._liveness = _LivenessModel(settings.models_dir) if settings.liveness_enabled else None

    @contextmanager
    def _worker(self) -> Iterator[_Worker]:
        try:
            worker = self._workers.get(timeout=self._settings.inference_wait_seconds)
        except queue.Empty:
            raise ProblemError(
                503,
                "service_busy",
                "Service busy",
                "All inference workers are busy. Retry shortly.",
                headers={"Retry-After": "5"},
            ) from None
        try:
            yield worker
        finally:
            self._workers.put(worker)

    def detect(self, image: ImageArray) -> list[DetectedFace]:
        with self._worker() as worker:
            return self._detect(worker, image)

    def embed(self, image: ImageArray, face: DetectedFace) -> Embedding:
        with self._worker() as worker:
            return self._embed(worker, image, face)

    def analyze(self, image: ImageArray, *, check_liveness: bool) -> FaceAnalysis:
        with self._worker() as worker:
            face = self._select_face(self._detect(worker, image), image)
            sharpness = self._check_quality(image, face)
            embedding = self._embed(worker, image, face)
            liveness_score = None
            if check_liveness:
                if self._liveness is None:
                    raise RuntimeError("Liveness was requested but its models are not loaded")
                liveness_score = self._liveness.score(image, face)
        return FaceAnalysis(embedding, face.score, sharpness, liveness_score)

    @staticmethod
    def _detect(worker: _Worker, image: ImageArray) -> list[DetectedFace]:
        height, width = image.shape[:2]
        worker.detector.setInputSize((width, height))
        _, rows = worker.detector.detect(image)
        return [] if rows is None else [DetectedFace(row) for row in rows]

    @staticmethod
    def _embed(worker: _Worker, image: ImageArray, face: DetectedFace) -> Embedding:
        aligned = worker.recognizer.alignCrop(image, face.row)
        feature = worker.recognizer.feature(aligned).flatten()
        normalized: Embedding = (feature / np.linalg.norm(feature)).astype(np.float32)
        return normalized

    def _select_face(self, faces: list[DetectedFace], image: ImageArray) -> DetectedFace:
        confident = [face for face in faces if face.score >= self._settings.min_detection_score]
        if not confident:
            raise FaceRejectedError(
                "no_face_detected",
                "No face detected",
                "Center one face in the frame, facing the camera.",
            )
        largest_width = max(face.box[2] for face in confident)
        prominent = [f for f in confident if f.box[2] >= largest_width * _BYSTANDER_RATIO]
        if len(prominent) > 1:
            raise FaceRejectedError(
                "multiple_faces_detected",
                "Multiple faces detected",
                "Only one person may be in the frame.",
            )
        face = prominent[0]
        x, y, width, height = face.box
        image_height, image_width = image.shape[:2]
        if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
            raise FaceRejectedError(
                "face_out_of_frame", "Face out of frame", "Keep the whole face inside the frame."
            )
        if min(width, height) < self._settings.min_face_size:
            raise FaceRejectedError(
                "face_too_small", "Face too small", "Move closer to the camera."
            )
        return face

    def _check_quality(self, image: ImageArray, face: DetectedFace) -> float:
        x, y, width, height = (int(value) for value in face.box)
        crop = cv2.cvtColor(image[y : y + height, x : x + width], cv2.COLOR_BGR2GRAY)
        # A fixed size makes sharpness comparable across face sizes.
        crop = cv2.resize(crop, _SHARPNESS_SIZE)
        brightness = float(crop.mean())
        if not self._settings.min_brightness <= brightness <= self._settings.max_brightness:
            raise FaceRejectedError(
                "poor_lighting", "Poor lighting", "Use even lighting on the face."
            )
        sharpness = float(cv2.Laplacian(crop, cv2.CV_64F).var())
        if sharpness < self._settings.min_sharpness:
            raise FaceRejectedError("face_too_blurry", "Face too blurry", "Hold the camera still.")
        return sharpness
