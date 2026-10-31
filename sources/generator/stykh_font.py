"""Stykhiia font engine: letters are stroke skeletons, outlined and merged with skia-pathops.

style = {"n": superellipse exponent (2 = circle, higher = squarer),
         "caps": "round" | "flat",
         "w": stroke width}
"""
import math
from pathlib import Path as FsPath

import pathops
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

UPM = 1000
T, B, M = 658, 42, 350          # cap skeleton: top, bottom, middle
XT, XB, XM = 458, 42, 250       # x-height skeleton
ASC, DESC = 700, -170           # ascender / descender skeleton

STYLE = {"n": 2.0, "caps": "round", "w": 84}


# ------------------------------------------------------------ primitives
def _se(v, n):
    return math.copysign(abs(v) ** (2.0 / n), v)


def E(cx, cy, rx, ry, a0, a1, steps=None):
    """Superellipse arc, angles in degrees (counter-clockwise)."""
    n = STYLE["n"]
    if steps is None:
        steps = max(8, int(abs(a1 - a0) / 6))
    pts = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append((cx + rx * _se(math.cos(a), n), cy + ry * _se(math.sin(a), n)))
    tilt = STYLE.get("tilt", 0.0)
    if tilt and ry > 40 and rx > 40:
        c, s_ = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
        pts = [(cx + (x - cx) * c - (y - cy) * s_, cy + (x - cx) * s_ + (y - cy) * c) for x, y in pts]
    if ry > 40:
        d = bar_d()
        top, bot = cy + ry, cy - ry
        for line in (T, XT, ASC):
            if abs(top - line) <= 12:
                k = (ry + line + d - top) / ry
                pts = [(x, cy + (y - cy) * k) if y > cy else (x, y) for x, y in pts]
                break
        for line in (B, XB):
            if abs(bot - line) <= 12:
                k = (ry + bot - (line - d)) / ry
                pts = [(x, cy - (cy - y) * k) if y < cy else (x, y) for x, y in pts]
                break
    return pts


EXT = 42  # distance from a skeleton line (T, B, XT...) to the ink line (700, 0, 500...)
TOP_LINES, BOTTOM_LINES = (T, XT, ASC), (B, XB)


def bar_d():
    """how far a thin horizontal bar's centre line must move so its outer edge sits on the ink line"""
    return EXT - STYLE["w"] * STYLE.get("contrast", 1.0) / 2


def _on_line(y):
    return any(abs(y - l) < 0.5 for l in TOP_LINES + BOTTOM_LINES + (DESC,))


def align_bars(pts):
    """horizontal bars lying on a metric line move outward so their edge (not centre) is on the line"""
    d = bar_d()
    out = list(pts)
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        horiz = False
        for j in (i - 1, i + 1):
            if 0 <= j < n:
                dx, dy = pts[j][0] - x, pts[j][1] - y
                if abs(dx) > 1 and abs(dy) <= 0.25 * abs(dx):
                    horiz = True
        if not horiz:
            continue
        if any(abs(y - l) < 0.5 for l in TOP_LINES):
            out[i] = (x, y + d)
        elif any(abs(y - l) < 0.5 for l in BOTTOM_LINES):
            out[i] = (x, y - d)
    return out


def L(*pts):
    return list(pts)


def DOT(cx, cy, r=50):
    return ("fill", E(cx, cy, r, r, 0, 360, 40))


def _circle(x, y, r, k=16):
    return [(x + r * math.cos(2 * math.pi * i / k), y + r * math.sin(2 * math.pi * i / k)) for i in range(k)]


def _area(poly):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1])) / 2


def _ccw(poly):
    return poly if _area(poly) > 0 else poly[::-1]


def _unit(dx, dy):
    d = math.hypot(dx, dy) or 1
    return dx / d, dy / d


def _subdiv(raw, step=16):
    pts = [raw[0]]
    for i in range(1, len(raw)):
        (x1, y1), (x2, y2) = raw[i - 1], raw[i]
        k = max(1, int(math.dist(raw[i - 1], raw[i]) / step))
        for j in range(1, k + 1):
            pts.append((x1 + (x2 - x1) * j / k, y1 + (y2 - y1) * j / k))
    return pts


def _dirs(pts, closed):
    n = len(pts)
    seg = [_unit(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1]) for i in range(n if closed else n - 1)]
    out = []
    for i in range(n):
        if closed:
            a, b = seg[i - 1], seg[i]
        else:
            a = seg[i - 1] if i > 0 else seg[0]
            b = seg[i] if i < n - 1 else seg[-1]
        out.append(_unit(a[0] + b[0], a[1] + b[1]))
    return out


def _offset(pts, dirs, widths):
    left = [(x - d[1] * ww / 2, y + d[0] * ww / 2) for (x, y), d, ww in zip(pts, dirs, widths)]
    right = [(x + d[1] * ww / 2, y - d[0] * ww / 2) for (x, y), d, ww in zip(pts, dirs, widths)]
    return left, right


def _turn_sharp(a, b, deg=25):
    return a[0] * b[0] + a[1] * b[1] < math.cos(math.radians(deg))


