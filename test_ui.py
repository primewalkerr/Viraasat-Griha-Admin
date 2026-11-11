"""Offline tests for the city admin server: Instagram, OpenStreetMap and Google
are all stubbed, and everything runs against a temporary database.

    python3 -m unittest test_ui -v
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import pipeline as p
import places
import sitedb
import ui

GOOGLE_PLACE = {
    "id": "ChIJbidri123", "displayName": {"text": "Bidri Craft House"},
    "formattedAddress": "Shop 4, Gawan Chowk, Bidar, Karnataka 585401, India",
    "addressComponents": [{"longText": "585401", "types": ["postal_code"]}],
    "location": {"latitude": 17.9131234, "longitude": 77.5249876},
    "nationalPhoneNumber": "098450 00000", "websiteUri": "https://bidricrafts.example",
    "rating": 4.6, "userRatingCount": 212, "googleMapsUri": "https://maps.google.com/?cid=1",
    "businessStatus": "OPERATIONAL", "primaryTypeDisplayName": {"text": "Handicraft store"},
}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = (p.DB_PATH, p.MEDIA_DIR, ui.POST_DOWNLOADER, ui.PLACES_CALL, ui.PLACES_FOLLOW,
                     ui.PLACES_KEY, ui.GEOCODER)
        p.DB_PATH = Path(cls.tmp.name) / "site.db"
        p.MEDIA_DIR = Path(cls.tmp.name) / "media"
        # Always a throwaway local sqlite file here, never the real Turso
        # database a developer's .env may point at, and never a real upload
        # to a developer's Cloudinary account either.
        cls.saved_turso_url = os.environ.pop("TURSO_DATABASE_URL", None)
        cls.saved_cloudinary = {k: os.environ.pop(k, None) for k in
                                 ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")}
        cls.downloads = 0

        def post(url, code):
            if code.startswith("PHOTO"):
                raise Exception("ERROR: [Instagram] PHOTO: There is no video in this post")
            cls.downloads += 1
            p.MEDIA_DIR.mkdir(exist_ok=True)
            items = []
            for i in (1, 2) if code.startswith("CAR") else (1,):
                (p.MEDIA_DIR / f"{code}_{i}.mp4").write_bytes(bytes(range(256)) * 20)
                (p.MEDIA_DIR / f"{code}_{i}.jpg").write_bytes(b"jpeg")
                items.append({"kind": "video", "path": f"{code}_{i}.mp4", "poster": f"{code}_{i}.jpg",
                              "duration_s": 15, "width": 720, "height": 1280})
            return {"info": {"description": "Silver wire hammered into the groove #bidri",
                             "uploader": "Rashid Ahmed", "channel": "rashid.bidri", "duration": 15},
                    "items": items}

        cls.place_calls = []

        def call(req):
            cls.place_calls.append((req.full_url, req.get_method(), req.data))
            return GOOGLE_PLACE if "/places/" in req.full_url else {"places": [GOOGLE_PLACE]}

        ui.POST_DOWNLOADER = post
        ui.PLACES_CALL = call
        ui.PLACES_FOLLOW = lambda url: "https://www.google.com/maps/place/Bidri+Craft+House/@17.91,77.52,17z/data=!3d17.9131!4d77.5249"
        ui.PLACES_KEY = "test-key"
        ui.GEOCODER = lambda params: [{
            "osm_type": "relation", "osm_id": 9, "lat": "17.91", "lon": "77.52", "addresstype": "city",
            "display_name": "Bidar, Bidar District, Karnataka, India",
            "address": {"city": "Bidar", "state_district": "Bidar District", "state": "Karnataka",
                        "country": "India"}}]

        # an "old" reel saved by the first pipeline, before city pages existed
        con = p.connect()
        row = p.build_row("https://www.instagram.com/reel/OLD1/", "OLD1",
                          {"description": "Lovely work", "uploader": "Jay sharma"},
                          p.load_cities(), use_claude=False, lookup=False)
        p.save(con, row)
        con.close()

        cls.srv = ui.make_server(port=0)
        cls.port = cls.srv.server_address[1]
        ui.PORT = cls.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        (p.DB_PATH, p.MEDIA_DIR, ui.POST_DOWNLOADER, ui.PLACES_CALL, ui.PLACES_FOLLOW,
         ui.PLACES_KEY, ui.GEOCODER) = cls.saved
        if cls.saved_turso_url is not None:
            os.environ["TURSO_DATABASE_URL"] = cls.saved_turso_url
        for k, v in cls.saved_cloudinary.items():
            if v is not None:
                os.environ[k] = v
        cls.tmp.cleanup()

    # ---- helpers
    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read()
                return r.status, dict(r.headers), (json.loads(body) if "json" in r.headers.get("Content-Type", "") else body)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def post(self, path, body=None, raw=None, origin=None):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "Origin": origin or self.base})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def city(self, slug="bidar"):
        return self.get(f"/api/cities/{slug}")[2]

    # ---- tests run in name order
    def test_01_seeded_cities_and_old_reel(self):
        status, _, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Viraasat Admin", page)
        _, _, res = self.get("/api/cities")
        names = [c["name"] for c in res["cities"]]
        self.assertEqual(len(names), 14)
        self.assertEqual(names[0], "Srinagar")
        bidar = next(c for c in res["cities"] if c["slug"] == "bidar")
        self.assertEqual((bidar["pin_number"], bidar["craft_name"], bidar["status"]), (11, "Bidriware", "draft"))
        self.assertEqual([r["shortcode"] for r in res["unassigned"]], ["OLD1"])
        _, _, cfg = self.get("/api/config")
        self.assertTrue(cfg["google_key"])

    def test_02_add_instagram_post(self):
        status, c = self.post("/api/cities/bidar/posts", {"url": "https://www.instagram.com/reel/REEL1/?igsh=x"})
        self.assertEqual(status, 200, c)
        m = c["media"][0]
        self.assertEqual((m["kind"], m["local_path"], m["poster_path"]), ("video", "REEL1_1.mp4", "REEL1_1.jpg"))
        self.assertEqual((m["credit_name"], m["credit_url"], m["permission"]),
                         ("Rashid Ahmed", "https://www.instagram.com/rashid.bidri/", "not_asked"))
        self.assertEqual(m["alt_text"], "Bidriware in Bidar")
        self.assertEqual(m["source_url"], "https://www.instagram.com/reel/REEL1/")
        con = sitedb.connect()
        r = con.execute("SELECT city, city_id, craft, status FROM reels WHERE shortcode='REEL1'").fetchone()
        con.close()
        self.assertEqual((r["city"], r["craft"], r["status"]), ("Bidar", "Bidriware", "reviewed"))

        # the same post again, in another city: refused without downloading
        before = ServerTests.downloads
        status, err = self.post("/api/cities/jaipur/posts", {"url": "https://instagram.com/p/REEL1/"})
        self.assertEqual((status, ServerTests.downloads), (400, before))
        self.assertIn("already in Bidar", err["error"])

        # a carousel gives one item per video
        _, c = self.post("/api/cities/bidar/posts", {"url": "https://www.instagram.com/p/CAR1/"})
        self.assertEqual(sum(1 for m in c["media"] if m["source_url"].endswith("/CAR1/")), 2)

    def test_03_photo_only_post_explains(self):
        status, err = self.post("/api/cities/bidar/posts", {"url": "https://www.instagram.com/p/PHOTO1/"})
        self.assertEqual(status, 400)
        self.assertIn("Upload photo", err["error"])
        status, err = self.post("/api/cities/bidar/posts", {"url": "https://youtube.com/watch?v=1"})
        self.assertEqual(status, 400)

    def test_04_media_edit_hero_and_permission(self):
        c = self.city()
        mid = c["media"][-1]["id"]
        status, m = self.post(f"/api/media/{mid}", {"permission": "granted", "credit_name": "Rashid A.",
                                                     "alt_text": "Hands inlaying silver"})
        self.assertEqual((status, m["permission"], m["credit_name"]), (200, "granted", "Rashid A."))
        self.assertEqual(self.post(f"/api/media/{mid}", {"permission": "maybe"})[0], 400)
        self.assertEqual(self.post(f"/api/media/{mid}", {"credit_url": "instagram.com/x"})[0], 400)
        _, c = self.post("/api/cities/bidar/image", {"media_id": mid, "which": "hero"})
        self.assertEqual(c["city"]["hero_media_id"], mid)
        other = self.city("jaipur")
        self.assertEqual(self.post("/api/cities/jaipur/image", {"media_id": mid, "which": "card"})[0], 400)
        _, _, res = self.get("/api/cities")
        bidar = next(x for x in res["cities"] if x["slug"] == "bidar")
        self.assertEqual((bidar["media_usable"], bidar["image_url"]), (1, "/media/REEL1_1.jpg"))
        self.assertIsNotNone(other)

    def test_05_assign_old_reel_and_remove(self):
        _, _, res = self.get("/api/cities")
        rid = res["unassigned"][0]["id"]
        status, c = self.post(f"/api/reels/{rid}/assign", {"city": "lucknow"})
        self.assertEqual(status, 200)
        self.assertEqual(c["media"][0]["credit_name"], "Jay sharma")
        self.assertTrue(c["media"][0]["file_missing"] or c["media"][0]["local_path"] is None)
        _, _, res = self.get("/api/cities")
        self.assertEqual(res["unassigned"], [])
        # removing the only item puts the reel back in "waiting for a city"
        self.post(f"/api/media/{c['media'][0]['id']}/delete")
        _, _, res = self.get("/api/cities")
        self.assertEqual([r["id"] for r in res["unassigned"]], [rid])

    def test_06_upload_photo_and_serve_it(self):
        status, c = self.post("/api/cities/agra/upload?name=Taj%20inlay.JPG", raw=b"\xff\xd8photo")
        self.assertEqual(status, 200, c)
        m = c["media"][0]
        self.assertEqual(m["kind"], "photo")
        self.assertTrue(m["local_path"].startswith("uploads/agra-") and m["local_path"].endswith("-taj-inlay.jpg"))
        status, h, body = self.get(m["file_url"])
        self.assertEqual((status, h["Content-Type"], body), (200, "image/jpeg", b"\xff\xd8photo"))
        self.assertEqual(self.post("/api/cities/agra/upload?name=notes.txt", raw=b"x")[0], 400)
        # byte ranges for video seeking, and no escaping the media folder
        status, h, body = self.get("/media/REEL1_1.mp4", {"Range": "bytes=0-3"})
        self.assertEqual((status, h["Content-Range"], body), (206, "bytes 0-3/5120", b"\x00\x01\x02\x03"))
        self.assertEqual(self.get("/media/../site.db")[0], 404)
        self.assertEqual(self.get("/media/uploads/../../site.db")[0], 404)

    def test_07_shop_from_google_maps(self):
        status, res = self.post("/api/shops/resolve", {"url": "https://maps.app.goo.gl/AbCdEf123"})
        self.assertEqual(status, 200, res)
        cand = res["candidates"][0]
        self.assertEqual((cand["name"], cand["postcode"], cand["rating"], cand["rating_count"], cand["google_place_id"]),
                         ("Bidri Craft House", "585401", 4.6, 212, "ChIJbidri123"))
        url, method, data = ServerTests.place_calls[-1]
        self.assertEqual(method, "POST")
        body = json.loads(data)
        self.assertEqual(body["textQuery"], "Bidri Craft House")
        self.assertEqual(body["locationBias"]["circle"]["center"], {"latitude": 17.9131, "longitude": 77.5249})
        self.assertEqual(self.post("/api/shops/resolve", {"url": "https://example.com/x"})[0], 400)

        craft = self.city()["crafts"][0]["id"]
        status, c = self.post("/api/cities/bidar/stores", {**cand, "craft_ids": [craft], "verified": True})
        self.assertEqual(status, 200, c)
        s = c["stores"][0]
        self.assertEqual((s["name"], s["verified"], s["craft_ids"], s["rating_source"]),
                         ("Bidri Craft House", 1, [craft], "Google"))
        # same Google place twice is refused; bad rating refused; edit works
        self.assertEqual(self.post("/api/cities/bidar/stores", cand)[0], 400)
        self.assertEqual(self.post("/api/cities/bidar/stores", {"id": s["id"], "rating": 7})[0], 400)
        _, c = self.post("/api/cities/bidar/stores", {"id": s["id"], "phone": "08482 000000", "verified": False})
        self.assertEqual((c["stores"][0]["phone"], c["stores"][0]["verified"]), ("08482 000000", 0))
        # a shop added by hand, then deleted
        _, c = self.post("/api/cities/bidar/stores", {"name": "Fort Road Emporium"})
        sid = next(x["id"] for x in c["stores"] if x["name"] == "Fort Road Emporium")
        self.assertEqual(self.post(f"/api/stores/{sid}/delete")[0], 200)
        self.assertEqual(len(self.city()["stores"]), 1)

    def test_08_story_and_crafts(self):
        c = self.city("jagdalpur")
        craft = c["crafts"][0]["id"]
        paras = [
            {"body": "Dokra is cast by the lost-wax method.", "fact_status": "checked",
             "sources": [{"title": "Crafts of India", "publisher": "Book Co", "url": "https://a.example/1"},
                         {"title": "Museum note", "url": "https://b.example/2", "accessed_on": "2026-11-01"}]},
            {"body": "Bastar artisans add wax threads.", "fact_status": "unchecked", "sources": []},
            {"body": "   ", "fact_status": "unchecked", "sources": []},
        ]
        status, c = self.post("/api/cities/jagdalpur/story", {"craft_id": craft, "paragraphs": paras})
        self.assertEqual(status, 200, c)
        story = c["crafts"][0]["story"]
        self.assertEqual([s["position"] for s in story], [1, 2])
        self.assertEqual(len(story[0]["sources"]), 2)
        bad = [{"body": "x", "fact_status": "checked", "sources": [{"title": "one"}]}]
        status, err = self.post("/api/cities/jagdalpur/story", {"craft_id": craft, "paragraphs": bad})
        self.assertEqual(status, 400)
        self.assertIn("two published sources", err["error"])
        self.assertEqual(len(self.city("jagdalpur")["crafts"][0]["story"]), 2)  # unchanged

        # edit the craft; GI year sanity
        status, c = self.post("/api/cities/jagdalpur/crafts",
                              {"id": craft, "materials": "brass, beeswax, clay", "gi_tag": True, "gi_year": 2008,
                               "keywords": "dokra, dhokra"})
        self.assertEqual(c["crafts"][0]["materials"], "brass, beeswax, clay")
        self.assertEqual(c["crafts"][0]["keywords"], ["dokra", "dhokra"])
        self.assertEqual(self.post("/api/cities/jagdalpur/crafts", {"id": craft, "gi_tag": True, "gi_year": 1850})[0], 400)

        # a new craft, made featured, then removed; featured falls back
        _, c = self.post("/api/cities/jagdalpur/crafts", {"name": "Bell metal lamps", "featured": True})
        new = next(x for x in c["crafts"] if x["name"] == "Bell metal lamps")
        self.assertTrue(new["is_featured"])
        _, _, res = self.get("/api/cities")
        self.assertEqual(next(x for x in res["cities"] if x["slug"] == "jagdalpur")["craft_name"], "Bell metal lamps")
        _, c = self.post(f"/api/cities/jagdalpur/crafts/{new['id']}/unlink")
        self.assertEqual([x["name"] for x in c["crafts"] if x["is_featured"]], ["Dokra"])
        self.assertEqual(self.post(f"/api/cities/jagdalpur/crafts/{craft}/unlink")[0], 400)  # last craft

        # an existing craft shared with another city
        _, c = self.post("/api/cities/puri/crafts", {"existing_id": craft})
        self.assertIn("Dokra", [x["name"] for x in c["crafts"]])
        self.assertIn("Jagdalpur", next(x for x in c["crafts"] if x["name"] == "Dokra")["also_in"])

    def test_09_city_details_and_lookup(self):
        _, places_ = self.get("/api/geocode?q=Bidar")[0:3:2]
        self.assertEqual(places_[0]["district"], "Bidar")
        status, c = self.post("/api/cities/bidar", {"district": "Bidar", "aliases": "bidar fort, ", "status": "in_review",
                                                    "tagline": "Silver on black"})
        self.assertEqual((c["city"]["district"], c["city"]["aliases"], c["city"]["status"]),
                         ("Bidar", ["bidar fort"], "in_review"))
        self.assertEqual(self.post("/api/cities/bidar", {"status": "live"})[0], 400)
        self.assertEqual(self.post("/api/cities/bidar", {"lat": "200"})[0], 400)
        self.assertEqual(self.get("/api/cities/atlantis")[0], 404)

    def test_10_other_sites_cannot_write(self):
        status, _ = self.post("/api/cities/bidar", {"status": "hidden"}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.city()["city"]["status"], "in_review")


    def test_11_add_city(self):
        _, _, res = self.get("/api/cities")
        self.assertTrue(res["crafts"] and res["categories"])
        status, c = self.post("/api/cities", {"name": "Khajuraho", "name_hi": "खजुराहो", "state": "Madhya Pradesh",
                                              "district": "Chhatarpur", "lat": "24.85", "lon": "79.93",
                                              "craft_name": "Stone carving", "aliases": "khajraho"})
        self.assertEqual(status, 200, c)
        self.assertEqual((c["city"]["slug"], c["city"]["pin_number"], c["city"]["status"], c["city"]["aliases"]),
                         ("khajuraho", 15, "draft", ["khajraho"]))
        self.assertEqual([(x["name"], x["is_featured"]) for x in c["crafts"]], [("Stone carving", 1)])
        _, _, res = self.get("/api/cities")
        self.assertEqual(res["cities"][-1]["name"], "Khajuraho")
        # an existing craft, no craft, duplicates and missing state
        _, c = self.post("/api/cities", {"name": "Kondagaon", "state": "Chhattisgarh",
                                         "craft_id": next(x["id"] for x in res["crafts"] if x["name"] == "Dokra")})
        self.assertIn("Jagdalpur", c["crafts"][0]["also_in"])
        _, c = self.post("/api/cities", {"name": "Pochampally", "state": "Telangana"})
        self.assertEqual(c["crafts"], [])
        self.assertEqual(self.post("/api/cities", {"name": "khajuraho", "state": "MP"})[0], 400)
        self.assertEqual(self.post("/api/cities", {"name": "Hampi"})[0], 400)
        self.assertEqual(self.post("/api/cities", {"name": "Hampi", "state": "Karnataka", "pin_number": 1})[0], 400)
        self.assertEqual(self.get("/api/cities/pochampally")[0], 200)


    def test_12_delete_city(self):
        # Khajuraho (from test_11): give it a reel, a shop and a story
        _, c = self.post("/api/cities/khajuraho/posts", {"url": "https://www.instagram.com/reel/KHAJ1/"})
        self.post("/api/cities/khajuraho/image", {"media_id": c["media"][0]["id"], "which": "hero"})
        self.post("/api/cities/khajuraho/stores", {"name": "Temple Road Crafts"})
        craft = c["crafts"][0]["id"]
        self.post("/api/cities/khajuraho/story", {"craft_id": craft, "paragraphs": [
            {"body": "Sandstone carving.", "sources": [{"title": "Only here", "url": "https://k.example"}]}]})

        status, err = self.post("/api/cities/khajuraho/delete", {"confirm_name": "Khaju"})
        self.assertEqual(status, 400)
        self.assertIn("Type the city's name", err["error"])
        status, res = self.post("/api/cities/khajuraho/delete", {"confirm_name": " khajuraho "})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["media"], res["shops"], res["paragraphs"], res["crafts_removed"]),
                         (1, 1, 1, ["Stone carving"]))
        self.assertEqual(self.get("/api/cities/khajuraho")[0], 404)
        _, _, cities = self.get("/api/cities")
        self.assertIn("KHAJ1", [r["shortcode"] for r in cities["unassigned"]])  # reel kept, waiting
        self.assertNotIn("Stone carving", [x["name"] for x in cities["crafts"]])
        self.assertTrue((p.MEDIA_DIR / "KHAJ1_1.mp4").is_file())                 # file kept
        con = sitedb.connect()
        self.assertIsNone(con.execute("SELECT 1 FROM sources WHERE title = 'Only here'").fetchone())
        con.close()

        # a shared craft survives when one of its cities is deleted
        _, res = self.post("/api/cities/kondagaon/delete", {"confirm_name": "Kondagaon"})
        self.assertEqual(res["crafts_removed"], [])
        self.assertIn("Dokra", [x["name"] for x in self.get("/api/cities")[2]["crafts"]])


    def test_13_youtube(self):
        calls = []

        def yt(link):
            calls.append(link)
            p.MEDIA_DIR.mkdir(exist_ok=True)
            (p.MEDIA_DIR / f"{link['file_stem']}.mp4").write_bytes(b"mp4")
            (p.MEDIA_DIR / f"{link['file_stem']}.webp").write_bytes(b"webp")
            return {"info": {"title": "Handmade in Khurja", "description": "A potter shapes a vessel.",
                             "uploader": "Asia Featured", "uploader_id": "@asiafeatured", "duration": 1500},
                    "items": [{"kind": "video", "path": f"{link['file_stem']}.mp4", "poster": f"{link['file_stem']}.webp",
                               "duration_s": (link["end"] - link["start"]) if link["start"] is not None else 1500}],
                    "note": "Saved at 360p because ffmpeg isn't installed."}

        saved, ui.YT_DOWNLOADER = ui.YT_DOWNLOADER, yt
        try:
            status, c = self.post("/api/cities/khurja/posts",
                                  {"url": "https://www.youtube.com/watch?v=FL5cXCWSCGQ", "start": "2:15", "end": "3:40"})
            self.assertEqual(status, 200, c)
            m = next(x for x in c["media"] if x["source_url"].startswith("https://www.youtube.com/"))
            self.assertEqual((m["source_url"], m["local_path"], m["poster_path"], m["duration_s"]),
                             ("https://www.youtube.com/watch?v=FL5cXCWSCGQ&t=135s", "yt_FL5cXCWSCGQ_135-220.mp4",
                              "yt_FL5cXCWSCGQ_135-220.webp", 85))
            self.assertEqual((m["credit_name"], m["credit_url"]), ("Asia Featured", "https://www.youtube.com/@asiafeatured"))
            self.assertTrue(m["caption"].startswith("Handmade in Khurja"))
            self.assertIn("360p", c["note"])

            # another part of the same video is fine; the same part again is not
            status, c = self.post("/api/cities/khurja/posts",
                                  {"url": "https://youtu.be/FL5cXCWSCGQ", "start": "5:00", "end": "5:30"})
            self.assertEqual(status, 200, c)
            before = len(calls)
            status, err = self.post("/api/cities/agra/posts",
                                    {"url": "https://youtu.be/FL5cXCWSCGQ", "start": "135", "end": "220"})
            self.assertEqual((status, len(calls)), (400, before))
            self.assertIn("video section is already in Khurja", err["error"])

            # a Short, whole
            status, c = self.post("/api/cities/khurja/posts", {"url": "https://www.youtube.com/shorts/698p33IR-rY"})
            self.assertEqual(status, 200, c)
            self.assertIn("https://www.youtube.com/shorts/698p33IR-rY", [x["source_url"] for x in c["media"]])
            self.assertIsNone(calls[-1]["start"])

            # bad times are explained without downloading
            status, err = self.post("/api/cities/khurja/posts",
                                    {"url": "https://youtu.be/FL5cXCWSCGQ", "start": "9:00", "end": "8:00"})
            self.assertEqual(status, 400)
            self.assertIn("To time must be after", err["error"])
            _, _, cfg = self.get("/api/config")
            self.assertIn("ffmpeg", cfg)
        finally:
            ui.YT_DOWNLOADER = saved


class PlacesLinkTests(unittest.TestCase):
    def test_link_formats(self):
        f = places.parse_maps_url
        a = f("https://www.google.com/maps/place/Bidri+Craft+House/@17.91,77.52,17z/data=!4m6!3m5!1s0x0:0x1!8m2!3d17.9131!4d77.5249")
        self.assertEqual((a["name"], a["lat"], a["lon"]), ("Bidri Craft House", 17.9131, 77.5249))
        b = f("https://www.google.com/maps/search/?api=1&query=Chikan+Emporium&query_place_id=ChIJabcdefghijklmnopqrst")
        self.assertEqual((b["name"], b["google_place_id"]), ("Chikan Emporium", "ChIJabcdefghijklmnopqrst"))
        c = f("https://maps.google.com/?q=26.85,80.94")
        self.assertEqual((c["name"], c["lat"]), (None, 26.85))
        d = f("https://www.google.com/maps/place/Ch%C3%A2teau%20Crafts/@1,2,3z")
        self.assertEqual(d["name"], "Château Crafts")

    def test_without_key_uses_link_only(self):
        res = places.shop_from_link("https://maps.app.goo.gl/x", key=None,
                                    follow=lambda u: "https://www.google.com/maps/place/Kashi+Silk/@25.3,82.9,15z")
        self.assertEqual(res["candidates"][0]["name"], "Kashi Silk")
        self.assertIn("No Google API key", res["note"])

    def test_api_error_is_readable(self):
        def call(req):
            raise places.PlacesError("Google Places said: API key not valid (HTTP 400). Check that GOOGLE_MAPS_API_KEY is right")
        res = places.shop_from_link("https://www.google.com/maps/place/Shop+A/@1.0,2.0,15z", key="bad", call=call)
        self.assertIn("API key not valid", res["note"])
        self.assertEqual(res["candidates"][0]["name"], "Shop A")


class UpgradeTests(unittest.TestCase):
    def test_existing_database_keeps_its_reels(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE reels (id INTEGER PRIMARY KEY AUTOINCREMENT, shortcode TEXT UNIQUE NOT NULL, "
                        "url TEXT NOT NULL, uploader TEXT, caption TEXT, hashtags TEXT, posted_at TEXT, duration_s REAL, "
                        "video_path TEXT, location_raw TEXT, city TEXT, state TEXT, lat REAL, lon REAL, "
                        "location_source TEXT, craft TEXT, summary TEXT, extracted_by TEXT, "
                        "status TEXT NOT NULL DEFAULT 'needs_review', notes TEXT, created_at TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL)")
            con.execute("INSERT INTO reels (shortcode, url, created_at, updated_at) VALUES ('X', 'u', 't', 't')")
            con.commit()
            con.close()
            con = sitedb.connect(path)
            self.assertEqual(len(sitedb.unassigned_reels(con)), 1)
            self.assertEqual(len(sitedb.list_cities(con)), 14)
            con.close()
            con = sitedb.connect(path)  # second start: no duplicate seed
            self.assertEqual(con.execute("SELECT COUNT(*) FROM cities").fetchone()[0], 14)
            con.close()


if __name__ == "__main__":
    unittest.main()
