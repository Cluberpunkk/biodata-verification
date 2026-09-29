"""
image_fetcher.py
Downloads govt ID images from public URLs.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20  # seconds
USER_AGENT = "ProjectTrust-Verifier/1.0"


def fetch_image_bytes(url: str) -> Optional[bytes]:
    """
    Download image at URL. Returns raw bytes on success, None on failure.

    Caller is expected to handle None gracefully (tag = INVALID_DOWNLOAD).
    """
    if not url or not url.startswith(("http://", "https://")):
        logger.warning(f"Invalid URL skipped: {url!r}")
        return None

    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=False,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to download {url}: {e}")
        return None

    content = resp.content
    if not content:
        logger.warning(f"Empty response from {url}")
        return None

    # Sanity-check it's an image
    content_type = resp.headers.get("content-type", "").lower()
    if not content_type.startswith("image/"):
        logger.warning(f"Non-image content-type at {url}: {content_type}")
        # Still return — sometimes CDN sends generic application/octet-stream
        # Gemini will reject it cleanly if not a real image.

    return content


def enhance_image_bytes(image_bytes: bytes) -> Optional[bytes]:
    """Upscale + sharpen + auto-contrast a low-quality ID image to aid OCR.

    Targeted at blurry / small / low-contrast govt-ID photos. Returns JPEG
    bytes, or None if the bytes can't be opened as an image.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # honour camera rotation
        img = img.convert("RGB")
    except Exception as e:
        logger.warning(f"Could not open image for enhancement: {e}")
        return None

    # Upscale small images so fine print becomes legible (target ~2000px long edge)
    w, h = img.size
    longest = max(w, h)
    if longest < 2000:
        scale = 2000 / longest
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    img = ImageOps.autocontrast(img, cutoff=1)            # stretch contrast
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    img = ImageEnhance.Sharpness(img).enhance(1.6)        # extra edge sharpening

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()
    


def guess_mime_type(image_bytes: bytes) -> str:
    """Detect mime type from magic bytes."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    # Default — Gemini will fail loudly if it can't parse
    return "image/jpeg"