def stroke_polys(points, w, joined=(False, False), extend=False):
    """Variable-width stroke -> list of shapes (each shape: list of contours, outer first).

    Smooth runs become ONE polygon each (no seams); sharp corners get a mitre patch that
    overlaps both runs; closed smooth curves become a ring (outer + hole).
    contrast: horizontals thinner; flare: open ends widen (not where the end joins another stroke).
    """
    contrast = STYLE.get("contrast", 1.0)
    flare = STYLE.get("flare", 0.0)
    round_caps = STYLE["caps"] == "round"
    raw = [points[0]]
    for p in points[1:]:
        if math.dist(p, raw[-1]) > 0.5:
            raw.append(p)
    if len(raw) == 1:
        return [[_circle(raw[0][0], raw[0][1], w / 2)]]
    closed = len(raw) > 2 and math.dist(raw[0], raw[-1]) < 1
    if closed:
        raw = raw[:-1]
    n = len(raw)
    segd = [_unit(raw[(i + 1) % n][0] - raw[i][0], raw[(i + 1) % n][1] - raw[i][1]) for i in range(n if closed else n - 1)]
    if closed:
        corners = [i for i in range(n) if _turn_sharp(segd[i - 1], segd[i])]
    else:
        corners = [i for i in range(1, n - 1) if _turn_sharp(segd[i - 1], segd[i])]

    def fw(d):
        return w * (contrast + (1 - contrast) * abs(d[1]) ** 1.4)

    shapes = []
    if closed and not corners:
        pts = _subdiv(raw + [raw[0]])[:-1]
        dirs = _dirs(pts, True)
        widths = [fw(d) for d in dirs]
        left, right = _offset(pts, dirs, widths)
        a, b = (left, right) if abs(_area(left)) > abs(_area(right)) else (right, left)
        return [[_ccw(a), _ccw(b)[::-1]]]

    if closed:  # rotate to start at a corner, then treat as open polyline that returns to it
        k = corners[0]
        raw = raw[k:] + raw[:k] + [raw[k]]
        corner_set = {(c - k) % n for c in corners}
        corner_idx = sorted(corner_set - {0}) + [0]
    else:
        corner_idx = corners
    # global arc length for flare
    cum = [0.0]
    for i in range(1, len(raw)):
        cum.append(cum[-1] + math.dist(raw[i - 1], raw[i]))
    total = cum[-1]
    lam = min(120, total / 2.2)

    def flare_k(s):
        if closed or not flare or lam <= 0:
            return 1.0
        dd = min(s if not joined[0] else 1e9, (total - s) if not joined[1] else 1e9)
        return 1 + flare * max(0.0, 1 - dd / lam) ** 2

    cuts = sorted(set([0] + [c for c in corner_idx if 0 < c < len(raw) - 1] + [len(raw) - 1]))
    piece_ends = {}
    for a_i, b_i in zip(cuts, cuts[1:]):
        sub = raw[a_i:b_i + 1]
        pts = _subdiv(sub)
        # arc length of subdivided points
        s0 = cum[a_i]
        ss = [s0]
        for i in range(1, len(pts)):
            ss.append(ss[-1] + math.dist(pts[i - 1], pts[i]))
        dirs = _dirs(pts, False)
        widths = [fw(d) * flare_k(s) for d, s in zip(dirs, ss)]
        left, right = _offset(pts, dirs, widths)
        if extend and not closed and not round_caps:
            if a_i == 0 and not joined[0] and abs(dirs[0][1]) >= 0.45 and _on_line(pts[0][1]):
                d0 = dirs[0]
                yt = pts[0][1] - math.copysign(EXT, d0[1])
                left.insert(0, (left[0][0] - d0[0] * (left[0][1] - yt) / d0[1], yt))
                right.insert(0, (right[0][0] - d0[0] * (right[0][1] - yt) / d0[1], yt))
            if b_i == len(raw) - 1 and not joined[1] and abs(dirs[-1][1]) >= 0.45 and _on_line(pts[-1][1]):
                dn = dirs[-1]
                yt = pts[-1][1] + math.copysign(EXT, dn[1])
                left.append((left[-1][0] + dn[0] * (yt - left[-1][1]) / dn[1], yt))
                right.append((right[-1][0] + dn[0] * (yt - right[-1][1]) / dn[1], yt))
        shapes.append([_ccw(left + right[::-1])])
        piece_ends[a_i] = piece_ends.get(a_i, {})
        piece_ends[a_i]["out"] = (dirs[0], widths[0])
        piece_ends[b_i] = piece_ends.get(b_i, {})
        piece_ends[b_i]["in"] = (dirs[-1], widths[-1])
    # corner patches
    joins = [c for c in cuts if 0 < c < len(raw) - 1]
    if closed:
        piece_ends.setdefault(0, {})["in"] = piece_ends[len(raw) - 1]["in"]
        joins.append(0)
    for c in joins:
        (a, wa), (b, wb) = piece_ends[c]["in"], piece_ends[c]["out"]
        x, y = raw[c]
        cross = a[0] * b[1] - a[1] * b[0]
        sg = -1 if cross > 0 else 1
        na, nb = (-a[1] * sg, a[0] * sg), (-b[1] * sg, b[0] * sg)
        ra, rb = wa / 2, wb / 2
        pa, pb = (x + na[0] * ra, y + na[1] * ra), (x + nb[0] * rb, y + nb[1] * rb)
        m = _unit(na[0] + nb[0], na[1] + nb[1])
        r = (ra + rb) / 2
        cos_half = max(1e-3, m[0] * na[0] + m[1] * na[1])
        q = (x - m[0] * 0.4 * r, y - m[1] * 0.4 * r)
        ml = r / cos_half
        if ml <= 2.4 * r:
            poly = [q, pa, (x + m[0] * ml, y + m[1] * ml), pb]
        else:
            poly = [q, pa, pb]
        if abs(_area(poly)) > 1:
            shapes.append([_ccw(poly)])
    if not closed and round_caps:
        for i, end in ((0, 0), (len(raw) - 1, 1)):
            if not joined[end]:
                d = piece_ends[i]["out" if end == 0 else "in"]
                shapes.append([_circle(raw[i][0], raw[i][1], d[1] / 2, 20)])
    return shapes


def _seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _joined_ends(lines, w):
    """For each polyline: does its start / end lie inside another stroke (or a far part of itself)?"""
    res = []
    for i, (pts, ww) in enumerate(lines):
        flags = []
        for end in (0, 1):
            end_pt = pts[0] if end == 0 else pts[-1]
            best = 1e9
            for j, (q, wq) in enumerate(lines):
                if len(q) < 2:
                    continue
                if j == i:
                    sub = _subdiv(q, 8)
                    if end == 1:
                        sub = sub[::-1]
                    acc = 0.0
                    for k in range(1, len(sub)):
                        acc += math.dist(sub[k - 1], sub[k])
                        if acc > ww * 1.6:
                            best = min(best, math.dist(end_pt, sub[k]))
                else:
                    for k in range(len(q) - 1):
                        best = min(best, _seg_dist(end_pt, q[k], q[k + 1]))
            flags.append(best < ww * 0.45)
        res.append(tuple(flags))
    return res


def _union_polys(shapes):
    """Union of shapes; if skia fails on a big batch, split it and merge halves."""
    p = pathops.Path()
    pen = p.getPen()
    for shape in shapes:
        for c in shape:
            pen.moveTo(c[0])
            for q in c[1:]:
                pen.lineTo(q)
            pen.closePath()
    try:
        p.simplify(fix_winding=True)
        return p
    except pathops.PathOpsError:
        if len(shapes) == 1:
            return pathops.Path()
        h = len(shapes) // 2
        a, b = _union_polys(shapes[:h]), _union_polys(shapes[h:])
        try:
            return pathops.op(a, b, pathops.PathOp.UNION, fix_winding=True)
        except pathops.PathOpsError:
            return a


def _round_shape(shape):
    return [[(round(x, 1), round(y, 1)) for x, y in c] for c in shape]


def make_path(items):
    """items: polylines (stroked with STYLE w), ("fill", polygon), ("stroke", pts, w), ("path", pathops.Path)."""
    extra, shapes, lines = [], [], []
    for it in items:
        if isinstance(it, tuple) and it[0] == "path":
            extra.append(it[1])
        elif isinstance(it, tuple) and it[0] == "fill":
            if len(it[1]) >= 3 and abs(_area(it[1])) >= 0.5:
                shapes.append([_ccw(it[1])])
        elif isinstance(it, tuple) and it[0] == "stroke":
            lines.append((it[1], it[2]))
        else:
            lines.append((align_bars(it), STYLE["w"], True))
    lines = [ln if len(ln) == 3 else (ln[0], ln[1], False) for ln in lines]
    flags = _joined_ends([(p, w_) for p, w_, _ in lines], STYLE["w"]) if len(lines) < 60 else [(False, False)] * len(lines)
    for (pts, ww, ext), jf in zip(lines, flags):
        shapes.extend(stroke_polys(pts, ww, jf, extend=ext))
    shapes = [_round_shape(s) for s in shapes if s and all(len(c) >= 3 for c in s)]
    p = _union_polys(shapes)
    for e in extra:
        p = pathops.op(p, e, pathops.PathOp.UNION, fix_winding=True)
    return _drop_slivers(p)


def _drop_slivers(p, min_area=40):
    cs = contours_of(p)
    keep = [c for c in cs if abs(_area(c)) >= min_area]
    if len(keep) == len(cs):
        return p
    q = pathops.Path()
    pen = q.getPen()
    for c in keep:
        pen.moveTo(c[0])
        for pt in c[1:]:
            pen.lineTo(pt)
        pen.closePath()
    q.simplify(fix_winding=True)
    return q


def _rect(x0, y0, x1, y1):
    p = pathops.Path()
    pen = p.getPen()
    pen.moveTo((x0, y0)); pen.lineTo((x1, y0)); pen.lineTo((x1, y1)); pen.lineTo((x0, y1)); pen.closePath()
    return p


def trim_bumps(p, depth=24, probe=40):
    """Cut shallow bumps (flared bar ends, mitre tips) that poke past the baseline, x-height,
    cap height or ascender. Real descenders / ascenders (ink continuing further) are kept."""
    def cut(p, band_y0, band_y1, probe_y0, probe_y1):
        band = _rect(-3000, band_y0, 3000, band_y1)
        try:
            deeper = pathops.op(p, _rect(-3000, probe_y0, 3000, probe_y1), pathops.PathOp.INTERSECTION, fix_winding=True)
        except pathops.PathOpsError:
            return p
        keep = pathops.Path()
        for c in contours_of(deeper):
            xs = [x for x, _ in c]
            keep = pathops.op(keep, _rect(min(xs) - 3, band_y0 - 1, max(xs) + 3, band_y1 + 1), pathops.PathOp.UNION, fix_winding=True)
        try:
            remove = pathops.op(band, keep, pathops.PathOp.DIFFERENCE, fix_winding=True)
            return pathops.op(p, remove, pathops.PathOp.DIFFERENCE, fix_winding=True)
        except pathops.PathOpsError:
            return p
    p = cut(p, -depth, 0, -depth - probe, -depth)
    for line in (500, 700, 742):
        p = cut(p, line, line + depth, line + depth, line + depth + probe)
    return p


