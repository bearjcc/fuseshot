"""Split a project into the parts its control points really join, and level a row.

cpfind links almost any two frames with a stray point or two, and the optimiser
then places a frame that shares nothing real with the rest wherever it likes:
rotated across the middle of the panorama or smeared over half the sphere (a
camera-made sweep panorama treated as one wide-angle frame does exactly that).
Frames are kept only when at least MIN_LINK_POINTS points join them to the
rest and their own points fit the solution. The largest joined group is the
panorama; other groups of MIN_PART_FRAMES or more become panoramas of their
own, and anything smaller is left out of the render.
"""
import math
import re
from collections import defaultdict
from pathlib import Path

MIN_LINK_POINTS = 4
MIN_PART_FRAMES = 3
BAD_FRAME_RATIO = 4.0
BAD_FRAME_PX = 20.0
MAX_FRAME_SHARE = 3.0
LINK = re.compile(r'(?<=\s)([A-Za-z]+)=(\d+)(?=\s|$)')


def read_project(pto_path):
    """Lines of a project with its images and control points picked out."""
    lines = Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines()
    images, points = [], []
    for index, line in enumerate(lines):
        if line.startswith('i '):
            images.append(index)
        elif line.startswith('c '):
            n = int(re.search(r'\sn(\d+)', line).group(1))
            m = int(re.search(r'\sN(\d+)', line).group(1))
            xy = [float(re.search(rf'\s{k}(-?[\d.e+-]+)', line).group(1)) for k in ('x', 'y', 'X', 'Y')]
            points.append((n, m, *xy))
    return lines, images, points


def value(lines, images, index, key):
    """A lens or pose value of one image, following links to the image that holds it."""
    seen = set()
    while index not in seen:
        seen.add(index)
        line = lines[images[index]]
        found = re.search(rf'\s{key}(=?)(-?[\d.e+-]+)', line)
        if not found:
            return None
        if found.group(1):
            index = int(found.group(2))
            continue
        return float(found.group(2))
    return None


def frame_info(pto_path):
    """Size, lens and pose of every image (rectilinear frames only get a lens)."""
    lines, images, points = read_project(pto_path)
    frames = []
    for k, at in enumerate(images):
        line = lines[at]
        get = lambda key: value(lines, images, k, key)
        lens = re.search(r'\sf(\d+)', line)
        name = re.search(r' n"([^"]+)"', line)
        frames.append({'w': get('w'), 'h': get('h'), 'f': int(lens.group(1)) if lens else 0,
                       'v': get('v'), 'y': get('y') or 0.0, 'p': get('p') or 0.0, 'r': get('r') or 0.0,
                       'a': get('a') or 0.0, 'b': get('b') or 0.0, 'c': get('c') or 0.0,
                       'd': get('d') or 0.0, 'e': get('e') or 0.0,
                       'moved': any(abs(get(t) or 0.0) > 1e-9 for t in ('TrX', 'TrY', 'TrZ')),
                       'file': name.group(1) if name else ''})
    return frames, points


