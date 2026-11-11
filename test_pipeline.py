"""Offline tests: the downloader is stubbed, so no network or yt-dlp needed.

    python -m unittest test_pipeline -v
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pipeline as p

CITIES = p.load_cities()


def fake_info(caption, location=None, uploader="artisan_handle"):
    info = {"description": caption, "uploader": uploader, "upload_date": "20261101", "duration": 42.0}
    if location:
        info["location"] = {"name": location}
    return info


class LinkTests(unittest.TestCase):
    def test_reel_post_tv_links(self):
        for link in ["https://www.instagram.com/reel/C9xYz12AbC/?igsh=abc",
                     "https://instagram.com/p/C9xYz12AbC/",
                     "https://www.instagram.com/tv/C9xYz12AbC",
                     "https://www.instagram.com/someuser/reel/C9xYz12AbC/"]:
            url, code = p.parse_link(link)
            self.assertEqual(code, "C9xYz12AbC")
            self.assertEqual(url, "https://www.instagram.com/reel/C9xYz12AbC/")

    def test_rejects_other_links(self):
        for bad in ["https://www.instagram.com/someuser/", "https://youtube.com/shorts/abc", "hello"]:
            with self.assertRaises(ValueError):
                p.parse_link(bad)


class ExtractTests(unittest.TestCase):
    def row(self, caption, location=None):
        return p.build_row("u", "code", fake_info(caption, location), CITIES, use_claude=False)

    def test_instagram_tag_wins(self):
        r = self.row("Silver inlay on metal", location="Bidar, Karnataka")
        self.assertEqual((r["city"], r["location_source"]), ("Bidar", "instagram_tag"))
        self.assertEqual(r["lat"], 17.9133)

    def test_city_and_craft_from_caption(self):
        r = self.row("Watch this master at work in Lucknow ✨ #chikankari #handembroidery")
        self.assertEqual((r["city"], r["craft"], r["location_source"]),
                         ("Lucknow", "Chikankari", "caption_match"))

    def test_alias_and_hashtag(self):
        r = self.row("Lost-wax casting from the tribal belt #bastarart #dokra")
        self.assertEqual((r["city"], r["craft"]), ("Jagdalpur", "Dokra"))

    def test_city_specific_craft_first(self):
        r = self.row("Blue pottery studio visit in Jaipur")
        self.assertEqual(r["craft"], "Blue pottery")

    def test_craft_without_city(self):
        r = self.row("Making a bidri vase, start to finish")
        self.assertIsNone(r["city"])
        self.assertEqual(r["craft"], "Bidriware")
        self.assertEqual(r["location_source"], "none")
        self.assertIn("Bidar", r["notes"])

    def test_nothing_found(self):
        r = self.row("Good morning everyone")
        self.assertIsNone(r["city"])
        self.assertIsNone(r["craft"])
        self.assertEqual(r["status"], "needs_review")
        self.assertEqual(r["extracted_by"], "rules")

    def test_no_partial_word_match(self):
        r = self.row("We love embrassing new ideas")  # contains 'brass' inside a word
        self.assertIsNone(r["craft"])

    def test_fields(self):
        r = self.row("Hi #Dokra #Bastar")
        self.assertEqual(r["posted_at"], "2026-11-01")
        self.assertEqual(r["hashtags"], '["bastar", "dokra"]')
        self.assertEqual(r["uploader"], "artisan_handle")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = p.connect(Path(self.tmp.name) / "t.db")
        self.calls = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def stub(self, url, code, with_video):
        self.calls += 1
        return fake_info("Bidriware from Bidar #bidri")

    def test_add_skip_duplicate_and_fix(self):
        link = "https://www.instagram.com/reel/ABC123xyz/"
        rid = p.add(link, con=self.con, downloader=self.stub, use_claude=False)
        self.assertEqual(rid, 1)
        self.assertIsNone(p.add(link + "?igsh=x", con=self.con, downloader=self.stub, use_claude=False))
        self.assertEqual(self.calls, 1)

        p.fix(self.con, rid, city="kutch", craft="Ajrakh", notes="checked with artisan")
        r = self.con.execute("SELECT * FROM reels WHERE id = 1").fetchone()
        self.assertEqual((r["city"], r["state"], r["craft"], r["location_source"], r["status"]),
                         ("Bhuj", "Gujarat", "Ajrakh", "manual", "reviewed"))

    def test_fix_unknown_city_keeps_name(self):
        rid = p.add("https://www.instagram.com/reel/ZZZ/", con=self.con, downloader=self.stub, use_claude=False)
        p.fix(self.con, rid, city="Nirmal", lookup=False)
        r = self.con.execute("SELECT city, lat FROM reels WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((r["city"], r["lat"]), ("Nirmal", None))


# A trimmed real-shaped Nominatim jsonv2 answer
OSM_RAGHURAJPUR = [{
    "osm_type": "node", "osm_id": 123, "lat": "19.8854321", "lon": "85.8277654",
    "addresstype": "village", "name": "Raghurajpur",
    "display_name": "Raghurajpur, Puri Sadar, Puri, Odisha, 752012, India",
    "address": {"village": "Raghurajpur", "county": "Puri Sadar", "state_district": "Puri District",
                "state": "Odisha", "postcode": "752012", "country": "India", "country_code": "in"},
}]


class GeocodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = p.connect(Path(self.tmp.name) / "g.db")
        self.calls = []

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def fetcher(self, params):
        self.calls.append(params)
        return OSM_RAGHURAJPUR if "raghu" in params["q"].lower() else []

    def test_parses_and_caches(self):
        places = p.geocode("  Raghurajpur ", con=self.con, fetcher=self.fetcher)
        pl = places[0]
        self.assertEqual((pl["city"], pl["district"], pl["state"], pl["postcode"], pl["country"]),
                         ("Raghurajpur", "Puri", "Odisha", "752012", "India"))
        self.assertEqual((pl["lat"], pl["lon"]), (19.885432, 85.827765))
        self.assertEqual(pl["osm_url"], "https://www.openstreetmap.org/node/123")
        self.assertEqual(self.calls[0]["countrycodes"], "in")
        p.geocode("raghurajpur", con=self.con, fetcher=self.fetcher)  # cached, case-insensitive
        self.assertEqual(len(self.calls), 1)

    def test_nothing_found(self):
        self.assertEqual(p.geocode("Zzzzz", con=self.con, fetcher=self.fetcher), [])
        self.assertEqual(p.geocode("   ", con=self.con, fetcher=self.fetcher), [])

    def test_fix_uses_lookup_for_unknown_city(self):
        row = p.build_row("https://www.instagram.com/reel/R1/", "R1", fake_info("Pattachitra"),
                          CITIES, use_claude=False, lookup=False)
        rid = p.save(self.con, row)
        look = lambda q: p.geocode(q, con=self.con, fetcher=self.fetcher)[0]
        p.fix(self.con, rid, city="Raghurajpur heritage village", lookup=look)  # not in cities.json
        r = self.con.execute("SELECT * FROM reels WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((r["city"], r["district"], r["postcode"], r["location_source"]),
                         ("Raghurajpur", "Puri", "752012", "lookup"))

    def test_unlisted_instagram_tag_is_looked_up(self):
        look = lambda q: p.geocode(q, con=self.con, fetcher=self.fetcher)[0] if "Raghu" in q else None
        r = p.build_row("u", "c", fake_info("Painting session", location="Raghurajpur Heritage Village"),
                        CITIES, use_claude=False, lookup=look)
        self.assertEqual((r["city"], r["state"], r["location_source"]),
                         ("Raghurajpur", "Odisha", "instagram_tag"))

    def test_listed_city_gets_country(self):
        r = p.build_row("u", "c", fake_info("Bidri from Bidar"), CITIES, use_claude=False, lookup=False)
        self.assertEqual(r["country"], "India")

    def test_old_database_is_upgraded(self):
        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE reels (id INTEGER PRIMARY KEY, shortcode TEXT UNIQUE NOT NULL, "
                    "url TEXT NOT NULL, city TEXT, status TEXT NOT NULL DEFAULT 'needs_review', "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        old.execute("INSERT INTO reels (shortcode, url, created_at, updated_at) VALUES ('A','u','t','t')")
        old.commit(); old.close()
        con = p.connect(path)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(reels)")}
        self.assertTrue({"district", "country", "postcode"} <= cols)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM reels").fetchone()[0], 1)
        con.close()


class ClaudeFallbackTests(unittest.TestCase):
    def test_claude_fills_missing_city_and_summary(self):
        orig = p.summarise_with_claude
        p.summarise_with_claude = lambda c, u: {"summary": "An artisan inlays silver.", "city": "Bidar", "craft": None}
        try:
            r = p.build_row("u", "c", fake_info("An artisan at work"), CITIES)
        finally:
            p.summarise_with_claude = orig
        self.assertEqual((r["city"], r["extracted_by"], r["summary"]),
                         ("Bidar", "claude", "An artisan inlays silver."))

    def test_no_key_means_no_call(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertIsNone(p.summarise_with_claude("caption", "u"))



class YouTubeLinkTests(unittest.TestCase):
    def test_links(self):
        f = p.parse_media_link
        self.assertEqual(f("https://www.youtube.com/shorts/698p33IR-rY?feature=share")["url"],
                         "https://www.youtube.com/shorts/698p33IR-rY")
        w = f("https://www.youtube.com/watch?list=PL1&v=4zRk7GgRCgM&t=30s")
        self.assertEqual((w["platform"], w["id"], w["key"], w["url"], w["file_stem"]),
                         ("youtube", "4zRk7GgRCgM", "yt:4zRk7GgRCgM", "https://www.youtube.com/watch?v=4zRk7GgRCgM",
                          "yt_4zRk7GgRCgM"))
        self.assertEqual(f("https://youtu.be/FL5cXCWSCGQ?si=abc")["id"], "FL5cXCWSCGQ")
        self.assertEqual(f("https://m.youtube.com/watch?v=FL5cXCWSCGQ")["id"], "FL5cXCWSCGQ")
        s = f("https://youtu.be/FL5cXCWSCGQ", "2:15", "3:40")
        self.assertEqual((s["key"], s["url"], s["file_stem"], s["start"], s["end"]),
                         ("yt:FL5cXCWSCGQ@135-220", "https://www.youtube.com/watch?v=FL5cXCWSCGQ&t=135s",
                          "yt_FL5cXCWSCGQ_135-220", 135.0, 220.0))
        self.assertEqual(f("https://www.instagram.com/reel/ABC/")["key"], "ABC")
        for bad in ("https://www.youtube.com/channel/UC123", "https://vimeo.com/1", "https://youtu.be/short"):
            with self.assertRaises(ValueError):
                f(bad)
        for start, end in (("3:00", "2:00"), ("1:00", None), ("0:00", "11:00"), ("1:75", "2:00")):
            with self.assertRaises(ValueError):
                f("https://youtu.be/FL5cXCWSCGQ", start, end)
        self.assertEqual(f("https://youtu.be/FL5cXCWSCGQ", "", "0:45")["key"], "yt:FL5cXCWSCGQ@0-45")

    def test_times(self):
        t = p.parse_time
        self.assertEqual([t("83"), t("83s"), t("1:23"), t("1:02:03"), t("1m23s"), t("1h"), t(""), t(None)],
                         [83, 83, 83, 3723, 83, 3600, None, None])
        self.assertEqual((p.fmt_time(83), p.fmt_time(3723)), ("1:23", "1:02:03"))
        with self.assertRaises(ValueError):
            t("soon")

    def test_long_video_needs_section(self):
        whole = p.parse_media_link("https://youtu.be/FL5cXCWSCGQ")
        p.check_youtube_info({"duration": 14 * 60}, whole)
        with self.assertRaises(p.FetchError) as cm:
            p.check_youtube_info({"duration": 42 * 60 + 5}, whole)
        self.assertIn("42:05 long", str(cm.exception))
        section = p.parse_media_link("https://youtu.be/FL5cXCWSCGQ", "50:00", "52:00")
        with self.assertRaises(p.FetchError):
            p.check_youtube_info({"duration": 42 * 60}, section)
        for info in ({"_type": "playlist"}, {"live_status": "is_live"}):
            with self.assertRaises(p.FetchError):
                p.check_youtube_info(info, whole)

    def test_youtube_errors_get_youtube_hints(self):
        def boom(*a):
            raise Exception("ERROR: [youtube] x: Sign in to confirm you're not a bot")
        with self.assertRaises(p.FetchError) as cm:
            p.fetch_info("u", "k", True, boom, platform="youtube")
        self.assertIn("sign in to YouTube", str(cm.exception))
        self.assertIn("Could not fetch the video", str(cm.exception))
        def fmt(*a):
            raise Exception("ERROR: [youtube] x: Requested format is not available")
        with self.assertRaises(p.FetchError) as cm:
            p.fetch_info("u", "k", True, fmt, platform="youtube")
        self.assertNotIn("Instagram", str(cm.exception))
        self.assertIn('pip install -U "yt-dlp[default]"', str(cm.exception))
        self.assertIn("brew install deno", str(cm.exception))

    def test_cli_add_youtube_section(self):
        with tempfile.TemporaryDirectory() as t:
            saved = p.MEDIA_DIR
            p.MEDIA_DIR = Path(t)
            try:
                con = p.connect(Path(t) / "c.db")
                seen = []
                def yt(link):
                    seen.append(link)
                    (Path(t) / f"{link['file_stem']}.mp4").write_bytes(b"v")
                    return {"info": {"title": "Handmade in Khurja", "description": "Potter at the wheel",
                                     "uploader": "Asia Featured", "duration": 600},
                            "items": [{"kind": "video", "path": f"{link['file_stem']}.mp4"}], "note": ""}
                rid = p.add("https://youtu.be/FL5cXCWSCGQ", con=con, use_claude=False, start="1:00", end="1:30",
                            yt_downloader=yt)
                r = con.execute("SELECT shortcode, url, uploader, video_path FROM reels WHERE id = ?", (rid,)).fetchone()
                self.assertEqual((r["shortcode"], r["uploader"]), ("yt:FL5cXCWSCGQ@60-90", "Asia Featured"))
                self.assertTrue(r["video_path"].endswith("yt_FL5cXCWSCGQ_60-90.mp4"))
                self.assertEqual(seen[0]["start"], 60.0)
                self.assertIsNone(p.add("https://www.youtube.com/watch?v=FL5cXCWSCGQ", con=con, use_claude=False,
                                        start="60", end="90", yt_downloader=yt))  # same section: skipped
                con.close()
            finally:
                p.MEDIA_DIR = saved


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL in tests."""
    formats = []
    fail_with = None
    warn = []
    seen_opts = None

    def __init__(self, opts):
        FakeYDL.seen_opts = opts
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False, process=True):
        for w in FakeYDL.warn:
            self.opts["logger"].warning(w)
        return {"id": "FL5cXCWSCGQ", "title": "Handmade in Khurja", "duration": 600,
                "uploader": "Asia Featured", "formats": FakeYDL.formats}

    def process_ie_result(self, info, download=True):
        if FakeYDL.fail_with:
            raise Exception(FakeYDL.fail_with)
        stem = Path(self.opts["outtmpl"]).name.split(".%(")[0]
        out = Path(self.opts["outtmpl"]).parent / f"{stem}.mp4"
        out.write_bytes(b"v")
        return {**info, "height": 360, "acodec": "mp4a", "requested_downloads": [{"filepath": str(out)}]}


