"""Open calls for entry, read from EntryThingy's published listing pages.

WHY THIS AND NOT A SEARCH. Nothing in the calls-for-entry world publishes a
feed -- no RSS, no JSON API, and the regional arts calendars answer 403 or 404.
Paying a model to read the open web was tried and removed: $1.14 a press, and
the answers still had to be checked by hand.

But EntryThingy's public list -- which aggregates CaFE, ZAPPlication, ArtCall,
ShowSubmit and the independent galleries -- embeds schema.org JSON-LD on the
page: one `Event` per call, carrying the name, the DEADLINE, the town as a
structured address, the organiser and the fee. That is published machine-
readable data, put there to be read, and their robots.txt could not be plainer
about it:

    # EntryThingy benefits from AI discovery - we're a platform for
    # opportunities, not content that can be "stolen". Allow all crawlers

So this reads the structured data, not the page furniture: no HTML scraping, no
key, no bill, and a layout change does not break it. One request per state, a
few times a month, with a real User-Agent that says who is asking.
"""
import re
import json
import urllib.request
import urllib.error

SOURCE = "entrythingy"
LIST_URL = "https://app.entrythingy.com/calls_list/?state=%s"
# Says who is asking and how to reach us. A blank or forged agent on somebody
# else's server is bad manners and the first thing a rate-limiter blocks.
UA = ("Mozilla/5.0 (compatible; artbymccarthy/1.0; +https://artbymccarthy.com) "
      "one artist's studio, a few requests a month")
TIMEOUT = 25

# She is in Westchester, nine miles from the Connecticut line, so the state she
# lives in is not the only one within an easy drive.
NEIGHBOURS = {
    "NY": ["CT", "NJ"], "CT": ["NY", "MA", "RI"], "NJ": ["NY", "PA"],
    "MA": ["CT", "RI", "NH"], "RI": ["MA", "CT"], "PA": ["NJ", "NY", "MD"],
}


def fetch(state):
    """The listing page for one state, or None. Never raises: a listing site
    that is slow or down is an empty panel, not a 500 in the studio."""
    try:
        req = urllib.request.Request(LIST_URL % state, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _events(html):
    """Every schema.org Event in the page's JSON-LD, at any nesting."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("@type") == "Event":
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for block in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.S):
        try:
            walk(json.loads(block))
        except ValueError:
            continue          # one malformed block must not lose the others
    return out


def _fee(ev):
    """What it costs to enter, as published. Left as text on purpose: "free"
    and "$35.00" are both answers, and a fee parsed wrongly into a money field
    is worse than one she reads herself."""
    if ev.get("isAccessibleForFree") is True:
        return "free"
    price = (ev.get("offers") or {}).get("price")
    if price in (None, ""):
        return None
    try:
        return "$%g" % float(price)
    except (TypeError, ValueError):
        return str(price)[:40]


def _nonempty(v):
    """Some listings carry the literal string "None" where a town should be --
    a null that has been through a template on the way out. Treated as missing,
    or the panel prints "None, NY" as if it were a place."""
    v = (v or "").strip()
    return None if v.lower() in ("", "none", "null", "n/a", "-") else v


def _place(ev):
    loc = ev.get("location") or {}
    addr = loc.get("address") or {}
    town_, region = _nonempty(addr.get("addressLocality")), _nonempty(addr.get("addressRegion"))
    # Some listings put the state in the locality too ("New Haven, CT"), which
    # joined naively reads "New Haven, CT, CT".
    if town_ and region and town_.upper().endswith(", " + region.upper()):
        region = None
    town = ", ".join(b for b in (town_, region) if b)
    # Some listings carry only a display name ("Online", "New York, NY").
    return town or _nonempty(loc.get("name"))


def _clean_text(v, limit=400):
    if not v:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v[:limit] or None


def to_call(ev):
    """One JSON-LD Event, mapped to the shape the holding pen stores."""
    name = _clean_text(ev.get("name"), 300)
    if not name:
        return None
    # endDate is the deadline on these listings -- verified against the date
    # printed on the card itself. startDate is when the call opened.
    deadline = (ev.get("endDate") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", deadline):
        deadline = None
    return {"title": name,
            "org": _clean_text((ev.get("organizer") or {}).get("name"), 200),
            "location": _place(ev),
            "deadline": deadline,
            "fee": _fee(ev),
            "kind": "show",
            "url": _clean_text(ev.get("url"), 500),
            "why": _clean_text(ev.get("description"), 400),
            "source": SOURCE}


def states_for(cfg):
    """Her state, then the nearest neighbour. Two requests, not fifty."""
    town = (cfg.get("studio_location") or "").strip()
    bits = [b.strip() for b in town.split(",") if b.strip()]
    state = (bits[1] if len(bits) > 1 else "").upper()[:2]
    if not re.match(r"^[A-Z]{2}$", state or ""):
        return []
    return [state] + NEIGHBOURS.get(state, [])[:1]


def open_calls(cfg, today=None):
    """(calls, note). Open calls only, newest deadline last, already deduped."""
    states = states_for(cfg)
    if not states:
        return [], ("Put your town in Your site first -- these listings are "
                    "fetched by state.")
    seen, calls, failed = set(), [], []
    for st in states:
        html = fetch(st)
        if html is None:
            failed.append(st)
            continue
        for ev in _events(html):
            c = to_call(ev)
            if not c:
                continue
            key = (c["url"] or "").rstrip("/").lower() or c["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            calls.append(c)
    # The list is published as open calls, but a stale page is still possible
    # and a closed call is worthless to her. Checked here rather than trusted.
    if today:
        calls = [c for c in calls if not c["deadline"] or c["deadline"] >= today]
    calls.sort(key=lambda c: (c["deadline"] or "9999-99-99", c["title"]))
    note = "%d open call%s in %s." % (len(calls), "" if len(calls) == 1 else "s",
                                      " and ".join(states))
    if failed:
        note += " %s could not be reached." % " and ".join(failed)
    return calls, note
