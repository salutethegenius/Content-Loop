"""Serve generated images from the Railway volume.

Mounted at /static/images/{filename}. Files are written by
`image_generator.save_image` to IMAGE_DIR (default /data/images), which is a
Railway volume mounted on the nova service. Long-cache headers since filenames
include a unix timestamp and are never overwritten.
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")

# 30-day immutable cache. Filenames are content-addressed by unix timestamp,
# so a regenerated image gets a new URL and clients always re-fetch the new one.
CACHE_MAX_AGE = 60 * 60 * 24 * 30


@router.get("/static/images/{filename}")
def serve_image(filename: str):
    # Reject path traversal attempts. Allow only bare filenames (no slashes,
    # no dots prefix).
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Bad filename")
    if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise HTTPException(status_code=400, detail="Unsupported image type")

    path = os.path.join(IMAGE_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(
        path,
        # Let FileResponse infer the media type from the extension.
        headers={
            "Cache-Control": f"public, max-age={CACHE_MAX_AGE}, immutable",
        },
    )
