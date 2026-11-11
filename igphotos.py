"""Download the images of an Instagram photo post or carousel.

yt-dlp only handles videos, so photo posts are read from Instagram's public embed
page (no login needed). If that fails and gallery-dl is installed
(python3 -m pip install gallery-dl), it is tried next, using your browser's
Instagram sign-in when VG_COOKIES_BROWSER is set.
"""
from __future__ import annotations

import html as htmllib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pipeline as p  # trusts the system certificates (truststore) and loads .env

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic"}


class PhotoError(Exception):
    """A photo post could not be downloaded; the message is safe to show."""


def _get(url: str, timeout: int = 30) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise PhotoError(f"Instagram answered HTTP {e.code} for {url.split('?')[0]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PhotoError(f"Could not reach Instagram ({e}).") from e


# ------------------------------------------------------------ embed page
def _walk(node, out: list):
    """Collect (display_url, is_video) of every slide, in document order. A video
    slide keeps its place (its display_url is the cover image) so slide numbers
    match Instagram's img_index."""
    if isinstance(node, dict):
        if node.get("display_url"):
            out.append((node["display_url"], bool(node.get("is_video"))))
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _unescape_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s.replace("\\/", "/").replace("\\u0026", "&")


def parse_slides(page: str) -> list[tuple[str, bool]]:
    """Every slide of a post as (full-size image URL, is_video), in order."""
    slides: list[tuple[str, bool]] = []
    m = re.search(r'"contextJSON"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    if m:
        try:
            _walk(json.loads(_unescape_json_str(m.group(1))), slides)
        except (json.JSONDecodeError, TypeError):
            pass
    if not slides:  # display_url inside other inline JSON, escaped once or twice
        for raw in re.findall(r'\\?"display_url\\?"\s*:\s*\\?"(https?:(?:[^"\\]|\\.)*?)\\?"', page):
            u = raw
            for _ in range(2):
                u = _unescape_json_str(u)
            slides.append((u, False))
    if not slides:
        for tag in re.findall(r"<img[^>]*EmbeddedMediaImage[^>]*>", page):
            src = re.search(r'src="([^"]+)"', tag)
            if src:
                slides.append((htmllib.unescape(src.group(1)), False))
    seen, out = set(), []
    for u, vid in slides:  # a carousel repeats slide 1 as the post's own display_url
        key = urllib.parse.urlparse(u).path
        if key and key not in seen:
            seen.add(key)
            out.append((u, vid))
    return out


def parse_embed(page: str) -> list[str]:
    """Image URLs of the photo slides only, in order."""
    return [u for u, vid in parse_slides(page) if not vid]


def _ext(url: str, ctype: str) -> str:
    ext = IMAGE_TYPES.get(ctype.split(";")[0].strip().lower())
    if ext:
        return ext
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in p.IMAGE_EXT else ".jpg"


def via_embed(shortcode: str, get=None) -> list[tuple[str, bool]]:
    get = get or _get
    body, _ = get(f"https://www.instagram.com/p/{shortcode}/embed/captioned/")
    return parse_slides(body.decode("utf-8", "replace"))


# ------------------------------------------------------------ gallery-dl
def via_gallery_dl(shortcode: str) -> list[tuple[bytes, str]]:
    if not importlib.util.find_spec("gallery_dl"):
        raise PhotoError("gallery-dl is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "gallery_dl", "-D", tmp, "-f", "{num:>02}.{extension}"]
        if os.environ.get("VG_COOKIES_BROWSER"):
            cmd += ["--cookies-from-browser", os.environ["VG_COOKIES_BROWSER"]]
        cmd.append(f"https://www.instagram.com/p/{shortcode}/")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        files = sorted(f for f in Path(tmp).iterdir() if f.suffix.lower() in p.IMAGE_EXT)
        if not files:
            raise PhotoError("gallery-dl got no images: " + (res.stderr.strip().splitlines() or ["no output"])[-1])
        return [(f.read_bytes(), f.suffix.lower()) for f in files]


# ------------------------------------------------------------- public API
def fetch_slide(shortcode: str, slide: int = 1, get=None, gallery=None) -> tuple[bytes, str, str]:
    """(image bytes, extension, note) for one slide of a photo post.
    Falls back to slide 1 if the post has fewer slides than asked."""
    slide = max(1, int(slide or 1))
    get = get or _get
    errors = []
    try:
        slides = via_embed(shortcode, get)
        if slides:
            note = ""
            if slide > len(slides):
                note = f"only {len(slides)} slide(s) found, took slide 1"
                slide = 1
            url, is_video = slides[slide - 1]
            if is_video:
                note = (note + "; " if note else "") + f"slide {slide} is a video, saved its cover image"
            data, ctype = get(url)
            if not ctype.lower().startswith("image/"):
                raise PhotoError(f"expected an image, got {ctype or 'unknown type'}")
            return data, _ext(url, ctype), note
        errors.append("embed page had no images (post may be private, removed or video-only)")
    except PhotoError as e:
        errors.append(f"embed page: {e}")
    try:
        imgs = (gallery or via_gallery_dl)(shortcode)
        note = ""
        if slide > len(imgs):
            note = f"only {len(imgs)} slide(s) found, took slide 1"
            slide = 1
        data, ext = imgs[slide - 1]
        return data, ext, note
    except PhotoError as e:
        errors.append(f"gallery-dl: {e}")
    except (subprocess.TimeoutExpired, OSError) as e:
        errors.append(f"gallery-dl: {e}")
    raise PhotoError("; ".join(errors))
