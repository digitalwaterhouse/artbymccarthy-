"""Data layer for the gallery: works, images, orders, inquiries, list.

One painting is one row and the quantity is always one, so nothing here
counts stock -- the whole question is what state a work is in, which is why
status carries reserved/sold rather than a number going up and down.
"""
import os
import re
import io
import json
import time
import sqlite3
import secrets
import zipfile
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "gallery.db")
PHOTO_DIR = os.path.join(BASE, "data", "photos")

# Three widths, because a phone should not download a print-quality file and a
# desktop should not get a soft one. WebP with a JPEG beside it for old Safari.
SIZES = [("s", 600), ("m", 1400), ("l", 2400)]

SHIP_BANDS = ["small", "medium", "large", "rolled", "quote"]
# draft is UNPUBLISHED: the work exists, photographs can be uploaded and the
# story written over several evenings, and none of it is on the wall until
# the status changes. Everything else in this list is public.
STATUSES = ["available", "reserved", "sold", "nfs", "draft"]
PUBLIC_STATUSES = [s for s in STATUSES if s != "draft"]
# What each status is CALLED in the studio. The stored values stay short and
# stable -- they are in the database, in queries and in the CSV export -- while
# these are what a person reads. "nfs" is real gallery usage, printed on
# exhibition labels, but sitting lowercase between "available" and "sold" in a
# dropdown it reads like a filesystem.
STATUS_LABELS = {"available": "available", "reserved": "reserved",
                 "sold": "sold", "nfs": "not for sale", "draft": "draft"}


def status_label(status):
    return STATUS_LABELS.get(status, status)

DEFAULT_SETTINGS = {
    "site_title": "Art by McCarthy",
    "tagline": "Original paintings",
    "about": "",
    "artist_email": "",
    "ship_small_cents": "2500",
    "ship_medium_cents": "5500",
    "ship_large_cents": "12000",
    "ship_rolled_cents": "6500",
    "currency": "usd",
    "commission_note": "",
    # The landing page. hero_image is a photo base (the random stem written by
    # add_image), not a work id, so the piece on the front can be one the shop
    # is not selling -- a sold favourite, or work still in the studio.
    "hero_image": "",
    "hero_title": "",
    "hero_sub": "",
    "hero_caption": "",
    # A portrait for the About page. Same idea as hero_image: a photo base,
    # not a work, because the artist is not for sale.
    "about_image": "",
    "about_caption": "",
    # THE BOX BAND on the landing page. These pieces are shadow boxes, 2.5in
    # deep, and every photograph on the site is straight-on -- so the one thing
    # that makes them objects rather than pictures is the one thing a visitor
    # cannot see. box_image is a photo base like the two above: a single box
    # shot from an angle, or edge-on in raking light. The band still renders
    # without it, because her sentence is worth reading on its own.
    "box_image": "",
    "box_caption": "",
    # HER WORDS, cut down for the front page by Paul over two passes on
    # 2026-09-11: first the bio's opening clause went, then everything after
    # "collected". What is left is a phrase, not a sentence, which is why the
    # band sets it large with her name under it -- it reads as a wall label.
    # Her punctuation stands: "humor- lovingly" is as she typed it, and there
    # is no full stop because she did not put one. DO NOT TIDY IT; see the
    # About note. The live database holds a row for this key, so the value
    # here only ever applies to a fresh install.
    "box_note": "Nuggets of hope and humor- lovingly collected",
    # quiet | rosette | rose | none -- the backdrop on about/commissions/contact
    "page_bg": "quiet",
    # Printed on wall labels and checklists. A setting, not a constant: she
    # writes it "Lisa Mc Carthy" with a space in her own bio and "McCarthy"
    # everywhere else on the site, and that is hers to settle, not mine.
    "artist_name": "Lisa McCarthy",
    # Written once, sent with every application. Nearly every call asks for
    # both, they change maybe twice a year, and retyping them into a web form
    # at midnight is how a good statement turns into a rushed one.
    "artist_statement": "",
    "artist_bio": "",
    # The last date a deadline digest went out, so it goes out once a day and
    # not once a page view. Not in the UI; see remind_due().
    "opps_reminded_on": "",
    # WHERE SHE WORKS, and the point it measures from. A call for entry two
    # hours away is a different proposition from one across the county -- the
    # work has to be driven there and driven back -- so Opportunities shows a
    # distance, and this is the other end of it. Typed as a place name; the
    # two numbers are geocoded from it once and cached so nothing looks a
    # coordinate up on a page view.
    # Seeded with the real answer rather than left blank, so the distances are
    # there the first time she opens Opportunities instead of after a trip to
    # Settings. These are DEFAULTS: the live database has no row for these keys
    # yet, so they apply until she saves a location, and typing a different one
    # geocodes it and overwrites all four. Bedford is in WESTCHESTER COUNTY,
    # NEW YORK -- not New Bedford, which is in Massachusetts and is a different
    # place two hundred miles away.
    "studio_location": "Bedford, NY",
    "studio_lat": "41.2041304",
    "studio_lon": "-73.6425090",
    "studio_place": "Town of Bedford, Westchester County, New York, United States",
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)
    with open(os.path.join(BASE, "schema.sql")) as f:
        sql = f.read()
    with connect() as conn:
        conn.executescript(sql)
        _migrate(conn)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


# Columns added to a table that already exists. schema.sql is all CREATE TABLE
# IF NOT EXISTS, which does nothing at all to a live database -- so a new
# column has to be added here or it only ever appears on a fresh install, and
# the one database that matters is the live one on Render.
NEW_COLUMNS = {
    "subscribers": [
        ("unsubscribed_at", "TEXT"),
    ],
    # The packing record lives on the join row: which pieces have physically
    # left for this show, and which have come back. Two stamps rather than two
    # flags, because "when did it go" is the question asked next.
    "exhibition_works": [
        ("out_at",  "TEXT"),
        ("back_at", "TEXT"),
    ],
    # One level of nesting under a collection (Sri Lanka > Fireflies). The
    # collections table already existed, so this cannot live in schema.sql.
    "collections": [
        ("parent_id", "INTEGER REFERENCES collections(id) ON DELETE SET NULL"),
    ],
    # Where a call is, as two numbers. Geocoded from `location` when the row is
    # saved and then left alone -- a call does not move, and Nominatim asks for
    # one request a second, which is not a thing to spend on a page view.
    # geo_query records WHAT was looked up, so a corrected location re-geocodes
    # and an unchanged one never does.
    "opportunities": [
        ("lat",         "REAL"),
        ("lon",         "REAL"),
        ("geo_query",   "TEXT"),
        ("geo_place",   "TEXT"),
        ("geocoded_at", "TEXT"),
    ],
    "works": [
        ("location",       "TEXT"),
        ("collection_id",  "INTEGER REFERENCES collections(id) ON DELETE SET NULL"),
        ("edition_size",   "INTEGER"),
        ("edition_number", "INTEGER"),
    ],
}


def _migrate(conn):
    for table, cols in NEW_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in have:
                # SQLite allows ADD COLUMN with a REFERENCES clause as long as
                # the default is NULL, which every one of these is.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    # Indexes on the new columns belong here too, and NOT in schema.sql: that
    # file is executed in full before this runs, so an index naming a column
    # this migration has yet to add takes the whole script down with it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_works_collection "
                 "ON works(collection_id, sort, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collections_parent "
                 "ON collections(parent_id, sort, name)")


# ------------------------------------------------------------------ settings
def settings():
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def admin_password_is_set():
    """Has she set her own password, or is the site still on the env one?"""
    return bool(settings().get("admin_pass_hash"))


def set_admin_password(pw):
    from werkzeug.security import generate_password_hash
    save_settings({"admin_pass_hash": generate_password_hash(pw)})


def check_admin_password(pw, env_password=""):
    """True if this password opens /admin.

    WHY THERE ARE TWO. The password used to live only in ADMIN_PASS on the
    host, which meant the artist could not change her own password without
    asking whoever administers the server — the wrong dependency for a site she
    owns. Hers is stored here as a hash and takes precedence.

    The environment password KEEPS WORKING as a recovery path. That is a
    deliberate second key rather than an oversight: if she forgets hers, the
    alternative is an admin with database access, at which point the recovery
    path exists anyway and is merely undocumented. Revoke it by clearing
    ADMIN_PASS on the host once she has set her own.
    """
    from werkzeug.security import check_password_hash
    if not pw:
        return False
    stored = settings().get("admin_pass_hash") or ""
    if stored and check_password_hash(stored, pw):
        return True
    return bool(env_password) and pw == env_password


def save_settings(d):
    with connect() as conn:
        for k, v in d.items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


# --------------------------------------------------------------------- works
def slugify(title, year=None):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if year:
        s = f"{s}-{year}"
    return s or secrets.token_hex(4)


def unique_slug(base_slug, work_id=None, table="works"):
    with connect() as conn:
        slug, n = base_slug, 2
        while True:
            row = conn.execute(f"SELECT id FROM {table} WHERE slug=?", (slug,)).fetchone()
            if row is None or (work_id and row["id"] == work_id):
                return slug
            slug = f"{base_slug}-{n}"
            n += 1


