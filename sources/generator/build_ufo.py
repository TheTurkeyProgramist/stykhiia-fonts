"""Export Stykhiia Text as a UFO source (cubic Bezier outlines, components, anchors, features)
and build TTF + WOFF2 with fontmake, as Google Fonts expects.

usage: python build_ufo.py <out_dir>
"""
import math
import subprocess
import sys
import unicodedata
from pathlib import Path

import glyphsets
import ufoLib2
from fontTools.agl import UV2AGL
from fontTools.ttLib import TTFont
from ufoLib2.objects import Anchor, Component

import bezfit
import stykh_font as sf

FAMILY = "Stykhiia Text"
PS = "StykhiiaText"
STYLE = {"n": 3.0, "caps": "flat", "w": 92, "contrast": 0.5, "flare": 0.5, "bow": 0.035}
WITH_ICONS = "--with-icons" in sys.argv   # python build_ufo.py <out> --with-icons  -> site version
REPO_URL = "https://github.com/TheTurkeyProgramist/stykhiia-fonts"
COPYRIGHT = f"Copyright 2026 The Stykhiia Project Authors ({REPO_URL})"
OFL_DESC = ("This Font Software is licensed under the SIL Open Font License, Version 1.1. "
            "This license is available with a FAQ at: https://openfontlicense.org")

TOP_MARKS = {0x0300, 0x0301, 0x0302, 0x0303, 0x0304, 0x0306, 0x0307, 0x0308, 0x030A, 0x030B, 0x030C}
BOTTOM_MARKS = {0x0326, 0x0327}
COMMA_BELOW_BASES = set("GKLNRgklnr")       # Latvian / Romanian style: cedilla shown as comma
CARON_ALT_BASES = set("dltL")               # ď ľ ť Ľ use an apostrophe-like caron


def gname(cp):
    return UV2AGL.get(cp) or (f"uni{cp:04X}" if cp <= 0xFFFF else f"u{cp:05X}")


def ink_band(contours, y0, y1):
    xs = [x for c in contours for x, y in c if y0 <= y <= y1]
    return (min(xs), max(xs)) if xs else None


def poly_bbox(contours):
    xs = [x for c in contours for x, _ in c]
    ys = [y for c in contours for _, y in c]
    return min(xs), min(ys), max(xs), max(ys)


def orient_ccw(contours):
    """UFO (PostScript) convention: outer contours counter-clockwise."""
    if not contours:
        return contours
    big = max(contours, key=lambda c: abs(sf._area(c)))
    if sf._area(big) < 0:
        return [c[::-1] for c in contours]
    return contours


def draw_into(glyph, contours):
    fitted = bezfit.fit_all(contours)
    bezfit.draw_fitted(fitted, glyph.getPen())
    return fitted