class YouTubeDownloadTests(unittest.TestCase):
    def setUp(self):
        import sys, types
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (p.MEDIA_DIR, sys.modules.get("yt_dlp"))
        p.MEDIA_DIR = Path(self.tmp.name)
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = FakeYDL
        sys.modules["yt_dlp"] = fake
        FakeYDL.formats = [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}]
        FakeYDL.fail_with, FakeYDL.warn = None, []
        self.link = p.parse_media_link("https://youtu.be/FL5cXCWSCGQ?si=PJV2ZOyGlhvRI6oA")

    def tearDown(self):
        import sys
        p.MEDIA_DIR, mod = self.saved
        if mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = mod
        self.tmp.cleanup()

    def test_ok(self):
        res = p.download_youtube(self.link)
        self.assertEqual(res["items"][0]["path"], "yt_FL5cXCWSCGQ.mp4")
        self.assertTrue(FakeYDL.seen_opts["format"].endswith("/bv*"))   # never fails for lack of a format
        self.assertIn("logger", FakeYDL.seen_opts)

    def test_only_storyboards_explains_js_runtime(self):
        FakeYDL.formats = [{"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "protocol": "mhtml"}]
        FakeYDL.warn = ["WARNING: [youtube] No supported JavaScript runtime could be found. "
                        "YouTube extraction without a JS runtime has been deprecated, and some formats may be missing"]
        with self.assertRaises(p.FetchError) as cm:
            p.download_youtube(self.link)
        msg = str(cm.exception)
        self.assertIn("only offered preview images", msg)
        self.assertIn("No supported JavaScript runtime", msg)
        self.assertIn("brew install deno", msg)

    def test_requested_format_error_gets_fix(self):
        FakeYDL.fail_with = "ERROR: [youtube] FL5cXCWSCGQ: Requested format is not available. Use --list-formats"
        with self.assertRaises(p.FetchError) as cm:
            p.download_youtube(self.link)
        self.assertIn('pip install -U "yt-dlp[default]"', str(cm.exception))

    def test_helpers(self):
        self.assertEqual(len(p.usable_formats([
            {"format_id": "sb2", "ext": "mhtml", "vcodec": "none"},
            {"format_id": "140", "ext": "m4a", "vcodec": "none"},
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1"}])), 1)
        self.assertIn("bv*+ba", p.youtube_format(True))
        self.assertNotIn("+", p.youtube_format(False))


if __name__ == "__main__":
    unittest.main()
