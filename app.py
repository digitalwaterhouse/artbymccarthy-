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
from datetime import datetime

from flask import (Flask, Blueprint, render_template, request, redirect,
                   has_request_context, g,
                   url_for, session, abort, send_from_directory, jsonify,
                   Response, flash, send_file)
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
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
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


@app.context_processor
def inject():
    c = cfg()
    # Which quiet backdrop this page gets. Taken from the endpoint rather than
    # from a Jinja block: a block renders, so it printed "about" as loose text
    # at the top of the document.
    ep = (request.endpoint or "").rsplit(".", 1)[-1] if has_request_context() else ""
    ep = PAGE_ALIAS.get(ep, ep)
    key = ep if ep in QUIET_PAGES else ""
    # Which nav item to light up. A single work and the archive still belong
    # under Work, and /commissions is the Contact page wearing another URL.
    nav = {"index": "work", "work": "work", "archive": "work",
           "collection": "work",
           "about": "about", "contact": "contact"}.get(ep, "")
    # slides/has_archive go to every template because the viewer is included
    # from base.html now rather than from the landing page alone.
    slides = viewer_slides()
    return {"cfg": c, "prefix": PREFIX, "stripe_on": payments.enabled(),
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
@app.template_filter("hostof")
def hostof(url):
    """Show a source as its site, not as 80 characters of query string."""
    m = re.match(r"https?://(?:www\.)?([^/]+)", (url or "").strip(), re.I)
    return m.group(1) if m else (url or "")


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
        "slug": w["slug"], "title": w["title"], "year": w["year"],
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
    if not hero["base"]:
        first = next((w for w in works + sold if w["images"]), None)
        if first:
            hero["base"] = first["images"][0]["base"]
    return render_template("index.html", works=works, hero=hero)


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
    return render_template("work.html", w=w, near=gallery.neighbours(w))


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


@site.route("/buy/<slug>", methods=["POST"])
def buy(slug):
    w = gallery.get_work(slug=slug)
    if not w:
        abort(404)
    if w["status"] != "available":
        return redirect(url_for("site.work", slug=slug))
    if not payments.enabled() or w["ship_band"] == "quote" or not w["price_cents"]:
        # No keys yet, a piece too large to price shipping on sight, or a work
        # that has no price set yet: the enquiry is the checkout. Without the
        # last case the Enquire button on an unpriced work posted here and was
        # bounced straight back to the page it came from, doing nothing.
        return render_template("enquire.html", w=w)
    if not gallery.reserve(w["id"]):
        return redirect(url_for("site.work", slug=slug))
    try:
        s = payments.create_session(
            w,
            success_url=url_for("site.thanks", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("site.work", slug=slug, _external=True))
    except Exception:
        gallery.set_status(w["id"], "available")
        raise
    return redirect(s.url, code=303)


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


@site.route("/thanks")
def thanks():
    return render_template("thanks.html", heading="Thank you",
                           msg="Your receipt is on its way by email. The painting will be "
                               "packed and shipped within a few days, and you'll get tracking.")


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
    work_id = _int((obj.get("metadata") or {}).get("work_id"))
    if kind == "checkout.session.completed":
        if work_id:
            gallery.set_status(work_id, "sold")
            details = obj.get("customer_details") or {}
            ship = ((obj.get("shipping_details") or {}).get("address")
                    or (details.get("address") or {}))
            gallery.record_order(work_id, obj.get("id"), obj.get("amount_total"),
                                 {"name": details.get("name"), "email": details.get("email")},
                                 ship or {})
    elif kind == "checkout.session.expired":
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
                   stripe=payments.enabled(), live=payments.live_mode(), prefix=PREFIX)


@site.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: %s\n"
                    % url_for("site.sitemap", _external=True), mimetype="text/plain")


@site.route("/sitemap.xml")
def sitemap():
    urls = [url_for("site.index", _external=True), url_for("site.about", _external=True),
            url_for("site.archive", _external=True), url_for("site.commissions", _external=True)]
    urls += [url_for("site.work", slug=w["slug"], _external=True) for w in gallery.list_works()]
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
                           due=gallery.due_soon(REMIND_WITHIN_DAYS))


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
    on, been = gallery.list_outings()
    return render_template("admin/exhibitions.html", upcoming=upcoming, past=past,
                           on=on, been=been)


# --------------------------------------------------------- shows to go and see
# Somebody else's show. A separate table from exhibitions on purpose -- see the
# schema note -- and a separate set of routes, sharing only the page they are
# read on and the distance machinery.
@site.route("/admin/outings", methods=["POST"])
@admin_required
def admin_outing_new():
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("A show needs a name.")
        return redirect(url_for("site.admin_exhibitions"))
    oid = gallery.save_outing({"title": title, "city": (request.form.get("city") or "").strip(),
                               "ends_on": request.form.get("ends_on")})
    if not app.config.get("TESTING"):
        gallery.locate_outing(oid)
    return redirect(url_for("site.admin_outing", outing_id=oid))


