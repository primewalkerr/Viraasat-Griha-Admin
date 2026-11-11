-- Viraasat Griha site database (same file as the reel pipeline: viraasat.db).
-- Based on pipeline/viraasat_schema.sql in the Viraasat project. Safe to run
-- on every start: every statement is IF NOT EXISTS. The reels table is created
-- by pipeline.py; its city_id / craft_id links are added there.
-- Seed data lives in sitedb.py and runs only on an empty database.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS categories (
  id           INTEGER PRIMARY KEY,
  slug         TEXT NOT NULL UNIQUE,            -- art | food | clothing
  name         TEXT NOT NULL,
  name_hi      TEXT,
  blurb        TEXT,
  accent_hex   TEXT,
  sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cities (
  id            INTEGER PRIMARY KEY,
  slug          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  name_hi       TEXT,
  state         TEXT NOT NULL,
  district      TEXT,
  country       TEXT NOT NULL DEFAULT 'India',
  lat           REAL,
  lon           REAL,
  pin_number    INTEGER UNIQUE,
  map_x         REAL,
  map_y         REAL,
  tagline       TEXT,
  aliases       TEXT,                           -- JSON array
  hero_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
  card_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','in_review','published','hidden')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crafts (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  name_hi         TEXT,
  category_id     INTEGER NOT NULL REFERENCES categories(id),
  short_desc      TEXT,
  materials       TEXT,
  gi_tag          INTEGER NOT NULL DEFAULT 0 CHECK (gi_tag IN (0,1)),
  gi_year         INTEGER,
  keywords        TEXT,                         -- JSON array
  making_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
  status          TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','in_review','published','hidden')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS city_crafts (
  city_id     INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
  craft_id    INTEGER NOT NULL REFERENCES crafts(id) ON DELETE CASCADE,
  is_featured INTEGER NOT NULL DEFAULT 0 CHECK (is_featured IN (0,1)),
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, craft_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_featured_craft_per_city
  ON city_crafts(city_id) WHERE is_featured = 1;

CREATE TABLE IF NOT EXISTS craft_story_paragraphs (
  id          INTEGER PRIMARY KEY,
  craft_id    INTEGER NOT NULL REFERENCES crafts(id) ON DELETE CASCADE,
  city_id     INTEGER REFERENCES cities(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  body        TEXT NOT NULL,
  fact_status TEXT NOT NULL DEFAULT 'unchecked'
              CHECK (fact_status IN ('unchecked','checked','disputed')),
  UNIQUE (craft_id, city_id, position)
);

CREATE TABLE IF NOT EXISTS sources (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL,
  publisher   TEXT,
  url         TEXT,
  accessed_on TEXT
);

CREATE TABLE IF NOT EXISTS paragraph_sources (
  paragraph_id INTEGER NOT NULL REFERENCES craft_story_paragraphs(id) ON DELETE CASCADE,
  source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY (paragraph_id, source_id)
);

CREATE TABLE IF NOT EXISTS media (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('photo','video')),
  url           TEXT,
  local_path    TEXT,                           -- relative to the media folder
  poster_url    TEXT,
  poster_path   TEXT,                           -- local poster frame, relative to media
  alt_text      TEXT,
  width         INTEGER,
  height        INTEGER,
  duration_s    REAL,
  credit_name   TEXT,
  credit_url    TEXT,
  license       TEXT,
  permission    TEXT NOT NULL DEFAULT 'not_asked'
                CHECK (permission IN ('not_asked','requested','granted','refused','own')),
  source_url    TEXT,                           -- the Instagram post it came from
  caption       TEXT,
  reel_id       INTEGER REFERENCES reels(id) ON DELETE SET NULL,
  city_id       INTEGER REFERENCES cities(id) ON DELETE SET NULL,
  craft_id      INTEGER REFERENCES crafts(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The actual bytes of every file referenced by media.local_path / poster_path.
-- The site serves /media/<path> straight out of this table (no local disk on
-- the deployed server); the admin tool also keeps a local copy under
-- VG_MEDIA_DIR, but this table is what makes a file visible on the live site.
CREATE TABLE IF NOT EXISTS media_files (
  path          TEXT PRIMARY KEY,
  content_type  TEXT NOT NULL,
  data          BLOB NOT NULL,
  size          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  id                INTEGER PRIMARY KEY,
  city_id           INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
  name              TEXT NOT NULL,
  address           TEXT,
  postcode          TEXT,
  lat               REAL,
  lon               REAL,
  phone             TEXT,
  website           TEXT,
  maps_url          TEXT,
  google_place_id   TEXT,                       -- lets the site refresh details from Google
  rating            REAL CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
  rating_count      INTEGER,
  rating_source     TEXT,
  rating_checked_at TEXT,
  verified          INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),
  sort_order        INTEGER NOT NULL DEFAULT 0,
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS store_crafts (
  store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  craft_id INTEGER NOT NULL REFERENCES crafts(id) ON DELETE CASCADE,
  PRIMARY KEY (store_id, craft_id)
);

CREATE TABLE IF NOT EXISTS shop_collections (
  id                    INTEGER PRIMARY KEY,
  craft_id              INTEGER REFERENCES crafts(id) ON DELETE CASCADE,
  city_id               INTEGER REFERENCES cities(id) ON DELETE CASCADE,
  shopify_handle        TEXT UNIQUE,
  shopify_collection_id TEXT,
  is_live               INTEGER NOT NULL DEFAULT 0 CHECK (is_live IN (0,1)),
  CHECK (craft_id IS NOT NULL OR city_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_city_crafts_craft ON city_crafts(craft_id);
CREATE INDEX IF NOT EXISTS idx_media_city        ON media(city_id);
CREATE INDEX IF NOT EXISTS idx_media_craft       ON media(craft_id);
CREATE INDEX IF NOT EXISTS idx_stores_city       ON stores(city_id);

CREATE VIEW IF NOT EXISTS v_map_pins AS
SELECT c.pin_number, c.slug, c.name, c.state, c.map_x, c.map_y,
       cr.name AS craft_name, cat.slug AS category
FROM cities c
JOIN city_crafts cc ON cc.city_id = c.id AND cc.is_featured = 1
JOIN crafts cr      ON cr.id = cc.craft_id
JOIN categories cat ON cat.id = cr.category_id
WHERE c.status = 'published'
ORDER BY c.pin_number;

CREATE VIEW IF NOT EXISTS v_city_cards AS
SELECT c.slug, c.name, c.state, cr.name AS craft_name, cr.short_desc,
       m.url AS photo_url, m.local_path AS photo_path, m.alt_text AS photo_alt, m.credit_name
FROM cities c
JOIN city_crafts cc ON cc.city_id = c.id AND cc.is_featured = 1
JOIN crafts cr      ON cr.id = cc.craft_id
LEFT JOIN media m   ON m.id = c.card_media_id
WHERE c.status = 'published'
ORDER BY c.pin_number;

CREATE VIEW IF NOT EXISTS v_city_stores AS
SELECT c.slug AS city_slug, s.name, s.address, s.rating, s.rating_source,
       s.rating_checked_at, s.maps_url, s.website
FROM stores s JOIN cities c ON c.id = s.city_id
WHERE s.verified = 1
ORDER BY c.slug, s.sort_order;

-- Site-wide singleton settings (currently just which video plays behind the
-- home page hero). A plain key/value table so new settings don't need a
-- schema migration each time.
CREATE TABLE IF NOT EXISTS site_settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
