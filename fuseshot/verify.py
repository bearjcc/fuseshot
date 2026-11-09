"""Check candidate sets by matching features between Immich previews.

Two stages. `measure` downloads previews (cached as small grey JPEGs), matches
neighbouring and likely-overlapping frames and stores raw pair geometry per set in
cache/verify/. It can be stopped and restarted. `classify` reads those files and
decides the motion model, the verified type, coverage and sub-sets. It is fast, so
thresholds can be changed without matching again.
"""
import hashlib
import heapq
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from . import lens, limits, log
from .progress import Progress

LOG = log.get(__name__)

MEASURE_VERSION = 1
SIZE = 1024
FEATURES = 1500
WINDOW = 4
ALL_PAIRS_UP_TO = 30
COARSE_FEATURES = 300
COARSE_NEIGHBOURS = 8
MAX_STORED_POINTS = 150
HFOV_GRID = list(range(25, 105, 5))
ROT_THRESHOLD = 6
ZOOM_RATIO = 1.4
SAME_SHIFT = 0.05
MIN_MATCH_AREA = 0.08
DEFAULT_HFOV = 60

SINGLE_ROW = "single-row rotation"
MULTI_ROW = "multi-row grid"
SPHERE = "sphere/360"
LAYERED = "multi-zoom layered"
TRANSLATION = "linear walk/translation"
OBJECT_SCAN = "object scan/walk-around"
MIXED = "mixed motion"
BRACKET = "bracket"
NOT_PANORAMA = "not a panorama"
PANORAMA_TYPES = (SINGLE_ROW, MULTI_ROW, SPHERE, LAYERED, TRANSLATION, OBJECT_SCAN, MIXED)

STITCH_PLAN = {
    "rotation": ["rotation", "mosaic"],
    "planar": ["mosaic", "rotation"],
    "parallax": ["mosaic", "sfm"],
    "mixed": ["sfm"],
}


def set_key(asset_ids):
    digest = hashlib.sha1(" ".join(asset_ids).encode()).hexdigest()[:16]
    return f"{digest}_v{MEASURE_VERSION}"


def thumb_path(cache_dir, asset_id):
    return Path(cache_dir) / "thumbs" / asset_id[:2] / f"{asset_id}.jpg"


