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
# Public by design -- it is printed into the checkout page for Stripe.js. With
# it set, checkout happens ON the site (Stripe's card fields inside her page);
# without it, the buyer is sent to Stripe's hosted page as before.
PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
# Sales tax through Stripe Tax. Stripe adds it only where the account has a
# tax registration (for her, New York), so an out-of-state buyer sees none.
# Turn it on only AFTER the registration is entered in the Stripe dashboard.
TAX_ENABLED = os.environ.get("STRIPE_TAX", "0") == "1"
TAX_CODE_WORK = "txcd_99999999"   # General - Tangible Goods
TAX_CODE_SHIPPING = "txcd_92010001"

if stripe and SECRET:
    stripe.api_key = SECRET


def enabled():
    return bool(stripe and SECRET)


def live_mode():
    return SECRET.startswith("sk_live_")


def on_site():
    """Pay on the site rather than on Stripe's page. Needs the publishable key,
    and one from the SAME mode as the secret -- a pk_test_ beside an sk_live_
    loads a checkout that can never be paid."""
    return (enabled() and PUBLISHABLE.startswith(("pk_live_", "pk_test_"))
            and PUBLISHABLE.startswith("pk_live_") == live_mode())


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
            **({"tax_behavior": "exclusive", "tax_code": TAX_CODE_SHIPPING}
               if TAX_ENABLED else {}),
        }
    }]


def create_session(works, success_url=None, cancel_url=None, return_url=None):
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
                # Exclusive: tax goes ON TOP of her price, never out of it.
                **({"tax_behavior": "exclusive"} if TAX_ENABLED else {}),
                "product_data": {
                    **({"tax_code": TAX_CODE_WORK} if TAX_ENABLED else {}),
                    "name": w["title"],
                    "description": ", ".join(
                        [x for x in (w.get("medium"), w.get("dims"),
                                     str(w["year"]) if w.get("year") else None) if x]) or None,
                },
            },
        } for w in works],
        shipping_address_collection={"allowed_countries": ["US", "CA"]},
        shipping_options=_shipping_options(works, cfg),
        # Stripe's floor is 30 minutes; the pieces are held a few minutes
        # longer than this (gallery.CHECKOUT_HOLD_MINUTES) so the session
        # always expires before the hold does.
        expires_at=int(time.time()) + gallery.CHECKOUT_SESSION_MINUTES * 60,
        metadata={"items": items},
        payment_intent_data={"metadata": {"items": items}},
        # Codes are made in her Stripe dashboard (a coupon, then a promotion
        # code on it); Stripe checks them, so nothing is stored here.
        allow_promotion_codes=True,
    )
    if return_url:
        # On-site: Stripe.js draws the fields in her page, and return_url is
        # where the buyer lands after paying (or after a bank redirect).
        kwargs.update(ui_mode="elements", return_url=return_url)
    else:
        kwargs.update(success_url=success_url, cancel_url=cancel_url)
    if TAX_ENABLED:
        kwargs["automatic_tax"] = {"enabled": True}
    return stripe.checkout.Session.create(**kwargs)


def retrieve(session_id):
    return stripe.checkout.Session.retrieve(session_id)


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


def split_cents(cents, weights):
    """Share one checkout-wide figure across its paintings in proportion to
    `weights`. The leftover cents go to the largest remainders, so the shares
    always add back up to exactly what Stripe charged."""
    cents = int(cents or 0)
    whole = sum(weights)
    if cents <= 0 or whole <= 0:
        return [0] * len(weights)
    exact = [cents * w / whole for w in weights]
    out = [int(x) for x in exact]
    order = sorted(range(len(weights)), key=lambda i: exact[i] - out[i], reverse=True)
    for i in order[:cents - sum(out)]:
        out[i] += 1
    return out


def split_checkout(items, discount_cents=0, tax_cents=0):
    """Per painting, (discount, tax) out of the checkout's totals. A promo
    code comes off the works, not the shipping, so it is shared by price;
    tax is then shared by what each piece cost after it (price - discount +
    its shipping). One address means one rate, so both are exact but for
    rounding. Returns one (discount, tax) pair per item, in order."""
    disc = split_cents(discount_cents, [p for _, p, _ in items])
    tax = split_cents(tax_cents, [p - d + s for (_, p, s), d in zip(items, disc)])
    return list(zip(disc, tax))


def promo_code_text(session):
    """The code the buyer typed (SPRING10), for the order and the invoice.
    The session only carries the promotion code's id, so it is looked up;
    a failed lookup costs the label, never the order."""
    for d in session.get("discounts") or []:
        pid = d.get("promotion_code")
        if isinstance(pid, dict):
            return pid.get("code")
        if pid:
            try:
                return stripe.PromotionCode.retrieve(pid).code
            except Exception:
                return None
    return None


def parse_webhook(payload, sig_header):
    """Returns the event, or raises. Signature check is the security boundary:
    without it anyone who finds the URL can mark paintings sold."""
    if not WEBHOOK_SECRET:
        raise ValueError("no webhook secret configured")
    return stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