def outline(items):
    return contours_of(make_path(items))


def contours_of(p):
    rec = RecordingPen()
    p.draw(rec)
    contours, cur = [], []
    for op, args in rec.value:
        if op == "moveTo":
            cur = [args[0]]
        elif op == "lineTo":
            cur.append(args[0])
        elif op in ("qCurveTo", "curveTo"):
            cur.extend(args)
        elif op in ("closePath", "endPath"):
            if len(cur) > 2:
                contours.append(cur)
            cur = []
    return contours


# ------------------------------------------------------------ glyph skeletons
def define():
    g = {}
    # ---------- Latin capitals
    g["A"] = [L((0, B), (250, T), (500, B)), L((95, 230), (405, 230))]
    g["B"] = [L((220, T), (0, T), (0, B), (240, B)) + E(240, 203, 190, 161, -90, 90) + L((0, 364)),
              L((0, 364), (220, 364)) + E(220, 511, 170, 147, -90, 90) + L((220, T))]
    g["C"] = [E(300, 350, 300, 308, 45, 315)]
    g["D"] = [L((0, B), (0, T), (160, T)) + E(160, 350, 290, 308, 90, -90) + L((0, B))]
    g["E"] = [L((430, T), (0, T), (0, B), (430, B)), L((0, 420), (370, 420))]
    g["F"] = [L((430, T), (0, T), (0, B)), L((0, 420), (370, 420))]
    g["G"] = [E(300, 350, 300, 308, 45, 360) + L((360, 350))]
    g["H"] = [L((0, B), (0, T)), L((440, B), (440, T)), L((0, M), (440, M))]
    g["I"] = [L((0, B), (0, T))]
    g["J"] = [L((330, T), (330, 220)) + E(165, 220, 165, 178, 0, -175)]
    g["K"] = [L((0, B), (0, T)), L((410, T), (0, 250)), L((150, 399), (430, B))]
    g["L"] = [L((0, T), (0, B), (400, B))]
    g["M"] = [L((0, B), (0, T), (280, 250), (560, T), (560, B))]
    g["N"] = [L((0, B), (0, T), (450, B), (450, T))]
    g["O"] = [E(320, 350, 320, 308, 0, 360)]
    g["P"] = [L((0, B), (0, T), (220, T)) + E(220, 495, 200, 163, 90, -90) + L((0, 332))]
    g["Q"] = [E(320, 350, 320, 308, 0, 360), L((390, 170), (620, -40))]
    g["R"] = [L((0, B), (0, T), (220, T)) + E(220, 495, 200, 163, 90, -90) + L((0, 332)),
              L((230, 332), (450, B))]
    g["S"] = [E(245, 503, 205, 155, 25, 270) + E(245, 198, 230, 150, 90, -155)]
    g["T"] = [L((0, T), (480, T)), L((240, T), (240, B))]
    g["U"] = [L((0, T), (0, 250)) + E(220, 250, 220, 208, 180, 360) + L((440, T))]
    g["V"] = [L((0, T), (250, B), (500, T))]
    g["W"] = [L((0, T), (175, B), (350, T), (525, B), (700, T))]
    g["X"] = [L((0, T), (460, B)), L((460, T), (0, B))]
    g["Y"] = [L((0, T), (240, 330), (480, T)), L((240, 330), (240, B))]
    g["Z"] = [L((20, T), (450, T), (0, B), (460, B))]

    # ---------- Latin lowercase
    bowl = E(205, XM, 205, 208, 0, 360)
    g["a"] = [bowl, L((410, XT), (410, XB))]
    g["b"] = [L((0, ASC), (0, XB)), [(x - 0, y) for x, y in E(205, XM, 205, 208, 0, 360)]]
    g["c"] = [E(210, XM, 210, 208, 45, 315)]
    g["d"] = [bowl, L((410, ASC), (410, XB))]
    g["e"] = [L((0, XM)) + E(210, XM, 210, 208, 0, 320)]
    g["f"] = [L((90, XB), (90, 560)) + E(200, 560, 110, 140, 180, 55), L((0, XT), (240, XT))]
    g["g"] = [bowl, L((410, XT), (410, -40)) + E(205, -40, 205, 130, 0, -165)]
    g["h"] = [L((0, ASC), (0, XB)), L((0, 300)) + E(250, 300, 250, 158, 180, 90) + E(250, 300, 150, 158, 90, 0) + L((400, XB))]
    g["i"] = [L((0, XB), (0, XT)), DOT(0, 610)]
    g["j"] = [L((150, XT), (150, -60)) + E(30, -60, 120, 110, 0, -150), DOT(150, 610)]
    g["k"] = [L((0, ASC), (0, XB)), L((360, XT), (0, 180)), L((130, 280), (380, XB))]
    g["l"] = [L((0, ASC), (0, 150)) + E(110, 150, 110, 108, 180, 285)]
    g["m"] = [L((0, XT), (0, XB)), L((0, 320)) + E(193, 320, 193, 138, 180, 90) + E(193, 320, 117, 138, 90, 0) + L((310, XB)),
              L((310, 320)) + E(503, 320, 193, 138, 180, 90) + E(503, 320, 117, 138, 90, 0) + L((620, XB))]
    g["n"] = [L((0, XT), (0, XB)), L((0, 300)) + E(250, 300, 250, 158, 180, 90) + E(250, 300, 150, 158, 90, 0) + L((400, XB))]
    g["o"] = [E(215, XM, 215, 208, 0, 360)]
    g["p"] = [L((0, XT), (0, DESC)), E(205, XM, 205, 208, 0, 360)]
    g["q"] = [bowl, L((410, XT), (410, DESC))]
    g["r"] = [L((0, XT), (0, XB)), L((0, 290)) + E(190, 290, 190, 168, 180, 55)]
    g["s"] = [E(175, 355, 152, 103, 25, 270) + E(175, 149, 168, 107, 90, -155)]
    g["t"] = [L((90, 640), (90, 150)) + E(200, 150, 110, 108, 180, 285), L((0, XT), (270, XT))]
    g["u"] = [L((0, XT), (0, 200)) + E(150, 200, 150, 158, 180, 270) + E(150, 200, 250, 158, 270, 360), L((400, XT), (400, XB))]
    g["v"] = [L((0, XT), (210, XB), (420, XT))]
    g["w"] = [L((0, XT), (150, XB), (300, 400), (450, XB), (600, XT))]
    g["x"] = [L((0, XT), (400, XB)), L((400, XT), (0, XB))]
    g["y"] = [L((0, XT), (205, 40)), L((420, XT), (110, DESC))]
    g["z"] = [L((10, XT), (380, XT), (0, XB), (390, XB))]

    # ---------- Cyrillic capitals (shared shapes reuse Latin)
    for cyr, lat in zip("АВЕКМНОРСТХІ", "ABEKMHOPCTXI"):
        g[cyr] = g[lat]
    g["Б"] = [L((430, T), (0, T), (0, B), (240, B)) + E(240, 203, 190, 161, -90, 90) + L((0, 364))]
    g["Г"] = [L((420, T), (0, T), (0, B))]
    g["Ґ"] = [L((0, B), (0, T), (400, T), (400, 800))]
    g["Д"] = [L((40, B), (200, T), (440, T), (440, B)), L((-30, -130), (-30, B), (510, B), (510, -130))]
    g["Є"] = [E(300, 350, 300, 308, 45, 315), L((5, 420), (380, 420))]
    g["Ж"] = [L((20, T), (300, M), (580, T)), L((0, B), (300, M), (600, B)), L((300, B), (300, T))]
    g["З"] = [E(230, 508, 200, 150, 150, -90) + E(240, 200, 220, 158, 90, -150)]
    g["И"] = [L((0, T), (0, B), (450, T), (450, B))]
    g["Й"] = g["И"] + [E(225, 800, 115, 70, 200, 340)]
    g["Ї"] = [L((0, B), (0, T)), DOT(-95, 790, 46), DOT(95, 790, 46)]
    g["Л"] = [L((0, B), (150, T), (450, T), (450, B))]
    g["П"] = [L((-45, T), (485, T)), L((0, T), (0, B)), L((440, T), (440, B))]
    g["У"] = [L((0, T), (313, 290)), L((480, T), (200, B), (90, B))]
    g["Ф"] = [L((300, B), (300, T)), E(300, 370, 290, 200, 0, 360)]
    g["Ц"] = [L((0, T), (0, B), (500, B), (500, -130)), L((440, T), (440, B))]
    g["Ч"] = [L((0, T), (0, 440)) + E(160, 440, 160, 110, 180, 270) + L((420, 330)), L((420, T), (420, B))]
    g["Ш"] = [L((0, T), (0, B), (560, B), (560, T)), L((280, T), (280, B))]
    g["Щ"] = [L((0, T), (0, B), (620, B), (620, -130)), L((280, T), (280, B)), L((560, T), (560, B))]
    g["Ь"] = [L((0, T), (0, B), (240, B)) + E(240, 203, 190, 161, -90, 90) + L((0, 364))]
    g["Ю"] = [L((0, B), (0, T)), L((0, M), (160, M)), E(400, 350, 240, 308, 0, 360)]
    g["Я"] = [L((450, B), (450, T), (230, T)) + E(230, 495, 200, 163, 90, 270) + L((450, 332)),
              L((240, 332), (0, B))]

    # ---------- Cyrillic lowercase
    for cyr, lat in zip("аеорсухі", "aeopcyxi"):
        g[cyr] = g[lat]
    g["б"] = [E(210, XM, 210, 208, 0, 360), L((0, XM), (20, 520), (120, 650), (380, ASC))]
    g["в"] = [L((170, XT), (0, XT), (0, XB), (190, XB)) + E(190, 146, 150, 104, -90, 90) + L((0, 250)),
              L((0, 250), (170, 250)) + E(170, 354, 130, 104, -90, 90) + L((170, XT))]
    g["г"] = [L((330, XT), (0, XT), (0, XB))]
    g["ґ"] = [L((0, XB), (0, XT), (310, XT), (310, 590))]
    g["д"] = [L((30, XB), (150, XT), (330, XT), (330, XB)), L((-30, -120), (-30, XB), (400, XB), (400, -120))]
    g["є"] = [E(210, XM, 210, 208, 45, 315), L((0, XM), (300, XM))]
    g["ж"] = [L((20, XT), (260, XM), (500, XT)), L((0, XB), (260, XM), (520, XB)), L((260, XB), (260, XT))]
    g["з"] = [E(170, 356, 145, 102, 150, -90) + E(180, 148, 165, 106, 90, -150)]
    g["и"] = [L((0, XT), (0, XB), (380, XT), (380, XB))]
    g["й"] = g["и"] + [E(190, 600, 100, 60, 200, 340)]
    g["ї"] = [L((0, XB), (0, XT)), DOT(-88, 610, 44), DOT(88, 610, 44)]
    g["к"] = [L((0, XT), (0, XB)), L((330, XT), (0, 240)), L((110, 313), (360, XB))]
    g["л"] = [L((0, XB), (110, XT), (370, XT), (370, XB))]
    g["м"] = [L((0, XB), (0, XT), (230, 180), (460, XT), (460, XB))]
    g["н"] = [L((0, XB), (0, XT)), L((380, XB), (380, XT)), L((0, XM), (380, XM))]
    g["п"] = [L((-35, XT), (415, XT)), L((0, XT), (0, XB)), L((380, XT), (380, XB))]
    g["т"] = [L((0, XT), (400, XT)), L((200, XT), (200, XB))]
    g["ф"] = [L((250, DESC), (250, ASC)), E(250, XM, 250, 208, 0, 360)]
    g["ц"] = [L((0, XT), (0, XB), (440, XB), (440, -120)), L((380, XT), (380, XB))]
    g["ч"] = [L((0, XT), (0, 300)) + E(140, 300, 140, 90, 180, 270) + L((360, 210)), L((360, XT), (360, XB))]
    g["ш"] = [L((0, XT), (0, XB), (520, XB), (520, XT)), L((260, XT), (260, XB))]
    g["щ"] = [L((0, XT), (0, XB), (580, XB), (580, -120)), L((260, XT), (260, XB)), L((520, XT), (520, XB))]
    g["ь"] = [L((0, XT), (0, XB), (200, XB)) + E(200, 146, 150, 104, -90, 90) + L((0, 250))]
    g["ю"] = [L((0, XB), (0, XT)), L((0, XM), (140, XM)), E(340, XM, 200, 208, 0, 360)]
    g["я"] = [L((380, XB), (380, XT), (170, XT)) + E(170, 354, 150, 104, 90, 270) + L((380, 250)),
              L((200, 250), (0, XB))]

    # ---------- Stykhiia signature features
    def wing(x0, x1, y, sag, k=16):        # bar that dips in the middle, ends lifted like wings
        y += bar_d()
        xm, hw = (x0 + x1) / 2, (x1 - x0) / 2
        return [(x0 + (x1 - x0) * i / k, y - sag * (1 - ((x0 + (x1 - x0) * i / k - xm) / hw) ** 2)) for i in range(k + 1)]

    def flick(x0, x1, y, sag, k=16):       # arm rising from the stem towards its free end
        y += bar_d()
        return [(x0 + (x1 - x0) * i / k, y - sag * (1 - i / k) ** 2) for i in range(k + 1)]

    def Q(p0, p1, p2, k=18):              # quadratic curve
        return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in [i / k for i in range(k + 1)]]

    g["T"] = [wing(0, 480, T, 50), L((240, T - 50), (240, B))]
    g["т"] = [wing(0, 400, XT, 38), L((200, XT - 38), (200, XB))]
    g["A"] = [L((0, B), (205, T), (295, T), (500, B)), L((70, 270), (430, 270))]
    g["M"] = [L((0, B), (70, T), (290, 230), (510, T), (580, B))]
    g["V"] = [L((0, T), (225, B), (275, B), (500, T))]
    g["v"] = [L((0, XT), (185, XB), (235, XB), (420, XT))]
    g["e"] = [L((0, 232), (420, 275), (420, XM)) + E(210, XM, 210, 208, 0, 320)]
    atail = L((410, XT), (410, 140)) + E(495, 140, 85, 98, 180, 280)
    g["a"] = [E(205, XM, 205, 208, 0, 360), atail]
    g["d"] = [E(205, XM, 205, 208, 0, 360), L((410, ASC), (410, 140)) + E(495, 140, 85, 98, 180, 280)]
    g["Y"] = [Q((0, T), (60, 400), (240, 330)), Q((480, T), (420, 400), (240, 330)), L((240, 330), (240, B))]
    g["z"] = [L((10, XT), (380, XT), (0, XB), (390, XB)), L((90, XM), (300, XM))]
    g["Г"] = [L((0, B)) + flick(0, 420, T, 30)]
    g["Ґ"] = [L((0, B)) + flick(0, 400, T, 30) + L((400, 800))]
    g["г"] = [L((0, XB)) + flick(0, 330, XT, 24)]
    g["ґ"] = [L((0, XB)) + flick(0, 310, XT, 24) + L((310, 590))]
    g["Б"] = [flick(0, 430, T, 30)[::-1] + L((0, B), (240, B)) + E(240, 203, 190, 161, -90, 90) + L((0, 364))]
    g["Ш"] = [L((0, T), (0, B), (560, B), (560, T)), L((280, T - 120), (280, B))]
    g["Щ"] = [L((0, T), (0, B), (620, B), (620, -130)), L((280, T - 120), (280, B)), L((560, T), (560, B))]
    g["ш"] = [L((0, XT), (0, XB), (520, XB), (520, XT)), L((260, XT - 105), (260, XB))]
    g["щ"] = [L((0, XT), (0, XB), (580, XB), (580, -120)), L((260, XT - 105), (260, XB)), L((520, XT), (520, XB))]
    g["H"] = [L((0, B), (0, T)), L((440, B), (440, T)), L((0, 430), (440, 430))]
    g["н"] = [L((0, XB), (0, XT)), L((380, XB), (380, XT)), L((0, 300), (380, 300))]
    g["U"] = [L((0, T), (0, 250)) + E(220, 250, 220, 208, 180, 360) + L((440, T)), L((440, 250), (440, B))]
    tail = L((0, XT), (0, 140)) + E(85, 140, 85, 98, 180, 280)
    g["i"] = [tail, DOT(0, 610)]
    g["ї"] = [tail, DOT(-88, 610, 44), DOT(88, 610, 44)]
    g["Т"], g["Н"], g["і"] = g["T"], g["H"], g["i"]
    g["А"], g["М"], g["а"], g["е"] = g["A"], g["M"], g["a"], g["e"]

    # ---------- other Cyrillic languages (ru, be, bg, sr, mk)
    def cx_of(items):
        xs = [x for it in items for x, _ in (it[1] if isinstance(it, tuple) else it)]
        return (min(xs) + max(xs)) / 2

    def acc(items, kind, cap=True):
        c = cx_of(items)
        y = 770 if cap else 560
        if kind == "acute":
            mark = [L((c - 40, y), (c + 70, y + 110))]
        elif kind == "grave":
            mark = [L((c + 40, y), (c - 70, y + 110))]
        elif kind == "dots":
            mark = [DOT(c - 95, y + 30, 46), DOT(c + 95, y + 30, 46)]
        else:  # breve
            mark = [E(c, y + 40, 110 if cap else 100, 65, 200, 340)]
        return items + mark

    g["Ъ"] = [L((0, T), (130, T), (130, B), (360, B)) + E(360, 203, 190, 161, -90, 90) + L((130, 364))]
    g["Ы"] = [L((0, T), (0, B), (220, B)) + E(220, 203, 175, 161, -90, 90) + L((0, 364)), L((560, T), (560, B))]
    g["Э"] = [E(300, 350, 300, 308, 135, -135), L((220, 420), (595, 420))]
    g["Ё"] = acc(g["E"], "dots")
    g["Ѐ"] = acc(g["E"], "grave")
    g["Ѓ"] = acc(g["Г"], "acute")
    g["Ќ"] = acc(g["K"], "acute")
    g["Ѝ"] = acc(g["И"], "grave")
    g["Ў"] = acc(g["У"], "breve")
    g["Ѕ"] = g["S"]
    g["Ј"] = g["J"]
    g["Ђ"] = [L((0, T), (380, T)), L((150, T), (150, B)),
              L((150, 400)) + E(310, 330, 160, 110, 160, 0) + L((470, 150)) + E(390, 150, 80, 108, 0, -120)]
    g["Ћ"] = [L((0, T), (380, T)), L((150, T), (150, B)),
              L((150, 400)) + E(310, 330, 160, 110, 160, 0) + L((470, B))]
    g["Љ"] = [L((0, B), (130, T), (330, T), (330, B), (480, B)) + E(480, 203, 170, 161, -90, 90) + L((330, 364))]
    g["Њ"] = [L((0, B), (0, T)), L((0, 364), (480, 364)) + E(480, 203, 170, 161, 90, -90) + L((330, B), (330, T))]
    g["Џ"] = [L((0, T), (0, B), (440, B), (440, T)), L((220, B), (220, -130))]

    g["ъ"] = [L((0, XT), (110, XT), (110, XB), (300, XB)) + E(300, 146, 150, 104, -90, 90) + L((110, 250))]
    g["ы"] = [L((0, XT), (0, XB), (180, XB)) + E(180, 146, 140, 104, -90, 90) + L((0, 250)), L((460, XT), (460, XB))]
    g["э"] = [E(210, XM, 210, 208, 135, -135), L((120, XM), (420, XM))]
    g["ё"] = acc(g["e"], "dots", cap=False)
    g["ѐ"] = acc(g["e"], "grave", cap=False)
    g["ѓ"] = acc(g["г"], "acute", cap=False)
    g["ќ"] = acc(g["к"], "acute", cap=False)
    g["ѝ"] = acc(g["и"], "grave", cap=False)
    g["ў"] = acc(g["y"], "breve", cap=False)
    g["ѕ"] = g["s"]
    g["ј"] = g["j"]
    g["ђ"] = [L((0, ASC), (0, XB)), L((-60, 600), (220, 600)),
              L((0, 300)) + E(200, 300, 200, 158, 180, 0) + L((400, -40)) + E(310, -40, 90, 130, 0, -150)]
    g["ћ"] = [L((0, ASC), (0, XB)), L((-60, 600), (220, 600)),
              L((0, 300)) + E(200, 300, 200, 158, 180, 0) + L((400, XB))]
    g["љ"] = [L((0, XB), (110, XT), (300, XT), (300, XB), (430, XB)) + E(430, 146, 150, 104, -90, 90) + L((300, 250))]
    g["њ"] = [L((0, XB), (0, XT)), L((0, 250), (430, 250)) + E(430, 146, 150, 104, 90, -90) + L((300, XB), (300, XT))]
    g["џ"] = [L((0, XT), (0, XB), (380, XB), (380, XT)), L((190, XB), (190, -120))]
    g["·"] = [DOT(0, 330, 50)]

    # ---------- digits
    g["0"] = [E(240, 350, 205, 308, 0, 360)]
    g["1"] = [L((240, T), (240, B)), L((228, T - 80), (70, 540)), L((110, B), (370, B))]
    g["2"] = [E(240, 495, 205, 165, 165, -35) + L((30, B), (460, B))]
    g["3"] = [E(230, 520, 195, 150, 150, -90) + E(230, 195, 215, 153, 90, -150)]
    g["4"] = [L((355, B), (355, T)), L((335, T - 85), (10, 215), (470, 215))]
    g["5"] = [L((445, T), (85, T), (55, 360)) + E(230, 248, 215, 206, 118, -140)]
    g["6"] = [L((410, 640), (280, 678), (150, 620), (45, 440), (22, 250)) + E(240, 240, 218, 205, 180, -180)]
    g["7"] = [L((15, 658), (460, 658), (160, B))]
    g["8"] = [E(240, 520, 168, 148, 0, 360), E(240, 198, 212, 156, 0, 360)]
    g["9"] = [[(480 - x, 700 - y) for x, y in g["6"][0]]]

    # ---------- punctuation & weather symbols
    g["."] = [DOT(0, 55, 55)]
    g[","] = [DOT(0, 60, 55), L((0, 60), (-30, -90))]
    g[":"] = [DOT(0, 55, 55), DOT(0, 420, 55)]
    g[";"] = [DOT(0, 60, 55), L((0, 60), (-30, -90)), DOT(0, 420, 55)]
    g["!"] = [L((0, T), (0, 230)), DOT(0, 55, 55)]
    g["?"] = [E(180, 500, 180, 158, 165, -60) + L((180, 340), (180, 230)), DOT(180, 55, 55)]
    g["'"] = [L((0, T + 40), (0, 500))]
    g["’"] = [DOT(0, 660, 50), L((0, 660), (-30, 510))]
    g["ʼ"] = g["’"]
    g["-"] = [L((0, 290), (240, 290))]
    g["−"] = [L((0, 320), (300, 320))]
    g["+"] = [L((0, 320), (320, 320)), L((160, 160), (160, 480))]
    g["/"] = [L((0, -60), (300, 720))]
    g["("] = [E(260, 300, 260, 460, 110, 250)]
    g[")"] = [E(-60, 300, 260, 460, 70, -70)]
    g["°"] = [E(95, 545, 95, 95, 0, 360)]
    g["%"] = [E(90, 545, 90, 100, 0, 360), E(390, 160, 90, 100, 0, 360), L((420, 665), (60, 35))]
    g["—"] = [L((0, 300), (800, 300))]
    g["–"] = [L((0, 300), (420, 300))]
    g["…"] = [DOT(0, 55, 55), DOT(190, 55, 55), DOT(380, 55, 55)]
    g['"'] = [L((0, T + 40), (0, 500)), L((150, T + 40), (150, 500))]
    g["“"] = [DOT(0, 520, 50), L((0, 520), (30, 670)), DOT(150, 520, 50), L((150, 520), (180, 670))]
    g["”"] = [DOT(0, 660, 50), L((0, 660), (-30, 510)), DOT(150, 660, 50), L((150, 660), (120, 510))]
    g["„"] = [DOT(0, 60, 50), L((0, 60), (-30, -90)), DOT(150, 60, 50), L((150, 60), (120, -90))]
    g["№"] = [L((0, B), (0, T), (380, B), (380, T)), E(570, 450, 85, 105, 0, 360), L((470, 260), (670, 260))]
    g["₴"] = [[(480 - x, y) for x, y in g["S"][0]], L((0, 440), (480, 440)), L((0, 265), (480, 265))]
    g["$"] = [g["S"][0], L((245, -50), (245, 750))]
    g["€"] = [E(330, 350, 300, 308, 45, 315), L((-40, 410), (380, 410)), L((-40, 290), (330, 290))]
    g["="] = [L((0, 410), (380, 410)), L((0, 230), (380, 230))]
    g["<"] = [L((380, 560), (0, 320), (380, 80))]
    g[">"] = [L((0, 560), (380, 320), (0, 80))]
    g["["] = [L((150, 760), (0, 760), (0, -120), (150, -120))]
    g["]"] = [L((0, 760), (150, 760), (150, -120), (0, -120))]
    g["*"] = [("stroke", [(150, 400), (150, 720)], 54), ("stroke", [(12, 480), (288, 640)], 54), ("stroke", [(12, 640), (288, 480)], 54)]
    g["#"] = [L((150, B), (230, T)), L((350, B), (430, T)), L((40, 450), (520, 450)), L((20, 250), (500, 250))]
    g["_"] = [L((0, -120), (500, -120))]
    g["«"] =[L((180, 420), (0, 250), (180, 80)), L((380, 420), (200, 250), (380, 80))]
    g["»"] = [L((0, 420), (180, 250), (0, 80)), L((200, 420), (380, 250), (200, 80))]
    add_core_extras(g, Q)
    if STYLE.get("bow"):
        bow_diagonals(g, STYLE["bow"], STYLE.get("stem_bow", 0.0))
    return g


