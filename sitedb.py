"""Viraasat site data: cities, crafts, story, media and shops.

Everything lives in the same viraasat.db as the reel pipeline. Tables follow
schema.sql (from the project's viraasat_schema.sql). Each function takes an open
connection from connect() and returns plain dicts ready to send as JSON.
"""
from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

import pipeline as p

SCHEMA_PATH = p.HERE / "schema.sql"

CATEGORIES = [
    ("art", "Art", "कला", "Metal, stone, clay, paint. The craft first, then who makes it.", "#1F2F5C", 1),
    ("food", "Food", "भोजन", None, "#9C3B2E", 2),
    ("clothing", "Clothing", "वस्त्र", "Weaves, prints and embroidery, from Banarasi silk to Chikankari.", "#A9803A", 3),
]

# pin, slug, name, name_hi, state, map_x, map_y, lat, lon, aliases
CITIES = [
    (1, "srinagar", "Srinagar", "श्रीनगर", "Jammu & Kashmir", 156, 95, 34.0837, 74.7973, ["kashmir"]),
    (2, "khurja", "Khurja", "खुर्जा", "Uttar Pradesh", 217, 224, 28.2514, 77.8540, []),
    (3, "agra", "Agra", "आगरा", "Uttar Pradesh", 237, 247, 27.1767, 78.0081, []),
    (4, "jaipur", "Jaipur", "जयपुर", "Rajasthan", 176, 253, 26.9124, 75.7873, ["pink city"]),
    (5, "lucknow", "Lucknow", "लखनऊ", "Uttar Pradesh", 278, 254, 26.8467, 80.9462, ["lko", "lakhnau"]),
    (6, "varanasi", "Varanasi", "वाराणसी", "Uttar Pradesh", 320, 288, 25.3176, 82.9739, ["banaras", "benares", "kashi"]),
    (7, "madhubani", "Madhubani", "मधुबनी", "Bihar", 381, 265, 26.3483, 86.0712, ["mithila"]),
    (8, "bhuj", "Bhuj", "भुज", "Gujarat", 53, 334, 23.2420, 69.6669, ["kutch", "kachchh"]),
    (9, "puri", "Puri", "पुरी", "Odisha", 376, 409, 19.8135, 85.8312, ["raghurajpur"]),
    (10, "jagdalpur", "Jagdalpur", "जगदलपुर", "Chhattisgarh", 300, 425, 19.0748, 82.0080, ["bastar"]),
    (11, "bidar", "Bidar", "बीदर", "Karnataka", 212, 453, 17.9133, 77.5301, []),
    (12, "channapatna", "Channapatna", "चन्नपटना", "Karnataka", 204, 567, 12.6518, 77.2089, []),
    (13, "thanjavur", "Thanjavur", "तंजावुर", "Tamil Nadu", 243, 608, 10.7870, 79.1378, ["tanjore"]),
    (14, "kanyakumari", "Kanyakumari", "कन्याकुमारी", "Tamil Nadu", 211, 667, 8.0883, 77.5385, ["cape comorin"]),
]

# slug, name, category, short_desc, gi_tag, gi_year, keywords  (same order as CITIES)
CRAFTS = [
    ("papier-mache", "Papier-mâché", "art", None, 0, None, ["papier mache", "papier-mache", "papiermache"]),
    ("khurja-pottery", "Glazed ceramics", "art", "Glazed ceramics", 0, None, ["pottery", "ceramic", "ceramics"]),
    ("marble-inlay", "Makrana marble inlay", "art", "Makrana marble inlay", 0, None, ["marble inlay", "pietra dura", "parchin kari"]),
    ("jaipur-stone-jewellery", "Stone carving and jewellery", "art", "Stone carving and jewellery", 0, None, ["stone carving", "gemstone", "kundan"]),
    ("chikankari", "Chikankari", "clothing", "Chikankari embroidery", 0, None, ["chikankari", "chikan"]),
    ("banarasi-silk", "Banarasi silk", "clothing", None, 0, None, ["banarasi", "brocade", "zari"]),
    ("madhubani-painting", "Mithila painting", "art", None, 0, None, ["madhubani", "mithila painting"]),
    ("rogan-ajrakh", "Rogan, Ajrakh", "art", None, 0, None, ["rogan", "ajrakh", "ajrak"]),
    ("pattachitra", "Pattachitra", "art", None, 0, None, ["pattachitra", "patachitra"]),
    ("dokra", "Dokra", "art", "lost-wax metal casting", 0, None, ["dokra", "dhokra", "lost wax", "bell metal"]),
    ("bidriware", "Bidriware", "art", "silver inlay on dark metal", 1, 2006, ["bidri", "bidriware"]),
    ("channapatna-toys", "Lacquered toys", "art", None, 0, None, ["channapatna toys", "wooden toys", "lacquer toys"]),
    ("thanjavur-painting", "Gold-foil painting", "art", None, 0, None, ["tanjore painting", "thanjavur painting"]),
    ("seashell-craft", "Seashell craft", "art", None, 0, None, ["seashell", "shell craft"]),
]

CITY_FIELDS = ["name_hi", "state", "district", "country", "lat", "lon", "tagline", "status", "aliases"]
CRAFT_FIELDS = ["name", "name_hi", "category_id", "short_desc", "materials", "gi_tag", "gi_year",
                "keywords", "status"]
MEDIA_FIELDS = ["alt_text", "credit_name", "credit_url", "license", "permission", "craft_id"]
STORE_FIELDS = ["name", "address", "postcode", "lat", "lon", "phone", "website", "maps_url",
                "google_place_id", "rating", "rating_count", "rating_source", "rating_checked_at",
                "verified", "sort_order", "notes"]
