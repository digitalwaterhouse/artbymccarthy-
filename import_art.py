"""Import Lisa's photographs of her own work, and clear out every placeholder.

The raw phone photographs were dropped straight into data/photos/. That folder
is what the /photo route serves, so anything sitting in it is publicly
fetchable by name and none of it has been through the resizer. This moves the
originals to data/originals/ (kept, never served) and puts each one through
gallery.add_image, which strips EXIF -- the studio's GPS included -- and writes
the three widths in WebP and JPEG.

    python3 import_art.py

TITLES ARE DESCRIPTIVE PLACEHOLDERS chosen from what is in each picture, not
Lisa's own titles. Price, size and year are deliberately left EMPTY rather than
invented: an unpriced work shows an Enquire button, which is the honest state
until she sets them.
"""
import os
import shutil
import sys

import gallery

BASE = os.path.dirname(os.path.abspath(__file__))
ORIGINALS = os.path.join(BASE, "data", "originals")

# filename -> a plain description of what the piece shows. Working labels only.
TITLES = [
    ("IMG_8898.jpg", "Tuk-tuk, Red Ground"),
    ("IMG_8903.jpg", "Tuk-tuk, Circles"),
    ("IMG_8911.jpg", "The Cup"),
    ("IMG_8915.jpg", "Fish and Cocktail"),
    ("IMG_8917.jpg", "Sailboat, Moon"),
    ("IMG_8922.jpg", "Face"),
    ("IMG_8934.jpg", "Sailboat, Patchwork"),
    ("IMG_8995.jpg", "Cicada"),
    ("IMG_9001.jpg", "Red Flower"),
    ("IMG_9004.jpg", "Dragonfly, Blossom"),
    ("IMG_9013.jpg", "Dragonfly, Pink"),
    ("IMG_9017.jpg", "Spiral"),
    ("IMG_9022.jpg", "Small Bloom"),
    ("IMG_9027.jpg", "Dragonfly, Reeds"),
    ("IMG_9035.jpg", "Lightbulb"),
    ("IMG_9038.jpg", "Dragonfly, Pale"),
    ("IMG_9053.jpg", "Two Dragonflies"),
    ("IMG_9059.jpg", "Glasses"),
    ("IMG_9071.jpg", "Heart"),
    ("IMG_9077.jpg", "Bee"),
    ("IMG_9083.jpg", "Fish, Yellow Ground"),
    ("IMG_9087.jpg", "Dog on a Branch"),
    ("IMG_9105.jpg", "Dragonfly, Red"),
    ("IMG_9106.jpg", "Fish, Patterned"),
    ("IMG_9111.jpg", "Sailboat, Turquoise"),
    ("IMG_0024.jpeg", "Leaping Figure"),
]

PLACEHOLDER_PREFIXES = ("sample-",)
PLACEHOLDER_SLUGS = ("low-tide-case-inlet-2026",)


def clear_placeholders():
    n = 0
    for w in gallery.list_works():
        if w["slug"].startswith(PLACEHOLDER_PREFIXES) or w["slug"] in PLACEHOLDER_SLUGS:
            gallery.delete_work(w["id"])   # also unlinks its resized files
            n += 1
    print("removed %d placeholder works" % n)


def import_photos():
    os.makedirs(ORIGINALS, exist_ok=True)
    made = 0
    for i, (fname, title) in enumerate(TITLES):
        src = os.path.join(gallery.PHOTO_DIR, fname)
        keep = os.path.join(ORIGINALS, fname)
        path = src if os.path.exists(src) else keep
        if not os.path.exists(path):
            print("  MISSING %s" % fname)
            continue
        with open(path, "rb") as fh:
            data = fh.read()

        wid = gallery.save_work({
            "title": title,
            "year": None, "medium": "Mixed media", "h_in": None, "w_in": None,
            "d_in": None, "price_cents": None, "status": "available",
            "framed": 1, "ready_to_hang": 1, "signed_where": "", "story": "",
            "ship_band": "small", "sort": i, "slug": None,
        })
        gallery.add_image(wid, data, alt=title)

        # The original leaves the served folder, but is kept: it is the only
        # full-resolution copy on this box.
        if path == src:
            shutil.move(src, keep)
        made += 1
        print("  %-18s %s" % (fname, title))
    print("imported %d works; originals in data/originals/" % made)


if __name__ == "__main__":
    gallery.init_db()
    if "--placeholders-only" in sys.argv:
        clear_placeholders()
    else:
        clear_placeholders()
        import_photos()