def _hydrate(conn, rows):
    works = [dict(r) for r in rows]
    if not works:
        return works
    ids = [w["id"] for w in works]
    q = "SELECT * FROM images WHERE work_id IN (%s) ORDER BY sort, id" % ",".join("?" * len(ids))
    by_work = {}
    for img in conn.execute(q, ids).fetchall():
        by_work.setdefault(img["work_id"], []).append(dict(img))
    names = {r["id"]: dict(r) for r in
             conn.execute("SELECT id, slug, name, parent_id FROM collections").fetchall()}
    # A subcategory is named with the collection it sits under ("Sri Lanka
    # \u2192 Fireflies") wherever a piece shows where it is filed: on its own the
    # word "Fireflies" does not say which body of work it belongs to.
    for c in names.values():
        par = names.get(c.get("parent_id"))
        c["path"] = f'{par["name"]} \u2192 {c["name"]}' if par else c["name"]
        c["parent"] = par
    for w in works:
        w["images"] = by_work.get(w["id"], [])
        w["price"] = money(w["price_cents"])
        w["dims"] = dims(w)
        w["edition"] = edition(w)
        w["collection"] = names.get(w.get("collection_id"))
    return works


def money(cents):
    if cents is None:
        return None
    return "${:,.0f}".format(cents / 100.0) if cents % 100 == 0 else "${:,.2f}".format(cents / 100.0)


def dims(w):
    """Galleries write height before width. Depth only when it matters."""
    def n(v):
        if v is None:
            return None
        return str(int(v)) if float(v) == int(v) else str(v)
    h, wd, d = n(w.get("h_in")), n(w.get("w_in")), n(w.get("d_in"))
    if not (h and wd):
        return None
    s = f"{h} × {wd}"
    if d:
        s += f" × {d}"
    return s + " in"


def edition(w):
    """How a run is written on a gallery label.

    Catalogue detail only: this says the piece is one of a run, never that
    several of it are for sale. Stock stays at one -- see the note at the top
    of this file and the comment in schema.sql."""
    size, num = w.get("edition_size"), w.get("edition_number")
    if size and num:
        return f"#{num} of {size}"
    if size:
        return f"Edition of {size}"
    if num:
        return f"#{num}"
    return None


def list_works(status=None, include_nfs=True, include_draft=False, collection_id=None):
    """The works, in the order the wall shows them.

    include_draft is FALSE by default and that default is the safety. The
    unfiltered call is what the sitemap uses, and a drafted painting listed
    there invites Google to index a page that is not meant to exist yet. Only
    the admin asks for drafts, and it asks explicitly.
    """
    sql = "SELECT * FROM works"
    args = []
    if status == "for_sale":
        sql += " WHERE status IN ('available','reserved')"
    elif status == "sold":
        sql += " WHERE status='sold'"
    elif status:
        sql += " WHERE status=?"
        args.append(status)
    if status == "for_sale" and include_nfs:
        sql = "SELECT * FROM works WHERE status IN ('available','reserved','nfs')"
    elif status is None and not include_draft:
        sql += " WHERE status <> 'draft'"
    if collection_id is not None:
        sql += (" AND " if " WHERE " in sql else " WHERE ") + "collection_id=?"
        args.append(collection_id)
    sql += " ORDER BY sort, id DESC"
    with connect() as conn:
        return _hydrate(conn, conn.execute(sql, args).fetchall())


def count_works():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) n FROM works").fetchone()["n"]


def search_works(q=None, status=None, collection_id=None, place=None, medium=None):
    """The studio's own view of the collection, filtered.

    Separate from list_works on purpose: that one serves the PUBLIC wall and its
    defaults are the safety (drafts excluded unless asked). This one always shows
    everything she owns, including drafts, because the studio is where you go to
    find the piece that is not on the site yet.
    """
    sql = "SELECT * FROM works WHERE 1=1"
    args = []
    if q:
        # Matched across the fields she would actually search by. A LIKE with
        # both wildcards, not a prefix match: "tuk" should find "Tuk-tuk" and
        # "red" should find "Red Ground".
        like = "%" + q.strip().lower() + "%"
        sql += (" AND (LOWER(title) LIKE ? OR LOWER(COALESCE(medium,'')) LIKE ?"
                " OR LOWER(COALESCE(story,'')) LIKE ? OR LOWER(COALESCE(location,'')) LIKE ?"
                " OR LOWER(COALESCE(signed_where,'')) LIKE ?)")
        args += [like] * 5
    if status:
        sql += " AND status=?"
        args.append(status)
    if collection_id == 0:
        sql += " AND collection_id IS NULL"
    elif collection_id:
        # Asking for a collection means asking for its subcategories too --
        # filtering by Sri Lanka and being shown none of the Fireflies pieces
        # reads as a broken filter, not as a distinction.
        fam = collection_family(collection_id)
        sql += " AND collection_id IN (%s)" % ",".join("?" * len(fam))
        args += fam
    if place == 0:
        sql += " AND (location IS NULL OR location='')"
    elif place:
        sql += " AND location=?"
        args.append(place)
    if medium:
        sql += " AND medium=?"
        args.append(medium)
    sql += " ORDER BY sort, id DESC"
    with connect() as conn:
        return _hydrate(conn, conn.execute(sql, args).fetchall())


def media_list():
    """Distinct mediums actually in use, for the filter."""
    with connect() as conn:
        return [r["medium"] for r in conn.execute(
            "SELECT DISTINCT medium FROM works WHERE medium IS NOT NULL AND medium<>''"
            " ORDER BY medium").fetchall()]


def current_places():
    """Places that currently hold something, for the filter — distinct from
    places(), which is every place anything has EVER been."""
    with connect() as conn:
        return [r["location"] for r in conn.execute(
            "SELECT location, COUNT(*) n FROM works WHERE location IS NOT NULL AND location<>''"
            " GROUP BY location ORDER BY n DESC, location").fetchall()]


def get_work(slug=None, work_id=None):
    with connect() as conn:
        if slug is not None:
            row = conn.execute("SELECT * FROM works WHERE slug=?", (slug,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if row is None:
            return None
        return _hydrate(conn, [row])[0]


def neighbours(work):
    """The pieces either side of this one, in the order the wall shows them.

    A sold work is sequenced against the archive and everything else against
    the wall, so Next always keeps you inside the run you were browsing. The
    ends wrap: with a couple of dozen pieces, a dead end is more annoying than
    a loop, and the counter tells you where you are."""
    same = list_works(status="sold" if work["status"] == "sold" else "for_sale")
    slugs = [w["slug"] for w in same]
    if work["slug"] not in slugs or len(slugs) < 2:
        return None
    i = slugs.index(work["slug"])
    return {"prev": same[i - 1], "next": same[(i + 1) % len(same)],
            "index": i + 1, "total": len(same)}


def save_work(data, work_id=None):
    fields = ["title", "year", "medium", "h_in", "w_in", "d_in", "price_cents",
              "status", "framed", "ready_to_hang", "signed_where", "story",
              "ship_band", "sort", "collection_id", "edition_size",
              "edition_number"]
    # location is NOT here on purpose: it changes by MOVING a work, never by
    # editing the form, so that works.location and work_movements cannot drift.
    vals = {k: data.get(k) for k in fields}
    vals["slug"] = unique_slug(data.get("slug") or slugify(vals["title"], vals["year"]), work_id)
    with connect() as conn:
        if work_id:
            vals["updated_at"] = now()
            sets = ", ".join(f"{k}=:{k}" for k in vals)
            conn.execute(f"UPDATE works SET {sets} WHERE id=:id", {**vals, "id": work_id})
            return work_id
        vals["created_at"] = now()
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO works ({cols}) VALUES ({', '.join(':'+k for k in vals)})", vals)
        return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def delete_work(work_id):
    """Remove a work. Returns False if it cannot go.

    Two other tables point at works. An ORDER is the record of a sale and must
    outlive the listing, so a work that has sold through the site is refused
    rather than silently orphaning it. An INQUIRY is a message from a person
    and is worth keeping on its own, so it is simply detached. Without either
    of these the delete hit a foreign key error and the admin's delete button
    threw a 500."""
    w = get_work(work_id=work_id)
    if not w:
        return False
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM orders WHERE work_id=?",
                         (work_id,)).fetchone()["c"]
        if n:
            return False
    for img in w["images"]:
        drop_image_files(img["base"])
    with connect() as conn:
        conn.execute("UPDATE inquiries SET work_id=NULL WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM images WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM works WHERE id=?", (work_id,))
    return True


# ----------------------------------------------------------------- location
# Where each physical painting is. See schema.sql for why this is separate from
# status and why place is a name rather than a foreign key.

def places():
    """Every place a work has been, most-used first — feeds the datalist so the
    same cafe is not typed three different ways."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT place, COUNT(*) n FROM work_movements GROUP BY place ORDER BY n DESC, place"
        ).fetchall()
    return [r["place"] for r in rows]


def movements(work_id):
    """Newest first — where it is now reads before where it used to be."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM work_movements WHERE work_id=? ORDER BY COALESCE(moved_on,'') DESC, id DESC",
            (work_id,)).fetchall()]


def relocate(work_id, place, note=None, moved_on=None):
    """Record a move and update the work's current location in one transaction.

    NOT called move_work: that name is already the sort-order mover further down
    this file, and defining it twice meant the second definition silently won.

    Both, or neither: works.location is a denormalised copy of the newest
    movement, and if the two ever disagree the list view starts lying about
    where a painting is."""
    place = (place or "").strip()
    if not place:
        return False
    with connect() as conn:
        conn.execute(
            "INSERT INTO work_movements (work_id, place, note, moved_on, created_at)"
            " VALUES (?,?,?,?,?)",
            (work_id, place, (note or "").strip() or None, (moved_on or "").strip() or today(), now()))
        conn.execute("UPDATE works SET location=?, updated_at=? WHERE id=?",
                     (place, now(), work_id))
    return True


