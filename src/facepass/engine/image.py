from io import BytesIO

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from facepass.core.errors import ProblemError
from facepass.engine.base import ImageArray

ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")


def decode_image(raw: bytes, *, max_pixels: int, max_side: int) -> ImageArray:
    """Decode an upload into a BGR array with EXIF rotation applied.

    The format comes from the file's bytes, not the client's content type. Phone cameras
    usually store rotation in EXIF, and detection fails on a sideways face without this step.
    """
    try:
        with Image.open(BytesIO(raw), formats=ALLOWED_FORMATS) as source:
            # Image.size only reads the header, which stops decompression bombs before decoding.
            width, height = source.size
            if width * height > max_pixels:
                raise ProblemError(
                    413,
                    "image_too_large",
                    "Image too large",
                    f"The image must have at most {max_pixels} pixels.",
                )
            upright = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise _unsupported_image() from None
    except (OSError, SyntaxError, ValueError):
        # Pillow raises these for truncated or corrupt files.
        raise _unsupported_image() from None

    upright.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    rgb = np.asarray(upright, dtype=np.uint8)
    return rgb[:, :, ::-1].copy()


def _unsupported_image() -> ProblemError:
    return ProblemError(
        415,
        "unsupported_image",
        "Unsupported image",
        f"Upload a valid image in one of these formats: {', '.join(ALLOWED_FORMATS)}.",
    )