def bow_diagonals(g, k, ks=0.0):
    """Long diagonal strokes of letters bend gently towards the glyph's centre (concave),
    like grass under wind. k = sagitta as a fraction of the stroke length."""
    done = set()
    for ch, items in g.items():
        if not ch.isalpha() or id(items) in done:
            continue
        done.add(id(items))
        pts_all = [p for it in items if isinstance(it, list) for p in it]
        if not pts_all:
            continue
        cxg = (min(x for x, _ in pts_all) + max(x for x, _ in pts_all)) / 2
        for idx, it in enumerate(items):
            if not isinstance(it, list) or len(it) < 2:
                continue
            new = [it[0]]
            for p, q in zip(it, it[1:]):
                dx, dy = q[0] - p[0], q[1] - p[1]
                L_ = math.hypot(dx, dy)
                diag = L_ > 250 and abs(dx) > 0.25 * L_ and abs(dy) > 0.4 * L_
                stem = ks > 0 and L_ > 300 and abs(dx) <= 0.25 * L_
                kk = k if diag else ks
                if diag or stem:
                    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
                    nx, ny = -dy / L_, dx / L_
                    if abs(mx - cxg) < 30:          # stroke through the centre (X): leave straight
                        new.append(q)
                        continue
                    sgn = 1 if (cxg - mx) * nx > 0 else -1
                    c = (mx + nx * sgn * 2 * kk * L_, my + ny * sgn * 2 * kk * L_)
                    for t in [i / 16 for i in range(1, 17)]:
                        new.append(((1 - t) ** 2 * p[0] + 2 * (1 - t) * t * c[0] + t * t * q[0],
                                    (1 - t) ** 2 * p[1] + 2 * (1 - t) * t * c[1] + t * t * q[1]))
                else:
                    new.append(q)
            items[idx] = new


