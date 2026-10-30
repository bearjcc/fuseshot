"""Blend nona layers where enblend cannot: duplicates, and zoom layers inside wider frames.

enblend looks for one seam between each pair of images, so it gives up when an
image lies wholly inside another ("excessive image overlap") and leaves a hard
rectangle when it is forced. Here the layers go on one at a time, least detailed
first, so the sharpest frames end up on top. Each new layer has its low
frequencies matched to what is already there, is faded in from its edges over a
band a few percent of its size, and is merged band by band (Laplacian pyramid) so
the fade is wide for smooth tones and narrow for detail.
"""

from pathlib import Path

CONTAINED = 0.85
DUPLICATE_IOU = 0.97
FEATHER = 0.06
GAIN_LIMITS = (0.67, 1.5)


def read_layer(path):
    import cv2
    import numpy as np
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise OSError(f"cannot read {path}")
    scale = 65535.0 if img.dtype == np.uint16 else 255.0
    img = img.astype(np.float32) / scale
    if img.ndim == 2:
        img = np.dstack([img, img, img, np.ones_like(img)])
    if img.shape[2] == 3:
        img = np.dstack([img, np.ones(img.shape[:2], np.float32)])
    return img[..., :3], img[..., 3] > 0.5


def masks(paths, step=4):
    import cv2
    out = []
    for path in paths:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        alpha = img[..., 3] if img.ndim == 3 and img.shape[2] == 4 else None
        out.append((alpha[::step, ::step] > 0) if alpha is not None else None)
    return out


def plan_layers(paths, detail):
    """Order layers least detailed first and drop exact duplicates.

    detail gives the pixels per degree of each layer. Two layers covering the
    same footprint are copies of one photo (an original and an edited copy), and
    only the more detailed one is kept. Returns the kept layers in blending order,
    the number dropped and whether any layer is mostly inside the others."""
    import numpy as np
    ms = masks(paths)
    if any(m is None for m in ms):
        return list(paths), 0, False
    keep = list(range(len(paths)))
    dropped = 0
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            if i not in keep or j not in keep:
                continue
            inter = np.count_nonzero(ms[i] & ms[j])
            union = np.count_nonzero(ms[i] | ms[j])
            if union and inter / union >= DUPLICATE_IOU:
                keep.remove(i if detail[i] < detail[j] else j)
                dropped += 1
    keep.sort(key=lambda i: detail[i])
    contained = False
    for i in keep:
        others = np.zeros_like(ms[i])
        for j in keep:
            if j != i:
                others |= ms[j]
        area = np.count_nonzero(ms[i])
        if area and np.count_nonzero(ms[i] & others) / area > CONTAINED:
            contained = True
    return [paths[i] for i in keep], dropped, contained


def push_pull(img, mask):
    """Fill the pixels outside mask with smooth colour from inside, so the pyramid
    does not pull black in at the edges."""
    import cv2
    import numpy as np
    levels = []
    num, den = img * mask[..., None], mask.astype(np.float32)
    while min(den.shape[:2]) > 8:
        levels.append((num, den))
        num, den = cv2.pyrDown(num), cv2.pyrDown(den)
    filled = num / np.maximum(den, 1e-6)[..., None]
    for num, den in reversed(levels):
        up = cv2.resize(filled, (num.shape[1], num.shape[0]), interpolation=cv2.INTER_LINEAR)
        known = den > 1e-3
        filled = np.where(known[..., None], num / np.maximum(den, 1e-6)[..., None], up)
    return np.where(mask[..., None], img, filled)


def pyramid_blend(a, b, w, levels):
    import cv2
    ga, gb, gw = [a], [b], [w]
    for _ in range(levels):
        ga.append(cv2.pyrDown(ga[-1]))
        gb.append(cv2.pyrDown(gb[-1]))
        gw.append(cv2.pyrDown(gw[-1]))
    out = ga[-1] * (1 - gw[-1][..., None]) + gb[-1] * gw[-1][..., None]
    for k in range(levels - 1, -1, -1):
        size = (ga[k].shape[1], ga[k].shape[0])
        la = ga[k] - cv2.pyrUp(ga[k + 1], dstsize=size)
        lb = gb[k] - cv2.pyrUp(gb[k + 1], dstsize=size)
        lw = gw[k][..., None]
        out = cv2.pyrUp(out, dstsize=size) + la * (1 - lw) + lb * lw
    return out


