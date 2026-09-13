"""Find calls for entry on the web, with Claude's server-side web search.

WHY THIS EXISTS AND WHAT IT IS NOT. The calls-for-entry world publishes no
feed: CaFE, ZAPPlication and EntryThingy have no RSS, no JSON and no API, and
the local arts calendars that might have had one mostly answer 403 or 404. So
the Opportunities list could record calls but never find them, and finding them
was the tedious half.

This asks Claude to search the open web from the studio's own location and hand
back a structured list. Three rules follow from the fact that a model can be
wrong about a deadline:

  1. NOTHING IS EVER ADDED AUTOMATICALLY. Every result is a suggestion with its
     source link attached, and it enters her Opportunities list only when she
     presses Add.
  2. EVERY RESULT CARRIES ITS SOURCE. She checks the deadline against the
     gallery's own page before she ever writes an application, and the API
     terms require citing sources shown to a reader.
  3. IT COSTS MONEY, so it is a button and the answers are kept. Searches are
     $10 per 1,000 plus tokens; a press is a few cents. Nothing here runs on a
     page view, on a timer, or on behalf of a visitor -- there is no scheduler
     on this host anyway.
"""
import os
import re
import json

try:
    import anthropic
except ImportError:          # the studio must not 500 because a lib is missing
    anthropic = None

KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-opus-5"
# Latest web search variant: it filters results in code before they reach the
# context window, which is the difference between a handful of tokens and a
# page of boilerplate per hit.
TOOL = "web_search_20260318"
MAX_SEARCHES = 8


# What a press costs, so the screen can say so instead of guessing. Rates as
# published for claude-opus-5 and the web search tool; kept here as one table
# because a price that changes should be a one-line edit. Every receipt also
# stores the dollar figure computed at the time, so history stays honest.
PRICE = {
    "in":          5.00 / 1_000_000,     # $5.00 per 1M input tokens
    "out":        25.00 / 1_000_000,     # $25.00 per 1M output tokens
    "cache_read":  0.50 / 1_000_000,     # cache reads are a tenth of input
    "cache_write": 6.25 / 1_000_000,     # writes are a quarter more than input
    "search":     10.00 / 1_000,         # $10 per 1,000 web searches
}


def enabled():
    return bool(anthropic and KEY)


def _usage_of(msg, used_searches):
    """Pull the counters off the reply and price them. Every field is read
    defensively: a usage block that gains or loses a key must not take the
    search down with it."""
    u = getattr(msg, "usage", None)

    def n(name):
        return int(getattr(u, name, 0) or 0) if u else 0

    d = {"model": MODEL,
         "in_tokens": n("input_tokens"), "out_tokens": n("output_tokens"),
         "cache_read": n("cache_read_input_tokens"),
         "cache_write": n("cache_creation_input_tokens"),
         "web_searches": used_searches}
    dollars = (d["in_tokens"] * PRICE["in"] + d["out_tokens"] * PRICE["out"]
               + d["cache_read"] * PRICE["cache_read"]
               + d["cache_write"] * PRICE["cache_write"]
               + d["web_searches"] * PRICE["search"])
    d["cost_micros"] = int(round(dollars * 1_000_000))
    return d


def blank_usage():
    return {"model": MODEL, "in_tokens": 0, "out_tokens": 0, "cache_read": 0,
            "cache_write": 0, "web_searches": 0, "cost_micros": 0}


SYSTEM = """You find open calls for entry for a working artist and report them as data.

The artist makes mixed-media shadow boxes, 10.5 x 10.5 x 2.5 inches, black and
white, and shows them as original one-off pieces. Small juried shows, local and
regional galleries, arts councils, libraries, and open calls suit her. Large
public-art commissions, mural programmes, film, performance and craft-fair booth
rentals do not.

RULES YOU MUST FOLLOW:
- Report only calls you actually found on a page you searched. Never invent a
  call, a deadline, a fee or a URL. A call you half-remember is not a result.
- The deadline must be one you READ on the page. If the page does not state one,
  set deadline to null rather than guessing.
- Prefer calls that are still open. Say so in `closed` if a deadline has passed.
- `url` must be the page you actually read, so she can check it herself.
- Fewer real results beat a long list. Six good ones is a good answer; so is one.

Answer with a single JSON object and nothing else, in a ```json fenced block:

{"calls": [{
  "title":    "the name of the call",
  "org":      "the gallery, council or foundation running it",
  "location": "Town, ST -- or \\"online\\" if it is not a physical place",
  "deadline": "YYYY-MM-DD or null",
  "fee":      "entry fee as written, e.g. \\"$35\\", or null",
  "kind":     "show | grant | residency | fair | other",
  "url":      "the page you read",
  "why":      "one short sentence on why it suits her work"
}]}"""


