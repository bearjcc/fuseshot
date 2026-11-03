"""Short clips that come with a still: Pixel and Samsung motion photos and Apple Live Photos.

A Pixel .MP.jpg or a Samsung motion photo is a JPEG with an MP4 after the image; the
XMP says where it starts (GCamera:MicroVideoOffset, or a Container item with the
MotionPhoto semantic), and Samsung also marks it with MotionPhoto_Data. A Live Photo
is a HEIC or JPEG with a MOV of the same name beside it, made within a second or two.
Immich pairs both kinds itself (livePhotoVideoId), so for Immich photos that is all
there is to it.

Pairing is cheap: names and times from the cache, and the first 128 KB of a JPEG for
its XMP. Only a still that has a clip has its clip read, a few frames of it, to see
whether the camera stayed still while something moved (the same measure action.py
uses on bursts). Those are offered on the review page as loops. Nothing is encoded
until one is kept: the page plays the clip as it is (the bytes cut out of the JPEG,
or the MOV), and keeping it makes the loop files the usual way.
"""

import json
import os
import re
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from . import action, log
from .files import write_atomic
from .progress import Progress

LOG = log.get(__name__)

MEASURE_VERSION = 1
HEAD = 1 << 17
STILL_TYPES = (".jpg", ".jpeg", ".heic", ".heif")
EMBEDDED_TYPES = (".jpg", ".jpeg")
VIDEO_TYPES = (".mov", ".mp4", ".m4v")
CLIP_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4"}
# A Live Photo's MOV starts at most this long before or after the still was taken.
PAIR_S = 3.0
SAMPLES = 6
CHECK_WIDTH = 640
# Loops from a clip: about this many frames a second, at most this many frames.
LOOP_FPS = 12
LOOP_FRAMES = 36
MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
SEEN_SCHEMA = "CREATE TABLE IF NOT EXISTS motion_seen (id TEXT PRIMARY KEY, stamp TEXT, clip TEXT)"

MICRO_OFFSET = re.compile(rb'MicroVideoOffset(?:="|>)(\d+)')
CONTAINER_ITEM = re.compile(rb"<Container:Item\b[^>]*>", re.S)
ITEM_LENGTH = re.compile(rb'Item:Length="(\d+)"')
SAMSUNG_MARK = b"MotionPhoto_Data"
TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"udta", b"pnot",
             b"sidx", b"moof", b"mfra", b"styp", b"prfl"}


def clip_key(asset_id):
    return "motion-" + asset_id


def is_mp4(data):
    return len(data) >= 12 and data[4:8] == b"ftyp"


def embedded_range(path):
    """(start, end) of the MP4 inside a motion photo JPEG, or None."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(HEAD)
        if not head.startswith(b"\xff\xd8"):
            return None
        starts = []
        found = MICRO_OFFSET.search(head)
        if found:
            starts.append(size - int(found.group(1)))
        for item in CONTAINER_ITEM.findall(head):
            length = ITEM_LENGTH.search(item)
            if b"MotionPhoto" in item and length:
                starts.append(size - int(length.group(1)))
        for start in starts:
            if 0 < start < size - 12:
                f.seek(start)
                if is_mp4(f.read(12)):
                    return start, video_end(f, start, size)
        if not (starts or b"MotionPhoto" in head or b"MicroVideo" in head or ".MP." in path.name.upper()):
            # Older Samsung phones say nothing in the XMP; their mark is after the image.
            f.seek(0)
            data = f.read()
            mark = data.rfind(SAMSUNG_MARK)
            if mark > 0 and is_mp4(data[mark + len(SAMSUNG_MARK):mark + len(SAMSUNG_MARK) + 12]):
                start = mark + len(SAMSUNG_MARK)
                return start, video_end(f, start, size)
            return None
        # The XMP says there is a clip but not where: take the first MP4 after the image.
        f.seek(0)
        data = f.read()
        at = data.find(b"ftyp", data.find(b"\xff\xd9") + 2)
        while at > 4:
            if data[at - 4:at] != b"\x00\x00\x00\x00" and 8 <= struct.unpack(">I", data[at - 4:at])[0] <= 64:
                return at - 4, video_end(f, at - 4, size)
            at = data.find(b"ftyp", at + 4)
    return None


def video_end(f, start, size):
    """Where the MP4 boxes from start stop (a Samsung trailer can follow them)."""
    at = start
    while at + 8 <= size:
        f.seek(at)
        head = f.read(16)
        length, kind = struct.unpack(">I4s", head[:8])
        if kind not in TOP_BOXES:
            return at
        if length == 1 and len(head) == 16:
            length = struct.unpack(">Q", head[8:16])[0]
        elif length == 0:
            return size
        if length < 8:
            return at
        at += length
    return min(at, size)


def mov_time(path):
    """When a MOV or MP4 says it was made (mvhd, UTC), or None."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
    except OSError:
        return None
    at = data.find(b"mvhd")
    if at < 4 or at + 16 > len(data):
        return None
    version = data[at + 4]
    seconds = struct.unpack(">Q" if version == 1 else ">I", data[at + 8:at + (16 if version == 1 else 12)])[0]
    return MAC_EPOCH + timedelta(seconds=seconds) if seconds else None


