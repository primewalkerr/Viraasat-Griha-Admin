# Viraasat admin

A local web page for building the Viraasat Griha city pages. It reads and
writes the live Turso database the deployed site also uses — a save here
shows up on the real site immediately. Photos and videos are kept both in the
`media/` folder (for local preview) and as blobs in Turso's `media_files`
table (what the deployed site actually serves — nothing here reaches it over
local disk).

## Start

    python3 -m pip install yt-dlp truststore libsql      # once
    python3 ui.py

Your browser opens http://127.0.0.1:8765. Stop it with Ctrl+C in Terminal.
After updating the code, restart it. If the page says its server doesn't match,
an older copy is still running: `pkill -f ui.py`, then `python3 ui.py`.

## The .env file

Create a file named `.env` in this folder (see `.env.example`):

    TURSO_DATABASE_URL=libsql://...
    TURSO_AUTH_TOKEN=...
    GOOGLE_MAPS_API_KEY=your-key-here
    ANTHROPIC_API_KEY=your-key-here

- **Turso URL/token**: required — same values as `viraasat-site/.env.local`.
  Without these it falls back to a local `viraasat.db` file, which nothing
  deployed reads.
- **Google Maps key** (optional): fills shop address, phone, website and
  rating from a Maps link. In Google Cloud, enable **Places API (New)** and
  turn on billing (there is a monthly free allowance). Without it, shops get
  name and location from the link only.
- **Anthropic key** (optional): writes a short summary of each Instagram caption.

## How it works

**Cities page.** Your 14 cities, each showing what's done: photos and videos (and
how many are cleared for use), shops (and how many are verified), and the story
(and how many paragraphs are checked). Reels saved before the city pages existed
appear at the top; pick a city for each.

**A city page** has four tabs:

1. **Photos & videos** has three sections:
   - **Instagram**: paste a reel or post link. Every video in it is downloaded with
     its poster image and the creator's name and profile as credit. Photo-only posts
     can't be downloaded here; save the photo and upload it, or use the batch importer.
   - **YouTube**: paste a video or Shorts link. Shorts and videos up to 15 minutes
     download whole. For a longer video, give **From** and **To** (e.g. 2:15 and 3:40,
     up to 10 minutes) to keep just the part with the artisan; several parts of one
     video can be added. Credit is the channel. Needs ffmpeg for HD and for From/To:
     `brew install ffmpeg`.
   - **From your computer**: upload files creators send you.

   Then set each item's permission. Only "Permission granted" or "Our own" may
   be published. Choose the hero image and the card image here; on a video,
   you can also mark it as that craft's "story video" (the one the city page
   leads with) or as the home page's background video.
2. **Shops**: paste a Google Maps link (from Share in Google Maps) and press
   **Find shop**. Check the details, tick what the shop sells, mark it verified
   once someone has confirmed it, and save. The site shows up to 3 verified shops.
   Google's terms allow keeping only the place ID long-term, so refresh other
   details from Google before publishing.
3. **About the art**: the city's crafts (★ = featured on the map and card); name,
   Hindi name, section, short description, materials, GI tag, keywords; and the
   story, written as paragraphs, each with its sources. A paragraph can be marked
   "checked" only with at least two sources.
4. **City details**: Hindi name, tagline, district, state, coordinates (with an
   OpenStreetMap lookup), and other names used to recognise the city in captions.

The status at the top of each city page (Draft, In review, Published, Hidden)
decides whether it appears on the site.

## Files

| File | What it is |
| --- | --- |
| `ui.py`, `ui.html` | The admin page and its local server |
| `sitedb.py`, `schema.sql` | Cities, crafts, story, media and shops (from the project's viraasat_schema.sql) |
| `places.py` | Google Maps links and the Places API |
| `pipeline.py`, `cities.json` | Instagram download, place lookup, and the command line |
| `test_pipeline.py`, `test_ui.py` | Offline tests: `python3 -m unittest test_pipeline test_ui` |

## Command line (still works)

    python3 pipeline.py add "<reel link>"     # saves to "Reels waiting for a city"
    python3 pipeline.py add "<YouTube link>" --from 2:15 --to 3:40
    python3 import_picks.py picks/khurja.json  # a city's picks; videos may be YouTube, with "from"/"to"
    python3 pipeline.py list | show <id> | export --out reels.json
    python3 pipeline.py lookup "Khajuraho"

## Keep in mind

Videos belong to their creators. Keep the credit, and get permission before
anything goes on the site.