def delete_movement(movement_id):
    """Remove a mis-entered move and re-point the work at whatever is now newest.

    Deleting the latest entry has to roll works.location back, or the work keeps
    claiming to be somewhere its own history no longer mentions."""
    with connect() as conn:
        row = conn.execute("SELECT work_id FROM work_movements WHERE id=?", (movement_id,)).fetchone()
        if not row:
            return False
        wid = row["work_id"]
        conn.execute("DELETE FROM work_movements WHERE id=?", (movement_id,))
        newest = conn.execute(
            "SELECT place FROM work_movements WHERE work_id=? ORDER BY COALESCE(moved_on,'') DESC, id DESC LIMIT 1",
            (wid,)).fetchone()
        conn.execute("UPDATE works SET location=?, updated_at=? WHERE id=?",
                     (newest["place"] if newest else None, now(), wid))
    return True


# ---------------------------------------------------------------------- care
# Conservation, repair, reframing. A log, like movements — the interesting thing
# is the sequence and what it cost, not a single current value.

def care_for(work_id):
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM work_care WHERE work_id=? ORDER BY COALESCE(happened_on,'') DESC, id DESC",
            (work_id,)).fetchall()]
    for r in rows:
        r["cost"] = money(r["cost_cents"])
    return rows


def care_total(work_id):
    with connect() as conn:
        v = conn.execute("SELECT COALESCE(SUM(cost_cents),0) t FROM work_care WHERE work_id=?",
                         (work_id,)).fetchone()["t"]
    return v or 0


def add_care(work_id, what, who=None, cost_cents=None, happened_on=None, note=None):
    what = (what or "").strip()
    if not what:
        return False
    with connect() as conn:
        conn.execute(
            "INSERT INTO work_care (work_id, happened_on, what, who, cost_cents, note, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, (happened_on or "").strip() or today(), what,
             (who or "").strip() or None, cost_cents, (note or "").strip() or None, now()))
    return True


def delete_care(care_id):
    with connect() as conn:
        conn.execute("DELETE FROM work_care WHERE id=?", (care_id,))
    return True


def works_in_show(exhibition_id):
    """The paintings attached to a show, in the wall order she set."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT w.*, x.out_at, x.back_at FROM works w"
            " JOIN exhibition_works x ON x.work_id = w.id"
            " WHERE x.exhibition_id=? ORDER BY w.sort, w.id", (exhibition_id,)).fetchall()
        return _hydrate(conn, rows)


def shows_for(work_id):
    """Which exhibitions a piece has hung in. The attach side lives on the show;
    this is the same link read from the painting's end, which is the question
    asked far more often."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.* FROM exhibitions e JOIN exhibition_works x ON x.exhibition_id = e.id"
            " WHERE x.work_id=? ORDER BY COALESCE(e.starts_on,'') DESC", (work_id,)).fetchall()]
    return rows


# --------------------------------------------------------------- collections
# A named body of work. One collection per piece, or none -- see schema.sql
# for why this is not a tag list, and why nesting stops at one level.

def _nest(rows):
    """Parents in hanging order, each followed by its own children.

    A child whose parent has vanished is shown at the top level rather than
    hidden: an orphan is still a body of work, and a collection that silently
    disappears from the list is worse than one filed in the wrong place."""
    by_id = {c["id"]: c for c in rows}
    tops, kids = [], {}
    for c in rows:
        pid = c.get("parent_id")
        if pid and pid in by_id and pid != c["id"]:
            kids.setdefault(pid, []).append(c)
        else:
            tops.append(c)
    out = []
    for c in tops:
        c["depth"] = 0
        c["parent"] = None
        c["path"] = c["name"]
        c["children"] = kids.get(c["id"], [])
        out.append(c)
        for k in c["children"]:
            k["depth"] = 1
            k["parent"] = c
            k["path"] = f'{c["name"]} \u2192 {k["name"]}'
            k["children"] = []
            out.append(k)
    return out


def list_collections(with_counts=True):
    """Collections in hanging order, each with how many pieces are in it.

    Flat, but ordered as the tree reads: a subcategory comes directly after the
    collection it sits under, and carries depth/path so a <select> or a table
    can show the nesting without a second query.

    The count is of PUBLIC pieces: a collection whose only members are still
    drafts reads as empty on the site, and the studio list should say the same
    number the visitor will see rather than a more flattering one. `total` adds
    the children's pieces in, because that is what a parent's page shows."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM collections ORDER BY sort, name").fetchall()]
        if with_counts:
            counts = {r["collection_id"]: r["c"] for r in conn.execute(
                "SELECT collection_id, COUNT(*) c FROM works "
                "WHERE status <> 'draft' AND collection_id IS NOT NULL "
                "GROUP BY collection_id").fetchall()}
            drafts = {r["collection_id"]: r["c"] for r in conn.execute(
                "SELECT collection_id, COUNT(*) c FROM works "
                "WHERE status = 'draft' AND collection_id IS NOT NULL "
                "GROUP BY collection_id").fetchall()}
            for c in rows:
                c["count"] = counts.get(c["id"], 0)
                c["draft_count"] = drafts.get(c["id"], 0)
    out = _nest(rows)
    if with_counts:
        for c in out:
            c["total"] = c["count"] + sum(k["count"] for k in c["children"])
            c["total_draft"] = (c["draft_count"]
                                + sum(k["draft_count"] for k in c["children"]))
    return out


def get_collection(slug=None, collection_id=None):
    """One collection, with its parent and children attached.

    Both are looked up here rather than at each call site: every page that
    shows a collection needs to say what it sits under or what sits under it."""
    with connect() as conn:
        if slug is not None:
            row = conn.execute("SELECT * FROM collections WHERE slug=?", (slug,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM collections WHERE id=?",
                               (collection_id,)).fetchone()
        if not row:
            return None
        c = dict(row)
        c["parent"] = None
        if c.get("parent_id"):
            par = conn.execute("SELECT * FROM collections WHERE id=?",
                               (c["parent_id"],)).fetchone()
            c["parent"] = dict(par) if par else None
            if not c["parent"]:
                c["parent_id"] = None
        c["children"] = [dict(r) for r in conn.execute(
            "SELECT * FROM collections WHERE parent_id=? AND id<>? "
            "ORDER BY sort, name", (c["id"], c["id"]))] if not c["parent_id"] else []
    return c


def collection_family(collection_id):
    """This collection and anything filed under it.

    What a parent's page and the studio's collection filter both mean by
    "Sri Lanka": the pieces filed there AND the ones filed in its
    subcategories."""
    with connect() as conn:
        kids = [r["id"] for r in conn.execute(
            "SELECT id FROM collections WHERE parent_id=? AND id<>?",
            (collection_id, collection_id))]
    return [collection_id] + kids


def save_collection(data, collection_id=None):
    vals = {"name": (data.get("name") or "").strip(),
            "blurb": data.get("blurb") or "",
            "sort": data.get("sort") or 0,
            "parent_id": _valid_parent(data.get("parent_id"), collection_id)}
    vals["slug"] = unique_slug(data.get("slug") or slugify(vals["name"]),
                               collection_id, table="collections")
    with connect() as conn:
        if collection_id:
            sets = ", ".join(f"{k}=:{k}" for k in vals)
            conn.execute(f"UPDATE collections SET {sets} WHERE id=:id",
                         {**vals, "id": collection_id})
            return collection_id
        vals["created_at"] = now()
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO collections ({cols}) "
                     f"VALUES ({', '.join(':'+k for k in vals)})", vals)
        return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def _current_parent(collection_id):
    if not collection_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT parent_id FROM collections WHERE id=?",
                           (collection_id,)).fetchone()
    return row["parent_id"] if row else None


def _valid_parent(parent_id, collection_id=None):
    """The one rule that keeps the tree one level deep, enforced here rather
    than in the form: a subcategory cannot itself have subcategories, nothing
    can be its own parent, and a collection that already has children cannot be
    filed under something else (that would make grandchildren).

    An EMPTY choice means "on its own" and clears the parent. An impossible one
    leaves the collection where it is: a refused move must not have the side
    effect of turning a subcategory loose."""
    try:
        parent_id = int(parent_id)
    except (TypeError, ValueError):
        return None if parent_id in (None, "", 0, "0") else _current_parent(collection_id)
    if not parent_id:
        return None
    if collection_id and parent_id == int(collection_id):
        return _current_parent(collection_id)
    with connect() as conn:
        par = conn.execute("SELECT parent_id FROM collections WHERE id=?",
                           (parent_id,)).fetchone()
        if not par or par["parent_id"]:
            return _current_parent(collection_id)
        if collection_id and conn.execute(
                "SELECT 1 FROM collections WHERE parent_id=? LIMIT 1",
                (collection_id,)).fetchone():
            return _current_parent(collection_id)
    return parent_id


def set_collection_works(collection_id, work_ids):
    """Say exactly which pieces are in this collection.

    The counterpart to choosing a collection on the painting's own page -- the
    same fact written from the other end, which is the end you are at when you
    are filing twenty paintings at once.

    A piece has ONE collection, so ticking one that belongs elsewhere MOVES it.
    Unticking only ever clears pieces that were in THIS collection: it must not
    reach into another one and empty it."""
    ids = {int(i) for i in work_ids if str(i).strip().isdigit()}
    with connect() as conn:
        now_in = {r["id"] for r in conn.execute(
            "SELECT id FROM works WHERE collection_id=?", (collection_id,))}
        removed = now_in - ids
        added = ids - now_in
        if removed:
            conn.execute("UPDATE works SET collection_id=NULL WHERE id IN (%s)"
                         % ",".join("?" * len(removed)), list(removed))
        if added:
            conn.execute("UPDATE works SET collection_id=? WHERE id IN (%s)"
                         % ",".join("?" * len(added)),
                         [collection_id] + list(added))
    return len(added), len(removed)


def delete_collection(collection_id):
    """Delete the collection, keep the paintings.

    The pieces are detached rather than deleted -- the same reasoning as the
    inquiries in delete_work. Nobody types "delete the Box Series" meaning
    "delete the eleven paintings in it". Subcategories are promoted to
    collections in their own right for the same reason: deleting the parent
    must not quietly take four other bodies of work with it."""
    with connect() as conn:
        conn.execute("UPDATE collections SET parent_id=NULL WHERE parent_id=?",
                     (collection_id,))
        conn.execute("UPDATE works SET collection_id=NULL WHERE collection_id=?",
                     (collection_id,))
        conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
    return True


def list_editioned_works():
    """Pieces that carry a run, newest first. The Editions section is a view of
    the catalogue, not a second inventory -- editing happens on the piece."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM works WHERE edition_size IS NOT NULL "
            "OR edition_number IS NOT NULL ORDER BY sort, id DESC").fetchall()
        return _hydrate(conn, rows)


