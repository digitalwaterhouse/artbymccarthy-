"""Data layer for the gallery: works, images, orders, inquiries, list.

One painting is one row and the quantity is always one, so nothing here
counts stock -- the whole question is what state a work is in, which is why
status carries reserved/sold rather than a number going up and down.
"""
import os
import re
import io
import json
import sqlite3
import secrets
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
    # quiet | rosette | rose | none -- the backdrop on about/commissions/contact
    "page_bg": "quiet",
    # Printed on wall labels and checklists. A setting, not a constant: she
    # writes it "Lisa Mc Carthy" with a space in her own bio and "McCarthy"
    # everywhere else on the site, and that is hers to settle, not mine.
    "artist_name": "Lisa McCarthy",
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
             conn.execute("SELECT id, slug, name FROM collections").fetchall()}
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
        sql += " AND collection_id=?"
        args.append(collection_id)
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
            "SELECT w.* FROM works w JOIN exhibition_works x ON x.work_id = w.id"
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
# for why this is not a tag list.

def list_collections(with_counts=True):
    """Collections in hanging order, each with how many pieces are in it.

    The count is of PUBLIC pieces: a collection whose only members are still
    drafts reads as empty on the site, and the studio list should say the same
    number the visitor will see rather than a more flattering one."""
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
    return rows


def get_collection(slug=None, collection_id=None):
    with connect() as conn:
        if slug is not None:
            row = conn.execute("SELECT * FROM collections WHERE slug=?", (slug,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM collections WHERE id=?",
                               (collection_id,)).fetchone()
    return dict(row) if row else None


def save_collection(data, collection_id=None):
    vals = {"name": (data.get("name") or "").strip(),
            "blurb": data.get("blurb") or "",
            "sort": data.get("sort") or 0}
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


def delete_collection(collection_id):
    """Delete the collection, keep the paintings.

    The pieces are detached rather than deleted -- the same reasoning as the
    inquiries in delete_work. Nobody types "delete the Box Series" meaning
    "delete the eleven paintings in it"."""
    with connect() as conn:
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
    ticked, so what arrives IS the answer -- working out which ones changed
    would only invent a way to get it wrong."""
    with connect() as conn:
        conn.execute("DELETE FROM exhibition_works WHERE exhibition_id=?",
                     (exhibition_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO exhibition_works (exhibition_id, work_id) "
            "VALUES (?,?)", [(exhibition_id, int(w)) for w in work_ids])
    return True


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


def unsubscribe(sub_id, removed=True):
    """Take an address off the list, or put it back.

    The row survives either way. Anything that mails people -- the export, the
    copy-all box, the count -- reads the ones with no stamp, so a suppression
    is honoured everywhere at once rather than in each place separately."""
    with connect() as conn:
        conn.execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?",
                     (now() if removed else None, sub_id))
    return True
