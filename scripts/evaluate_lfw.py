"""Measure recognition accuracy on the LFW benchmark, using the standard 10-fold protocol.

Download the funneled images and pairs.txt from https://vis-www.cs.umass.edu/lfw/ then run:

    python scripts/evaluate_lfw.py --images path/to/lfw_funneled --pairs path/to/pairs.txt

Results for the current model are recorded in docs/evaluation.md.
"""

import argparse
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from facepass.core.config import Settings
from facepass.engine.base import Embedding, ImageArray
from facepass.engine.opencv import DetectedFace, OpenCVFaceEngine

_FOLD_SIZE = 600
_THRESHOLDS = (0.30, 0.363, 0.40, 0.45, 0.50, 0.55, 0.60)


def _image_path(images: Path, name: str, number: str) -> Path:
    return images / name / f"{name}_{int(number):04d}.jpg"


def _center_face(faces: list[DetectedFace], image: ImageArray) -> DetectedFace:
    """LFW photos often include bystanders, and its protocol is about the centered person."""
    height, width = image.shape[:2]

    def distance(face: DetectedFace) -> float:
        x, y, face_width, face_height = face.box
        return float(np.hypot(x + face_width / 2 - width / 2, y + face_height / 2 - height / 2))

    return min(faces, key=distance)


def evaluate(engine: OpenCVFaceEngine, images: Path, pairs: Path) -> None:
    cache: dict[Path, Embedding | None] = {}

    def embedding(path: Path) -> Embedding | None:
        if path not in cache:
            with Image.open(path) as source:
                rgb = np.asarray(source.convert("RGB"), dtype=np.uint8)
            bgr: ImageArray = rgb[:, :, ::-1].copy()
            faces = engine.detect(bgr)
            cache[path] = engine.embed(bgr, _center_face(faces, bgr)) if faces else None
        return cache[path]

    scores: list[float] = []
    labels: list[bool] = []
    folds: list[int] = []
    missed = 0
    for index, line in enumerate(pairs.read_text().splitlines()[1:]):
        fields = line.split("\t")
        if len(fields) == 3:
            name, first, second = fields
            pair = _image_path(images, name, first), _image_path(images, name, second)
        else:
            name_a, first, name_b, second = fields
            pair = _image_path(images, name_a, first), _image_path(images, name_b, second)
        embedding_a, embedding_b = embedding(pair[0]), embedding(pair[1])
        if embedding_a is None or embedding_b is None:
            missed += 1
            continue
        scores.append(float(embedding_a @ embedding_b))
        labels.append(len(fields) == 3)
        folds.append(index // _FOLD_SIZE)

    score = np.array(scores)
    label = np.array(labels)
    fold = np.array(folds)
    print(f"pairs: {len(score)}, skipped with no face: {missed}, images: {len(cache)}")
    _print_accuracy(score, label, fold)
    genuine, impostor = score[label], score[~label]
    for threshold in _THRESHOLDS:
        far = float((impostor >= threshold).mean())
        frr = float((genuine < threshold).mean())
        print(f"threshold {threshold:.3f}: false accepts {far:.4%}, false rejects {frr:.4%}")


def _print_accuracy(
    score: NDArray[np.float64], label: NDArray[np.bool_], fold: NDArray[np.int64]
) -> None:
    grid = np.arange(0, 1, 0.005)
    accuracies = []
    for held_out in np.unique(fold):
        train = fold != held_out
        best = max(grid, key=lambda t: float(((score[train] >= t) == label[train]).mean()))
        accuracies.append(float(((score[~train] >= best) == label[~train]).mean()))
    print(f"10-fold accuracy: {np.mean(accuracies):.4%} +/- {np.std(accuracies):.4%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--models", type=Path, default=Path("models"))
    args = parser.parse_args()
    settings = Settings(
        _env_file=None,
        database_url="unused",
        secret_key="unused-" * 6,
        models_dir=args.models,
        liveness_enabled=False,
        inference_workers=1,
    )
    evaluate(OpenCVFaceEngine(settings), args.images, args.pairs)


if __name__ == "__main__":
    main()
