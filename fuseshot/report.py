"""Write candidate sets to CSV, JSON and a plain text summary."""

import csv
import json
from collections import Counter
from pathlib import Path

from .detect import distance_m
from .files import write_atomic

CSV_FIELDS = [
    "set_id", "start_local", "start_utc", "camera", "count", "files", "positions",
    "span_s", "median_gap_s", "focal_min", "focal_max", "focal_levels",
    "ev_range", "bracket", "gps_points", "gps_max_disp_m", "lat", "lon", "places",
    "guessed_type", "confidence", "splat_candidate", "flags", "people_share",
    "verified_type", "verified_confidence", "motion_model", "stitch_plan", "coverage_h", "coverage_v",
    "rows", "sub_sets", "junk_frames", "verified_splat", "quality", "verify_reason", "best_link",
    "first_link", "asset_ids",
]

# Which verified types count as agreeing with each metadata guess.
GUESS_MATCHES = {
    "single-row rotation": {"single-row rotation"},
    "multi-row grid": {"multi-row grid", "sphere/360"},
    "sphere or large multi-row": {"sphere/360", "multi-row grid"},
    "multi-zoom layered": {"multi-zoom layered"},
    "exposure bracket": {"bracket"},
    "linear walk": {"linear walk/translation", "object scan/walk-around"},
}


def base_type(verified_type):
    return verified_type.split(" (+")[0] if verified_type else None


def changed_type(c):
    v = base_type(c.get("verified_type"))
    return v is not None and v not in GUESS_MATCHES.get(c["guessed_type"], {c["guessed_type"]})


def merge_verified(candidates, results, web_url):
    """Copy verification results onto the candidates (in place)."""
    for c in candidates:
        r = results.get(c["set_id"])
        if r is None:
            continue
        main = None
        for sub in r["sub_sets"]:
            if sub["type"] == base_type(r["verified_type"]) and (main is None or sub["quality"] > main["quality"]):
                main = sub
        first = (main["frames"][0] - 1) if main else 0
        c.update(
            verified_type=r["verified_type"], verified_confidence=r["verified_confidence"],
            motion_model=r["motion_model"], stitch_plan=" > ".join(r["stitch_plan"]),
            coverage_h=r["coverage_h"], coverage_v=r["coverage_v"], rows=r["rows"],
            sub_sets="; ".join(f"{s['type']}: {s['frames'][0]}-{s['frames'][-1]} ({s['frames_count']})"
                               for s in r["sub_sets"]),
            junk_frames=" ".join(str(i) for i in r["junk_frames"]),
            verified_splat=r["splat_candidate"], quality=r["quality"], verify_reason=r["reason"],
            best_link=link(web_url, c["asset_ids"][first]), verify=r)


def band(confidence):
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.55:
        return "medium"
    return "low"


def link(web_url, asset_id):
    """Immich page for a photo; photos read from a folder have none."""
    if str(asset_id).startswith("file-"):
        return ""
    return f"{web_url.rstrip('/')}/photos/{asset_id}"


def match_sites(db, candidates, sites):
    """For each known place, find candidates there by GPS, by Immich place name,
    or (for cameras without GPS) by being shot on a day other photos were taken there."""
    gps_rows = db.execute(
        "SELECT substr(local_time, 1, 10) AS day, latitude, longitude, city, state "
        "FROM assets WHERE latitude IS NOT NULL").fetchall()
    results = []
    for site in sites:
        radius = site["radius_km"] * 1000
        centres = site.get("points") or [[site["lat"], site["lon"]]]

        def near(lat, lon):
            return any(distance_m(lat, lon, c[0], c[1]) <= radius for c in centres)

        names = [n.lower() for n in site.get("place_names", [])]
        photo_days = Counter(r["day"] for r in gps_rows if near(r["latitude"], r["longitude"])
                             and f"{r['city']}, {r['state']}".lower() not in names)
        for row in db.execute("SELECT substr(local_time, 1, 10) AS day, city, state FROM assets "
                              "WHERE city IS NOT NULL"):
            if f"{row['city']}, {row['state']}".lower() in names:
                photo_days[row["day"]] += 1
        matches = []
        for c in candidates:
            day = c["start_local"][:10]
            if c["lat"] is not None and near(c["lat"], c["lon"]):
                how = "gps"
            elif any(p.lower() in names for p in c["places"].split("; ") if p):
                how = "place name"
            elif c["lat"] is None and day in photo_days:
                how = "same day, no gps"
            else:
                continue
            matches.append((how, c))
        results.append({"site": site, "photo_days": photo_days, "matches": matches})
    return results


def write_bursts(bursts, stats, out_dir, web_url):
    """Burst-mode runs go to their own file so candidate numbering stays put."""
    for b in bursts:
        b["first_link"] = link(web_url, b["asset_ids"][0])
    data = {"stats": {"runs": len(bursts), "shots": sum(b["count"] for b in bursts),
                      "excluded_burst": stats.get("excluded_burst")},
            "candidates": bursts}
    write_atomic(Path(out_dir) / "bursts.json", json.dumps(data, indent=1))


