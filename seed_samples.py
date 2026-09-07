"""Fill the gallery with sample works so the layout can be judged.

Every piece this creates is GENERATED PLACEHOLDER ART, not Lisa's work. The
images are drawn here in code, in the site's own palette and in the rough shape
of a torn-paper collage, only so that the wall, the viewer and the archive have
something to show before the real photographs exist.

    python3 seed_samples.py            # create them
    python3 seed_samples.py --remove   # delete every one of them again

Removal is by slug prefix, so nothing the artist adds later can be caught by it.
"""
import io
import random
import sys

from PIL import Image, ImageDraw, ImageFilter

import gallery

PREFIX = "sample-"

PALETTE = ["#09BBD5", "#1ECAE1", "#BED566", "#F8F3C6", "#7C6779",
           "#683346", "#124053", "#3B876C", "#C86FA8", "#E8A33D"]
DARK = "#151313"

# Shapes vary on purpose: the wall is built in columns precisely so paintings
# do not have to share an aspect ratio, and one square test proves nothing.
WORKS = [
    # title, year, medium, h, w, price, status, shape
    ("Serekunda, Late Market", 2026, "Paper, acrylic and ink on board",
     20, 16, 68000, "available", (1000, 1250)),
    ("Galle Road, Two Dogs", 2025, "Collage and acrylic on panel",
     16, 20, 54000, "available", (1250, 1000)),
    ("Lake Light, Vevey", 2026, "Paper, acrylic and wire on board",
     24, 24, 96000, "available", (1150, 1150)),
    ("Ninth Avenue, Sunday", 2025, "Mixed media on board",
     30, 22, 140000, "available", (1000, 1360)),
    ("Kotu, Green Season", 2024, "Collage, acrylic and ink on paper",
     14, 11, 38000, "sold", (960, 1220)),
    ("Fort Ramparts, Evening", 2025, "Paper and acrylic on panel",
     18, 18, 62000, "sold", (1120, 1120)),
    ("Fourth Floor, Brooklyn", 2026, "Mixed media on board",
     22, 28, 105000, "reserved", (1360, 1070)),
    ("The Long Way Round", 2026, "Paper, acrylic, ink and wire on board",
     12, 12, 32000, "available", (1080, 1080)),
]


def torn(d, box, fill, rng, bite=0.035):
    """A rectangle with a chewed edge, the way cut paper actually sits."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    step = max(12, int(min(w, h) * 0.09))
    pts = []
    for x in range(x0, x1, step):
        pts.append((x, y0 + rng.uniform(-bite, bite) * h))
    for y in range(y0, y1, step):
        pts.append((x1 + rng.uniform(-bite, bite) * w, y))
    for x in range(x1, x0, -step):
        pts.append((x, y1 + rng.uniform(-bite, bite) * h))
    for y in range(y1, y0, -step):
        pts.append((x0 + rng.uniform(-bite, bite) * w, y))
    if len(pts) > 2:
        d.polygon(pts, fill=fill)


def draw(size, seed):
    rng = random.Random(seed)
    W, H = size
    im = Image.new("RGB", (W, H), rng.choice(PALETTE))
    d = ImageDraw.Draw(im)

    # Ground: overlapping blocks of colour, torn at the edges.
    for _ in range(rng.randint(4, 6)):
        c = rng.choice(PALETTE)
        x0 = rng.randint(-int(W * .2), int(W * .7))
        y0 = rng.randint(-int(H * .2), int(H * .7))
        torn(d, (x0, y0, x0 + rng.randint(int(W*.35), int(W*.9)),
                 y0 + rng.randint(int(H*.25), int(H*.7))), c, rng)

    # Ruled marks -- the pencil hatching that shows through the layers.
    for _ in range(rng.randint(2, 4)):
        bx = rng.randint(0, int(W * .6)); by = rng.randint(0, int(H * .7))
        bw = rng.randint(int(W*.18), int(W*.4)); gap = rng.randint(10, 22)
        for k in range(0, rng.randint(6, 14)):
            d.line([(bx, by + k*gap), (bx + bw, by + k*gap)], fill=DARK, width=3)

    # The card the face sits on.
    cw, ch = int(W * rng.uniform(.52, .66)), int(H * rng.uniform(.5, .64))
    cx0 = (W - cw) // 2 + rng.randint(-int(W*.05), int(W*.05))
    cy0 = int(H * rng.uniform(.16, .26))
    torn(d, (cx0, cy0, cx0 + cw, cy0 + ch), rng.choice(["#09BBD5", "#1ECAE1", "#F8F3C6"]), rng, .012)

    # A face: the white strip, the glasses, the mouth.
    fw = int(cw * .34)
    fx = cx0 + (cw - fw) // 2
    d.rectangle([fx, cy0 + int(ch*.12), fx + fw, cy0 + ch], fill="#F7F5EF")
    er = int(cw * .21)
    ey = cy0 + int(ch * .28)
    for i, ex in enumerate((cx0 + int(cw*.28), cx0 + int(cw*.72))):
        d.ellipse([ex-er, ey-er, ex+er, ey+er], fill=rng.choice(PALETTE), outline=DARK,
                  width=max(6, int(er*.3)))
    d.line([(cx0 + int(cw*.28) + er, ey), (cx0 + int(cw*.72) - er, ey)], fill=DARK,
           width=max(5, int(er*.22)))
    mw, mh = int(fw * .62), int(ch * .07)
    mx, my = fx + (fw - mw)//2, cy0 + int(ch*.70)
    d.ellipse([mx, my, mx+mw, my+mh*2], fill="#D81E4A", outline=DARK, width=4)

    return im.filter(ImageFilter.SMOOTH)


def remove():
    n = 0
    for w in gallery.list_works():
        if w["slug"].startswith(PREFIX):
            gallery.delete_work(w["id"])
            n += 1
    print("removed %d sample works" % n)


def create():
    made = 0
    for i, (title, year, medium, h, wd, price, status, shape) in enumerate(WORKS):
        slug = PREFIX + gallery.slugify(title, year)
        if gallery.get_work(slug=slug):
            continue
        wid = gallery.save_work({
            "title": title, "year": year, "medium": medium,
            "h_in": h, "w_in": wd, "d_in": None, "price_cents": price,
            "status": status, "framed": 1, "ready_to_hang": 1,
            "signed_where": "verso", "story": "", "sort": i,
            "ship_band": "large" if max(h, wd) >= 28 else "medium",
            "slug": slug,
        })
        buf = io.BytesIO()
        draw(shape, seed=1000 + i).save(buf, "JPEG", quality=92)
        gallery.add_image(wid, buf.getvalue(), alt=title)
        if status == "sold":
            gallery.set_status(wid, "sold")
        made += 1
        print("  %-26s %-10s %s" % (title, status, gallery.money(price)))
    print("created %d sample works" % made)


if __name__ == "__main__":
    gallery.init_db()
    if "--remove" in sys.argv:
        remove()
    else:
        create()
