#!/usr/bin/env python3
"""Viraasat admin: a local web page for building the city pages.

    python3 ui.py            # opens http://127.0.0.1:8765 in your browser

Cities page -> pick a city -> add Instagram reels or posts (photos and videos),
shops from Google Maps links, and the story of its craft.

Runs only on your computer (127.0.0.1). Stop it with Ctrl+C.
Optional keys go in a .env file next to this script:
    GOOGLE_MAPS_API_KEY=...   shop details from Google Places
    ANTHROPIC_API_KEY=...     short reel summaries
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pipeline as p
import places
import sitedb

HOST = "127.0.0.1"
PORT = int(os.environ.get("VG_PORT", 8765))
PAGE = p.HERE / "ui.html"

# Tests replace these; None means the real service.
POST_DOWNLOADER = None   # Instagram (yt-dlp)
YT_DOWNLOADER = None     # YouTube (yt-dlp)
GEOCODER = None          # OpenStreetMap Nominatim
PLACES_CALL = None       # Google Places API
PLACES_FOLLOW = None     # following maps.app.goo.gl short links
PLACES_KEY = None


def db():
    return sitedb.connect()


def with_db(fn):
    def run(*args):
        con = db()
        try:
            return fn(con, *args)
        finally:
            con.close()
    return run


# --------------------------------------------------------------------- API
def api_config() -> dict:
    import shutil
    return {"google_key": bool(PLACES_KEY or places.api_key()),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "claude_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "media_dir": str(p.MEDIA_DIR), "db": str(p.DB_PATH)}


def api_cities(con) -> dict:
    return {"cities": sitedb.list_cities(con), "unassigned": sitedb.unassigned_reels(con),
            "crafts": [dict(r) for r in con.execute("SELECT id, name FROM crafts ORDER BY name")],
            "categories": [dict(r) for r in con.execute("SELECT id, name FROM categories ORDER BY sort_order")],
            "videos": sitedb.list_videos(con), "homepage_video": sitedb.homepage_video_info(con)}


def api_geocode(con, query: str, anywhere: bool = False) -> list:
    if len((query or "").strip()) < 2:
        raise ValueError("Type at least two letters of the place name.")
    return p.geocode(query, con=con, country=None if anywhere else "in", fetcher=GEOCODER)


def api_add_post(con, slug, body):
    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return sitedb.add_post(con, slug, body.get("url", ""), body.get("craft_id"),
                           downloader=POST_DOWNLOADER, use_claude=use_claude,
                           start=body.get("start"), end=body.get("end"), yt_downloader=YT_DOWNLOADER)


def api_resolve_shop(_con, body):
    return places.shop_from_link(body.get("url", ""), follow=PLACES_FOLLOW, call=PLACES_CALL, key=PLACES_KEY)


ROUTES_POST = [
    (r"/api/cities", lambda con, b: sitedb.create_city(con, b)),
    (r"/api/cities/([a-z0-9-]+)", lambda con, s, b: sitedb.update_city(con, s, b)),
    (r"/api/cities/([a-z0-9-]+)/delete", lambda con, s, b: sitedb.delete_city(con, s, b.get("confirm_name", ""))),
    (r"/api/cities/([a-z0-9-]+)/image", lambda con, s, b: sitedb.set_city_image(con, s, b.get("media_id"), b.get("which", ""))),
    (r"/api/cities/([a-z0-9-]+)/crafts/(\d+)/video", lambda con, s, cid, b: sitedb.set_craft_media(con, s, cid, b.get("media_id"))),
    (r"/api/settings/homepage-video", lambda con, b: sitedb.set_homepage_video(con, b.get("media_id"))),
    (r"/api/cities/([a-z0-9-]+)/crafts", lambda con, s, b: sitedb.save_craft(con, s, b)),
    (r"/api/cities/([a-z0-9-]+)/crafts/(\d+)/unlink", lambda con, s, cid, b: sitedb.unlink_craft(con, s, cid)),
    (r"/api/cities/([a-z0-9-]+)/story", lambda con, s, b: sitedb.save_story(con, s, b.get("craft_id"), b.get("paragraphs"))),
    (r"/api/cities/([a-z0-9-]+)/posts", api_add_post),
    (r"/api/cities/([a-z0-9-]+)/stores", lambda con, s, b: sitedb.save_store(con, s, b)),
    (r"/api/reels/(\d+)/assign", lambda con, rid, b: sitedb.assign_reel(con, rid, b.get("city", ""), b.get("craft_id"))),
    (r"/api/reels/(\d+)/delete", lambda con, rid, b: sitedb.delete_reel(con, rid)),
    (r"/api/media/(\d+)", lambda con, mid, b: sitedb.update_media(con, mid, b)),
    (r"/api/media/(\d+)/delete", lambda con, mid, b: sitedb.delete_media(con, mid)),
    (r"/api/stores/(\d+)/delete", lambda con, sid, b: sitedb.delete_store(con, sid)),
    (r"/api/shops/resolve", api_resolve_shop),
]


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "ViraasatAdmin/2"

    def log_message(self, fmt, *args):  # quiet except for errors
        if len(args) > 1 and str(args[1])[:1] in "45":
            sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _run(self, fn, *args):
        try:
            self._json(fn(*args))
        except (p.GeocodeError, places.PlacesError) as e:
            self._json({"error": str(e)}, 502)
        except (ValueError, p.FetchError) as e:
            self._json({"error": str(e)}, 400)
        except LookupError as e:
            self._json({"error": str(e)}, 404)
        except Exception as e:  # unexpected: show it rather than hang the page
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _same_origin(self) -> bool:
        """Only accept changes from this page, not from other websites."""
        origin = self.headers.get("Origin")
        return origin in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/config":
            self._run(api_config)
        elif path == "/api/cities":
            self._run(with_db(api_cities))
        elif m := re.fullmatch(r"/api/cities/([a-z0-9-]+)", path):
            self._run(with_db(sitedb.get_city), m.group(1))
        elif path == "/api/geocode":
            self._run(with_db(api_geocode), qs.get("q", [""])[0], qs.get("anywhere", ["0"])[0] == "1")
        elif path.startswith("/media/"):
            self._media(unquote(path[len("/media/"):]))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "Forbidden"}, 403)
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)

        if m := re.fullmatch(r"/api/cities/([a-z0-9-]+)/upload", path):
            if length > sitedb.MAX_UPLOAD:
                return self._json({"error": "The file is larger than 300 MB."}, 413)
            data = self.rfile.read(length)
            return self._run(with_db(sitedb.add_upload), m.group(1), qs.get("name", [""])[0], data,
                             qs.get("craft_id", [None])[0])

        if re.fullmatch(r"/api/settings/homepage-video/upload", path):
            if length > sitedb.MAX_UPLOAD:
                return self._json({"error": "The file is larger than 300 MB."}, 413)
            data = self.rfile.read(length)
            return self._run(with_db(sitedb.upload_homepage_video), qs.get("name", [""])[0], data)

        if length > 2_000_000:
            return self._json({"error": "Request too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": f"Bad request: {e}"}, 400)
        for pattern, fn in ROUTES_POST:
            if m := re.fullmatch(pattern, path):
                return self._run(with_db(fn), *m.groups(), body)
        self._json({"error": "Not found"}, 404)

    def _media(self, name: str):
        """Serve a file from the media folder, with byte ranges so videos can seek."""
        media = p.MEDIA_DIR.resolve()
        f = (media / name).resolve()
        if media not in f.parents or not f.is_file():
            return self._json({"error": "Not found"}, 404)
        size = f.stat().st_size
        start, end, partial = 0, size - 1, False
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and size:
            a, b = m.groups()
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(size - int(b), 0)
            partial = True
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        ctype = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                 ".gif": "image/gif"}.get(f.suffix.lower(), "application/octet-stream")
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        self.send_header("Cache-Control", "private, max-age=3600")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1 if size else 0
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser stopped reading; normal while seeking


def make_server(port: int = PORT) -> ThreadingHTTPServer:
    db().close()  # create tables and seed the cities if needed
    return ThreadingHTTPServer((HOST, port), Handler)


def main():
    try:
        srv = make_server()
    except OSError as e:
        sys.exit(f"Could not start on port {PORT} ({e}).\n"
                 "An older copy is probably still running. Stop it with:  pkill -f ui.py\n"
                 "then run python3 ui.py again.")
    url = f"http://{HOST}:{PORT}"
    print(f"Viraasat admin running at {url}")
    print(f"Database: {p.DB_PATH}\nMedia:    {p.MEDIA_DIR}")
    print(f"Google Places key: {'found' if places.api_key() else 'not set (shops get name + location from the link only)'}")
    print("Press Ctrl+C to stop.")
    if not os.environ.get("VG_NO_BROWSER"):
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
