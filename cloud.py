"""Cloudinary upload helper, used by sitedb.push_media_file so photos and
videos live on a CDN instead of as blobs read through Turso's Hrana stream.
Large video blobs were slow enough to push or pull that the stream expired
mid-request (see push_media_file / pipeline._TursoConnection for the symptom).
"""
from __future__ import annotations

import os
from pathlib import Path


def configured() -> bool:
    return bool(os.environ.get("CLOUDINARY_CLOUD_NAME") and os.environ.get("CLOUDINARY_API_KEY")
                and os.environ.get("CLOUDINARY_API_SECRET"))


def upload(full: Path, rel_path: str, resource_type: str) -> str:
    """Upload a file already saved under MEDIA_DIR to Cloudinary, keyed by its
    relative path so re-uploading the same path overwrites instead of
    duplicating, and return its secure URL."""
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(
        cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
        api_key=os.environ["CLOUDINARY_API_KEY"],
        api_secret=os.environ["CLOUDINARY_API_SECRET"],
        secure=True,
    )
    public_id = "viraasat/" + rel_path.rsplit(".", 1)[0]
    uploader = cloudinary.uploader.upload_large if resource_type == "video" else cloudinary.uploader.upload
    res = uploader(str(full), resource_type=resource_type, public_id=public_id, overwrite=True)
    return res["secure_url"]
