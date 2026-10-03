"""Art by McCarthy -- a small gallery shop.

Runs behind Apache on a subpath (APP_PREFIX), which is why every route hangs
off a blueprint rather than off the app: moving to the artist's own domain
later means setting APP_PREFIX="" and repointing the proxy, nothing else.
"""
import os
import re
import csv
import json
import io
import time
import functools
import hashlib
import secrets
import threading
from datetime import datetime, timedelta, timezone

from flask import (Flask, Blueprint, render_template, request, redirect,
                   has_request_context, g,
                   url_for, session, abort, send_from_directory, jsonify,
                   Response, flash, send_file)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix

import gallery
import mailer
import payments
import listings

PREFIX = os.environ.get("APP_PREFIX", "/artbymccarthy").rstrip("/")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
PORT = int(os.environ.get("PORT", "5070"))
# While the shop lives on a borrowed path it must not be indexed under
# somebody else's domain -- the site's own robots.txt sits at the domain
# root, out of this app's reach, so the meta tag is the only lever here.
NOINDEX = os.environ.get("NOINDEX", "1") == "1"
# Set on a staging copy and nowhere else. Everything it switches on is a
# WARNING, never a behaviour change: the code that runs on the copy has to be
# the code that runs on the real thing or the copy proves nothing. The one
# exception is mail, which is refused outright -- see mailer.py.
ENV_NAME = os.environ.get("ENV_NAME", "").strip()

# The landing headline when she has not set one in the admin. It used to fall
# through to `tagline`, which is "Original Works" -- a category label, and the
# first words on the site said nothing about the work. This is HER phrase,
# taken from her own bio ("kept safe in black and white treasure boxes"), with
# only the first letter raised for a headline. It names the object, which is
# the thing the photographs cannot do: these are shadow boxes, not pictures.
# `hero_title` still wins whenever it is filled in, so this is a default and
# not a decision taken away from her. It cannot simply be the default value of
# hero_title, because that key has a real (empty) row in settings and a stored
# empty string beats a default.
HERO_HEAD = "Black and white treasure boxes"

# Flask serves /static from the app root, not the blueprint, so on a subpath
# the stylesheet would 404 -- the prefix has to be pushed into it here.
app = Flask(__name__, static_url_path=(PREFIX + "/static") if PREFIX else "/static")


def _secret_key():
    """The key that signs her session cookie, and WHERE IT CAME FROM.

    A key that changes signs her out, mid-sentence and with nothing in any log
    to say why. `os.urandom()` as the fallback meant exactly that: a new key
    every time the instance restarted, and -- worse, because it needs no
    restart at all -- a DIFFERENT key in every gunicorn worker, so two requests
    a second apart could disagree about whether she was logged in.

    SECRET_KEY from the environment still wins. Without one, a key is minted
    ONCE and kept beside the database, on the disk that survives a deploy. The
    file is created exclusively, so workers starting together cannot each write
    their own; the loser reads the winner's. Only if the directory refuses to
    be written does this fall back to a per-process key, and then it says so on
    /health rather than looking fine and logging her out all afternoon.
    """
    env = (os.environ.get("SECRET_KEY") or "").strip()
    if env:
        return env, "env"
    path = os.path.join(gallery.BASE, "data", "secret_key")
    try:
        # This runs before gallery.init_db(), so on a first deploy onto an
        # empty disk the directory is not there yet.
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    for attempt in (1, 2):
        try:
            with open(path) as fh:
                saved = fh.read().strip()
            if saved:
                return saved, "file"
        except OSError:
            pass
        if attempt == 2:
            break
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
        except FileExistsError:
            continue            # another worker got there first; read theirs
        except OSError:
            break
    return secrets.token_hex(32), "ephemeral"


app.secret_key, SECRET_KEY_SOURCE = _secret_key()
# Behind the Plesk proxy: without this every generated absolute URL is http,
# which Stripe rejects as a return URL.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    # The cookie is scoped to this subpath so it is never sent to anything else
    # sharing the domain -- /gis is a private app on the same origin.
    SESSION_COOKIE_NAME="abm_session",
    SESSION_COOKIE_PATH=(PREFIX or "/"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,     # one phone photo, comfortably
    # Ninety days, and it slides forward on every request she makes, so the
    # studio does not ask for a password in the middle of a working afternoon.
    # Flask's own default is 31 days; it is written down here because the
    # length of time she stays signed in is a decision, not a framework detail.
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
    SESSION_REFRESH_EACH_REQUEST=True,
)

site = Blueprint("site", __name__, url_prefix=PREFIX or None)


# --------------------------------------------------------------- helpers
def cfg():
    return gallery.settings()