def write_all(candidates, stats, settings, out_dir, web_url, db=None, sites=None):
    for c in candidates:
        c["first_link"] = link(web_url, c["asset_ids"][0])

    with open(out_dir / "candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for c in candidates:
            row = dict(c)
            row.pop("verify", None)
            row["asset_ids"] = " ".join(c["asset_ids"])
            writer.writerow(row)

    with open(out_dir / "candidates.json", "w", encoding="utf-8") as f:
        settings = settings if isinstance(settings, dict) else vars(settings)
        json.dump({"settings": settings, "stats": stats, "candidates": candidates}, f, indent=1)

    lines = []
    lines.append(f"Candidate sets: {len(candidates)} (metadata only, types and scores are provisional)")
    lines.append("")
    lines.append("Library: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in stats.items()))
    lines.append("")
    lines.append("By confidence:")
    for name in ("high", "medium", "low"):
        lines.append(f"  {name:8} {sum(1 for c in candidates if band(c['confidence']) == name)}")
    lines.append("")
    lines.append("By guessed type (high / medium / low):")
    types = Counter(c["guessed_type"] for c in candidates)
    for kind, count in types.most_common():
        parts = [sum(1 for c in candidates if c["guessed_type"] == kind and band(c["confidence"]) == b)
                 for b in ("high", "medium", "low")]
        lines.append(f"  {kind:42} {count:5}  ({parts[0]} / {parts[1]} / {parts[2]})")
    lines.append(f"  possible Gaussian splat sets: {sum(1 for c in candidates if c['splat_candidate'])}")
    lines.append("")
    lines.append("Most confident sets:")
    top = sorted(candidates, key=lambda c: (-c["confidence"], -c["positions"]))[:15]
    for c in top:
        lines.append(f"  {c['set_id']} {c['start_local']}  {c['camera']:30} {c['count']:3} shots  "
                     f"{c['guessed_type']:28} {c['confidence']:.2f}  {c['first_link']}")

    verified = [c for c in candidates if c.get("verified_type")]
    if verified:
        from .verify import PANORAMA_TYPES
        real = [c for c in verified if base_type(c["verified_type"]) in PANORAMA_TYPES]
        lines.append("")
        lines.append(f"Overlap check: {len(verified)} of {len(candidates)} sets checked, "
                     f"{len(real)} look like real panoramas or scans")
        lines.append("By verified type (high / medium / low confidence):")
        for kind, count in Counter(base_type(c["verified_type"]) for c in verified).most_common():
            parts = [sum(1 for c in verified if base_type(c["verified_type"]) == kind
                         and band(c["verified_confidence"]) == b) for b in ("high", "medium", "low")]
            lines.append(f"  {kind:42} {count:5}  ({parts[0]} / {parts[1]} / {parts[2]})")
        multi = sum(1 for c in verified if "(+" in c["verified_type"])
        lines.append(f"  sets holding more than one panorama: {multi}")
        lines.append("By motion model (picks the first stitching model to try):")
        for kind, count in Counter(c["motion_model"] for c in verified).most_common():
            lines.append(f"  {kind:42} {count:5}")
        changed = [c for c in verified if changed_type(c)]
        lines.append(f"Type changed from the metadata guess: {len(changed)}")
        for (g, v), count in Counter((c["guessed_type"], base_type(c["verified_type"])) for c in changed).most_common(12):
            lines.append(f"  {g:28} -> {v:28} {count:5}")
        splats = sorted((c for c in verified if c.get("verified_splat")), key=lambda c: -c["quality"])
        lines.append(f"Splat candidates (object scans and walk-arounds with heavy overlap): {len(splats)}")
        for c in splats[:20]:
            lines.append(f"  {c['set_id']} {c['start_local']}  {c['camera']:28} {c['count']:3} shots  "
                         f"{c['verified_type']:28} {c['verified_confidence']:.2f}  {c['best_link']}")
        lines.append("")
        lines.append("Best verified panoramas (object scans and mixed sets are listed with the splat candidates):")
        flat = [c for c in real if base_type(c["verified_type"]) not in ("object scan/walk-around", "mixed motion")]
        for c in sorted(flat, key=lambda c: -c["quality"])[:15]:
            cover = f"{c['coverage_h']}x{c['coverage_v']} deg" if c.get("coverage_h") else c["motion_model"]
            lines.append(f"  {c['set_id']} {c['start_local']}  {c['camera']:28} {c['count']:3} shots  "
                         f"{c['verified_type']:28} {cover:14} q {c['quality']:.2f}  {c['places'] or '-'}  {c['best_link']}")

    if db is not None and sites:
        lines.append("")
        lines.append("Known panorama places:")
        for result in match_sites(db, candidates, sites):
            site = result["site"]
            days = result["photo_days"]
            lines.append(f"  {site['name']}: {sum(days.values())} photos with GPS or place name "
                         f"on {len(days)} days, {len(result['matches'])} candidate sets")
            for how, c in result["matches"]:
                kind = c["guessed_type"]
                if c.get("verified_type"):
                    cover = f" {c['coverage_h']}x{c['coverage_v']}" if c.get("coverage_h") else ""
                    kind = f"{c['verified_type']}{cover} (was {kind})"
                score = c.get("verified_confidence", c["confidence"])
                lines.append(f"    {c['set_id']} {c['start_local']} {c['camera']:28} {c['count']:3} shots  "
                             f"{kind:44} {score:.2f}  [{how}]  {c.get('best_link') or c['first_link']}")

    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
