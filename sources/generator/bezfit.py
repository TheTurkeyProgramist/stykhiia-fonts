"""Turn dense polygon contours into clean Bezier outlines.

- corners (sharp turns) are kept as corner points
- smooth runs are split at x/y extrema (horizontal / vertical handles there, as Google Fonts asks)
- each piece becomes a line (if straight) or as few cubic curves as possible (Schneider's algorithm)
Result: list of contours, each = (start_point, [("line", p) | ("curve", c1, c2, p), ...]).
"""
import math

import numpy as np

CORNER_DEG = 30.0
FIT_ERR = 1.6       # max deviation in font units (UPM 1000)
LINE_ERR = 0.9


# ---------------------------------------------------------------- Schneider fitting
def _bez(ctrl, t):
    mt = 1 - t
    return (mt ** 3)[:, None] * ctrl[0] + (3 * mt * mt * t)[:, None] * ctrl[1] + (3 * mt * t * t)[:, None] * ctrl[2] + (t ** 3)[:, None] * ctrl[3]


def _bez1(ctrl, t):
    mt = 1 - t
    return (3 * mt * mt)[:, None] * (ctrl[1] - ctrl[0]) + (6 * mt * t)[:, None] * (ctrl[2] - ctrl[1]) + (3 * t * t)[:, None] * (ctrl[3] - ctrl[2])


def _bez2(ctrl, t):
    mt = 1 - t
    return (6 * mt)[:, None] * (ctrl[2] - 2 * ctrl[1] + ctrl[0]) + (6 * t)[:, None] * (ctrl[3] - 2 * ctrl[2] + ctrl[1])


def _chord_param(pts):
    d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    return d / d[-1] if d[-1] > 0 else d


def _generate(pts, u, t1, t2):
    p0, p3 = pts[0], pts[-1]
    A1 = (3 * (1 - u) ** 2 * u)[:, None] * t1
    A2 = (3 * (1 - u) * u ** 2)[:, None] * t2
    C = np.array([[np.sum(A1 * A1), np.sum(A1 * A2)], [np.sum(A1 * A2), np.sum(A2 * A2)]])
    base = _bez(np.array([p0, p0, p3, p3]), u)
    tmp = pts - base
    X = np.array([np.sum(A1 * tmp), np.sum(A2 * tmp)])
    det = C[0, 0] * C[1, 1] - C[0, 1] * C[1, 0]
    seg = np.linalg.norm(p3 - p0)
    if abs(det) > 1e-12:
        a1 = (X[0] * C[1, 1] - X[1] * C[0, 1]) / det
        a2 = (C[0, 0] * X[1] - C[1, 0] * X[0]) / det
    else:
        a1 = a2 = seg / 3
    eps = 1e-6 * seg
    if a1 < eps or a2 < eps or a1 > seg * 1.6 or a2 > seg * 1.6:
        a1 = a2 = seg / 3
    return np.array([p0, p0 + t1 * a1, p3 + t2 * a2, p3])


def _max_err(pts, ctrl, u):
    d = np.linalg.norm(_bez(ctrl, u) - pts, axis=1)
    i = int(np.argmax(d))
    return d[i], i


