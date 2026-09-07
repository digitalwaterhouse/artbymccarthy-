"""Fill the gallery with real paintings, for layout testing only.

These are PUBLIC DOMAIN works from the Art Institute of Chicago's open access
collection, fetched over its public API. They are here because generated
placeholder art cannot show how the wall handles a real photograph of a real
painting -- the texture, the odd proportions, the dark and light grounds.

    python3 seed_museum.py            # create them
    python3 seed_museum.py --remove   # delete them again

NOTHING HERE IS LISA'S WORK. Each one carries its true artist, date and source
in the story field, which shows on the work's own page, so a placeholder can
never quietly pass for hers. The slug prefix is "sample-pd-", which also falls
under seed_samples.py --remove.
"""
import io
import json
import sys
import urllib.parse
import urllib.request

from PIL import Image

import gallery

PREFIX = "sample-pd-"
API = "https://api.artic.edu/api/v1/artworks/"
IIIF = "https://www.artic.edu/iiif/2/%s/full/1686,/0/default.jpg"
UA = {"User-Agent": "artbymccarthy-placeholder-seed/1.0 (layout testing)"}

# Chosen for a spread of shape, ground and palette rather than for period.
PICKS = [
    (8991,   "available", 145000),   # Kandinsky, Improvisation No. 30
    (34461,  "available", 128000),   # Gauguin, Polynesian Woman with Children
    (109819, "available",  86000),   # Mondrian, Lozenge Composition
    (16568,  "sold",      210000),   # Monet, Water Lilies
    (80607,  "available",  74000),   # Van Gogh, Self-Portrait
    (16617,  "available",  59000),   # Renoir, Chrysanthemums
]


def fetch(art_id):
    fields = "id,title,artist_title,date_display,image_id,is_public_domain,medium_display"
    url = API + str(art_id) + "?" + urllib.parse.urlencode({"fields": fields})
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def remove():
    n = 0
    for w in gallery.list_works():
        if w["slug"].startswith(PREFIX):
            gallery.delete_work(w["id"])
            n += 1
    print("removed %d museum placeholders" % n)


def create():
    made = 0
    for i, (art_id, status, price) in enumerate(PICKS):
        meta = fetch(art_id)
        if not meta.get("is_public_domain"):
            # Never publish a rights-restricted image, even as a placeholder.
            print("  SKIP %s -- not public domain" % art_id)
            continue
        slug = PREFIX + gallery.slugify(meta["title"])[:60]
        if gallery.get_work(slug=slug):
            continue

        req = urllib.request.Request(IIIF % meta["image_id"], headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()

        # Real inches are not in this response, so the stated size is derived
        # from the picture's own proportions -- wrong in fact, right in shape,
        # which is all the wall is being tested on.
        im = Image.open(io.BytesIO(data))
        long_in = 30.0
        if im.width >= im.height:
            w_in, h_in = long_in, round(long_in * im.height / im.width)
        else:
            h_in, w_in = long_in, round(long_in * im.width / im.height)

        wid = gallery.save_work({
            "title": meta["title"], "year": None,
            "medium": meta.get("medium_display") or "",
            "h_in": h_in, "w_in": w_in, "d_in": None,
            "price_cents": price, "status": "available",
            "framed": 0, "ready_to_hang": 1, "signed_where": "",
            "story": ("PLACEHOLDER — this is not work by the artist. %s, %s. "
                      "Public domain, Art Institute of Chicago."
                      % (meta.get("artist_title") or "Unknown",
                         meta.get("date_display") or "n.d.")),
            "ship_band": "large", "sort": 20 + i, "slug": slug,
        })
        gallery.add_image(wid, data, alt=meta["title"])
        if status != "available":
            gallery.set_status(wid, status)
        made += 1
        print("  %-46s %-22s %s" % (meta["title"][:46],
                                    (meta.get("artist_title") or "?")[:22], status))
    print("created %d museum placeholders" % made)


if __name__ == "__main__":
    gallery.init_db()
    remove() if "--remove" in sys.argv else create()
