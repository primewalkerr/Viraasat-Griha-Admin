#!/usr/bin/env python3
"""One-off: move every media asset still stored as a Turso media_files blob
(photos and videos uploaded before Cloudinary was wired in) onto Cloudinary,
and point media.local_path / poster_path at the resulting URL instead.

Also creates the media rows for any reel that was assigned to a city but
never got one, because an earlier run died mid-way through saving it (the
original trigger for this whole migration: a slow blob upload timed out the
Turso connection's Hrana stream between the reel row's own commit and the
media row's).

    python3 migrate_to_cloudinary.py            # do it
    python3 migrate_to_cloudinary.py --dry-run  # just print what would move
"""
from __future__ import annotations

import sys
from pathlib import Path

import cloud
import pipeline as p


def _upload(con, rel_path: str) -> str:
    """Upload rel_path (preferring the local disk copy; falling back to the
    blob already in media_files if the local cache was cleared) to Cloudinary
    and return its URL."""
    full = p.MEDIA_DIR / rel_path
    if not full.is_file():
        row = con.execute("SELECT data FROM media_files WHERE path = ?", (rel_path,)).fetchone()
        if not row:
            raise FileNotFoundError(f"{rel_path}: not on disk and no blob in media_files")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(row["data"])
    resource_type = "video" if full.suffix.lower() in p.VIDEO_EXT else "image"
    return cloud.upload(full, rel_path, resource_type)


def migrate_existing_rows(con, dry_run: bool) -> None:
    rows = con.execute(
        "SELECT id, local_path, poster_path FROM media "
        "WHERE (local_path IS NOT NULL AND local_path NOT LIKE 'http%') "
        "OR (poster_path IS NOT NULL AND poster_path NOT LIKE 'http%') ORDER BY id").fetchall()
    cache: dict[str, str] = {}
    for r in rows:
        updates = {}
        for col in ("local_path", "poster_path"):
            rel = r[col]
            if not rel or rel.startswith("http"):
                continue
            if rel not in cache:
                print(f"  uploading {rel} ...")
                cache[rel] = f"<would upload {rel}>" if dry_run else _upload(con, rel)
            updates[col] = cache[rel]
        if updates and not dry_run:
            sets = ", ".join(f"{c} = ?" for c in updates)
            con.execute(f"UPDATE media SET {sets} WHERE id = ?", [*updates.values(), r["id"]])
            con.commit()
        print(f"media #{r['id']}: {updates}")
    if dry_run:
        return
    # Reclaim the blobs now that every reference has moved to Cloudinary.
    for rel in cache:
        con.execute("DELETE FROM media_files WHERE path = ?", (rel,))
        con.commit()


def repair_orphaned_reels(con, dry_run: bool) -> None:
    """Reels that got a city/craft assigned (so add_post's duplicate check
    finds them) but have no media row, because the original request died
    between the reel's own commit and the media row's."""
    reels = con.execute("""
        SELECT r.* FROM reels r
        WHERE r.city_id IS NOT NULL AND r.video_path IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM media m WHERE m.reel_id = r.id)
    """).fetchall()
    for r in reels:
        name = Path(r["video_path"]).name
        stem = Path(name).stem
        poster_name = next((f"{stem}{ext}" for ext in (".webp", ".jpg", ".jpeg")
                             if (p.MEDIA_DIR / f"{stem}{ext}").is_file()), None)
        craft = con.execute("SELECT name FROM crafts WHERE id = ?",
                             (r["craft_id"],)).fetchone() if r["craft_id"] else None
        city = con.execute("SELECT name FROM cities WHERE id = ?", (r["city_id"],)).fetchone()
        alt = f"{craft['name']} in {city['name']}" if craft else city["name"]
        print(f"reel #{r['id']} ({r['shortcode']}): video={name} poster={poster_name}")
        if dry_run:
            continue
        video_url = _upload(con, name)
        poster_url = _upload(con, poster_name) if poster_name else None
        con.execute(
            "INSERT INTO media (kind, local_path, poster_path, alt_text, duration_s, "
            "credit_name, permission, source_url, caption, reel_id, city_id, craft_id) "
            "VALUES ('video', ?, ?, ?, ?, ?, 'not_asked', ?, ?, ?, ?, ?)",
            (video_url, poster_url, alt, r["duration_s"], r["uploader"], r["url"], r["caption"],
             r["id"], r["city_id"], r["craft_id"]))
        con.execute("UPDATE cities SET updated_at = datetime('now') WHERE id = ?", (r["city_id"],))
        con.commit()


def main():
    dry_run = "--dry-run" in sys.argv
    if not dry_run and not cloud.configured():
        sys.exit("Cloudinary isn't configured (CLOUDINARY_* in .env) - refusing to touch the database.")
    con = p.connect()
    print("== existing blob-backed media rows ==")
    migrate_existing_rows(con, dry_run)
    print("== orphaned reels (assigned to a city, no media row) ==")
    repair_orphaned_reels(con, dry_run)
    con.close()
    print("done" if not dry_run else "dry run done - nothing changed")


if __name__ == "__main__":
    main()