def gain_map(acc, layer, overlap, sigma):
    """Per channel gain that brings the layer's low frequencies to the blend so far."""
    import cv2
    import numpy as np
    ov = overlap.astype(np.float32)
    if ov.sum() < 64:
        return np.ones_like(layer)
    lo, hi = GAIN_LIMITS
    sel = overlap
    whole = np.clip(acc[sel].mean(axis=0) / np.maximum(layer[sel].mean(axis=0), 1e-4), lo, hi)
    # the gain is smooth, so work on a small copy
    f = max(1, int(sigma / 4))
    small = (max(1, ov.shape[1] // f), max(1, ov.shape[0] // f))
    shrink = lambda x: cv2.resize(x, small, interpolation=cv2.INTER_AREA)
    k, s = (0, 0), sigma / f
    w = cv2.GaussianBlur(shrink(ov), k, s)
    num = cv2.GaussianBlur(shrink(acc * ov[..., None]), k, s)
    den = cv2.GaussianBlur(shrink(layer * ov[..., None]), k, s)
    local = np.clip(num / np.maximum(den, 1e-4), lo, hi)
    trust = np.clip(w / 0.5, 0, 1)[..., None]
    gain = local * trust + whole[None, None, :] * (1 - trust)
    return cv2.resize(gain, (ov.shape[1], ov.shape[0]), interpolation=cv2.INTER_LINEAR)


def smooth(x):
    return x * x * (3 - 2 * x)


def blend_layers(paths, dest, detail):
    """Blend layers (full canvas RGBA) into dest as 8-bit RGBA PNG.

    Returns the overlap bands (uint8 mask) where a seam was faded."""
    import cv2
    import numpy as np
    acc = cover = seams = None
    for path in paths:
        layer, m = read_layer(path)
        if acc is None:
            acc = np.zeros_like(layer)
            cover = np.zeros(m.shape, bool)
            seams = np.zeros(m.shape, np.uint8)
        ys, xs = np.nonzero(m)
        if not len(ys):
            continue
        if not cover.any():
            acc[m] = layer[m]
            cover |= m
            continue
        bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        feather = max(8.0, FEATHER * min(bw, bh))
        levels = max(1, min(6, int(np.log2(max(2, min(bw, bh) / 16)))))
        margin = int(2 * feather + 2 ** levels * 2)
        y0, y1 = max(0, ys.min() - margin), min(m.shape[0], ys.max() + margin + 1)
        x0, x1 = max(0, xs.min() - margin), min(m.shape[1], xs.max() + margin + 1)
        sl = (slice(y0, y1), slice(x0, x1))
        a, ca, b, mb = acc[sl], cover[sl], layer[sl], m[sl]
        overlap = ca & mb
        if overlap.any():
            b = b * gain_map(a, b, overlap, max(4.0, 0.15 * min(bw, bh)))
        # fade the layer in from its edges where something is already there, and
        # fade the blend so far out at its own edges inside the layer
        edge = ((~mb) & ca).astype(np.uint8)
        db = cv2.distanceTransform((1 - edge).astype(np.uint8), cv2.DIST_L2, 5) if edge.any() \
            else np.full(mb.shape, feather, np.float32)
        da = cv2.distanceTransform(ca.astype(np.uint8), cv2.DIST_L2, 5)
        wb = np.clip(db / feather, 0, 1)
        wa = np.clip(da / feather, 0, 1)
        w = smooth(1 - (1 - wb) * wa) * mb
        w = np.where(mb & ~ca, 1.0, w).astype(np.float32)
        union = ca | mb
        out = pyramid_blend(push_pull(a, ca), push_pull(b, mb), w, levels)
        acc[sl] = np.where(union[..., None], np.clip(out, 0, 1), 0)
        cover[sl] = union
        band = overlap & (w > 0.02) & (w < 0.98)
        seams[sl][band] = 255
    # layers were read with OpenCV, so acc is already in BGR order
    bgra = (np.dstack([acc, cover.astype(np.float32)]) * 255 + 0.5).astype(np.uint8)
    cv2.imwrite(str(dest), bgra)
    return seams


def layer_detail(paths, images, files):
    """Pixels per degree of each nona layer (layerNNNN.tif is image NNNN)."""
    import re
    detail = []
    first_v = next((i["v"] for i in images if i["v"]), 50.0)
    for path in paths:
        index = int(re.search(r"(\d+)$", Path(path).stem).group(1))
        v = images[index]["v"] or first_v
        detail.append(files[index]["w"] / max(v, 1e-3))
    return detail