def signature_name():
    """The name templates/_signature.html was drawn for, or None."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates", "_signature.json")) as fh:
            return json.load(fh).get("name")
    except (OSError, ValueError):
        return None


def wordmark(title):
    """Split the title so the name can be signed. "Art by McCarthy" gives
    "Art by" set small over "McCarthy" in script -- the way a painter signs a
    canvas. A title with no "by" in it is simply signed whole, so renaming the
    shop in the admin cannot break the lockup."""
    if " by " in title:
        pre, name = title.rsplit(" by ", 1)
        return {"pre": f"{pre} by", "name": name}
    return {"pre": "", "name": title}


QUIET_PAGES = ("about", "contact")
# /commissions renders the contact page, so it takes the contact backdrop
PAGE_ALIAS = {"commissions": "contact"}


def _asset_version():
    """A cache-buster the stylesheet cannot forget to change.

    `?v=116` was typed into base.html by hand, which means every edit to the
    stylesheet is also a promise to remember a number in another file -- and
    the promise was broken the first time it was tested: a day of layout work
    shipped behind a version returning visitors already had cached, so the new
    CSS was live and invisible. This is the file's own content, so it changes
    when and only when the file does.
    """
    h = hashlib.sha1()
    for rel in ("css/site.css", "js/theme.js"):
        try:
            with open(os.path.join(app.static_folder, rel), "rb") as fh:
                h.update(fh.read())
        except OSError:          # missing file is not worth a 500 on every page
            h.update(rel.encode())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()
# The commit this instance is running, short. Empty off Render.
BUILD = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]


@app.context_processor
def inject():
    c = cfg()
    # Which quiet backdrop this page gets. Taken from the endpoint rather than
    # from a Jinja block: a block renders, so it printed "about" as loose text
    # at the top of the document.
    ep = (request.endpoint or "").rsplit(".", 1)[-1] if has_request_context() else ""
    ep = PAGE_ALIAS.get(ep, ep)
    key = ep if ep in QUIET_PAGES else ""
    # Mail and other off-request renders have no session, hence no cart.
    cart_tok = session.get("cart") if has_request_context() else None
    # Which nav item to light up. A single work and the archive still belong
    # under Work, and /commissions is the Contact page wearing another URL.
    nav = {"index": "work", "work": "work", "archive": "work",
           "collection": "work",
           "about": "about", "contact": "contact"}.get(ep, "")
    # slides/has_archive go to every template because the viewer is included
    # from base.html now rather than from the landing page alone.
    slides = viewer_slides()
    return {"cfg": c, "prefix": PREFIX, "stripe_on": payments.enabled(),
            "cart_token": cart_tok,
            "cart_n": gallery.cart_count(cart_tok) if cart_tok else 0,
            "asset_version": ASSET_VERSION,
            "noindex": NOINDEX, "wordmark": wordmark(c["site_title"]),
            "slides": slides, "has_archive": getattr(g, "_has_archive", False),
            "signature_name": signature_name(), "pagekey": key, "nav": nav, "endpoint": ep,
            "signup_source": ("work:" + request.view_args["slug"]
                              if ep == "work" and has_request_context()
                              and request.view_args and "slug" in request.view_args
                              else (ep or "site"))}


@app.after_request
def hsts(resp):
    # Without this the browser has no standing instruction that the site is
    # HTTPS-only, so an address remembered from before the move to her own
    # domain navigates over plain http first. The 301 does upgrade it, but the
    # address bar reads http for that hop and Chrome paints "Not secure" --
    # which is exactly why the site looks fine in an incognito window and not
    # in a normal one. Only sent on a secure request (ProxyFix reads
    # X-Forwarded-Proto from Render's edge) so a plain http hop is never the
    # thing that pins the policy.
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("site.admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def img_url(base, size="m", ext="webp"):
    return url_for("site.photo", name=f"{base}-{size}.{ext}")


app.jinja_env.globals["img_url"] = img_url


def dollars(cents):
    """Cents out of the database, dollars into the form field."""
    try:
        c = int(cents)
    except (TypeError, ValueError):
        return ""
    return "%d" % (c // 100) if c % 100 == 0 else "%.2f" % (c / 100.0)


app.jinja_env.globals["dollars"] = dollars


def day(iso):
    """2026-09-10T17:04:22Z -> 10 Sep 2026.

    The studio reads dates, it does not sort them by eye, so the month is a
    name. Anything that is not a stored timestamp comes back untouched rather
    than raising on a page that is only listing people."""
    try:
        return datetime.strptime((iso or "")[:10], "%Y-%m-%d").strftime("%-d %b %Y")
    except (ValueError, TypeError):
        return iso or ""


app.jinja_env.filters["day"] = day
app.jinja_env.globals["status_label"] = gallery.status_label
@app.template_filter("paras")
def paras(text):
    """Her line breaks, kept.

    Everything she writes -- a collection's About box, the story beside a
    painting -- arrives as plain text with blank lines in it, and HTML throws
    those away: two paragraphs and a scrap of dialogue came out as one run-on
    block. Each line becomes its own paragraph, escaped, which is what the
    About page already did by hand and now does through here.
    """
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    return Markup("".join("<p>%s</p>" % escape(line) for line in lines))


@app.template_filter("hostof")
def hostof(url):
    """Show a source as its site, not as 80 characters of query string."""
    m = re.match(r"https?://(?:www\.)?([^/]+)", (url or "").strip(), re.I)
    return m.group(1) if m else (url or "")


# The landing headline's default, handed to the templates rather than retyped
# in them. Settings had its own copy of the fallback chain and it had already
# drifted: it still put `tagline` in front of this, which is the behaviour
# app.py moved away from in September, so the preview showed a headline the
# front page had not used for days.
app.jinja_env.globals["her_share"] = gallery.her_share
app.jinja_env.globals["money"] = gallery.money
app.jinja_env.globals["env_name"] = ENV_NAME
app.jinja_env.globals["hero_head"] = HERO_HEAD
app.jinja_env.globals["exhibition_dates"] = gallery.exhibition_dates
app.jinja_env.globals["opp_kind_label"] = gallery.opp_kind_label
app.jinja_env.globals["opp_status_label"] = gallery.opp_status_label


def slide_index(slug):
    """Where this painting sits in the viewer's slide list, or None.

    The work page opens the viewer AT THE PIECE YOU ARE LOOKING AT rather than
    at slide zero, which is the whole reason it beats the floating button it
    replaces. None when the piece is not in the list at all -- a draft, or one
    with no photograph yet -- and the template then renders no control rather
    than one that would open on somebody else's painting.
    """
    for i, s in enumerate(viewer_slides()):
        if s["slug"] == slug:
            return i
    return None


app.jinja_env.globals["slide_index"] = slide_index
# One photograph's three sizes, as the viewer wants them (work.html -> PIECE_SHOTS).
app.jinja_env.filters["abm_shot"] = lambda base: {
    "l": img_url(base, "l", "jpg"), "m": img_url(base, "m", "jpg"), "s": img_url(base, "s", "jpg")}


def notify(kind, name, email, body, work_title=None):
    """Tell the artist. Best effort only -- the enquiry is already saved, and a
    mail server having a bad day must not turn into a lost customer."""
    to = (cfg().get("artist_email") or "").strip()
    if not to:
        return False
    try:
        return mailer.notify_inquiry(
            to, kind, name, email, body, work=work_title,
            admin_url=url_for("site.admin_inquiries", _external=True))
    except Exception:
        return False


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _price_cents(v):
    """Accept '2,400', '2400.00', '$2400'."""
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


# ---------------------------------------------------------------- public
def viewer_slides():
    """Every photographed piece, as the viewer's slide list.

    Cached on `g` because the context processor hands this to EVERY page and
    the landing page wants the same list for its own hero fallback — without
    the cache, rendering the home page would read the whole gallery twice.

    Sold work is included on purpose: a sold painting is the best argument for
    the next one. Drafts are not, because list_works excludes them.
    """
    if not has_request_context():
        return []
    if not hasattr(g, "_slides"):
        works = gallery.list_works(status="for_sale")
        sold = gallery.list_works(status="sold")
        g._slides = ([_viewer_entry(w, "available") for w in works if w["images"]]
                     + [_viewer_entry(w, "archive") for w in sold if w["images"]])
        g._has_archive = bool(sold)
    return g._slides


def _viewer_entry(w, group):
    """One slide in the cinematic viewer. Kept to the fields the caption needs
    so the payload sitting in the page stays small."""
    img = w["images"][0] if w["images"] else None
    return {
        "slug": w["slug"], "title": w["title"],
        "medium": w["medium"], "dims": w["dims"], "group": group,
        "price": w["price"], "status": w["status"],
        "m": img_url(img["base"], "m", "jpg") if img else None,
        "l": img_url(img["base"], "l", "jpg") if img else None,
        "s": img_url(img["base"], "s", "jpg") if img else None,
        "url": url_for("site.work", slug=w["slug"]),
    }


@site.route("/")
def index():
    gallery.release_expired()
    works = gallery.list_works(status="for_sale")
    sold = gallery.list_works(status="sold")
    # The slide list comes from viewer_slides() via the context processor now,
    # so it is built once per request and shared with every other page.
    c = cfg()
    hero = {
        "base": c.get("hero_image") or "",
        "title": c.get("hero_title") or HERO_HEAD,
        "sub": c.get("hero_sub") or "",
        "caption": c.get("hero_caption") or "",
    }
    # EVERY photograph of the hero painting, not just one, so the hero can
    # cross-dissolve between them. hero["base"] stays the first and is what
    # og:image still points at -- a rotating share image would be a lie.
    #
    # Three cases, in the order they actually happen: the setting is empty and
    # the hero falls back to the first painting on the wall (all its shots);
    # the setting names one of her paintings' photographs (that painting's
    # shots, led by the one she picked); or it is a standalone upload that
    # belongs to no work, and then there is nothing to rotate.
    pool = works + sold
    owner = None
    if not hero["base"]:
        owner = next((w for w in pool if w["images"]), None)
        if owner:
            hero["base"] = owner["images"][0]["base"]
            hero["shots"] = [im["base"] for im in owner["images"]]
    else:
        owner = next((w for w in pool
                      if any(im["base"] == hero["base"] for im in w["images"])), None)
        if owner:
            rest = [im["base"] for im in owner["images"] if im["base"] != hero["base"]]
            hero["shots"] = [hero["base"]] + rest
    hero.setdefault("shots", [hero["base"]] if hero["base"] else [])
    # Which painting this actually is. The headline above it is hero_title, a
    # line she writes -- live it reads "The Sri Lanka Collection", which names
    # the body of work and not the piece on screen. Only set when the hero is
    # one of her paintings; a standalone uploaded photograph has no name to give.
    hero["piece"] = owner["title"] if owner else ""
    hero["piece_slug"] = owner["slug"] if owner else ""
    # The wall is one body of work at the moment, so it says which one, in her
    # words: the opening of that collection's own About box, with the rest a
    # click away. Her subtitle stands in until the box is written.
    wall = gallery.common_collection(works)
    say, more = gallery.excerpt(wall["blurb"] or hero["sub"], 300) if wall else ("", False)
    return render_template("index.html", works=works, hero=hero,
                           wall=wall, wall_say=say, wall_more=more)


@site.route("/archive")
def archive():
    return render_template("archive.html", works=gallery.list_works(status="sold"))


@site.route("/work/<slug>")
def work(slug):
    gallery.release_expired()
    w = gallery.get_work(slug=slug)
    # A draft is unpublished, and that has to hold for a direct link too: the
    # slug is guessable from the title and she will be writing these while the
    # site is live. Signed in it still renders, which is how she previews one.
    if not w or (w["status"] == "draft" and not session.get("admin")):
        abort(404)
    # Someone else's hold says how long it can last: 10 minutes in a cart, up
    # to 36 once they are on the payment page -- "a few minutes" was only true
    # of the first. Rounded up, so it never promises sooner than it will be.
    hold_left = None
    if w["status"] == "reserved" and w.get("reserved_until"):
        try:
            until = datetime.strptime(w["reserved_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            hold_left = max(1, -(-int((until - datetime.now(timezone.utc)).total_seconds()) // 60))
        except ValueError:
            pass
    return render_template("work.html", w=w, near=gallery.neighbours(w), hold_left=hold_left)


@site.route("/collection/<slug>")
def collection(slug):
    c = gallery.get_collection(slug=slug)
    if not c:
        abort(404)
    works = gallery.list_works(collection_id=c["id"])
    # A collection with subcategories shows its own pieces first and then each
    # subcategory under its own heading -- grouping is the entire point of
    # having them, so the page must not just pour everything into one grid.
    groups = [(k, gallery.list_works(collection_id=k["id"])) for k in c["children"]]
    groups = [g for g in groups if g[1]]
    # An empty collection is a page with nothing on it, and the slug is
    # guessable. Signed in it still renders, so she can see one before it
    # has anything in it.
    if not works and not groups and not session.get("admin"):
        abort(404)
    return render_template("collection.html", c=c, works=works, groups=groups)


@site.route("/about")
def about():
    return render_template("about.html")


def _is_bot():
    """A field a person never sees and a bot cannot resist.

    Named `website` rather than anything that says "trap": scrapers skip inputs
    called honeypot, and a plausible field name is the whole trick. It is hidden
    off-screen in CSS rather than with display:none or hidden, because the
    cruder bots skip those too.

    The caller returns SUCCESS when this is true. A bot told it failed simply
    tries again with the field left blank; a bot told it worked goes away.
    """
    return bool((request.form.get("website") or "").strip())


# Bump when the wording of either policy changes; the page prints it.
POLICIES_UPDATED = "September 29, 2026"


@site.route("/policies")
def policies():
    return render_template("policies.html", updated=POLICIES_UPDATED)


@site.route("/commissions", methods=["GET", "POST"])
def commissions():
    """Same page as /contact, opened on the commission pane. The URL is kept so
    existing links and the sitemap still land somewhere sensible."""
    if request.method == "POST":
        if _is_bot():
            return render_template("thanks.html", heading="Thank you",
                                   msg="Your note is with the studio. Expect a reply within a few days.")
        gallery.add_inquiry("commission", request.form.get("name"),
                            request.form.get("email"), request.form.get("body"))
        notify("commission", request.form.get("name"), request.form.get("email"),
               request.form.get("body"))
        return render_template("thanks.html", heading="Thank you",
                               msg="Your note is with the studio. Expect a reply within a few days.")
    return render_template("contact.html", mode="commission")


@site.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        if _is_bot():
            return render_template("thanks.html", heading="Thank you",
                                   msg="Your message has been received.")
        gallery.add_inquiry("contact", request.form.get("name"),
                            request.form.get("email"), request.form.get("body"))
        notify("contact", request.form.get("name"), request.form.get("email"),
               request.form.get("body"))
        return render_template("thanks.html", heading="Thank you",
                               msg="Your message has been received.")
    return render_template("contact.html", mode="message")


@site.route("/subscribe", methods=["POST"])
def subscribe():
    if _is_bot():
        return render_template("thanks.html", heading="You're on the list",
                               msg="New work, about once a month. Nothing else.")
    ok = gallery.subscribe(request.form.get("email"), request.form.get("source") or "site")
    return render_template("thanks.html",
                           heading="You're on the list" if ok else "That email didn't look right",
                           msg="New work, about once a month. Nothing else."
                               if ok else "Try again with a full email address.")


@site.route("/photo/<name>")
def photo(name):
    if "/" in name or ".." in name:
        abort(404)
    # Some hosts' mimetypes tables have no .webp, and the fallback
    # application/octet-stream makes a browser download the photograph
    # instead of showing it.
    kw = {"mimetype": "image/webp"} if name.endswith(".webp") else {}
    resp = send_from_directory(gallery.PHOTO_DIR, name, conditional=True, **kw)
    # Filenames carry a random stem, so a changed image is a new URL.
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------------- cart
# A buyer's cart is the set of pieces reserved with their token -- see the
# note above gallery.reserve(). The token is random and lives in the session
# cookie; it identifies a cart, never a person.
def _cart_token(create=False):
    tok = session.get("cart")
    if not tok and create:
        tok = session["cart"] = secrets.token_urlsafe(16)
    return tok


def _cart_totals(works):
    c = cfg()
    sub = sum(int(w["price_cents"] or 0) for w in works)
    ship = sum(payments.ship_cents(w, c) or 0 for w in works)
    return {"sub": gallery.money(sub), "ship": gallery.money(ship),
            "total": gallery.money(sub + ship), "ship_cents": ship,
            "tax_on": payments.TAX_ENABLED}


def _settle_checkouts(tok):
    """Back on the site with a checkout still open (Stripe's Back link, the
    browser's back button, a second tab): cancel it at Stripe so it can no
    longer be paid, and return its pieces to the cart. A session that turns
    out to be PAID is left alone -- the webhook marks those pieces sold."""
    for sid in gallery.open_checkouts(tok):
        try:
            status = payments.expire_if_open(sid)
        except Exception:
            app.logger.exception("cart: could not check checkout %s", sid)
            continue
        if status != "complete":
            gallery.reopen_cart(tok, sid)


@site.route("/buy/<slug>", methods=["POST"])
def buy(slug):
    """Add to cart. (The URL predates the cart and is kept so a work page
    already open in someone's browser still posts somewhere sensible.)"""
    gallery.release_expired()
    w = gallery.get_work(slug=slug)
    if not w:
        abort(404)
    tok = _cart_token()
    js = request.headers.get("X-Requested-With") == "fetch"
    if w["status"] == "reserved" and tok and w.get("reserved_by") == tok:
        return _added(js)
    if w["status"] != "available":
        if js:
            return jsonify(ok=False, reload=True)
        return redirect(url_for("site.work", slug=slug))
    if not payments.enabled() or w["ship_band"] == "quote" or not w["price_cents"]:
        # No keys yet, a piece too large to price shipping on sight, or a work
        # that has no price set yet: the enquiry is the checkout. Without the
        # last case the Enquire button on an unpriced work posted here and was
        # bounced straight back to the page it came from, doing nothing.
        return render_template("enquire.html", w=w)
    if not gallery.reserve(w["id"], by=_cart_token(create=True)):
        msg = ("Someone else is buying this piece right now. If they don't finish, "
               "it comes back within a few minutes.")
        if js:
            return jsonify(ok=False, msg=msg, reload=True)
        flash(msg)
        return redirect(url_for("site.work", slug=slug))
    return _added(js)


def _added(js):
    """The work page adds with fetch() so the photograph can fly into the
    cart and the buyer stays put; without JavaScript it is a plain redirect."""
    if not js:
        return redirect(url_for("site.cart"))
    return jsonify(ok=True, n=gallery.cart_count(_cart_token()), cart=url_for("site.cart"),
                   minutes=gallery.CART_HOLD_MINUTES)


@site.route("/cart")
def cart():
    gallery.release_expired()
    tok = _cart_token()
    if tok and payments.enabled():
        _settle_checkouts(tok)
    works = gallery.cart_works(tok)
    return render_template("cart.html", works=works, totals=_cart_totals(works),
                           hold_minutes=gallery.CART_HOLD_MINUTES)


@site.route("/cart/remove/<int:work_id>", methods=["POST"])
def cart_remove(work_id):
    tok = _cart_token()
    if tok:
        gallery.release_hold(work_id, tok)
    return redirect(url_for("site.cart"))


@site.route("/checkout", methods=["GET", "POST"])
def checkout():
    gallery.release_expired()
    tok = _cart_token()
    if not tok or not payments.enabled():
        return redirect(url_for("site.cart"))
    if request.method == "POST" and payments.on_site():
        # The cart's button posts; the page itself is a GET so a refresh does
        # not ask to resubmit a form.
        return redirect(url_for("site.checkout"))
    works = gallery.cart_works(tok)
    if not works:
        flash("Your cart is empty. Pieces are held for %d minutes, then go back "
              "on the wall." % gallery.CART_HOLD_MINUTES)
        return redirect(url_for("site.cart"))
    if payments.on_site():
        return _checkout_on_site(tok, works)
    _settle_checkouts(tok)
    works = gallery.cart_works(tok)
    ids = [w["id"] for w in works]
    # Stretch the hold BEFORE Stripe opens the session, so there is no moment
    # at which the buyer can pay for a piece that is no longer held for them.
    gallery.extend_holds(tok, ids, gallery.CHECKOUT_HOLD_MINUTES)
    try:
        s = payments.create_session(
            works,
            success_url=url_for("site.thanks", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("site.cart", _external=True))
    except Exception:
        app.logger.exception("checkout: Stripe session failed")
        gallery.extend_holds(tok, ids, gallery.CART_HOLD_MINUTES)
        flash("Checkout could not be opened just now. Your pieces are still held; "
              "please try again in a moment.")
        return redirect(url_for("site.cart"))
    gallery.mark_checkout(tok, ids, s.id)
    session["checkout_n"] = len(works)
    return redirect(s.url, code=303)


def _reusable_session(tok, works):
    """A refresh of the checkout page should not open a second Stripe session.
    Reuse the open one if it covers exactly this cart and has time left."""
    sids = {w.get("checkout_session") for w in works}
    if len(sids) != 1 or None in sids:
        return None
    try:
        s = payments.retrieve(sids.pop())
    except Exception:
        return None
    if getattr(s, "status", None) != "open":
        return None
    if int(getattr(s, "expires_at", 0) or 0) < time.time() + 120:
        return None
    return s


def _checkout_on_site(tok, works):
    s = _reusable_session(tok, works)
    if s is None:
        _settle_checkouts(tok)
        works = gallery.cart_works(tok)
        if not works:
            return redirect(url_for("site.cart"))
        ids = [w["id"] for w in works]
        gallery.extend_holds(tok, ids, gallery.CHECKOUT_HOLD_MINUTES)
        try:
            s = payments.create_session(
                works,
                return_url=url_for("site.thanks", _external=True) + "?session_id={CHECKOUT_SESSION_ID}")
        except Exception:
            app.logger.exception("checkout: Stripe session failed")
            gallery.extend_holds(tok, ids, gallery.CART_HOLD_MINUTES)
            flash("Checkout could not be opened just now. Your pieces are still held; "
                  "please try again in a moment.")
            return redirect(url_for("site.cart"))
        gallery.mark_checkout(tok, ids, s.id)
        works = gallery.cart_works(tok)
    session["checkout_n"] = len(works)
    resp = app.make_response(render_template(
        "checkout.html", works=works, totals=_cart_totals(works),
        client_secret=s.client_secret, publishable=payments.PUBLISHABLE,
        # The page's clock runs to the SESSION's end, which is before the hold's.
        expires_at=int(s.expires_at)))
    # A page carrying a client secret is not one to keep in any cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@site.route("/enquire/<slug>", methods=["POST"])
def enquire(slug):
    w = gallery.get_work(slug=slug)
    if _is_bot():
        return render_template("thanks.html", heading="Thank you",
                               msg="The studio will be in touch with shipping and payment.")
    gallery.add_inquiry("purchase", request.form.get("name"), request.form.get("email"),
                        request.form.get("body"), w["id"] if w else None)
    notify("purchase", request.form.get("name"), request.form.get("email"),
           request.form.get("body"), work_title=w["title"] if w else None)
    return render_template("thanks.html", heading="Thank you",
                           msg="The studio will be in touch with shipping and payment.")


# Prints aren't for sale (2026-10-03). This only counts who would want one, and of
# which piece, so the decision to set up print-on-demand rests on real asks
# rather than a guess. Each ask is an ordinary enquiry of kind "print".
PRINT_SIZES = ("Small, about 8 x 10", "Medium, about 12 x 16", "Large, 18 x 24 or bigger", "Not sure yet")


@site.route("/print-interest/<slug>", methods=["GET", "POST"])
def print_interest(slug):
    w = gallery.get_work(slug=slug)
    if not w:
        abort(404)
    if request.method == "GET":
        return render_template("print_interest.html", w=w, sizes=PRINT_SIZES, noindex=True)
    done = render_template("thanks.html", heading="Thank you", noindex=True,
                           msg=f"If prints of {w['title']} become available, you'll be the first to hear.")
    if _is_bot():
        return done
    size = request.form.get("size") or ""
    note = (request.form.get("body") or "").strip()
    body = "\n".join(x for x in (f"Size: {size}" if size in PRINT_SIZES else "", note) if x)
    gallery.add_inquiry("print", request.form.get("name"), request.form.get("email"), body, w["id"])
    notify("print", request.form.get("name"), request.form.get("email"), body, work_title=w["title"])
    return done


@site.route("/thanks")
def thanks():
    sid = request.args.get("session_id") or ""
    if sid.startswith("cs_") and payments.enabled():
        try:
            status = getattr(payments.retrieve(sid), "status", None)
        except Exception:
            app.logger.exception("thanks: could not look up %s", sid)
            status = None
        if status == "open":
            # Back from a bank or wallet redirect without the payment going
            # through: the checkout is still open and the pieces still held.
            flash("The payment didn't go through. Nothing was charged; "
                  "your pieces are still held, so you can try again.")
            return redirect(url_for("site.checkout"))
        if status == "complete":
            # Paid. The webhook marks the pieces sold; the buyer starts a new
            # cart from here rather than seeing these ones "still held".
            session.pop("cart", None)
    n = session.pop("checkout_n", 1)
    what = "The painting" if n == 1 else "Your %d paintings" % n
    return render_template("thanks.html", heading="Thank you",
                           msg="Your receipt is on its way by email. %s will be "
                               "packed and shipped within a few days, and you'll get tracking." % what)


@site.route("/stripe/webhook", methods=["POST"])
def webhook():
    try:
        payments.parse_webhook(request.data, request.headers.get("Stripe-Signature", ""))
    except Exception:
        return "bad signature", 400
    # The signature check above is the security boundary, and verifying it is
    # the only thing the SDK is needed for here. The event is then read back
    # out of the RAW BODY as plain JSON rather than off the object the SDK
    # returns: stripe-python stopped making StripeObject a dict subclass, so
    # `.get()` on it raises AttributeError -- which 500'd this webhook on every
    # paid checkout, leaving the painting unsold and the order unrecorded while
    # Stripe retried into the same wall. A dict cannot break that way again,
    # and this handler now reads the same whatever version is installed.
    event = json.loads(request.data or b"{}")
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    sid = obj.get("id")
    items = payments.parse_items(meta)
    # A session opened before the cart existed names one piece as work_id.
    work_id = _int(meta.get("work_id"))
    if kind == "checkout.session.completed":
        details = obj.get("customer_details") or {}
        ship = ((obj.get("shipping_details") or {}).get("address")
                or (details.get("address") or {}))
        buyer = {"name": details.get("name"), "email": details.get("email")}
        # Sales tax is one figure for the whole checkout (0 unless Stripe Tax
        # is on and the address is somewhere she is registered).
        totals = obj.get("total_details") or {}
        tax = int(totals.get("amount_tax") or 0)
        disc = int(totals.get("amount_discount") or 0)
        code = payments.promo_code_text(obj) if disc else None
        new = False
        if items:
            # One order row per painting, each carrying its own price and its
            # own shipping band, so the per-piece invoice still splits cleanly.
            # "<session>#<work>" keeps stripe_session_id unique per row, which
            # is what makes a retried webhook a no-op (INSERT OR IGNORE).
            shares = payments.split_checkout(items, disc, tax)
            for (wid, price, ship_c), (disc_c, tax_c) in zip(items, shares):
                gallery.set_status(wid, "sold")
                new |= gallery.record_order(wid, "%s#%d" % (sid, wid),
                                     price - disc_c + ship_c + tax_c,
                                     buyer, ship or {}, checkout_id=sid, tax_cents=tax_c,
                                     discount_cents=disc_c or None,
                                     promo_code=code if disc_c else None)
        elif work_id:
            gallery.set_status(work_id, "sold")
            new = gallery.record_order(work_id, sid, obj.get("amount_total"), buyer,
                                       ship or {}, tax_cents=tax,
                                       discount_cents=disc or None, promo_code=code)
            items = [(work_id, None, None)]
        if new:
            notify_sale(items, obj.get("amount_total"), buyer, ship or {}, disc, code, tax)
    elif kind == "checkout.session.expired":
        if sid:
            gallery.release_checkout(sid)
        if work_id:
            w = gallery.get_work(work_id=work_id)
            if w and w["status"] == "reserved":
                gallery.set_status(work_id, "available")
    return jsonify(ok=True)


@site.route("/health")
def health():
    works = gallery.list_works()
    return jsonify(ok=True, works=len(works),
                   for_sale=len([w for w in works if w["status"] == "available"]),
                   sold=len([w for w in works if w["status"] == "sold"]),
                   stripe=payments.enabled(), live=payments.live_mode(),
                   env=ENV_NAME or "production", prefix=PREFIX,
                   # "ephemeral" is the one to worry about: it means the
                   # session key dies with the process and she will be signed
                   # out at every restart. Where the key came from, never the
                   # key.
                   session_key=SECRET_KEY_SOURCE,
                   # WHICH BUILD IS ANSWERING. Render sets this on every
                   # deploy; without it "is my change live yet" can only be
                   # asked of the stylesheet's hash, which says nothing at all
                   # when the change was in the Python or a template.
                   build=BUILD)


@site.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: %s\n"
                    % url_for("site.sitemap", _external=True), mimetype="text/plain")


@site.route("/sitemap.xml")
def sitemap():
    urls = [url_for("site.index", _external=True), url_for("site.about", _external=True),
            url_for("site.archive", _external=True), url_for("site.commissions", _external=True),
            url_for("site.policies", _external=True)]
    urls += [url_for("site.work", slug=w["slug"], _external=True) for w in gallery.list_works()]
    # Only collections with public pieces: an empty one 404s to visitors.
    urls += [url_for("site.collection", slug=c["slug"], _external=True)
             for c in gallery.list_collections() if c["total"]]
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"{body}</urlset>", mimetype="application/xml")


# ----------------------------------------------------------------- admin
@site.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = None
    if request.method == "POST":
        # Hers first, the host's ADMIN_PASS as recovery — see
        # gallery.check_admin_password for why both are accepted.
        if gallery.check_admin_password(request.form.get("password") or "", ADMIN_PASS):
            session["admin"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("site.admin_works"))
        err = "That password didn't match."
    return render_template("admin/login.html", err=err)


@site.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("site.index"))


