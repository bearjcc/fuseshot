"""Find candidate panorama shot sets from photo metadata alone.

Everything here is a first guess. Types and confidence scores are provisional
until image overlap matching confirms how the frames fit together.
"""

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePath


# Makes and models that are scanners, printers or software rather than cameras.
NON_CAMERA_WORDS = ("scanner", "scan", "pstc", "photonet", "film", "printer")
RAW_MIMES = {"image/arw", "image/cr2", "image/cr3", "image/nef", "image/dng",
             "image/orf", "image/rw2", "image/raf", "image/pef", "image/x-adobe-dng"}


@dataclass
class Settings:
    max_gap: float = 30.0          # seconds between consecutive shots
    min_shots: int = 3             # camera positions, after pairing RAW+JPEG
    max_jump_m: float = 250.0      # split if consecutive GPS fixes are further apart
    burst_gap: float = 0.6         # median gap below this looks like phone burst mode
    max_people_share: float = 0.8  # small sets that are nearly all faces are people photos
    since: str = None              # first local date to look at, YYYY-MM-DD
    until: str = None              # last local date to look at, YYYY-MM-DD


@dataclass
class Shot:
    id: str
    file_name: str
    mime: str
    when: datetime
    local_time: str
    time_zone: str
    make: str
    model: str
    lens: str
    focal: float
    f_number: float
    exposure: float
    iso: float
    lat: float
    lon: float
    width: int
    height: int
    people: int
    city: str
    state: str
    country: str
    projection: str
    extra_ids: list = field(default_factory=list)

    @property
    def ev(self):
        """Exposure value normalised to ISO 100, or None if anything is missing."""
        if not (self.f_number and self.exposure and self.iso):
            return None
        return math.log2(self.f_number ** 2 / self.exposure) - math.log2(self.iso / 100)

    @property
    def landscape(self):
        if not (self.width and self.height):
            return None
        return self.width >= self.height


def parse_time(text):
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_camera(make, model):
    text = f"{make or ''} {model or ''}".lower()
    return bool(make) and not any(word in text for word in NON_CAMERA_WORDS)


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def load_shots(db, stats, since=None, until=None):
    """Yield usable shots ordered by camera then time, one row at a time.

    A photo that is in the folders more than once (Takeout puts it in its year
    folder and in each album) is counted once. since and until limit the local
    capture date, as YYYY-MM-DD, until being the last day included."""
    where = ["type = 'IMAGE'", "is_trashed = 0", "taken_utc IS NOT NULL"]
    params = []
    if since:
        where.append("COALESCE(local_time, taken_utc) >= ?")
        params.append(since)
    if until:
        where.append("COALESCE(local_time, taken_utc) < ?")
        params.append(until + "T99")
    columns = {r[1] for r in db.execute("PRAGMA table_info(source_files)")} if has_table(db, "source_files") else set()
    if "content" in columns:
        # The copy kept is one with its Takeout .json (an album copy often has none, and
        # with it the place); among equals, the lowest id, so the choice never changes.
        better = "other.id < mine.id"
        if "sidecar" in columns:
            better = """(COALESCE(other.sidecar, '') <> '') > (COALESCE(mine.sidecar, '') <> '')
                  OR ((COALESCE(other.sidecar, '') <> '') = (COALESCE(mine.sidecar, '') <> '')
                      AND other.id < mine.id)"""
        copy = f"""EXISTS (SELECT 1 FROM source_files mine JOIN source_files other
                  ON other.content = mine.content AND ({better})
                  WHERE mine.id = assets.id AND mine.content IS NOT NULL)"""
        stats["skipped_copy"] = db.execute(f"SELECT COUNT(*) FROM assets WHERE {' AND '.join(where)} AND {copy}",
                                           params).fetchone()[0]
        where.append(f"NOT {copy}")
    query = f"SELECT * FROM assets WHERE {' AND '.join(where)} ORDER BY make, model, taken_utc, file_name"
    for row in db.execute(query, params):
        stats["images"] += 1
        if not is_camera(row["make"], row["model"]):
            stats["skipped_no_camera"] += 1
            continue
        name = (row["file_name"] or "").lower()
        if name.startswith(("screenshot", "screen shot", "screen_shot")):
            stats["skipped_screenshot"] += 1
            continue
        try:
            when = parse_time(row["taken_utc"])
        except ValueError:
            stats["skipped_bad_time"] += 1
            continue
        yield Shot(
            id=row["id"], file_name=row["file_name"] or "", mime=row["mime"] or "",
            when=when, local_time=row["local_time"] or row["taken_utc"],
            time_zone=row["time_zone"], make=row["make"], model=row["model"] or "",
            lens=row["lens"], focal=row["focal_length"], f_number=row["f_number"],
            exposure=row["exposure_time"], iso=row["iso"], lat=row["latitude"],
            lon=row["longitude"], width=row["width"], height=row["height"],
            people=row["people"] or 0, city=row["city"], state=row["state"],
            country=row["country"], projection=row["projection"],
        )


