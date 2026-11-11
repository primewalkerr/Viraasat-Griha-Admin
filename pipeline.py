#!/usr/bin/env python3
"""Viraasat reel pipeline.

Share an Instagram reel link -> save the video, place and craft details to a
local SQLite database, ready to feed the Viraasat Griha city pages.

    python pipeline.py add "<reel link>" [--no-video]
    python pipeline.py add "<YouTube link>" [--from 2:15 --to 3:40]
    python pipeline.py list [--status needs_review]
    python pipeline.py show <id>
    python pipeline.py fix <id> --city Bidar --craft Bidriware --notes "..."
    python pipeline.py lookup "Raghurajpur" [--anywhere]
    python pipeline.py doctor          # check yt-dlp, JavaScript runtime, ffmpeg
    python pipeline.py export [--out reels.json]

Environment:
    VG_DB                 database path            (default: viraasat.db)
    VG_MEDIA_DIR          where videos are saved   (default: media/)
    VG_COOKIES_BROWSER    browser to read Instagram cookies from, e.g. chrome
    ANTHROPIC_API_KEY     optional; enables the Claude summary step
    VG_CLAUDE_MODEL       model for the summary    (default: claude-haiku-5-5)

Place lookups use OpenStreetMap Nominatim (free, no key; max 1 request a second,
results cached in the database). Data (c) OpenStreetMap contributors, ODbL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Trust the operating system's certificates (macOS keychain / Windows store), like a
# browser does. Needed when antivirus, a VPN or an office network inspects HTTPS.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env (API keys) without overriding the shell."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


_load_dotenv(HERE / ".env")

DB_PATH = Path(os.environ.get("VG_DB", HERE / "viraasat.db"))
MEDIA_DIR = Path(os.environ.get("VG_MEDIA_DIR", HERE / "media"))
CITIES_PATH = HERE / "cities.json"

LINK_RE = re.compile(
    r"^https?://(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode       TEXT UNIQUE NOT NULL,
    url             TEXT NOT NULL,
    uploader        TEXT,
    caption         TEXT,
    hashtags        TEXT,            -- JSON list
    posted_at       TEXT,            -- ISO date
    duration_s      REAL,
    video_path      TEXT,
    location_raw    TEXT,
    city            TEXT,
    state           TEXT,
    district        TEXT,
    country         TEXT,
    postcode        TEXT,
    lat             REAL,
    lon             REAL,
    location_source TEXT,            -- instagram_tag | caption_match | lookup | manual | none
    craft           TEXT,
    summary         TEXT,
    extracted_by    TEXT,            -- rules | claude
    status          TEXT NOT NULL DEFAULT 'needs_review',
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    query      TEXT PRIMARY KEY,
    result     TEXT NOT NULL,        -- JSON list of places
    fetched_at TEXT NOT NULL
);
"""
# Columns added after the first version; added to older databases on connect.
NEW_COLUMNS = {"district": "TEXT", "country": "TEXT", "postcode": "TEXT",
               "city_id": "INTEGER", "craft_id": "INTEGER"}