@site.route("/admin")
@admin_required
def admin_works():
    gallery.release_expired()
    q = (request.args.get("q") or "").strip()
    st = request.args.get("status") or ""
    med = request.args.get("medium") or ""
    place = request.args.get("place") or ""
    coll = request.args.get("collection") or ""
    # "none" is a real choice, not an empty filter: show me what is filed
    # nowhere / sitting nowhere. 0 is the sentinel for it in the query layer.
    coll_id = 0 if coll == "none" else (_int(coll) if coll else None)
    place_v = 0 if place == "none" else (place or None)
    works = gallery.search_works(q=q or None,
                                 status=st if st in gallery.STATUSES else None,
                                 collection_id=coll_id,
                                 place=place_v,
                                 medium=med or None)
    filtered = bool(q or st or med or place or coll)
    return render_template("admin/works.html", works=works,
                           orders=gallery.list_orders()[:5],
                           inquiries=[i for i in gallery.list_inquiries() if not i["handled"]],
                           q=q, f_status=st, f_medium=med, f_place=place, f_collection=coll,
                           filtered=filtered, total=gallery.count_works(),
                           collections=gallery.list_collections(),
                           media=gallery.media_list(), places=gallery.current_places(),
                           due=gallery.due_soon(REMIND_WITHIN_DAYS),
                           # Money somebody else is holding is the thing most
                           # easily forgotten, because nothing prompts for it.
                           consign=gallery.consignment_totals())


@site.route("/admin/exhibitions", methods=["GET", "POST"])
@admin_required
def admin_exhibitions():
    """The schedule. Adding one needs only a name and a date; everything else
    is filled in on the show's own page once it is real."""
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("A show needs a name.")
            return redirect(url_for("site.admin_exhibitions"))
        eid = gallery.save_exhibition({"title": title,
                                       "starts_on": request.form.get("starts_on")})
        return redirect(url_for("site.admin_exhibition", exhibition_id=eid))
    upcoming, past = gallery.list_exhibitions()
    return render_template("admin/exhibitions.html", upcoming=upcoming, past=past)