# Google Photos names an edited copy with "-edited" in the language of the account.
EDITED = r"[-_](?:edited|bearbeitet|modifi[ée]|editado|bewerkt|edytowany|modificato|redigeret|redigerad|編集済み)"


def base_stem(file_name):
    """IMG_1234.CR2, PXL_x+1.jpg, PXL_x.MP.jpg and PXL_x~2.jpg all share a base stem."""
    stem = PurePath(file_name).name.lower()
    stem = stem.split(".", 1)[0]
    return re.sub(rf"([+~]\d+|{EDITED})$", "", stem)


def preference(shot):
    """Lower is better when choosing which copy of a moment stands for it: the camera's
    JPEG (quick to read, already finished), then JPEG XL, then the raw; an edited
    copy only when nothing else is left."""
    edited = 3 if re.search(rf"{EDITED}|[+~]\d+$", PurePath(shot.file_name).stem, re.I) else 0
    if shot.mime in RAW_MIMES:
        return 2 + edited
    if shot.mime == "image/jxl":
        return 1 + edited
    return edited


def burst_index(file_name):
    """The frame number in a burst whose frames share a name stem, or None."""
    name = PurePath(file_name).name
    match = re.search(r"\.(?:burst|raw)-?(\d+)|_burst(\d{2,4})(?:_cover)?\.|^(\d{5})img_", name, re.I)
    if not match or (match.group(0).lower().startswith(".raw")):
        return None
    return next(g for g in match.groups() if g is not None)


def collapse_copies(shots):
    """Fold copies of the same moment (RAW+JPEG pairs, phone +1/.jxl/~2 copies)
    into one camera position, remembering the extra asset ids."""
    merged = []
    for shot in shots:
        if merged:
            last = merged[-1]
            dt = abs((shot.when - last.when).total_seconds())
            same_stem = base_stem(last.file_name) == base_stem(shot.file_name)
            if same_stem and burst_index(last.file_name) != burst_index(shot.file_name):
                same_stem = False  # two frames of one burst, not two copies of one frame
            if dt < 1 and (same_stem or (dt < 0.01 and shot.mime != last.mime)):
                keep, extra = (last, shot) if preference(last) <= preference(shot) else (shot, last)
                keep.extra_ids = last.extra_ids + shot.extra_ids + [extra.id]
                keep.people = max(last.people, shot.people)
                merged[-1] = keep
                continue
        merged.append(shot)
    return merged


def split_runs(shots, settings):
    """Group shots from one camera into runs separated by long gaps or GPS jumps."""
    run = []
    last_fix = None
    for shot in shots:
        if run:
            gap = (shot.when - run[-1].when).total_seconds()
            jumped = False
            if shot.lat is not None and last_fix is not None:
                jumped = distance_m(last_fix[0], last_fix[1], shot.lat, shot.lon) > settings.max_jump_m
            if gap > settings.max_gap or gap < 0 or jumped:
                yield run
                run, last_fix = [], None
        run.append(shot)
        if shot.lat is not None:
            last_fix = (shot.lat, shot.lon)
    if run:
        yield run


def bracket_size(run):
    """Return the bracket size (3, 5, 7, 9) if shutter speed cycles in a fixed
    pattern while aperture and ISO stay put, as auto exposure bracketing does."""
    evs = [s.ev for s in run]
    if len(evs) < 3 or any(e is None for e in evs):
        return 0
    if len({s.f_number for s in run}) > 1 or len({s.iso for s in run}) > 1:
        return 0
    if max(evs) - min(evs) < 1.0:
        return 0
    for size in (3, 5, 7, 9):
        if len(evs) % size:
            continue
        groups = [run[i:i + size] for i in range(0, len(run), size)]
        first = sorted(s.ev for s in groups[0])
        if first[-1] - first[0] < 1.0:
            continue
        quick = all((g[-1].when - g[0].when).total_seconds() <= 2.0 * size for g in groups)
        same = all(
            all(abs(a - b) < 0.4 for a, b in zip(sorted(s.ev for s in g), first))
            for g in groups
        )
        if quick and same:
            return size
    return 0


