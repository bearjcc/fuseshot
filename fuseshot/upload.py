"""Upload kept results to Immich as new photos.

Off unless the settings file says

    [upload]
    immich = true

or export is run with --immich --upload. Only results made from Immich photos go
up, and only once kept. Each becomes a new asset; the photos it was made
from are never changed or deleted. An XMP sidecar sent with each file carries
the date and place of the first photo in the set and a description saying what
Fuseshot did, including when edges were filled in by the fill model. Splats are
not uploaded, since Immich cannot show them.

work/uploads.json records what went up (item, file checksum and asset id), so
running it again sends nothing twice. Immich also checks the checksum and
answers "duplicate" for a file it already has. If a different view is kept
later, that one is uploaded too; Fuseshot does not remove the earlier one.

The key needs asset.upload on top of the read permissions, and album.read,
album.create and albumAsset.create when an album is set."""

import hashlib
import json
import threading
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from . import config, log
from .export import KIND_WORDS, finished, result_file
from .files import write_atomic

LOG = log.get(__name__)

KINDS = ("stitch", "group", "action")
DEFAULT_ALBUM = "Fuseshot"
DESCRIBE = {
    "stitch": "Panorama stitched by Fuseshot from {n} photos.",
    "group": "Group photo merged by Fuseshot from {n} photos; faces come from different shots.",
    "action": "Loop made by Fuseshot from {n} photos.",
}
CLIP_LOOP = "Loop made by Fuseshot from the clip of a motion photo."
FILLED = " Edges filled in by an AI model (LaMa); those pixels are invented."


def settings():
    """The [upload] section: whether kept results go to Immich and the album they go in."""
    section = config.settings().get("upload") or {}
    album = section.get("album", DEFAULT_ALBUM)
    return {"immich": section.get("immich") is True, "album": str(album).strip() or None}


def enabled():
    return settings()["immich"]


def describe(item, candidate, ai_filled):
    n = len(candidate.get("asset_ids") or []) or item.get("frames") or "several"
    text = CLIP_LOOP if candidate.get("clip") else DESCRIBE[item["kind"]].format(n=n)
    return text + (FILLED if ai_filled else "")


def taken_at(candidate):
    """When the first photo was taken, as ISO 8601 with its offset when known."""
    local, utc = candidate.get("start_local"), candidate.get("start_utc")
    try:
        local_dt = datetime.fromisoformat(str(local).replace(" ", "T")) if local else None
        utc_dt = datetime.fromisoformat(str(utc).replace("Z", "+00:00")) if utc else None
    except ValueError:
        local_dt = utc_dt = None
    if local_dt and utc_dt:
        offset = local_dt.replace(tzinfo=timezone.utc) - utc_dt.astimezone(timezone.utc)
        minutes = round(offset.total_seconds() / 60)
        sign = "+" if minutes >= 0 else "-"
        return local_dt.isoformat(timespec="seconds") + f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    if utc_dt:
        return utc_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if local_dt:
        return local_dt.isoformat(timespec="seconds")
    return None


def xmp_gps(value, positive, negative):
    """XMP writes GPS as degrees, decimal minutes and a direction letter."""
    letter = positive if value >= 0 else negative
    value = abs(value)
    degrees = int(value)
    return f"{degrees},{(value - degrees) * 60:.6f}{letter}"


def sidecar(description, taken=None, lat=None, lon=None):
    """An XMP sidecar Immich reads the date, place and description from."""
    props = []
    if taken:
        props.append(f"<exif:DateTimeOriginal>{escape(taken)}</exif:DateTimeOriginal>")
        props.append(f"<xmp:CreateDate>{escape(taken)}</xmp:CreateDate>")
    if lat is not None and lon is not None:
        props.append(f"<exif:GPSLatitude>{xmp_gps(lat, 'N', 'S')}</exif:GPSLatitude>")
        props.append(f"<exif:GPSLongitude>{xmp_gps(lon, 'E', 'W')}</exif:GPSLongitude>")
    props.append("<xmp:CreatorTool>Fuseshot</xmp:CreatorTool>")
    props.append(f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(description)}'
                 "</rdf:li></rdf:Alt></dc:description>")
    body = "\n   ".join(props)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
            '    xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
            '    xmlns:xmp="http://ns.adobe.com/xap/1.0/">\n'
            f"   {body}\n"
            "  </rdf:Description>\n"
            " </rdf:RDF>\n"
            "</x:xmpmeta>\n").encode("utf-8")


