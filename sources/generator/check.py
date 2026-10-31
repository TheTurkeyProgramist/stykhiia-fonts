"""Compare every letter/digit of our font against reference fonts (skeleton chamfer distance).

d = mean symmetric distance between the skeletons of two glyphs rendered at cap height 200 px,
expressed in % of cap height. Weight-independent. Smaller = more similar.
"""
import glob
import json
import os
import re
import sys
from functools import lru_cache

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import distance_transform_edt
from skimage.morphology import skeletonize

REF = "/tmp/claude-0/-home-claude/8f9d9e74-dd51-5a61-8cd8-f177a40d87c2/scratchpad/reffonts/node_modules/@fontsource"
CACHE = "/tmp/claude-0/-home-claude/8f9d9e74-dd51-5a61-8cd8-f177a40d87c2/scratchpad/refcache"
LAT = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
CYR = "АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгґдеєжзиіїйклмнопрстуфхцчшщьюяЁЫЭЪЎЂЃЅЈЉЊЋЌЍЏЀёыэъўђѓѕјљњћќѝџѐ"
DIG = "0123456789"
CHARS = LAT + CYR + DIG
CAP = 200
CANVAS = (420, 460)
BASE = (210, 330)


def ref_fonts():
    os.makedirs(CACHE, exist_ok=True)
    fonts = {}
    for d in sorted(glob.glob(REF + "/*")):
        name = os.path.basename(d)
        files = glob.glob(d + "/files/*-normal.woff2")
        out = {}
        for subset in ("latin", "cyrillic"):
            cand = [f for f in files if re.search(rf"-{subset}-(\d+)-normal", f)]
            if not cand:
                continue
            best = min(cand, key=lambda f: abs(int(re.search(r"-(\d+)-normal", f).group(1)) - 400))
            ttf = os.path.join(CACHE, os.path.basename(best).replace(".woff2", ".ttf"))
            if not os.path.exists(ttf):
                t = TTFont(best)
                t.flavor = None
                t.save(ttf)
            out[subset] = ttf
        if out:
            fonts[name] = out
    return fonts


@lru_cache(maxsize=None)
def cmap_of(path):
    return set(TTFont(path).getBestCmap().keys())


@lru_cache(maxsize=None)
def scale_for(path):
    f = ImageFont.truetype(path, 400)
    for probe in ("H", "Н"):
        if ord(probe) in cmap_of(path):
            l, t, r, b = f.getbbox(probe, anchor="ls")
            return 400 * CAP / (b - t)
    return None


def render(path, ch):
    s = scale_for(path)
    if s is None or ord(ch) not in cmap_of(path):
        return None
    f = ImageFont.truetype(path, max(8, round(s)))
    img = Image.new("L", CANVAS, 0)
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox(BASE, ch, font=f, anchor="ls")
    dx = BASE[0] - (l + r) / 2
    d.text((BASE[0] + dx, BASE[1]), ch, font=f, fill=255, anchor="ls")
    a = np.array(img) > 127
    if a.sum() < 30:
        return None
    return a


def skel(path, ch):
    a = render(path, ch)
    if a is None:
        return None
    sk = skeletonize(a)
    return sk, distance_transform_edt(~sk).astype("float32")


def dist(p1, p2, ch, s1=None):
    s1, s2 = s1 or skel(p1, ch), skel(p2, ch)
    if s1 is None or s2 is None:
        return None
    a = s2[1][s1[0]].mean()
    b = s1[1][s2[0]].mean()
    return 100 * (a + b) / 2 / CAP


def font_for(fonts_entry, ch):
    sub = "cyrillic" if ch in CYR else "latin"
    return fonts_entry.get(sub)


def compare(ours, threshold, fonts=None):
    fonts = fonts or ref_fonts()
    rows = {}
    for ch in CHARS:
        best = (999, None)
        mine = skel(ours, ch)
        for name, ent in fonts.items():
            p = font_for(ent, ch)
            if not p:
                continue
            d = dist(ours, p, ch, mine) if mine else None
            if d is not None and d < best[0]:
                best = (d, name)
        rows[ch] = best
    unique = [c for c, (d, _) in rows.items() if d >= threshold]
    return rows, len(unique) / len(rows)


if __name__ == "__main__":
    ours, thr = sys.argv[1], float(sys.argv[2])
    rows, share = compare(ours, thr)
    for c, (d, n) in sorted(rows.items(), key=lambda kv: kv[1][0]):
        print(f"{c} {d:5.2f} {n}")
    print(f"UNIQUE {share*100:.1f}% at threshold {thr}")