# -------------------------------------------------------------- exhibitions
# Where the work is going and when. Studio-only: nothing here has a public
# route. Dates are plain YYYY-MM-DD strings, which sort correctly as text and
# compare directly against today's date.

def _past_cutoff():
    """A show counts as finished only once its end date is a full day behind
    UTC. This is deliberately generous and deliberately timezone-free: an
    exhibition ending today must never read as "past" while it is still on the
    wall somewhere, and nothing here knows which timezone the reader keeps.
    The cost is a show sitting under Coming up for a few hours after it closes,
    which is the harmless direction to be wrong in."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def list_exhibitions():
    """Every show, each with how many paintings are attached and whether it has
    finished. Upcoming first and soonest-first, because a schedule is read
    forwards; everything past follows, most recent first."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM exhibitions").fetchall()]
        counts = {r["exhibition_id"]: r["c"] for r in conn.execute(
            "SELECT exhibition_id, COUNT(*) c FROM exhibition_works "
            "GROUP BY exhibition_id").fetchall()}
    cutoff, now_ = _past_cutoff(), today()
    for e in rows:
        e["count"] = counts.get(e["id"], 0)
        # No end date means a one-day show, so the start date decides.
        end = e["ends_on"] or e["starts_on"] or ""
        e["past"] = bool(end) and end <= cutoff
        e["running"] = (not e["past"] and bool(e["starts_on"])
                        and e["starts_on"] <= now_)
    upcoming = sorted([e for e in rows if not e["past"]],
                      key=lambda e: (e["starts_on"] or "9999-99-99", e["title"]))
    past = sorted([e for e in rows if e["past"]],
                  key=lambda e: (e["ends_on"] or e["starts_on"] or ""), reverse=True)
    return upcoming, past


# ------------------------------------------------------------------- outings
# Somebody else's show, worth going to look at. Deliberately NOT exhibitions
# (that is the record of where her work hung) and not opportunities (nothing
# here is applied to). See the schema note.
def _decorate_outing(o, here, cutoff, now_):
    end = o["ends_on"] or o["starts_on"] or ""
    o["past"] = bool(end) and end <= cutoff
    o["running"] = (not o["past"] and bool(o["starts_on"]) and o["starts_on"] <= now_)
    # How many days until it CLOSES, which is the number that decides whether
    # this weekend is the last chance. Opening day is the wrong deadline: a
    # show that opened a month ago is still there to be seen.
    o["closing_in"] = _days_until(o["ends_on"]) if o["ends_on"] else None
    o["last_chance"] = (o["closing_in"] is not None and 0 <= o["closing_in"] <= 14
                        and not o["past"])
    return _decorate_distance(o, here)


def list_outings():
    """Still on (or still to open) first, closing soonest at the top; then the
    ones that have been and gone.

    Sorted by WHEN IT CLOSES rather than when it opens, because the question a
    list of shows to see actually answers is "what am I about to miss".
    """
    here, cutoff, now_ = studio_point(), _past_cutoff(), today()
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM outings").fetchall()]
    for o in rows:
        _decorate_outing(o, here, cutoff, now_)
    on = sorted([o for o in rows if not o["past"]],
                key=lambda o: (o["ends_on"] or "9999-99-99", o["starts_on"] or "", o["title"]))
    been = sorted([o for o in rows if o["past"]],
                  key=lambda o: (o["ends_on"] or o["starts_on"] or ""), reverse=True)
    return on, been


def get_outing(outing_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM outings WHERE id=?", (outing_id,)).fetchone()
    if row is None:
        return None
    return _decorate_outing(dict(row), studio_point(), _past_cutoff(), today())


def save_outing(data, outing_id=None):
    fields = ["title", "venue", "city", "starts_on", "ends_on", "url", "notes", "went"]
    vals = {k: (data.get(k) if data.get(k) not in ("", None) else None) for k in fields}
    vals["title"] = (data.get("title") or "Untitled show").strip()
    vals["went"] = 1 if data.get("went") else 0
    with connect() as conn:
        if outing_id:
            sets = ", ".join(f"{k}=:{k}" for k in vals)
            conn.execute(f"UPDATE outings SET {sets} WHERE id=:id", {**vals, "id": outing_id})
            return outing_id
        vals["created_at"] = now()
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO outings ({cols}) "
                     f"VALUES ({', '.join(':'+k for k in vals)})", vals)
        return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def set_outing_went(outing_id, went):
    with connect() as conn:
        conn.execute("UPDATE outings SET went=? WHERE id=?", (1 if went else 0, outing_id))


def delete_outing(outing_id):
    with connect() as conn:
        conn.execute("DELETE FROM outings WHERE id=?", (outing_id,))


def outing_place(o):
    """What to hand the geocoder: the venue is only useful WITH a town, and a
    town on its own is a perfectly good answer."""
    bits = [x for x in ((o.get("venue") or "").strip(), (o.get("city") or "").strip()) if x]
    return ", ".join(bits) if len(bits) > 1 else (bits[0] if bits else "")


def locate_outing(outing_id, place=None, force=False):
    """Same contract as locate_opportunity: never raises, remembers a miss.

    THE VENUE IS TRIED FIRST AND THEN DROPPED. "Katonah Museum of Art, Katonah
    NY" puts the pin on the building, which is the better answer -- but half of
    the small galleries worth driving to are not in OpenStreetMap at all, and
    on those the whole lookup was failing and throwing away a perfectly good
    town. A gallery she cannot find on a map is still in a town she can, and
    twenty-eight miles to the town is the right answer to "is this near me".
    """
    with connect() as conn:
        row = conn.execute("SELECT venue, city, geo_query, lat FROM outings WHERE id=?",
                           (outing_id,)).fetchone()
    if row is None:
        return None
    place = place if place is not None else outing_place(dict(row))
    if not force and row["lat"] is not None and (row["geo_query"] or "") == place.strip():
        return None
    if not place.strip():
        with connect() as conn:
            conn.execute("UPDATE outings SET lat=NULL, lon=NULL, geo_query=NULL,"
                         " geo_place=NULL, geocoded_at=NULL WHERE id=?", (outing_id,))
        return None
    # The town on its own is the fallback, and only when it is actually a
    # different question from the one already asked.
    town = (row["city"] or "").strip()
    tries = [place] + ([town] if town and town != place.strip() else [])
    hit = None
    for i, q in enumerate(tries):
        hit = geocode(q)
        if hit:
            break
        if i + 1 < len(tries):
            time.sleep(1.05)      # still one request a second, even retrying
    with connect() as conn:
        if hit:
            conn.execute("UPDATE outings SET lat=?, lon=?, geo_query=?, geo_place=?,"
                         " geocoded_at=? WHERE id=?",
                         (hit[0], hit[1], place.strip(), hit[2], now(), outing_id))
        else:
            conn.execute("UPDATE outings SET lat=NULL, lon=NULL, geo_query=?,"
                         " geo_place=NULL, geocoded_at=? WHERE id=?",
                         (place.strip(), now(), outing_id))
    return hit


def get_exhibition(exhibition_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM exhibitions WHERE id=?",
                           (exhibition_id,)).fetchone()
        if row is None:
            return None
        e = dict(row)
        e["work_ids"] = [r["work_id"] for r in conn.execute(
            "SELECT work_id FROM exhibition_works WHERE exhibition_id=?",
            (exhibition_id,)).fetchall()]
    return e


