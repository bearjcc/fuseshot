"""Where photos come from: an Immich server or folders on disk, behind one interface.

Every source lists its photos as Immich-shaped assets (id, file name, EXIF, time,
GPS) and hands out originals and previews, so detection, stitching and review do not
care where a photo came from. Folders are only ever read: nothing is written beside
the originals.
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cache, config, formats, log, raw, takeout
from .progress import Progress
from .immich import Immich

LOG = log.get(__name__)

FOLDER_PREFIX = "file-"
# Where kept results from folder photos go when no output folder is set.
DEFAULT_OUTPUT = Path.home() / "Pictures" / "Fuseshot"
PHOTO_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp",
    ".jxl": "image/jxl", ".mpo": "image/mpo", **raw.RAW_TYPES,
}
PREVIEW_SIDES = {"preview": 1440, "thumbnail": 250}
FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    id TEXT PRIMARY KEY,
    source TEXT,
    path TEXT,
    size INTEGER,
    mtime REAL,
    content TEXT,
    folder TEXT,
    sidecar TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_files_source ON source_files (source);
"""
# Copies of one photo (Takeout puts each photo in its year folder and in every album
# it is in) are told apart by size and the first 64 KB, which hold the EXIF.
CONTENT_HEAD = 1 << 16
EARLIEST = datetime(1990, 1, 1)
OFFSET = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")
# Pixel names carry the moment in UTC to the millisecond: PXL_20240309_010003123.
PIXEL_NAME = re.compile(r"^PXL_(\d{8}_\d{6})(\d{3})", re.I)
# A date and time in a file name, as phones and apps write them: IMG_20240108_080218_268,
# IMG20240420181421, 20240629_161840, PANO_20241110_104217, Screenshot_2024-05-01-10-00-00.
NAME_TIME = re.compile(r"(?<!\d)((?:19|20)\d\d)-?([01]\d)-?([0-3]\d)[_ T-]?([0-2]\d)[-.]?([0-5]\d)[-.]?([0-5]\d)"
                       r"(?:[_.]?(\d{3}))?(?!\d)")


class SourceError(RuntimeError):
    pass


def is_folder_asset(asset_id):
    return str(asset_id).startswith(FOLDER_PREFIX)


def takeout_inside(folder):
    """The part of a folder's path from its Takeout folder on (Takeout/Google Photos/Photos
    from 2024), the same in every zip of one export; None outside a Takeout export."""
    parts = Path(folder).parts
    for n in range(len(parts) - 1, -1, -1):
        if parts[n].lower() == "takeout":
            return os.path.join(*parts[n:]).lower()
    return None


def same_or_inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def open_image(path):
    from PIL import Image

    formats.register()
    return Image.open(path)


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if value is None:
        return None
    value = str(value).strip("\x00 ").strip()
    return value or None


def _number(value):
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number == number else None


def _degrees(values, ref):
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    value = d + m / 60 + s / 3600
    return -value if _text(ref) in ("S", "W") else value


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def content_key(path, size):
    with open(path, "rb") as f:
        return f"{size}:{hashlib.sha1(f.read(CONTENT_HEAD)).hexdigest()}"


def stripped_stem(name):
    from .detect import base_stem

    return base_stem(name)


def plausible(moment):
    """A camera clock reading worth believing: not before 1990 and not in the future."""
    return moment is not None and EARLIEST <= moment <= datetime.now() + timedelta(days=2)


def read_exif(path):
    """(width, height, EXIF, EXIF sub-IFD, GPS IFD) for an ordinary photo or a camera raw."""
    if raw.is_raw(path):
        exif, sub, gps = raw.exif(path)
        try:
            width, height = raw.size(path)
        except Exception:
            width, height = _number(sub.get(0xA002)), _number(sub.get(0xA003))
        return width, height, exif, sub, gps
    with open_image(path) as img:
        width, height = img.size
        exif = img.getexif()
        return width, height, exif, exif.get_ifd(0x8769), exif.get_ifd(0x8825)