def parse_utc(text):
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def companion(path, videos, taken_utc):
    """The MOV or MP4 of a Live Photo: same name, made within PAIR_S of the still.
    videos is {lower-case stem: file name} for the still's folder."""
    path = Path(path)
    name = videos.get(path.stem.lower())
    if not name:
        return None
    video = path.with_name(name)
    made, taken = mov_time(video), parse_utc(taken_utc)
    if made and taken and abs((made - taken).total_seconds()) > PAIR_S:
        return None
    return video


def folder_videos(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return {}
    return {os.path.splitext(n)[0].lower(): n for n in names if os.path.splitext(n)[1].lower() in VIDEO_TYPES}


def find_clips(db):
    """Stills with a clip, as {asset_id: (clip, row)}: an Immich live photo video, the
    MP4 inside a motion photo, or the MOV beside a Live Photo."""
    db.execute(SEEN_SCHEMA)
    has_files = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'source_files'").fetchone()
    query = ("SELECT a.id, a.file_name, a.taken_utc, a.local_time, a.make, a.model, a.live_photo_video_id, "
             + ("s.path, s.size, s.mtime, s.folder" if has_files else "NULL, NULL, NULL, NULL")
             + " FROM assets a" + (" LEFT JOIN source_files s ON s.id = a.id" if has_files else "")
             + " WHERE COALESCE(a.is_trashed, 0) = 0 AND COALESCE(a.type, 'IMAGE') = 'IMAGE'")
    seen = {row[0]: (row[1], row[2]) for row in db.execute("SELECT id, stamp, clip FROM motion_seen")}
    folders, found, looked = {}, {}, []
    for asset_id, name, taken, local, make, model, video_id, path, size, mtime, folder in db.execute(query).fetchall():
        row = {"file_name": name, "taken_utc": taken, "taken": local, "camera": f"{make or ''} {model or ''}".strip(),
               "library": "folder" if path else "immich"}
        if not path:
            if video_id:
                found[asset_id] = ({"kind": "immich", "video": video_id}, row)
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in STILL_TYPES:
            continue
        if folder not in folders:
            folders[folder] = folder_videos(folder)
        video = companion(path, folders[folder], taken)
        if video:
            found[asset_id] = ({"kind": "pair", "path": str(video)}, row)
            continue
        if suffix not in EMBEDDED_TYPES:
            continue
        stamp = f"{size}:{mtime}"
        if asset_id in seen and seen[asset_id][0] == stamp:
            clip = json.loads(seen[asset_id][1]) if seen[asset_id][1] else None
        else:
            try:
                span = embedded_range(path)
            except OSError:
                continue
            clip = {"kind": "embedded", "path": path, "start": span[0], "end": span[1]} if span else None
            looked.append((asset_id, stamp, json.dumps(clip) if clip else ""))
        if clip:
            found[asset_id] = (clip, row)
    if looked:
        db.executemany("INSERT OR REPLACE INTO motion_seen (id, stamp, clip) VALUES (?, ?, ?)", looked)
        db.commit()
    return found


def clip_suffix(clip):
    if clip["kind"] == "pair":
        return Path(clip["path"]).suffix.lower()
    return ".mp4"


def fetch_clip(library, clip, dest):
    """Put the clip at dest as it is: the bytes cut from the JPEG, the MOV copied, or
    the video Immich keeps for the photo. Nothing is encoded."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if clip["kind"] == "embedded":
        with open(clip["path"], "rb") as f:
            f.seek(clip["start"])
            write_atomic(dest, f.read(clip["end"] - clip["start"]))
    elif clip["kind"] == "pair":
        write_atomic(dest, Path(clip["path"]).read_bytes())
    else:
        library.download_original(clip["video"], dest)
    return dest


def read_frames(path, count=None, fps=None, limit=None, width=None):
    """Frames of a clip as BGR arrays: count spread over it, or about fps a second up to
    limit. Returns (frames, frames per second of what was returned)."""
    video = cv2.VideoCapture(str(path))
    try:
        total = int(video.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        rate = video.get(cv2.CAP_PROP_FPS) or 30.0
        frames = []
        while True:
            ok, frame = video.read()
            if not ok:
                break
            frames.append(frame)
            if total <= 0 and len(frames) > 600:
                break
    finally:
        video.release()
    if not frames:
        return [], rate
    if count:
        picks = sorted({round(i * (len(frames) - 1) / max(1, count - 1)) for i in range(count)})
        step = 1
    else:
        step = max(1, round(rate / (fps or rate)))
        picks = list(range(0, len(frames), step))[:limit or None]
    out = []
    for i in picks:
        frame = frames[i]
        if width and frame.shape[1] > width:
            frame = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])), interpolation=cv2.INTER_AREA)
        out.append(frame)
    return out, rate / step


def line_up(first, second):
    """ORB first, then SIFT, which still finds points in dark or soft frames (night skies)."""
    from .found import small_homography

    H = action.match_thumbs(first, second)[0]
    return H if H is not None else small_homography(first, second)


def check(frames):
    """The action check on frames from one clip: the camera held still while something moved."""
    grey = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    if len(grey) < 3:
        return {"action": False, "reason": "too few frames in the clip"}
    size = grey[0].shape[::-1]
    shifts, moving, missed = [], [], 0
    for first, second in zip(grey, grey[1:]):
        H = line_up(first, second)
        if H is None:
            # A burst or a flash can blank out one step; a few of those are fine.
            missed += 1
            if missed > (len(grey) - 1) // 3:
                return {"action": False, "reason": "frames do not line up"}
            continue
        shifts.append(action.corner_shift(H, size))
        share = action.moving_share(first, second, H)
        moving.append(share if share is not None else 0.0)
    H = line_up(grey[0], grey[-1])
    drift = action.corner_shift(H, size) if H is not None else 1.0
    result = {"shift": round(max(shifts), 4), "drift": round(drift, 4), "moving": round(float(np.median(moving)), 4)}
    if max(shifts) > action.MAX_SHIFT or drift > action.MAX_DRIFT:
        return {**result, "action": False, "reason": "the camera moved"}
    if result["moving"] < action.MIN_MEDIAN_MOVING:
        return {**result, "action": False, "reason": "nothing moves"}
    if result["moving"] > action.MAX_MOVING:
        return {**result, "action": False, "reason": "the whole frame changes"}
    return {**result, "action": True, "reason": f"still camera, {result['moving']:.0%} of the frame moving"}


def scan(db_path, library, cache_dir, out_file, log=LOG.info):
    """Check every still with a clip and write the result to out_file (motion.json)."""
    out_file = Path(out_file)
    old = json.loads(out_file.read_text(encoding="utf-8")).get("clips", {}) if out_file.exists() else {}
    db = sqlite3.connect(db_path)
    try:
        found = find_clips(db)
    finally:
        db.close()
    clips = {}
    with Progress("checking motion clips", len(found)) as bar:
        for asset_id, (clip, row) in sorted(found.items()):
            bar.update()
            key = clip_key(asset_id)
            before = old.get(key)
            if before and before.get("clip") == clip and before.get("version") == MEASURE_VERSION:
                clips[key] = before
                continue
            entry = {"asset_id": asset_id, "clip": clip, **row, "version": MEASURE_VERSION}
            try:
                path = fetch_clip(library, clip, Path(cache_dir) / "motion" / key[7:9] / (key + clip_suffix(clip)))
                frames, _ = read_frames(path, count=SAMPLES, width=CHECK_WIDTH)
                entry.update(check(frames))
                if frames:
                    entry["size"] = [int(frames[0].shape[1]), int(frames[0].shape[0])]
            except Exception as err:
                entry.update({"action": False, "reason": f"could not read the clip: {err}"})
            clips[key] = entry
    out_file.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out_file, json.dumps({"rules": action.rules(), "clips": clips}, indent=1))
    loops = sum(1 for c in clips.values() if c.get("action"))
    log(f"{len(clips)} stills with a clip, {loops} with a still camera and movement")
    return clips


def make_loop(clip_path, out_dir, info, log=LOG.info):
    """The loop files for a kept clip, made the way action.py makes them for bursts."""
    frames, rate = read_frames(clip_path, fps=LOOP_FPS, limit=LOOP_FRAMES, width=action.LOOP_WIDTH * 2)
    return action.loop_from_frames(frames, out_dir, delay=round(1000 / rate),
                                   extra={"asset_ids": [info["asset_id"]], "clip": info["clip"]["kind"]}, log=log)