def describe(run, settings):
    """Work out the measurements, provisional type and confidence for one run."""
    n = len(run)
    times = [s.when for s in run]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    span = (times[-1] - times[0]).total_seconds()
    median_gap = statistics.median(gaps) if gaps else 0.0

    focals = [s.focal for s in run if s.focal]
    focal_min = min(focals) if focals else None
    focal_max = max(focals) if focals else None
    focal_ratio = focal_max / focal_min if focals and focal_min > 0 else 1.0
    focal_levels = {}
    for f in focals:
        key = round(f * 2) / 2
        focal_levels[key] = focal_levels.get(key, 0) + 1
    focal_switches = sum(1 for a, b in zip(focals, focals[1:]) if abs(a - b) / max(a, b) > 0.15)

    evs = [s.ev for s in run]
    known_evs = [e for e in evs if e is not None]
    ev_range = max(known_evs) - min(known_evs) if known_evs else 0.0
    bracket = bracket_size(run)

    fixes = [(s.lat, s.lon) for s in run if s.lat is not None]
    max_disp = path = 0.0
    straightness = 0.0
    if len(fixes) >= 2:
        max_disp = max(distance_m(*fixes[0], *f) for f in fixes)
        path = sum(distance_m(*a, *b) for a, b in zip(fixes, fixes[1:]))
        end = distance_m(*fixes[0], *fixes[-1])
        straightness = end / path if path > 0 else 0.0

    people_share = sum(1 for s in run if s.people) / n
    orientations = {s.landscape for s in run if s.landscape is not None}
    exposure_set = {round(e, 1) for e in known_evs}

    positions = n // bracket if bracket else n
    flags = []
    is_burst = median_gap < settings.burst_gap and span < 8 and ev_range < 0.5 and focal_ratio < 1.1
    if is_burst:
        flags.append("burst-mode")
    if any(s.projection for s in run):
        flags.append("contains-stitched-panorama")
    if people_share >= settings.max_people_share and positions <= 6:
        flags.append("mostly-people")

    layered = (focal_ratio >= 1.4 and len([k for k, v in focal_levels.items() if v >= 2]) >= 2
               and focal_switches <= max(3, n // 4))
    linear = max_disp >= 30 and straightness >= 0.6

    if bracket and positions <= 2:
        kind = "exposure bracket"
    elif linear:
        kind = "linear walk"
    elif layered:
        kind = "multi-zoom layered"
    elif positions >= 30:
        kind = "sphere or large multi-row"
    elif positions >= 12:
        kind = "multi-row grid"
    else:
        kind = "single-row rotation"
    if bracket and positions > 2:
        kind = f"{kind} (bracketed x{bracket})"

    score = 0.45
    if median_gap <= 6:
        score += 0.15
    elif median_gap <= 12:
        score += 0.05
    elif median_gap > 20:
        score -= 0.1
    if positions == 3:
        score -= 0.15
    elif positions <= 5:
        score += 0.0
    elif positions <= 80:
        score += 0.1
    elif positions > 150:
        score -= 0.15
    if bracket:
        score += 0.05
    elif ev_range <= 1.0:
        score += 0.1
    elif ev_range > 2.5:
        score -= 0.15
    if focal_ratio < 1.05:
        score += 0.05
    elif not layered:
        score -= 0.1 if focal_switches > 3 else 0.05
    if people_share == 0:
        score += 0.05
    elif people_share > 0.3:
        score -= 0.25
    if fixes and not linear:
        if max_disp <= 30:
            score += 0.05
        elif max_disp > 150:
            score -= 0.15
    if len(orientations) == 1:
        score += 0.05
    if span > 900:
        score -= 0.1
    if gaps and statistics.pstdev(gaps) > 2 * max(median_gap, 1) and median_gap > 3:
        score -= 0.05
    if is_burst:
        score -= 0.4
    confidence = max(0.05, min(0.98, score))

    splat = (positions >= 25 and span >= 45 and people_share < 0.2
             and (not fixes or max_disp <= 120) and not linear)

    lat = statistics.fmean(f[0] for f in fixes) if fixes else None
    lon = statistics.fmean(f[1] for f in fixes) if fixes else None
    cities = sorted({f"{s.city}, {s.state}" for s in run if s.city})

    return {
        "camera": f"{run[0].make} {run[0].model}".strip(),
        "start_utc": times[0].isoformat() if run[0].time_zone else "",
        "start_local": run[0].local_time[:19].replace("T", " "),
        "count": n,
        "files": n + sum(len(s.extra_ids) for s in run),
        "positions": positions,
        "span_s": round(span, 1),
        "median_gap_s": round(median_gap, 1),
        "focal_min": focal_min,
        "focal_max": focal_max,
        "focal_levels": len(focal_levels),
        "ev_range": round(ev_range, 2),
        "exposure_levels": len(exposure_set),
        "bracket": bracket,
        "gps_points": len(fixes),
        "gps_max_disp_m": round(max_disp, 1),
        "gps_path_m": round(path, 1),
        "lat": lat,
        "lon": lon,
        "places": "; ".join(cities),
        "people_share": round(people_share, 2),
        "guessed_type": kind,
        "confidence": round(confidence, 2),
        "splat_candidate": splat,
        "flags": ";".join(flags),
        "asset_ids": [s.id for s in run],
        "paired_ids": [i for s in run for i in s.extra_ids],
    }


def folder_sources(db, ids):
    """Asset id -> folder source for those of ids read from folders."""
    found = {}
    if not has_table(db, "source_files"):
        return found
    ids = list(ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        found.update(db.execute(f"SELECT id, source FROM source_files WHERE id IN ({','.join('?' * len(chunk))})",
                                chunk))
    return found


def mark_library(found, folders):
    """Note where the photos of each set came from. Immich sets are left as they were."""
    for c in found:
        keys = [folders[i] for i in c["asset_ids"] + c["paired_ids"] if i in folders]
        if keys:
            c["library"] = "folder"
            c["library_path"] = keys[0].split(":", 1)[1]
            if len(keys) < len(c["asset_ids"]) + len(c["paired_ids"]):
                c["library_mixed"] = True


def burst_ids(bursts):
    """Name burst sets after their start time so the names survive a later sync.
    Bursts from folder photos start with F so they never take an Immich burst's name."""
    seen = {}
    for b in sorted(bursts, key=lambda b: (b["start_local"], b["camera"])):
        name = ("FB" if b.get("library") == "folder" else "B") + \
            b["start_local"].replace("-", "").replace(":", "").replace(" ", "T")
        seen[name] = seen.get(name, 0) + 1
        b["set_id"] = name if seen[name] == 1 else f"{name}{chr(ord('a') + seen[name] - 2)}"
        b["source"] = "burst"
    return bursts


def find_candidates(db, settings, bursts=None):
    """Return candidate sets and counts. Burst-mode runs are left out of the
    candidates; pass a list as bursts to collect them for the action check."""
    stats = {k: 0 for k in ("images", "skipped_no_camera", "skipped_screenshot",
                            "skipped_bad_time", "runs", "excluded_small",
                            "excluded_burst", "excluded_people", "skipped_copy")}
    candidates = []
    camera, shots = None, []

    def flush():
        for run in split_runs(collapse_copies(shots), settings):
            stats["runs"] += 1
            if len(run) < settings.min_shots:
                stats["excluded_small"] += 1
                continue
            info = describe(run, settings)
            if info["positions"] < settings.min_shots and info["guessed_type"] != "exposure bracket":
                stats["excluded_small"] += 1
                continue
            if "burst-mode" in info["flags"]:
                stats["excluded_burst"] += 1
                if bursts is not None:
                    bursts.append(info)
                continue
            if "mostly-people" in info["flags"]:
                stats["excluded_people"] += 1
                continue
            candidates.append(info)

    for shot in load_shots(db, stats, settings.since, settings.until):
        key = (shot.make, shot.model)
        if key != camera or (shots and (shot.when - shots[-1].when).total_seconds() > settings.max_gap):
            # A gap longer than any set allows ends what came before, so only a
            # moment's worth of shots is held at once.
            if shots:
                flush()
            camera, shots = key, []
        shots.append(shot)
    if shots:
        flush()

    folders = folder_sources(db, [i for c in candidates + (bursts or []) for i in c["asset_ids"] + c["paired_ids"]])
    mark_library(candidates, folders)
    if bursts is not None:
        mark_library(bursts, folders)
    candidates.sort(key=lambda c: c["start_local"])
    # Immich sets keep their numbers (P00001...). Folder sets are named after the
    # moment they start (F20240501T101500), so trying one year first and the whole
    # library later gives the same names, and the work already done is used again.
    numbers, seen = 0, {}
    for c in candidates:
        if c.get("library") == "folder":
            name = "F" + c["start_local"][:19].replace("-", "").replace(":", "").replace(" ", "T")
            seen[name] = seen.get(name, 0) + 1
            c["set_id"] = name if seen[name] == 1 else f"{name}{chr(ord('a') + seen[name] - 2)}"
        else:
            numbers += 1
            c["set_id"] = f"P{numbers:05d}"
    if bursts is not None:
        burst_ids(bursts)
    return candidates, stats
