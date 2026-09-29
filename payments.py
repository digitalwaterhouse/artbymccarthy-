"""Stripe Checkout. Card details never touch this server -- the buyer is sent
to a page Stripe hosts, and we hear back on a signed webhook. That is the whole
reason this app can live on a box that also serves other things.

With no keys in .env the site still runs: the Buy button becomes an enquiry
form instead, so the gallery is usable before any money is wired up.
"""
import os
import time

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


def ship_cents(work, cfg):
    """Bands, not per-item rates: a flat rate that works for a 12x16 loses
    money on a 36x48, and anything past 'large' has to be quoted by hand
    (None -- such a piece never reaches the cart, it goes to an enquiry)."""
    band = work.get("ship_band") or "medium"
    if band == "quote":
        return None
    return int(cfg.get(f"ship_{band}_cents") or 0)


def _shipping_options(works, cfg):
    """Each original ships in its own box, so a cart's shipping is the sum of
    its pieces' bands -- one line at checkout, named for what it covers."""
    cents = sum(ship_cents(w, cfg) or 0 for w in works)
    if len(works) == 1:
        label = {"small": "Shipping (small work)",
                 "medium": "Shipping (medium work)",
                 "large": "Shipping (large work, crated)",
                 "rolled": "Shipping (rolled in a tube)"}.get(works[0].get("ship_band") or "medium",
                                                           "Shipping")
    else:
        label = f"Shipping ({len(works)} works, each packed separately)"
    return [{
        "shipping_rate_data": {
            "type": "fixed_amount",
            "fixed_amount": {"amount": cents, "currency": cfg.get("currency", "usd")},
            "display_name": label,
        }
    }]


def create_session(works, success_url, cancel_url):
    """One hosted checkout for everything in a cart.

    metadata["items"] records, per piece, what was CHARGED for it --
    "id:price:shipping" joined by commas -- so the order rows the webhook
    writes say what this buyer paid even if the price is edited afterwards.
    Stripe allows 500 characters per value: room for 25+ pieces."""
    cfg = gallery.settings()
    currency = cfg.get("currency", "usd")
    items = ",".join(f"{w['id']}:{int(w['price_cents'])}:{ship_cents(w, cfg) or 0}"
                     for w in works)
    kwargs = dict(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": int(w["price_cents"]),
                "product_data": {
                    "name": w["title"],
                    "description": ", ".join(
                        [x for x in (w.get("medium"), w.get("dims"),
                                     str(w["year"]) if w.get("year") else None) if x]) or None,
                },
            },
        } for w in works],
        success_url=success_url,
        cancel_url=cancel_url,
        shipping_address_collection={"allowed_countries": ["US", "CA"]},
        shipping_options=_shipping_options(works, cfg),
        # Stripe's floor is 30 minutes; the pieces are held a few minutes
        # longer than this (gallery.CHECKOUT_HOLD_MINUTES) so the session
        # always expires before the hold does.
        expires_at=int(time.time()) + gallery.CHECKOUT_SESSION_MINUTES * 60,
        metadata={"items": items},
        payment_intent_data={"metadata": {"items": items}},
    )
    if TAX_ENABLED:
        kwargs["automatic_tax"] = {"enabled": True}
    return stripe.checkout.Session.create(**kwargs)


def expire_if_open(session_id):
    """Cancel a checkout the buyer walked away from, so it cannot be paid
    later in a forgotten tab. Returns the session's status as Stripe last saw
    it ("open" is never returned: an open one is expired here)."""
    s = stripe.checkout.Session.retrieve(session_id)
    # Attribute access: StripeObject stopped being a dict in stripe-python 12+.
    status = getattr(s, "status", None)
    if status == "open":
        stripe.checkout.Session.expire(session_id)
        return "expired"
    return status


def parse_items(metadata):
    """[(work_id, price_cents, ship_cents), ...] from a session's metadata."""
    out = []
    for part in (metadata.get("items") or "").split(","):
        bits = part.split(":")
        if len(bits) == 3 and all(b.isdigit() for b in bits):
            out.append(tuple(int(b) for b in bits))
    return out


def parse_webhook(payload, sig_header):
    """Returns the event, or raises. Signature check is the security boundary:
    without it anyone who finds the URL can mark paintings sold."""
    if not WEBHOOK_SECRET:
        raise ValueError("no webhook secret configured")
    return stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