def save_exhibition(data, exhibition_id=None):
    fields = ["title", "venue", "city", "starts_on", "ends_on", "blurb", "url"]
    vals = {k: (data.get(k) or None) for k in fields}
    vals["title"] = (data.get("title") or "Untitled show").strip()
    with connect() as conn:
        if exhibition_id:
            sets = ", ".join(f"{k}=:{k}" for k in vals)
            conn.execute(f"UPDATE exhibitions SET {sets} WHERE id=:id",
                         {**vals, "id": exhibition_id})
            return exhibition_id
        vals["created_at"] = now()
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO exhibitions ({cols}) "
                     f"VALUES ({', '.join(':'+k for k in vals)})", vals)
        return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def set_exhibition_works(exhibition_id, work_ids):
    """Replace the whole set in one go. The form posts every checkbox that is
    ticked, so what arrives IS the answer.

    It does NOT do that by deleting every row and re-inserting, which is what it
    used to do: those rows now carry the out/back stamps, and wiping them would
    mean that editing the show's title silently threw away the record of which
    paintings had already gone out. Only the rows that are genuinely no longer
    in the show are removed; the rest are left exactly as they are.
    """
    keep = {int(w) for w in work_ids}
    with connect() as conn:
        if keep:
            marks = ",".join("?" * len(keep))
            conn.execute(
                f"DELETE FROM exhibition_works WHERE exhibition_id=? "
                f"AND work_id NOT IN ({marks})", (exhibition_id, *keep))
        else:
            conn.execute("DELETE FROM exhibition_works WHERE exhibition_id=?",
                         (exhibition_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO exhibition_works (exhibition_id, work_id) "
            "VALUES (?,?)", [(exhibition_id, w) for w in keep])
    return True


CHECK_LEGS = {"out": "out_at", "back": "back_at"}


def set_exhibition_check(exhibition_id, work_id, leg, on):
    """Tick or untick one box on a show's checklist. Returns the stamp, or None.

    Ticking is NOT a move: works.location changes only through move_to_place, so
    that the current location and its history can never disagree. A piece being
    marked out for a show and a piece being recorded as at that venue are two
    different statements, and conflating them is how the location history starts
    telling a story nobody typed.
    """
    col = CHECK_LEGS.get(leg)
    if not col:
        raise ValueError("unknown column")
    stamp = now() if on else None
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE exhibition_works SET {col}=? WHERE exhibition_id=? AND work_id=?",
            (stamp, exhibition_id, work_id))
        if not cur.rowcount:
            raise ValueError("that piece is not in this show")
    return stamp


def delete_exhibition(exhibition_id):
    """The show goes, the paintings stay. exhibition_works cascades, and that
    is the only thing that should disappear with it."""
    with connect() as conn:
        conn.execute("DELETE FROM exhibition_works WHERE exhibition_id=?",
                     (exhibition_id,))
        conn.execute("DELETE FROM exhibitions WHERE id=?", (exhibition_id,))
    return True


def exhibition_dates(e):
    """One line for a date range: "12-30 March 2027", or a single day."""
    def parse(v):
        try:
            return datetime.strptime(v, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    a, b = parse(e.get("starts_on")), parse(e.get("ends_on"))
    if not a and not b:
        return "dates not set"
    if a and not b:
        return a.strftime("%-d %b %Y")
    if b and not a:
        return "until " + b.strftime("%-d %b %Y")
    if a == b:
        return a.strftime("%-d %b %Y")
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}\u2013{b.strftime('%-d %b %Y')}"
    if a.year == b.year:
        return f"{a.strftime('%-d %b')}\u2013{b.strftime('%-d %b %Y')}"
    return f"{a.strftime('%-d %b %Y')}\u2013{b.strftime('%-d %b %Y')}"


# ------------------------------------------------------------------ ordering
# The order the paintings hang in is an artistic decision, and it used to be
# made by typing numbers into a `sort` field. These move one item one place and
# leave the arithmetic to the machine.
#
# EVERY MOVE RENUMBERS FIRST. Rows arrive with sort=0 from before this existed,
# and ties break on id, so there is nothing to swap until the current display
# order is written down as 1..N. Renumbering in the SAME order the list is read
# in means the visible order does not change — it just becomes expressible.

def _renumber(conn, table, where, args, order):
    rows = conn.execute("SELECT id FROM %s %s ORDER BY %s" % (table, where, order),
                        args).fetchall()
    for i, r in enumerate(rows, start=1):
        conn.execute("UPDATE %s SET sort=? WHERE id=?" % table, (i, r["id"]))
    return [r["id"] for r in rows]


def _swap(conn, table, ids, item_id, direction):
    """Move one id one place. Returns False at the ends rather than wrapping.

    Wrapping would mean pressing "up" on the first item silently sends it to
    the bottom, which reads as the button having done something random.
    """
    if item_id not in ids:
        return False
    i = ids.index(item_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(ids):
        return False
    conn.execute("UPDATE %s SET sort=? WHERE id=?" % table, (j + 1, ids[i]))
    conn.execute("UPDATE %s SET sort=? WHERE id=?" % table, (i + 1, ids[j]))
    return True


def move_work(work_id, direction):
    """One place up or down the wall.

    Ordered across ALL works, drafts and sold included, rather than within the
    filtered view: there is one wall order and the public lists are windows on
    it. Moving inside a filtered list would reshuffle pieces the person moving
    them cannot see.
    """
    with connect() as conn:
        ids = _renumber(conn, "works", "", (), "sort, id DESC")
        return _swap(conn, "works", ids, work_id, direction)


def move_image(image_id, direction):
    """One place through a work's photographs.

    The FIRST photograph is the one the wall, the archive and the share card
    all use, so this is also how the lead image is chosen — which is why it
    matters that it is not "delete everything and re-upload in order".
    """
    with connect() as conn:
        row = conn.execute("SELECT work_id FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None:
            return False
        ids = _renumber(conn, "images", "WHERE work_id=?", (row["work_id"],), "sort, id")
        return _swap(conn, "images", ids, image_id, direction)


def set_status(work_id, status):
    with connect() as conn:
        conn.execute(
            "UPDATE works SET status=?, sold_at=CASE WHEN ?='sold' THEN ? ELSE NULL END,"
            " reserved_until=NULL, updated_at=? WHERE id=?",
            (status, status, now(), now(), work_id))


def reserve(work_id, minutes=30):
    """Hold a work while a checkout is open. Returns False if it is not free."""
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE works SET status='reserved', reserved_until=?, updated_at=? "
            "WHERE id=? AND status='available'", (until, now(), work_id))
        return cur.rowcount == 1


def release_expired():
    """An abandoned checkout must not retire a painting for good."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE works SET status='available', reserved_until=NULL "
            "WHERE status='reserved' AND reserved_until IS NOT NULL AND reserved_until < ?",
            (now(),))
        return cur.rowcount


# -------------------------------------------------------------------- images
MAGIC = {b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png", b"RIFF": "webp"}


def sniff(data):
    """Trust the bytes, not the browser's content-type."""
    for magic, kind in MAGIC.items():
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def store_photo(data):
    """Write the three widths and return the base stem. No database row --
    the hero photo on the landing page is not attached to a work."""
    if sniff(data) is None:
        raise ValueError("That file is not a JPEG, PNG or WebP.")
    im = Image.open(io.BytesIO(data))
    # EXIF holds the orientation AND, on a phone, the GPS of the studio.
    # exif_transpose applies the first; converting drops the rest.
    im = ImageOps.exif_transpose(im).convert("RGB")
    base = secrets.token_hex(8)
    for suffix, width in SIZES:
        out = im.copy()
        if out.width > width:
            out.thumbnail((width, width * 4), Image.LANCZOS)
        out.save(os.path.join(PHOTO_DIR, f"{base}-{suffix}.webp"), "WEBP", quality=86, method=5)
        out.save(os.path.join(PHOTO_DIR, f"{base}-{suffix}.jpg"), "JPEG", quality=88, optimize=True)
    return base


def add_image(work_id, data, kind="full", alt=None):
    base = store_photo(data)
    with connect() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort),0)+1 AS n FROM images WHERE work_id=?", (work_id,)).fetchone()["n"]
        conn.execute("INSERT INTO images (work_id, base, kind, alt, sort) VALUES (?,?,?,?,?)",
                     (work_id, base, kind, alt, nxt))
    return base


def drop_image_files(base):
    for suffix, _ in SIZES:
        for ext in ("webp", "jpg"):
            p = os.path.join(PHOTO_DIR, f"{base}-{suffix}.{ext}")
            if os.path.exists(p):
                os.remove(p)


def delete_image(image_id):
    with connect() as conn:
        row = conn.execute("SELECT base FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    drop_image_files(row["base"])
    return True


# -------------------------------------------------------- orders & messages
def record_order(work_id, session_id, amount_cents, buyer, ship):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orders (work_id, stripe_session_id, amount_cents,"
            " buyer_name, buyer_email, ship_line1, ship_line2, ship_city, ship_state,"
            " ship_zip, ship_country, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, session_id, amount_cents, buyer.get("name"), buyer.get("email"),
             ship.get("line1"), ship.get("line2"), ship.get("city"), ship.get("state"),
             ship.get("postal_code"), ship.get("country"), now()))


def list_orders():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT o.*, w.title, w.slug FROM orders o LEFT JOIN works w ON w.id=o.work_id"
            " ORDER BY o.created_at DESC").fetchall()]


def mark_shipped(order_id, tracking=None):
    with connect() as conn:
        conn.execute("UPDATE orders SET status='shipped', tracking=? WHERE id=?",
                     (tracking or None, order_id))


def add_inquiry(kind, name, email, body, work_id=None):
    with connect() as conn:
        conn.execute("INSERT INTO inquiries (kind,name,email,body,work_id,created_at)"
                     " VALUES (?,?,?,?,?,?)", (kind, name, email, body, work_id, now()))


def list_inquiries():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT i.*, w.title FROM inquiries i LEFT JOIN works w ON w.id=i.work_id"
            " ORDER BY i.created_at DESC LIMIT 300").fetchall()]


def handle_inquiry(inq_id):
    with connect() as conn:
        conn.execute("UPDATE inquiries SET handled=1 WHERE id=?", (inq_id,))


def delete_inquiry(inq_id):
    """Gone for good. Unlike an address on the mailing list there is nothing to
    remember here -- junk should actually leave, and a message that mattered
    was answered from her own mail client long before this."""
    with connect() as conn:
        conn.execute("DELETE FROM inquiries WHERE id=?", (inq_id,))
    return True


