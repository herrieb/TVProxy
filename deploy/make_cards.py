#!/usr/bin/env python3
"""Draw the still images for the TV notice clips (mountains + lake, text on glass).

    python3 make_cards.py OUT_DIR [--font-dir DIR]

Writes recording-in-progress.png (sunset) and stream-in-use.png (moonlit night),
1920x1080. Everything is procedural (no stock photos). Needs Pillow only; the
PNGs are turned into video on the Pi by make-busy-clip.sh.
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
HORIZON = 700                     # waterline


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops, height):
    """Vertical gradient image from [(pos 0..1, (r,g,b)), ...]."""
    col = Image.new("RGB", (1, height))
    px = col.load()
    for y in range(height):
        t = y / (height - 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                px[0, y] = lerp(c0, c1, (t - p0) / (p1 - p0 or 1))
                break
    return col.resize((W, height))


def ridge(rng, base, rough, peaks, jag=0.55):
    """Mountain ridge heights across the width (midpoint displacement + big peaks)."""
    n = 1 << 10
    pts = [0.0] * (n + 1)
    pts[0], pts[n] = rng.uniform(-1, 1), rng.uniform(-1, 1)
    step, scale = n, 1.0
    while step > 1:
        half = step // 2
        for i in range(half, n, step):
            pts[i] = (pts[i - half] + pts[i + half]) / 2 + rng.uniform(-scale, scale)
        step, scale = half, scale * jag
    ys = []
    for x in range(W):
        f = x / W * n
        i = int(f)
        v = pts[i] + (pts[min(i + 1, n)] - pts[i]) * (f - i)
        bump = sum(h * max(0.0, 1 - abs(x - cx) / wd) ** 1.15 for cx, wd, h in peaks)
        ys.append(base - bump - v * rough)
    return ys


def layer(ys, color, top_light=None):
    """Filled mountain silhouette as RGBA, optional lit rim / snow on the peaks."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(0, H)] + [(x, ys[x]) for x in range(W)] + [(W - 1, H)], fill=color + (255,))
    if top_light:
        light, snow_below = top_light
        for x in range(W):
            y = ys[x]
            if y < snow_below:                       # snow caps on the high parts
                ragged = 0.55 + 0.45 * math.sin(x / 9.0) * math.sin(x / 23.0 + 1.3)
                depth = (snow_below - y) * 0.42 * ragged
                d.line([(x, y), (x, y + depth)], fill=light + (175,))
    return img


def pine(d, x, base, h, color):
    w = h * 0.32
    d.rectangle([x - 2, base - h * 0.12, x + 2, base], fill=color)
    for k in range(6):
        t = k / 6
        y = base - h * 0.12 - t * h * 0.88
        ww = w * (1 - t) + 3
        d.polygon([(x - ww, y), (x + ww, y), (x, y - h * 0.26)], fill=color)


