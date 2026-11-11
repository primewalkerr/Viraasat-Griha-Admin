"""Offline tests for igphotos.py and import_picks.py (Instagram stubbed).

    python3 -m unittest test_import -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import igphotos
import import_picks
import pipeline as p
import sitedb

IMG = "https://scontent.cdninstagram.com/v/t51/{}.jpg?stp=dst-jpg&_nc_ht=x&oh=1"


def embed_with_context(n):
    media = {"shortcode_media": {"__typename": "GraphSidecar", "display_url": IMG.format(1), "is_video": False,
             "edge_sidecar_to_children": {"edges": [
                 {"node": {"display_url": IMG.format(i), "is_video": i == 3}} for i in range(1, n + 1)]}}}
    ctx = json.dumps({"context": {"x": 1}, "gql_data": media})
    return ('<html><script>window.__additionalDataLoaded("extra",{"contextJSON":'
            + json.dumps(ctx) + '});</script></html>')


class ParseEmbedTests(unittest.TestCase):
    def test_context_json_carousel(self):
        urls = igphotos.parse_embed(embed_with_context(4))
        self.assertEqual([u.split("/")[-1].split(".")[0] for u in urls], ["1", "2", "4"])  # photo slides only
        self.assertIn("&_nc_ht=x", urls[0])
        slides = igphotos.parse_slides(embed_with_context(4))
        self.assertEqual([vid for _, vid in slides], [False, False, True, False])  # numbering kept

    def test_video_slide_keeps_numbering(self):
        def get(url):
            if "/embed/" in url:
                return embed_with_context(4).encode(), "text/html"
            return b"JPEG:" + url.split("/")[-1].split(".")[0].encode(), "image/jpeg"
        self.assertEqual(igphotos.fetch_slide("X", 4, get=get)[0], b"JPEG:4")
        data, _, note = igphotos.fetch_slide("X", 3, get=get)
        self.assertEqual((data, note), (b"JPEG:3", "slide 3 is a video, saved its cover image"))

    def test_escaped_display_url(self):
        page = r'<script>{\"display_url\":\"https:\\/\\/scontent.x\\/a.jpg?x=1\\u0026y=2\"}</script>'
        self.assertEqual(igphotos.parse_embed(page), ["https://scontent.x/a.jpg?x=1&y=2"])

    def test_embedded_media_image(self):
        page = '<img class="EmbeddedMediaImage" alt="x" src="https://scontent.x/b.jpg?a=1&amp;b=2">'
        self.assertEqual(igphotos.parse_embed(page), ["https://scontent.x/b.jpg?a=1&b=2"])

    def test_nothing(self):
        self.assertEqual(igphotos.parse_embed("<html>Sorry, this page isn't available.</html>"), [])

    def test_fetch_slide_and_fallbacks(self):
        def get(url):
            if "/embed/" in url:
                return embed_with_context(2).encode(), "text/html"
            return b"JPEG:" + url.split("/")[-1].split(".")[0].encode(), "image/jpeg"
        self.assertEqual(igphotos.fetch_slide("X", 2, get=get)[:2], (b"JPEG:2", ".jpg"))
        data, _, note = igphotos.fetch_slide("X", 6, get=get)
        self.assertEqual((data, note), (b"JPEG:1", "only 2 slide(s) found, took slide 1"))
        # embed empty -> gallery-dl; both failing -> one readable error
        empty = lambda url: (b"<html></html>", "text/html")
        self.assertEqual(igphotos.fetch_slide("X", 1, get=empty, gallery=lambda sc: [(b"G1", ".png")])[:2],
                         (b"G1", ".png"))
        def no_gallery(sc):
            raise igphotos.PhotoError("gallery-dl is not installed")
        with self.assertRaises(igphotos.PhotoError) as cm:
            igphotos.fetch_slide("X", 1, get=empty, gallery=no_gallery)
        self.assertIn("embed page had no images", str(cm.exception))
        self.assertIn("gallery-dl is not installed", str(cm.exception))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (p.DB_PATH, p.MEDIA_DIR)
        p.DB_PATH = Path(self.tmp.name) / "t.db"
        p.MEDIA_DIR = Path(self.tmp.name) / "media"
        # These tests always want a throwaway local sqlite file, never the real
        # Turso database a developer's .env may point at, and never a real
        # upload to a developer's Cloudinary account either.
        self.saved_turso_url = os.environ.pop("TURSO_DATABASE_URL", None)
        self.saved_cloudinary = {k: os.environ.pop(k, None) for k in
                                  ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")}
        self.picks = Path(self.tmp.name) / "khurja.json"
        real = json.loads((p.HERE / "picks" / "khurja.json").read_text())
        self.picks.write_text(json.dumps(real))
        self.real = real
        self.lines = []

    def tearDown(self):
        p.DB_PATH, p.MEDIA_DIR = self.saved
        if self.saved_turso_url is not None:
            os.environ["TURSO_DATABASE_URL"] = self.saved_turso_url
        for k, v in self.saved_cloudinary.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def downloader(self, url, code):
        if code == "BhLztC-Fb2B":
            raise Exception("ERROR: [Instagram] BhLztC-Fb2B: Requested content is not available, login required")
        p.MEDIA_DIR.mkdir(exist_ok=True)
        (p.MEDIA_DIR / f"{code}_1.mp4").write_bytes(b"mp4")
        return {"info": {"description": "Khurja #khurjapottery", "uploader": "Maker", "channel": "maker.handle"},
                "items": [{"kind": "video", "path": f"{code}_1.mp4", "poster": None}]}

    def get(self, url):
        if "/embed/" in url:
            if "DfRdbtqk4au" in url:
                return b"<html>removed</html>", "text/html"
            return embed_with_context(6).encode(), "text/html"
        return b"\xff\xd8" + url.split("/")[-1].split(".")[0].encode(), "image/jpeg"

    def no_gallery(self, sc):
        raise igphotos.PhotoError("gallery-dl is not installed")

    def run_import(self):
        self.lines = []
        return import_picks.run(self.picks, downloader=self.downloader, get=self.get, gallery=self.no_gallery,
                                out=self.lines.append)

    def test_import_khurja(self):
        self.assertEqual((len(self.real["videos"]), len(self.real["photos"])), (5, 5))
        res = self.run_import()
        self.assertEqual((len(res["added"]), len(res["skipped"]), len(res["failed"])), (8, 0, 2))
        self.assertIn("login", dict(res["failed"])["https://www.instagram.com/reel/BhLztC-Fb2B/"])

        con = sitedb.connect()
        city = sitedb.get_city(con, "khurja")
        con.close()
        vids = [m for m in city["media"] if m["kind"] == "video"]
        photos = {m["source_url"]: m for m in city["media"] if m["kind"] == "photo"}
        self.assertEqual(len(vids), 4)
        painting = next(m for m in vids if "Db7VRDIphdu" in m["source_url"])
        self.assertEqual(painting["alt_text"], "Khurja artisan brush-painting a single pot, close-up")
        self.assertEqual(painting["permission"], "not_asked")
        self.assertEqual(len(photos), 4)
        mug = photos["https://www.instagram.com/p/DAOB8IbyIO8/"]
        self.assertEqual((mug["local_path"], mug["credit_name"], mug["credit_url"]),
                         ("DAOB8IbyIO8_photo2.jpg", "@authindia", "https://www.instagram.com/authindia/"))
        self.assertEqual((p.MEDIA_DIR / "DAOB8IbyIO8_photo2.jpg").read_bytes(), b"\xff\xd82")  # slide 2
        self.assertEqual((p.MEDIA_DIR / "DXcT8p_mbSt_photo6.jpg").read_bytes(), b"\xff\xd86")  # slide 6
        self.assertEqual(photos["https://www.instagram.com/p/DaQDe94S6IY/"]["credit_name"],
                         "@upculturedept (UP Culture Dept)")
        self.assertTrue(any("Upload photo or video" in line for line in self.lines))

        # running again skips what's there and retries only the failures
        res = self.run_import()
        self.assertEqual((len(res["added"]), len(res["skipped"]), len(res["failed"])), (0, 8, 2))

    def test_youtube_picks(self):
        data = json.loads(self.picks.read_text())
        data["videos"] = [
            {"url": "https://www.youtube.com/watch?v=4zRk7GgRCgM", "from": "1:10", "to": "1:40", "alt": "One painter"},
            {"url": "https://www.youtube.com/shorts/698p33IR-rY", "alt": "Pot painting short"},
            {"url": "https://youtu.be/4zRk7GgRCgM", "from": "2:00", "to": "1:00"},
        ]
        data["photos"] = []
        self.picks.write_text(json.dumps(data))
        def yt(link):
            p.MEDIA_DIR.mkdir(exist_ok=True)
            (p.MEDIA_DIR / f"{link['file_stem']}.mp4").write_bytes(b"v")
            return {"info": {"title": "Khurja", "uploader": "Khurja Pottery Hub", "channel_url": "https://www.youtube.com/@kph"},
                    "items": [{"kind": "video", "path": f"{link['file_stem']}.mp4"}], "note": ""}
        self.lines = []
        res = import_picks.run(self.picks, yt_downloader=yt, out=self.lines.append)
        self.assertEqual((len(res["added"]), len(res["failed"])), (2, 1))
        self.assertIn("To time must be after", res["failed"][0][1])
        con = sitedb.connect()
        rows = con.execute("SELECT source_url, alt_text, credit_url FROM media ORDER BY id").fetchall()
        con.close()
        self.assertEqual([tuple(r) for r in rows], [
            ("https://www.youtube.com/watch?v=4zRk7GgRCgM&t=70s", "One painter", "https://www.youtube.com/@kph"),
            ("https://www.youtube.com/shorts/698p33IR-rY", "Pot painting short", "https://www.youtube.com/@kph")])
        res = import_picks.run(self.picks, yt_downloader=yt, out=self.lines.append)
        self.assertEqual(len(res["skipped"]), 2)

    def test_unknown_city(self):
        data = json.loads(self.picks.read_text())
        data["city"] = "atlantis"
        self.picks.write_text(json.dumps(data))
        with self.assertRaises(LookupError):
            self.run_import()


if __name__ == "__main__":
    unittest.main()
