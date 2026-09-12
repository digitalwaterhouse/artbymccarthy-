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

-- What she writes back. Drafts and sent mail are the same row at two points in
-- its life, which is why there is one table and a status rather than two: a
-- draft becomes sent in place, so nothing has to be copied between tables and
-- a half-written reply can never be lost by the act of sending it.
--
-- inquiry_id is ON DELETE SET NULL on purpose. Deleting a piece of junk from
-- the inbox must not take a reply she actually sent out with it -- same
-- reasoning as detaching inquiries from a deleted work.
CREATE TABLE IF NOT EXISTS replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id INTEGER REFERENCES inquiries(id) ON DELETE SET NULL,
    to_email   TEXT NOT NULL,
    to_name    TEXT,
    subject    TEXT,
    body       TEXT,
    status     TEXT NOT NULL DEFAULT 'draft',   -- draft | sent
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at    TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_status ON replies(status, id DESC);

-- Coming off the list is a SUPPRESSION, not a delete. The row stays and is
-- stamped instead, so the address is remembered as "do not mail" -- a plain
-- delete forgets that the person asked to be left alone, and the next import
-- or form submission silently puts them back on a list they opted out of.
CREATE TABLE IF NOT EXISTS subscribers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    source          TEXT,
    unsubscribed_at TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- EXHIBITIONS are a schedule, not a catalogue: where the work is going and
-- when. Studio-only for now -- nothing here renders on the public site.
--
-- The link to paintings is MANY-TO-MANY, which is the difference between this
-- and a collection. A piece belongs to one collection (see above) but it can
-- hang in a show in March and another one two years later, and both facts are
-- worth keeping. That is why this is a join table rather than a column.
CREATE TABLE IF NOT EXISTS exhibitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    venue      TEXT,
    city       TEXT,
    starts_on  TEXT,          -- YYYY-MM-DD
    ends_on    TEXT,
    blurb      TEXT,
    url        TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exhibition_works (
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id) ON DELETE CASCADE,
    work_id       INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    PRIMARY KEY (exhibition_id, work_id)
);
CREATE INDEX IF NOT EXISTS idx_exwork_work ON exhibition_works(work_id);

-- WHERE THE PHYSICAL PAINTING IS RIGHT NOW. Deliberately separate from
-- works.status: a piece can be `available` AND hanging in a cafe forty miles
-- away, and those two facts answer different questions. status is "can someone
-- buy it"; location is "where do I drive to collect it".
--
-- works.location holds the CURRENT place name, denormalised so the list view and
-- filters do not need a join. work_movements is the history, one row per move.
-- Place is a plain name rather than a foreign key: with one studio and a handful
-- of venues, a places table would be a second screen to maintain for no gain.
-- The form offers previously-used names so spellings stay consistent.
CREATE TABLE IF NOT EXISTS work_movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    place      TEXT NOT NULL,
    note       TEXT,
    moved_on   TEXT,          -- YYYY-MM-DD, hers to set; may differ from entry day
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_work ON work_movements(work_id, moved_on, id);

-- Conservation and repair. One row per thing done to a painting: a reframe, a
-- varnish, a touch-up after a knock in transit. cost_cents is nullable because
-- plenty of care costs nothing but her afternoon.
CREATE TABLE IF NOT EXISTS work_care (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id     INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    happened_on TEXT,          -- YYYY-MM-DD
    what        TEXT NOT NULL,
    who         TEXT,          -- framer, conservator, herself
    cost_cents  INTEGER,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_care_work ON work_care(work_id, happened_on, id);

-- OPPORTUNITIES: calls for entry, grants, residencies, fairs. The thing that
-- makes this worth having in the studio rather than in a notebook is the
-- DEADLINE -- everything else on the row exists to answer "should I bother"
-- and "what do they want".
--
-- img_longest / img_max_mb are not decoration: nearly every call states an
-- image spec, and re-exporting a folder of JPEGs to it by hand is the most
-- tedious part of applying. Stored here, they PREFILL the packet builder, so
-- the spec is typed once when the call is added and never again.
--
-- Deliberately NOT joined to exhibitions. A call is a thing you apply to and
-- usually lose; a show is a thing that is happening. Conflating them would
-- put twenty rejections in the permanent exhibition record.
CREATE TABLE IF NOT EXISTS opportunities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    org          TEXT,
    kind         TEXT NOT NULL DEFAULT 'show',   -- show | grant | residency | fair | other
    url          TEXT,
    location     TEXT,
    fee_cents    INTEGER,
    opens_on     TEXT,          -- YYYY-MM-DD
    deadline     TEXT,          -- YYYY-MM-DD -- the date everything sorts by
    notified_on  TEXT,          -- when they say they will let you know
    event_on     TEXT,          -- when the show itself runs, if you get in
    event_ends   TEXT,
    max_works    INTEGER,       -- how many pieces they will look at
    img_longest  INTEGER,       -- px on the longest side, per their spec
    img_max_mb   REAL,          -- per-file ceiling, per their spec
    notes        TEXT,
    -- watching: found it, not applied. applied: sent. accepted / declined:
    -- heard back. passed: looked and decided against, which is worth keeping
    -- so the same call is not reconsidered from scratch next year.
    status       TEXT NOT NULL DEFAULT 'watching',
    applied_on   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opps_deadline ON opportunities(deadline, id);

-- Which paintings went to which call. Many-to-many for the same reason
-- exhibition_works is: a piece gets submitted to several calls over its life,
-- and knowing it was already rejected from one is exactly what you want in
-- front of you before submitting it there again.
CREATE TABLE IF NOT EXISTS opportunity_works (
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    work_id        INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    PRIMARY KEY (opportunity_id, work_id)
);
CREATE INDEX IF NOT EXISTS idx_oppwork_work ON opportunity_works(work_id);