# ---------------------------------------------------------------- replies
# The outgoing half of the Messages section. An enquiry used to end at a
# mailto: link, which meant the answer happened somewhere this app could not
# see and there was no record here of what was said or whether it was said at
# all. These three folders -- inbox, drafts, sent -- keep the whole exchange in
# one place, which is the whole point of the change.

REPLY_STATUSES = ("draft", "sent")


def get_inquiry(inq_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT i.*, w.title FROM inquiries i LEFT JOIN works w ON w.id=i.work_id"
            " WHERE i.id=?", (inq_id,)).fetchone()
    return dict(row) if row else None


def list_replies(status=None, limit=300):
    sql = ("SELECT r.*, i.name AS inq_name, i.kind AS inq_kind FROM replies r"
           " LEFT JOIN inquiries i ON i.id=r.inquiry_id")
    args = []
    if status:
        sql += " WHERE r.status=?"
        args.append(status)
    # Drafts sort by when they were last touched; sent mail by when it went.
    sql += " ORDER BY COALESCE(r.sent_at, r.updated_at) DESC, r.id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_reply(rid):
    with connect() as conn:
        row = conn.execute("SELECT * FROM replies WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def replies_for_inquiry(inq_id):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM replies WHERE inquiry_id=? ORDER BY id", (inq_id,)).fetchall()]


def save_reply(rid, inquiry_id, to_email, to_name, subject, body):
    """Create or update a draft. Returns its id.

    A sent reply is never rewritten: once it has left, the row is the record of
    what was actually said, and editing it would quietly make that record a lie.
    """
    to_email = (to_email or "").strip()
    if "@" not in to_email:
        raise ValueError("that doesn't look like an email address")
    stamp = now()
    with connect() as conn:
        if rid:
            row = conn.execute("SELECT status FROM replies WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ValueError("that draft is gone")
            if row["status"] == "sent":
                raise ValueError("that message has already been sent")
            conn.execute(
                "UPDATE replies SET inquiry_id=?, to_email=?, to_name=?, subject=?,"
                " body=?, updated_at=?, error=NULL WHERE id=?",
                (inquiry_id, to_email, to_name, subject, body, stamp, rid))
            return rid
        cur = conn.execute(
            "INSERT INTO replies (inquiry_id,to_email,to_name,subject,body,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,'draft',?,?)",
            (inquiry_id, to_email, to_name, subject, body, stamp, stamp))
        return cur.lastrowid


def mark_reply_sent(rid):
    stamp = now()
    with connect() as conn:
        conn.execute(
            "UPDATE replies SET status='sent', sent_at=?, updated_at=?, error=NULL"
            " WHERE id=?", (stamp, stamp, rid))
        row = conn.execute("SELECT inquiry_id FROM replies WHERE id=?", (rid,)).fetchone()
        # Answering something IS handling it. Making her then tick a second box
        # is how an inbox fills with messages she has already dealt with.
        if row and row["inquiry_id"]:
            conn.execute("UPDATE inquiries SET handled=1 WHERE id=?", (row["inquiry_id"],))


def mark_reply_failed(rid, error):
    """Sending failed, so the row stays a draft and says why.

    The alternative -- marking it sent and hoping -- is the worst outcome
    available: she would believe she had answered somebody and never find out
    otherwise.
    """
    with connect() as conn:
        conn.execute("UPDATE replies SET status='draft', error=?, updated_at=?"
                     " WHERE id=?", ((error or "")[:300], now(), rid))


def delete_reply(rid):
    with connect() as conn:
        conn.execute("DELETE FROM replies WHERE id=?", (rid,))
    return True


def message_counts():
    with connect() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]
        return {
            "inbox": q("SELECT COUNT(*) FROM inquiries WHERE handled=0"),
            "inbox_all": q("SELECT COUNT(*) FROM inquiries"),
            "drafts": q("SELECT COUNT(*) FROM replies WHERE status='draft'"),
            "sent": q("SELECT COUNT(*) FROM replies WHERE status='sent'"),
        }


def subscribe(email, source="site"):
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 200:
        return False
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO subscribers (email, source, created_at)"
                     " VALUES (?,?,?)", (email, source, now()))
        # Somebody who comes back and signs up again is giving consent again,
        # and that outranks an old suppression. This is the ONLY thing that
        # clears the stamp automatically -- a bulk import must never do it.
        conn.execute("UPDATE subscribers SET unsubscribed_at=NULL WHERE email=?",
                     (email,))
    return True


def add_contact(email, source="message"):
    """Put an address on the Contacts list from the STUDIO rather than from the
    sign-up form on the site.

    Returns "added", "already", "removed" or "bad", because the studio has to be
    able to say which of those happened.

    Deliberately not `subscribe()`: that one clears an unsubscribe stamp, on the
    grounds that a person signing up again is consenting again. Nobody can
    consent on somebody else's behalf, so an address that asked to come off the
    list STAYS off and this reports it rather than quietly resurrecting it."""
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 200:
        return "bad"
    with connect() as conn:
        row = conn.execute("SELECT unsubscribed_at FROM subscribers WHERE email=?",
                           (email,)).fetchone()
        if row:
            return "removed" if row["unsubscribed_at"] else "already"
        conn.execute("INSERT INTO subscribers (email, source, created_at)"
                     " VALUES (?,?,?)", (email, source, now()))
    return "added"


def contacts_index():
    """{address: on the list?} -- so a list of messages can say which senders
    are already contacts without a query per row."""
    with connect() as conn:
        return {r["email"]: not r["unsubscribed_at"] for r in
                conn.execute("SELECT email, unsubscribed_at FROM subscribers")}


def list_subscribers(include_removed=True):
    """Everyone, newest first. Removed addresses come back too unless asked
    otherwise, because the studio page shows them rather than pretending they
    were never there."""
    sql = "SELECT * FROM subscribers"
    if not include_removed:
        sql += " WHERE unsubscribed_at IS NULL"
    sql += " ORDER BY created_at DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def delete_subscriber(sub_id):
    """Forget an address completely. Only one already OFF the list.

    Deliberately a second step, not an alternative to removing: the row is what
    remembers that this person asked to be left alone, so deleting it is the
    one thing that makes them addable again -- by an import, by the button on a
    message, by anything. Taking off the list is the safe act; this is for junk
    that was never a real person. Refuses a live subscriber outright rather
    than silently doing both at once."""
    with connect() as conn:
        row = conn.execute("SELECT unsubscribed_at FROM subscribers WHERE id=?",
                           (sub_id,)).fetchone()
        if not row or not row["unsubscribed_at"]:
            return False
        conn.execute("DELETE FROM subscribers WHERE id=?", (sub_id,))
    return True


def unsubscribe(sub_id, removed=True):
    """Take an address off the list, or put it back.

    The row survives either way. Anything that mails people -- the export, the
    copy-all box, the count -- reads the ones with no stamp, so a suppression
    is honoured everywhere at once rather than in each place separately."""
    with connect() as conn:
        conn.execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?",
                     (now() if removed else None, sub_id))
    return True


# --------------------------------------------------------------- opportunities
# Calls for entry, grants, residencies, fairs. See the comment in schema.sql
# for why this is separate from exhibitions.

OPP_KINDS = ["show", "grant", "residency", "fair", "other"]
OPP_KIND_LABELS = {"show": "juried show", "grant": "grant", "residency": "residency",
                   "fair": "art fair", "other": "other"}
OPP_STATUSES = ["watching", "applied", "accepted", "declined", "passed"]
OPP_STATUS_LABELS = {"watching": "watching", "applied": "applied",
                     "accepted": "accepted", "declined": "not accepted",
                     "passed": "passed on it"}
# A call is OPEN until its deadline has fully passed, same generous cutoff and
# same reasoning as _past_cutoff() for shows: nothing here knows her timezone,
# and a deadline reading "closed" on the morning it is actually still open is
# the expensive direction to be wrong in.
OPP_OPEN_STATUSES = ("watching", "applied", "accepted")


def opp_kind_label(k):
    return OPP_KIND_LABELS.get(k, k)


def opp_status_label(s):
    return OPP_STATUS_LABELS.get(s, s)


def _days_until(date_str):
    """Whole days from today to a YYYY-MM-DD, or None if it is not a date."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - datetime.now(timezone.utc).date()).days


# ------------------------------------------------------------------ distance
# Nominatim is OpenStreetMap's own geocoder: free, no key, and it asks two
# things in return -- a User-Agent that identifies the caller, and no more than
# one request a second. Both are honoured here. Nothing on a public page ever
# calls this; it runs when SHE saves a row, which is a human-paced action.
GEO_UA = "artbymccarthy.com opportunity map (contact: paull1967@gmail.com)"
GEO_URL = "https://nominatim.openstreetmap.org/search"


def geocode(place):
    """(lat, lon, matched_name) for a place name, or None. Never raises.

    A geocoder that throws takes the save with it, and she would lose the row
    she just typed because a third party was slow. Every failure here is a
    missing distance and nothing else.
    """
    import urllib.parse, urllib.request
    q = (place or "").strip()
    if not q:
        return None
    url = GEO_URL + "?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "addressdetails": 0})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": GEO_UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not rows:
        return None
    try:
        return float(rows[0]["lat"]), float(rows[0]["lon"]), rows[0].get("display_name") or q
    except (KeyError, TypeError, ValueError):
        return None


def miles_between(lat1, lon1, lat2, lon2):
    """Great-circle miles. Straight-line, and the screen says so -- turning it
    into driving distance means a routing service and a key, and for "is this
    worth the trip" the crow's flight is the right resolution anyway."""
    import math
    R = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def studio_point():
    """(lat, lon) of the studio, or None if she has not set a location."""
    c = settings()
    try:
        return float(c.get("studio_lat")), float(c.get("studio_lon"))
    except (TypeError, ValueError):
        return None