def rotation(yaw, pitch, roll):
    """Camera-to-world rotation in the panotools order: roll, then pitch, then yaw."""
    def turn(axis, angle):
        c, s = math.cos(angle), math.sin(angle)
        if axis == 'x':
            return [[1, 0, 0], [0, c, -s], [0, s, c]]
        if axis == 'y':
            return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
        return [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    mul = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(turn('z', -math.radians(yaw)), mul(turn('y', -math.radians(pitch)), turn('x', math.radians(roll))))


def focal_px(frame):
    if not frame['v'] or frame['v'] <= 0 or frame['v'] >= 180:
        return None
    return frame['w'] / 2 / math.tan(math.radians(frame['v']) / 2)


def ray(frame, x, y, matrix):
    """World direction of a pixel: x forward, y left, z up. Undoes the a, b, c lens distortion."""
    f = focal_px(frame)
    u = x - frame['w'] / 2 - frame['d']
    w = y - frame['h'] / 2 - frame['e']
    half = min(frame['w'], frame['h']) / 2
    r = math.hypot(u, w) / half
    a, b, c = frame['a'], frame['b'], frame['c']
    if r > 0 and (a or b or c):
        ideal = r
        for _ in range(20):
            ideal = r / (a * ideal ** 3 + b * ideal ** 2 + c * ideal + 1 - a - b - c)
        u, w = u * ideal / r, w * ideal / r
    cam = (f, -u, -w)
    norm = math.sqrt(sum(t * t for t in cam))
    return [sum(matrix[i][k] * cam[k] / norm for k in range(3)) for i in range(3)]


def point_errors(frames, points):
    """Angle between the two ends of every control point, in pixels of the first frame.

    Only defined for rectilinear frames under the rotation model; returns None
    otherwise (a mosaic moves the camera, so directions alone do not match)."""
    if any(fr['f'] != 0 or fr['moved'] or focal_px(fr) is None for fr in frames):
        return None
    mats = [rotation(fr['y'], fr['p'], fr['r']) for fr in frames]
    errors = []
    for n, m, x, y, X, Y in points:
        a, b = ray(frames[n], x, y, mats[n]), ray(frames[m], X, Y, mats[m])
        angle = math.acos(max(-1.0, min(1.0, sum(i * j for i, j in zip(a, b)))))
        errors.append(angle * (focal_px(frames[n]) + focal_px(frames[m])) / 2)
    return errors


def groups(count, pairs, min_points):
    """Frames joined by min_points or more points, counting every point between two groups together."""
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (a, b), n in pairs.items():
        if n >= min_points:
            parent[find(a)] = find(b)
    merged = True
    while merged:
        merged = False
        between = defaultdict(int)
        for (a, b), n in pairs.items():
            ra, rb = find(a), find(b)
            if ra != rb:
                between[tuple(sorted((ra, rb)))] += n
        for (ra, rb), n in sorted(between.items(), key=lambda kv: -kv[1]):
            if n >= min_points and find(ra) != find(rb):
                parent[find(ra)] = find(rb)
                merged = True
    found = defaultdict(list)
    for i in range(count):
        found[find(i)].append(i)
    return sorted(found.values(), key=lambda g: (-len(g), g[0]))


def median(values):
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def split(pto_path, px_scale=1.0):
    """Which frames to stitch, and into how many panoramas.

    Returns {'parts': [[i, ...], ...], 'dropped': {i: reason}, 'frame_error': {i: px}}
    with 0-based indices, the largest part first."""
    frames, points = frame_info(pto_path)
    count = len(frames)
    dropped = {}
    frame_error = {}
    errors = point_errors(frames, points)
    if errors is not None:
        own = defaultdict(list)
        for (n, m, *_), err in zip(points, errors):
            own[n].append(err)
            own[m].append(err)
        frame_error = {i: round(median(e), 2) for i, e in own.items()}
        typical = median(list(frame_error.values()))
        limit = max(BAD_FRAME_PX * px_scale, BAD_FRAME_RATIO * typical)
        for i, err in frame_error.items():
            if err > limit:
                dropped[i] = f'its points miss by {err:.0f} px (typical {typical:.1f} px)'
    shapes = [fr['w'] / fr['h'] for fr in frames if fr['w'] and fr['h']]
    usual = median([max(s, 1 / s) for s in shapes])
    for i, fr in enumerate(frames):
        shape = max(fr['w'] / fr['h'], fr['h'] / fr['w'])
        if i not in dropped and shape > MAX_FRAME_SHARE * usual / 1.5 and shape > 2.5:
            dropped[i] = f'a {shape:.1f}:1 strip among {usual:.1f}:1 frames, a camera-made panorama'
    pairs = defaultdict(int)
    for n, m, *_ in points:
        if n not in dropped and m not in dropped and n != m:
            pairs[tuple(sorted((n, m)))] += 1
    found = [g for g in groups(count, pairs, MIN_LINK_POINTS) if g[0] not in dropped or len(g) > 1]
    parts = []
    for g in found:
        g = [i for i in g if i not in dropped]
        if len(g) >= MIN_PART_FRAMES or (not parts and len(g) >= 2):
            parts.append(g)
        else:
            for i in g:
                dropped[i] = 'not joined to the rest by enough control points'
    return {'parts': parts, 'dropped': dict(sorted(dropped.items())), 'frame_error': frame_error}


def subset(pto_path, keep, out_path):
    """Write a copy of a project holding only the images in keep (0-based, in order)."""
    lines, images, _ = read_project(pto_path)
    keep = list(keep)
    new = {old: k for k, old in enumerate(keep)}
    owner = {at: k for k, at in enumerate(images)}
    # A mosaic is seen from where the first camera stood; keep that true when the
    # first camera is left out, or the view ends up behind the frames.
    shift = {key: (value(lines, images, keep[0], key) or 0.0) if keep else 0.0 for key in ('TrX', 'TrY', 'TrZ')}
    out = []
    for index, line in enumerate(lines):
        if line.startswith('i '):
            k = owner[index]
            if k not in new:
                continue
            for key, by in shift.items():
                if by:
                    line = re.sub(rf'(\s){key}(-?[\d.]+(?:e[+-]?\d+)?)',
                                  lambda m, by=by: f'{m.group(1)}{key}{float(m.group(2)) - by:.10g}', line, count=1)

            def relink(found, k=k):
                key, ref = found.group(1), int(found.group(2))
                if ref in new:
                    return f'{key}={new[ref]}'
                held = value(lines, images, ref, key)
                return f'{key}{held:.10g}' if held is not None else f'{key}0'
            head, sep, name = line.partition(' n"')
            out.append(LINK.sub(relink, head) + sep + name)
        elif line.startswith('#-hugin') and index + 1 < len(lines) and lines[index + 1].startswith('i '):
            if owner[index + 1] in new:
                out.append(line)
        elif line.startswith('c '):
            n = int(re.search(r'\sn(\d+)', line).group(1))
            m = int(re.search(r'\sN(\d+)', line).group(1))
            if n in new and m in new:
                line = re.sub(r'\sn\d+', f' n{new[n]}', line, count=1)
                out.append(re.sub(r'\sN\d+', f' N{new[m]}', line, count=1))
        elif line.startswith('v ') and line.strip() != 'v':
            found = re.fullmatch(r'v ([A-Za-z]+)(\d+)', line.strip())
            if not found:
                out.append(line)
            elif int(found.group(2)) in new:
                out.append(f'v {found.group(1)}{new[int(found.group(2))]}')
        elif line.startswith('k '):
            found = re.search(r'\si(\d+)', line)
            if found and int(found.group(1)) in new:
                out.append(re.sub(r'\si\d+', f' i{new[int(found.group(1))]}', line, count=1))
        else:
            out.append(line)
    Path(out_path).write_text('\n'.join(out) + '\n', encoding='utf-8')
    return out_path


def axes(frame):
    """World directions of a frame's centre, right edge and top edge."""
    m = rotation(frame['y'], frame['p'], frame['r'])
    return [m[i][0] for i in range(3)], [-m[i][1] for i in range(3)], [m[i][2] for i in range(3)]


def level_up(frames):
    """The up direction a set of hand-held or tripod frames agree on, or None.

    A camera turned about a vertical axis keeps its horizontal edge horizontal
    whatever its tilt, so up is the direction every frame's horizontal axis is
    most nearly square to (Hugin's straighten works the same way). For a row at
    zero tilt this is the normal of the great circle through the view
    directions. Returns None when the axes do not pin it down (all frames
    sharing one horizontal axis, as in a vertical column). A frame turned on
    its side (portrait shot read as landscape) counts its short edge instead."""
    rights, ups = [], []
    for fr in frames:
        _, right, top = axes(fr)
        if abs(top[2]) < abs(right[2]):
            right, top = top, [-c for c in right]
        rights.append(right)
        ups.append(top)
    m = [[sum(r[i] * r[j] for r in rights) for j in range(3)] for i in range(3)]
    values, vectors = eigen(m)
    if values[1] < 0.02 * len(frames):
        return None
    up = vectors[0]
    if sum(sum(a * b for a, b in zip(up, top)) for top in ups) < 0:
        up = [-c for c in up]
    return up


def eigen(m):
    """Eigenvalues (ascending) and unit eigenvectors of a symmetric 3x3 matrix, by Jacobi sweeps."""
    a = [row[:] for row in m]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(50):
        size, p, q = max((abs(a[i][j]), i, j) for i in range(3) for j in range(3) if i < j)
        if size < 1e-12:
            break
        theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
        t = (1 if theta >= 0 else -1) / (abs(theta) + math.sqrt(theta * theta + 1))
        c = 1 / math.sqrt(t * t + 1)
        s = t * c
        for k in range(3):
            a[k][p], a[k][q] = c * a[k][p] - s * a[k][q], s * a[k][p] + c * a[k][q]
        for k in range(3):
            a[p][k], a[q][k] = c * a[p][k] - s * a[q][k], s * a[p][k] + c * a[q][k]
        for k in range(3):
            v[k][p], v[k][q] = c * v[k][p] - s * v[k][q], s * v[k][p] + c * v[k][q]
    order = sorted(range(3), key=lambda i: a[i][i])
    return [a[i][i] for i in order], [[v[k][i] for k in range(3)] for i in order]


def level_turn(up):
    """Yaw and tilt (degrees) that bring up to straight up: turn by yaw, pitch by tilt, turn back.

    pano_modify --rotate=yaw,pitch,roll turns the whole panorama; doing the yaw
    first puts the tilt into pitch alone, and turning the yaw back afterwards
    keeps the panorama facing the way it did."""
    x, y, z = up
    yaw = math.degrees(math.atan2(-y, x))
    tilt = math.degrees(math.atan2(math.hypot(x, y), z))
    return yaw, tilt


def centre_pitch(frames):
    return [math.degrees(math.asin(max(-1.0, min(1.0, axes(fr)[0][2])))) for fr in frames]


def swing(pto_path, rows=None):
    """Up/down swing of the frame centres in degrees: the largest spread inside one row."""
    frames, _ = frame_info(pto_path)
    if len(frames) < 3 or any(focal_px(fr) is None for fr in frames):
        return None
    pitches = centre_pitch(frames)
    rows = rows or [list(range(len(frames)))]
    return round(max(max(pitches[i] for i in r) - min(pitches[i] for i in r) for r in rows if r), 1)
