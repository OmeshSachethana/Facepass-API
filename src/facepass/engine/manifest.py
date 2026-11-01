import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    license: str


YUNET = ModelFile(
    "face_detection_yunet_2023mar.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "MIT",
)
SFACE = ModelFile(
    "face_recognition_sface_2021dec.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    "Apache-2.0",
)
MINIFASNET_V2 = ModelFile(
    "MiniFASNetV2.onnx",
    "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx",
    "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
    "Apache-2.0",
)
MINIFASNET_V1SE = ModelFile(
    "MiniFASNetV1SE.onnx",
    "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV1SE.onnx",
    "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676",
    "Apache-2.0",
)
ALL_MODELS = (YUNET, SFACE, MINIFASNET_V2, MINIFASNET_V1SE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_models(directory: Path) -> None:
    """Download any missing model and verify every checksum, so weights can't silently change."""
    directory.mkdir(parents=True, exist_ok=True)
    for model in ALL_MODELS:
        target = directory / model.filename
        if not target.exists():
            partial = target.with_suffix(".part")
            with (
                httpx.stream("GET", model.url, follow_redirects=True, timeout=120) as response,
                partial.open("wb") as file,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    file.write(chunk)
            partial.replace(target)
        actual = _sha256(target)
        if actual != model.sha256:
            target.unlink()
            raise RuntimeError(
                f"Checksum mismatch for {model.filename}: expected {model.sha256}, got {actual}"
            )