def load_thumb(immich, cache_dir, asset_id):
    path = thumb_path(cache_dir, asset_id)
    if path.exists():
        return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    try:
        data = immich.thumbnail(asset_id, "preview")
    except Exception as err:
        LOG.warning(f"  left out of its set: {err}")
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    scale = SIZE / max(img.shape)
    if scale < 1:
        img = cv2.resize(img, (round(img.shape[1] * scale), round(img.shape[0] * scale)),
                         interpolation=cv2.INTER_AREA)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.jpg")
    cv2.imwrite(str(tmp), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    os.replace(tmp, path)
    return img


def candidate_pairs(descriptors, responses):
    n = len(descriptors)
    valid = [d is not None and len(d) >= 8 for d in descriptors]
    if n <= ALL_PAIRS_UP_TO:
        return [(i, j) for i in range(n) for j in range(i + 1, n) if valid[i] and valid[j]]
    pairs = {(i, j) for i in range(n) for j in range(i + 1, min(n, i + WINDOW + 1))
             if valid[i] and valid[j]}
    coarse = []
    for d, r in zip(descriptors, responses):
        if d is None or len(d) < 8:
            coarse.append(None)
            continue
        coarse.append(d[np.argsort(-r)[:COARSE_FEATURES]])
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    scores = np.zeros((n, n), np.int32)
    for i in range(n):
        for j in range(i + 1, n):
            if coarse[i] is None or coarse[j] is None:
                continue
            knn = matcher.knnMatch(coarse[i], coarse[j], k=2)
            good = sum(1 for m in knn if len(m) == 2 and m[0].distance < 0.75 * m[1].distance)
            scores[i, j] = scores[j, i] = good
    for i in range(n):
        for j in np.argsort(-scores[i])[:COARSE_NEIGHBOURS]:
            if j != i and scores[i, j] >= 8:
                pairs.add((min(i, j), max(i, j)))
    return sorted(pairs)


def match_pair(matcher, kp1, d1, kp2, d2, rng):
    knn = matcher.knnMatch(d1, d2, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < 0.8 * m[1].distance]
    result = {"matches": len(good), "h_inl": 0, "f_inl": 0, "H": None, "pts": None, "pts_h": None}
    if len(good) < 12:
        return result
    p1 = np.float32([kp1[m.queryIdx].pt for m in good])
    p2 = np.float32([kp2[m.trainIdx].pt for m in good])
    H, hmask = cv2.findHomography(p1, p2, cv2.RANSAC, 4.0, maxIters=2000, confidence=0.995)
    hmask = hmask.ravel().astype(bool) if hmask is not None else np.zeros(len(good), bool)
    F, fmask = cv2.findFundamentalMat(p1, p2, cv2.FM_RANSAC, 1.5, 0.995)
    if F is None or fmask is None or F.shape != (3, 3):
        fmask = np.zeros(len(good), bool)
    else:
        fmask = fmask.ravel().astype(bool)
    keep = fmask | hmask
    idx = np.flatnonzero(keep)
    if len(idx) > MAX_STORED_POINTS:
        idx = np.sort(rng.choice(idx, MAX_STORED_POINTS, replace=False))
    result.update(h_inl=int(hmask.sum()), f_inl=int(fmask.sum()),
                  H=None if H is None else H.astype(np.float32).ravel(),
                  pts=np.hstack([p1[idx], p2[idx]]), pts_h=hmask[idx])
    return result


def measure_set(immich, cache_dir, asset_ids):
    t0 = time.time()
    with ThreadPoolExecutor(limits.workers()) as pool:
        images = list(pool.map(lambda a: load_thumb(immich, cache_dir, a), asset_ids))
    t_download = time.time() - t0
    sift = cv2.SIFT_create(FEATURES)
    keypoints, descriptors, responses, sizes = [], [], [], []
    for img in images:
        if img is None:
            keypoints.append([]); descriptors.append(None); responses.append(None); sizes.append((0, 0))
            continue
        kp, d = sift.detectAndCompute(img, None)
        keypoints.append(kp)
        descriptors.append(d)
        responses.append(np.float32([k.response for k in kp]))
        sizes.append((img.shape[1], img.shape[0]))
    del images
    pairs = candidate_pairs(descriptors, responses)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    rng = np.random.default_rng(0)
    stats, homs, offsets, points, points_h = [], [], [0], [], []
    for i, j in pairs:
        r = match_pair(matcher, keypoints[i], descriptors[i], keypoints[j], descriptors[j], rng)
        stats.append((r["matches"], r["h_inl"], r["f_inl"]))
        homs.append(r["H"] if r["H"] is not None else np.full(9, np.nan, np.float32))
        if r["pts"] is not None:
            points.append(r["pts"]); points_h.append(r["pts_h"])
            offsets.append(offsets[-1] + len(r["pts"]))
        else:
            offsets.append(offsets[-1])
    return dict(
        sizes=np.int32(sizes).reshape(-1, 2),
        keypoints=np.int32([len(k) for k in keypoints]),
        pairs=np.int32(pairs).reshape(-1, 2),
        stats=np.int32(stats).reshape(-1, 3),
        H=np.float32(homs).reshape(-1, 9),
        offsets=np.int32(offsets),
        pts=np.vstack(points).astype(np.float32) if points else np.zeros((0, 4), np.float32),
        pts_h=np.concatenate(points_h) if points_h else np.zeros(0, bool),
        timing=np.float32([t_download, time.time() - t0]),
    )


def measure_all(immich, candidates, cache_dir, stop_at=None, log=print):
    import psutil

    cv2.setNumThreads(2)
    out_dir = Path(cache_dir) / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = psutil.Process()
    peak = 0
    done = skipped = 0
    t0 = time.time()
    todo = [c for c in candidates if not (out_dir / f"{set_key(c['asset_ids'])}.npz").exists()]
    log(f"{len(candidates) - len(todo)} sets already measured, {len(todo)} to go")
    with Progress("checking overlap", len(todo), unit="sets") as bar:
        for n, c in enumerate(todo, 1):
            if stop_at and time.strftime("%H:%M") >= stop_at:
                log(f"Stopping at {stop_at} as asked; run again to continue")
                break
            t = time.time()
            data = measure_set(immich, cache_dir, c["asset_ids"])
            path = out_dir / f"{set_key(c['asset_ids'])}.npz"
            tmp = path.with_name(path.stem + ".tmp.npz")
            np.savez_compressed(tmp, **data)
            os.replace(tmp, path)
            rss = proc.memory_info().rss / 1048576
            peak = max(peak, rss)
            done += 1
            bar.update()
            LOG.debug(f"[{n}/{len(todo)}] {c['set_id']} {len(c['asset_ids'])} frames {len(data['pairs'])} pairs "
                      f"{time.time() - t:.1f}s rss {rss:.0f} MB elapsed {(time.time() - t0) / 60:.1f} min")
    return {"measured": done, "skipped": skipped, "seconds": round(time.time() - t0, 1),
            "peak_rss_mb": round(peak)}


def rays(pts, size, focal):
    w, h = size
    x = (pts[:, 0] - w / 2) / focal
    y = (pts[:, 1] - h / 2) / focal
    v = np.stack([x, y, np.ones_like(x)], 1)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def fit_rotation(a, b):
    """Rotation R with b ~ R a for unit vectors."""
    u, _, vt = np.linalg.svd(b.T @ a)
    d = np.sign(np.linalg.det(u @ vt))
    return u @ np.diag([1, 1, d]) @ vt


def rotation_check(p1, p2, size1, size2, f1, f2, threshold=4.0):
    """Fit a pure camera rotation; return (fraction of points within threshold px, median error, R)."""
    a, b = rays(p1, size1, f1), rays(p2, size2, f2)
    keep = np.ones(len(a), bool)
    R = np.eye(3)
    err = np.full(len(a), np.inf)
    for _ in range(3):
        if keep.sum() < 6:
            break
        R = fit_rotation(a[keep], b[keep])
        q = a @ R.T
        with np.errstate(divide="ignore", invalid="ignore"):
            proj = q[:, :2] / q[:, 2:3] * f2
        target = p2 - np.float32(size2) / 2
        err = np.where(q[:, 2] > 0.05, np.linalg.norm(proj - target, axis=1), np.inf)
        keep = err < max(threshold, 2 * np.median(err[np.isfinite(err)]) if np.isfinite(err).any() else threshold)
    frac = float(np.mean(err < threshold)) if len(err) else 0.0
    med = float(np.median(err)) if len(err) else float("inf")
    return frac, med, R


def homography_shape(H, size1, pts):
    """Return (mean corner shift as a share of the long side, local scale, H error per point)."""
    if H is None or not np.all(np.isfinite(H)) or abs(H[8]) < 1e-9:
        return 1.0, 1.0, np.full(len(pts), np.inf)
    H = H.reshape(3, 3).astype(np.float64) / H[8]
    w, h = size1
    corners = np.float64([[0, 0], [w, 0], [w, h], [0, h]])
    moved = cv2.perspectiveTransform(corners[None], H)[0]
    shift = float(np.mean(np.linalg.norm(moved - corners, axis=1)) / max(w, h))
    mapped = cv2.perspectiveTransform(pts[:, :2].astype(np.float64)[None], H)[0]
    err = np.linalg.norm(mapped - pts[:, 2:], axis=1)
    c1, c2 = np.cov(pts[:, :2].T), np.cov(pts[:, 2:].T)
    d1, d2 = np.linalg.det(c1), np.linalg.det(c2)
    scale = (d2 / d1) ** 0.25 if d1 > 1e-6 and d2 > 1e-6 else 1.0
    return shift, float(scale), err


def match_area(pts, size1, size2):
    """(smaller, larger) convex-hull area of the matched points, as shares of each frame."""
    if len(pts) < 3:
        return 0.0, 0.0
    out = []
    for cols, (w, h) in ((slice(0, 2), size1), (slice(2, 4), size2)):
        hull = cv2.convexHull(pts[:, cols].astype(np.float32))
        out.append(cv2.contourArea(hull) / max(w * h, 1))
    return float(min(out)), float(max(out))


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def ev_of(row):
    try:
        n, t, iso = row["f_number"], row["exposure_time"], row["iso"] or 100
        return math.log2(n * n / t) - math.log2(iso / 100)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def circular_span(angles, fov):
    if not angles:
        return 0.0
    a = sorted(x % 360 for x in angles)
    gaps = [b - c for c, b in zip(a, a[1:])] + [a[0] + 360 - a[-1]]
    largest = max(gaps)
    if largest <= fov:
        return 360.0
    return min(360.0, 360 - largest + fov)


def orientations(frames, edges, rotations):
    """Chain pairwise rotations along a maximum spanning tree. Returns {frame: R} for reachable frames."""
    if not frames:
        return {}
    adj = {f: [] for f in frames}
    for (i, j), w in edges.items():
        if i in adj and j in adj:
            adj[i].append((w, j, i))
            adj[j].append((w, i, j))
    start = frames[0]
    result = {start: np.eye(3)}
    heap = [(-w, a, b) for w, a, b in adj[start]]
    heapq.heapify(heap)
    while heap:
        _, to, frm = heapq.heappop(heap)
        if to in result:
            continue
        R = rotations[(frm, to)] if (frm, to) in rotations else rotations[(to, frm)].T
        result[to] = R @ result[frm]
        for w, nxt, cur in adj[to]:
            if nxt not in result:
                heapq.heappush(heap, (-w, nxt, cur))
    return result


def classify_set(candidate, data, meta, crops=None, focal35=None):
    """meta: list of asset rows (dict-like) in candidate order, or None per missing row.

    crops: per-frame crop factors (None where unknown) for an EXIF field of view.
    focal35: per-frame 35 mm focal length from the file (0 where missing); it includes digital zoom.
    """
    n = len(candidate["asset_ids"])
    sizes = [tuple(s) for s in data["sizes"]]
    present = [s[0] > 0 for s in sizes]
    focals_mm = [m["focal_length"] if m and m["focal_length"] else None for m in meta]
    equivalent = [focal35[k] if focal35 and focal35[k]
                  else focals_mm[k] * crops[k] if focals_mm[k] and crops and crops[k] else None
                  for k in range(n)]
    if all(equivalent[k] for k in range(n) if present[k]) and any(equivalent):
        focals_mm, crops = equivalent, [1.0] * n
    known = [f for f in focals_mm if f]
    ref_mm = min(known) if known else None
    rel = [f / ref_mm if f and ref_mm else 1.0 for f in focals_mm]
    evs = [ev_of(m) if m else None for m in meta]

    pairs = []
    for k, (i, j) in enumerate(data["pairs"]):
        matches, h_inl, f_inl = (int(x) for x in data["stats"][k])
        lo, hi = data["offsets"][k], data["offsets"][k + 1]
        pairs.append({"i": int(i), "j": int(j), "matches": matches, "h": h_inl, "f": f_inl,
                      "H": data["H"][k], "pts": data["pts"][lo:hi], "pts_h": data["pts_h"][lo:hi]})

    overlapping, subject_pairs = [], []
    for p in pairs:
        m = max(p["matches"], 1)
        if not ((p["h"] >= 15 and p["h"] >= 0.15 * m) or (p["f"] >= 25 and p["f"] >= 0.3 * m)):
            continue
        if len(p["pts"]) < 8:
            continue
        shift, scale, herr = homography_shape(p["H"], sizes[p["i"]], p["pts"])
        p["shift"], p["scale"], p["h_frac"] = shift, scale, float(np.mean(herr < ROT_THRESHOLD))
        inl = herr[p["pts_h"]] if p["pts_h"].any() else herr
        p["h_med"] = float(np.median(inl)) if np.isfinite(inl).any() else 99.0
        exif_ratio = rel[p["j"]] / rel[p["i"]]
        p["zoom"] = max(scale, 1 / scale) > ZOOM_RATIO or max(exif_ratio, 1 / exif_ratio) > ZOOM_RATIO
        p["area"] = match_area(p["pts"], sizes[p["i"]], sizes[p["j"]])
        if p["area"][1] >= MIN_MATCH_AREA and p["area"][0] >= MIN_MATCH_AREA / 4:
            overlapping.append(p)
        else:
            subject_pairs.append(p)

    def focal_px(idx, hfov):
        w, h = sizes[idx]
        return max(w, h) / 2 / math.tan(math.radians(hfov) / 2) * rel[idx]

    def rot_eval(p, hfov):
        f1 = focal_px(p["i"], hfov)
        if not p["zoom"]:
            ratios = [rel[p["j"]] / rel[p["i"]]]
        else:
            base = {round(rel[p["j"]] / rel[p["i"]], 3), round(p["scale"], 3)}
            ratios = [b * (1 + k / 100) for b in base for k in range(-6, 7, 2)]
        best = None
        for r in ratios:
            threshold = ROT_THRESHOLD * max(1.0, r)
            got = rotation_check(p["pts"][:, :2], p["pts"][:, 2:], sizes[p["i"]], sizes[p["j"]], f1, f1 * r,
                                 threshold)
            if best is None or (got[0], -got[1]) > (best[0], -best[1]):
                best = got
        return best

    def fits_rotation(p, frac, med=None):
        if frac < 0.5:
            return False
        return frac >= 0.8 * p["h_frac"] or (med is not None and med <= max(3.0, 2.0 * p["h_med"]))

    moving = [p for p in overlapping if p["shift"] >= SAME_SHIFT]
    hfov, focal_source = DEFAULT_HFOV, "default"
    ref = [k for k in range(n) if focals_mm[k] and focals_mm[k] == ref_mm and crops and crops[k]]
    grid = HFOV_GRID
    if ref:
        f35 = ref_mm * crops[ref[0]]
        hfov, focal_source = round(math.degrees(2 * math.atan(18.0 / f35)), 1), "exif"
        grid = [round(hfov * (1 + k / 100), 1) for k in range(-20, 21, 5)]
    if moving:
        scores = []
        for g in grid:
            s, resid, fitted = 0.0, 0.0, 0
            for p in moving:
                frac, med, _ = rot_eval(p, g)
                if not fits_rotation(p, frac, med):
                    continue
                s += p["h"]
                resid += min(med, 20)
                fitted += 1
            scores.append((s, -resid / max(fitted, 1), g))
        best = max(scores)
        if best[0] > 0:
            near = [x for x in scores if x[0] >= 0.98 * best[0]]
            hfov = max(near, key=lambda x: x[1])[2]
            focal_source = "exif-refined" if ref else "estimated"

    rotations = {}
    for p in overlapping:
        if p["shift"] < SAME_SHIFT:
            p["kind"] = "same"
            continue
        frac, med, R = rot_eval(p, hfov)
        p["rot_frac"], p["rot_err"] = round(frac, 2), round(med, 2)
        h_share = p["h"] / max(p["f"], 1)
        if fits_rotation(p, frac, med):
            p["kind"] = "zoom" if p["zoom"] else "rotation"
            rotations[(p["i"], p["j"])] = R
        elif h_share >= 0.7 or p["f"] < 30:
            p["kind"] = "planar"
        else:
            p["kind"] = "parallax"

    same = UnionFind(n)
    for p in overlapping:
        if p["kind"] == "same":
            same.union(p["i"], p["j"])
    groups = UnionFind(n)
    for p in overlapping:
        groups.union(p["i"], p["j"])
    comps = {}
    for i in range(n):
        if present[i]:
            comps.setdefault(groups.find(i), []).append(i)

    sub_sets = []
    junk = []
    for frames in sorted(comps.values(), key=lambda f: (-len(f), f[0])):
        positions = len({same.find(i) for i in frames})
        fs = set(frames)
        cp = [p for p in overlapping if p["i"] in fs]
        if len(frames) < 3 and not (len(frames) == 2 and positions == 2 and n <= 3):
            junk.extend(frames)
            continue
        sub_sets.append(describe_subset(candidate, frames, positions, cp, rotations, sizes, rel, hfov, evs, same))

    real = [s for s in sub_sets if s["type"] in PANORAMA_TYPES]
    main = max(real or sub_sets or [None], key=lambda s: (s["quality"], s["frames_count"]) if s else 0)
    seq_total = sum(1 for i in range(n - 1) if present[i] and present[i + 1])
    seq_overlap = sum(1 for p in overlapping if p["j"] == p["i"] + 1)
    kinds = {}
    for p in overlapping:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    result = {
        "set_id": candidate["set_id"],
        "frames": n,
        "missing_previews": n - sum(present),
        "pairs_tested": len(pairs),
        "pairs_overlapping": len(overlapping),
        "sequential_overlap": f"{seq_overlap}/{seq_total}",
        "pair_kinds": kinds,
        "subject_only_pairs": len(subject_pairs),
        "hfov_estimate": hfov,
        "hfov_source": focal_source,
        "sub_sets": sub_sets,
        "junk_frames": [i + 1 for i in sorted(junk)],
        "splat_candidate": any(s["splat_candidate"] for s in sub_sets),
    }
    if main is None:
        confidence = round(min(0.95, 0.5 + 0.1 * len(pairs) ** 0.5), 2)
        result.update(verified_type=NOT_PANORAMA, verified_confidence=confidence, motion_model="none",
                      stitch_plan=[], coverage_h=None, coverage_v=None, rows=None, quality=0.0,
                      reason="no group of three or more overlapping frames")
        return result
    result.update(verified_type=main["type"], verified_confidence=main["confidence"],
                  motion_model=main["motion_model"], stitch_plan=main["stitch_plan"],
                  coverage_h=main["coverage_h"], coverage_v=main["coverage_v"], rows=main["rows"],
                  quality=main["quality"], reason=main["reason"])
    if len(real) > 1:
        result["verified_type"] = f"{main['type']} (+{len(real) - 1} more)"
    return result


def describe_subset(candidate, frames, positions, cp, rotations, sizes, rel, hfov, evs, same):
    moving = [p for p in cp if p["kind"] != "same"]
    count = {k: sum(1 for p in moving if p["kind"] == k) for k in ("rotation", "zoom", "planar", "parallax")}
    rot = count["rotation"] + count["zoom"]
    trans = count["planar"] + count["parallax"]
    total = max(rot + trans, 1)
    sub = {"frames": [i + 1 for i in frames], "frames_count": len(frames), "positions": positions,
           "pair_kinds": count, "coverage_h": None, "coverage_v": None, "rows": None, "splat_candidate": False}
    frame_evs = [evs[i] for i in frames if evs[i] is not None]
    ev_range = max(frame_evs) - min(frame_evs) if frame_evs else 0.0
    bracketed = positions < len(frames) and ev_range >= 0.9
    strength = min(1.0, float(np.median([max(p["h"], p["f"]) for p in moving])) / 60) if moving else 0.0
    order = sorted(frames)
    seq = sum(1 for a, b in zip(order, order[1:])
              if any(p["i"] == a and p["j"] == b for p in cp)) / max(len(order) - 1, 1)
    degree = 2 * len(moving) / max(positions, 1)
    nonseq = sum(1 for p in moving if p["j"] - p["i"] > 2)

    if positions <= 1:
        kind = BRACKET if ev_range >= 0.9 else NOT_PANORAMA
        sub.update(type=kind, motion_model="none", stitch_plan=[],
                   reason="one view, exposures differ" if kind == BRACKET else "repeats of one view")
        sub["confidence"] = round(float(0.5 + 0.4 * strength), 2)
        sub["quality"] = 0.0
        return sub

    parallax_share = count["parallax"] / total
    if parallax_share < 0.25 or count["parallax"] < 3:
        motion = "rotation" if rot >= count["planar"] else "planar"
    elif rot / total >= 0.3:
        motion = "mixed"
    else:
        motion = "parallax"
    purity = {"rotation": rot, "planar": count["planar"] + rot, "parallax": trans,
              "mixed": max(rot, trans)}[motion] / total
    if motion == "rotation":
        R = orientations(frames, {(p["i"], p["j"]): p["h"] for p in moving if (p["i"], p["j"]) in rotations},
                         rotations)
        yaws, pitches = [], []
        for Ri in R.values():
            d = Ri.T @ np.array([0.0, 0.0, 1.0])
            yaws.append(math.degrees(math.atan2(d[0], d[2])))
            pitches.append(math.degrees(math.atan2(-d[1], math.hypot(d[0], d[2]))))
        w, h = sizes[frames[0]]
        base_h = hfov if w >= h else math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * w / h))
        base_v = math.degrees(2 * math.atan(math.tan(math.radians(base_h) / 2) * h / w))
        cov_h = circular_span(yaws, base_h)
        cov_v = min(180.0, max(pitches) - min(pitches) + base_v) if pitches else base_v
        ps = sorted(pitches)
        rows = 1 + sum(1 for a, b in zip(ps, ps[1:]) if b - a > 0.5 * base_v)
        sub.update(coverage_h=round(cov_h), coverage_v=round(cov_v), rows=rows, oriented=f"{len(R)}/{len(frames)}")
        zoom_frames = {p["j"] if p["scale"] > 1 else p["i"] for p in moving if p["kind"] == "zoom"}
        zoomed_share = len(zoom_frames) / len(frames)
        if count["zoom"] >= 2 and len(zoom_frames) >= 2 and zoomed_share <= 0.7:
            kind = LAYERED
        elif cov_h >= 330 and cov_v >= 120:
            kind = SPHERE
        elif rows >= 2:
            kind = MULTI_ROW
        else:
            kind = SINGLE_ROW
        area = cov_h * cov_v / max(base_h * base_v, 1)
        if area < 1.3 and kind != LAYERED:
            kind = NOT_PANORAMA
            reason = "frames overlap almost completely, little extra coverage"
        else:
            reason = f"{rot} of {total} moving pairs fit a pure rotation"
        cover_factor = min(1.0, 0.4 + 0.6 * min(1.0, (area - 1) / 4))
        errs = [p["rot_err"] for p in moving if p["kind"] in ("rotation", "zoom")]
        fit = 1.0 if not errs or np.median(errs) <= 2 else max(0.3, 1 - (np.median(errs) - 2) / 6)
    elif motion in ("planar", "parallax") and seq < 0.5:
        kind = NOT_PANORAMA
        reason = "matches do not follow the shooting order (moving subject or repeated pattern)"
        cover_factor, fit = 0.0, 0.5
    elif motion in ("planar", "parallax"):
        gps = candidate.get("gps_max_disp_m") or 0
        orbit = positions >= 8 and (nonseq >= 0.5 * positions or degree >= 3.5) and gps < 50
        kind = OBJECT_SCAN if (orbit and motion == "parallax") or (orbit and degree >= 5) else TRANSLATION
        reason = f"{trans} of {total} moving pairs need camera movement ({count['parallax']} with parallax)"
        gained = sum(min(p["shift"], 1.0) for p in moving if p["j"] - p["i"] == 1)
        sub["extent_frames"] = round(1 + float(gained), 2)
        rolls = [abs(math.degrees(math.atan2(p["H"][3] - p["H"][1], p["H"][0] + p["H"][4]))) for p in moving]
        landscape = {sizes[i][0] >= sizes[i][1] for i in frames}
        if kind == TRANSLATION and gained < 0.35:
            kind = NOT_PANORAMA
            reason = "frames overlap almost completely, little extra coverage"
        elif kind == TRANSLATION and (candidate.get("people_share") or 0) >= 0.6 and gained < 0.5:
            kind = NOT_PANORAMA
            reason = "people are the subject and the camera follows them"
        elif kind in (TRANSLATION, OBJECT_SCAN) and len(landscape) > 1:
            kind = NOT_PANORAMA
            reason = "camera turned between portrait and landscape, snapshots of one subject"
        elif kind == TRANSLATION and rolls and float(np.median(rolls)) > 12:
            kind = NOT_PANORAMA
            reason = "frames turn in the image plane, handheld shots of a flat subject"
        if kind == TRANSLATION:
            cover_factor = 0.75 * min(1.0, 0.3 + 0.2 * gained)
        else:
            cover_factor = min(1.0, 0.4 + 0.1 * positions)
        strong = sum(1 for p in moving if p["kind"] in ("planar", "parallax") and max(p["h"], p["f"]) >= 40)
        fit = min(1.0, 0.5 + 0.1 * strong)
    else:
        kind = MIXED
        reason = f"{rot} rotation and {trans} moving-camera pairs on the same subject"
        if (candidate.get("people_share") or 0) >= 0.6 and degree < 4:
            kind = NOT_PANORAMA
            reason = "people are the subject and the camera follows them"
        cover_factor = min(1.0, 0.4 + 0.1 * positions)
        fit = 1.0

    if bracketed and kind in (SINGLE_ROW, MULTI_ROW, SPHERE, LAYERED):
        sub["bracketed"] = True
    frame_share = len(frames) / max(len(candidate["asset_ids"]), 1)
    conf = (0.35 * purity + 0.25 * strength + 0.25 * seq + 0.15 * frame_share) * fit
    sub.update(type=kind, motion_model=motion, stitch_plan=STITCH_PLAN[motion], reason=reason,
               confidence=round(float(min(conf, 0.99)), 2))
    sub["splat_candidate"] = kind in (OBJECT_SCAN, MIXED) and positions >= 12 and degree >= 3
    sub["quality"] = round(float(sub["confidence"] * cover_factor), 3) if kind in PANORAMA_TYPES else 0.0
    return sub


def plain(value):
    """Turn numpy scalars inside a result into JSON-friendly Python values."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def classify_all(candidates, cache_dir, db, crop_factors=None, focal35=None):
    out_dir = Path(cache_dir) / "verify"
    results = {}
    for c in candidates:
        path = out_dir / f"{set_key(c['asset_ids'])}.npz"
        if not path.exists():
            continue
        with np.load(path) as f:
            data = {k: f[k] for k in f.files}
        rows = {r["id"]: dict(r) for r in db.execute(
            f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(c['asset_ids']))})", c["asset_ids"])}
        meta = [rows.get(a) for a in c["asset_ids"]]
        crops = [lens.crop_factor(crop_factors or {}, m["make"], m["model"], m["focal_length"]) if m else None
                 for m in meta]
        f35 = [float((focal35 or {}).get(a) or 0) for a in c["asset_ids"]]
        r = classify_set(c, data, meta, crops, f35)
        r["measure_seconds"] = round(float(data["timing"][1]), 1)
        results[c["set_id"]] = plain(r)
    return results
