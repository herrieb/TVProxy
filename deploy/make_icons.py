#!/usr/bin/env python3
"""Category / brand icons for channels without a logo of their own (512x512 PNG).

    python3 make_icons.py OUT_DIR [--font-dir /mnt/c/Windows/Fonts]
Served by the media service at /media/icons/<name>.png (see app/logos.py).
"""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ICONS = {  # name: (label, glyph font, glyph, top colour, bottom colour)
    "films":       ("Films",        "SegoeIcons.ttf", "", (229, 57, 53), (126, 23, 23)),
    "kids":        ("Kids",         "seguiemj.ttf",   "\U0001F9F8", (255, 167, 38), (230, 81, 0)),
    "music":       ("Muziek",       "SegoeIcons.ttf", "", (171, 71, 188), (74, 20, 140)),
    "docu":        ("Docu",         "SegoeIcons.ttf", "", (38, 166, 154), (0, 77, 64)),
    "regional":    ("Regionaal",    "SegoeIcons.ttf", "", (102, 187, 106), (27, 94, 32)),
    "sport":       ("Sport",        "seguisym.ttf",   "⚽", (66, 165, 245), (13, 71, 161)),
    "tv":          ("TV",           "SegoeIcons.ttf", "", (120, 144, 156), (38, 50, 56)),
    "section":     ("",             "SegoeIcons.ttf", "", (255, 112, 67), (191, 54, 12)),
    "prime-video": ("Prime Video",  None, None, (0, 168, 225), (0, 82, 147)),
    "clipmyhorse": ("ClipMyHorse",  None, None, (141, 110, 99), (62, 39, 35)),
    "exxen":       ("Exxen",        None, None, (255, 202, 40), (245, 127, 23)),
}
SIZE = 512


def tile(top, bottom):
    img = Image.new("RGBA", (SIZE, SIZE))
    d = ImageDraw.Draw(img)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        d.line([(0, y), (SIZE, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=96, fill=255)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    shine = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shine).ellipse([-SIZE * 0.4, -SIZE * 0.9, SIZE * 1.4, SIZE * 0.45], fill=(255, 255, 255, 38))
    out.alpha_composite(Image.composite(shine, Image.new("RGBA", (SIZE, SIZE)), mask))
    return out


def centered(d, text, font, cy, fill=(255, 255, 255, 255), **kw):
    d.text((SIZE / 2, cy), text, font=font, fill=fill, anchor="mm", **kw)


def make(name, spec, fonts: Path, out: Path):
    label, gfont, glyph, top, bottom = spec
    img = tile(top, bottom)
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd, d = ImageDraw.Draw(shadow), ImageDraw.Draw(img)
    if glyph:
        emoji = gfont == "seguiemj.ttf"
        f = ImageFont.truetype(str(fonts / gfont), 220 if not emoji else 200)
        cy = 215 if label else SIZE / 2
        if emoji:
            centered(d, glyph, f, cy, embedded_color=True)
        else:
            centered(sd, glyph, f, cy + 8, fill=(0, 0, 0, 90))
            centered(d, glyph, f, cy)
        if label:
            lf = ImageFont.truetype(str(fonts / "seguisb.ttf"), 78)
            centered(sd, label, lf, 400 + 5, fill=(0, 0, 0, 90))
            centered(d, label, lf, 400)
    else:                                  # brand text tile
        size = 120 if len(label) <= 6 else 88
        lf = ImageFont.truetype(str(fonts / "segoeuib.ttf"), size)
        while ImageDraw.Draw(img).textlength(label, font=lf) > SIZE - 70:
            size -= 4; lf = ImageFont.truetype(str(fonts / "segoeuib.ttf"), size)
        centered(sd, label, lf, SIZE / 2 + 6, fill=(0, 0, 0, 90))
        centered(d, label, lf, SIZE / 2)
    img = Image.alpha_composite(Image.alpha_composite(Image.new("RGBA", (SIZE, SIZE)),
                                img), shadow.filter(ImageFilter.GaussianBlur(6))) if False else img
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
    # redraw foreground over the soft shadow
    d = ImageDraw.Draw(img)
    if glyph:
        f = ImageFont.truetype(str(fonts / gfont), 220 if gfont != "seguiemj.ttf" else 200)
        cy = 215 if label else SIZE / 2
        centered(d, glyph, f, cy, embedded_color=(gfont == "seguiemj.ttf"))
        if label:
            centered(d, label, ImageFont.truetype(str(fonts / "seguisb.ttf"), 78), 400)
    else:
        centered(d, label, lf, SIZE / 2)
    img.save(out / f"{name}.png", optimize=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("--font-dir", default="/mnt/c/Windows/Fonts")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for name, spec in ICONS.items():
        make(name, spec, Path(a.font_dir), out)
    print("made", len(ICONS), "icons in", out)


if __name__ == "__main__":
    main()