def main(out_dir):
    out = Path(out_dir)
    (out / "sources").mkdir(parents=True, exist_ok=True)
    sf.STYLE.update(STYLE)
    defs = sf.define()
    mark_defs = sf.marks()

    ufo = ufoLib2.Font()
    order, categories = [".notdef", "space"], {}
    info = {}           # glyph name -> dict(contours=polygons after shift, width)

    # .notdef / space
    nd = ufo.newGlyph(".notdef")
    pen = nd.getPen()
    for box in ([(60, 0), (460, 0), (460, 700), (60, 700)], [(130, 70), (130, 630), (390, 630), (390, 70)]):
        pen.moveTo(box[0]); [pen.lineTo(p) for p in box[1:]]; pen.closePath()
    nd.width = 520
    sp = ufo.newGlyph("space"); sp.width = 260; sp.unicodes = [0x20, 0xA0]

    def add_outline(name, contours, sb=None, fixed=None, cps=()):
        contours = orient_ccw(contours)
        x0, y0, x1, y1 = poly_bbox(contours)
        if fixed:
            dx = (fixed - (x1 - x0)) / 2 - x0
            width = fixed
        elif sb is None:   # marks: keep their own x (centred on 0)
            dx, width = 0, 0
        else:
            dx = sb - x0
            width = round(x1 - x0 + 2 * sb)
        contours = [[(x + dx, y) for x, y in c] for c in contours]
        gl = ufo.newGlyph(name)
        draw_into(gl, contours)
        gl.width = width
        gl.unicodes = list(cps)
        order.append(name)
        info[name] = {"contours": contours, "width": width}
        return gl

    # ---------------------------------------------------------- what we need to cover
    target = set()
    for gs in ("GF_Latin_Core", "GF_Cyrillic_Core"):
        target |= set(glyphsets.unicodes_per_glyphset(gs))
    target |= {ord(c) for c in defs}
    # whole Latin-1 + Latin Extended-A + Romanian/Welsh/Vietnamese-free extras, where they are "letter + accent"
    target |= set(range(0xC0, 0x180)) | set(range(0x218, 0x21C)) | set(range(0x1E80, 0x1E86)) | {0x1EF2, 0x1EF3}
    if WITH_ICONS:                         # PUA weather icons: site build only, not for Google Fonts
        target |= set(range(0xE000, 0xE005))
    target -= {0x20, 0xA0}

    def decomposition(cp):
        ch = chr(cp)
        nfd = unicodedata.normalize("NFD", ch)
        if len(nfd) < 2 or nfd == ch:
            return None
        base, mks = nfd[0], [ord(m) for m in nfd[1:]]
        if base not in defs and ord(base) not in (0x131, 0x237):
            return None
        if not all(m in mark_defs for m in mks):
            return None
        return base, mks

    # ---------------------------------------------------------- drawn glyphs
    seen = {}
    name_of = {}
    for ch, items in defs.items():
        cp = ord(ch)
        if decomposition(cp):          # accented letters become base + mark components
            continue
        key = id(items)
        name = gname(cp)
        if key in seen:                # identical shape to an earlier glyph (e.g. Cyrillic А = Latin A)
            src = seen[key]
            gl = ufo.newGlyph(name)
            gl.components.append(Component(src))
            gl.width = info[src]["width"]
            gl.unicodes = [cp]
            order.append(name)
            info[name] = dict(info[src], alias=src)
            name_of[ch] = name
            continue
        path = sf.make_path(items)
        if ch.isalnum():
            path = sf.trim_bumps(path)
        contours = sf.contours_of(path)
        if ch in "+−=×÷<>~":
            add_outline(name, contours, fixed=600, cps=[cp])
        elif ch.isdigit():
            add_outline(name, contours, sb=44, cps=[cp])
            add_outline(name + ".tf", contours, fixed=580)
        else:
            sb = 48 if ch in sf.ROUND_START else 62
            if not ch.isalnum():
                sb = 50
            add_outline(name, contours, sb=sb, cps=[cp])
        seen[key] = name
        name_of[ch] = name
    ufo["hyphen"].unicodes = [0x2D, 0x2010]

    # ---------------------------------------------------------- weather icons (monoline)
    old = dict(sf.STYLE)
    sw, ic = sf.icons() if WITH_ICONS else (0, {})
    sf.STYLE.update({"w": sw, "contrast": 1.0, "flare": 0.0, "caps": "round", "n": 2.0})
    for cp, items in ic.items():
        add_outline(gname(cp), sf.outline(items), fixed=840, cps=[cp])
    sf.STYLE.clear(); sf.STYLE.update(old)

    # ---------------------------------------------------------- marks
    mark_name = {}
    for key, items in mark_defs.items():
        name = gname(key) if isinstance(key, int) else key
        add_outline(name, sf.outline(items), sb=None)
        g = ufo[name]
        if key in (0x0326, 0x0327):
            g.anchors.append(Anchor(0, 0, "_bottom"))
        elif key == 0x0328:
            g.anchors.append(Anchor(0, 0, "_ogonek"))
        elif key != "caroncomb.alt":
            g.anchors.append(Anchor(0, 500, "_top"))
        categories[name] = "mark"
        mark_name[key] = name
    for key in mark_defs:
        if isinstance(key, int):
            ufo[mark_name[key]].unicodes = [key]

    # ---------------------------------------------------------- anchors on bases
    def set_anchors(name):
        g = ufo[name]
        cs = info[name]["contours"]
        if not cs:
            return
        x0, y0, x1, y1 = poly_bbox(cs)
        top = ink_band(cs, y1 - 70, y1 + 1)
        bot = ink_band(cs, -2, 70) or ink_band(cs, y0 - 1, y0 + 70)
        g.anchors.append(Anchor(round((top[0] + top[1]) / 2), round(y1), "top"))
        if bot:
            g.anchors.append(Anchor(round((bot[0] + bot[1]) / 2), 0, "bottom"))
            g.anchors.append(Anchor(round(bot[1] - 45), 0, "ogonek"))

    letters = [n for ch, n in name_of.items() if (ch.isalpha() or ch == "◌") and "alias" not in info[n]]
    for n in letters:
        set_anchors(n)
    for ch, n in name_of.items():          # aliases (Cyrillic А etc.) copy anchors of their source
        if "alias" in info[n]:
            for a in ufo[info[n]["alias"]].anchors:
                ufo[n].anchors.append(Anchor(a.x, a.y, a.name))

    # ---------------------------------------------------------- composites (accented letters)
    def base_name(base_ch, mks):
        if base_ch in "iі" and any(m in TOP_MARKS for m in mks):
            return gname(0x131)
        if base_ch in "jј" and any(m in TOP_MARKS for m in mks):
            return gname(0x237)
        n = name_of[base_ch]
        return info[n].get("alias", n)

    def anchor(gl, nm):
        for a in gl.anchors:
            if a.name == nm:
                return a
        return None

    made = 0
    for cp in sorted(target):
        dec = decomposition(cp)
        if not dec:
            continue
        base_ch, mks = dec
        bname = base_name(base_ch, mks)
        bg = ufo[bname]
        name = gname(cp)
        gl = ufo.newGlyph(name)
        gl.components.append(Component(bname))
        gl.width = bg.width
        gl.unicodes = [cp]
        stack_top = None
        for m in mks:
            if m == 0x0327 and base_ch in COMMA_BELOW_BASES:
                if base_ch == "g":   # Latvian ģ: turned comma above
                    mk = mark_name["commaturnedabovecomb"]; an, man = "top", "_top"
                else:
                    mk = mark_name[0x0326]; an, man = "bottom", "_bottom"
            elif m == 0x030C and base_ch in CARON_ALT_BASES:
                cs = info[bname]["contours"]
                x0, y0, x1, y1 = poly_bbox(cs)
                top = ink_band(cs, y1 - 70, y1 + 1)
                dx = top[1] + 55
                dy = (y1 - 742) if base_ch != "L" else (700 - 742)
                gl.components.append(Component(mark_name["caroncomb.alt"], (1, 0, 0, 1, round(dx), round(dy))))
                gl.width = max(gl.width, round(dx + 90))
                continue
            elif m == 0x0328:
                mk, an, man = mark_name[m], "ogonek", "_ogonek"
            elif m in BOTTOM_MARKS:
                mk, an, man = mark_name[m], "bottom", "_bottom"
            else:
                mk, an, man = mark_name[m], "top", "_top"
            ba, ma = anchor(bg, an), anchor(ufo[mk], man)
            if ba is None or ma is None:
                continue
            dx, dy = ba.x - ma.x, ba.y - ma.y
            if an == "top" and stack_top is not None:
                dy += stack_top
            gl.components.append(Component(mk, (1, 0, 0, 1, dx, dy)))
            if an == "top":
                stack_top = (stack_top or 0) + 150
        for a in bg.anchors:
            y = a.y
            if a.name == "top" and any(m in TOP_MARKS or m == 0x030C for m in mks):
                y = a.y + 200
            if a.name == "bottom" and any(m in BOTTOM_MARKS for m in mks):
                y = a.y - 240
            gl.anchors.append(Anchor(a.x, y, a.name))
        order.append(name)
        made += 1

    # dotless variant of į for "į + top accent"
    io = ufo.newGlyph("iogonek.dotless")
    io.components.append(Component(gname(0x131)))
    ba, ma = anchor(ufo[gname(0x131)], "ogonek"), anchor(ufo[mark_name[0x0328]], "_ogonek")
    io.components.append(Component(mark_name[0x0328], (1, 0, 0, 1, ba.x - ma.x, ba.y - ma.y)))
    io.width = ufo[gname(0x131)].width
    order.append("iogonek.dotless")

    # ---------------------------------------------------------- spacing accents ( ˆ ˇ ˘ ˙ ˚ ˛ ˜ ˝ ¨ ¯ ´ ¸ ` )
    spacing = {0x02C6: 0x0302, 0x02C7: 0x030C, 0x02D8: 0x0306, 0x02D9: 0x0307, 0x02DA: 0x030A, 0x02DB: 0x0328,
               0x02DC: 0x0303, 0x02DD: 0x030B, 0x00A8: 0x0308, 0x00AF: 0x0304, 0x00B4: 0x0301, 0x00B8: 0x0327, 0x0060: 0x0300}
    for cp, m in spacing.items():
        name = gname(cp)
        cs = info[mark_name[m]]["contours"]
        x0, _, x1, _ = poly_bbox(cs)
        gl = ufo.newGlyph(name)
        gl.components.append(Component(mark_name[m], (1, 0, 0, 1, round(60 - x0), 0)))
        gl.width = round(x1 - x0 + 120)
        gl.unicodes = [cp]
        order.append(name)

    # ---------------------------------------------------------- features
    cmap = {}
    for g in ufo:
        for u in g.unicodes:
            cmap[u] = g.name

    def cls(chars):
        names = []
        for c in chars:
            for cp in [ord(c)] + [k for k in cmap if unicodedata.normalize("NFD", chr(k))[:1] == c and k != ord(c)]:
                n = cmap.get(cp)
                if n and n not in names:
                    names.append(n)
        return "[" + " ".join(names) + "]"

    digits = [cmap[ord(d)] for d in "0123456789"]
    strong, open_r = "TТГҐЃ", "FPРYVWУЎ"
    low = "acdegoqsuаседоєюёсьзфяæœø"
    punct = ".,…"
    topmarks = " ".join(mark_name[m] for m in sorted(TOP_MARKS))
    fea = f"""languagesystem DFLT dflt;
languagesystem latn dflt;
languagesystem cyrl dflt;

@CombTop = [{topmarks}];
@CombBottom = [{mark_name[0x0326]} {mark_name[0x0327]} {mark_name[0x0328]}];

feature ccmp {{
  sub [{cmap[ord('i')]} {cmap[0x456]}]' @CombTop by {gname(0x131)};
  sub [{cmap[ord('j')]} {cmap[0x458]}]' @CombTop by {gname(0x237)};
  sub [{cmap[ord('i')]} {cmap[0x456]}]' @CombBottom @CombTop by {gname(0x131)};
  sub [{cmap[ord('i')]} {cmap[0x456]}]' @CombBottom @CombBottom @CombTop by {gname(0x131)};
  sub [{cmap[ord('j')]} {cmap[0x458]}]' @CombBottom @CombTop by {gname(0x237)};
  sub {cmap[0x12F]}' @CombTop by iogonek.dotless;
  sub {cmap[0x12F]}' @CombBottom @CombTop by iogonek.dotless;
  sub [{cmap[ord('i')]} {cmap[0x456]}]' @CombBottom @CombBottom @CombBottom @CombTop by {gname(0x131)};
}} ccmp;

feature tnum {{
  sub [{' '.join(digits)}] by [{' '.join(d + '.tf' for d in digits)}];
}} tnum;

feature kern {{
  pos {cls(strong)} {cls(low)} -80;
  pos {cls(open_r)} {cls(low)} -45;
  pos {cls(strong + open_r)} {cls(punct)} -90;
  pos {cls('AАДЛ')} {cls('TТVWYУЎ')} -55;
  pos {cls('TТVWYУЎ')} {cls('AАДЛ')} -55;
  pos {cls('LЬЪ')} {cls('TТVWYУ')} -60;
  pos {cls('rгґ')} {cls(punct)} -45;
}} kern;
"""
    ufo.features.text = fea

    # ---------------------------------------------------------- font info
    fi = ufo.info
    fi.familyName = FAMILY
    fi.styleName = "Regular"
    fi.styleMapFamilyName = FAMILY
    fi.styleMapStyleName = "regular"
    fi.versionMajor, fi.versionMinor = 1, 0
    fi.copyright = COPYRIGHT
    fi.openTypeNameLicense = OFL_DESC
    fi.openTypeNameLicenseURL = "https://openfontlicense.org"
    fi.openTypeNameDesigner = "Stykhiia Project Authors"
    fi.openTypeNameManufacturer = "Stykhiia Project Authors"
    fi.openTypeNameManufacturerURL = REPO_URL
    fi.openTypeOS2VendorID = "NONE"
    fi.unitsPerEm = 1000
    fi.xHeight, fi.capHeight = 500, 700
    fi.ascender, fi.descender = 742, -212
    fi.openTypeOS2WeightClass, fi.openTypeOS2WidthClass = 400, 5
    fi.openTypeOS2Selection = [7]                 # USE_TYPO_METRICS
    fi.openTypeOS2TypoAscender, fi.openTypeOS2TypoDescender, fi.openTypeOS2TypoLineGap = 950, -250, 0
    fi.openTypeHheaAscender, fi.openTypeHheaDescender, fi.openTypeHheaLineGap = 950, -250, 0
    fi.postscriptUnderlinePosition, fi.postscriptUnderlineThickness = -110, 50
    fi.openTypeOS2Type = []                       # installable embedding
    fi.postscriptFontName = f"{PS}-Regular"
    # win metrics must cover every glyph
    ymax = ymin = 0
    for g in ufo:
        b = g.getBounds(ufo)
        if b:
            ymin, ymax = min(ymin, b.yMin), max(ymax, b.yMax)
    fi.openTypeOS2WinAscent = int(math.ceil(max(ymax, 950)))
    fi.openTypeOS2WinDescent = int(math.ceil(max(-ymin, 250)))

    ufo.lib["public.glyphOrder"] = order
    ufo.lib["public.openTypeCategories"] = {n: ("mark" if categories.get(n) == "mark" else "base") for n in order}
    ufo_path = out / "sources" / f"{PS}.ufo"
    ufo.save(ufo_path, overwrite=True)
    have = {u for g in ufo for u in g.unicodes}
    missing = sorted(c for c in target - have if not (0x180 > c >= 0x100 and not decomposition(c)))
    print("UFO saved:", ufo_path, "| glyphs:", len(ufo), "| composites made:", made,
          "| missing:", "".join(chr(c) for c in missing) or "none")

    # ---------------------------------------------------------- build with fontmake
    ttf_dir = out / "fonts" / "ttf"
    subprocess.run(["fontmake", "-u", str(ufo_path), "-o", "ttf", "--output-dir", str(ttf_dir)], check=True)
    ttf = ttf_dir / f"{PS}-Regular.ttf"
    built = ttf_dir / f"{PS}.ttf"
    if built.exists():
        built.replace(ttf)
    import ttfautohint
    from fontTools.ttLib import newTable
    hinted = ttfautohint.ttfautohint(in_file=str(ttf), out_file=None, default_script="latn",
                                     fallback_script="latn", no_info=True)
    ttf.write_bytes(hinted)
    f = TTFont(ttf)
    f["head"].flags |= 1 << 3          # hinted font: round PPEM to integers
    meta = newTable("meta")
    meta.data = {"dlng": "Latn, Cyrl", "slng": "Latn, Cyrl"}
    f["meta"] = meta
    f.save(ttf)
    web = out / "fonts" / "webfonts"
    web.mkdir(parents=True, exist_ok=True)
    f = TTFont(ttf)
    f.flavor = "woff2"
    f.save(web / f"{PS}-Regular.woff2")
    print("TTF:", ttf, ttf.stat().st_size, "bytes | WOFF2:", (web / f"{PS}-Regular.woff2").stat().st_size, "bytes")


if __name__ == "__main__":
    main(sys.argv[1])
