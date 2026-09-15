"""Read the visitor counts back out of Umami, for the studio's Visitors page.

WHY A DASHBOARD HERE AT ALL when Umami has a perfectly good one of its own: the
numbers are only useful if they are seen, and a second site with a second login
is a place you stop going. This puts the answer where she already is.

WHY NOT AN IFRAME of Umami's share URL, which would have been ten minutes: it
is served with X-Frame-Options SAMEORIGIN, so it cannot be embedded anywhere
but Umami. Reading the API and drawing our own is the only route that works,
and it has the side benefit that the page looks like the rest of the studio
rather than like somebody else's product bolted on.

EVERY FAILURE IS SHOWN, NEVER SWALLOWED. A dashboard that quietly renders zero
when the key is wrong is worse than one that says it could not ask -- zero
visitors and "I could not reach Umami" are the same picture otherwise, and the
first is a thing you act on. So the functions here return (data, error) and the
template prints the error.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.umami.is/v1"

# Umami allows 50 calls per 15 seconds. Nothing here is near that, but the page
# makes three calls and a reload should not make three more: one person opens
# this a few times a day and five-minute-old numbers are no less true.
_TTL = 300
_CACHE = {}


def _get(path, api_key, params=None):
    """(payload, error). Never raises: the page must still render."""
    if not api_key:
        return None, "no-key"
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    hit = _CACHE.get(url)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1], None

    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 is the one worth naming: it is what a wrong key looks like, and
        # also what a key on a plan without API access looks like.
        if e.code in (401, 403):
            return None, "Umami refused the API key (HTTP %d). Check the key, and "\
                         "that your plan includes API access." % e.code
        return None, "Umami answered HTTP %d." % e.code
    except urllib.error.URLError as e:
        return None, "Could not reach Umami: %s" % (e.reason,)
    except (ValueError, TimeoutError) as e:
        return None, "Umami sent something unreadable: %s" % (e,)

    _CACHE[url] = (time.time(), payload)
    return payload, None


def _window(days):
    """Umami wants milliseconds, and it wants them as whole days back from now."""
    now = int(time.time() * 1000)
    return {"startAt": now - days * 86400000, "endAt": now}


def overview(website_id, api_key, days=30):
    """The headline numbers. Umami returns {value, prev} per metric so the
       comparison with the previous period comes free -- and a number with
       nothing to compare it against is the reason most dashboards go unread."""
    data, err = _get("/websites/%s/stats" % website_id, api_key, _window(days))
    if err:
        return None, err

    def pair(name):
        d = (data or {}).get(name) or {}
        return {"value": d.get("value") or 0, "prev": d.get("prev") or 0}

    out = {k: pair(k) for k in ("visitors", "visits", "pageviews", "bounces")}
    tt = pair("totaltime")
    # Average seconds on the site per visit, which is the form anyone reads it
    # in. Guarding the divide matters on a new site: visits is 0 on day one.
    visits = out["visits"]["value"]
    out["avg_seconds"] = int(tt["value"] / visits) if visits else 0
    out["bounce_pct"] = int(round(100 * out["bounces"]["value"] / visits)) if visits else 0
    return out, None


def top(website_id, api_key, kind, days=30, limit=8):
    """A leaderboard: kind is 'url', 'referrer', 'country' or 'device'."""
    params = _window(days)
    params["type"] = kind
    data, err = _get("/websites/%s/metrics" % website_id, api_key, params)
    if err:
        return [], err
    rows = data if isinstance(data, list) else []
    rows = [{"name": r.get("x") or "", "count": r.get("y") or 0} for r in rows]
    rows.sort(key=lambda r: -r["count"])
    return rows[:limit], None