def _xform(items, fn):
    out = []
    for it in items:
        if isinstance(it, tuple) and it[0] == "fill":
            out.append(("fill", [fn(p) for p in it[1]]))
        elif isinstance(it, tuple) and it[0] == "stroke":
            out.append(("stroke", [fn(p) for p in it[1]], it[2]))
        else:
            out.append([fn(p) for p in it])
    return out


def rot180(items, cx, cy):
    return _xform(items, lambda p: (2 * cx - p[0], 2 * cy - p[1]))


def shift(items, dx, dy):
    return _xform(items, lambda p: (p[0] + dx, p[1] + dy))


def add_core_extras(g, Q):
    """Glyphs needed for Google Fonts GF Latin Core + GF Cyrillic Core that are not plain accents."""
    S = lambda pts, w: ("stroke", pts, w)
    # ---- ASCII / Latin-1 symbols
    g["&"] = [L((470, B), (149, 470)) + E(220, 548, 100, 110, 225, -45) + L((60, 252)) + E(210, 195, 165, 153, 163, 385) + L((440, 340))]
    g["@"] = [E(330, 300, 125, 140, 0, 360),
              L((455, 440), (455, 200)) + E(535, 200, 80, 90, 180, 360) + L((698, 204)) + E(360, 300, 350, 370, -15, 290)]
    g["\\"] = [L((0, 720), (300, -60))]
    g["^"] = [S([(0, 380), (180, 680), (360, 380)], 70)]
    g["{"] = [Q((200, 770), (110, 770), (110, 650)) + L((110, 410)) + Q((110, 410), (110, 330), (30, 320)) + Q((30, 320), (110, 310), (110, 230)) + L((110, 0)) + Q((110, 0), (110, -120), (200, -120))]
    g["}"] = _xform(g["{"], lambda p: (230 - p[0], p[1]))
    g["|"] = [L((0, -160), (0, 800))]
    g["~"] = [S([(i * 10, 290 + 55 * math.sin(i / 40 * 2 * math.pi)) for i in range(41)], 72)]
    g["¡"] = [DOT(0, 445, 55), L((0, 270), (0, -170))]
    g["¿"] = rot180(g["?"], 180, 230)
    g["¢"] = g["c"] + [S([(215, -60), (215, 560)], 50)]
    g["£"] = [E(300, 520, 150, 140, 30, 180) + L((150, 230)) + Q((150, 230), (150, 100), (40, B)) + L((470, B)), L((20, 330), (320, 330))]
    g["¥"] = [L((0, T), (240, 360), (480, T)), L((240, 360), (240, B)), L((90, 290), (390, 290)), L((90, 160), (390, 160))]
    g["§"] = shift(g["s"], 0, 230) + shift(g["s"], 0, -40)
    g["©"] = [S(E(380, 340, 360, 360, 0, 360), 56), S(E(380, 340, 160, 170, 45, 315), 56)]
    g["®"] = [S(E(380, 340, 360, 360, 0, 360), 56),
              S([(280, 160), (280, 520), (400, 520)] + E(400, 440, 90, 80, 90, -90) + [(280, 360)], 56),
              S([(390, 360), (480, 160)], 56)]
    g["™"] = [S([(0, 700), (280, 700)], 50), S([(140, 700), (140, 430)], 50),
              S([(350, 430), (350, 700), (490, 520), (630, 700), (630, 430)], 50)]
    g["ª"] = [S(E(110, 580, 105, 115, 0, 360), 52), S([(215, 695), (215, 465)], 52), S([(0, 400), (240, 400)], 46)]
    g["º"] = [S(E(115, 580, 110, 115, 0, 360), 52), S([(0, 400), (230, 400)], 46)]
    g["¶"] = [("fill", E(180, 530, 170, 170, 90, 270) + [(270, 360), (270, 700)]),
              L((270, 700), (270, -120)), L((420, 700), (420, -120)), L((200, T), (450, T))]
    g["×"] = [L((0, 140), (320, 460)), L((0, 460), (320, 140))]
    g["÷"] = [L((0, 300), (380, 300)), DOT(190, 480, 52), DOT(190, 120, 52)]
    g["•"] = [DOT(0, 330, 100)]
    g["‘"] = [DOT(0, 560, 50), L((0, 560), (30, 710))]
    g["‚"] = g[","]
    g["‹"] = [L((180, 420), (0, 250), (180, 80))]
    g["›"] = [L((0, 420), (180, 250), (0, 80))]
    g["◌"] = [DOT(260 + 230 * math.cos(math.radians(a)), 330 + 230 * math.sin(math.radians(a)), 30) for a in range(0, 360, 30)]
    # ---- Latin letters that are not "base + accent"
    g["Æ"] = [L((0, B), (330, T), (720, T)), L((380, T), (380, B), (720, B)), L((380, M), (670, M)), L((130, 240), (380, 240))]
    g["æ"] = [E(190, XM, 190, 208, 0, 360), L((380, XT), (380, XB)), L((380, 205), (790, XM)) + E(580, XM, 210, 208, 0, 320)]
    g["Œ"] = [L((740, T), (340, T)) + E(330, 350, 330, 308, 90, 270) + L((740, B)), L((400, T), (400, B)), L((400, M), (690, M))]
    g["œ"] = [E(200, XM, 200, 208, 0, 360), L((395, 205), (805, XM)) + E(595, XM, 210, 208, 0, 320)]
    g["Ø"] = g["O"] + [L((40, -30), (600, 730))]
    g["ø"] = g["o"] + [L((10, -40), (420, 540))]
    g["Ð"] = g["D"] + [L((-70, M), (200, M))]
    g["Đ"] = g["Ð"]
    g["đ"] = g["d"] + [L((290, 610), (540, 610))]
    g["ð"] = [E(205, 225, 205, 195, 0, 360), L((410, 225), (405, 420)) + Q((405, 420), (395, 620), (170, 710)), L((140, 530), (430, 660))]
    g["Þ"] = [L((0, B), (0, T)), L((0, 540), (220, 540)) + E(220, 385, 200, 155, 90, -90) + L((0, 230))]
    g["þ"] = [L((0, ASC), (0, DESC)), E(205, XM, 205, 208, 0, 360)]
    g["ß"] = [L((0, XB), (0, 520)) + E(185, 520, 185, 180, 180, -50) + L((230, 335)) + E(230, 185, 185, 150, 95, -128)]
    g["ẞ"] = [L((0, B), (0, 520)) + E(210, 520, 210, 138, 180, 0) + L((250, 370)) + E(250, 205, 210, 165, 90, -128)]
    g["ı"] = [g["i"][0]]
    g["ȷ"] = [g["j"][0]]
    g["Ł"] = [L((0, T), (0, B), (400, B)), L((-80, 270), (160, 440))]
    g["ł"] = [g["l"][0], L((-80, 300), (170, 480))]
    g["Ħ"] = g["H"] + [L((-60, 560), (500, 560))]
    g["ħ"] = g["h"] + [L((-70, 600), (230, 600))]
    # ---- GF Cyrillic Core extras (Kazakh, Tatar, Bashkir, Tajik, Uzbek, Kyrgyz...)
    g["Ғ"] = g["Г"] + [L((-70, 380), (240, 380))]
    g["ғ"] = g["г"] + [L((-60, 260), (200, 260))]
    g["Җ"] = g["Ж"] + [L((600, B), (670, B), (670, -130))]
    g["җ"] = g["ж"] + [L((520, XB), (590, XB), (590, -120))]
    g["Қ"] = g["K"] + [L((430, B), (500, B), (500, -130))]
    g["қ"] = g["к"] + [L((360, XB), (430, XB), (430, -120))]
    g["Ң"] = [L((0, B), (0, T)), L((440, T), (440, B), (520, B), (520, -130)), L((0, 430), (440, 430))]
    g["ң"] = [L((0, XB), (0, XT)), L((380, XT), (380, XB), (450, XB), (450, -120)), L((0, 276), (380, 276))]
    g["Ү"] = [L((0, T), (240, 330), (480, T)), L((240, 330), (240, B))]
    g["ү"] = [L((0, XT), (200, 120), (400, XT)), L((200, 120), (200, DESC))]
    g["Ұ"] = g["Ү"] + [L((90, 250), (390, 250))]
    g["ұ"] = g["ү"] + [L((60, -10), (340, -10))]
    g["Ҳ"] = g["X"] + [L((460, B), (530, B), (530, -130))]
    g["ҳ"] = g["x"] + [L((400, XB), (470, XB), (470, -120))]
    g["Ҷ"] = g["Ч"] + [L((420, B), (500, B), (500, -130))]
    g["ҷ"] = g["ч"] + [L((360, XB), (430, XB), (430, -120))]
    g["Һ"] = [L((0, B), (0, T)), L((0, 400)) + E(220, 380, 220, 180, 180, 0) + L((440, B))]
    g["һ"] = g["h"]
    g["Ә"] = rot180([L((0, 320), (600, M)) + E(300, 350, 300, 308, 0, 320)], 300, 350)
    g["ә"] = rot180(g["e"], 210, 250)
    g["Ө"] = g["O"] + [L((0, M), (640, M))]
    g["Ŀ"] = [L((0, T), (0, B), (400, B)), DOT(240, 360, 50)]
    g["ŀ"] = [g["l"][0], DOT(250, 380, 50)]
    g["Ѫ"] = [L((40, T), (560, T), (300, M), (40, T)), L((300, M), (300, B)), L((300, M), (20, B)), L((300, M), (580, B))]
    g["ѫ"] = [L((30, XT), (450, XT), (240, XM), (30, XT)), L((240, XM), (240, XB)), L((240, XM), (10, XB)), L((240, XM), (470, XB))]
    g["Ѣ"] = [L((130, 800), (130, B), (360, B)) + E(360, 203, 190, 161, -90, 90) + L((130, 364)), L((0, 640), (300, 640))]
    g["ѣ"] = [L((110, ASC), (110, XB), (300, XB)) + E(300, 146, 150, 104, -90, 90) + L((110, 250)), L((0, 560), (260, 560))]
    g["ө"] = g["o"] + [L((0, XM), (430, XM))]