def exif_time(stamp):
    """An EXIF "YYYY:MM:DD HH:MM:SS" time, or None when missing or not believable."""
    try:
        moment = datetime.strptime((stamp or "")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return moment if plausible(moment) else None


def name_time(name):
    """The local time written in a file name, or None."""
    match = NAME_TIME.search(name)
    if not match:
        return None
    try:
        moment = datetime(*(int(g) for g in match.groups()[:6]), int(match.group(7) or 0) * 1000)
    except ValueError:
        return None
    return moment if plausible(moment) else None


def read_photo(path, sidecar=None):
    """Immich-shaped asset fields for one file. The time comes from the EXIF time
    taken, else a Google Takeout sidecar, else a time in the file name, else the
    EXIF time last saved, else the file time.

    As in Immich, a time without a zone is kept as written and treated as UTC, with
    no time zone recorded."""
    path = Path(path)
    stat = path.stat()
    width, height, exif, sub, gps = read_exif(path)
    extra = takeout.read(sidecar) if sidecar else {}

    stamp = _text(sub.get(0x9003)) or _text(sub.get(0x9004))
    local, zone = exif_time(stamp), None
    from_exif = local is not None
    saved = None
    if local is None:
        # The plain DateTime tag is when the file was last saved, which for a phone's
        # own panorama or an edit can be days away from the shot. A Takeout sidecar
        # or a time in the name is closer to the truth.
        saved = exif_time(_text(exif.get(0x0132)))
    if local is None and extra.get("taken"):
        # Takeout gives the moment in UTC; it is shown in this computer's zone.
        taken = extra["taken"]
        named = PIXEL_NAME.match(path.name)
        if named:
            exact = datetime.strptime(named.group(1), "%Y%m%d_%H%M%S").replace(
                microsecond=int(named.group(2)) * 1000, tzinfo=timezone.utc)
            if abs((exact - taken).total_seconds()) < 2:
                taken = exact  # the sidecar only has whole seconds; a burst needs better
        moment = taken.astimezone()
        local, zone, from_exif = moment.replace(tzinfo=None), moment.utcoffset(), True
    elif local is not None:
        fraction = _text(sub.get(0x9291))
        if fraction and fraction.isdigit():
            local = local.replace(microsecond=int((fraction + "000000")[:6]))
        match = OFFSET.match(_text(sub.get(0x9011)) or _text(sub.get(0x9010)) or "")
        zone = None
        if match:
            sign = -1 if match.group(1) == "-" else 1
            zone = sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    elif PIXEL_NAME.match(path.name):
        # Pixel names are in UTC; show them in this computer's zone, as with Takeout.
        named = PIXEL_NAME.match(path.name)
        moment = datetime.strptime(named.group(1), "%Y%m%d_%H%M%S").replace(
            microsecond=int(named.group(2)) * 1000, tzinfo=timezone.utc).astimezone()
        local, zone = moment.replace(tzinfo=None), moment.utcoffset()
    elif name_time(path.name) or saved:
        local = name_time(path.name) or saved
    else:
        moment = datetime.fromtimestamp(stat.st_mtime).astimezone()
        local, zone = moment.replace(tzinfo=None), moment.utcoffset()
    utc = local - zone if zone is not None else local
    zone_name = None
    if zone is not None:
        minutes = int(zone.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        hours, rest = divmod(abs(minutes), 60)
        zone_name = f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")

    lat = _degrees(gps.get(2), gps.get(1)) if gps.get(2) else None
    lon = _degrees(gps.get(4), gps.get(3)) if gps.get(4) else None
    if lat == 0 and lon == 0:
        lat = lon = None
    if lat is None and extra.get("place"):
        lat, lon = extra["place"]
    make, model = _text(exif.get(0x010F)), _text(exif.get(0x0110))
    if not make and extra:
        # Takeout copies can lose their EXIF; keep them apart from scans and screenshots.
        make, model = "Google Photos", (extra.get("device") or "phone").replace("_", " ").lower()
    orientation = exif.get(0x0112)
    exposure = _number(sub.get(0x829A))
    return {
        "type": "IMAGE",
        "visibility": "timeline",
        "originalFileName": path.name,
        "originalMimeType": PHOTO_TYPES.get(path.suffix.lower(), "image/" + path.suffix.lower().lstrip(".")),
        "fileCreatedAt": _iso(utc),
        "localDateTime": _iso(local),
        "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "width": width,
        "height": height,
        "isTrashed": False,
        "people": [],
        "exifInfo": {
            "dateTimeOriginal": _iso(utc) if from_exif else None,
            "timeZone": zone_name,
            "make": make,
            "model": model,
            "lensModel": _text(sub.get(0xA434)),
            "focalLength": _number(sub.get(0x920A)),
            "fNumber": _number(sub.get(0x829D)),
            "exposureTime": None if exposure is None else repr(exposure),
            "iso": _number(sub.get(0x8827)),
            "latitude": lat,
            "longitude": lon,
            "exifImageWidth": width,
            "exifImageHeight": height,
            "orientation": str(orientation) if orientation else None,
            "projectionType": None,
            "fileSizeInByte": stat.st_size,
        },
    }


def preview_bytes(path, side, quality=85):
    """A JPEG of the photo turned upright and scaled so its long side is at most side."""
    try:
        return _preview_bytes(path, side, quality)
    except (OSError, SyntaxError, ValueError) as err:
        raise SourceError(f"{path} could not be read, it may be damaged or cut short ({err})") from err


def _preview_bytes(path, side, quality):
    from PIL import ImageOps

    if raw.is_raw(path):
        img = raw.preview(path)
        img.thumbnail((side, side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()
    with open_image(path) as img:
        img.draft("RGB", (side, side))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((side, side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()


def read_key(key_file):
    """The Immich API key: FUSESHOT_IMMICH_KEY, else the key file. Never logged."""
    key = os.environ.get("FUSESHOT_IMMICH_KEY", "").strip()
    if not key and Path(key_file).is_file():
        key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        raise SourceError(f"no Immich API key: set FUSESHOT_IMMICH_KEY or put the key in {key_file}. "
                          "Make one in Immich under Account Settings > API Keys; read access is enough "
                          "unless uploading is turned on")
    return key


def web_url(args):
    """The Immich address for links people click, which may differ from the one Fuseshot calls."""
    if getattr(args, "web_url", None):
        return args.web_url.rstrip("/")
    for spec in config.settings().get("sources") or []:
        if spec.get("type") == "immich" and (spec.get("web_url") or spec.get("url")):
            return (spec.get("web_url") or spec["url"]).rstrip("/")
    return args.url.rstrip("/")


class ImmichSource:
    kind = "immich"

    def __init__(self, url, key_file):
        self.url = url
        self.key_file = key_file
        self._client = None

    @property
    def key(self):
        return "immich"

    def label(self):
        return "Immich"

    def client(self):
        if self._client is None:
            self._client = Immich(self.url, read_key(self.key_file))
        return self._client

    def iter_assets(self, visibility, updated_after=None):
        return self.client().iter_assets(visibility, updated_after=updated_after)

    def asset_info(self, asset_id):
        return self.client().asset_info(asset_id)

    def download_original(self, asset_id, dest):
        return self.client().download_original(asset_id, dest)

    def original_head(self, asset_id, length=1 << 18):
        return self.client().original_head(asset_id, length)

    def thumbnail(self, asset_id, size="preview"):
        return self.client().thumbnail(asset_id, size)


class FolderSource:
    """Photos in a folder on disk, and optionally the folders below it."""

    kind = "folder"

    def __init__(self, path, recursive=False, db_path=None):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self.recursive = bool(recursive)
        self.db_path = db_path
        self.unreadable = {}

    @property
    def key(self):
        return f"folder:{self.path}"

    def label(self):
        return f"Folder {self.path}" + (" and below" if self.recursive else "")

    @staticmethod
    def asset_id(file):
        digest = hashlib.sha1(os.path.normcase(os.path.abspath(file)).encode("utf-8")).hexdigest()
        return FOLDER_PREFIX + digest[:24]

    def folders(self, skip=()):
        """(folder, names in it) for each folder this source covers, in a steady order."""
        def cannot_open(err):
            LOG.warning(f"  could not open {err.filename}: {err.strerror}; its photos are left out")

        for root, dirs, names in os.walk(self.path, onerror=cannot_open):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and not any(same_or_inside(os.path.join(root, d), s) for s in skip))
            yield root, names
            if not self.recursive:
                break

    @staticmethod
    def photos(names):
        return sorted(n for n in names if not n.startswith(".") and os.path.splitext(n)[1].lower() in PHOTO_TYPES)

    def files(self, skip=()):
        for root, names in self.folders(skip):
            for name in self.photos(names):
                yield Path(root) / name

    def wants(self, file, skip=()):
        """True when a path is a photo this source would list."""
        file = Path(file)
        if file.suffix.lower() not in PHOTO_TYPES or file.name.startswith("."):
            return False
        if any(same_or_inside(file, s) for s in skip) or not same_or_inside(file, self.path):
            return False
        return self.recursive or os.path.normcase(os.path.abspath(file.parent)) == os.path.normcase(str(self.path))

    def _known(self, db, root):
        return {row[0]: row for row in db.execute(
            "SELECT path, id, size, mtime, sidecar FROM source_files WHERE source = ? AND folder = ?",
            (self.key, root))}

    @staticmethod
    def _sidecars(root, names, photos, other_parts):
        """{photo name: sidecar path} for one folder, looking in the other zip parts of a
        Takeout export for the ones not beside their photo."""
        beside = takeout.sidecars(root, names) if any(n.lower().endswith(".json") for n in names) else {}
        alone = [n for n in photos if n not in beside]
        for other, jsons in other_parts.get(takeout_inside(root), ()) if alone else ():
            if other != root:
                # A big export is split into zips, and a sidecar can land in another part.
                found = takeout.sidecars(other, jsons + alone)
                beside.update((n, found[n]) for n in alone if n in found and n not in beside)
        return beside

    @staticmethod
    def _stale(old, stat, sidecar):
        """True when a photo has to be read (again): new, changed, or its sidecar has
        turned up (or gone) since, as when Takeout zips are unpacked over some hours."""
        if not (old and old[2] == stat.st_size and old[3] == stat.st_mtime):
            return True
        return (old[4] or "") != (sidecar or "")

    def sync(self, db, skip=(), log=LOG.info):
        """Bring the cache up to date with the folder. Returns (added or changed, removed, failed).

        The folders are read one at a time and the cache is committed as it goes, so
        memory stays small for any size of library and a stopped sync picks up where
        it was. Photos already read and unchanged are only looked at, not opened."""
        if not self.path.is_dir():
            raise SourceError(f"{self.path} is not a folder that can be read")
        db.executescript(FILES_SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(source_files)")}
        for column in ("content", "folder", "sidecar"):
            if column not in columns:
                db.execute(f"ALTER TABLE source_files ADD COLUMN {column} TEXT")
        if "folder" not in columns:
            db.executemany("UPDATE source_files SET folder = ? WHERE id = ?",
                           [(os.path.dirname(path), i) for i, path in db.execute("SELECT id, path FROM source_files")])
        db.execute("CREATE INDEX IF NOT EXISTS idx_source_files_folder ON source_files (source, folder)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_source_files_content ON source_files (content)")
        db.commit()

        todo, total = [], 0
        other_parts = {}  # Takeout folder inside the export -> [(folder, sidecar names)] across zip parts
        with_photos = []
        for root, names in self.folders(skip):
            inside = takeout_inside(root)
            if inside is not None:
                jsons = [n for n in names if n.lower().endswith(".json")]
                if jsons:
                    other_parts.setdefault(inside, []).append((root, jsons))
            if any(os.path.splitext(n)[1].lower() in PHOTO_TYPES for n in names):
                with_photos.append(root)
        # All folders are listed before any is read, so a sidecar in a zip part that sorts
        # after its photo's part is still found.
        for root in with_photos:
            try:
                names = os.listdir(root)
            except OSError:
                continue
            photos = self.photos(names)
            if not photos:
                continue
            beside = self._sidecars(root, names, photos, other_parts)
            known = self._known(db, root)
            waiting = 0
            for name in photos:
                try:
                    stat = os.stat(os.path.join(root, name))
                except OSError:
                    continue
                total += 1
                if self._stale(known.get(os.path.join(root, name)), stat, beside.get(name)):
                    waiting += 1
            if waiting:
                todo.append((root, waiting))
        waiting = sum(n for _, n in todo)
        log(f"  {self.path}: {total:,} photos, {waiting:,} to read")

        changed = failed = 0
        with Progress(f"reading {self.path.name or self.path}", waiting) as bar:
            for root, _ in todo:
                try:
                    names = os.listdir(root)
                except OSError:
                    continue
                photos = self.photos(names)
                beside = self._sidecars(root, names, photos, other_parts)
                cameras = {}  # base name -> (make, model) of the photos here that kept their EXIF
                known = self._known(db, root)
                for name in photos:
                    file = Path(root) / name
                    text = str(file)
                    try:
                        stat = file.stat()
                    except OSError:
                        continue
                    if not self._stale(known.get(text), stat, beside.get(name)):
                        continue
                    bar.update()
                    if self.unreadable.get(text) == (stat.st_size, stat.st_mtime):
                        continue
                    try:
                        asset = read_photo(file, beside.get(name))
                        content = content_key(file, stat.st_size)
                    except Exception as err:
                        self.unreadable[text] = (stat.st_size, stat.st_mtime)
                        failed += 1
                        log(f"  skipped {file}: " + ("empty file" if stat.st_size == 0 else str(err)))
                        continue
                    stem = stripped_stem(name)
                    info = asset["exifInfo"]
                    if info["make"] == "Google Photos" and stem in cameras:
                        # A copy Takeout stripped (a raw saved as JPEG) beside the camera's own
                        # file: take that camera so the two are seen as one frame.
                        info["make"], info["model"] = cameras[stem]
                    elif info["make"] and info["make"] != "Google Photos":
                        cameras[stem] = (info["make"], info["model"])
                    asset["id"] = self.asset_id(file)
                    cache.upsert(db, [asset])
                    db.execute("INSERT OR REPLACE INTO source_files "
                               "(id, source, path, size, mtime, content, folder, sidecar) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (asset["id"], self.key, text, stat.st_size, stat.st_mtime, content, root,
                                beside.get(name) or ""))
                    changed += 1
                    if changed % 200 == 0:
                        db.commit()
                db.commit()

        removed = 0
        batch = []
        for path, asset_id in db.execute("SELECT path, id FROM source_files WHERE source = ?", (self.key,)):
            if not os.path.isfile(path) or not self.wants(path, skip):
                batch.append(asset_id)
        for asset_id in batch:
            db.execute("UPDATE assets SET is_trashed = 1 WHERE id = ?", (asset_id,))
            db.execute("DELETE FROM source_files WHERE id = ?", (asset_id,))
            removed += 1
        db.commit()
        return changed, removed, failed

    def path_of(self, asset_id):
        return find_file(self.db_path, asset_id)

    def asset_info(self, asset_id):
        path = self.path_of(asset_id)
        sidecar = find_sidecar(self.db_path, asset_id)
        asset = read_photo(path, sidecar if sidecar and Path(sidecar).is_file() else None)
        asset["id"] = asset_id
        return asset

    def download_original(self, asset_id, dest):
        """Copy the original into the work folder; the original itself is only read."""
        dest = Path(dest)
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(self.path_of(asset_id), tmp)
        tmp.replace(dest)
        return dest.stat().st_size

    def original_head(self, asset_id, length=1 << 18):
        with open(self.path_of(asset_id), "rb") as f:
            return 206, f.read(length)

    def thumbnail(self, asset_id, size="preview"):
        return preview_bytes(self.path_of(asset_id), PREVIEW_SIDES.get(size, PREVIEW_SIDES["preview"]))


def find_sidecar(db_path, asset_id):
    """The Takeout .json found for a photo when it was read, or None."""
    if not db_path or not Path(db_path).exists():
        return None
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT sidecar FROM source_files WHERE id = ?", (asset_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        db.close()
    return row[0] if row and row[0] else None


def find_file(db_path, asset_id):
    row = None
    if db_path and Path(db_path).exists():
        db = sqlite3.connect(db_path)
        try:
            row = db.execute("SELECT path FROM source_files WHERE id = ?", (asset_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            db.close()
    if row is None:
        raise SourceError(f"photo {asset_id} is not in any folder source; run sync")
    path = Path(row[0])
    if not path.is_file():
        raise SourceError(f"{path} is gone; run sync")
    return path


class Library:
    """Hands each request to the source its photo came from. Pass this wherever an
    Immich client used to go."""

    def __init__(self, sources, db_path):
        self.sources = sources
        self.db_path = db_path
        self.folders = FolderSource(".", db_path=db_path)

    def immich(self):
        for source in self.sources:
            if source.kind == "immich":
                return source
        raise SourceError("these photos are from Immich, but Immich is not one of the sources")

    def source_for(self, asset_id):
        return self.folders if is_folder_asset(asset_id) else self.immich()

    def asset_info(self, asset_id):
        return self.source_for(asset_id).asset_info(asset_id)

    def download_original(self, asset_id, dest):
        return self.source_for(asset_id).download_original(asset_id, dest)

    def original_head(self, asset_id, length=1 << 18):
        return self.source_for(asset_id).original_head(asset_id, length)

    def thumbnail(self, asset_id, size="preview"):
        return self.source_for(asset_id).thumbnail(asset_id, size)


class SourceOption(argparse.Action):
    """--source immich or --source folder:PATH, any number of times."""

    def __call__(self, parser, namespace, value, option_string=None):
        found = list(getattr(namespace, self.dest) or [])
        if value == "immich":
            found.append({"type": "immich"})
        elif value.startswith("folder:") and value[len("folder:"):].strip():
            found.append({"type": "folder", "path": value[len("folder:"):].strip(), "recursive": False})
        else:
            parser.error(f"--source takes 'immich' or 'folder:PATH', not {value!r}")
        setattr(namespace, self.dest, found)


class RecursiveOption(argparse.Action):
    """--recursive or --no-recursive, for the folder source just before it."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, value, option_string=None):
        found = getattr(namespace, self.dest) or []
        if not found or found[-1]["type"] != "folder":
            parser.error(f"{option_string} goes after a --source folder:PATH")
        found[-1]["recursive"] = option_string == "--recursive"


def add_options(parser):
    parser.add_argument("--source", dest="sources", action=SourceOption, metavar="SOURCE",
                        help="where photos come from: 'immich' or 'folder:PATH'; give it more than once "
                             "for several. Replaces the sources in the settings file")
    parser.add_argument("--recursive", "--no-recursive", dest="sources", action=RecursiveOption,
                        help="include the folders below the folder source just before it, or not (the default)")
    parser.add_argument("--output-folder", type=Path,
                        help="where kept results are copied (default: Pictures/Fuseshot in your user folder)")


def configured(args):
    """The sources and output folder from the command line, else the settings file,
    else Immich alone as before. The output folder defaults to Pictures/Fuseshot, so
    review and export find it even when the sources were only given to run."""
    settings = config.settings()
    specs = getattr(args, "sources", None) or settings.get("sources") or [{"type": "immich"}]
    sources = []
    for spec in specs:
        kind = spec.get("type")
        if kind == "immich":
            key_file = spec.get("key_file") or args.key_file or config.path("secrets", "immich_key")
            sources.append(ImmichSource(spec.get("url") or args.url, Path(key_file).expanduser()))
        elif kind == "folder" and spec.get("path"):
            sources.append(FolderSource(spec["path"], spec.get("recursive", False), args.db))
        else:
            raise SourceError(f"unknown source {spec!r}; use type = 'immich' or type = 'folder' with a path")
    output = getattr(args, "output_folder", None) or settings.get("output") or DEFAULT_OUTPUT
    output = Path(os.path.abspath(os.path.expanduser(str(output))))
    if output:
        for source in sources:
            if source.kind == "folder" and same_or_inside(output, source.path):
                raise SourceError(f"the output folder {output} would sit among the originals in {source.path}; "
                                  "pick a folder outside it with --output-folder or output in the settings file")
    return sources, output


def library(args):
    sources, _ = configured(args)
    return Library(sources, args.db)