def _prompt(place, miles, extra):
    ask = ("Find open calls for entry for an artist based in %s, within about "
           "%d miles. Search the open web: gallery and arts-council sites, "
           "library and museum open calls, regional juried shows, and the "
           "public listing pages of the call-for-entry platforms."
           % (place, miles))
    if extra:
        ask += " She also asks: " + extra.strip()
    ask += ("\n\nCheck each call's own page for the deadline before you report "
            "it. Then give me the JSON object.")
    return ask


def _location_block(cfg):
    """user_location localises the SEARCH itself, which is most of the value:
    the same query run from nowhere in particular returns national listings."""
    town = (cfg.get("studio_location") or "").strip()
    loc = {"type": "approximate", "country": "US",
           "timezone": "America/New_York"}
    # "Bedford, NY" -> city Bedford, region NY. Anything less structured is
    # handed over as the city and left alone; the field is approximate anyway.
    bits = [b.strip() for b in town.split(",") if b.strip()]
    if bits:
        loc["city"] = bits[0]
    if len(bits) > 1:
        loc["region"] = bits[1]
    return loc


def _text_of(msg):
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _searches_used(msg):
    u = getattr(msg, "usage", None)
    stu = getattr(u, "server_tool_use", None) if u else None
    return getattr(stu, "web_search_requests", 0) if stu else 0


def parse_calls(text):
    """Pull the JSON object out of the reply. Tolerant on purpose: a fenced
    block is what was asked for, but a bare object is not a reason to throw
    away a good answer."""
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = m.group(1) if m else None
    if blob is None:
        i, j = text.find("{"), text.rfind("}")
        blob = text[i:j + 1] if i != -1 and j > i else None
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except ValueError:
        return []
    calls = data.get("calls") if isinstance(data, dict) else data
    return [c for c in (calls or []) if isinstance(c, dict) and c.get("title")]


def search(cfg, miles=75, extra=""):
    """Returns (calls, note). Never raises: a search that fails is a message
    on the screen, not a 500 in the studio."""
    if not enabled():
        return [], "Web search is not switched on for this site.", blank_usage()
    place = (cfg.get("studio_location") or "").strip() or "the north-east United States"
    client = anthropic.Anthropic(api_key=KEY)
    messages = [{"role": "user", "content": _prompt(place, miles, extra)}]
    tool = {"type": TOOL, "name": "web_search", "max_uses": MAX_SEARCHES,
            "user_location": _location_block(cfg)}
    used, spent = 0, blank_usage()

    def bank(m):
        """Money is spent per REQUEST, so every turn is banked as it returns --
        including the turns of a paused search that never reaches an answer.
        A cost that is only counted on success is not a cost meter."""
        u = _usage_of(m, 0)
        for k in ("in_tokens", "out_tokens", "cache_read", "cache_write", "cost_micros"):
            spent[k] += u[k]

    try:
        # pause_turn: the API can stop a long search turn part-way and ask to
        # be continued with the assistant message handed straight back.
        for _ in range(4):
            msg = client.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM,
                messages=messages, tools=[tool])
            bank(msg)
            used += _searches_used(msg)
            if msg.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": msg.content}]
        else:
            spent["web_searches"] = used
            spent["cost_micros"] += int(round(used * PRICE["search"] * 1_000_000))
            return [], "The search kept going and was stopped. Try again.", spent
    except Exception as e:                       # network, auth, rate limit
        return [], "The search could not run: %s" % _short(e), spent

    spent["web_searches"] = used
    spent["cost_micros"] += int(round(used * PRICE["search"] * 1_000_000))

    if getattr(msg, "stop_reason", "") == "refusal":
        return [], "The search was declined.", spent
    # A server tool error arrives as a 200 with an error object where the list
    # of results should be -- it is not raised, so it has to be looked for.
    for b in msg.content:
        if getattr(b, "type", "") == "web_search_tool_result":
            c = getattr(b, "content", None)
            if isinstance(c, dict) or getattr(c, "error_code", None):
                code = c.get("error_code") if isinstance(c, dict) else c.error_code
                return [], "The web search stopped: %s." % str(code).replace("_", " "), spent
    calls = parse_calls(_text_of(msg))
    if not calls:
        return [], "Nothing came back that looked like a call for entry.", spent
    return calls, "%d found, %d search%s used." % (
        len(calls), used, "" if used == 1 else "es"), spent


def _short(e):
    s = str(e) or e.__class__.__name__
    return s[:160]