@site.route("/admin/exhibition/<int:exhibition_id>", methods=["GET", "POST"])
@admin_required
def admin_exhibition(exhibition_id):
    e = gallery.get_exhibition(exhibition_id)
    if not e:
        abort(404)
    if request.method == "POST":
        gallery.save_exhibition({
            "title": request.form.get("title") or e["title"],
            "venue": (request.form.get("venue") or "").strip(),
            "city": (request.form.get("city") or "").strip(),
            "starts_on": request.form.get("starts_on"),
            "ends_on": request.form.get("ends_on"),
            "blurb": (request.form.get("blurb") or "").strip(),
            "url": (request.form.get("url") or "").strip(),
        }, exhibition_id)
        gallery.set_exhibition_works(exhibition_id, request.form.getlist("work_ids"))
        flash("Saved.")
        return redirect(url_for("site.admin_exhibition", exhibition_id=exhibition_id))
    return render_template("admin/exhibition_form.html", e=e,
                           works=gallery.list_works(include_draft=True))


def _print_ctx():
    """Shared by both print sheets — the artist's name is a setting because she
    spells it two ways and that is hers to settle."""
    return (cfg().get("artist_name") or "").strip()


@site.route("/admin/exhibition/<int:exhibition_id>/checklist")
@admin_required
def admin_exhibition_checklist(exhibition_id):
    e = gallery.get_exhibition(exhibition_id)
    if not e:
        abort(404)
    works = gallery.works_in_show(exhibition_id)
    total = sum(w["price_cents"] or 0 for w in works)
    return render_template("admin/print_checklist.html", e=e, works=works,
                           artist=_print_ctx(),
                           total_value=gallery.money(total) if total else None,
                           printed_on=day(gallery.today()))


@site.route("/admin/exhibition/<int:exhibition_id>/check", methods=["POST"])
@admin_required
def admin_exhibition_check(exhibition_id):
    """Tick one box on the checklist, from the page, without reloading it.

    The sheet is still a print document -- it goes out with the work and gets
    signed -- but it is also the thing she has open on a phone while wrapping
    paintings, and a square that can only be marked with a pen is no use there.
    """
    try:
        stamp = gallery.set_exhibition_check(
            exhibition_id, request.form.get("work_id", type=int),
            request.form.get("leg", ""), request.form.get("on") == "1")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "stamp": day(stamp) if stamp else None})


@site.route("/admin/exhibition/<int:exhibition_id>/labels")
@admin_required
def admin_exhibition_labels(exhibition_id):
    e = gallery.get_exhibition(exhibition_id)
    if not e:
        abort(404)
    qr = request.args.get("qr") != "0"
    return render_template("admin/print_labels.html",
                           works=gallery.works_in_show(exhibition_id),
                           heading=e["title"], artist=_print_ctx(), qr=qr,
                           back=url_for("site.admin_exhibition", exhibition_id=exhibition_id),
                           toggle_qr_url=url_for("site.admin_exhibition_labels",
                                                 exhibition_id=exhibition_id,
                                                 qr="0" if qr else "1"))


@site.route("/admin/work/<int:work_id>/coa")
@admin_required
def admin_work_coa(work_id):
    """A certificate for one painting. The site's buy panel already promises
    buyers one ("includes a signed certificate of authenticity"), so this is a
    promise being kept rather than a feature being added.

    The wording is deliberately plain and it is HERS to approve — a certificate
    is a statement she signs, not text a tool should quietly author on her
    behalf. It lives in the template, one place, easy to change.
    """
    w = gallery.get_work(work_id=work_id)
    if not w:
        abort(404)
    c = cfg()
    return render_template("admin/print_coa.html", w=w, artist=_print_ctx(),
                           site_title=c.get("site_title") or "")


@site.route("/admin/pricelist")
@admin_required
def admin_pricelist():
    """The sheet she hands a gallery or a cafe. Same filters as Pieces, so the
    list is whatever she just narrowed on screen."""
    q = (request.args.get("q") or "").strip()
    st = request.args.get("status") or ""
    med = request.args.get("medium") or ""
    place = request.args.get("place") or ""
    coll = request.args.get("collection") or ""
    works = gallery.search_works(
        q=q or None,
        status=st if st in gallery.STATUSES else None,
        collection_id=(0 if coll == "none" else (_int(coll) if coll else None)),
        place=(0 if place == "none" else (place or None)),
        medium=med or None)
    # A price list should not advertise what is already gone unless asked.
    if not st:
        works = [w for w in works if w["status"] != "draft"]
    pics = request.args.get("pics") != "0"
    priced = [w for w in works if w["price_cents"]]
    total = sum(w["price_cents"] or 0 for w in priced)
    bits = []
    if st:
        bits.append(gallery.status_label(st))
    if place and place != "none":
        bits.append("at " + place)
    if q:
        bits.append('matching "%s"' % q)
    args = {k: v for k, v in request.args.items() if k != "pics"}
    c = cfg()
    return render_template("admin/print_pricelist.html", works=works,
                           artist=_print_ctx(), pics=pics,
                           priced=len(priced),
                           total_value=gallery.money(total) if total else None,
                           printed_on=day(gallery.today()),
                           scope=" &middot; ".join(bits) if bits else None,
                           site_title=c.get("site_title") or "",
                           site_note=c.get("tagline") or "",
                           back=url_for("site.admin_works", **args),
                           toggle_img_url=url_for("site.admin_pricelist",
                                                  pics="0" if pics else "1", **args))


@site.route("/admin/labels")
@admin_required
def admin_labels():
    """Labels for whatever the Pieces filter is currently showing, so a set of
    labels is one click from the list you just narrowed."""
    q = (request.args.get("q") or "").strip()
    st = request.args.get("status") or ""
    med = request.args.get("medium") or ""
    place = request.args.get("place") or ""
    coll = request.args.get("collection") or ""
    works = gallery.search_works(
        q=q or None,
        status=st if st in gallery.STATUSES else None,
        collection_id=(0 if coll == "none" else (_int(coll) if coll else None)),
        place=(0 if place == "none" else (place or None)),
        medium=med or None)
    qr = request.args.get("qr") != "0"
    args = {k: v for k, v in request.args.items() if k != "qr"}
    return render_template("admin/print_labels.html", works=works,
                           heading="Labels", artist=_print_ctx(), qr=qr,
                           back=url_for("site.admin_works", **args),
                           toggle_qr_url=url_for("site.admin_labels",
                                                 qr="0" if qr else "1", **args))


@site.route("/admin/exhibition/<int:exhibition_id>/delete", methods=["POST"])
@admin_required
def admin_exhibition_delete(exhibition_id):
    e = gallery.get_exhibition(exhibition_id)
    gallery.delete_exhibition(exhibition_id)
    flash(f"Deleted {e['title'] if e else 'it'}. The paintings that were in it "
          "are untouched.")
    return redirect(url_for("site.admin_exhibitions"))


@site.route("/admin/collections", methods=["GET", "POST"])
@admin_required
def admin_collections():
    """List, and create from the same page. A collection is a name and a
    sentence, so a separate 'new' screen would be a form with two fields on
    it and a round trip to reach them."""
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("A collection needs a name.")
        else:
            parent_id = _int(request.form.get("parent_id")) or None
            new_id = gallery.save_collection({"name": name, "parent_id": parent_id})
            flash(f"Added {name}.")
            # A subcategory named from its parent's page sends you STRAIGHT
            # INTO it, because the next thing you do is put pieces in it.
            if parent_id and request.form.get("open"):
                return redirect(url_for("site.admin_collection", collection_id=new_id))
        return redirect(url_for("site.admin_collections"))
    return render_template("admin/collections.html",
                           collections=gallery.list_collections())


@site.route("/admin/collection/<int:collection_id>", methods=["GET", "POST"])
@admin_required
def admin_collection(collection_id):
    c = gallery.get_collection(collection_id=collection_id)
    if not c:
        abort(404)
    if request.method == "POST":
        gallery.save_collection({
            "name": (request.form.get("name") or c["name"]).strip(),
            "blurb": (request.form.get("blurb") or "").strip(),
            "sort": _int(request.form.get("sort"), 0),
            "slug": (request.form.get("slug") or "").strip() or None,
            "parent_id": _int(request.form.get("parent_id")) or None,
        }, collection_id)
        # The pieces are edited on the same form, so one Save means one trip.
        # Ticking a painting that is filed somewhere else MOVES it -- a piece
        # has one collection -- and the flash says so out loud.
        added, removed = gallery.set_collection_works(
            collection_id, request.form.getlist("work_ids"))
        bits = []
        if added:
            bits.append(f"{added} piece{'s' if added != 1 else ''} put in")
        if removed:
            bits.append(f"{removed} taken out")
        flash("Saved." + (" " + ", ".join(bits) + "." if bits else ""))
        return redirect(url_for("site.admin_collection", collection_id=collection_id))
    # Everything she owns, drafts included: the picker is where a piece is put
    # IN, so it has to offer the ones that are not in it yet.
    all_works = gallery.list_works(include_draft=True)
    member_ids = {w["id"] for w in all_works if w["collection_id"] == collection_id}
    return render_template("admin/collection_form.html", c=c,
                           all_works=all_works, member_ids=member_ids,
                           works=[w for w in all_works if w["id"] in member_ids],
                           collections=gallery.list_collections())


@site.route("/admin/collection/<int:collection_id>/delete", methods=["POST"])
@admin_required
def admin_collection_delete(collection_id):
    c = gallery.get_collection(collection_id=collection_id)
    kids = len(c["children"]) if c else 0
    gallery.delete_collection(collection_id)
    # Said out loud, because "delete" next to a count of paintings reads as if
    # it might take them with it.
    flash(f"Deleted {c['name'] if c else 'it'}. Its paintings are still here, "
          "now in no collection."
          + (f" Its {kids} subcategor{'ies are' if kids != 1 else 'y is'} now "
             "listed on their own." if kids else ""))
    return redirect(url_for("site.admin_collections"))


@site.route("/admin/editions")
@admin_required
def admin_editions():
    return render_template("admin/editions.html",
                           works=gallery.list_editioned_works())


@site.route("/admin/work/new", methods=["GET", "POST"])
@site.route("/admin/work/<int:work_id>", methods=["GET", "POST"])
@admin_required
def admin_work(work_id=None):
    w = gallery.get_work(work_id=work_id) if work_id else None
    if request.method == "POST":
        f = request.form
        data = {
            "title": (f.get("title") or "Untitled").strip(),
            "year": _int(f.get("year")),
            "medium": (f.get("medium") or "").strip(),
            "h_in": _float(f.get("h_in")), "w_in": _float(f.get("w_in")),
            "d_in": _float(f.get("d_in")),
            "price_cents": _price_cents(f.get("price")),
            "status": f.get("status") if f.get("status") in gallery.STATUSES else "available",
            "framed": 1 if f.get("framed") else 0,
            "ready_to_hang": 1 if f.get("ready_to_hang") else 0,
            "signed_where": (f.get("signed_where") or "").strip(),
            "story": (f.get("story") or "").strip(),
            "ship_band": f.get("ship_band") if f.get("ship_band") in gallery.SHIP_BANDS else "medium",
            "sort": _int(f.get("sort"), 0),
            "collection_id": _int(f.get("collection_id")) or None,
            # Catalogue detail, not stock. See gallery.edition().
            "edition_size": _int(f.get("edition_size")) or None,
            "edition_number": _int(f.get("edition_number")) or None,
            "slug": (f.get("slug") or "").strip() or None,
        }
        work_id = gallery.save_work(data, work_id)
        for file in request.files.getlist("photos"):
            if file and file.filename:
                try:
                    gallery.add_image(work_id, file.read(),
                                      kind=request.form.get("kind") or "full",
                                      alt=data["title"])
                except ValueError as e:
                    flash(str(e))
        return redirect(url_for("site.admin_work", work_id=work_id))
    return render_template("admin/work_form.html", w=w,
                           collections=gallery.list_collections(),
                           places=gallery.places(),
                           movements=gallery.movements(work_id) if work_id else [],
                           care=gallery.care_for(work_id) if work_id else [],
                           care_total=gallery.money(gallery.care_total(work_id)) if work_id else None,
                           shows=gallery.shows_for(work_id) if work_id else [])