def marks():
    """Combining marks, drawn for lowercase (ink top of x-height = 500). Anchors: _top (0,500), _bottom/_ogonek (0,0)."""
    S = lambda pts, w: ("stroke", pts, w)

    def Q(p0, p1, p2, k=18):
        return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in [i / k for i in range(k + 1)]]
    m = {
        0x0300: [S([(45, 560), (-55, 680)], 78)],
        0x0301: [S([(-45, 560), (55, 680)], 78)],
        0x0302: [S([(-115, 555), (0, 665), (115, 555)], 74)],
        0x0303: [S([(-125 + i * 10, 610 + 38 * math.sin(i / 25 * 2 * math.pi)) for i in range(26)], 64)],
        0x0304: [S([(-120, 610), (120, 610)], 70)],
        0x0306: [S(E(0, 625, 105, 70, 200, 340), 68)],
        0x0307: [DOT(0, 605, 50)],
        0x0308: [DOT(-88, 605, 46), DOT(88, 605, 46)],
        0x030A: [S(E(0, 630, 62, 62, 0, 360), 46)],
        0x030B: [S([(-105, 560), (-15, 680)], 64), S([(45, 560), (135, 680)], 64)],
        0x030C: [S([(-115, 665), (0, 555), (115, 665)], 74)],
        0x0326: [DOT(0, -95, 46), S([(0, -95), (-25, -230)], 58)],
        0x0327: [S([(0, 5), (12, -40)] + E(-5, -95, 58, 52, 75, -150), 52)],
        0x0328: [S(Q((0, 30), (-75, -120), (95, -170)), 58)],
        "caroncomb.alt": [DOT(0, 668, 46), S([(0, 668), (-22, 545)], 58)],
        "commaturnedabovecomb": [DOT(0, 590, 46), S([(0, 590), (22, 715)], 58)],
    }
    return m


