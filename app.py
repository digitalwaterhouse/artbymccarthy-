"""Art by McCarthy -- a small gallery shop.

Runs behind Apache on a subpath (APP_PREFIX), which is why every route hangs
off a blueprint rather than off the app: moving to the artist's own domain
later means setting APP_PREFIX="" and repointing the proxy, nothing else.
"""
import os
import csv
import json
import io
import functools

from flask import (Flask, Blueprint, render_template, request, redirect,
                   has_request_context,
                   url_for, session, abort, send_from_directory, jsonify,
                   Response, flash)
from werkzeug.middleware.proxy_fix import ProxyFix

import gallery
import mailer
import payments

PREFIX = os.environ.get("APP_PREFIX", "/artbymccarthy").rstrip("/")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
PORT = int(os.environ.get("PORT", "5070"))
# While the shop lives on a borrowed path it must not be indexed under
# somebody else's domain -- the site's own robots.txt sits at the domain
# root, out of this app's reach, so the meta tag is the only lever here.
NOINDEX = os.environ.get("NOINDEX", "1") == "1"

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
           "about": "about", "contact": "contact"}.get(ep, "")
    return {"cfg": c, "prefix": PREFIX, "stripe_on": payments.enabled(),
            "noindex": NOINDEX, "wordmark": wordmark(c["site_title"]),
            "signature_name": signature_name(), "pagekey": key, "nav": nav, "endpoint": ep}


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
    # The viewer shows everything with a photograph -- sold work included,
    # because a sold painting is the best argument for the next one.
    slides = ([_viewer_entry(w, "available") for w in works if w["images"]]
              + [_viewer_entry(w, "archive") for w in sold if w["images"]])
    c = cfg()
    hero = {
        "base": c.get("hero_image") or "",
        "title": c.get("hero_title") or c.get("tagline") or "Original paintings",
        "sub": c.get("hero_sub") or "",
        "caption": c.get("hero_caption") or "",
    }
    if not hero["base"]:
        first = next((w for w in works + sold if w["images"]), None)
        if first:
            hero["base"] = first["images"][0]["base"]
    return render_template("index.html", works=works, slides=slides, hero=hero,
                           has_archive=bool(sold))


@site.route("/archive")
def archive():
    return render_template("archive.html", works=gallery.list_works(status="sold"))


@site.route("/work/<slug>")
def work(slug):
    gallery.release_expired()
    w = gallery.get_work(slug=slug)
    if not w:
        abort(404)
    return render_template("work.html", w=w, near=gallery.neighbours(w))


@site.route("/about")
def about():
    return render_template("about.html")


@site.route("/commissions", methods=["GET", "POST"])
def commissions():
    """Same page as /contact, opened on the commission pane. The URL is kept so
    existing links and the sitemap still land somewhere sensible."""
    if request.method == "POST":
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
        gallery.add_inquiry("contact", request.form.get("name"),
                            request.form.get("email"), request.form.get("body"))
        notify("contact", request.form.get("name"), request.form.get("email"),
               request.form.get("body"))
        return render_template("thanks.html", heading="Thank you",
                               msg="Your message has been received.")
    return render_template("contact.html", mode="message")


@site.route("/subscribe", methods=["POST"])
def subscribe():
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
        event = payments.parse_webhook(request.data, request.headers.get("Stripe-Signature", ""))
    except Exception:
        return "bad signature", 400
    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        work_id = _int((s.get("metadata") or {}).get("work_id"))
        if work_id:
            gallery.set_status(work_id, "sold")
            details = s.get("customer_details") or {}
            ship = ((s.get("shipping_details") or {}).get("address")
                    or (details.get("address") or {}))
            gallery.record_order(work_id, s.get("id"), s.get("amount_total"),
                                 {"name": details.get("name"), "email": details.get("email")},
                                 ship or {})
    elif event["type"] in ("checkout.session.expired",):
        work_id = _int((event["data"]["object"].get("metadata") or {}).get("work_id"))
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
        if ADMIN_PASS and request.form.get("password") == ADMIN_PASS:
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
    return render_template("admin/works.html", works=gallery.list_works(),
                           orders=gallery.list_orders()[:5],
                           inquiries=[i for i in gallery.list_inquiries() if not i["handled"]])


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
    return render_template("admin/work_form.html", w=w)


@site.route("/admin/work/<int:work_id>/status", methods=["POST"])
@admin_required
def admin_status(work_id):
    st = request.form.get("status")
    if st in gallery.STATUSES:
        gallery.set_status(work_id, st)
    return redirect(request.form.get("back") or url_for("site.admin_works"))


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


@site.route("/admin/inquiries")
@admin_required
def admin_inquiries():
    return render_template("admin/inquiries.html", inquiries=gallery.list_inquiries())


@site.route("/admin/inquiry/<int:inq_id>/handled", methods=["POST"])
@admin_required
def admin_inq_handled(inq_id):
    gallery.handle_inquiry(inq_id)
    return redirect(url_for("site.admin_inquiries"))


@site.route("/admin/subscribers")
@admin_required
def admin_subscribers():
    return render_template("admin/subscribers.html", subs=gallery.list_subscribers())


@site.route("/admin/subscribers.csv")
@admin_required
def admin_subscribers_csv():
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(["email", "source", "added"])
    for s in gallery.list_subscribers():
        wtr.writerow([s["email"], s["source"], s["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=subscribers.csv"})


@site.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        keys = ["site_title", "tagline", "about", "artist_email", "commission_note",
                "hero_title", "hero_sub", "hero_caption", "about_caption", "page_bg"]
        vals = {k: request.form.get(k, "") for k in keys}

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
        gallery.save_settings(vals)
        return redirect(url_for("site.admin_settings"))
    return render_template("admin/settings.html")


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


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