NOMINATIM_URL = os.environ.get("VG_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
GEOCODER_UA = "ViraasatReelPipeline/1.0 (local review tool)"


# ---------------------------------------------------------------- 1. validate
def parse_link(url: str) -> tuple[str, str]:
    """Return (clean_url, shortcode) or raise ValueError."""
    url = url.strip()
    m = LINK_RE.match(url)
    if not m:
        raise ValueError(f"Not an Instagram reel/post/tv link: {url}")
    shortcode = m.group(1)
    return f"https://www.instagram.com/reel/{shortcode}/", shortcode


YT_RE = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:shorts/|live/|embed/|watch\?(?:[^#]*&)?v=)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
YT_MAX_WHOLE_S = 15 * 60     # longer videos need a From/To section
YT_MAX_SECTION_S = 10 * 60


def parse_time(v) -> float | None:
    """'83', '83s', '1:23', '1:02:03', '1m23s', '1h2m3s' -> seconds. Empty -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        t = float(v)
    else:
        s = str(v).strip().lower()
        if not s:
            return None
        if re.fullmatch(r"\d+(\.\d+)?s?", s):
            t = float(s.rstrip("s"))
        elif re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", s):
            t = 0.0
            for part in s.split(":"):
                t = t * 60 + float(part)
            if any(float(x) >= 60 for x in s.split(":")[1:]):
                raise ValueError(f"Not a valid time: {v!r} (minutes and seconds go up to 59)")
        elif (m := re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)) and any(m.groups()):
            h, mi, se = (int(x or 0) for x in m.groups())
            t = h * 3600 + mi * 60 + se
        else:
            raise ValueError(f"Not a valid time: {v!r}. Use minutes:seconds, like 2:15.")
    if t < 0:
        raise ValueError("Times can't be negative.")
    return t


def fmt_time(t: float) -> str:
    t = int(round(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_media_link(url: str, start=None, end=None) -> dict:
    """An Instagram or YouTube link -> {platform, id, key, url, file_stem, start, end}.
    `key` is unique per item (a YouTube section gets its own key), `url` is the
    canonical link stored as the source."""
    url = (url or "").strip()
    if LINK_RE.match(url):
        clean, sc = parse_link(url)
        return {"platform": "instagram", "id": sc, "key": sc, "url": clean, "file_stem": sc,
                "start": None, "end": None}
    m = YT_RE.match(url)
    if not m:
        raise ValueError("Paste an Instagram reel or post link, or a YouTube video or Short link.")
    vid = m.group(1)
    s, e = parse_time(start), parse_time(end)
    if s is not None or e is not None:
        if e is None:
            raise ValueError("Give a To time as well as a From time.")
        s = s or 0.0
        if e <= s:
            raise ValueError("The To time must be after the From time.")
        if e - s > YT_MAX_SECTION_S:
            raise ValueError(f"A section can be at most {YT_MAX_SECTION_S // 60} minutes long.")
    is_short = "/shorts/" in url.lower()
    if s is not None:
        a, b = int(s), int(round(e))
        return {"platform": "youtube", "id": vid, "key": f"yt:{vid}@{a}-{b}",
                "url": f"https://www.youtube.com/watch?v={vid}&t={a}s",
                "file_stem": f"yt_{vid}_{a}-{b}", "start": s, "end": e, "is_short": is_short}
    return {"platform": "youtube", "id": vid, "key": f"yt:{vid}",
            "url": f"https://www.youtube.com/shorts/{vid}" if is_short else f"https://www.youtube.com/watch?v={vid}",
            "file_stem": f"yt_{vid}", "start": None, "end": None, "is_short": is_short}


# ---------------------------------------------------------------- 2. download
class FetchError(Exception):
    """A reel could not be fetched; the message is safe to show the user."""


def fetch_info(url: str, shortcode: str, with_video: bool = True, downloader=None,
               platform: str = "instagram") -> dict:
    """Run the downloader and turn any failure into a short FetchError with a hint."""
    downloader = downloader or download
    try:
        return downloader(url, shortcode, with_video)
    except FetchError:
        raise
    except Exception as e:  # yt-dlp DownloadError and network errors
        msg = str(e).replace("ERROR: ", "").split(" (caused by")[0].split("; please report")[0]
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            hint = (f"\nFix: {sys.executable} -m pip install truststore   (then try again)\n"
                    "Still failing? Antivirus web protection, a VPN or an office network is "
                    "intercepting HTTPS; try another network or pause that protection.")
        elif platform == "youtube":
            if re.search(r"sign in to confirm|not a bot|age", msg, re.I):
                hint = ("\nFix: sign in to YouTube in Chrome, then set VG_COOKIES_BROWSER=chrome "
                        "(in .env, or export it in Terminal) and try again.")
            elif re.search(r"javascript|js runtime|deno|n challenge|nsig|signature", msg, re.I):
                hint = "\n" + YT_FIX.format(py=sys.executable)
            elif re.search(r"ffmpeg", msg, re.I):
                hint = "\nFix: brew install ffmpeg"
            elif re.search(r"requested format", msg, re.I):
                hint = "\n" + YT_FIX.format(py=sys.executable)
        elif re.search(r"no video|no formats|video formats found", msg, re.I):
            hint = ("\nThis post seems to have photos only, which the downloader can't fetch. "
                    "Save the photo from Instagram and use 'Upload photo' instead.")
        elif re.search(r"login|rate.?limit|not available|401|403", msg, re.I):
            hint = "\nFix: sign in to Instagram in Chrome, then set VG_COOKIES_BROWSER=chrome"
        what = "video" if platform == "youtube" else "reel"
        raise FetchError(f"Could not fetch the {what}: {msg}{hint}") from e


def download(url: str, shortcode: str, with_video: bool = True) -> dict:
    """Fetch metadata (and the video) with yt-dlp. Returns the yt-dlp info dict."""
    try:
        import yt_dlp  # imported here so the rest of the tool works without it
    except ImportError as e:
        raise FetchError(f"Could not load yt-dlp ({e}).\n"
                         f"Python in use: {sys.executable}\n"
                         f"Fix: {sys.executable} -m pip install yt-dlp")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(MEDIA_DIR / f"{shortcode}.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": not with_video,
    }
    browser = os.environ.get("VG_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=with_video)
        if with_video:
            info["_video_path"] = ydl.prepare_filename(info)
    return info


VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def download_post(url: str, shortcode: str) -> dict:
    """Download every video in a reel or post (carousels too), each with its
    poster image. Returns {"info": metadata, "items": [...]} with paths relative
    to MEDIA_DIR."""
    try:
        import yt_dlp
    except ImportError as e:
        raise FetchError(f"Could not load yt-dlp ({e}).\n"
                         f"Fix: {sys.executable} -m pip install yt-dlp")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(MEDIA_DIR / f"{shortcode}_%(playlist_index|1)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
    }
    browser = os.environ.get("VG_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        entries = [e for e in (info.get("entries") or [info]) if e]
        items = []
        for e in entries:
            video = Path(ydl.prepare_filename(e))
            if not video.is_file():
                continue
            poster = next((f for f in sorted(video.parent.glob(video.stem + ".*"))
                           if f.suffix.lower() in IMAGE_EXT), None)
            items.append({
                "kind": "video" if video.suffix.lower() in VIDEO_EXT else "photo",
                "path": video.name,
                "poster": poster.name if poster else None,
                "duration_s": e.get("duration"),
                "width": e.get("width"),
                "height": e.get("height"),
            })
    meta = info if info.get("description") else {**entries[0], **{k: v for k, v in info.items() if v}} if entries else info
    if not items:
        raise FetchError("Could not fetch the post: no video was downloaded. If it has photos only, "
                         "save the photo from Instagram and use 'Upload photo' instead.")
    return {"info": meta, "items": items}


def check_youtube_info(info: dict, link: dict) -> None:
    """Refuse playlists, live streams and long videos without a section."""
    if info.get("_type") in ("playlist", "multi_video"):
        raise FetchError("That link is a playlist. Paste the link of one video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise FetchError("Live streams can't be downloaded. Wait for the recording, then try again.")
    dur = info.get("duration") or 0
    if link.get("start") is None and dur > YT_MAX_WHOLE_S:
        raise FetchError(f"This video is {fmt_time(dur)} long. Give a From and To time for the part you "
                         f"need (up to {YT_MAX_SECTION_S // 60} minutes), e.g. the shots of one artisan.")
    if link.get("start") is not None and dur and link["start"] >= dur:
        raise FetchError(f"The From time is after the end of the video ({fmt_time(dur)}).")


JS_RUNTIMES = ("deno", "node", "bun")
YT_FIX = ("Fix, in Terminal:\n"
          "  {py} -m pip install -U \"yt-dlp[default]\"\n"
          "  brew install deno        (YouTube now needs a JavaScript runtime; Node.js also works)\n"
          "Then restart ui.py and try again. To see what's missing:  python3 pipeline.py doctor")


def js_runtimes() -> dict:
    """JavaScript runtimes yt-dlp may use for YouTube's checks, as its js_runtimes option."""
    import shutil
    return {rt: {} for rt in JS_RUNTIMES if shutil.which(rt)}


def usable_formats(formats: list | None) -> list:
    """Formats that actually contain video (not storyboard images)."""
    return [f for f in formats or []
            if f.get("vcodec") not in (None, "none") and f.get("ext") != "mhtml"
            and f.get("protocol") != "mhtml" and not str(f.get("format_id", "")).startswith("sb")]


def youtube_format(has_ffmpeg: bool) -> str:
    """Best up to 1080p. Falls back to whatever video exists, so a download never fails
    just because the ideal format is missing (music is replaced in the edit anyway)."""
    if has_ffmpeg:
        return ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                "b[height<=1080][ext=mp4]/b[height<=1080]/bv*+ba/b/bv*")
    return "b[ext=mp4]/b/bv*[ext=mp4]/bv*"


class _Collect:
    """yt-dlp logger that keeps warnings (they explain most YouTube failures)."""
    def __init__(self):
        self.warnings = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.warnings.append(str(msg))


def _yt_error(msg: str, log: "_Collect") -> "FetchError":
    relevant = [w for w in log.warnings
                if re.search(r"javascript|js runtime|ejs|challenge|n challenge|signature|deno|formats may be missing|po token|sign in", w, re.I)]
    detail = ("\nyt-dlp said: " + " | ".join(dict.fromkeys(w.replace("WARNING: ", "")[:220] for w in relevant[:3]))) if relevant else ""
    return FetchError(f"Could not fetch the video: {msg}{detail}\n" + YT_FIX.format(py=sys.executable))


def download_youtube(link: dict) -> dict:
    """Download a YouTube video or Short, or one section of it, with its thumbnail.
    Returns {"info", "items", "note"} like download_post."""
    import shutil
    try:
        import yt_dlp
    except ImportError as e:
        raise FetchError(f"Could not load yt-dlp ({e}).\nFix: {sys.executable} -m pip install -U \"yt-dlp[default]\"")
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    section = link.get("start") is not None
    if section and not has_ffmpeg:
        raise FetchError("Downloading part of a video needs ffmpeg. Install it with:  brew install ffmpeg\n"
                         "(or leave From and To empty to download the whole video, if it's short).")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    log = _Collect()
    opts = {
        "outtmpl": str(MEDIA_DIR / f"{link['file_stem']}.%(ext)s"),
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True,
        "logger": log,
        "format": youtube_format(has_ffmpeg),
    }
    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"
    runtimes = js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    if section:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [(link["start"], link["end"])])
        opts["force_keyframes_at_cuts"] = True
    browser = os.environ.get("VG_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = ydl.extract_info(link["url"], download=False, process=False)
            check_youtube_info(raw, link)
            if raw.get("formats") is not None and not usable_formats(raw["formats"]):
                raise _yt_error("YouTube only offered preview images, no video formats.", log)
            info = ydl.process_ie_result(raw, download=True) or raw
            done = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
    except FetchError:
        raise
    except Exception as e:  # yt-dlp DownloadError
        msg = str(e).replace("ERROR: ", "").split(" (caused by")[0].split("; please report")[0]
        if re.search(r"requested format|no video formats|only images", msg, re.I):
            raise _yt_error(msg, log) from e
        raise
    video = next((Path(f) for f in done if Path(f).suffix.lower() in VIDEO_EXT and Path(f).is_file()), None)
    if not video:
        video = next((f for f in sorted(MEDIA_DIR.glob(link["file_stem"] + ".*"))
                      if f.suffix.lower() in VIDEO_EXT), None)
    if not video:
        raise FetchError("The download finished but no video file was found in the media folder.")
    poster = next((f for f in sorted(MEDIA_DIR.glob(link["file_stem"] + ".*"))
                   if f.suffix.lower() in IMAGE_EXT), None)
    notes = []
    if not has_ffmpeg:
        notes.append(f"Saved at {info.get('height') or 'low'}p because ffmpeg isn't installed. For HD, run "
                     "brew install ffmpeg, then remove this video and add it again.")
    if info.get("acodec") in (None, "none") and not info.get("requested_formats"):
        notes.append("This copy has no sound (only a silent version was available); fine if the music is replaced.")
    info.setdefault("description", info.get("title"))
    return {
        "info": info,
        "items": [{"kind": "video", "path": video.name, "poster": poster.name if poster else None,
                   "duration_s": (link["end"] - link["start"]) if section else info.get("duration"),
                   "width": info.get("width"), "height": info.get("height")}],
        "note": " ".join(notes),
    }


def doctor() -> None:
    """Print what the downloads need and whether it's there."""
    import importlib.metadata as md
    import shutil
    def ver(pkg):
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return None
    rows = [
        ("Python", sys.executable, True),
        ("yt-dlp", ver("yt-dlp") or "missing  → python3 -m pip install -U \"yt-dlp[default]\"", bool(ver("yt-dlp"))),
        ("yt-dlp-ejs (YouTube scripts)", ver("yt-dlp-ejs") or "missing  → python3 -m pip install -U \"yt-dlp[default]\"",
         bool(ver("yt-dlp-ejs"))),
        ("JavaScript runtime", ", ".join(js_runtimes()) or "missing  → brew install deno", bool(js_runtimes())),
        ("ffmpeg", shutil.which("ffmpeg") or "missing  → brew install ffmpeg (HD and From/To)", bool(shutil.which("ffmpeg"))),
        ("truststore", ver("truststore") or "missing  → python3 -m pip install truststore", bool(ver("truststore"))),
        ("Cookies browser", os.environ.get("VG_COOKIES_BROWSER") or "not set (only needed if a site asks you to sign in)", True),
    ]
    for name, value, ok in rows:
        print(f"{'✓' if ok else '✗'} {name:<30} {value}")


def info_to_fields(info: dict) -> dict:
    """Pull the fields we keep out of a yt-dlp info dict."""
    caption = info.get("description") or info.get("title") or ""
    posted = None
    if info.get("timestamp"):
        posted = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).date().isoformat()
    elif info.get("upload_date"):
        d = info["upload_date"]
        posted = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    video = info.get("_video_path")
    return {
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "caption": caption,
        "hashtags": extract_hashtags(caption),
        "posted_at": posted,
        "duration_s": info.get("duration"),
        "video_path": video if video and Path(video).exists() else None,
        # Instagram's location tag, when yt-dlp passes it through
        "location_raw": _location_tag(info),
    }


def _location_tag(info: dict) -> str | None:
    loc = info.get("location")
    if isinstance(loc, dict):
        loc = loc.get("name")
    return loc or info.get("location_name") or None


def extract_hashtags(text: str) -> list[str]:
    return sorted({h.lower() for h in re.findall(r"#(\w+)", text or "", re.UNICODE)})


# ------------------------------------------------------- 3 & 4. place + craft
def load_cities(path: Path = CITIES_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _mentions(term: str, text: str, tags: list[str]) -> bool:
    """Whole-word match in the caption, or substring match inside a hashtag."""
    t = _norm(term)
    if not t:
        return False
    if re.search(rf"\b{re.escape(t)}\b", text):
        return True
    squashed = t.replace(" ", "")
    return any(squashed in tag for tag in tags)


def find_city(location_raw: str | None, caption: str, hashtags: list[str], cities: list[dict]):
    """Return (city_dict or None, source)."""
    def match(text: str, tags: list[str]):
        for c in cities:
            for name in [c["city"], *c.get("aliases", [])]:
                if _mentions(name, text, tags):
                    return c
        return None

    if location_raw:
        c = match(_norm(location_raw), [])
        if c:
            return c, "instagram_tag"
    c = match(_norm(caption), hashtags)
    if c:
        return c, "caption_match"
    return None, "none"


def find_craft(caption: str, hashtags: list[str], cities: list[dict], city: dict | None):
    """Check the found city's crafts first, then every craft. Returns (craft, city_hint)."""
    text = _norm(caption)
    ordered = ([city] if city else []) + [c for c in cities if c is not city]
    for c in ordered:
        for craft in c.get("crafts", []):
            for kw in [craft["name"], *craft["keywords"]]:
                if _mentions(kw, text, hashtags):
                    return craft["name"], c
    return None, None


# ----------------------------------------------------- 4b. place lookup (OSM)
class GeocodeError(Exception):
    """A place lookup failed; the message is safe to show the user."""


_geo_lock = threading.Lock()
_geo_last = [0.0]


def _nominatim_get(params: dict) -> list:
    """One request to Nominatim, at most one a second (their usage policy)."""
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": GEOCODER_UA,
                                               "Accept-Language": "en"})
    with _geo_lock:
        wait = 1.1 - (time.monotonic() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise GeocodeError(f"Place lookup refused by OpenStreetMap (HTTP {e.code}). "
                               "Wait a minute and try again.") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GeocodeError(f"Place lookup failed: could not reach OpenStreetMap ({e}).") from e
        finally:
            _geo_last[0] = time.monotonic()


def _place_from_osm(r: dict) -> dict:
    a = r.get("address") or {}
    name = (a.get("city") or a.get("town") or a.get("village") or a.get("hamlet")
            or a.get("suburb") or a.get("municipality") or r.get("name")
            or (r.get("display_name") or "").split(",")[0])
    district = a.get("state_district") or a.get("county") or a.get("district")
    osm = f"https://www.openstreetmap.org/{r['osm_type']}/{r['osm_id']}" if r.get("osm_id") else None
    return {
        "city": name,
        "district": (district or "").replace(" District", "").strip() or None,
        "state": a.get("state") or a.get("territory"),
        "country": a.get("country"),
        "postcode": a.get("postcode"),
        "lat": round(float(r["lat"]), 6),
        "lon": round(float(r["lon"]), 6),
        "kind": r.get("addresstype") or r.get("type"),
        "display_name": r.get("display_name"),
        "osm_url": osm,
    }


def geocode(query: str, con: sqlite3.Connection | None = None, limit: int = 5,
            country: str | None = "in", fetcher=None) -> list[dict]:
    """Look a place name up on OpenStreetMap. Returns up to `limit` candidates,
    best first. Results are cached in the database so each name is fetched once."""
    q = " ".join((query or "").split())
    if not q:
        return []
    key = f"{country or '*'}|{limit}|{q.lower()}"
    own = con is None
    con = con or connect()
    try:
        hit = con.execute("SELECT result FROM geocode_cache WHERE query = ?", (key,)).fetchone()
        if hit:
            return json.loads(hit["result"])
        params = {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": limit}
        if country:
            params["countrycodes"] = country
        raw = (fetcher or _nominatim_get)(params)
        places = [_place_from_osm(r) for r in raw if r.get("lat") and r.get("lon")]
        con.execute("INSERT OR REPLACE INTO geocode_cache (query, result, fetched_at) VALUES (?, ?, ?)",
                    (key, json.dumps(places, ensure_ascii=False), now()))
        con.commit()
        return places
    finally:
        if own:
            con.close()


def lookup_quietly(query: str) -> dict | None:
    """Best match for a place, or None if offline or nothing found (used during add)."""
    try:
        found = geocode(query, limit=1)
    except GeocodeError as e:
        print(f"  (place lookup skipped: {e})", file=sys.stderr)
        return None
    return found[0] if found else None


# --------------------------------------------------------------- 5. summarise
def summarise_with_claude(caption: str, uploader: str | None) -> dict | None:
    """Ask Claude for a short summary from the caption only. None if not configured."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not caption.strip():
        return None
    prompt = (
        "Below is the caption of an Instagram reel about Indian crafts. Using ONLY what the "
        "caption states, reply with JSON and nothing else:\n"
        '{"summary": <at most 40 words>, "city": <city or null>, "craft": <craft name or null>, '
        '"artisan": <artisan name or null>}\n'
        "Use null for anything the caption does not state. Do not guess.\n\n"
        f"Uploader: {uploader or 'unknown'}\nCaption:\n{caption[:4000]}"
    )
    body = json.dumps({
        "model": os.environ.get("VG_CLAUDE_MODEL", "claude-haiku-5-5"),
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        m = re.search(r"\{.*\}", text, re.DOTALL)
        out = json.loads(m.group(0)) if m else {}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  (Claude summary skipped: {e})", file=sys.stderr)
        return None
    if out.get("summary"):
        out["summary"] = " ".join(str(out["summary"]).split()[:40])
    return out


# -------------------------------------------------------------------- 6. save
class _Row(dict):
    """A plain dict standing in for sqlite3.Row: supports both row["col"] and
    dict(row), which is every access pattern this codebase uses. Needed because
    libsql's Connection/Cursor are Rust-backed builtins with no row_factory
    support at all, and return bare tuples."""


class _TursoCursor:
    def __init__(self, cur):
        self._cur = cur

    def _wrap(self, raw):
        if raw is None:
            return None
        cols = [c[0] for c in self._cur.description]
        return _Row(zip(cols, raw))

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _TursoConnection:
    """Wraps a libsql connection so it's close enough to sqlite3.Connection
    for sitedb.py/ui.py to use unchanged: wrapped cursors, dict-like rows.

    Also reconnects once on "stream not found": Turso's Hrana stream for a
    connection expires if it sits idle for a while (e.g. a slow yt-dlp
    download running between two queries on the same request), and the next
    query on it then fails with a 404 instead of being retried server-side."""

    def __init__(self, con, database: str, auth_token: str):
        self._con = con
        self._database = database
        self._auth_token = auth_token

    def execute(self, sql, params=()):
        try:
            return _TursoCursor(self._con.execute(sql, params))
        except Exception as e:
            if "stream not found" not in str(e):
                raise
            import libsql
            self._con = libsql.connect(database=self._database, auth_token=self._auth_token)
            return _TursoCursor(self._con.execute(sql, params))

    def __getattr__(self, name):
        return getattr(self._con, name)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    if path is None and turso_url:
        auth_token = os.environ.get("TURSO_AUTH_TOKEN", "")
        import libsql
        con = _TursoConnection(libsql.connect(database=turso_url, auth_token=auth_token), turso_url, auth_token)
    else:
        con = sqlite3.connect(path or DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(reels)")}
    for col, typ in NEW_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE reels ADD COLUMN {col} {typ}")
    con.commit()
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_row(url: str, shortcode: str, info: dict, cities: list[dict], use_claude: bool = True,
              lookup=None) -> dict:
    """lookup(name) -> place dict or None; used for places not in cities.json.
    Pass lookup=False to stay offline."""
    if lookup is None:
        lookup = lookup_quietly
    f = info_to_fields(info)
    city, source = find_city(f["location_raw"], f["caption"], f["hashtags"], cities)
    if not city and f["location_raw"] and lookup:
        place = lookup(f["location_raw"])  # Instagram tag for a place we don't list yet
        if place:
            city, source = place, "instagram_tag"
    craft, craft_city = find_craft(f["caption"], f["hashtags"], cities, city)
    extracted_by = "rules"
    summary = None

    if use_claude:
        ai = summarise_with_claude(f["caption"], f["uploader"])
        if ai:
            extracted_by = "claude"
            summary = ai.get("summary")
            if not city and ai.get("city"):
                city, source = find_city(None, ai["city"], [], cities)
                if not city:  # a city we don't list yet: look it up, else keep the name
                    place = lookup(ai["city"]) if lookup else None
                    city = place or {"city": ai["city"]}
                    source = "lookup" if place else "caption_match"
            craft = craft or ai.get("craft")

    return {
        "shortcode": shortcode, "url": url, **f,
        "hashtags": json.dumps(f["hashtags"]),
        "city": city["city"] if city else None,
        "state": city.get("state") if city else None,
        "district": city.get("district") if city else None,
        "country": (city.get("country") or ("India" if city.get("state") else None)) if city else None,
        "postcode": city.get("postcode") if city else None,
        "lat": city.get("lat") if city else None,
        "lon": city.get("lon") if city else None,
        "location_source": source,
        "craft": craft,
        "summary": summary,
        "extracted_by": extracted_by,
        "status": "needs_review",
        "notes": (f"craft usually from {craft_city['city']}" if craft_city and not city else None),
        "created_at": now(), "updated_at": now(),
    }


def save(con: sqlite3.Connection, row: dict) -> int:
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    cur = con.execute(f"INSERT INTO reels ({cols}) VALUES ({qs})", list(row.values()))
    con.commit()
    return cur.lastrowid


def add(link: str, with_video: bool = True, con=None, downloader=download, use_claude=True,
        start=None, end=None, yt_downloader=None) -> int | None:
    """Save a reel (Instagram) or video (YouTube, optionally a From/To section)."""
    con = con or connect()
    lk = parse_media_link(link, start, end)
    url, shortcode = lk["url"], lk["key"]
    existing = con.execute("SELECT id FROM reels WHERE shortcode = ?", (shortcode,)).fetchone()
    if existing:
        print(f"Already saved as #{existing['id']} ({shortcode}) - skipped.")
        return None
    print(f"Fetching {shortcode} ...")
    if lk["platform"] == "youtube":
        def downloader(_u, _k, _v):
            res = (yt_downloader or download_youtube)(lk)
            if res.get("note"):
                print("  " + res["note"])
            return {**res["info"], "_video_path": str(MEDIA_DIR / res["items"][0]["path"])}
    try:
        info = fetch_info(url, shortcode, with_video, downloader, platform=lk["platform"])
    except FetchError as e:
        sys.exit(str(e))
    row = build_row(url, shortcode, info, load_cities(), use_claude=use_claude)
    rid = save(con, row)
    print(f"Saved #{rid}: city={row['city'] or '?'} ({row['location_source']}), "
          f"craft={row['craft'] or '?'}, video={'yes' if row['video_path'] else 'no'}")
    if not row["city"] or not row["craft"]:
        print(f"  Needs a manual fix:  python3 pipeline.py fix {rid} --city ... --craft ...")
    return rid


# ------------------------------------------------------------------ 7. review
def fix(con, rid: int, city: str | None = None, craft: str | None = None, notes: str | None = None,
        lookup=None) -> None:
    """lookup(name) -> place or None, for cities not in cities.json (default: OpenStreetMap).
    Pass lookup=False to keep an unknown city as typed, without details."""
    if lookup is None:
        lookup = lookup_quietly
    row = con.execute("SELECT * FROM reels WHERE id = ?", (rid,)).fetchone()
    if not row:
        sys.exit(f"No reel #{rid}")
    updates = {"status": "reviewed", "updated_at": now()}
    if city:
        match = next((c for c in load_cities() if _norm(c["city"]) == _norm(city)
                      or _norm(city) in map(_norm, c.get("aliases", []))), None)
        source = "manual"
        if not match and lookup:
            match = lookup(city)
            if match:
                source = "lookup"
                print(f"  Found on OpenStreetMap: {match['display_name']}")
        m = match or {}
        updates.update(city=m.get("city") or city, state=m.get("state"),
                       district=m.get("district"), postcode=m.get("postcode"),
                       country=m.get("country") or ("India" if m.get("state") else None),
                       lat=m.get("lat"), lon=m.get("lon"), location_source=source)
    if craft:
        updates["craft"] = craft
    if notes is not None:
        updates["notes"] = notes
    sets = ", ".join(f"{k} = ?" for k in updates)
    con.execute(f"UPDATE reels SET {sets} WHERE id = ?", [*updates.values(), rid])
    con.commit()
    print(f"#{rid} updated and marked reviewed.")


# --------------------------------------------------------------------- CLI
def cmd_list(con, status=None):
    q = "SELECT id, shortcode, uploader, city, craft, status FROM reels"
    rows = con.execute(q + (" WHERE status = ?" if status else "") + " ORDER BY id",
                       (status,) if status else ()).fetchall()
    if not rows:
        print("No reels saved yet.")
        return
    print(f"{'id':>3}  {'shortcode':<13} {'uploader':<20} {'city':<14} {'craft':<20} status")
    for r in rows:
        print(f"{r['id']:>3}  {r['shortcode']:<13} {(r['uploader'] or '-')[:20]:<20} "
              f"{(r['city'] or '?')[:14]:<14} {(r['craft'] or '?')[:20]:<20} {r['status']}")


def cmd_show(con, rid):
    r = con.execute("SELECT * FROM reels WHERE id = ?", (rid,)).fetchone()
    if not r:
        sys.exit(f"No reel #{rid}")
    for k in r.keys():
        v = r[k]
        if k == "caption" and v and len(v) > 300:
            v = v[:300] + " ..."
        print(f"{k:>16}: {v}")


def cmd_lookup(con, query, country):
    places = geocode(query, con=con, country=country)
    if not places:
        print(f"Nothing found for {query!r}." + (" Try --anywhere to search outside India."
                                                 if country else ""))
        return
    for i, pl in enumerate(places, 1):
        print(f"{i}. {pl['display_name']}")
        print(f"   city={pl['city']}  district={pl['district'] or '-'}  state={pl['state'] or '-'}  "
              f"PIN={pl['postcode'] or '-'}  lat={pl['lat']}  lon={pl['lon']}")
    print("Data (c) OpenStreetMap contributors")


def cmd_export(con, out):
    rows = [dict(r) for r in con.execute("SELECT * FROM reels ORDER BY id")]
    for r in rows:
        r["hashtags"] = json.loads(r["hashtags"] or "[]")
    Path(out).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(rows)} reels to {out}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Instagram reel -> local craft database")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add an Instagram reel or a YouTube video by link")
    a.add_argument("link")
    a.add_argument("--from", dest="start", help="YouTube only: start of the part to keep, e.g. 2:15")
    a.add_argument("--to", dest="end", help="YouTube only: end of the part to keep, e.g. 3:40")
    a.add_argument("--no-video", action="store_true", help="save details only, skip the video file")
    a.add_argument("--no-claude", action="store_true", help="skip the Claude summary even if a key is set")
    l = sub.add_parser("list", help="list saved reels")
    l.add_argument("--status", choices=["needs_review", "reviewed"])
    s = sub.add_parser("show", help="show one reel")
    s.add_argument("id", type=int)
    f = sub.add_parser("fix", help="correct a reel and mark it reviewed")
    f.add_argument("id", type=int)
    f.add_argument("--city")
    f.add_argument("--craft")
    f.add_argument("--notes")
    sub.add_parser("doctor", help="check what downloads need (yt-dlp, JavaScript runtime, ffmpeg)")
    g = sub.add_parser("lookup", help="look a place up on OpenStreetMap")
    g.add_argument("place")
    g.add_argument("--anywhere", action="store_true", help="search outside India too")
    e = sub.add_parser("export", help="dump all rows to JSON")
    e.add_argument("--out", default="reels.json")
    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return doctor()

    con = connect()
    try:
        if args.cmd == "add":
            add(args.link, with_video=not args.no_video, con=con, use_claude=not args.no_claude,
                start=args.start, end=args.end)
        elif args.cmd == "list":
            cmd_list(con, args.status)
        elif args.cmd == "show":
            cmd_show(con, args.id)
        elif args.cmd == "fix":
            fix(con, args.id, args.city, args.craft, args.notes)
        elif args.cmd == "lookup":
            cmd_lookup(con, args.place, None if args.anywhere else "in")
        elif args.cmd == "export":
            cmd_export(con, args.out)
    except (ValueError, GeocodeError) as err:
        sys.exit(str(err))
    finally:
        con.close()


if __name__ == "__main__":
    main()