@site.route("/admin/outing/<int:outing_id>", methods=["GET", "POST"])
@admin_required
def admin_outing(outing_id):
    o = gallery.get_outing(outing_id)
    if not o:
        abort(404)
    if request.method == "POST":
        f = request.form
        gallery.save_outing({
            "title": f.get("title") or o["title"],
            "venue": (f.get("venue") or "").strip(),
            "city": (f.get("city") or "").strip(),
            "starts_on": f.get("starts_on"),
            "ends_on": f.get("ends_on"),
            "url": (f.get("url") or "").strip(),
            "notes": (f.get("notes") or "").strip(),
            "went": f.get("went"),
        }, outing_id)
        if not app.config.get("TESTING"):
            gallery.locate_outing(outing_id)
        flash("Saved.")
        return redirect(url_for("site.admin_outing", outing_id=outing_id))
    return render_template("admin/outing_form.html", o=o)


@site.route("/admin/outing/<int:outing_id>/went", methods=["POST"])
@admin_required
def admin_outing_went(outing_id):
    """One click from the list, because "I went" is the only thing she will
    ever want to change without opening the row."""
    gallery.set_outing_went(outing_id, request.form.get("went") == "1")
    return redirect(url_for("site.admin_exhibitions") + "#tosee")


@site.route("/admin/outing/<int:outing_id>/delete", methods=["POST"])
@admin_required
def admin_outing_delete(outing_id):
    gallery.delete_outing(outing_id)
    flash("Removed.")
    return redirect(url_for("site.admin_exhibitions") + "#tosee")


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
    return render_template("admin/orders.html", orders=gallery.list_orders())


@site.route("/admin/order/<int:order_id>/shipped", methods=["POST"])
@admin_required
def admin_shipped(order_id):
    gallery.mark_shipped(order_id, request.form.get("tracking"))
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
                "hero_title", "hero_sub", "hero_caption", "about_caption",
                "box_caption", "box_note", "page_bg",
                "artist_name", "artist_statement", "artist_bio",
                "studio_location"]
        # PRESENT, not "every key with a default". The page is now a stack of
        # small forms -- one per place on the site -- so a post carries the
        # front page's four fields and nothing else. Reading the whole list
        # with a "" default would write an empty string over every field the
        # submitted card does not contain, i.e. saving the headline would wipe
        # the About text. save_settings only writes the keys it is handed.
        vals = {k: request.form[k] for k in keys if k in request.form}

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
        if f and f.filename:
            try:
                vals["hero_image"] = gallery.store_photo(f.read())
            except ValueError as e:
                flash(str(e))
        elif request.form.get("clear_hero"):
            vals["hero_image"] = ""
        # The hero photo has no images row, so nothing else would ever collect
        # it -- six files per replacement, at print widths.
        if old_hero and vals.get("hero_image", old_hero) != old_hero:
            gallery.drop_image_files(old_hero)

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
            gallery.drop_image_files(old_about)

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
            gallery.drop_image_files(old_box)

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
        # Back to the card she was working in, and marked as saved there rather
        # than in a banner at the top of a long page: on a screen this tall the
        # confirmation has to appear where her eyes already are, or pressing
        # Save looks like it did nothing.
        sec = request.form.get("section", "")
        return redirect(url_for("site.admin_settings", saved=sec or None)
                        + ("#" + sec if sec else ""))
    return render_template("admin/settings.html")


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
    within = _int(request.args.get("within"))
    order = "distance" if request.args.get("sort") == "distance" else "deadline"
    live, done = gallery.list_opportunities(within=within, order=order)
    return render_template("admin/opportunities.html", live=live, done=done,
                           within=within, order=order,
                           here=gallery.studio_point(),
                           pending=len(gallery.ungeocoded_opportunities()),
                           # Open calls pulled from EntryThingy's published
                           # listings, waiting in the pen to be judged.
                           found=gallery.list_found("new"),
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
    gone = gallery.drop_closed_found(today_)
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
    return redirect(url_for("site.admin_opportunities") + "#found")


@site.route("/admin/opportunities/found-distance", methods=["POST"])
@admin_required
def admin_found_distance():
    """Distances for the listings, through the cached place lookup. A town seen
    before is free and instant; a new one is a request a second to somebody
    else's server, so this is a button and capped per press."""
    done, placed = 0, 0
    # Twelve a press: uncached towns are a second each, and a request that
    # takes half a minute looks broken however honest it is.
    for row in gallery.found_without_distance()[:12]:
        placed += 1 if gallery.locate_found(row["id"], row["location"]) else 0
        done += 1
    left = len(gallery.found_without_distance())
    flash("Looked up %d town%s, %d placed on the map.%s"
          % (done, "" if done == 1 else "s", placed,
             (" %d to go -- press again." % left) if left else ""))
    return redirect(url_for("site.admin_opportunities") + "#found")


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
