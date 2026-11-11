#!/usr/bin/env python3
"""Add a city's chosen Instagram videos and photos to the admin in one go.

    python3 import_picks.py picks/khurja.json

The picks file lists the videos (Instagram reels, or YouTube videos and Shorts,
downloaded with yt-dlp; a YouTube entry may add "from" and "to" times to keep only
that part) and photo posts (one slide each) for one city, with alt text and credit. Safe to run again: anything
already in the city is skipped. Every item starts as "permission: not asked".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import igphotos
import pipeline as p
import sitedb


def canonical(url: str, kind: str, start=None, end=None) -> str:
    if kind == "video":
        return p.parse_media_link(url, start, end)["url"]   # Instagram reel or YouTube (section)
    _, sc = p.parse_link(url)
    return f"https://www.instagram.com/p/{sc}/"


def run(picks_path, downloader=None, get=None, gallery=None, out=print, yt_downloader=None) -> dict:
    picks = json.loads(Path(picks_path).read_text(encoding="utf-8"))
    slug = picks["city"]
    con = sitedb.connect()
    done = {"added": [], "skipped": [], "failed": []}
    try:
        city = sitedb.get_city(con, slug)["city"]
        out(f"Importing into {city['name']}: {len(picks.get('videos', []))} videos, "
            f"{len(picks.get('photos', []))} photos\n")

        for i, v in enumerate(picks.get("videos", []), 1):
            try:
                url = canonical(v["url"], "video", v.get("from"), v.get("to"))
            except ValueError as e:
                out(f"  ✗ Video {i} {v.get('url')}: {e}")
                done["failed"].append((v.get("url"), str(e)))
                continue
            label = f"Video {i} {url}"
            if con.execute("SELECT 1 FROM media WHERE source_url = ? AND city_id = ?", (url, city["id"])).fetchone():
                out(f"  = {label}: already in {city['name']}")
                done["skipped"].append(url)
                continue
            out(f"  … {label}: downloading")
            try:
                res = sitedb.add_post(con, slug, v["url"], v.get("craft_id"), downloader=downloader,
                                      use_claude=False, start=v.get("from"), end=v.get("to"),
                                      yt_downloader=yt_downloader)
                if res.get("note"):
                    out(f"    {res['note']}")
                if v.get("alt"):
                    con.execute("UPDATE media SET alt_text = ? WHERE source_url = ? AND city_id = ?",
                                (v["alt"], url, city["id"]))
                    con.commit()
                n = con.execute("SELECT COUNT(*) FROM media WHERE source_url = ?", (url,)).fetchone()[0]
                out(f"  ✓ {label}: added ({n} clip{'s' if n != 1 else ''})")
                done["added"].append(url)
            except (ValueError, p.FetchError, LookupError) as e:
                out(f"  ✗ {label}: {e}")
                done["failed"].append((url, str(e)))

        for i, ph in enumerate(picks.get("photos", []), 1):
            url = canonical(ph["url"], "photo")
            label = f"Photo {i} {url}"
            if con.execute("SELECT 1 FROM media WHERE source_url = ?", (url,)).fetchone():
                out(f"  = {label}: already added")
                done["skipped"].append(url)
                continue
            out(f"  … {label}: downloading slide {ph.get('slide', 1)}")
            try:
                _, sc = p.parse_link(url)
                data, ext, note = igphotos.fetch_slide(sc, ph.get("slide", 1), get=get, gallery=gallery)
                handle = (ph.get("credit") or "").lstrip("@") or None
                credit = f"@{handle}" + (f" ({ph['credit_label']})" if ph.get("credit_label") else "") if handle else None
                sitedb.add_photo_post(con, slug, url, data, ext, ph.get("craft_id"), credit_name=credit,
                                      credit_url=f"https://www.instagram.com/{handle}/" if handle else None,
                                      alt_text=ph.get("alt"), slide=ph.get("slide", 1))
                out(f"  ✓ {label}: added" + (f" ({note})" if note else ""))
                done["added"].append(url)
            except (ValueError, igphotos.PhotoError, LookupError) as e:
                out(f"  ✗ {label}: {e}")
                done["failed"].append((url, str(e)))
    finally:
        con.close()

    out(f"\nDone: {len(done['added'])} added, {len(done['skipped'])} already there, {len(done['failed'])} failed.")
    if any("/p/" in (u or "") for u, _ in done["failed"]):
        out("For photos that failed: open the post in Instagram, save the image, and use\n"
            "'Upload photo or video' on the city page. Or install gallery-dl and run this again:\n"
            "  python3 -m pip install gallery-dl\n  export VG_COOKIES_BROWSER=chrome")
    if any("youtu" in (u or "") for u, _ in done["failed"]):
        out("For YouTube videos that failed: read the Fix line above each one. Most often it is\n"
            "  brew install ffmpeg      (needed for From/To sections and HD)\n"
            "  python3 -m pip install -U yt-dlp")
    if any("/reel/" in (u or "") for u, _ in done["failed"]):
        out("For videos that failed with a login message: sign in to Instagram in Chrome, then\n"
            "  export VG_COOKIES_BROWSER=chrome\nand run this again.")
    out("Open the admin (python3 ui.py) to check credits and ask each creator for permission.")
    return done


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 import_picks.py picks/<city>.json")
    try:
        run(sys.argv[1])
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        sys.exit(f"Could not read the picks file: {e}")
    except LookupError as e:
        sys.exit(str(e))
