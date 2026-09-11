-- A COLLECTION is a named body of work -- the Box Series, the Sri Lanka
-- pieces. A painting belongs to one or to none; it is not a tag list, because
-- a piece shown in four places at once is a piece with no home. Deleting a
-- collection frees its pieces rather than taking them with it.
CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    blurb      TEXT,
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Art by McCarthy -- one-of-a-kind inventory. Quantity is always 1, so the
-- interesting state lives in works.status, not in a stock count.
CREATE TABLE IF NOT EXISTS works (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT UNIQUE NOT NULL,
    title          TEXT NOT NULL,
    year           INTEGER,
    medium         TEXT,
    h_in           REAL,
    w_in           REAL,
    d_in           REAL,
    price_cents    INTEGER,
    -- available: buyable. reserved: a checkout is open, released by the
    -- sweeper. sold: keep it up with a red dot. nfs: shown, not for sale.
    status         TEXT NOT NULL DEFAULT 'available',
    reserved_until TEXT,
    sold_at        TEXT,
    framed         INTEGER NOT NULL DEFAULT 0,
    ready_to_hang  INTEGER NOT NULL DEFAULT 1,
    signed_where   TEXT,
    story          TEXT,
    ship_band      TEXT NOT NULL DEFAULT 'medium',
    sort           INTEGER NOT NULL DEFAULT 0,
    collection_id  INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    -- EDITION IS CATALOGUE DETAIL, NOT STOCK. "Edition of 25, #4" describes a
    -- piece that is one of a run; it does NOT mean 25 of them are for sale
    -- here. Quantity is still always 1, which is what keeps reserve/release
    -- honest -- see the note at the top of this file.
    edition_size   INTEGER,
    edition_number INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_works_status ON works(status, sort, id);

CREATE TABLE IF NOT EXISTS images (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id  INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    base     TEXT NOT NULL,          -- filename stem; sizes derived from it
    kind     TEXT NOT NULL DEFAULT 'full',   -- full | detail | scale
    alt      TEXT,
    sort     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_images_work ON images(work_id, sort, id);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id           INTEGER REFERENCES works(id),
    stripe_session_id TEXT UNIQUE,
    amount_cents      INTEGER,
    buyer_name        TEXT,
    buyer_email       TEXT,
    ship_line1        TEXT,
    ship_line2        TEXT,
    ship_city         TEXT,
    ship_state        TEXT,
    ship_zip          TEXT,
    ship_country      TEXT,
    status            TEXT NOT NULL DEFAULT 'paid',   -- paid | shipped
    tracking          TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inquiries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'contact',  -- contact | commission | purchase
    name       TEXT,
    email      TEXT,
    body       TEXT,
    work_id    INTEGER REFERENCES works(id),
    handled    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    source     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