def _reparam(pts, ctrl, u):
    p = _bez(ctrl, u) - pts
    d1, d2 = _bez1(ctrl, u), _bez2(ctrl, u)
    num = np.sum(p * d1, axis=1)
    den = np.sum(d1 * d1, axis=1) + np.sum(p * d2, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        nu = np.where(np.abs(den) > 1e-12, u - num / den, u)
    return np.clip(nu, 0, 1)


def _fit(pts, t1, t2, err, depth=0):
    if len(pts) == 2:
        seg = np.linalg.norm(pts[1] - pts[0]) / 3
        return [np.array([pts[0], pts[0] + t1 * seg, pts[1] + t2 * seg, pts[1]])]
    u = _chord_param(pts)
    ctrl = _generate(pts, u, t1, t2)
    e, split = _max_err(pts, ctrl, u)
    if e < err:
        return [ctrl]
    if e < err * 6:
        for _ in range(25):
            u = _reparam(pts, ctrl, u)
            ctrl = _generate(pts, u, t1, t2)
            e, split = _max_err(pts, ctrl, u)
            if e < err:
                return [ctrl]
    if depth > 12 or len(pts) < 4:
        return [ctrl]
    split = min(max(split, 1), len(pts) - 2)
    tc = pts[split - 1] - pts[split + 1]
    n = np.linalg.norm(tc)
    tc = tc / n if n > 0 else t2
    return _fit(pts[:split + 1], t1, tc, err, depth + 1) + _fit(pts[split:], -tc, t2, err, depth + 1)


# ---------------------------------------------------------------- contour analysis
def _clean(c):
    out = []
    for p in c:
        if not out or math.dist(p, out[-1]) > 0.35:
            out.append((float(p[0]), float(p[1])))
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 0.35:
        out.pop()
    return out


def _despike(pts):
    """drop needle-like vertices (outline goes out and straight back) left by boolean unions"""
    changed = True
    while changed and len(pts) > 3:
        changed = False
        n = len(pts)
        for i in range(n):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            if _turn(a, b, c) > 163 and min(math.dist(a, b), math.dist(b, c)) < 45:
                pts = pts[:i] + pts[i + 1:]
                changed = True
                break
    return pts


def _turn(a, b, c):
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    cs = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cs))


def _tangent_out(run, k=1):
    """unit direction leaving run[0], robust to a tiny first edge"""
    p0 = np.array(run[0])
    for q in run[1:]:
        v = np.array(q) - p0
        if np.linalg.norm(v) > 6 or q is run[-1]:
            return v / (np.linalg.norm(v) or 1)
    v = np.array(run[1]) - p0
    return v / (np.linalg.norm(v) or 1)


def _is_line(run):
    a, b = np.array(run[0]), np.array(run[-1])
    ab = b - a
    L = np.linalg.norm(ab)
    if L == 0:
        return True
    pts = np.array(run)
    r = pts - a
    d = np.abs(ab[0] * r[:, 1] - ab[1] * r[:, 0]) / L
    return d.max() < LINE_ERR


def _densify(run, step=7.0):
    out = [run[0]]
    for p, q in zip(run, run[1:]):
        k = max(1, int(math.dist(p, q) / step))
        for j in range(1, k + 1):
            out.append((p[0] + (q[0] - p[0]) * j / k, p[1] + (q[1] - p[1]) * j / k))
    return out


def _extrema_idx(run):
    """indices (inside the run) of local x/y extrema, with the axis of the handle there"""
    out = []
    n = len(run)
    for axis in (0, 1):
        v = [p[axis] for p in run]
        i = 1
        while i < n - 1:
            # plateau-aware: find j with v[j] != v[i]
            j = i
            while j < n - 1 and abs(v[j + 1] - v[i]) < 1e-6:
                j += 1
            if j >= n - 1:
                break
            prev, nxt = v[i] - v[i - 1], v[j + 1] - v[j]
            if prev * nxt < 0:
                k = (i + j) // 2
                out.append((k, axis))
            i = j + 1
    return out


def _postprocess(start, segs):
    """snap almost-vertical / almost-horizontal lines, merge colinear lines"""
    start = tuple(start)
    nodes = [list(sg) for sg in segs]
    prev = start
    for k, sg in enumerate(nodes):
        end = sg[-1]
        if sg[0] == "line" and k < len(nodes) - 1:
            dx, dy = end[0] - prev[0], end[1] - prev[1]
            new = end
            if 0 < abs(dx) <= 2.5 and abs(dy) > 15:
                new = (prev[0], end[1])
            elif 0 < abs(dy) <= 2.5 and abs(dx) > 15:
                new = (end[0], prev[1])
            if new != end:
                sg[-1] = new
                nxt = nodes[k + 1]
                if nxt[0] == "curve":   # keep the next curve's first handle attached
                    nxt[1] = (nxt[1][0] + new[0] - end[0], nxt[1][1] + new[1] - end[1])
                end = new
        prev = tuple(end)
    out, prev = [], start
    for sg in nodes:
        if sg[0] == "line" and out and out[-1][0] == "line":
            p0 = out_prev
            a = (out[-1][1][0] - p0[0], out[-1][1][1] - p0[1])
            b = (sg[1][0] - out[-1][1][0], sg[1][1] - out[-1][1][1])
            na, nb = math.hypot(*a), math.hypot(*b)
            if na > 0 and nb > 0 and abs(a[0] * b[1] - a[1] * b[0]) / (na * nb) < 0.03 and (a[0] * b[0] + a[1] * b[1]) > 0:
                out[-1] = ("line", sg[1])
                continue
        out_prev = prev if not out or out[-1][0] != "line" else out_prev
        if sg[0] == "line":
            out_prev = prev
        out.append(tuple(sg))
        prev = sg[-1]
    return start, out


