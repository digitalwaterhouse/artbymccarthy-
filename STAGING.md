# The staging copy

A second Render service running the **same repo on the `staging` branch**, with
its own database and its own photos. Nothing it does can reach a customer.

The point of it is narrow and worth stating: *somewhere a change can be wrong.*
Before it existed, every commit was live on her shop within two minutes — which
was fine until the day a migration would have 500'd the first press of a button.

## The workflow

    git checkout staging
    ...work...
    git push origin staging          # staging.artbymccarthy.com rebuilds

    # once it looks right:
    git checkout main
    git merge staging
    git push origin main             # artbymccarthy.com rebuilds

`main` is the shop. Nothing lands there that has not been seen on staging first.

## Creating the service (once, in Render's dashboard)

**New → Web Service**, same repo (`digitalwaterhouse/artbymccarthy-`), then:

| Setting | Value |
|---|---|
| Name | `artbymccarthy-staging` |
| Branch | **`staging`** — this is the one setting that matters |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT` |
| Health check path | `/health` |
| Instance type | **Free** — see the note below |
| Disk | **none** |

### Environment variables

    ENV_NAME=staging       # the hazard band, and the refusal to send mail
    NOINDEX=1              # it must never be indexed as a second copy of her shop
    APP_PREFIX=/
    ADMIN_PASS=            # a DIFFERENT password from the live one
    SECRET_KEY=            # generate; never the live one

And, deliberately, **nothing else**. No `STRIPE_*`: with no keys the Purchase
button falls back to the enquiry form, so staging cannot take a card. No
`SMTP_*`: and even if somebody pastes them in one afternoon, `mailer.send()`
refuses outright while `ENV_NAME` is set. No `ANTHROPIC_API_KEY`.

## Free instance, no disk — on purpose

A disk needs a paid instance (~$7/mo), and on a copy it buys the wrong thing.
Without one the filesystem is ephemeral, so **every deploy starts from an empty
database**: a clean, reproducible copy every time, which is what you want to
test a migration against. It also spins down when idle, so the first request
after a quiet spell takes about fifty seconds. That is the trade.

Fill it with something to look at:

    python3 seed_samples.py            # generated placeholder art, not her work
    python3 seed_samples.py --remove

If you would rather it kept its data between deploys, give it a Starter
instance and a 1 GB disk at `/opt/render/project/src/data`, exactly as
production has. That is the only change.

## Do not copy production's data into it

Her orders carry real names and addresses, and her contacts are real people. A
second system holding a copy of both, behind a password somebody picked in a
hurry, is a liability and not a convenience. Seed it instead.

## Telling them apart

* The band across the top of every page, in the shop and the studio.
* `/health` reports `"env": "staging"` or `"env": "production"`.
