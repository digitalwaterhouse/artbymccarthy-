# Deploying to Render

The app is a plain Flask + SQLite process. Everything that changes at runtime —
the database and every uploaded painting photo — lives under `data/`, which is
why that directory is gitignored and must be a **persistent disk** on Render.
Without the disk, a redeploy silently empties the gallery.

## Render settings

| Setting | Value |
|---|---|
| Environment | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT` |
| Instance type | Starter ($7/mo) — chosen on the service-creation form, not at signup |
| Disk mount path | `/opt/render/project/src/data` |
| Disk size | 1 GB (81 MB in use; a few MB per painting) |

## Environment variables

Set these in Render's dashboard — never in the repo.

    APP_PREFIX=            # empty: the app is at the domain root, not /artbymccarthy
    NOINDEX=0              # it should be indexed on her own domain
    SECRET_KEY=            # generate a fresh one, do not reuse the old server's
    ADMIN_PASS=            # likewise
    STRIPE_SECRET_KEY=     # sk_test_ first
    STRIPE_WEBHOOK_SECRET=
    STRIPE_TAX=0
    SMTP_HOST=             # no local postfix on Render — use Resend or enquiries die silently
    SMTP_PORT=
    MAIL_FROM=

`APP_PREFIX` drives the routes, `static_url_path` and `SESSION_COOKIE_PATH`
together, so setting it empty is the whole move off the subpath.

## Order of operations

1. Deploy on the Free instance first and confirm the build works and pages render.
2. Switch to Starter and attach the disk at the mount path above.
3. Upload a photo through `/admin`, redeploy, confirm it survived. This is the
   persistent-disk check and it is the failure that is expensive to find later.
4. Move `data/` across from the old host (`gallery.db` plus `photos/` and
   `originals/`).
5. Point the domain's A record at Render, let the certificate issue.
6. Register the Stripe webhook at `https://<domain>/stripe/webhook`.
7. Set up the nightly backup — `backup.sh` ran at 03:40 by cron on the old box.

## Things already fixed, do not re-earn

* `gallery.init_db()` runs at module scope, not under `__main__`, so gunicorn
  creates the schema on a fresh disk.
* Flask serves `/static` from the app root rather than the blueprint; handled
  automatically once `APP_PREFIX` is empty.
* If a proxy ever sits in front of this, it must send `X-Forwarded-Proto`, or
  every absolute URL comes out `http` and Stripe rejects the return URL.