def icons():
    sw = 64

    def cloud(dy):
        pts = E(215, 320 + dy, 95, 95, -90, -270) + E(395, 400 + dy, 150, 150, 150, 10) + E(595, 320 + dy, 95, 95, 80, -90)
        return pts + [pts[0]]

    sun = [E(400, 400, 150, 150, 0, 360)]
    for k in range(8):
        a = math.radians(k * 45)
        sun.append(L((400 + 235 * math.cos(a), 400 + 235 * math.sin(a)), (400 + 330 * math.cos(a), 400 + 330 * math.sin(a))))
    snow = []
    for k in range(3):
        a = math.radians(90 + k * 60)
        snow.append(L((400 - 300 * math.cos(a), 400 - 300 * math.sin(a)), (400 + 300 * math.cos(a), 400 + 300 * math.sin(a))))
    return sw, {
        0xE000: sun,
        0xE001: [cloud(130)],
        0xE002: [cloud(260)] + [L((240 + i * 150, 270), (190 + i * 150, 110)) for i in range(3)],
        0xE003: snow,
        0xE004: [L((90, 520), (520, 520)) + E(520, 600, 80, 80, -90, 120),
                 L((90, 330), (600, 330)) + E(600, 250, 80, 80, 90, -120)],
    }


ROUND_START = set("CGOQSaceosбєзоосєфюЄЗОСЮ0368906")


