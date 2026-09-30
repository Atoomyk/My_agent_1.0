from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps


def prepare_image(path: Path) -> dict:
    raw = path.read_bytes()
    return prepare_image_bytes(raw)


def prepare_image_bytes(raw: bytes) -> dict:
    if len(raw) > 20_000_000:
        raise ValueError("изображение слишком большое")
    with Image.open(io.BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB":
            image = image.convert("RGB")
        if max(image.size) > 1280:
            image.thumbnail((1280, 1280))
        width, height = image.size
        quality = 85
        data = b""
        while quality >= 40:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            data = buffer.getvalue()
            if len(data) <= 1_500_000:
                break
            quality -= 15
    if len(data) > 4_000_000:
        raise ValueError("изображение слишком большое")
    encoded = base64.standard_b64encode(data).decode("ascii")
    return {"media_type": "image/jpeg", "b64": encoded, "width": width, "height": height}