def fit_contour(poly):
    pts = _despike(_clean(poly))
    n = len(pts)
    if n < 3:
        return None
    turns = [_turn(pts[i - 1], pts[i], pts[(i + 1) % n]) for i in range(n)]
    elen = [math.dist(pts[i], pts[(i + 1) % n]) for i in range(n)]
    corners = [i for i in range(n)
               if turns[i] > CORNER_DEG or (turns[i] > 11 and min(elen[i - 1], elen[i]) > 24)]
    if not corners:
        # smooth closed contour: start at the leftmost point (an extremum)
        k = min(range(n), key=lambda i: (pts[i][0], pts[i][1]))
        corners_virtual = True
    else:
        k = corners[0]
        corners_virtual = False
    pts = pts[k:] + pts[:k]
    corner_set = set(((c - k) % n) for c in corners)
    if corners_virtual:
        corner_set = {0}
    cuts = sorted(corner_set) + [n]
    ring = pts + [pts[0]]
    segments = []
    for a, b in zip(cuts, cuts[1:]):
        run = _densify(ring[a:b + 1])
        # split smooth run at extrema
        ex = [(i, ax) for i, ax in _extrema_idx(run) if 3 <= i <= len(run) - 4
              and math.dist(run[i], run[0]) > 10 and math.dist(run[i], run[-1]) > 10]
        ex.sort()
        # drop extrema too close to each other (keep first)
        filt = []
        for i, ax in ex:
            if not filt or math.dist(run[i], run[filt[-1][0]]) > 18:
                filt.append((i, ax))
        bounds = [(0, None)] + filt + [(len(run) - 1, None)]
        for (i0, ax0), (i1, ax1) in zip(bounds, bounds[1:]):
            sub = run[i0:i1 + 1]
            if len(sub) < 2:
                continue
            if len(sub) == 2 or _is_line(sub):
                segments.append(("line", sub[-1]))
                continue
            arr = np.array(sub)
            t1 = _tangent_out(sub)
            t2 = _tangent_out(sub[::-1])
            if ax0 is not None:   # extremum: handle exactly horizontal (y-extremum) or vertical (x-extremum)
                t1 = np.array([0.0, math.copysign(1, t1[1])]) if ax0 == 0 else np.array([math.copysign(1, t1[0]), 0.0])
            if ax1 is not None:
                t2 = np.array([0.0, math.copysign(1, t2[1])]) if ax1 == 0 else np.array([math.copysign(1, t2[0]), 0.0])
            for ctrl in _fit(arr, t1, t2, FIT_ERR):
                c1, c2, p3 = (tuple(ctrl[1]), tuple(ctrl[2]), tuple(ctrl[3]))
                segments.append(("curve", c1, c2, p3))
    return _postprocess(pts[0], segments)


def _r(p):
    return (round(p[0]), round(p[1]))


def draw_fitted(fitted, pen):
    """draw contours to a fontTools pen (cubic), rounding to integers"""
    for start, segs in fitted:
        pen.moveTo(_r(start))
        last = _r(start)
        for s in segs:
            if s[0] == "line":
                p = _r(s[1])
                if p != last:
                    pen.lineTo(p)
                    last = p
            else:
                pen.curveTo(_r(s[1]), _r(s[2]), _r(s[3]))
                last = _r(s[3])
        pen.closePath()


def fit_all(contours):
    out = []
    for c in contours:
        f = fit_contour(c)
        if f and f[1]:
            out.append(f)
    return out


def point_count(fitted):
    return sum(1 + sum(1 if s[0] == "line" else 3 for s in segs) for _, segs in fitted)