def features(cmap, names):
    def cls(chars):
        out = []
        for c in chars:
            n = cmap.get(ord(c))
            if n and n not in out:
                out.append(n)
        return "[" + " ".join(out) + "]"

    digits = [cmap[ord(d)] for d in "0123456789"]
    strong = "TТГҐЃ"
    open_r = "FPРYVWУЎ"
    low = "acdegoqsuаседоєюёсьзфя"
    punct = ".,…"
    fea = ["languagesystem DFLT dflt;", "languagesystem latn dflt;", "languagesystem cyrl dflt;",
           "feature tnum {", "  sub [" + " ".join(digits) + "] by [" + " ".join(d + ".tf" for d in digits) + "];", "} tnum;",
           "feature kern {",
           f"  pos {cls(strong)} {cls(low)} -80;",
           f"  pos {cls(open_r)} {cls(low)} -45;",
           f"  pos {cls(strong + open_r)} {cls(punct)} -90;",
           f"  pos {cls('AАДЛ')} {cls('TТVWYУЎ')} -55;",
           f"  pos {cls('TТVWYУЎ')} {cls('AАДЛ')} -55;",
           f"  pos {cls('LЬЪ')} {cls('TТVWYУ')} -60;",
           f"  pos {cls('rгґ')} {cls(punct)} -45;",
           "} kern;"]
    return "\n".join(fea)


# ------------------------------------------------------------ build
def build(style, out_dir, family="Stykhiia", glyph_hook=None, unicase=False, curves=True):
    STYLE.update(style)
    out = FsPath(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    defs = define()
    order, glyphs, metrics, cmap = [".notdef", "space"], {}, {}, {0x20: "space", 0xA0: "space"}

    nd = TTGlyphPen(None)
    for box in ([(60, 0), (60, 700), (460, 700), (460, 0)], [(130, 70), (390, 70), (390, 630), (130, 630)]):
        nd.moveTo(box[0]); [nd.lineTo(p) for p in box[1:]]; nd.closePath()
    glyphs[".notdef"], metrics[".notdef"] = nd.glyph(), (520, 60)
    glyphs["space"], metrics["space"] = TTGlyphPen(None).glyph(), (260, 0)

    shared = {}

    def add(name, contours, sb, fixed_adv=None):
        xs = [x for c in contours for x, _ in c]
        shift = sb - min(xs)
        adv = fixed_adv or round(max(xs) - min(xs) + 2 * sb)
        if fixed_adv:
            shift = (fixed_adv - (max(xs) - min(xs))) / 2 - min(xs)
        pen = TTGlyphPen(None)
        moved = [[(x + shift, y) for x, y in c] for c in contours]
        if curves:
            import bezfit
            from fontTools.pens.cu2quPen import Cu2QuPen
            big = max(moved, key=lambda c: abs(_area(c)))
            if _area(big) < 0:
                moved = [c[::-1] for c in moved]
            bezfit.draw_fitted(bezfit.fit_all(moved), Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
        else:
            for c in moved:
                c = [(round(x), round(y)) for x, y in c]
                pen.moveTo(c[0]); [pen.lineTo(p) for p in c[1:]]; pen.closePath()
        order.append(name)
        glyphs[name] = pen.glyph()
        metrics[name] = (adv, round(min(xs) + shift))

    for ch, items in defs.items():
        key = id(items)
        if key in shared:                      # Cyrillic letter identical to Latin -> same glyph
            cmap[ord(ch)] = shared[key]
            continue
        name = f"uni{ord(ch):04X}"
        contours = (glyph_hook(ch, items) if glyph_hook else None) or outline(items)
        if ch.isdigit():
            add(name, contours, 44)                       # proportional (default)
            add(name + ".tf", contours, 0, fixed_adv=580)  # tabular, via font-feature tnum
        else:
            sb = 48 if ch in ROUND_START else 62
            if not ch.isalnum():
                sb = 50
            add(name, contours, sb)
        shared[key] = name
        cmap[ord(ch)] = name
    cmap[0x2010] = cmap[ord("-")]
    if unicase:  # decorative font: lowercase shows the decorated capital
        for ch in list(defs):
            up = ch.upper()
            if ch.islower() and len(up) == 1 and up in defs:
                cmap[ord(ch)] = cmap[ord(up)]

    old = dict(STYLE)
    sw, ic = icons()
    STYLE.update({"w": sw, "contrast": 1.0, "flare": 0.0, "caps": "round", "n": 2.0})
    for cp, items in ic.items():
        name = f"uni{cp:04X}"
        add(name, outline(items), 0, fixed_adv=840)
        cmap[cp] = name
    STYLE.update(old)

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=900, descent=-250)
    fb.addOpenTypeFeatures(features(cmap, set(order)))
    fb.setupNameTable({"familyName": family, "styleName": "Regular",
                       "uniqueFontIdentifier": f"{family.replace(' ', '')}-Regular",
                       "fullName": f"{family} Regular", "psName": f"{family.replace(' ', '')}-Regular",
                       "version": "Version 1.0", "copyright": "Stykhiia project"})
    fb.setupOS2(sTypoAscender=900, sTypoDescender=-250, sTypoLineGap=100,
                usWinAscent=1000, usWinDescent=300, sxHeight=500, sCapHeight=700,
                ulUnicodeRange1=(1 << 0) | (1 << 1) | (1 << 9), ulCodePageRange1=(1 << 0) | (1 << 2))
    fb.setupPost()
    ttf = out / f"{family.replace(' ', '')}.ttf"
    fb.save(str(ttf))
    f = TTFont(str(ttf))
    f.flavor = "woff2"
    f.save(str(out / f"{family.replace(' ', '')}.woff2"))
    return ttf


if __name__ == "__main__":
    import json
    import sys
    print(build(json.loads(sys.argv[1]), sys.argv[2]))