def locate_studio(place):
    """Geocode the studio address and store it. Returns the matched name."""
    hit = geocode(place)
    if not hit:
        save_settings({"studio_location": place or "", "studio_lat": "",
                       "studio_lon": "", "studio_place": ""})
        return None
    lat, lon, name = hit
    save_settings({"studio_location": place or "", "studio_lat": str(lat),
                   "studio_lon": str(lon), "studio_place": name})
    return name


def locate_opportunity(opportunity_id, place, force=False):
    """Geocode one call's location, unless the same text is already looked up.

    `geo_query` is the guard: re-saving a row without touching its location
    must not spend another request, and correcting a typo must.
    """
    with connect() as conn:
        row = conn.execute("SELECT location, geo_query, lat FROM opportunities "
                           "WHERE id=?", (opportunity_id,)).fetchone()
    if row is None:
        return None
    place = (place if place is not None else row["location"]) or ""
    if not force and row["lat"] is not None and (row["geo_query"] or "") == place.strip():
        return None
    if not place.strip():
        with connect() as conn:
            conn.execute("UPDATE opportunities SET lat=NULL, lon=NULL, geo_query=NULL,"
                         " geo_place=NULL, geocoded_at=NULL WHERE id=?", (opportunity_id,))
        return None
    hit = geocode(place)
    with connect() as conn:
        if hit:
            conn.execute("UPDATE opportunities SET lat=?, lon=?, geo_query=?, geo_place=?,"
                         " geocoded_at=? WHERE id=?",
                         (hit[0], hit[1], place.strip(), hit[2], now(), opportunity_id))
        else:
            # Remember the miss too, or every page load retries a place name
            # that Nominatim does not know.
            conn.execute("UPDATE opportunities SET lat=NULL, lon=NULL, geo_query=?,"
                         " geo_place=NULL, geocoded_at=? WHERE id=?",
                         (place.strip(), now(), opportunity_id))
    return hit


def ungeocoded_opportunities():
    """Rows with a location typed but no coordinates worked out yet."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, location FROM opportunities"
            " WHERE location IS NOT NULL AND TRIM(location) <> ''"
            "   AND lat IS NULL"
            "   AND (geo_query IS NULL OR TRIM(geo_query) <> TRIM(location))"
            " ORDER BY id").fetchall()]


# ------------------------------------------------------------ where to look
# The calls-for-entry world publishes no feed, and paying a model to read the
# web for her turned out to cost more than the answers were worth -- $1.14 for
# four calls, every one of them already closed. So this is the honest version
# of "find me calls": the free listing sites, with her own state and town
# already in the query, opened in a tab. She reads them and adds what suits.
#
# She has to open the call's own page to check the deadline before applying
# whatever finds it, so the only thing the model was really saving was the
# reading -- and that is not worth a dollar a press.
def where_to_look(cfg):
    import urllib.parse
    town = (cfg.get("studio_location") or "").strip()
    bits = [b.strip() for b in town.split(",") if b.strip()]
    state = (bits[1] if len(bits) > 1 else "").upper()[:2]
    year = datetime.now(timezone.utc).year
    out = []
    # EntryThingy aggregates CaFE, ZAPPlication, ArtCall, ShowSubmit and the
    # independents, free to browse, and its list takes a state in the URL --
    # which is the closest thing to "near me" any of these offer.
    if state:
        out.append(("Open calls in " + state,
                    "app.entrythingy.com/calls_list/?state=" + state,
                    "Every call they list in your state, updated daily."))
    # Bedford is nine miles from Connecticut, so the neighbouring state is not
    # a nicety. Skipped when she is already in it.
    for nb in ("CT", "NJ", "MA"):
        if state and nb != state and state in ("NY", "CT", "NJ", "MA"):
            out.append(("Open calls in " + nb,
                        "app.entrythingy.com/calls_list/?state=" + nb,
                        "The next state over."))
            break
    out.append(("CaF\u00c9",
                "www.callforentry.org/",
                "The big one. Browsing is free; applying through it is not."))
    out.append(("NYFA Opportunities",
                "www.nyfa.org/opportunities/",
                "Open calls, residencies and grants, heavy on New York."))
    if town:
        q = '"call for entry" OR "call for artists" juried exhibition %s %s' % (town, year)
        out.append(("Search the web for " + town,
                    "www.google.com/search?q=" + urllib.parse.quote(q),
                    "A plain web search, already written."))
    return out


def _decorate_opp(o, counts=None):
    o["days"] = _days_until(o.get("deadline"))
    # Closed means the deadline is behind us, regardless of what she did about
    # it. Status says what happened; this says whether she can still act.
    o["closed"] = o["days"] is not None and o["days"] < 0
    o["fee"] = money(o.get("fee_cents"))
    # Urgency for the list, computed once here so the template does no
    # arithmetic: a deadline is either gone, this week, this month, or later.
    if o["days"] is None:
        o["urgency"] = "undated"
    elif o["days"] < 0:
        o["urgency"] = "gone"
    elif o["days"] <= 7:
        o["urgency"] = "soon"
    elif o["days"] <= 30:
        o["urgency"] = "month"
    else:
        o["urgency"] = "later"
    if counts is not None:
        o["count"] = counts.get(o["id"], 0)
    return o


def _decorate_distance(o, here):
    """How far this call is from the studio, if both ends are known."""
    o["miles"] = None
    o["near"] = None
    if here and o.get("lat") is not None and o.get("lon") is not None:
        m = miles_between(here[0], here[1], o["lat"], o["lon"])
        o["miles"] = int(round(m))
        # Named bands rather than a raw number alone: the question is never
        # "how many miles", it is "can I drive there and back in a day".
        o["near"] = "here" if m <= 25 else "close" if m <= 75 else \
                    "day" if m <= 200 else "far"
    return o


def list_opportunities(include_done=True, within=None, order="deadline"):
    """Everything, split into what is still live and what is finished.

    Live is sorted by DEADLINE ASCENDING and that is the whole point of the
    screen -- the next thing she has to act on is the first thing she reads.
    Undated calls sort last within live rather than first, because a call with
    no deadline is never the urgent one.

    `within` is a radius in miles and filters the LIVE list only: what is
    already closed is a record, and a record does not get filtered by how far
    away it was. A call with no location survives the filter rather than being
    hidden -- a grant or an online call has no distance to fail, and dropping
    those would quietly empty the screen.
    """
    here = studio_point()
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM opportunities").fetchall()]
        counts = {r["opportunity_id"]: r["c"] for r in conn.execute(
            "SELECT opportunity_id, COUNT(*) c FROM opportunity_works "
            "GROUP BY opportunity_id").fetchall()}
    for o in rows:
        _decorate_distance(_decorate_opp(o, counts), here)
    live = [o for o in rows
            if not o["closed"] and o["status"] in OPP_OPEN_STATUSES]
    if within:
        live = [o for o in live if o["miles"] is None or o["miles"] <= within]
    live_ids = {o["id"] for o in live}
    done = [o for o in rows if o["id"] not in live_ids]
    if order == "distance" and here:
        # Undated and unplaced both sort last, for the same reason: they are
        # the rows that cannot answer the question the sort was asked.
        live.sort(key=lambda o: (o["miles"] is None, o["miles"] or 0,
                                 o["deadline"] or "9999-99-99"))
    else:
        live.sort(key=lambda o: (o["deadline"] or "9999-99-99", o["title"]))
    done.sort(key=lambda o: (o["deadline"] or "", o["title"]), reverse=True)
    return live, (done if include_done else [])


def due_soon(days=14):
    """Live calls whose deadline falls inside the next `days`, soonest first.

    Feeds both the banner in the studio and the digest email, so the two can
    never disagree about what is urgent."""
    live, _ = list_opportunities(include_done=False)
    return [o for o in live
            if o["days"] is not None and 0 <= o["days"] <= days
            and o["status"] == "watching"]


def get_opportunity(opportunity_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE id=?",
                           (opportunity_id,)).fetchone()
        if row is None:
            return None
        o = _decorate_distance(_decorate_opp(dict(row)), studio_point())
        o["work_ids"] = [r["work_id"] for r in conn.execute(
            "SELECT work_id FROM opportunity_works WHERE opportunity_id=?",
            (opportunity_id,)).fetchall()]
    return o


def save_opportunity(data, opportunity_id=None):
    fields = ["title", "org", "kind", "url", "location", "fee_cents", "opens_on",
              "deadline", "notified_on", "event_on", "event_ends", "max_works",
              "img_longest", "img_max_mb", "notes", "status", "applied_on"]
    vals = {k: (data.get(k) if data.get(k) not in ("", None) else None) for k in fields}
    vals["title"] = (data.get("title") or "Untitled call").strip()
    if vals["kind"] not in OPP_KINDS:
        vals["kind"] = "show"
    if vals["status"] not in OPP_STATUSES:
        vals["status"] = "watching"
    # Applying is a date, and she should not have to remember to type it. Stamp
    # it the first time the status becomes applied and leave it alone after --
    # editing the row later must not move the date she actually sent it.
    if vals["status"] == "applied" and not vals["applied_on"]:
        vals["applied_on"] = today()
    with connect() as conn:
        if opportunity_id:
            sets = ", ".join(f"{k}=:{k}" for k in vals)
            conn.execute(f"UPDATE opportunities SET {sets} WHERE id=:id",
                         {**vals, "id": opportunity_id})
            return opportunity_id
        vals["created_at"] = now()
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO opportunities ({cols}) "
                     f"VALUES ({', '.join(':'+k for k in vals)})", vals)
        return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def set_opportunity_works(opportunity_id, work_ids):
    with connect() as conn:
        conn.execute("DELETE FROM opportunity_works WHERE opportunity_id=?",
                     (opportunity_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO opportunity_works (opportunity_id, work_id) "
            "VALUES (?,?)", [(opportunity_id, int(w)) for w in work_ids])
    return True


def add_opportunity_works(opportunity_id, work_ids):
    """Add without clearing -- what the packet builder does when it records a
    submission, because building a second packet for the same call must not
    erase the first one."""
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO opportunity_works (opportunity_id, work_id) "
            "VALUES (?,?)", [(opportunity_id, int(w)) for w in work_ids])
    return True


def delete_opportunity(opportunity_id):
    with connect() as conn:
        conn.execute("DELETE FROM opportunity_works WHERE opportunity_id=?",
                     (opportunity_id,))
        conn.execute("DELETE FROM opportunities WHERE id=?", (opportunity_id,))
    return True


def works_in_opportunity(opportunity_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT w.* FROM works w JOIN opportunity_works ow ON ow.work_id=w.id "
            "WHERE ow.opportunity_id=? ORDER BY w.sort, w.id", (opportunity_id,)).fetchall()
        return _hydrate(conn, rows)


def submissions_for(work_id):
    """Where this painting has been sent, newest deadline first. Shown on the
    work's own page so she can see it was already declined somewhere before
    sending it there again."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT o.* FROM opportunities o JOIN opportunity_works ow ON ow.opportunity_id=o.id "
            "WHERE ow.work_id=? ORDER BY COALESCE(o.deadline,'') DESC, o.id DESC",
            (work_id,)).fetchall()]
    return [_decorate_opp(o) for o in rows]


