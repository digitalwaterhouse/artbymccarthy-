"""Stripe Checkout. Card details never touch this server -- the buyer is sent
to a page Stripe hosts, and we hear back on a signed webhook. That is the whole
reason this app can live on a box that also serves other things.

With no keys in .env the site still runs: the Buy button becomes an enquiry
form instead, so the gallery is usable before any money is wired up.
"""
import os

import gallery

try:
    import stripe
except ImportError:          # the site must not 500 because a lib is missing
    stripe = None

SECRET = os.environ.get("STRIPE_SECRET_KEY", "").strip()
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
TAX_ENABLED = os.environ.get("STRIPE_TAX", "0") == "1"

if stripe and SECRET:
    stripe.api_key = SECRET


def enabled():
    return bool(stripe and SECRET)


def live_mode():
    return SECRET.startswith("sk_live_")


def _shipping_options(work, cfg):
    """Bands, not per-item rates: a flat rate that works for a 12x16 loses
    money on a 36x48, and anything past 'large' has to be quoted by hand."""
    band = work.get("ship_band") or "medium"
    if band == "quote":
        return []
    cents = int(cfg.get(f"ship_{band}_cents") or 0)
    label = {"small": "Shipping (small work)",
             "medium": "Shipping (medium work)",
             "large": "Shipping (large work, crated)",
             "rolled": "Shipping (rolled in a tube)"}.get(band, "Shipping")
    return [{
        "shipping_rate_data": {
            "type": "fixed_amount",
            "fixed_amount": {"amount": cents, "currency": cfg.get("currency", "usd")},
            "display_name": label,
        }
    }]


def create_session(work, success_url, cancel_url):
    cfg = gallery.settings()
    currency = cfg.get("currency", "usd")
    kwargs = dict(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": int(work["price_cents"]),
                "product_data": {
                    "name": work["title"],
                    "description": ", ".join(
                        [x for x in (work.get("medium"), work.get("dims"),
                                     str(work["year"]) if work.get("year") else None) if x]) or None,
                },
            },
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        shipping_address_collection={"allowed_countries": ["US", "CA"]},
        shipping_options=_shipping_options(work, cfg),
        metadata={"work_id": str(work["id"]), "slug": work["slug"]},
        payment_intent_data={"metadata": {"work_id": str(work["id"])}},
    )
    if TAX_ENABLED:
        kwargs["automatic_tax"] = {"enabled": True}
    return stripe.checkout.Session.create(**kwargs)


def parse_webhook(payload, sig_header):
    """Returns the event, or raises. Signature check is the security boundary:
    without it anyone who finds the URL can mark paintings sold."""
    if not WEBHOOK_SECRET:
        raise ValueError("no webhook secret configured")
    return stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