class Uploader:
    """Sends kept results to one Immich server and remembers what it sent."""

    def __init__(self, review, client, album=DEFAULT_ALBUM, log=LOG.info):
        self.review = review
        self.client = client
        self.album = album
        self.log = log
        self.record_file = review.work_dir / "uploads.json"
        self.lock = threading.Lock()
        self._device_fields = None

    def records(self):
        try:
            data = json.loads(self.record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("items", {})
        data.setdefault("albums", {})
        return data

    def save(self, data):
        self.record_file.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.record_file, json.dumps(data, indent=2))

    def device_fields(self):
        """Immich 2 wants deviceId and deviceAssetId with each upload; Immich 3 refuses them."""
        if self._device_fields is None:
            self._device_fields = self.client.major_version() < 3
        return self._device_fields

    def album_id(self, data):
        if not self.album:
            return None
        known = data["albums"].get(self.album)
        if known:
            return known
        found = next((a["id"] for a in self.client.albums() if a.get("albumName") == self.album), None)
        if not found:
            found = self.client.create_album(self.album, "Results kept in Fuseshot")["id"]
            self.log(f"made the Immich album {self.album!r}")
        data["albums"][self.album] = found
        return found

    def run(self, keys=None, dry_run=False):
        """Upload kept Immich results (only keys, when given). Returns the uploads made."""
        with self.lock:
            return self._run(keys, dry_run)

    def _run(self, keys, dry_run):
        data = self.records()
        decisions = self.review.decisions()
        candidates = {c["set_id"]: c for c in self.review.candidates()}
        sent = []
        for item in self.review.items()["items"]:
            if item.get("library") != "immich" or item.get("decision") != "kept":
                continue
            if item["kind"] not in KINDS or (keys is not None and item["key"] not in keys):
                continue
            path = result_file(self.review, item, decisions, log=self.log, full=not dry_run)
            if path is None:
                self.log(f"{item['key']}: kept, but its result file is missing")
                continue
            payload = finished(self.review, item, path)
            checksum = hashlib.sha1(payload).hexdigest()
            done = data["items"].setdefault(item["key"], [])
            if any(entry.get("sha1") == checksum for entry in done):
                continue
            candidate = candidates.get(item["id"].split("-")[0]) or (
                self.review.motion_candidate(item["id"]) if item["id"].startswith("motion-") else {})
            ai_filled = item["kind"] == "stitch" and path.stem.endswith("_fill")
            day = (item.get("taken") or "")[:10]
            filename = f"{day} {item['id']} {KIND_WORDS[item['kind']]}{path.suffix}".strip()
            if dry_run:
                self.log(f"{item['key']}: would upload {filename}")
                sent.append({"key": item["key"], "file": filename})
                continue
            taken = taken_at(candidate)
            xmp = sidecar(describe(item, candidate, ai_filled), taken, candidate.get("lat"), candidate.get("lon"))
            answer = self.client.upload(payload, filename,
                                        taken or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        sidecar=xmp, checksum=checksum, device_fields=self.device_fields())
            entry = {"asset_id": answer["id"], "sha1": checksum, "file": filename, "status": answer.get("status"),
                     "ai_filled": ai_filled, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            album = self.album_id(data)
            if album:
                self.client.add_to_album(album, [answer["id"]])
                entry["album"] = album
            done.append(entry)
            self.save(data)
            self.log(f"{item['key']}: {answer.get('status', 'sent')} as Immich asset {answer['id']}")
            sent.append({"key": item["key"], **entry})
        return sent