def scene(theme: str, seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    sunset = theme == "sunset"
    if sunset:
        sky_stops = [(0, (24, 26, 64)), (0.35, (84, 54, 118)), (0.62, (206, 104, 112)),
                     (0.85, (250, 170, 110)), (1, (255, 214, 150))]
        far, mid, near, front = (150, 104, 140), (96, 64, 108), (54, 38, 74), (22, 16, 34)
        rim, glow_c, glow_xy, glow_r = (255, 214, 180), (255, 186, 120), (1190, HORIZON - 175), 520
    else:
        sky_stops = [(0, (4, 8, 24)), (0.45, (12, 26, 58)), (0.8, (30, 56, 96)),
                     (1, (52, 84, 124))]
        far, mid, near, front = (52, 74, 110), (34, 50, 82), (20, 30, 54), (8, 12, 24)
        rim, glow_c, glow_xy, glow_r = (190, 214, 255), (170, 200, 255), (640, 250), 230

    img = gradient(sky_stops, HORIZON).convert("RGBA")
    canvas = Image.new("RGBA", (W, H))
    canvas.paste(img, (0, 0))

    # stars (more at night)
    stars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for _ in range(140 if sunset else 650):
        x, y = rng.uniform(0, W), rng.uniform(0, HORIZON * (0.45 if sunset else 0.8)) ** 1.0
        a = int(rng.uniform(60, 230) * (1 - y / HORIZON))
        r = rng.choice([0.6, 0.8, 1.0, 1.0, 1.4, 2.0])
        sd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    canvas = Image.alpha_composite(canvas, stars)

    # sun / moon with a wide soft glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(glow_r, 0, -6):
        a = int(110 * (1 - r / glow_r) ** 2.2)
        gx, gy = glow_xy
        gd.ellipse([gx - r, gy - r, gx + r, gy + r], fill=glow_c + (a,))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(30)))
    # sun / moon disc, drawn 4x larger and scaled down for clean edges (no dark fringe)
    gx, gy = glow_xy
    S, R = 4, 70
    big = Image.new("L", (2 * R * S, 2 * R * S), 0)
    bd = ImageDraw.Draw(big)
    c = R * S
    if sunset:
        bd.ellipse([c - 62 * S, c - 62 * S, c + 62 * S, c + 62 * S], fill=255)
        color = (255, 240, 214)
    else:
        bd.ellipse([c - 46 * S, c - 46 * S, c + 46 * S, c + 46 * S], fill=255)
        bd.ellipse([c - 20 * S, c - 52 * S, c + 70 * S, c + 30 * S], fill=0)   # crescent cut
        color = (236, 240, 255)
    mask = big.resize((2 * R, 2 * R), Image.LANCZOS)
    disc = Image.new("RGBA", (W, H), color + (0,))
    disc.putalpha(0)
    alpha = Image.new("L", (W, H), 0)
    alpha.paste(mask, (int(gx - R), int(gy - R)))
    disc.putalpha(alpha)
    canvas = Image.alpha_composite(canvas, disc)

    # thin clouds
    clouds = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(clouds)
    for _ in range(18):
        cx, cy = rng.uniform(-200, W + 200), rng.uniform(120, HORIZON - 260)
        cw, ch = rng.uniform(300, 700), rng.uniform(10, 26)
        c = (255, 200, 170) if sunset else (150, 170, 210)
        cd.ellipse([cx - cw, cy - ch, cx + cw, cy + ch], fill=c + (rng.randint(18, 45),))
    canvas = Image.alpha_composite(canvas, clouds.filter(ImageFilter.GaussianBlur(14)))

    # mountain layers, far (hazy, snowy) to near (dark)
    m_far = ridge(rng, HORIZON - 140, 70, [(470, 330, 300), (860, 260, 200), (1560, 360, 330),
                                            (1900, 260, 180)], 0.62)
    m_mid = ridge(rng, HORIZON - 60, 55, [(180, 300, 220), (1000, 360, 190), (1750, 280, 170)], 0.6)
    m_near = ridge(rng, HORIZON - 8, 35, [(90, 300, 150), (700, 330, 80), (1700, 300, 140)], 0.55)
    land = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    land = Image.alpha_composite(land, layer(m_far, far, (rim, HORIZON - 330)))
    mist = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(mist).rectangle([0, HORIZON - 130, W, HORIZON - 40],
                                   fill=(glow_c if sunset else (120, 150, 200)) + (70,))
    land = Image.alpha_composite(land, mist.filter(ImageFilter.GaussianBlur(40)))
    land = Image.alpha_composite(land, layer(m_mid, mid, (rim, HORIZON - 300)))
    land = Image.alpha_composite(land, mist.filter(ImageFilter.GaussianBlur(60)))
    land = Image.alpha_composite(land, layer(m_near, near))
    # pines on both shores
    td = ImageDraw.Draw(land)
    for x0, x1 in ((0, 430), (1500, W)):
        for _ in range(55):
            x = rng.uniform(x0, x1)
            pine(td, x, HORIZON + rng.uniform(-4, 6), rng.uniform(40, 120), near + (255,))
    canvas = Image.alpha_composite(canvas, land)
    canvas = canvas.crop((0, 0, W, HORIZON)).resize((W, HORIZON))

    # lake: darkened, blurred mirror image with ripples and a glitter path
    out = Image.new("RGBA", (W, H))
    out.paste(canvas, (0, 0))
    mirror = canvas.transpose(Image.FLIP_TOP_BOTTOM).crop((0, 0, W, H - HORIZON))
    mirror = mirror.resize((W, int(mirror.size[1] * 1.0))).filter(ImageFilter.GaussianBlur(5))
    shade = Image.new("RGBA", mirror.size, (6, 10, 24, 95 if sunset else 115))
    mirror = Image.alpha_composite(mirror, shade)
    rip = Image.new("RGBA", mirror.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(rip)
    lake_h = mirror.size[1]
    c = (255, 226, 180) if sunset else (215, 228, 255)
    for _ in range(900):
        y = rng.uniform(0, 1) ** 1.6 * lake_h
        spread = 60 + y * 1.1
        x = rng.gauss(glow_xy[0], spread)
        ln = rng.uniform(8, 50) * (1 + y / 120)
        a = int(rng.uniform(80, 210) * max(0.0, 1 - abs(x - glow_xy[0]) / (spread * 2.2)))
        if a > 6:
            rd.line([(x - ln / 2, y), (x + ln / 2, y)], fill=c + (a,), width=2 if y > 140 else 1)
    for _ in range(260):                                   # faint ripples elsewhere
        y = rng.uniform(0, lake_h)
        x = rng.uniform(0, W)
        ln = rng.uniform(40, 220)
        rd.line([(x, y), (x + ln, y)], fill=(255, 255, 255, rng.randint(6, 18)), width=1)
    mirror = Image.alpha_composite(mirror, rip.filter(ImageFilter.GaussianBlur(0.8)))
    out.paste(mirror, (0, HORIZON))

    # foreground shore + soft vignette
    fg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)
    shore = ridge(rng, H - 40, 30, [(140, 300, 70), (1820, 320, 90)], 0.5)
    fd.polygon([(0, H)] + [(x, shore[x]) for x in range(W)] + [(W - 1, H)], fill=front + (255,))
    for x in list(range(20, 340, 38)) + list(range(1640, 1920, 34)):
        pine(fd, x + rng.uniform(-10, 10), shore[min(int(x), W - 1)] + 8,
             rng.uniform(150, 290), front + (255,))
    out = Image.alpha_composite(out, fg)
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse([-W * 0.25, -H * 0.35, W * 1.25, H * 1.35], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    out = Image.composite(out, dark, ImageChops.multiply(vig, Image.new("L", (W, H), 255))
                          .point(lambda v: 90 + v * 165 // 255))
    return out.convert("RGB")


def glass_card(img: Image.Image, lines, fonts, accent=None, center_y=H / 2 - 40) -> Image.Image:
    """Frosted-glass panel with centred text (and an optional coloured dot)."""
    d = ImageDraw.Draw(img)
    sizes = [(d.textlength(t, font=f), f.size) for t, f in zip(lines, fonts)]
    dot = 26 if accent else 0
    gap = 34 if accent else 0
    w = max(sw for sw, _ in sizes) + (2 * dot + gap if accent else 0) + 160
    h = sum(sz * 1.35 for _, sz in sizes) + 110
    x0, y0 = (W - w) / 2, center_y - h / 2
    box = tuple(int(v) for v in (x0, y0, x0 + w, y0 + h))
    region = img.crop(box).filter(ImageFilter.GaussianBlur(22))
    tint = Image.new("RGB", region.size, (10, 14, 30))
    region = Image.blend(region, tint, 0.38)
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, *region.size], radius=36, fill=255)
    # soft shadow under the panel
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([box[0], box[1] + 18, box[2], box[3] + 18], 40, fill=150)
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img,
                          sh.filter(ImageFilter.GaussianBlur(30)))
    img.paste(region, box[:2], mask)
    img = img.convert("RGBA")
    edge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(box, radius=36, outline=(255, 255, 255, 60), width=2)
    img = Image.alpha_composite(img, edge)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer_txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdw, d = ImageDraw.Draw(shadow), ImageDraw.Draw(layer_txt)
    y = y0 + 55
    for (text, font), (sw, sz) in zip(zip(lines, fonts), sizes):
        lh = sz * 1.35
        tx = (W - sw - (2 * dot + gap if accent and font is fonts[0] else 0)) / 2
        if accent and font is fonts[0]:
            cy = y + lh / 2 + 6
            sdw.ellipse([tx - 14, cy - dot - 14, tx + 2 * dot + 14, cy + dot + 14],
                        fill=accent + (150,))                       # red glow
            d.ellipse([tx, cy - dot, tx + 2 * dot, cy + dot], fill=accent + (255,))
            tx += 2 * dot + gap
        sdw.text((tx, y + lh / 2 + 4), text, font=font, fill=(0, 0, 0, 170), anchor="lm")
        d.text((tx, y + lh / 2), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
        y += lh
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(10)))
    return Image.alpha_composite(img, layer_txt).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--font-dir", default="/mnt/c/Windows/Fonts")
    a = ap.parse_args()
    fd = Path(a.font_dir)
    title = ImageFont.truetype(str(fd / "segoeuisl.ttf"), 96)
    title2 = ImageFont.truetype(str(fd / "segoeuisl.ttf"), 76)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rec = glass_card(scene("sunset"), ["Recording in progress"], [title], accent=(239, 68, 68),
                     center_y=330)
    rec.save(out / "recording-in-progress.png", optimize=True)
    use = glass_card(scene("night", seed=11),
                     ["This stream is being used", "on a different device"], [title2, title2])
    use.save(out / "stream-in-use.png", optimize=True)


if __name__ == "__main__":
    main()
