"""Shops from Google Maps links.

1. resolve_link(): follow a share link (maps.app.goo.gl/...) to the full Maps URL
   and read the place name and coordinates out of it. Free, no key.
2. lookup(): ask the Google Places API (New) for name, address, PIN, phone,
   website and rating. Needs GOOGLE_MAPS_API_KEY (in .env or the shell) with the
   "Places API (New)" enabled in Google Cloud.

Google's terms let you keep the place ID indefinitely, but not copies of the
other details; refresh them from the API (google_place_id) before publishing.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pipeline  # noqa: F401  (loads .env and trusts the system certificates)

PLACES_SEARCH = "https://places.googleapis.com/v1/places:searchText"
PLACES_GET = "https://places.googleapis.com/v1/places/"
FIELDS = ["id", "displayName", "formattedAddress", "addressComponents", "location",
          "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri", "rating",
          "userRatingCount", "googleMapsUri", "businessStatus", "primaryTypeDisplayName"]
MAPS_HOSTS = re.compile(r"(^|\.)(google\.[a-z.]+|goo\.gl|maps\.app\.goo\.gl|g\.co)$", re.I)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ViraasatAdmin/1.0"


class PlacesError(Exception):
    """A shop lookup failed; the message is safe to show the user."""


def api_key() -> str | None:
    return os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_PLACES_API_KEY")


# ------------------------------------------------------------ link parsing
def _follow(url: str) -> str:
    """Final URL after redirects (short links, consent pages)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        final = e.geturl() or url
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PlacesError(f"Could not open the Maps link ({e}).") from e
    q = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
    if "consent." in final and q.get("continue"):
        final = q["continue"][0]
    return final


def parse_maps_url(url: str) -> dict:
    """Name, coordinates and place ID, as far as the URL itself says."""
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    path = urllib.parse.unquote(u.path)
    out = {"maps_url": url, "name": None, "lat": None, "lon": None, "google_place_id": None}

    m = re.search(r"/maps/place/([^/]+)", path)
    if m and not re.fullmatch(r"-?\d+\.\d+,\s*-?\d+\.\d+", m.group(1)):
        out["name"] = urllib.parse.unquote_plus(m.group(1)).strip()
    for key in ("q", "query", "destination"):
        if not out["name"] and q.get(key) and not re.fullmatch(r"-?\d+\.\d+,\s*-?\d+\.\d+", q[key][0]):
            out["name"] = q[key][0].strip()

    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url) or re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if not m:
        for key in ("q", "query", "ll", "center"):
            if q.get(key):
                m = re.fullmatch(r"(-?\d+\.\d+),\s*(-?\d+\.\d+)", q[key][0])
                if m:
                    break
    if m:
        out["lat"], out["lon"] = round(float(m.group(1)), 6), round(float(m.group(2)), 6)

    pid = (q.get("query_place_id") or q.get("place_id") or [None])[0]
    if not pid:
        m = re.search(r"place_id:([A-Za-z0-9_-]{20,})", url)
        pid = m.group(1) if m else None
    out["google_place_id"] = pid
    return out


def resolve_link(link: str, follow=None) -> dict:
    link = (link or "").strip()
    u = urllib.parse.urlparse(link)
    if u.scheme not in ("http", "https") or not MAPS_HOSTS.search(u.hostname or ""):
        raise ValueError("Paste a Google Maps link (google.com/maps/... or maps.app.goo.gl/...).")
    final = link
    if not re.search(r"/maps/(place|search|dir)|[?&](q|query|query_place_id)=", link):
        final = (follow or _follow)(link)
    info = parse_maps_url(final)
    info["maps_url"] = link if "goo.gl" in (u.hostname or "") else final
    return info


# ------------------------------------------------------------- Places API
def _call(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        hint = ""
        if e.code in (400, 403) and re.search(r"key|not been used|disabled|billing|denied", msg, re.I):
            hint = (" Check that GOOGLE_MAPS_API_KEY is right, that 'Places API (New)' is enabled "
                    "for its project, and that billing is set up in Google Cloud.")
        raise PlacesError(f"Google Places said: {msg or e.reason} (HTTP {e.code}).{hint}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PlacesError(f"Could not reach Google Places ({e}).") from e


def _store_from_place(pl: dict) -> dict:
    comps = pl.get("addressComponents") or []
    postcode = next((c.get("longText") for c in comps if "postal_code" in (c.get("types") or [])), None)
    loc = pl.get("location") or {}
    return {
        "name": (pl.get("displayName") or {}).get("text"),
        "address": pl.get("formattedAddress"),
        "postcode": postcode,
        "lat": round(loc["latitude"], 6) if "latitude" in loc else None,
        "lon": round(loc["longitude"], 6) if "longitude" in loc else None,
        "phone": pl.get("nationalPhoneNumber") or pl.get("internationalPhoneNumber"),
        "website": pl.get("websiteUri"),
        "maps_url": pl.get("googleMapsUri"),
        "google_place_id": pl.get("id"),
        "rating": pl.get("rating"),
        "rating_count": pl.get("userRatingCount"),
        "rating_source": "Google" if pl.get("rating") is not None else None,
        "rating_checked_at": date.today().isoformat() if pl.get("rating") is not None else None,
        "business_status": pl.get("businessStatus"),
        "kind": (pl.get("primaryTypeDisplayName") or {}).get("text"),
    }


def lookup(info: dict, key: str | None = None, call=None) -> list[dict]:
    """Candidates from Google Places for a parsed link, best first."""
    key = key or api_key()
    if not key:
        raise PlacesError("No Google API key. Add GOOGLE_MAPS_API_KEY=... to the .env file "
                          "in the viraasat-pipeline folder and restart ui.py.")
    call = call or _call
    headers = {"X-Goog-Api-Key": key, "Content-Type": "application/json"}
    if info.get("google_place_id"):
        req = urllib.request.Request(
            PLACES_GET + urllib.parse.quote(info["google_place_id"]) + "?languageCode=en",
            headers={**headers, "X-Goog-FieldMask": ",".join(FIELDS)})
        return [_store_from_place(call(req))]
    if not info.get("name"):
        raise PlacesError("The link doesn't name a place. In Google Maps, open the shop itself, "
                          "press Share, and paste that link.")
    body = {"textQuery": info["name"], "languageCode": "en", "regionCode": "IN", "maxResultCount": 5}
    if info.get("lat") is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": info["lat"], "longitude": info["lon"]},
                                           "radius": 500.0}}
    req = urllib.request.Request(PLACES_SEARCH, data=json.dumps(body).encode(), method="POST",
                                 headers={**headers, "X-Goog-FieldMask": ",".join("places." + f for f in FIELDS)})
    return [_store_from_place(pl) for pl in call(req).get("places", [])]


def shop_from_link(link: str, follow=None, call=None, key=None) -> dict:
    """Everything the page needs for 'Add shop': {link_info, candidates, note}."""
    info = resolve_link(link, follow)
    try:
        cands = lookup(info, key=key, call=call)
        note = None if cands else "Google found no matching place; check the details below."
    except PlacesError as e:
        if "No Google API key" not in str(e) and not (info.get("name") or info.get("lat")):
            raise
        cands, note = [], str(e)
    if not cands:  # fall back to what the link itself says
        cands = [{"name": info["name"], "lat": info["lat"], "lon": info["lon"],
                  "maps_url": info["maps_url"], "google_place_id": info["google_place_id"]}]
    for c in cands:
        c.setdefault("maps_url", info["maps_url"])
        c["maps_url"] = c.get("maps_url") or info["maps_url"]
    return {"link": info, "candidates": cands, "note": note}