STATUSES = {"draft", "in_review", "published", "hidden"}
PERMISSIONS = {"not_asked", "requested", "granted", "refused", "own"}
FACT_STATUSES = {"unchecked", "checked", "disputed"}
UPLOAD_EXT = p.IMAGE_EXT | p.VIDEO_EXT
MAX_UPLOAD = 300 * 1024 * 1024


# ----------------------------------------------------------------- setup
def connect(path=None) -> sqlite3.Connection:
    con = p.connect(path)  # reels table + its newer columns
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not con.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        seed(con)
    return con


def seed(con: sqlite3.Connection) -> None:
    con.executemany("INSERT INTO categories (slug, name, name_hi, blurb, accent_hex, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)", CATEGORIES)
    cat = {r["slug"]: r["id"] for r in con.execute("SELECT id, slug FROM categories")}
    for (pin, slug, name, hi, state, mx, my, lat, lon, aliases), craft in zip(CITIES, CRAFTS):
        cid = con.execute(
            "INSERT INTO cities (pin_number, slug, name, name_hi, state, map_x, map_y, lat, lon, aliases) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pin, slug, name, hi, state, mx, my, lat, lon, json.dumps(aliases, ensure_ascii=False))).lastrowid
        cslug, cname, ccat, desc, gi, giy, kw = craft
        crid = con.execute(
            "INSERT INTO crafts (slug, name, category_id, short_desc, gi_tag, gi_year, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (cslug, cname, cat[ccat], desc, gi, giy, json.dumps(kw))).lastrowid
        con.execute("INSERT INTO city_crafts (city_id, craft_id, is_featured) VALUES (?, ?, 1)", (cid, crid))
    con.execute("UPDATE cities SET tagline = 'Bidriware: silver that glows on dark metal' WHERE slug = 'bidar'")
    con.commit()


# --------------------------------------------------------------- helpers
def _num(v, cast=float):
    if v in (None, ""):
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        raise ValueError(f"Not a number: {v!r}")


def _text(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _jlist(v) -> str:
    if isinstance(v, str):
        v = [x for x in (s.strip() for s in v.split(",")) if x]
    return json.dumps([str(x).strip() for x in (v or []) if str(x).strip()], ensure_ascii=False)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def _city(con, slug: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM cities WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise LookupError(f"No city {slug!r}")
    return row


def _craft_in_city(con, city_id: int, craft_id) -> int | None:
    if craft_id in (None, ""):
        return None
    craft_id = int(craft_id)
    if not con.execute("SELECT 1 FROM city_crafts WHERE city_id = ? AND craft_id = ?",
                       (city_id, craft_id)).fetchone():
        raise ValueError("That craft is not linked to this city.")
    return craft_id


def _featured_craft(con, city_id: int):
    return con.execute("SELECT craft_id FROM city_crafts WHERE city_id = ? AND is_featured = 1",
                       (city_id,)).fetchone()


def media_out(r) -> dict:
    d = dict(r)
    d["file_url"] = f"/media/{d['local_path']}" if d.get("local_path") else d.get("url")
    d["poster_file_url"] = f"/media/{d['poster_path']}" if d.get("poster_path") else d.get("poster_url")
    d["file_missing"] = bool(d.get("local_path")) and not (p.MEDIA_DIR / d["local_path"]).is_file()
    return d


def _touch_city(con, city_id: int) -> None:
    con.execute("UPDATE cities SET updated_at = datetime('now') WHERE id = ?", (city_id,))


def push_media_file(con, rel_path: str | None) -> None:
    """Upload a file already saved under MEDIA_DIR into the media_files blob
    table, keyed by the same relative path stored in media.local_path /
    poster_path. The deployed site has no local disk of its own — it serves
    /media/<path> straight out of this table, so a new upload or import isn't
    visible there until this runs."""
    if not rel_path:
        return
    full = p.MEDIA_DIR / rel_path
    if not full.is_file():
        return
    data = full.read_bytes()
    content_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
    con.execute(
        "INSERT OR REPLACE INTO media_files (path, content_type, data, size) VALUES (?, ?, ?, ?)",
        (rel_path, content_type, data, len(data)))


# ------------------------------------------------------------------ read
def list_cities(con) -> list[dict]:
    rows = con.execute("""
        SELECT c.*, cr.name AS craft_name, cr.id AS craft_id, cat.slug AS category,
          (SELECT COUNT(*) FROM media m WHERE m.city_id = c.id) AS media_count,
          (SELECT COUNT(*) FROM media m WHERE m.city_id = c.id AND m.permission IN ('granted','own')) AS media_usable,
          (SELECT COUNT(*) FROM stores s WHERE s.city_id = c.id) AS store_count,
          (SELECT COUNT(*) FROM stores s WHERE s.city_id = c.id AND s.verified = 1) AS store_verified,
          (SELECT COUNT(*) FROM craft_story_paragraphs sp
             WHERE sp.city_id = c.id AND sp.craft_id = cr.id) AS story_count,
          (SELECT COUNT(*) FROM craft_story_paragraphs sp
             WHERE sp.city_id = c.id AND sp.craft_id = cr.id AND sp.fact_status = 'checked') AS story_checked,
          hm.local_path AS hero_path, hm.poster_path AS hero_poster, hm.kind AS hero_kind,
          cm.local_path AS card_path, cm.poster_path AS card_poster, cm.kind AS card_kind
        FROM cities c
        LEFT JOIN city_crafts cc ON cc.city_id = c.id AND cc.is_featured = 1
        LEFT JOIN crafts cr ON cr.id = cc.craft_id
        LEFT JOIN categories cat ON cat.id = cr.category_id
        LEFT JOIN media hm ON hm.id = c.hero_media_id
        LEFT JOIN media cm ON cm.id = c.card_media_id
        ORDER BY c.pin_number, c.name""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        img = None
        for kind, path, poster in ((d["card_kind"], d["card_path"], d["card_poster"]),
                                   (d["hero_kind"], d["hero_path"], d["hero_poster"])):
            pick = poster if kind == "video" else path
            if pick:
                img = f"/media/{pick}"
                break
        d["image_url"] = img
        out.append(d)
    return out


def get_city(con, slug: str) -> dict:
    c = dict(_city(con, slug))
    c["aliases"] = json.loads(c["aliases"] or "[]")
    crafts = [dict(r) for r in con.execute("""
        SELECT cr.*, cc.is_featured, cat.slug AS category FROM city_crafts cc
        JOIN crafts cr ON cr.id = cc.craft_id JOIN categories cat ON cat.id = cr.category_id
        WHERE cc.city_id = ? ORDER BY cc.is_featured DESC, cc.sort_order, cr.name""", (c["id"],))]
    for cr in crafts:
        cr["keywords"] = json.loads(cr["keywords"] or "[]")
        cr["story"] = get_story(con, c["id"], cr["id"])
        cr["also_in"] = [r["name"] for r in con.execute(
            "SELECT ci.name FROM city_crafts cc JOIN cities ci ON ci.id = cc.city_id "
            "WHERE cc.craft_id = ? AND cc.city_id != ? ORDER BY ci.pin_number", (cr["id"], c["id"]))]
    media = [media_out(r) for r in con.execute(
        "SELECT * FROM media WHERE city_id = ? ORDER BY id DESC", (c["id"],))]
    stores = []
    for r in con.execute("SELECT * FROM stores WHERE city_id = ? ORDER BY sort_order, id", (c["id"],)):
        s = dict(r)
        s["craft_ids"] = [x["craft_id"] for x in con.execute(
            "SELECT craft_id FROM store_crafts WHERE store_id = ?", (s["id"],))]
        stores.append(s)
    return {
        "city": c, "crafts": crafts, "media": media, "stores": stores,
        "categories": [dict(r) for r in con.execute("SELECT * FROM categories ORDER BY sort_order")],
        "all_crafts": [dict(r) for r in con.execute("SELECT id, name, slug FROM crafts ORDER BY name")],
        "homepage_video_media_id": get_setting(con, "homepage_video_media_id", cast=int),
    }


# -------------------------------------------------------------- settings
def get_setting(con, key: str, cast=str):
    r = con.execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    return cast(r["value"]) if r and r["value"] is not None else None


def set_setting(con, key: str, value) -> None:
    con.execute(
        "INSERT INTO site_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value in (None, "") else str(value)))
    con.commit()


def set_homepage_video(con, media_id) -> dict | None:
    """Pick (or unset, with media_id=None) the video that plays behind the
    home page hero. Any cleared video from any city works - it's a single
    site-wide choice, not scoped to one city."""
    if media_id not in (None, ""):
        m = con.execute("SELECT kind FROM media WHERE id = ?", (int(media_id),)).fetchone()
        if not m:
            raise LookupError(f"No photo or video #{media_id}")
        if m["kind"] != "video":
            raise ValueError("The home page video must be a video.")
        media_id = int(media_id)
    else:
        media_id = None
    set_setting(con, "homepage_video_media_id", media_id)
    return homepage_video_info(con)


def homepage_video_info(con) -> dict | None:
    """The currently-picked home page video, with enough city/craft context
    to show it on the dashboard, or None if nothing is picked."""
    media_id = get_setting(con, "homepage_video_media_id", cast=int)
    if not media_id:
        return None
    r = con.execute("""
        SELECT m.id, m.local_path, m.poster_path, m.alt_text,
               ci.name AS city_name, cr.name AS craft_name
        FROM media m JOIN cities ci ON ci.id = m.city_id
        LEFT JOIN crafts cr ON cr.id = m.craft_id
        WHERE m.id = ?""", (media_id,)).fetchone()
    return dict(r) if r else None


def list_videos(con) -> list[dict]:
    """Every video across every city, plus any uploaded just for the home
    page (no city of their own), for the home page video picker."""
    return [dict(r) for r in con.execute("""
        SELECT m.id, m.local_path, m.poster_path, m.alt_text, m.permission,
               ci.name AS city_name, ci.slug AS city_slug, cr.name AS craft_name
        FROM media m LEFT JOIN cities ci ON ci.id = m.city_id
        LEFT JOIN crafts cr ON cr.id = m.craft_id
        WHERE m.kind = 'video'
        ORDER BY ci.pin_number IS NULL, ci.pin_number, m.id DESC""")]


def upload_homepage_video(con, filename: str, data: bytes) -> dict:
    """Upload a video that's just for the home page background - not tied to
    any city or craft - and set it as the current pick right away."""
    ext = Path(filename or "").suffix.lower()
    if ext not in p.VIDEO_EXT:
        raise ValueError(f"Use a video file ({', '.join(sorted(p.VIDEO_EXT))}).")
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_UPLOAD:
        raise ValueError("The file is larger than 300 MB.")
    folder = p.MEDIA_DIR / "site"
    folder.mkdir(parents=True, exist_ok=True)
    stem = _slugify(Path(filename).stem)[:40]
    name = f"{int(time.time() * 1000)}-{stem}{ext}"
    (folder / name).write_bytes(data)
    rel_path = f"site/{name}"
    push_media_file(con, rel_path)
    media_id = con.execute(
        "INSERT INTO media (kind, local_path, alt_text, permission) VALUES ('video', ?, ?, 'not_asked')",
        (rel_path, "Home page background video")).lastrowid
    set_setting(con, "homepage_video_media_id", media_id)
    con.commit()
    return homepage_video_info(con)


def get_story(con, city_id: int, craft_id: int) -> list[dict]:
    paras = []
    for r in con.execute("SELECT * FROM craft_story_paragraphs WHERE craft_id = ? AND city_id = ? "
                         "ORDER BY position", (craft_id, city_id)):
        d = dict(r)
        d["sources"] = [dict(s) for s in con.execute(
            "SELECT s.* FROM paragraph_sources ps JOIN sources s ON s.id = ps.source_id "
            "WHERE ps.paragraph_id = ? ORDER BY s.id", (d["id"],))]
        paras.append(d)
    return paras


# ----------------------------------------------------------------- city
def create_city(con, data: dict) -> dict:
    """Add a city. data: name, state (required); name_hi, district, country, lat, lon,
    aliases, pin_number; craft_id (existing craft) or craft_name + category_id (new)."""
    name, state = _text(data.get("name")), _text(data.get("state"))
    if not name:
        raise ValueError("Give the city a name.")
    if not state:
        raise ValueError("Give the city's state (Look up can fill it in).")
    slug = _slugify(name)
    if con.execute("SELECT 1 FROM cities WHERE slug = ? OR lower(name) = lower(?)", (slug, name)).fetchone():
        raise ValueError(f"{name} is already in the list.")
    lat, lon = _num(data.get("lat")), _num(data.get("lon"))
    if lat is not None and not -90 <= lat <= 90 or lon is not None and not -180 <= lon <= 180:
        raise ValueError("Coordinates look wrong.")
    pin = _num(data.get("pin_number"), int)
    if pin is None:
        pin = con.execute("SELECT COALESCE(MAX(pin_number), 0) + 1 FROM cities").fetchone()[0]
    elif con.execute("SELECT 1 FROM cities WHERE pin_number = ?", (pin,)).fetchone():
        raise ValueError(f"Pin number {pin} is already used.")

    craft_id = _num(data.get("craft_id"), int)
    craft_name = _text(data.get("craft_name"))
    if craft_id and not con.execute("SELECT 1 FROM crafts WHERE id = ?", (craft_id,)).fetchone():
        raise LookupError(f"No craft #{craft_id}")

    cid = con.execute(
        "INSERT INTO cities (slug, name, name_hi, state, district, country, lat, lon, pin_number, aliases) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, name, _text(data.get("name_hi")), state, _text(data.get("district")),
         _text(data.get("country")) or "India", lat, lon, pin, _jlist(data.get("aliases")))).lastrowid
    con.commit()
    if craft_id:
        return save_craft(con, slug, {"existing_id": craft_id, "featured": True})
    if craft_name:
        return save_craft(con, slug, {"name": craft_name, "category_id": data.get("category_id"), "featured": True})
    return get_city(con, slug)


def delete_city(con, slug: str, confirm_name: str = "") -> dict:
    """Delete a city with its shops and story. Its Instagram reels go back to
    'waiting for a city'; video and photo files stay in the media folder; crafts
    used by no other city are deleted too."""
    c = _city(con, slug)
    if (confirm_name or "").strip().lower() != c["name"].lower():
        raise ValueError(f"Type the city's name ({c['name']}) to confirm.")
    crafts = [r["craft_id"] for r in con.execute("SELECT craft_id FROM city_crafts WHERE city_id = ?", (c["id"],))]
    counts = {
        "media": con.execute("SELECT COUNT(*) FROM media WHERE city_id = ?", (c["id"],)).fetchone()[0],
        "shops": con.execute("SELECT COUNT(*) FROM stores WHERE city_id = ?", (c["id"],)).fetchone()[0],
        "paragraphs": con.execute("SELECT COUNT(*) FROM craft_story_paragraphs WHERE city_id = ?", (c["id"],)).fetchone()[0],
    }
    con.execute("UPDATE reels SET city_id = NULL, craft_id = NULL WHERE city_id = ?", (c["id"],))
    con.execute("UPDATE cities SET hero_media_id = NULL, card_media_id = NULL WHERE id = ?", (c["id"],))
    con.execute("DELETE FROM media WHERE city_id = ?", (c["id"],))
    con.execute("DELETE FROM cities WHERE id = ?", (c["id"],))  # cascades: shops, story, craft links
    removed = []
    for cid in crafts:
        if not con.execute("SELECT 1 FROM city_crafts WHERE craft_id = ?", (cid,)).fetchone():
            row = con.execute("SELECT name FROM crafts WHERE id = ?", (cid,)).fetchone()
            con.execute("UPDATE reels SET craft_id = NULL WHERE craft_id = ?", (cid,))
            con.execute("DELETE FROM crafts WHERE id = ?", (cid,))
            removed.append(row["name"])
    con.execute("DELETE FROM sources WHERE id NOT IN (SELECT source_id FROM paragraph_sources)")
    con.commit()
    return {"deleted": c["name"], **counts, "crafts_removed": removed}


def update_city(con, slug: str, data: dict) -> dict:
    c = _city(con, slug)
    vals = {}
    for k in CITY_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("lat", "lon"):
            v = _num(v)
        elif k == "aliases":
            v = _jlist(v)
        else:
            v = _text(v)
        vals[k] = v
    if "state" in vals and not vals["state"]:
        raise ValueError("State can't be empty.")
    if "country" in vals and not vals["country"]:
        vals["country"] = "India"
    if vals.get("status") and vals["status"] not in STATUSES:
        raise ValueError(f"Unknown status {vals['status']!r}")
    if vals.get("lat") is not None and not -90 <= vals["lat"] <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if vals.get("lon") is not None and not -180 <= vals["lon"] <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    if vals:
        sets = ", ".join(f"{k} = ?" for k in vals)
        con.execute(f"UPDATE cities SET {sets}, updated_at = datetime('now') WHERE id = ?",
                    [*vals.values(), c["id"]])
        con.commit()
    return get_city(con, slug)


def set_city_image(con, slug: str, media_id, which: str) -> dict:
    c = _city(con, slug)
    if which not in ("hero", "card"):
        raise ValueError("which must be hero or card")
    if media_id not in (None, ""):
        m = con.execute("SELECT city_id FROM media WHERE id = ?", (int(media_id),)).fetchone()
        if not m or m["city_id"] != c["id"]:
            raise ValueError("That photo or video isn't in this city.")
        media_id = int(media_id)
    else:
        media_id = None
    con.execute(f"UPDATE cities SET {which}_media_id = ?, updated_at = datetime('now') WHERE id = ?",
                (media_id, c["id"]))
    con.commit()
    return get_city(con, slug)


def set_craft_media(con, slug: str, craft_id, media_id) -> dict:
    """Pick (or unset) a craft's single "story video" - the one the city page
    leads with, shown before any other video of that craft."""
    c = _city(con, slug)
    craft_id = int(craft_id)
    if not con.execute("SELECT 1 FROM city_crafts WHERE city_id = ? AND craft_id = ?",
                       (c["id"], craft_id)).fetchone():
        raise ValueError("That craft is not linked to this city.")
    if media_id not in (None, ""):
        m = con.execute("SELECT city_id, craft_id, kind FROM media WHERE id = ?", (int(media_id),)).fetchone()
        if not m or m["city_id"] != c["id"] or m["craft_id"] != craft_id:
            raise ValueError("That video isn't linked to this craft.")
        if m["kind"] != "video":
            raise ValueError("The story video must be a video.")
        media_id = int(media_id)
    else:
        media_id = None
    con.execute("UPDATE crafts SET making_media_id = ?, updated_at = datetime('now') WHERE id = ?",
                (media_id, craft_id))
    con.commit()
    return get_city(con, slug)


# ---------------------------------------------------------------- crafts
def save_craft(con, slug: str, data: dict) -> dict:
    """Create or update a craft and link it to this city.
    data.featured=True makes it the city's featured craft."""
    c = _city(con, slug)
    vals = {}
    for k in CRAFT_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "keywords":
            v = _jlist(v)
        elif k == "gi_tag":
            v = 1 if v in (1, True, "1", "true", "on") else 0
        elif k in ("gi_year", "category_id"):
            v = _num(v, int)
        else:
            v = _text(v)
        vals[k] = v
    if vals.get("status") and vals["status"] not in STATUSES:
        raise ValueError(f"Unknown status {vals['status']!r}")
    if vals.get("gi_year") is not None and not 1999 <= vals["gi_year"] <= date.today().year:
        raise ValueError("GI year looks wrong (India's GI Act dates from 1999).")
    if not vals.get("gi_tag", 1):
        vals["gi_year"] = None

    craft_id = data.get("id") or data.get("existing_id")
    if data.get("id"):
        craft_id = int(data["id"])
        if not con.execute("SELECT 1 FROM crafts WHERE id = ?", (craft_id,)).fetchone():
            raise LookupError(f"No craft #{craft_id}")
        if "name" in vals and not vals["name"]:
            raise ValueError("Craft name can't be empty.")
        if vals:
            sets = ", ".join(f"{k} = ?" for k in vals)
            con.execute(f"UPDATE crafts SET {sets}, updated_at = datetime('now') WHERE id = ?",
                        [*vals.values(), craft_id])
    elif data.get("existing_id"):
        craft_id = int(data["existing_id"])
        if not con.execute("SELECT 1 FROM crafts WHERE id = ?", (craft_id,)).fetchone():
            raise LookupError(f"No craft #{craft_id}")
    else:
        if not vals.get("name"):
            raise ValueError("Give the craft a name.")
        if not vals.get("category_id"):
            vals["category_id"] = con.execute("SELECT id FROM categories WHERE slug = 'art'").fetchone()["id"]
        base = cslug = _slugify(vals["name"])
        n = 2
        while con.execute("SELECT 1 FROM crafts WHERE slug = ?", (cslug,)).fetchone():
            cslug, n = f"{base}-{n}", n + 1
        vals["slug"] = cslug
        cols = ", ".join(vals)
        craft_id = con.execute(f"INSERT INTO crafts ({cols}) VALUES ({', '.join('?' * len(vals))})",
                               list(vals.values())).lastrowid

    con.execute("INSERT OR IGNORE INTO city_crafts (city_id, craft_id, is_featured) VALUES (?, ?, 0)",
                (c["id"], craft_id))
    if data.get("featured") or not _featured_craft(con, c["id"]):
        con.execute("UPDATE city_crafts SET is_featured = 0 WHERE city_id = ?", (c["id"],))
        con.execute("UPDATE city_crafts SET is_featured = 1 WHERE city_id = ? AND craft_id = ?",
                    (c["id"], craft_id))
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


def unlink_craft(con, slug: str, craft_id) -> dict:
    """Remove a craft from this city (the craft stays for other cities)."""
    c = _city(con, slug)
    craft_id = int(craft_id)
    if con.execute("SELECT COUNT(*) FROM city_crafts WHERE city_id = ?", (c["id"],)).fetchone()[0] <= 1:
        raise ValueError("A city needs at least one craft.")
    was = con.execute("SELECT is_featured FROM city_crafts WHERE city_id = ? AND craft_id = ?",
                      (c["id"], craft_id)).fetchone()
    if not was:
        raise LookupError("That craft isn't linked to this city.")
    con.execute("DELETE FROM city_crafts WHERE city_id = ? AND craft_id = ?", (c["id"], craft_id))
    con.execute("DELETE FROM craft_story_paragraphs WHERE city_id = ? AND craft_id = ?", (c["id"], craft_id))
    if was["is_featured"]:
        con.execute("UPDATE city_crafts SET is_featured = 1 WHERE rowid = "
                    "(SELECT rowid FROM city_crafts WHERE city_id = ? ORDER BY sort_order LIMIT 1)", (c["id"],))
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


def save_story(con, slug: str, craft_id, paragraphs: list) -> dict:
    """Replace this city's story for one craft. Each paragraph:
    {body, fact_status, sources: [{title, publisher, url, accessed_on}]}."""
    c = _city(con, slug)
    craft_id = _craft_in_city(con, c["id"], craft_id)
    if craft_id is None:
        raise ValueError("Choose a craft.")
    clean = []
    for i, para in enumerate(paragraphs or [], 1):
        body = _text(para.get("body"))
        if not body:
            continue
        fs = para.get("fact_status") or "unchecked"
        if fs not in FACT_STATUSES:
            raise ValueError(f"Unknown fact status {fs!r}")
        srcs = []
        for s in para.get("sources") or []:
            title, url = _text(s.get("title")), _text(s.get("url"))
            if not title and not url:
                continue
            if url and not re.match(r"https?://", url, re.I):
                raise ValueError(f"Source link must start with http:// or https:// ({url})")
            srcs.append({"title": title or url, "publisher": _text(s.get("publisher")),
                         "url": url, "accessed_on": _text(s.get("accessed_on"))})
        if fs == "checked" and len(srcs) < 2:
            raise ValueError(f"Paragraph {i} is marked checked but has {len(srcs)} source(s); "
                             "the site rule is two published sources per fact.")
        clean.append((body, fs, srcs))

    con.execute("DELETE FROM craft_story_paragraphs WHERE craft_id = ? AND city_id = ?", (craft_id, c["id"]))
    for pos, (body, fs, srcs) in enumerate(clean, 1):
        pid = con.execute("INSERT INTO craft_story_paragraphs (craft_id, city_id, position, body, fact_status) "
                          "VALUES (?, ?, ?, ?, ?)", (craft_id, c["id"], pos, body, fs)).lastrowid
        for s in srcs:
            row = con.execute("SELECT id FROM sources WHERE title = ? AND IFNULL(url, '') = IFNULL(?, '')",
                              (s["title"], s["url"])).fetchone()
            if row:
                sid = row["id"]
                con.execute("UPDATE sources SET publisher = COALESCE(?, publisher), "
                            "accessed_on = COALESCE(?, accessed_on) WHERE id = ?",
                            (s["publisher"], s["accessed_on"], sid))
            else:
                sid = con.execute("INSERT INTO sources (title, publisher, url, accessed_on) VALUES (?, ?, ?, ?)",
                                  (s["title"], s["publisher"], s["url"], s["accessed_on"])).lastrowid
            con.execute("INSERT OR IGNORE INTO paragraph_sources (paragraph_id, source_id) VALUES (?, ?)",
                        (pid, sid))
    # drop sources nothing points at any more
    con.execute("DELETE FROM sources WHERE id NOT IN (SELECT source_id FROM paragraph_sources)")
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


# ----------------------------------------------------------------- media
def _credit_url(info: dict, platform: str = "instagram") -> str | None:
    if info.get("uploader_url") or info.get("channel_url"):
        return info.get("uploader_url") or info.get("channel_url")
    handle = info.get("channel") or info.get("uploader_id")
    if platform == "youtube":
        uid = info.get("uploader_id") or ""
        if uid.startswith("@"):
            return f"https://www.youtube.com/{uid}"
        if info.get("channel_id"):
            return f"https://www.youtube.com/channel/{info['channel_id']}"
        return None
    if handle and re.fullmatch(r"[A-Za-z0-9._]+", str(handle)) and not str(handle).isdigit():
        return f"https://www.instagram.com/{handle}/"
    return None


def add_post(con, slug: str, link: str, craft_id=None, downloader=None, use_claude=True,
             start=None, end=None, yt_downloader=None) -> dict:
    """Download an Instagram reel or post, or a YouTube video or Short (optionally
    one From/To section of it), into this city's photos and videos."""
    c = _city(con, slug)
    craft_id = _craft_in_city(con, c["id"], craft_id) or (_featured_craft(con, c["id"]) or {"craft_id": None})["craft_id"]
    lk = p.parse_media_link(link, start, end)
    url, shortcode = lk["url"], lk["key"]
    what = "video section" if lk["start"] is not None else ("video" if lk["platform"] == "youtube" else "post")
    old = con.execute("SELECT r.id, r.city_id, ci.name AS city_name FROM reels r "
                      "LEFT JOIN cities ci ON ci.id = r.city_id WHERE r.shortcode = ?", (shortcode,)).fetchone()
    if old and old["city_id"]:
        raise ValueError(f"This {what} is already in {old['city_name']}.")
    if old:  # saved earlier by the reel pipeline but never placed in a city
        return assign_reel(con, old["id"], slug, craft_id)

    if lk["platform"] == "youtube":
        ytdl = yt_downloader or p.download_youtube
        res = p.fetch_info(url, shortcode, True, lambda _u, _k, _v: ytdl(lk), platform="youtube")
    else:
        dl = downloader or p.download_post
        res = p.fetch_info(url, shortcode, True, lambda u, sc, _v: dl(u, sc))
    info, items = res["info"], res["items"]
    craft = con.execute("SELECT name FROM crafts WHERE id = ?", (craft_id,)).fetchone() if craft_id else None

    row = p.build_row(url, shortcode, info, p.load_cities(), use_claude=use_claude, lookup=False)
    first_video = next((it for it in items if it["kind"] == "video"), None)
    row.update(
        video_path=str(p.MEDIA_DIR / first_video["path"]) if first_video else None,
        city=c["name"], state=c["state"], district=c["district"], country=c["country"],
        postcode=None, lat=c["lat"], lon=c["lon"], location_source="manual",
        craft=craft["name"] if craft else row["craft"], status="reviewed",
        city_id=c["id"], craft_id=craft_id)
    reel_id = p.save(con, row)

    credit, credit_url = row["uploader"], _credit_url(info, lk["platform"])
    alt = f"{craft['name']} in {c['name']}" if craft else c["name"]
    caption = row["caption"]
    if lk["platform"] == "youtube" and info.get("title") and info.get("title") not in (caption or ""):
        caption = f"{info['title']}\n\n{caption or ''}".strip()
    for it in items:
        push_media_file(con, it["path"])
        push_media_file(con, it.get("poster"))
        con.execute(
            "INSERT INTO media (kind, local_path, poster_path, alt_text, width, height, duration_s, "
            "credit_name, credit_url, permission, source_url, caption, reel_id, city_id, craft_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_asked', ?, ?, ?, ?, ?)",
            (it["kind"], it["path"], it.get("poster"), alt, it.get("width"), it.get("height"),
             it.get("duration_s"), credit, credit_url, url, caption, reel_id, c["id"], craft_id))
    _touch_city(con, c["id"])
    con.commit()
    out = get_city(con, slug)
    if res.get("note"):
        out["note"] = res["note"]
    return out


def assign_reel(con, reel_id, slug: str, craft_id=None) -> dict:
    """Place a reel saved earlier (before city pages existed) into a city."""
    c = _city(con, slug)
    craft_id = _craft_in_city(con, c["id"], craft_id) or (_featured_craft(con, c["id"]) or {"craft_id": None})["craft_id"]
    r = con.execute("SELECT * FROM reels WHERE id = ?", (int(reel_id),)).fetchone()
    if not r:
        raise LookupError(f"No reel #{reel_id}")
    craft = con.execute("SELECT name FROM crafts WHERE id = ?", (craft_id,)).fetchone() if craft_id else None
    con.execute("UPDATE reels SET city_id = ?, craft_id = ?, city = ?, state = ?, district = ?, country = ?, "
                "lat = ?, lon = ?, location_source = 'manual', craft = COALESCE(?, craft), status = 'reviewed', "
                "updated_at = ? WHERE id = ?",
                (c["id"], craft_id, c["name"], c["state"], c["district"], c["country"], c["lat"], c["lon"],
                 craft["name"] if craft else None, p.now(), r["id"]))
    if not con.execute("SELECT 1 FROM media WHERE reel_id = ?", (r["id"],)).fetchone():
        local = None
        if r["video_path"]:
            name = Path(r["video_path"]).name
            if (p.MEDIA_DIR / name).is_file():
                local = name
                push_media_file(con, local)
        con.execute(
            "INSERT INTO media (kind, local_path, alt_text, duration_s, credit_name, permission, "
            "source_url, caption, reel_id, city_id, craft_id) VALUES ('video', ?, ?, ?, ?, 'not_asked', ?, ?, ?, ?, ?)",
            (local, f"{craft['name']} in {c['name']}" if craft else c["name"], r["duration_s"],
             r["uploader"], r["url"], r["caption"], r["id"], c["id"], craft_id))
    else:
        con.execute("UPDATE media SET city_id = ?, craft_id = ? WHERE reel_id = ?", (c["id"], craft_id, r["id"]))
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


def unassigned_reels(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, shortcode, url, uploader, caption, city, craft, video_path, created_at "
        "FROM reels WHERE city_id IS NULL ORDER BY id DESC")]


def delete_reel(con, reel_id) -> dict:
    """Drop a reel that was saved before it was placed in a city. Refuses if
    it's since been assigned (it has media rows) - unassign it there first,
    or delete the photo/video itself."""
    r = con.execute("SELECT * FROM reels WHERE id = ?", (int(reel_id),)).fetchone()
    if not r:
        raise LookupError(f"No reel #{reel_id}")
    if con.execute("SELECT 1 FROM media WHERE reel_id = ?", (r["id"],)).fetchone():
        raise ValueError("This reel is already placed in a city; delete its photo/video instead.")
    con.execute("DELETE FROM reels WHERE id = ?", (r["id"],))
    con.commit()
    return {"deleted": r["id"]}


def add_photo_post(con, slug: str, link: str, data: bytes, ext: str, craft_id=None,
                   credit_name=None, credit_url=None, alt_text=None, slide: int = 1) -> int:
    """Save one image from an Instagram photo post into this city. Returns the media id."""
    c = _city(con, slug)
    craft_id = _craft_in_city(con, c["id"], craft_id) or (_featured_craft(con, c["id"]) or {"craft_id": None})["craft_id"]
    _, shortcode = p.parse_link(link)
    source = f"https://www.instagram.com/p/{shortcode}/"
    if con.execute("SELECT 1 FROM media WHERE source_url = ?", (source,)).fetchone():
        raise ValueError("This post is already added.")
    ext = ext.lower() if ext.lower() in p.IMAGE_EXT else ".jpg"
    if not data:
        raise ValueError("The image is empty.")
    p.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{shortcode}_photo{int(slide or 1)}{ext}"
    (p.MEDIA_DIR / name).write_bytes(data)
    push_media_file(con, name)
    craft = con.execute("SELECT name FROM crafts WHERE id = ?", (craft_id,)).fetchone() if craft_id else None
    mid = con.execute(
        "INSERT INTO media (kind, local_path, alt_text, credit_name, credit_url, permission, source_url, city_id, craft_id) "
        "VALUES ('photo', ?, ?, ?, ?, 'not_asked', ?, ?, ?)",
        (name, alt_text or (f"{craft['name']} in {c['name']}" if craft else c["name"]), credit_name, credit_url,
         source, c["id"], craft_id)).lastrowid
    _touch_city(con, c["id"])
    con.commit()
    return mid


def add_upload(con, slug: str, filename: str, data: bytes, craft_id=None) -> dict:
    """Save a photo or video file from the computer into this city."""
    c = _city(con, slug)
    craft_id = _craft_in_city(con, c["id"], craft_id) or (_featured_craft(con, c["id"]) or {"craft_id": None})["craft_id"]
    ext = Path(filename or "").suffix.lower()
    if ext not in UPLOAD_EXT:
        raise ValueError(f"Use a photo or video file ({', '.join(sorted(UPLOAD_EXT))}).")
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_UPLOAD:
        raise ValueError("The file is larger than 300 MB.")
    folder = p.MEDIA_DIR / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    stem = _slugify(Path(filename).stem)[:40]
    name = f"{c['slug']}-{int(time.time() * 1000)}-{stem}{ext}"
    (folder / name).write_bytes(data)
    rel_path = f"uploads/{name}"
    push_media_file(con, rel_path)
    craft = con.execute("SELECT name FROM crafts WHERE id = ?", (craft_id,)).fetchone() if craft_id else None
    con.execute("INSERT INTO media (kind, local_path, alt_text, permission, city_id, craft_id) "
                "VALUES (?, ?, ?, 'not_asked', ?, ?)",
                ("video" if ext in p.VIDEO_EXT else "photo", rel_path,
                 f"{craft['name']} in {c['name']}" if craft else c["name"], c["id"], craft_id))
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


def update_media(con, media_id, data: dict) -> dict:
    m = con.execute("SELECT * FROM media WHERE id = ?", (int(media_id),)).fetchone()
    if not m:
        raise LookupError(f"No photo or video #{media_id}")
    vals = {}
    for k in MEDIA_FIELDS:
        if k in data:
            vals[k] = _text(data[k]) if k != "craft_id" else _craft_in_city(con, m["city_id"], data[k])
    if vals.get("permission") and vals["permission"] not in PERMISSIONS:
        raise ValueError(f"Unknown permission {vals['permission']!r}")
    if "permission" in vals and not vals["permission"]:
        vals["permission"] = "not_asked"
    if vals.get("credit_url") and not re.match(r"https?://", vals["credit_url"], re.I):
        raise ValueError("Credit link must start with http:// or https://")
    if vals:
        sets = ", ".join(f"{k} = ?" for k in vals)
        con.execute(f"UPDATE media SET {sets} WHERE id = ?", [*vals.values(), m["id"]])
        if "craft_id" in vals and m["reel_id"]:
            con.execute("UPDATE reels SET craft_id = ? WHERE id = ?", (vals["craft_id"], m["reel_id"]))
        _touch_city(con, m["city_id"])
        con.commit()
    return media_out(con.execute("SELECT * FROM media WHERE id = ?", (m["id"],)).fetchone())


def delete_media(con, media_id) -> dict:
    """Remove from the city (the file stays in the media folder)."""
    m = con.execute("SELECT * FROM media WHERE id = ?", (int(media_id),)).fetchone()
    if not m:
        raise LookupError(f"No photo or video #{media_id}")
    con.execute("DELETE FROM media WHERE id = ?", (m["id"],))
    if m["reel_id"] and not con.execute("SELECT 1 FROM media WHERE reel_id = ?", (m["reel_id"],)).fetchone():
        # nothing from that post is left in the city: put the reel back in "unassigned"
        con.execute("UPDATE reels SET city_id = NULL, craft_id = NULL WHERE id = ?", (m["reel_id"],))
    con.commit()
    return {"deleted": m["id"]}


# ---------------------------------------------------------------- stores
def save_store(con, slug: str, data: dict) -> dict:
    c = _city(con, slug)
    vals = {}
    for k in STORE_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("lat", "lon", "rating"):
            v = _num(v)
        elif k in ("rating_count", "sort_order"):
            v = _num(v, int)
        elif k == "verified":
            v = 1 if v in (1, True, "1", "true", "on") else 0
        else:
            v = _text(v)
        vals[k] = v
    if "name" in vals and not vals["name"] or (not data.get("id") and not vals.get("name")):
        raise ValueError("Give the shop a name.")
    if vals.get("rating") is not None and not 0 <= vals["rating"] <= 5:
        raise ValueError("Rating must be between 0 and 5.")
    for k in ("website", "maps_url"):
        if vals.get(k) and not re.match(r"https?://", vals[k], re.I):
            raise ValueError(f"{'Website' if k == 'website' else 'Maps link'} must start with http:// or https://")
    if data.get("id"):
        sid = int(data["id"])
        if not con.execute("SELECT 1 FROM stores WHERE id = ? AND city_id = ?", (sid, c["id"])).fetchone():
            raise LookupError(f"No shop #{sid} in {c['name']}")
        if vals:
            sets = ", ".join(f"{k} = ?" for k in vals)
            con.execute(f"UPDATE stores SET {sets} WHERE id = ?", [*vals.values(), sid])
    else:
        if vals.get("google_place_id") and con.execute(
                "SELECT 1 FROM stores WHERE city_id = ? AND google_place_id = ?",
                (c["id"], vals["google_place_id"])).fetchone():
            raise ValueError("This shop is already listed in this city.")
        vals["city_id"] = c["id"]
        vals.setdefault("sort_order", (con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM stores "
                                                   "WHERE city_id = ?", (c["id"],)).fetchone()[0]))
        sid = con.execute(f"INSERT INTO stores ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                          list(vals.values())).lastrowid
    if "craft_ids" in data:
        con.execute("DELETE FROM store_crafts WHERE store_id = ?", (sid,))
        for cid in data["craft_ids"] or []:
            cid = _craft_in_city(con, c["id"], cid)
            con.execute("INSERT OR IGNORE INTO store_crafts (store_id, craft_id) VALUES (?, ?)", (sid, cid))
    _touch_city(con, c["id"])
    con.commit()
    return get_city(con, slug)


def delete_store(con, store_id) -> dict:
    s = con.execute("SELECT * FROM stores WHERE id = ?", (int(store_id),)).fetchone()
    if not s:
        raise LookupError(f"No shop #{store_id}")
    con.execute("DELETE FROM stores WHERE id = ?", (s["id"],))
    _touch_city(con, s["city_id"])
    con.commit()
    return {"deleted": s["id"]}