@site.route("/admin/work/<int:work_id>/qr.png")
@admin_required
def admin_work_qr(work_id):
    """A QR label for one painting, pointing at its PUBLIC page.

    Generated here rather than by a service: no account, no tracking pixel in
    the middle of her gallery, and a printed label that cannot stop working
    because somebody else's free tier ended. High error correction because these
    get printed small and live on a wall next to a cafe window.
    """
    w = gallery.get_work(work_id=work_id)
    if not w:
        abort(404)
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    url = url_for("site.work", slug=w["slug"], _external=True)
    img = qrcode.QRCode(box_size=10, border=2, error_correction=ERROR_CORRECT_H)
    img.add_data(url)
    img.make(fit=True)
    buf = io.BytesIO()
    img.make_image(fill_color="#171a18", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    name = gallery.slugify(w["title"]) or ("work-%d" % work_id)
    return Response(buf.getvalue(), mimetype="image/png", headers={
        # inline so the admin page can show it; the download link adds its own
        # filename via the anchor's download attribute
        "Content-Disposition": 'inline; filename="%s-qr.png"' % name,
        "Cache-Control": "no-store",
    })


@site.route("/admin/work/<int:work_id>/care", methods=["POST"])
@admin_required
def admin_work_care(work_id):
    ok = gallery.add_care(work_id,
                          request.form.get("what"),
                          request.form.get("who"),
                          _price_cents(request.form.get("cost")),
                          request.form.get("happened_on"),
                          request.form.get("note"))
    flash("Recorded." if ok else "Say what was done.")
    return redirect(url_for("site.admin_work", work_id=work_id) + "#care")


@site.route("/admin/care/<int:care_id>/delete", methods=["POST"])
@admin_required
def admin_care_delete(care_id):
    gallery.delete_care(care_id)
    flash("Entry removed.")
    return redirect(request.form.get("back") or url_for("site.admin_works"))


@site.route("/admin/work/<int:work_id>/place", methods=["POST"])
@admin_required
def admin_work_place(work_id):
    """Move a painting. Not part of the work form's save: location changes by
    moving, so there is one way it can change and one place it is recorded."""
    ok = gallery.relocate(work_id,
                           request.form.get("place"),
                           request.form.get("note"),
                           request.form.get("moved_on"))
    flash("Moved." if ok else "Say where it went.")
    return redirect(url_for("site.admin_work", work_id=work_id) + "#where")


@site.route("/admin/movement/<int:movement_id>/delete", methods=["POST"])
@admin_required
def admin_movement_delete(movement_id):
    gallery.delete_movement(movement_id)
    flash("Entry removed.")
    return redirect(request.form.get("back") or url_for("site.admin_works"))


@site.route("/admin/work/<int:work_id>/status", methods=["POST"])
@admin_required
def admin_status(work_id):
    st = request.form.get("status")
    if st in gallery.STATUSES:
        gallery.set_status(work_id, st)
    return redirect(request.form.get("back") or url_for("site.admin_works"))


@site.route("/admin/work/<int:work_id>/move", methods=["POST"])
@admin_required
def admin_move_work(work_id):
    gallery.move_work(work_id, request.form.get("dir"))
    # Back to the row that moved rather than the top of the page: reordering
    # happens in runs of several presses, and losing your place after each one
    # turns a two-minute job into a chore.
    return redirect((request.form.get("back") or url_for("site.admin_works"))
                    + "#w%d" % work_id)


@site.route("/admin/image/<int:image_id>/move", methods=["POST"])
@admin_required
def admin_move_image(image_id):
    gallery.move_image(image_id, request.form.get("dir"))
    return redirect(request.form.get("back") or url_for("site.admin_works"))


@site.route("/admin/password", methods=["POST"])
@admin_required
def admin_password():
    current = request.form.get("current") or ""
    new = request.form.get("new") or ""
    again = request.form.get("again") or ""
    if not gallery.check_admin_password(current, ADMIN_PASS):
        flash("That current password didn't match, so nothing was changed.")
    elif len(new) < 10:
        flash("Pick a longer password — at least 10 characters.")
    elif new != again:
        flash("The two new passwords didn't match, so nothing was changed.")
    else:
        gallery.set_admin_password(new)
        flash("Password changed. Use the new one next time you sign in.")
    return redirect(url_for("site.admin_settings"))


@site.route("/admin/work/<int:work_id>/delete", methods=["POST"])
@admin_required
def admin_delete(work_id):
    if not gallery.delete_work(work_id):
        flash("That work has an order against it and cannot be deleted. "
              "Mark it sold instead, so the sale keeps its record.")
    return redirect(url_for("site.admin_works"))


@site.route("/admin/image/<int:image_id>/delete", methods=["POST"])
@admin_required
def admin_image_delete(image_id):
    gallery.delete_image(image_id)
    return redirect(request.form.get("back") or url_for("site.admin_works"))


@site.route("/admin/orders")
@admin_required
def admin_orders():
    return render_template("admin/orders.html", orders=gallery.list_orders(),
                           samples=gallery.count_sample_orders(),
                           is_sample=gallery.is_sample)


@site.route("/admin/order/<int:order_id>/shipped", methods=["POST"])
@admin_required
def admin_shipped(order_id):
    tracking = (request.form.get("tracking") or "").strip()
    first_time = gallery.mark_shipped(order_id, tracking)
    o = gallery.get_order(order_id)
    if not (first_time and o and request.form.get("email_buyer")):
        return redirect(url_for("site.admin_orders"))
    if gallery.is_sample(o) or "@" not in (o.get("buyer_email") or ""):
        flash("Marked shipped. No email sent (%s)." % (
            "sample order" if gallery.is_sample(o) else "no buyer email on the order"))
        return redirect(url_for("site.admin_orders"))
    # The email is filed through Messages, so it shows in her Sent folder like
    # anything else she has written -- or stays in Drafts, with the reason, if
    # the mail server would not take it, so she can press send again.
    subject, body = shipped_email(o, tracking)
    rid = gallery.save_reply(None, None, o["buyer_email"], o.get("buyer_name"), subject, body)
    if mailer.send(o["buyer_email"], subject, body):
        gallery.mark_reply_sent(rid)
        flash("Marked shipped and emailed %s the tracking." % o["buyer_email"])
    else:
        gallery.mark_reply_failed(rid, "the mail server would not accept it")
        flash("Marked shipped, but the email to the buyer would not send. "
              "It is in Messages > Drafts, ready to send again.")
    return redirect(url_for("site.admin_orders"))


# ------------------------------------------------------ sale & shipped emails
# Plain text, like every other message this site sends. The sale notice goes
# to her; the shipped notice goes to the buyer in her name.
def tracking_link(number):
    """(carrier, url) for the formats the big three print on a label, or
    (None, None) -- then the email gives the number alone, never a guess."""
    n = re.sub(r"\s+", "", number or "").upper()
    if re.fullmatch(r"1Z[0-9A-Z]{16}", n):
        return "UPS", "https://www.ups.com/track?tracknum=" + n
    if re.fullmatch(r"(9[1-5]\d{18,24}|82\d{8}|[A-Z]{2}\d{9}US)", n):
        return "USPS", "https://tools.usps.com/go/TrackConfirmAction?tLabels=" + n
    if re.fullmatch(r"\d{12}|\d{15}", n):
        return "FedEx", "https://www.fedex.com/fedextrack/?trknbr=" + n
    return None, None


def _addr_lines(a):
    lines = [a.get("line1"), a.get("line2"),
             " ".join(x for x in (a.get("city"), a.get("state"), a.get("postal_code")) if x),
             a.get("country") if a.get("country") not in (None, "US") else None]
    return [x for x in lines if x]


def notify_sale(items, paid_cents, buyer, ship, disc, code, tax):
    """Tell her something sold, once per checkout. Best effort, like notify():
    the order is already recorded, and a mail hiccup must never 500 the
    webhook (Stripe would retry, and the retry finds nothing new to say)."""
    to = (cfg().get("artist_email") or "").strip()
    if not to:
        return False
    try:
        rows, ship_total = [], 0
        for wid, price, ship_c in items:
            w = gallery.get_work(work_id=wid) or {}
            title = w.get("title") or "Work #%s" % wid
            rows.append((title, price if price is not None else w.get("price_cents")))
            ship_total += ship_c or 0
        n = len(rows)
        subject = ("Sold: %s" % rows[0][0]) if n == 1 else "Sold: %d paintings" % n
        if paid_cents:
            subject += " (%s)" % gallery.money(paid_cents)
        # (label, cents, minus) first, then laid out once the widest is known.
        money_rows = [(t, p, False) for t, p in rows]
        if ship_total:
            money_rows.append(("Shipping", ship_total, False))
        if disc:
            money_rows.append(("Promo code %s" % (code or ""), disc, True))
        if tax:
            money_rows.append(("Sales tax (%s)" % (ship.get("state") or ""), tax, False))
        if paid_cents:
            money_rows.append(("Paid", paid_cents, False))
        width = max(len(t) for t, _, _ in money_rows) + 3
        def line(label, cents, minus):
            amt = ("-" if minus else "") + (gallery.money(cents) or "")
            return "  %s%10s" % (label.ljust(width), amt)
        body = ["%s on the website." % ("A painting sold" if n == 1
                                        else "%d paintings sold together" % n), ""]
        for r in money_rows:
            if r[0] == "Paid" and r is money_rows[-1]:
                body.append("  " + "-" * (width + 10))
            body.append(line(*r))
        body += ["", "Buyer: %s" % (buyer.get("name") or "(no name given)"),
                 "Email: %s" % (buyer.get("email") or "(none)"), "", "Ship to:"]
        body += ["  " + x for x in ([buyer.get("name")] if buyer.get("name") else [])
                 + _addr_lines(ship)] or ["  (no address)"]
        body += ["", "Next: print the invoice and certificate from Orders, pack and ship"
                 + (" each piece" if n > 1 else "") + ",",
                 "then enter the tracking number and press \"mark shipped\" -- the buyer",
                 "gets an email with the tracking.", "",
                 "Orders: %s" % url_for("site.admin_orders", _external=True), "",
                 "Reply to this email to write to the buyer."]
        return mailer.send(to, subject, "\n".join(body), reply_to=buyer.get("email"))
    except Exception:
        app.logger.exception("sale email failed")
        return False


def shipped_email(o, tracking):
    """(subject, body) for the buyer, in her name."""
    c = cfg()
    title = o.get("title") or "your painting"
    first = ((o.get("buyer_name") or "").split() or [""])[0]
    carrier, url = tracking_link(tracking)
    body = ["Hi %s," % first if first else "Hello,", "",
            "\"%s\" is on its way to you." % title, ""]
    if tracking:
        body.append("Tracking%s: %s" % (" (%s)" % carrier if carrier else "", tracking))
        if url:
            body.append(url)
        body.append("")
    body += ["It is insured and packed with corner protection, and the signed",
             "certificate of authenticity travels with it. If anything arrives",
             "less than right, just reply to this email.", "",
             "Thank you for giving it a home.", "",
             (c.get("artist_name") or c.get("site_title") or "").strip(),
             url_for("site.index", _external=True).rstrip("/")]
    return "Your painting has shipped: %s" % title, "\n".join(body)


# ---------------------------------------------------------------- the invoice
# One order, on a sheet she can print or save as a PDF for a buyer's records.
# Built from the order row rather than from Stripe: the receipt Stripe emails
# is theirs and says what was CHARGED; this says what was SOLD, in her name,
# with the piece described the way a gallery describes it.
@site.route("/admin/order/<int:order_id>/invoice")
@admin_required
def admin_invoice(order_id):
    o = gallery.get_order(order_id)
    if not o:
        abort(404)
    c = cfg()
    price = o.get("price_cents")
    total = o.get("amount_cents")
    tax = o.get("tax_cents") or 0
    disc = o.get("discount_cents") or 0
    # The order stores one total, with the tax inside it and any promo code
    # already taken off. The piece's own price splits the rest, and whatever
    # is left over is the shipping -- shown as a derived line, never invented:
    # if the arithmetic does not work the sheet shows the total alone.
    before = None if total is None else total - tax + disc
    ship = (before - price) if (price is not None and before is not None
                                and before >= price) else None
    return render_template(
        "admin/print_invoice.html", o=o, sample=gallery.is_sample(o),
        artist=_print_ctx(), site_title=c.get("site_title") or "",
        artist_email=c.get("artist_email") or "",
        studio=c.get("studio_location") or "",
        number=("SAMPLE" if gallery.is_sample(o) else "ABM-%04d" % o["id"]),
        price=gallery.money(price), ship=gallery.money(ship),
        tax=gallery.money(tax) if tax else None,
        discount=gallery.money(disc) if disc else None,
        promo_code=o.get("promo_code") or "",
        tax_place=o.get("ship_state") or "",
        total=gallery.money(total), dims=gallery.dims(o),
        printed_on=day(gallery.today()))


# --------------------------------------------------------------- sample order
# So the Orders page and the invoice can be SEEN before a real sale exists.
# It is marked as a sample in the database, in the table, and across the
# printed sheet, and it changes nothing else -- no painting is marked sold and
# no money is involved. One press removes every one of them again.
@site.route("/admin/orders/sample", methods=["POST"])
@admin_required
def admin_sample_order():
    if request.form.get("remove"):
        n = gallery.drop_sample_orders()
        flash("Removed %d sample order%s." % (n, "" if n == 1 else "s"))
    else:
        gallery.make_sample_order()
        flash("Added a sample order. It is marked as one everywhere it appears, "
              "and nothing about your paintings has changed.")
    return redirect(url_for("site.admin_orders"))


# ------------------------------------------------------------------ messages
# Three folders over one exchange. The inbox is what the website's forms
# collected; drafts and sent are what she wrote back, kept here rather than in
# whatever mail client happened to be open -- which is the difference between
# a record of the conversation and a memory of it.
#
# WHAT THIS INBOX IS NOT: a mailbox. Nothing can arrive here except through the
# site's own contact, commission and purchase forms. Mail somebody sends
# straight to her address lands in her Gmail as it always has, because this app
# has no MX record, no mailbox and no inbound webhook. Worth saying out loud
# before anyone waits here for a message that was never coming.

MSG_FOLDERS = ("inbox", "drafts", "sent")


def _quoted(inq):
    """The original, quoted under the reply, the way a mail client does it."""
    when = (inq.get("created_at") or "")[:16].replace("T", " ")
    head = "On %s, %s wrote:" % (when, inq.get("name") or inq.get("email") or "they")
    body = (inq.get("body") or "").strip()
    if not body:
        return "\n\n"
    quoted = "\n".join("> " + line for line in body.splitlines())
    return "\n\n\n%s\n%s" % (head, quoted)


@site.route("/admin/messages")
@site.route("/admin/inquiries")
@admin_required
def admin_inquiries():
    folder = request.args.get("folder", "inbox")
    if folder not in MSG_FOLDERS:
        folder = "inbox"
    rows = []
    if folder == "inbox":
        rows = gallery.list_inquiries()
    elif folder == "drafts":
        rows = gallery.list_replies("draft")
    else:
        rows = gallery.list_replies("sent")
    return render_template("admin/messages.html", folder=folder, rows=rows,
                           counts=gallery.message_counts(), open_msg=None,
                           reply=None, contacts=gallery.contacts_index())


@site.route("/admin/messages/<int:inq_id>")
@admin_required
def admin_message(inq_id):
    """Read one incoming message, with the reply box already under it."""
    inq = gallery.get_inquiry(inq_id)
    if inq is None:
        flash("That message is gone.")
        return redirect(url_for("site.admin_inquiries"))
    # An unsent draft for this message is the one to reopen -- otherwise she
    # would start a second reply and lose the first without being told.
    draft = next((r for r in gallery.replies_for_inquiry(inq_id)
                  if r["status"] == "draft"), None)
    if draft is None:
        subject = "Re: %s" % (inq.get("title") or "your message")
        draft = {"id": None, "to_email": inq.get("email") or "",
                 "to_name": inq.get("name") or "", "subject": subject,
                 "body": _quoted(inq), "error": None}
    return render_template("admin/messages.html", folder="inbox",
                           rows=gallery.list_inquiries(),
                           counts=gallery.message_counts(), open_msg=inq,
                           reply=draft, thread=gallery.replies_for_inquiry(inq_id),
                           contacts=gallery.contacts_index())


@site.route("/admin/messages/draft/<int:rid>")
@admin_required
def admin_message_draft(rid):
    reply = gallery.get_reply(rid)
    if reply is None:
        flash("That draft is gone.")
        return redirect(url_for("site.admin_inquiries", folder="drafts"))
    inq = gallery.get_inquiry(reply["inquiry_id"]) if reply["inquiry_id"] else None
    folder = "sent" if reply["status"] == "sent" else "drafts"
    return render_template("admin/messages.html", folder=folder,
                           rows=gallery.list_replies(reply["status"]),
                           counts=gallery.message_counts(), open_msg=inq,
                           reply=reply, thread=[], contacts=gallery.contacts_index())


@site.route("/admin/messages/new")
@admin_required
def admin_message_new():
    return render_template(
        "admin/messages.html", folder="drafts", rows=gallery.list_replies("draft"),
        counts=gallery.message_counts(), open_msg=None,
        reply={"id": None, "to_email": "", "to_name": "", "subject": "",
               "body": "", "error": None}, thread=[],
        contacts=gallery.contacts_index())


@site.route("/admin/messages/save", methods=["POST"])
@admin_required
def admin_message_save():
    rid = request.form.get("rid", type=int)
    inq_id = request.form.get("inquiry_id", type=int)
    try:
        rid = gallery.save_reply(
            rid, inq_id, request.form.get("to_email"), request.form.get("to_name"),
            request.form.get("subject"), request.form.get("body"))
    except ValueError as exc:
        flash(str(exc))
        return redirect(request.form.get("back") or url_for("site.admin_inquiries"))
    if request.form.get("action") == "send":
        return _send_reply(rid)
    flash("Draft saved.")
    return redirect(url_for("site.admin_message_draft", rid=rid))


def _send_reply(rid):
    reply = gallery.get_reply(rid)
    if reply is None:
        flash("That draft is gone.")
        return redirect(url_for("site.admin_inquiries"))
    if reply["status"] == "sent":
        flash("That message was already sent.")
        return redirect(url_for("site.admin_inquiries", folder="sent"))
    if not (reply["body"] or "").strip():
        flash("Write something first.")
        return redirect(url_for("site.admin_message_draft", rid=rid))

    # Replies leave as the site's verified sending identity, because that is the
    # only domain this app can prove it is allowed to send for.
    #
    # NO Reply-To. It used to be set to artist_email, which was right while
    # nothing at the domain could receive: without it a buyer's reply went
    # nowhere. The domain forwards now, so MAIL_FROM reaches her by itself, and
    # the only thing the header still did was put her personal Gmail address in
    # front of every customer she answered.
    #
    # THIS ASSUMES MAIL_FROM IS AN ADDRESS THAT RECEIVES. If it is ever pointed
    # back at a noreply@ or the forwarding is switched off, replies to her
    # replies fall on the floor silently -- so that variable is now load-bearing
    # in a way it was not before.
    ok = mailer.send(reply["to_email"], reply["subject"] or "(no subject)",
                     reply["body"])
    if ok:
        gallery.mark_reply_sent(rid)
        flash("Sent to %s." % reply["to_email"])
        return redirect(url_for("site.admin_inquiries", folder="sent"))
    gallery.mark_reply_failed(rid, "the mail server would not accept it")
    flash("That would not send — it is still in Drafts, nothing was lost.")
    return redirect(url_for("site.admin_message_draft", rid=rid))


@site.route("/admin/messages/draft/<int:rid>/send", methods=["POST"])
@admin_required
def admin_message_send(rid):
    return _send_reply(rid)


@site.route("/admin/messages/draft/<int:rid>/delete", methods=["POST"])
@admin_required
def admin_message_draft_delete(rid):
    reply = gallery.get_reply(rid)
    gallery.delete_reply(rid)
    flash("Draft deleted." if reply and reply["status"] == "draft"
          else "Message deleted.")
    return redirect(url_for("site.admin_inquiries",
                            folder="sent" if reply and reply["status"] == "sent"
                            else "drafts"))


@site.route("/admin/inquiry/<int:inq_id>/handled", methods=["POST"])
@admin_required
def admin_inq_handled(inq_id):
    gallery.handle_inquiry(inq_id)
    return redirect(request.form.get("back") or url_for("site.admin_inquiries"))


@site.route("/admin/inquiry/<int:inq_id>/delete", methods=["POST"])
@admin_required
def admin_inquiry_delete(inq_id):
    gallery.delete_inquiry(inq_id)
    flash("Message deleted.")
    return redirect(url_for("site.admin_inquiries"))


@site.route("/admin/visitors")
@admin_required
def admin_visitors():
    """Where the visitor numbers are, rather than the numbers themselves.

    They were drawn in here first, reading Umami's API. That needs an API key,
    and the free plan does not issue one -- so rather than leave a page that
    can only explain why it is empty, this hands over to Umami's own dashboard
    and says what is being counted. The link is built from the website id that
    is already set for the counter, so there is nothing extra to configure.
    """
    site_id = (cfg().get("umami_website_id") or "").strip()
    return render_template(
        "admin/visitors.html",
        site_id=site_id,
        dash_url="https://cloud.umami.is/websites/%s" % site_id if site_id else None)


@site.route("/admin/subscribers")
@admin_required
def admin_subscribers():
    everyone = gallery.list_subscribers()
    return render_template(
        "admin/subscribers.html",
        subs=[s for s in everyone if not s["unsubscribed_at"]],
        removed=[s for s in everyone if s["unsubscribed_at"]])


@site.route("/admin/subscriber/<int:sub_id>/remove", methods=["POST"])
@admin_required
def admin_subscriber_remove(sub_id):
    gallery.unsubscribe(sub_id, removed=request.form.get("undo") != "1")
    flash("Put back on the list." if request.form.get("undo") == "1"
          else "Taken off the list. The address is remembered so it cannot be "
               "added back by accident.")
    return redirect(url_for("site.admin_subscribers"))


@site.route("/admin/subscriber/<int:sub_id>/delete", methods=["POST"])
@admin_required
def admin_subscriber_delete(sub_id):
    """Forget an address that is already off the list -- see
    gallery.delete_subscriber for why it is only ever the second step."""
    subs = {s["id"]: s for s in gallery.list_subscribers()}
    email = subs[sub_id]["email"] if sub_id in subs else "that address"
    if gallery.delete_subscriber(sub_id):
        flash(f"Deleted {email} for good. Nothing here remembers them now, so "
              "they can be added again like anyone else.")
    else:
        flash("Take an address off the list before deleting it for good.")
    return redirect(url_for("site.admin_subscribers"))


@site.route("/admin/contacts/add", methods=["POST"])
@admin_required
def admin_contact_add():
    """Put the person who wrote to her on the Contacts list, from the message.

    Source is recorded as "message" so the Contacts page can say where each
    address came from -- these are people who wrote in, not people who asked
    for the mailing list, and she should be able to tell the difference before
    writing to everybody."""
    email = (request.form.get("email") or "").strip()
    result = gallery.add_contact(email, source="message")
    flash({"added": f"Added {email} to Contacts.",
           "already": f"{email} is already in Contacts.",
           # Never resurrected quietly: see gallery.add_contact.
           "removed": f"{email} asked to be taken off the list, so they have "
                      "not been added back. Put them back from Contacts if "
                      "they have asked you to.",
           "bad": "That does not look like an email address."}[result])
    return redirect(request.form.get("back") or url_for("site.admin_inquiries"))


@site.route("/admin/subscribers.csv")
@admin_required
def admin_subscribers_csv():
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(["email", "source", "added"])
    for s in gallery.list_subscribers(include_removed=False):
        wtr.writerow([s["email"], s["source"], s["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=subscribers.csv"})


@site.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        keys = ["site_title", "tagline", "about", "artist_email", "commission_note",
                "umami_website_id",
                "hero_eyebrow", "hero_title", "hero_sub", "hero_caption", "about_caption",
                "box_eyebrow", "box_caption", "box_note", "box_spec", "page_bg",
                "signup_head", "signup_sub", "signup_button",
                "artist_name", "artist_statement", "artist_bio",
                "studio_location"]
        # PRESENT, not "every key with a default". The page is now a stack of
        # small forms -- one per place on the site -- so a post carries the
        # front page's four fields and nothing else. Reading the whole list
        # with a "" default would write an empty string over every field the
        # submitted card does not contain, i.e. saving the headline would wipe
        # the About text. save_settings only writes the keys it is handed.
        vals = {k: request.form[k] for k in keys if k in request.form}

        # A WEBSITE ID, or nothing. Umami identifies the site by the id it
        # gives you when you add the site, which is a UUID -- so what belongs
        # in the box is that id and not the domain and not the whole snippet.
        # Refusing a near miss matters here because every one of them leaves
        # the script loading happily and reporting into nowhere, which looks
        # exactly like "no visitors" rather than like a mistake.
        # Empty is always allowed -- that is how the script comes off the site.
        uid = (request.form.get("umami_website_id") or "").strip().lower()
        if "umami_website_id" in request.form:
            if uid and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                                        r"-[0-9a-f]{4}-[0-9a-f]{12}", uid):
                flash("That does not look like a website ID. Umami shows it on "
                      "the site's Settings page \u2014 a long id with dashes, "
                      "like 0f8c1a2b-3d4e-5f60-7182-93a4b5c6d7e8.")
                vals.pop("umami_website_id", None)
            else:
                vals["umami_website_id"] = uid

        # The rates are entered in dollars but stored in cents, because that is
        # what payments.py charges from. A blank or unreadable field LEAVES THE
        # EXISTING RATE ALONE -- writing 0 there would quietly make shipping
        # free on every order.
        current = cfg()
        for band in ("small", "medium", "large", "rolled"):
            c = _price_cents(request.form.get("ship_" + band))
            if c is not None:
                vals["ship_%s_cents" % band] = c
        f = request.files.get("hero_photo")
        old_hero = cfg().get("hero_image") or ""
        # CHOSEN FROM THE WORK, or uploaded, or cleared -- in that order of
        # insistence. An empty pick means "leave the photograph alone", which is
        # not the same as clearing it, so the two are separate controls.
        # The stem is checked against the images table rather than trusted: it
        # arrives as text in a form field like anything else.
        pick = (request.form.get("hero_pick") or "").strip()
        if f and f.filename:
            try:
                vals["hero_image"] = gallery.store_photo(f.read())
            except ValueError as e:
                flash(str(e))
        elif pick and pick != old_hero and gallery.is_image_base(pick):
            vals["hero_image"] = pick
        elif request.form.get("clear_hero"):
            vals["hero_image"] = ""
        # Six files per replacement, at print widths, so the one it replaces is
        # collected -- but AFTER the new stem is saved, and only if nothing else
        # names the old one. The hero is often a painting's own photograph, and
        # unlinking it here took that painting's picture down with it.
        freed = []
        if old_hero and vals.get("hero_image", old_hero) != old_hero:
            freed.append(old_hero)

        old_about = cfg().get("about_image") or ""
        fa = request.files.get("about_photo")
        if fa and fa.filename:
            try:
                vals["about_image"] = gallery.store_photo(fa.read())
            except ValueError as e:
                flash(str(e))
        elif request.form.get("clear_about"):
            vals["about_image"] = ""
        if old_about and vals.get("about_image", old_about) != old_about:
            freed.append(old_about)

        # Same three moves again for the box photograph on the landing page.
        old_box = cfg().get("box_image") or ""
        fb = request.files.get("box_photo")
        if fb and fb.filename:
            try:
                vals["box_image"] = gallery.store_photo(fb.read())
            except ValueError as e:
                flash(str(e))
        elif request.form.get("clear_box"):
            vals["box_image"] = ""
        if old_box and vals.get("box_image", old_box) != old_box:
            freed.append(old_box)

        # The studio address is stored with the two numbers geocoded from it, so
        # Opportunities can say how far a call is. Looked up only when the text
        # changes, and a failure leaves the address typed and the distance
        # unknown rather than refusing the save.
        if "studio_location" in request.form:
            typed = (request.form.get("studio_location") or "").strip()
            if typed != (cfg().get("studio_location") or "").strip():
                # locate_studio writes the address itself, along with the two
                # numbers, so it owns this key rather than the bulk save below.
                # Called even when she CLEARS the field: that is what drops the
                # stale coordinates with it.
                vals.pop("studio_location", None)
                if app.config.get("TESTING"):
                    gallery.save_settings({"studio_location": typed,
                                           "studio_lat": "", "studio_lon": "",
                                           "studio_place": ""})
                elif not gallery.locate_studio(typed) and typed:
                    flash("Could not find \"%s\" on the map -- calls will show no "
                          "distance until it is written differently. "
                          "\"Town, State\" works best." % typed)

        gallery.save_settings(vals)
        # Now that the new stems are written, the replaced ones are no longer
        # named by settings and can go -- unless a painting's images row still
        # points at them, which drop_image_files checks for itself.
        for stem in freed:
            gallery.drop_image_files(stem)
        # Back to the card she was working in, and marked as saved there rather
        # than in a banner at the top of a long page: on a screen this tall the
        # confirmation has to appear where her eyes already are, or pressing
        # Save looks like it did nothing.
        sec = request.form.get("section", "")
        return redirect(url_for("site.admin_settings", saved=sec or None)
                        + ("#" + sec if sec else ""))
    # The hero can be picked from her own paintings, so the page needs them --
    # drafts included, because a piece can be photographed and written up long
    # before it goes on the wall, and it is still hers to put on the front page.
    return render_template("admin/settings.html",
                           works=gallery.list_works(include_draft=True),
                           # Whether the hero IS one of them, which is what says
                           # if any radio below is already the chosen one.
                           hero_is_work=gallery.is_image_base(cfg().get("hero_image")))


# -------------------------------------------------------------- consignment
# Her work in somebody else's shop. Two questions and no more: where is that
# painting, and who owes me.
@site.route("/admin/consignments", methods=["GET", "POST"])
@admin_required
def admin_consignments():
    if request.method == "POST":
        venue = (request.form.get("venue") or "").strip()
        if not venue:
            flash("A consignment needs a venue.")
            return redirect(url_for("site.admin_consignments"))
        cid = gallery.save_consignment({
            "venue": venue, "city": (request.form.get("city") or "").strip(),
            "commission": request.form.get("commission")})
        return redirect(url_for("site.admin_consignment", consignment_id=cid))
    active, ended = gallery.list_consignments()
    return render_template("admin/consignments.html", active=active, ended=ended,
                           totals=gallery.consignment_totals())


@site.route("/admin/consignment/<int:consignment_id>", methods=["GET", "POST"])
@admin_required
def admin_consignment(consignment_id):
    c = gallery.get_consignment(consignment_id)
    if not c:
        abort(404)
    if request.method == "POST":
        f = request.form
        gallery.save_consignment({
            "venue": f.get("venue") or c["venue"],
            "contact": (f.get("contact") or "").strip(),
            "city": (f.get("city") or "").strip(),
            "commission": f.get("commission"),
            "starts_on": f.get("starts_on"), "ends_on": f.get("ends_on"),
            "url": (f.get("url") or "").strip(),
            "notes": (f.get("notes") or "").strip(),
            "status": f.get("status"),
        }, consignment_id)
        flash("Saved.")
        return redirect(url_for("site.admin_consignment", consignment_id=consignment_id))
    out_ids = {r["work_id"] for r in c["works"]}
    # Only pieces that are here and sellable can be sent out. A sold one is
    # gone and a draft is not finished.
    pickable = [w for w in gallery.list_works(include_draft=False)
                if w["id"] not in out_ids and w["status"] in ("available", "nfs")]
    return render_template("admin/consignment_form.html", c=c, pickable=pickable)


@site.route("/admin/consignment/<int:consignment_id>/send", methods=["POST"])
@admin_required
def admin_consignment_send(consignment_id):
    c = gallery.get_consignment(consignment_id)
    if not c:
        abort(404)
    ids = [_int(x) for x in request.form.getlist("work_ids") if _int(x)]
    if ids:
        gallery.send_out(consignment_id, ids, venue_label=c["venue"])
        flash("%d piece%s out to %s." % (len(ids), "" if len(ids) == 1 else "s", c["venue"]))
    return redirect(url_for("site.admin_consignment", consignment_id=consignment_id))


@site.route("/admin/consignment/<int:consignment_id>/work/<int:work_id>/<action>",
            methods=["POST"])
@admin_required
def admin_consignment_work(consignment_id, work_id, action):
    c = gallery.get_consignment(consignment_id)
    if not c:
        abort(404)
    if action == "home":
        gallery.bring_home(consignment_id, work_id)
        flash("Back in the studio.")
    elif action == "sold":
        cents = _price_cents(request.form.get("sold"))
        if cents is None:
            flash("How much did it sell for?")
        else:
            gallery.record_consignment_sale(consignment_id, work_id, cents)
            flash("Sold. %s owed to you once %s pays."
                  % (gallery.money(gallery.her_share(cents, c["commission"])), c["venue"]))
    elif action == "paid":
        gallery.record_consignment_payment(consignment_id, work_id,
                                           paid=not request.form.get("undo"))
        flash("Payment recorded." if not request.form.get("undo") else "Marked unpaid.")
    elif action == "remove":
        gallery.remove_from_consignment(consignment_id, work_id)
        flash("Taken off this consignment.")
    else:
        abort(404)
    return redirect(url_for("site.admin_consignment", consignment_id=consignment_id))


@site.route("/admin/consignment/<int:consignment_id>/delete", methods=["POST"])
@admin_required
def admin_consignment_delete(consignment_id):
    gallery.delete_consignment(consignment_id)
    flash("Consignment removed. The paintings themselves are untouched.")
    return redirect(url_for("site.admin_consignments"))


# ------------------------------------------------------------- opportunities
@site.route("/admin/opportunities", methods=["GET", "POST"])
@admin_required
def admin_opportunities():
    """Calls for entry, grants, residencies. Adding one needs a name and the
    date it closes; everything else is filled in on its own page once she has
    decided it is worth applying to."""
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("A call needs a name.")
            return redirect(url_for("site.admin_opportunities"))
        oid = gallery.save_opportunity({"title": title,
                                        "deadline": request.form.get("deadline")})
        return redirect(url_for("site.admin_opportunity", opportunity_id=oid))
    # Plain GET so a filtered view is bookmarkable -- "everything inside 75
    # miles" is a view she comes back to, not a mode she sets each visit.
    # No `within` in the URL = the 35-mile default (Paul, 2026-09-28: light the
    # 35 mi button "so you know the area"); within=0 = any distance.
    within = _int(request.args.get("within"), gallery.FOUND_RADIUS_MILES) \
        if "within" in request.args else gallery.FOUND_RADIUS_MILES
    within = within or None
    order = "distance" if request.args.get("sort") == "distance" else "deadline"
    # Kick the drain on the way past. Cheap when there is nothing to do: one
    # indexed query that returns no rows.
    start_distance_worker()
    # Same idea for calls that have closed since she last looked. list_found
    # already hides them, so this is housekeeping rather than the fix -- but it
    # keeps the pen from growing forever, keeps found_counts honest, and stops
    # the geocoder spending a second a town on calls nobody can enter. Pressing
    # Get the latest does the same sweep and says how many it cleared.
    gallery.drop_closed_found()
    live, done = gallery.list_opportunities(within=within, order=order)
    return render_template("admin/opportunities.html", live=live, done=done,
                           within=within, order=order,
                           here=gallery.studio_point(),
                           pending=len(gallery.ungeocoded_opportunities()),
                           placing=len(gallery.found_without_distance()),
                           # Open calls from EntryThingy's published listings,
                           # waiting in the pen to be judged.
                           #
                           # ONE WINDOW, chosen here rather than offered as a
                           # control. Three chips and a "29 more close later"
                           # line were three decisions a week about a list whose
                           # whole job is to say what she can act on now. A call
                           # further out is not lost -- it appears by itself on
                           # the day it comes inside the window.
                           found=gallery.list_found("new", gallery.FOUND_DEFAULT_DAYS,
                                                   gallery.FOUND_RADIUS_MILES),
                           horizon=gallery.FOUND_DEFAULT_DAYS,
                           radius=gallery.FOUND_RADIUS_MILES,
                           found_counts=gallery.found_counts(),
                           last_found=gallery.last_found_at(),
                           states=listings.states_for(cfg()),
                           no_distance=len(gallery.found_without_distance()),
                           lookups=gallery.where_to_look(cfg()))


# ------------------------------------------------------- calls from outside
# EntryThingy publishes schema.org JSON-LD on its public listing pages and its
# robots.txt asks to be read. No key, no bill, no model -- see listings.py.
@site.route("/admin/opportunities/refresh", methods=["POST"])
@admin_required
def admin_opportunities_refresh():
    today_ = gallery.today()
    gone = gallery.drop_closed_found()
    calls, note = listings.open_calls(cfg(), today=today_)
    fresh = gallery.record_found(calls) if calls else 0
    bits = [note]
    if fresh:
        bits.append("%d new." % fresh)
    elif calls:
        bits.append("Nothing new since last time.")
    if gone:
        bits.append("%d closed one%s cleared." % (gone, "" if gone == 1 else "s"))
    flash(" ".join(bits))
    start_distance_worker()
    return redirect(url_for("site.admin_opportunities") + "#found")


# Distances fill in ON THEIR OWN, in a thread, one town a second.
#
# It cannot happen in the request that asks for them: Nominatim wants a second
# between lookups, fifty listings is fifty seconds, and gunicorn would have cut
# the response long before that. It used to be a button for exactly that
# reason. A button she has to press repeatedly to finish a job the machine
# could finish alone is a chore, so the machine finishes it.
#
# This is not a scheduler and does not pretend to be one -- see the note in the
# reminder code. It is a worker that drains a queue and stops, started by the
# page that wants the answers. If the instance restarts mid-drain the queue is
# still in the database and the next page view starts it again.
_distance_worker = threading.Lock()


def _drain_distances(app_obj):
    """One town a second until the queue is empty. Never raises into a request;
    it is not running in one."""
    try:
        while True:
            claim = gallery.claim_next_unplaced()
            if not claim:
                return
            try:
                gallery.place_found(*claim)      # sleeps 1.05s on a real lookup
            except Exception:
                continue                          # a bad row must not stop the rest
    finally:
        try:
            _distance_worker.release()
        except RuntimeError:
            pass


def start_distance_worker():
    """Start the drain if there is anything to drain and nobody is draining.

    The lock is per PROCESS; the claim in claim_next_unplaced is what keeps two
    gunicorn workers from asking the same question twice.
    """
    if app.config.get("TESTING") or not gallery.found_without_distance():
        return False
    if not _distance_worker.acquire(blocking=False):
        return False        # already draining in this process
    t = threading.Thread(target=_drain_distances, args=(app,), daemon=True,
                         name="distances")
    t.start()
    return True


@site.route("/admin/found/<int:found_id>/<action>", methods=["POST"])
@admin_required
def admin_found(found_id, action):
    f = gallery.get_found(found_id)
    if not f:
        abort(404)
    if action == "dismiss":
        # Remembered, not deleted: a call she has already turned down must not
        # be offered again by the next refresh.
        gallery.set_found_status(found_id, "dismissed")
        # Asked for by the page's own script? Then say nothing and let it take
        # the row out where it stands. A redirect here sent her to the top of
        # the Calls section, which is a long way from the row she was reading
        # when the list runs to thirty. The form still works without any of
        # this -- see the fallback below.
        if request.headers.get("X-Requested-With") == "fetch":
            return ("", 204)
        flash("Put aside.")
        return redirect(url_for("site.admin_opportunities") + "#found")
    if action != "add":
        abort(404)
    oid = gallery.save_opportunity({
        "title": f["title"], "org": f["org"], "location": f["location"],
        "deadline": f["deadline"], "url": f["url"], "kind": f["kind"] or "show",
        # The fee arrives as published ("$35", "free") and the column is cents.
        # Parse what parses and leave the rest -- a wrong number in a money
        # field is worse than an empty one.
        "fee_cents": _price_cents((f["fee"] or "").replace("$", "").strip()),
        "notes": "From the %s listings, %s.%s"
                 % (f["source"] or "public", (f["found_at"] or "")[:10],
                    ("\n\n" + f["why"]) if f["why"] else ""),
        "status": "watching",
    })
    gallery.set_found_status(found_id, "added", oid)
    if not app.config.get("TESTING"):
        gallery.locate_opportunity(oid, (f["location"] or "").strip())
    flash("Added to your list.")
    return redirect(url_for("site.admin_opportunity", opportunity_id=oid))


@site.route("/admin/opportunities/locate", methods=["POST"])
@admin_required
def admin_opportunities_locate():
    """Work out coordinates for the rows that have a location but no distance.

    A button rather than something that happens quietly on a page view: this
    talks to somebody else's server, one row a second, and the person who
    pressed it is the right person to be waiting for it. Capped per press so a
    long list never hangs the request -- press it again for the next batch.
    """
    done = 0
    for row in gallery.ungeocoded_opportunities()[:12]:
        gallery.locate_opportunity(row["id"], row["location"])
        done += 1
        if done < 12:
            time.sleep(1.05)      # Nominatim asks for one request a second
    left = len(gallery.ungeocoded_opportunities())
    flash("Worked out %d location%s.%s" % (done, "" if done == 1 else "s",
          (" %d still to do -- press it again." % left) if left else ""))
    return redirect(url_for("site.admin_opportunities"))


@site.route("/admin/opportunity/<int:opportunity_id>", methods=["GET", "POST"])
@admin_required
def admin_opportunity(opportunity_id):
    o = gallery.get_opportunity(opportunity_id)
    if not o:
        abort(404)
    if request.method == "POST":
        f = request.form
        gallery.save_opportunity({
            "title": f.get("title") or o["title"],
            "org": (f.get("org") or "").strip(),
            "kind": f.get("kind"),
            "url": (f.get("url") or "").strip(),
            "location": (f.get("location") or "").strip(),
            "fee_cents": _price_cents(f.get("fee")),
            "opens_on": f.get("opens_on"),
            "deadline": f.get("deadline"),
            "notified_on": f.get("notified_on"),
            "event_on": f.get("event_on"),
            "event_ends": f.get("event_ends"),
            "max_works": _int(f.get("max_works")),
            "img_longest": _int(f.get("img_longest")),
            "img_max_mb": _float(f.get("img_max_mb")),
            "notes": (f.get("notes") or "").strip(),
            "status": f.get("status"),
            # Kept rather than recomputed, so correcting a typo in the notes
            # months later does not move the date she actually submitted.
            "applied_on": o.get("applied_on"),
        }, opportunity_id)
        gallery.set_opportunity_works(opportunity_id, f.getlist("work_ids"))
        # Only when the text actually changed, and never in a way that can lose
        # her edit: locate_opportunity swallows every failure, so a geocoder
        # that is down or slow costs a distance, not the save.
        if not app.config.get("TESTING"):
            gallery.locate_opportunity(opportunity_id, (f.get("location") or "").strip())
        flash("Saved.")
        return redirect(url_for("site.admin_opportunity", opportunity_id=opportunity_id))
    return render_template("admin/opportunity_form.html", o=o,
                           works=gallery.list_works(include_draft=True),
                           kinds=gallery.OPP_KINDS, statuses=gallery.OPP_STATUSES)


@site.route("/admin/opportunity/<int:opportunity_id>/delete", methods=["POST"])
@admin_required
def admin_opportunity_delete(opportunity_id):
    o = gallery.get_opportunity(opportunity_id)
    gallery.delete_opportunity(opportunity_id)
    flash(f"Deleted {o['title'] if o else 'it'}. The paintings that were "
          "submitted to it are untouched.")
    return redirect(url_for("site.admin_opportunities"))


# ---------------------------------------------------------- submission packet
@site.route("/admin/packet", methods=["GET", "POST"])
@admin_required
def admin_packet():
    """Build the folder a call asks for: JPEGs at their spec, named to their
    pattern, plus the image list.

    GET renders the builder. POST streams a zip -- it is never written to disk
    and never cached, because it is derived from the works every time and a
    stale copy is worse than no copy.
    """
    c = cfg()
    artist = (c.get("artist_name") or "").strip()
    opp_id = _int(request.values.get("opportunity"))
    o = gallery.get_opportunity(opp_id) if opp_id else None

    if request.method == "POST":
        ids = [_int(i) for i in request.form.getlist("work_ids")]
        picked = [w for w in (gallery.get_work(work_id=i) for i in ids if i) if w]
        if not picked:
            flash("Tick at least one painting.")
            return redirect(url_for("site.admin_packet", opportunity=opp_id or None))
        pattern = request.form.get("pattern")
        if pattern not in gallery.NAME_PATTERNS:
            pattern = "last_title"
        longest = _int(request.form.get("longest"), 1920) or 0
        max_mb = _float(request.form.get("max_mb"))
        zf, rows, skipped = gallery.build_packet(
            picked, artist, pattern=pattern, longest=longest, max_mb=max_mb,
            statement=c.get("artist_statement", "") if request.form.get("inc_statement") else "",
            bio=c.get("artist_bio", "") if request.form.get("inc_bio") else "",
            title_line=(o["title"] if o else ""),
            note=("Submitted to %s" % o["title"]) if o else "")
        if not rows:
            flash("Nothing to send — none of those have a photograph yet.")
            return redirect(url_for("site.admin_packet", opportunity=opp_id or None))
        # Recording the submission is the point of picking a call: next year she
        # can see this piece was already sent there. Additive, so building a
        # second packet for the same call does not erase the first.
        if o:
            gallery.add_opportunity_works(o["id"], [w["id"] for w in picked if w.get("images")])
        stem = gallery._ascii_token(o["title"] if o else "Submission")
        name = f"{gallery.last_name(artist)}_{stem}.zip"
        resp = send_file(zf, mimetype="application/zip",
                         as_attachment=True, download_name=name)
        resp.headers["Cache-Control"] = "no-store"
        if skipped:
            resp.headers["X-Packet-Skipped"] = str(len(skipped))
        return resp

    return render_template("admin/packet.html", o=o,
                           works=gallery.list_works(include_draft=True),
                           artist=artist,
                           patterns=gallery.NAME_PATTERNS,
                           opps=gallery.list_opportunities(include_done=False)[0],
                           has_statement=bool((c.get("artist_statement") or "").strip()),
                           has_bio=bool((c.get("artist_bio") or "").strip()),
                           preselect=set(o["work_ids"]) if o else set())


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


# ------------------------------------------------------- deadline reminders
# THERE IS NO SCHEDULER ON THIS HOST. The app runs as a web process on Render
# and nothing wakes it on a timer, so the digest is sent from an ordinary
# request instead: cheap to check, at most once a day, and self-healing if the
# process restarts. The trade is honest and worth stating -- if the site gets
# no traffic at all on a given day, no digest goes out that day. It is never
# the only warning: the same list is on the Studio home whenever she opens it.
REMIND_WITHIN_DAYS = 14
# One database check per worker per day. Without this the hook would query on
# every single request to the public site for the sake of an email that is sent
# at most once -- the stamp in settings stops the SEND, this stops the LOOKING.
_remind_checked_on = None


def _maybe_remind():
    """Best effort, always silent. An exception here must never take down a
    page a visitor asked for."""
    global _remind_checked_on
    try:
        # Never from a test run. Found the hard way: exercising the new routes
        # through the test client fired a real digest at the artist's address
        # (it bounced, but only because this box cannot authenticate to Gmail).
        # A test must not be able to mail a person.
        if app.config.get("TESTING"):
            return
        d = gallery.today()
        if _remind_checked_on == d:
            return
        to = (cfg().get("artist_email") or "").strip()
        if not to:
            return
        due = gallery.due_soon(REMIND_WITHIN_DAYS)
        _remind_checked_on = d
        if not due:
            return
        if not gallery.claim_reminder_day():
            return
        lines = []
        for o in due:
            when = "today" if o["days"] == 0 else (
                "tomorrow" if o["days"] == 1 else "in %d days" % o["days"])
            bits = [o["title"]]
            if o["org"]:
                bits.append(o["org"])
            lines.append("%s\n  closes %s (%s)%s" % (
                " - ".join(bits), day(o["deadline"]), when,
                "\n  " + o["url"] if o["url"] else ""))
        n = len(due)
        mailer.send(
            to,
            "%d call%s closing soon" % (n, "" if n == 1 else "s"),
            "Deadlines inside the next %d days:\n\n%s\n\nOpen the studio: %s\n"
            % (REMIND_WITHIN_DAYS, "\n\n".join(lines),
               url_for("site.admin_opportunities", _external=True)))
    except Exception:
        pass


@app.before_request
def _remind_hook():
    # Skips its own work on nearly every request: due_soon reads one small
    # table, and the day stamp stops anything after the first send.
    if request.method == "GET" and not request.path.endswith((".css", ".js", ".jpg",
                                                              ".webp", ".png", ".ico")):
        _maybe_remind()


app.register_blueprint(site)

# Idempotent (CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE / makedirs exist_ok),
# and deliberately at module scope rather than inside the __main__ guard: under a
# WSGI server there is no __main__, so a fresh deploy onto an empty disk would
# otherwise start with no database at all and fail on the first request.
gallery.init_db()

if __name__ == "__main__":
    # Local/dev only. In production gunicorn does the binding, which is why this
    # stays on the loopback -- behind the Apache proxy here, nothing should be
    # listening on a public interface.
    app.run(host="127.0.0.1", port=PORT)