def claim_reminder_day(day_str=None):
    """True exactly once per day, for whichever worker gets there first.

    The digest is sent from an ordinary web request -- there is no scheduler on
    this host -- so two gunicorn workers can reach this in the same instant.
    The UPDATE is conditional on the stored value still being yesterday's, and
    SQLite applies it atomically, so the loser sees rowcount 0 and sends
    nothing. Without that guard a busy morning is a mailbox full of duplicates.
    """
    d = day_str or today()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE settings SET value=? WHERE key='opps_reminded_on' AND COALESCE(value,'')<>?",
            (d, d))
        if cur.rowcount:
            return True
        # First run ever: the key may not be in the table yet.
        row = conn.execute("SELECT value FROM settings WHERE key='opps_reminded_on'").fetchone()
        if row is None:
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('opps_reminded_on',?)", (d,))
            return True
    return False


# ------------------------------------------------------------ submission packet
# Every call states an image spec -- "1920px on the longest side, JPEG, under
# 5MB, named Lastname_Title.jpg" -- and no two state the same one. Meeting it
# by hand means opening a folder of photographs, resizing each, checking the
# file size, renaming, and then typing the same titles and dimensions into an
# image list. The app already holds the photographs and every one of those
# facts, so it can simply produce the folder.

# What is on disk is 600 / 1400 / 2400 wide (SIZES). The largest is the source
# for a packet; the original upload is not kept.
PACKET_SOURCES = [("l", "jpg"), ("m", "jpg"), ("s", "jpg")]
# Tried in order until the file fits the call's ceiling. Stops at 64 rather
# than grinding down to a smeared 40: past that point the right answer is a
# smaller pixel dimension, not more compression.
PACKET_QUALITY = [92, 86, 80, 72, 64]

NAME_PATTERNS = {
    "last_title":      "Lastname_Title.jpg",
    "last_title_year": "Lastname_Title_Year.jpg",
    "num_last_title":  "01_Lastname_Title.jpg",
    "title":           "Title.jpg",
}


def _ascii_token(s):
    """A filename fragment that survives every upload form on the internet.

    Jurors' systems mangle accents and spaces, and some reject them outright,
    so the title is flattened to ASCII words joined by nothing. Deliberately
    lossy -- the readable title travels in the image list, not the filename.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    # Upper-case the first letter and LEAVE THE REST ALONE. str.capitalize()
    # would lower-case the rest, which turns McCarthy into Mccarthy -- her own
    # name, misspelt, on every file a juror opens.
    parts = [p[0].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", s) if p]
    return "".join(parts) or "Untitled"


def last_name(full_name):
    return _ascii_token((full_name or "").split()[-1] if (full_name or "").split() else "Artist")


def packet_filename(work, artist, pattern="last_title", n=1):
    last = last_name(artist)
    title = _ascii_token(work.get("title"))
    year = work.get("year")
    if pattern == "title":
        stem = title
    elif pattern == "last_title_year":
        stem = f"{last}_{title}" + (f"_{year}" if year else "")
    elif pattern == "num_last_title":
        stem = f"{n:02d}_{last}_{title}"
    else:
        stem = f"{last}_{title}"
    return stem + ".jpg"


def _source_path(base):
    for suffix, ext in PACKET_SOURCES:
        p = os.path.join(PHOTO_DIR, f"{base}-{suffix}.{ext}")
        if os.path.exists(p):
            return p
    return None


def packet_image(base, longest=1920, max_bytes=None):
    """One JPEG to a call's spec. Returns (bytes, width, height, quality).

    NEVER UPSCALES. If the stored copy is smaller than the call asks for, the
    smaller file goes -- padding it with invented pixels would look worse on a
    juror's screen and is not what "1920px maximum" asks for anyway. The
    caller is told the real dimensions so it can say so.
    """
    path = _source_path(base)
    if not path:
        return None
    src = Image.open(path).convert("RGB")
    cap = longest or max(src.size)
    out = None
    # Quality first, pixels second. Compression is the cheap lever and a juror
    # sees the image at screen size anyway; only when the ladder bottoms out
    # does the picture actually have to get smaller. THE CEILING IS HONOURED --
    # a file over the stated limit is rejected by the upload form, and finding
    # that out at the deadline is the failure this whole feature exists to stop.
    for attempt in range(6):
        im = src.copy()
        if max(im.size) > cap:
            im.thumbnail((cap, cap), Image.LANCZOS)
        for q in PACKET_QUALITY:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
            out, used, w, h = buf.getvalue(), q, im.width, im.height
            if not max_bytes or len(out) <= max_bytes:
                return out, w, h, used
        cap = int(cap * 0.75)
        if cap < 400:
            break
    return out, w, h, used


def _packet_row(w, filename, px):
    """One line of the image list, in the order a call form asks for it."""
    return {
        "file": filename,
        "title": w.get("title") or "Untitled",
        "year": w.get("year") or "",
        "medium": w.get("medium") or "",
        "dimensions": dims(w) or "",
        "price": money(w.get("price_cents")) or "",
        "pixels": px,
    }


def build_packet(works, artist, pattern="last_title", longest=1920, max_mb=5.0,
                 statement="", bio="", title_line="", note=""):
    """Write the whole submission folder into a zip and return (fileobj, rows,
    skipped). The caller streams it; nothing is kept on disk.

    Built into a TemporaryFile rather than memory: twenty paintings at 1920px
    is tens of megabytes, and this runs on a small instance beside everything
    else the site is doing.
    """
    max_bytes = int(max_mb * 1024 * 1024) if max_mb else None
    tmp = tempfile.TemporaryFile()
    rows, skipped = [], []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        n = 0
        for w in works:
            if not w.get("images"):
                # A painting with no photograph cannot be submitted, and
                # silently leaving it out is how she finds out at the deadline.
                skipped.append(w.get("title") or "Untitled")
                continue
            n += 1
            made = packet_image(w["images"][0]["base"], longest, max_bytes)
            if not made:
                skipped.append(w.get("title") or "Untitled")
                n -= 1
                continue
            data, iw, ih, _q = made
            fn = packet_filename(w, artist, pattern, n)
            z.writestr(fn, data)
            rows.append(_packet_row(w, fn, f"{iw}×{ih}"))
        if rows:
            z.writestr("image-list.csv", _packet_csv(rows))
            z.writestr("image-list.txt", _packet_txt(rows, artist, title_line, note))
        if statement.strip():
            z.writestr("artist-statement.txt", statement.strip() + "\n")
        if bio.strip():
            z.writestr("artist-bio.txt", bio.strip() + "\n")
    tmp.seek(0)
    return tmp, rows, skipped


def _packet_csv(rows):
    import csv as _csv
    buf = io.StringIO()
    cols = ["file", "title", "year", "medium", "dimensions", "price", "pixels"]
    wr = _csv.DictWriter(buf, fieldnames=cols)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r[k] for k in cols})
    return buf.getvalue()


def _packet_txt(rows, artist, title_line="", note=""):
    """The same list as prose, because half of these forms want it pasted into
    a textarea rather than uploaded as a file."""
    out = []
    if artist:
        out.append(artist)
    if title_line:
        out.append(title_line)
    out.append("")
    for i, r in enumerate(rows, 1):
        bits = [r["title"]]
        if r["year"]:
            bits.append(str(r["year"]))
        if r["medium"]:
            bits.append(r["medium"])
        if r["dimensions"]:
            bits.append(r["dimensions"])
        if r["price"]:
            bits.append(r["price"])
        out.append(f"{i}. " + ", ".join(bits))
        out.append(f"   {r['file']}  ({r['pixels']})")
    if note:
        out += ["", note]
    return "\n".join(out) + "\n"
