"""Local review page for stitched previews, action loops, group merges and splats.

Nothing here talks to Immich. Decisions are written to work/reviews.json,
stitching model overrides to work/overrides.json and set type overrides to
work/types.json.

Each set has a type that decides what is made from it: panoramas are stitched,
object scans become Gaussian splats (see splat.py), action shots become
stabilised loops and group shots become best-shot merges (see group.py). The
detected type comes from verify, output/groups.json and output/actions.json; a
type chosen on the page wins over it.

The front page is a grid and one-at-a-time viewer laid out like the Immich web
app (web/review.js), fed by /api/items. The older tables are kept under
/tables. Splats are shown with Spark and three.js, and equirectangular
previews in Photo Sphere Viewer, all served from tools/viewer, so no page needs
anything from a CDN.

The page can live under a base path (for example /fuseshot behind a reverse
proxy). Every link, image and API call is prefixed with it.
"""

import hmac
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from . import assets, config, jobs, log, settings_web, stitch, tools
from .events import Changes
from .files import read_text, write_atomic
from .found import Found

LOG = log.get(__name__)

SET_ID = re.compile(r"^[A-Za-z0-9_-]+$")
FILE_NAME = re.compile(r"^[a-z-]+(_crop|_fill|_fillshow)?\.jpg$")
VARIANTS = ("_crop", "_fill", "_fillshow")
LOOP_FILES = {"animation.webp": "image/webp", "animation.mp4": "video/mp4",
              "animation.gif": "image/gif", "poster.jpg": "image/jpeg", "composite.jpg": "image/jpeg"}
OVERRIDE_MODELS = ("auto", "rotation", "mosaic", "sfm")
SET_TYPES = ("panorama", "object scan", "action", "group", "not")
MAKES = {"panorama": "stitch", "object scan": "splat", "action": "loop", "group": "best-shot merge",
         "not": "nothing"}
GROUP_FILES = ("base.jpg", "merged.jpg", "overlay.jpg")
FACE_FILE = re.compile(r"^\d+_\d+\.jpg$")
DECISIONS = ("kept", "discarded")
# What older versions saved: approve is now keep, reject is discard, and needs work
# goes back to review (its note stays). Edits in progress are saved as "editing".
LEGACY = {"approved": "kept", "rejected": "discarded", "needs-work": None}
EDITING = "editing"
PAGE_SIZE = 200
UPLOAD_DELAY_S = 30
SIZES = {"thumb": (480, 240), "row": (1600, 400), "screen": (2400, 1400)}
SCALE_SLOTS = threading.BoundedSemaphore(2)
CHUNK = 256 * 1024
VENDOR_FILES = {
    "three/three.module.js": "text/javascript", "three/three.core.js": "text/javascript",
    "three/addons/controls/OrbitControls.js": "text/javascript",
    "three/addons/postprocessing/Pass.js": "text/javascript",
    "spark/spark.module.min.js": "text/javascript",
    "psv/index.module.js": "text/javascript", "psv/index.css": "text/css", "psv/LICENSE": "text/plain; charset=utf-8",
    "three/LICENSE": "text/plain; charset=utf-8", "spark/LICENSE": "text/plain; charset=utf-8",
    "VERSIONS.txt": "text/plain; charset=utf-8",
}
SPLAT_FILES = {"scene.splat": "application/octet-stream"}
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = {"review.css": "text/css; charset=utf-8", "review.js": "text/javascript; charset=utf-8",
             "icons.js": "text/javascript; charset=utf-8", "found.js": "text/javascript; charset=utf-8",
             "settings.js": "text/javascript; charset=utf-8"}
KINDS = ("stitch", "group", "action", "splat", "found")


def normalise_base(base):
    base = (base or "").strip().strip("/")
    return "/" + base if base else ""


def scaled_copy(source, target, size):
    """Write a smaller JPEG of source to target, reusing it while it is newer."""
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    from PIL import Image

    with SCALE_SLOTS:
        if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as im:
            im.draft("RGB", size)
            im = im.convert("RGB")
            im.thumbnail(size, Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            os.close(fd)
            try:
                im.save(tmp, "JPEG", quality=85, optimize=True, progressive=True)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    return target


def normalise_record(record):
    """One saved decision as this version names it, so older reviews.json files still read right."""
    old = record.get("decision")
    if old not in LEGACY:
        return record
    new = dict(record, decision=LEGACY[old])
    if new["decision"] is None:
        new["was"] = old
    return new


def normalise(data):
    return {key: [normalise_record(r) for r in records] for key, records in data.items()}


def migrate_reviews(path):
    """Rewrite reviews.json in the new names, after copying the old file to reviews.json.bak
    (or reviews.json.bak.2 and up, when a backup is already there). Returns how many records
    changed and the backup written, or (0, None) when there was nothing to change."""
    path = Path(path)
    if not path.exists():
        return 0, None
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    changed = sum(r.get("decision") in LEGACY for records in data.values() for r in records)
    if not changed:
        return 0, None
    backup = path.with_name(path.name + ".bak")
    n = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak.{n}")
        n += 1
    write_atomic(backup, raw)
    write_atomic(path, json.dumps(normalise(data), indent=2))
    return changed, backup


def discarded(work_dir, kind="stitch"):
    """Keys whose latest decision is discard, so later runs leave them alone."""
    path = Path(work_dir) / "reviews.json"
    if not path.exists():
        return set()
    found = set()
    for key, records in normalise(json.loads(path.read_text(encoding="utf-8"))).items():
        mine = [r for r in records if r.get("kind", "stitch") == kind]
        if mine and mine[-1].get("decision") == "discarded":
            found.add(key)
    return found


def decision_name(value):
    """A decision from the page, taking the old names too (a page loaded before an update)."""
    value = LEGACY.get(value, value)
    if value not in DECISIONS:
        raise ValueError("decision must be one of " + ", ".join(DECISIONS))
    return value


def base_script(base):
    from . import config

    return ("<script>const BASE = " + json.dumps(base) + "; const CROP = "
            + json.dumps(config.crop_default()) + ";</script>")


def view_choice(body):
    """The crop and AI fill picked for a panorama. uncropped is set only when the
    whole canvas was picked on purpose; older records without it get the default."""
    crop, fill = bool(body.get("crop")), bool(body.get("fill"))
    return {"crop": crop, "uncropped": "crop" in body and not crop and not fill, "fill": fill}

INDEX_STYLE = (
    "<style>body{font-family:sans-serif;background:#1b1b1b;color:#ddd;padding:16px}"
    "a{color:#9cf}td,th{padding:3px 10px;text-align:left;vertical-align:top}"
    "tr:nth-child(even){background:#222}.dim{color:#888}form{margin:8px 0 12px}"
    "select,input{background:#2a2a2a;color:#ddd;border:1px solid #444}"
    ".thumb{width:240px;max-height:120px;object-fit:cover;background:#111;display:block}</style>"
)

OVERRIDE_SCRIPT = """<script>
document.querySelectorAll("select[data-set]").forEach(s => s.addEventListener("change", async () => {
  const r = await fetch(BASE + "/override", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({set: s.dataset.set, model: s.value})});
  s.style.outline = r.ok ? "2px solid #2e7d32" : "2px solid #b71c1c";
}));
document.querySelectorAll("select[data-type-set]").forEach(s => s.addEventListener("change", async () => {
  const r = await fetch(BASE + "/type", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({set: s.dataset.typeSet, type: s.value})});
  s.style.outline = r.ok ? "2px solid #2e7d32" : "2px solid #b71c1c";
}));
</script>"""

LOOP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Fuseshot loop {key}</title>
<link rel="stylesheet" href="{base}/static/review.css?v={static_version}">
{base_script}
</head><body class="editor">
<div class="viewer ed">
  <div class="v-main">
    <div class="ed-stage"><img id="loop" alt="stabilised loop" src="{base}/loop/{key}/animation.webp?v={version}"></div>
    <div class="v-top">
      <a class="icon-btn" href="{base}/" title="Back to the review grid (Esc)" data-icon="arrowLeft"></a>
      <div class="v-title"><b>Loop {key}</b><span>{subtitle}</span></div>
      <div class="spacer"></div>
      {immich}
      <a class="icon-btn" href="{base}/tables#actions" title="Tables and candidate sets" data-icon="table"></a>
      <button class="icon-btn on" id="vinfo" title="Info (I)" data-icon="info"></button>
    </div>
    <div class="v-bottom"><div class="v-actions">
      <button class="act discard" data-decision="discarded" title="Discard (D): hidden from the queue and never exported; undo it from the Discarded list"><span data-icon="close"></span><span class="lbl">Discard</span></button>
      <button class="act keep" data-decision="kept" title="Keep (K): exported, and uploaded when uploads are on"><span data-icon="check"></span><span class="lbl">Keep</span></button>
    </div></div>
  </div>
  <aside id="vpanel" class="v-panel">
    <div class="p-head"><button class="icon-btn" id="pclose" title="Close (I)" data-icon="close"></button><h2>Info</h2></div>
    <div class="p-body">
      <div class="p-section"><div class="p-label">Decision</div><span id="status" class="p-chip {decision_class}">{decision}</span></div>
      <div class="p-section"><div class="p-label">Details</div>{rows}</div>
      <label class="p-field"><span>Type</span>{type_select}</label>
      <label class="p-field"><span>Note</span><textarea id="note" placeholder="Optional, saved with the decision"></textarea></label>
      <div class="p-section"><div class="p-label">Download</div><div class="p-links">{downloads}</div></div>
      <details class="p-section"><summary class="p-label">Action check</summary><pre class="ed-pre">{details}</pre></details>
      <p class="ed-foot">Fuseshot is free software under the AGPL. <a href="https://github.com/bearjcc/fuseshot">Source code</a></p>
    </div>
  </aside>
</div>
<div id="toasts" class="toasts" aria-live="polite"></div>
""" + OVERRIDE_SCRIPT.replace("{", "{{").replace("}", "}}") + """
<script type="module">
import {{ ICONS }} from "{base}/static/icons.js?v={static_version}";
document.querySelectorAll("[data-icon]").forEach(el => {{
  const d = ICONS[el.dataset.icon];
  if (d) el.insertAdjacentHTML("afterbegin", `<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="${{d}}"/></svg>`);
}});
const panel = document.getElementById("vpanel"), info = document.getElementById("vinfo");
const showPanel = on => {{ panel.hidden = !on; info.classList.toggle("on", on); }};
showPanel(!matchMedia("(max-width: 899px)").matches);
info.onclick = () => showPanel(panel.hidden);
document.getElementById("pclose").onclick = () => showPanel(false);
function toast(text, error) {{
  const t = document.createElement("div");
  t.className = "toast" + (error ? " error" : "");
  t.textContent = text;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4000);
}}
async function decide(decision) {{
  const r = await fetch(BASE + "/decision", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{set: {key_json}, kind: "action", decision, note: document.getElementById("note").value}})}});
  const data = await r.json().catch(() => ({{}}));
  const status = document.getElementById("status");
  if (r.ok) {{
    status.textContent = data.decision;
    status.className = "p-chip " + data.decision;
    toast(data.decision === "kept" ? "Kept" : "Discarded");
  }} else toast("Not saved: " + (data.error || r.statusText), true);
}}
document.querySelectorAll("[data-decision]").forEach(b => b.addEventListener("click", () => decide(b.dataset.decision)));
document.addEventListener("keydown", e => {{
  if (e.target.closest("textarea, input, select") || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key.toLowerCase();
  if (key === "k") decide("kept");
  else if (key === "d") decide("discarded");
  else if (key === "i") showPanel(panel.hidden);
  else if (key === "escape") location.href = BASE + "/";
}});
</script></body></html>
"""

GROUP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fuseshot group {set_id}</title>
<style>
body {{ font-family: sans-serif; margin: 0; background: #1b1b1b; color: #ddd; }}
header {{ padding: 10px 16px; background: #262626; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
a {{ color: #9cf; }}
main {{ padding: 12px 16px; }}
button {{ padding: 6px 14px; font-size: 15px; cursor: pointer; }}
select, input {{ background: #2a2a2a; color: #ddd; border: 1px solid #444; }}
.keep {{ background: #2e7d32; color: white; border: 0; }}
.discard {{ background: #b71c1c; color: white; border: 0; }}
#status, #queue {{ color: #fc6; }}
.pair {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.shot {{ flex: 1 1 560px; }}
.frame {{ position: relative; }}
.frame img {{ width: 100%; display: block; background: #111; }}
.box {{ position: absolute; border: 2px solid #ffd54f; cursor: pointer; box-sizing: border-box; }}
.box span {{ position: absolute; left: 0; top: -18px; font-size: 12px; background: #000a; padding: 0 3px; white-space: nowrap; }}
.box.swapped {{ border-color: #66bb6a; }}
.box.rejected {{ border-color: #ef5350; }}
.box.pending {{ border-style: dashed; }}
.people {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }}
.person {{ background: #232323; padding: 6px; }}
.person img {{ height: 80px; margin: 2px; cursor: pointer; border: 2px solid transparent; }}
.person img.chosen {{ border-color: #66bb6a; }}
.person img.pending {{ border-color: #fc6; }}
.dim {{ color: #888; font-size: 13px; }}
pre {{ background: #222; padding: 8px; overflow: auto; font-size: 12px; }}
</style></head><body>
{base_script}
<header>
<a href="{base}/">Review grid</a> <a href="{base}/tables#groups">Tables</a>
<strong>{set_id}</strong>
<span>Type {type_select}</span>
<input id="note" placeholder="note (optional)" size="28">
<button class="keep" data-decision="kept" title="exported, and uploaded when uploads are on">Keep</button>
<button class="discard" data-decision="discarded" title="hidden from the queue and never exported; undo it from the Discarded list">Discard</button>
<span id="status">{decision}</span>
</header>
<main>
<p>{summary}</p>
<p class="dim">Composite (best-shot merge): each face may come from a different frame of the burst. Click a face box
to cycle through that person's frames, or pick a crop below; each change queues a small re-merge.
Green boxes were swapped in, yellow are from the base frame, red were kept because the swap failed its checks.
<span id="queue">{queue}</span></p>
<div class="pair">
<div class="shot"><div>Base frame #{base_frame}</div><div class="frame" id="basebox"><img alt="base frame" src="{base}/groupfile/{set_id}/base.jpg?v={version}"></div></div>
<div class="shot"><div>Merged</div><div class="frame" id="mergedbox"><img alt="merged" src="{base}/groupfile/{set_id}/merged.jpg?v={version}"></div></div>
</div>
<div class="people" id="people"></div>
<details><summary>Merge record</summary><pre>{details}</pre></details>
<p class="muted" style="margin-top:2em;font-size:12px">Fuseshot is free software under the AGPL. <a href="https://github.com/bearjcc/fuseshot">Source code</a></p>
</main>
""" + OVERRIDE_SCRIPT.replace("{", "{{").replace("}", "}}") + """
<script>
const SET = {set_json}, RECORD = {record_json}, VERSION = {version};
let CHOICES = {choices_json};
const pct = (v, total) => (100 * v / total) + "%";
function chosen(p) {{
  const picked = (CHOICES.faces || {{}})[String(p.id)];
  return picked === undefined ? p.source_frame : Number(picked);
}}
function draw() {{
  const [W, H] = RECORD.size;
  for (const id of ["basebox", "mergedbox"]) {{
    const holder = document.getElementById(id);
    holder.querySelectorAll(".box").forEach(b => b.remove());
    for (const p of RECORD.people) {{
      const [x, y, w, h] = p.box, b = document.createElement("div");
      const now = chosen(p), pending = now !== p.source_frame;
      b.className = "box" + (p.swapped ? " swapped" : "") + (p.rejected ? " rejected" : "") + (pending ? " pending" : "");
      Object.assign(b.style, {{left: pct(x, W), top: pct(y, H), width: pct(w, W), height: pct(h, H)}});
      b.title = p.rejected ? "kept from the base: " + p.rejected : "face " + p.id + " from frame #" + (now + 1);
      b.innerHTML = "<span>" + p.id + ": #" + (now + 1) + (pending ? " (queued)" : "") + "</span>";
      if (id === "mergedbox") b.addEventListener("click", () => {{
        const frames = p.options.map(o => o.frame), i = frames.indexOf(now);
        choose(p.id, frames[(i + 1) % frames.length]);
      }});
      holder.appendChild(b);
    }}
  }}
  const people = document.getElementById("people");
  people.innerHTML = "";
  for (const p of RECORD.people) {{
    const div = document.createElement("div"), now = chosen(p);
    div.className = "person";
    div.innerHTML = "<div class='dim'>face " + p.id + (p.rejected ? " (swap failed: " + p.rejected + ")" : "") + "</div>";
    for (const o of p.options) {{
      const img = document.createElement("img");
      img.src = BASE + "/groupfile/" + SET + "/faces/" + p.id + "_" + o.frame + ".jpg?v=" + VERSION;
      img.title = "frame #" + (o.frame + 1) + ": score " + o.score.toFixed(2) + ", eyes " + o.eyes.toFixed(2)
        + ", turned " + Math.round(o.turn) + " deg, sharpness " + o.sharp.toFixed(2);
      img.className = o.frame === p.source_frame ? "chosen" : (o.frame === now ? "pending" : "");
      img.addEventListener("click", () => choose(p.id, o.frame));
      div.appendChild(img);
    }}
    people.appendChild(div);
  }}
}}
async function choose(person, frame) {{
  const r = await fetch(BASE + "/group/choose", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{set: SET, person: person, frame: frame}})}});
  const data = await r.json();
  if (!r.ok) {{ document.getElementById("queue").textContent = "failed: " + (data.error || r.statusText); return; }}
  CHOICES = data.choices;
  document.getElementById("queue").textContent = "Re-merge queued as task " + data.task + "; reload when it is done.";
  draw();
}}
draw();
document.querySelectorAll("[data-decision]").forEach(b => b.addEventListener("click", async () => {{
  const r = await fetch(BASE + "/decision", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{set: SET, kind: "group", decision: b.dataset.decision,
      note: document.getElementById("note").value}})}});
  const data = await r.json();
  document.getElementById("status").textContent = r.ok ? `${{data.decision}} (saved to reviews.json)`
    : "save failed: " + (data.error || r.statusText);
}}));
</script></body></html>
"""

SPLAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fuseshot splat {set_id}</title>
<style>
body {{ font-family: sans-serif; margin: 0; background: #1b1b1b; color: #ddd; }}
header {{ padding: 10px 16px; background: #262626; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
a {{ color: #9cf; }}
main {{ padding: 12px 16px; }}
#view {{ width: 100%; height: 74vh; display: block; background: #111; touch-action: none; }}
button {{ padding: 6px 14px; font-size: 15px; cursor: pointer; }}
select, input {{ background: #2a2a2a; color: #ddd; border: 1px solid #444; }}
.keep {{ background: #2e7d32; color: white; border: 0; }}
.discard {{ background: #b71c1c; color: white; border: 0; }}
#status, #loading {{ color: #fc6; }}
pre {{ background: #222; padding: 8px; overflow: auto; font-size: 12px; }}
</style>
<script type="importmap">{import_map}</script>
</head><body>
{base_script}
<header>
<a href="{base}/">Review grid</a> <a href="{base}/tables#splats">Tables</a>
<strong>{set_id}</strong>
<span>Type {type_select}</span>
<input id="note" placeholder="note (optional)" size="28">
<button class="keep" data-decision="kept" title="exported, and uploaded when uploads are on">Keep</button>
<button class="discard" data-decision="discarded" title="hidden from the queue and never exported; undo it from the Discarded list">Discard</button>
<span id="status">{decision}</span>
</header>
<main>
<p>{summary} <span id="loading">{loading}</span></p>
<canvas id="view"></canvas>
<p class="dim">Drag to orbit, right-drag or two fingers to pan, scroll or pinch to zoom.
<button id="reset">Reset view</button> <button id="spin">Auto-rotate</button>
<a href="{base}/splatfile/{set_id}/scene.splat?v={version}" download="{set_id}.splat">Download .splat</a></p>
<details><summary>SfM and training</summary><pre>{details}</pre></details>
<p class="muted" style="margin-top:2em;font-size:12px">Fuseshot is free software under the AGPL. <a href="https://github.com/bearjcc/fuseshot">Source code</a></p>
</main>
""" + OVERRIDE_SCRIPT.replace("{", "{{").replace("}", "}}") + """
<script>
document.querySelectorAll("[data-decision]").forEach(b => b.addEventListener("click", async () => {{
  const r = await fetch(BASE + "/decision", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{set: {set_id_json}, kind: "splat", decision: b.dataset.decision,
      note: document.getElementById("note").value}})}});
  const data = await r.json();
  document.getElementById("status").textContent = r.ok ? `${{data.decision}} (saved to reviews.json)`
    : "save failed: " + (data.error || r.statusText);
}}));
</script>
<script type="module">
import * as THREE from "three";
import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";
import {{ SparkRenderer, SplatMesh }} from "spark";

const view = {view_json};
const url = {url_json};
const canvas = document.getElementById("view");
const loading = document.getElementById("loading");
if (url) {{
  const renderer = new THREE.WebGLRenderer({{canvas, antialias: false}});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(view.fov || 50, 1, 0.01, 1000);
  scene.add(new SparkRenderer({{renderer}}));
  const start = () => {{
    camera.up.fromArray(view.up || [0, -1, 0]);
    camera.position.fromArray(view.position || [0, 0, -3]);
    camera.lookAt(new THREE.Vector3().fromArray(view.target || [0, 0, 0]));
  }};
  start();
  const controls = new OrbitControls(camera, canvas);
  controls.target.fromArray(view.target || [0, 0, 0]);
  controls.enableDamping = true;
  controls.update();
  const mesh = new SplatMesh({{url, fileType: "splat"}});
  scene.add(mesh);
  mesh.initialized.then(() => {{ loading.textContent = ""; }},
    err => {{ loading.textContent = "could not load the splat: " + err; }});
  document.getElementById("reset").onclick = () => {{
    start(); controls.target.fromArray(view.target || [0, 0, 0]); controls.update();
  }};
  document.getElementById("spin").onclick = () => {{ controls.autoRotate = !controls.autoRotate; }};
  const resize = () => {{
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }};
  window.addEventListener("resize", resize);
  resize();
  renderer.setAnimationLoop(() => {{ controls.update(); renderer.render(scene, camera); }});
  window.fuseshotSplat = {{renderer, scene, camera, controls, mesh}};
}}
</script></body></html>
"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fuseshot review {set_id}</title>
<style>
body {{ font-family: sans-serif; margin: 0; background: #1b1b1b; color: #ddd; }}
header {{ padding: 10px 16px; background: #262626; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
header a {{ color: #9cf; }}
main {{ padding: 12px 16px; }}
#preview {{ max-width: 100%; max-height: 78vh; display: block; margin: 0 auto; background: #111; cursor: zoom-in; }}
#viewer {{ position: fixed; inset: 0; background: #000; z-index: 10; overflow: hidden; touch-action: none; cursor: grab; }}
#viewer img {{ position: absolute; left: 0; top: 0; transform-origin: 0 0; max-width: none; user-select: none; -webkit-user-drag: none; }}
#viewer .bar {{ position: absolute; top: 8px; right: 12px; z-index: 11; display: flex; gap: 8px; align-items: center; }}
#viewer .bar a, #viewer .bar span {{ color: #9cf; background: #000a; padding: 4px 8px; }}
button {{ padding: 6px 14px; font-size: 15px; cursor: pointer; }}
.keep {{ background: #2e7d32; color: white; border: 0; }}
.discard {{ background: #b71c1c; color: white; border: 0; }}
#status {{ color: #fc6; }}
pre {{ background: #222; padding: 8px; overflow: auto; font-size: 12px; }}
</style></head><body>
{base_script}
<header>
<a href="{base}/">Review grid</a> <a href="{base}/tables">Tables</a>
<strong>{set_id}</strong>
<label>Projection <select id="projection">{projection_options}</select></label>
<label>Orientation <select id="orientation">{orientation_options}</select></label>
<label><input type="checkbox" id="crop"> Crop to fit</label>
<label title="Empty border painted in by a local AI model (LaMa); real pixels are kept"><input type="checkbox" id="fill"> Fill (AI)</label>
<label><input type="checkbox" id="fillshow" disabled> show filled areas</label>
<button id="queuefill" title="Queue an AI fill of this view on the GPU queue">Queue AI fill</button>
<button id="render">Render</button>
<input id="note" placeholder="note (optional)" size="28">
<button class="keep" data-decision="kept" title="exported, and uploaded when uploads are on">Keep</button>
<button class="discard" data-decision="discarded" title="hidden from the queue and never exported; undo it from the Discarded list">Discard</button>
<span id="status">{decision}</span>
</header>
<main>
<p>{verified}</p>
<img id="preview" alt="preview" title="Click to zoom">
<p id="fullsize" hidden><a id="fulllink" target="_blank">Open full size</a> <span class="dim">(click the image to zoom and drag)</span></p>
<p id="missing" hidden>Not rendered yet. Press Render (takes 15 to 60 seconds).</p>
<p id="nofill" hidden></p>
<details><summary>Alignment details</summary><pre>{layout}</pre></details>
<p class="muted" style="margin-top:2em;font-size:12px">Fuseshot is free software under the AGPL. <a href="https://github.com/bearjcc/fuseshot">Source code</a></p>
</main>
<div id="viewer" hidden><div class="bar"><span id="zoomlevel"></span><a id="viewerclose" href="#">Close (Esc)</a></div><img id="full" alt="full size"></div>
""" + OVERRIDE_SCRIPT.replace("{", "{{").replace("}", "}}") + """
<script>
const setId = {set_id_json};
let versions = {versions_json};
let fills = {fills_json};
$("crop").checked = CROP;
const $ = id => document.getElementById(id);
function name() {{
  const o = $("orientation").value;
  return $("projection").value + (o === "level" ? "" : "-" + o);
}}
function imageUrl(size) {{
  const n = name();
  const filled = $("fill").checked;
  const v = filled ? (fills[n] || {{}}).v : versions[n];
  const q = `v=${{v}}` + (size ? `&size=${{size}}` : "");
  const suffix = filled ? ($("fillshow").checked ? "_fillshow" : "_fill") : ($("crop").checked ? "_crop" : "");
  return `${{BASE}}/img/${{setId}}/${{n}}${{suffix}}.jpg?${{q}}`;
}}
function fillNote() {{
  const f = fills[name()] || {{}};
  if (f.state === "done") return `AI fill: ${{Math.round((f.share || 0) * 1000) / 10}}% of the picture painted by ${{f.model}}`
    + (f.stale ? " (made from an older render; queue it again)" : "") + ". Marked as AI-filled for upload.";
  if (f.state === "queued") return `AI fill queued (GPU task ${{f.task}}, ${{f.queued}})`;
  if (f.state === "failed") return `AI fill failed (GPU task ${{f.task}}); you can queue it again`;
  return "";
}}
function show() {{
  const filled = $("fill").checked;
  const f = fills[name()] || {{}};
  $("fillshow").disabled = !filled;
  $("nofill").hidden = !filled;
  $("nofill").textContent = fillNote() || "No AI fill for this view yet. Press Queue AI fill; it runs as its own job on the GPU queue.";
  $("queuefill").disabled = f.state === "queued" || versions[name()] === undefined;
  $("status").title = fillNote();
  const have = versions[name()] !== undefined && (!filled || f.state === "done");
  $("missing").hidden = have;
  $("preview").hidden = !have;
  $("fullsize").hidden = !have;
  if (have) {{
    $("preview").src = imageUrl("screen");
    $("fulllink").href = imageUrl("");
  }}
}}
const view = {{scale: 1, x: 0, y: 0}};
function place() {{
  $("full").style.transform = `translate(${{view.x}}px, ${{view.y}}px) scale(${{view.scale}})`;
  $("zoomlevel").textContent = Math.round(view.scale * 100) + "%";
}}
function fit() {{
  const img = $("full"), w = innerWidth, h = innerHeight;
  if (!img.naturalWidth) return;
  view.scale = Math.min(w / img.naturalWidth, h / img.naturalHeight, 1);
  view.x = (w - img.naturalWidth * view.scale) / 2;
  view.y = (h - img.naturalHeight * view.scale) / 2;
  place();
}}
function zoomAt(cx, cy, factor) {{
  const s = Math.min(8, Math.max(0.02, view.scale * factor));
  view.x = cx - (cx - view.x) * s / view.scale;
  view.y = cy - (cy - view.y) * s / view.scale;
  view.scale = s;
  place();
}}
function openViewer() {{
  $("viewer").hidden = false;
  $("zoomlevel").textContent = "loading...";
  const img = $("full");
  img.onload = fit;
  img.src = imageUrl("");
  if (img.complete && img.naturalWidth) fit();
}}
function closeViewer() {{ $("viewer").hidden = true; }}
$("preview").addEventListener("click", openViewer);
$("viewerclose").addEventListener("click", e => {{ e.preventDefault(); closeViewer(); }});
document.addEventListener("keydown", e => {{ if (e.key === "Escape") closeViewer(); }});
$("viewer").addEventListener("wheel", e => {{ e.preventDefault(); zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.2 : 1 / 1.2); }}, {{passive: false}});
$("viewer").addEventListener("dblclick", e => zoomAt(e.clientX, e.clientY, 2));
const pointers = new Map();
let pinch = 0;
$("viewer").addEventListener("pointerdown", e => {{
  if (e.target.tagName === "A") return;
  $("viewer").setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, {{x: e.clientX, y: e.clientY}});
}});
$("viewer").addEventListener("pointermove", e => {{
  const last = pointers.get(e.pointerId);
  if (!last) return;
  if (pointers.size === 1) {{
    view.x += e.clientX - last.x;
    view.y += e.clientY - last.y;
    place();
  }}
  pointers.set(e.pointerId, {{x: e.clientX, y: e.clientY}});
  if (pointers.size === 2) {{
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinch) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, d / pinch);
    pinch = d;
  }}
}});
["pointerup", "pointercancel"].forEach(t => $("viewer").addEventListener(t, e => {{
  pointers.delete(e.pointerId);
  pinch = 0;
}}));
async function post(url, body) {{
  const r = await fetch(BASE + url, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(body)}});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}}
$("crop").addEventListener("change", () => {{ if ($("crop").checked) $("fill").checked = false; show(); }});
$("fill").addEventListener("change", () => {{ if ($("fill").checked) $("crop").checked = false; show(); }});
["projection", "orientation", "fillshow"].forEach(id => $(id).addEventListener("change", show));
$("queuefill").addEventListener("click", async () => {{
  try {{
    const r = await post("/fill", {{set: setId, name: name()}});
    fills = r.fills;
    $("status").textContent = `AI fill queued as GPU task ${{r.task}}`;
    show();
  }} catch (e) {{ $("status").textContent = "queue failed: " + e.message; }}
}});
$("render").addEventListener("click", async () => {{
  $("status").textContent = "rendering...";
  try {{
    const info = await post("/render", {{set: setId, projection: $("projection").value, orientation: $("orientation").value}});
    versions = info.versions;
    $("status").textContent = `rendered in ${{info.seconds}} s`;
    show();
  }} catch (e) {{ $("status").textContent = "render failed: " + e.message; }}
}});
document.querySelectorAll("[data-decision]").forEach(b => b.addEventListener("click", async () => {{
  try {{
    const r = await post("/decision", {{set: setId, decision: b.dataset.decision, projection: $("projection").value,
      orientation: $("orientation").value, crop: $("crop").checked, fill: $("fill").checked, note: $("note").value}});
    $("status").textContent = `${{r.decision}} (saved to reviews.json)`;
  }} catch (e) {{ $("status").textContent = "save failed: " + e.message; }}
}}));
$("projection").value = {default_projection_json};
$("orientation").value = {default_orientation_json};
show();
</script></body></html>
"""


def coverage(c):
    if c.get("coverage_h"):
        return f"{c['coverage_h']}&times;{c['coverage_v']}&deg;"
    return ""


def byte_range(header, size):
    """(start, end) for a single "bytes=a-b" Range header, or None for the whole file."""
    found = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not found or not any(found.groups()) or size == 0:
        return None
    first, last = found.groups()
    if not first:
        start, end = max(0, size - int(last)), size
    else:
        start, end = int(first), min(size, int(last) + 1) if last else size
    return (start, end) if start < end else None


def library_label(c):
    if c.get("library") != "folder":
        return "Immich"
    return f"Folder {c.get('library_path')}" + (" and Immich" if c.get("library_mixed") else "")


def library_fields(c):
    """Where a set's photos came from, for the Info panel and the export of folder sets."""
    return {"library": c.get("library") or "immich", "library_label": library_label(c)}


class Review:
    def __init__(self, work_dir, candidates_file=None, base_path="", cache_dir=None, output_dir=None, library=None):
        self.work_dir = Path(work_dir)
        self.output_dir = Path(output_dir) if output_dir else None
        self.base = normalise_base(base_path)
        self.cache_dir = Path(cache_dir) if cache_dir else self.work_dir / "web"
        self.decisions_file = self.work_dir / "reviews.json"
        self.candidates_file = Path(candidates_file) if candidates_file else None
        self.overrides_file = self.work_dir / "overrides.json"
        self.types_file = self.work_dir / "types.json"
        self.actions_file = (self.candidates_file.parent if candidates_file else self.work_dir) / "actions.json"
        self.loops_dir = self.work_dir / "actions"
        self.groups_file = self.actions_file.with_name("groups.json")
        self.groups_dir = self.work_dir / "groups"
        self.groups_cache = (None, {})
        self.splats_dir = self.work_dir / "splats"
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.uploader = None
        self.sizes_cache = {}
        self.found = Found(self, library)
        self.library = library
        self.motion_file = self.actions_file.with_name("motion.json")
        self.motion_cache = (None, {})
        self.making = set()
        self.changes = None
        self.cli_sources = False
        self.reconfigure = None

    def needs_setup(self):
        return self.reconfigure is not None and settings_web.needs_setup(self.cli_sources)

    def settings_saved(self):
        """Point the found sets at the sources just saved."""
        if self.reconfigure is not None:
            self.library = self.found.library = self.reconfigure()

    def candidates(self):
        """Candidate sets, then the burst-mode runs kept beside them for the action check."""
        if not (self.candidates_file and self.candidates_file.exists()):
            return []
        found = json.loads(self.candidates_file.read_text(encoding="utf-8"))["candidates"]
        bursts = self.candidates_file.with_name("bursts.json")
        if bursts.exists():
            found += json.loads(bursts.read_text(encoding="utf-8"))["candidates"]
        return found

    def overrides(self):
        if self.overrides_file.exists():
            return json.loads(self.overrides_file.read_text(encoding="utf-8"))
        return {}

    def override(self, body):
        set_id = body.get("set")
        model = body.get("model")
        if not SET_ID.match(set_id or "") or model not in OVERRIDE_MODELS:
            raise ValueError("bad set or model")
        with self.lock:
            data = self.overrides()
            if model == "auto":
                data.pop(set_id, None)
            else:
                data[set_id] = {"model": model, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            write_atomic(self.overrides_file, json.dumps(data, indent=2))
        return {"set": set_id, "model": model}

    def type_overrides(self):
        if self.types_file.exists():
            return json.loads(self.types_file.read_text(encoding="utf-8"))
        return {}

    def set_type(self, body):
        set_id = body.get("set")
        chosen = body.get("type")
        if not SET_ID.match(set_id or "") or chosen not in SET_TYPES + ("auto",):
            raise ValueError("bad set or type")
        with self.lock:
            data = self.type_overrides()
            if chosen == "auto":
                data.pop(set_id, None)
            else:
                data[set_id] = {"type": chosen, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            write_atomic(self.types_file, json.dumps(data, indent=2))
        return {"set": set_id, "type": chosen}

    def actions(self):
        """Action check results for candidate sets and stitch jobs, keyed by set or job."""
        if not self.actions_file.exists():
            return {}
        data = json.loads(self.actions_file.read_text(encoding="utf-8"))
        return {**data.get("sets", {}), **data.get("jobs", {})}

    def groups(self):
        """Group photo check results by set, reread only when groups.json changes."""
        if not self.groups_file.exists():
            return {}
        stamp = self.groups_file.stat().st_mtime
        if self.groups_cache[0] != stamp:
            data = json.loads(self.groups_file.read_text(encoding="utf-8"))
            self.groups_cache = (stamp, data.get("sets", {}))
        return self.groups_cache[1]

    def detected_type(self, set_id, candidate, actions):
        if set_id.startswith(("motion-", "wiggle-")):
            return "action"
        if self.groups().get(set_id.split("-")[0], {}).get("group"):
            return "group"
        if actions.get(set_id, {}).get("action"):
            return "action"
        if candidate.get("source") == "burst":
            return "not"
        verified = (candidate.get("verified_type") or "").split(" (+")[0]
        if verified.startswith("object scan") or candidate.get("verified_splat"):
            return "object scan"
        if verified == "not a panorama":
            return "not"
        return "panorama"

    def type_select(self, set_id, detected, types):
        current = types.get(set_id, {}).get("type", "auto")
        label = lambda t: f"auto ({detected}: {MAKES[detected]})" if t == "auto" else f"{t}: {MAKES[t]}"
        options = "".join(f"<option value='{t}'{' selected' if t == current else ''}>{html.escape(label(t))}</option>"
                          for t in ("auto",) + SET_TYPES)
        return f"<select data-type-set='{html.escape(set_id)}'>{options}</select>"

    def loops(self):
        if not self.loops_dir.exists():
            return []
        return sorted(p.parent.name for p in self.loops_dir.glob("*/action.json"))

    def loop_dir(self, key):
        if not SET_ID.match(key or "") or not (self.loops_dir / key / "action.json").exists():
            raise KeyError(key)
        return self.loops_dir / key

    def motion_clips(self):
        """Stills whose clip (a motion photo or Live Photo) passed the action check, by key,
        reread only when motion.json changes."""
        if not self.motion_file.exists():
            return {}
        stamp = self.motion_file.stat().st_mtime
        if self.motion_cache[0] != stamp:
            data = json.loads(self.motion_file.read_text(encoding="utf-8")).get("clips", {})
            self.motion_cache = (stamp, {k: v for k, v in data.items() if v.get("action") and SET_ID.match(k)})
        return self.motion_cache[1]

    def loop_candidate(self, key, info=None, candidates=None):
        """The set a loop was made from: a found set, a still with a clip, or the photo a
        wigglegram keeps in its action.json."""
        found = (candidates or {}).get(key.split("-")[0]) or self.motion_candidate(key)
        if found:
            return found
        if info is None:
            info = self.read_json(self.loops_dir / key / "action.json")
        return info.get("candidate") or {}

    def motion_info(self, key):
        info = self.motion_clips().get(key)
        if not info:
            raise KeyError(key)
        return info

    def motion_candidate(self, key):
        """What a loop from a clip knows of its photo, in the shape of a candidate set."""
        info = self.motion_clips().get(key) or {}
        if not info:
            return {}
        path = info["clip"].get("path")
        local = (info.get("taken") or "")[:19].replace("T", " ") or None
        return {"set_id": key, "name": info.get("file_name"), "start_local": local, "start_utc": info.get("taken_utc"),
                "camera": info.get("camera") or "", "asset_ids": [info["asset_id"]], "library": info.get("library"),
                "library_path": str(Path(path).parent) if path else None, "clip": True,
                "reason": info.get("reason")}

    def clip_file(self, key):
        """The clip as it came: cut out of the motion photo or the MOV beside it."""
        from . import motion

        info = self.motion_info(key)
        return motion.fetch_clip(self.library, info["clip"],
                                 self.cache_dir / "motion" / (key + motion.clip_suffix(info["clip"])))

    def clip_still(self, key, size=None):
        info = self.motion_info(key)
        path = self.cache_dir / "motion" / f"{key}.jpg"
        if not path.exists():
            if self.library is None:
                raise KeyError(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, self.library.thumbnail(info["asset_id"], "preview"))
        if size in SIZES:
            return scaled_copy(path, self.cache_dir / size / "motion" / path.name, SIZES[size])
        return path

    def make_clip_loop(self, key):
        """Make the loop of a kept clip in the background, then export it like any loop."""
        from . import motion

        info = self.motion_info(key)
        with self.lock:
            if key in self.making or (self.loops_dir / key / "action.json").exists():
                return
            self.making.add(key)

        def run():
            try:
                motion.make_loop(self.clip_file(key), self.loops_dir / key, info,
                                 log=lambda m: LOG.info("loop %s", m))
            except Exception as err:
                LOG.info(f"{key}: no loop from the clip: {err}")
            finally:
                self.making.discard(key)
                if self.changes:
                    self.changes.touch()
            if (self.loops_dir / key / "action.json").exists():
                self.export_later({f"action:{key}"})

        threading.Thread(target=run, daemon=True).start()

    def loop_file(self, key, file_name, size=None):
        if file_name not in LOOP_FILES:
            raise KeyError(file_name)
        path = self.loop_dir(key) / file_name
        if not path.exists():
            raise KeyError(file_name)
        if size in SIZES and file_name == "poster.jpg":
            return scaled_copy(path, self.cache_dir / size / "loops" / key / file_name, SIZES[size])
        return path

    def model_select(self, set_id, overrides, plan):
        current = overrides.get(set_id, {}).get("model", "auto")
        auto = "auto" + (f" ({html.escape(plan)})" if plan else "")
        options = "".join(
            f"<option value='{m}'{' selected' if m == current else ''}>{auto if m == 'auto' else m}</option>"
            for m in OVERRIDE_MODELS)
        return f"<select data-set='{html.escape(set_id)}'>{options}</select>"

    def sets(self):
        return sorted(p.parent.name for p in self.work_dir.glob("*/optimised.pto"))

    def set_dir(self, set_id):
        if not SET_ID.match(set_id or "") or not (self.work_dir / set_id / "optimised.pto").exists():
            raise KeyError(set_id)
        return self.work_dir / set_id

    def rendered(self, set_id):
        previews = self.set_dir(set_id) / "previews"
        return sorted(p.stem for p in previews.glob("*.jpg") if not p.stem.endswith(VARIANTS))

    def versions(self, set_id):
        """Preview name -> a number that changes whenever the preview or its crop is re-rendered."""
        previews = self.set_dir(set_id) / "previews"
        found = {}
        for p in previews.glob("*.jpg"):
            if p.stem.endswith(("_fill", "_fillshow")):
                continue
            stem = p.stem.removesuffix("_crop")
            found[stem] = max(found.get(stem, 0), int(p.stat().st_mtime))
        return {k: v for k, v in found.items() if (previews / f"{k}.jpg").exists()}

    def fills(self, set_id):
        """Preview name -> AI fill state: done (with its version and share), queued or failed."""
        previews = self.set_dir(set_id) / "previews"
        renders_file = previews / "renders.json"
        renders = json.loads(renders_file.read_text(encoding="utf-8")) if renders_file.exists() else {}
        found = {}
        for name, entry in self.fill_queue(set_id).items():
            found[name] = {"state": entry.get("state"), "task": entry.get("task"), "queued": entry.get("queued")}
        for p in previews.glob("*_fill.jpg"):
            name = p.stem.removesuffix("_fill")
            info = (renders.get(name) or {}).get("fill") or {}
            source = previews / f"{name}.jpg"
            stale = source.exists() and source.stat().st_mtime > p.stat().st_mtime
            if stale and found.get(name, {}).get("state") == "queued":
                continue
            found[name] = {"state": "done", "v": int(p.stat().st_mtime), "share": info.get("filled_share"),
                           "model": info.get("model"), "seconds": info.get("seconds"), "stale": stale}
        return found

    def fill_queue(self, set_id):
        path = self.set_dir(set_id) / "fill_queue.json"
        queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        waiting = {k: v for k, v in queue.items() if v.get("state") == "queued" and v.get("task") is not None}
        for entry in waiting.values():
            if jobs.runner().status(entry["task"]) in ("failed", "lost"):
                entry["state"] = "failed"
        return queue

    def queue_fill(self, body):
        """Queue an AI fill of one preview as its own GPU job."""
        set_id = body.get("set")
        set_dir = self.set_dir(set_id)
        name = body.get("name") or ""
        if name not in self.rendered(set_id):
            raise ValueError("render this view first")
        with self.lock:
            queue = self.fill_queue(set_id)
            if queue.get(name, {}).get("state") == "queued":
                raise ValueError(f"already queued as task {queue[name].get('task')}")
            try:
                task = jobs.runner().submit("fill", f"fill-{set_id}-{name}", ["fill", set_id, "--name", name],
                                            gpu=True)
            except jobs.JobError as err:
                raise ValueError(str(err)) from None
            queue[name] = {"state": "queued", "task": task,
                           "queued": datetime.now().isoformat(timespec="seconds")}
            write_atomic(set_dir / "fill_queue.json", json.dumps(queue, indent=1))
        return {"fills": self.fills(set_id), "task": queue[name]["task"]}

    def batch_record(self, set_id):
        path = self.work_dir / set_id / "batch.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def default_preview(self, set_id):
        """The preview the batch run chose for this set, if it is still there."""
        name = Path(self.batch_record(set_id).get("preview_file") or "").stem
        return name if name and name in self.rendered(set_id) else None

    def thumb_name(self, set_id):
        rendered = self.rendered(set_id)
        chosen = self.default_preview(set_id)
        if chosen:
            return chosen
        for name in ("cylindrical", "cylindrical-centred"):
            if name in rendered:
                return name
        return rendered[0] if rendered else None

    def image_file(self, set_id, file_name, size=None):
        if not FILE_NAME.match(file_name or ""):
            raise KeyError(file_name)
        image = self.set_dir(set_id) / "previews" / file_name
        if not image.exists():
            raise KeyError(file_name)
        if size in SIZES:
            return scaled_copy(image, self.cache_dir / size / set_id / file_name, SIZES[size])
        return image

    def decisions(self):
        if self.decisions_file.exists():
            return normalise(json.loads(read_text(self.decisions_file)))
        return {}

    def migrate(self):
        """Bring reviews.json up to the current names once, keeping a backup of the old file."""
        with self.lock:
            changed, backup = migrate_reviews(self.decisions_file)
        if changed:
            LOG.info(f"reviews.json: {changed} decision(s) renamed (approve to keep, reject to discard, "
                     f"needs work back to review); the old file is in {backup.name}")
        return changed, backup

    def last_decision(self, decisions, set_id, kind):
        records = [r for r in decisions.get(set_id, []) if r.get("kind", "stitch") == kind]
        return records[-1] if records else {}

    def decide(self, body):
        set_id = body.get("set")
        kind = body.get("kind") if body.get("kind") in KINDS else "stitch"
        if kind == "found":
            self.found.check(set_id)
        elif kind == "action":
            if set_id not in self.motion_clips():
                self.loop_dir(set_id)
        elif kind == "group":
            self.group_dir(set_id)
        elif kind == "splat":
            self.splat_dir(set_id)
        else:
            self.set_dir(set_id)
        decision = decision_name(body.get("decision"))
        record = {
            "decision": decision,
            "projection": body.get("projection"),
            "orientation": body.get("orientation"),
            **view_choice(body),
            "note": str(body.get("note") or "")[:500],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if kind != "stitch":
            record = {"kind": kind, "decision": decision, "note": record["note"], "at": record["at"]}
        with self.lock:
            data = self.decisions()
            data.setdefault(set_id, []).append(record)
            write_atomic(self.decisions_file, json.dumps(data, indent=2))
        self.after_decisions([(set_id, record)])
        return record

    def after_decisions(self, records):
        """Export what was kept, and take sets discarded while found off the stitching queue."""
        kept = {f"{r.get('kind', 'stitch')}:{s}" for s, r in records
                if r["decision"] == "kept" and r.get("kind") != "found"}
        clips = self.motion_clips()
        for s, r in records:
            if r.get("kind") == "action" and r["decision"] == "kept" and s in clips \
                    and not (self.loops_dir / s / "action.json").exists():
                kept.discard(f"action:{s}")
                self.make_clip_loop(s)
        if kept:
            self.export_later(kept)
        dropped = [s for s, r in records if r.get("kind") == "found" and r["decision"] == "discarded"]
        if dropped:
            self.found.discarded(dropped)

    def export_later(self, keys):
        """Copy kept folder results to the output folder, and upload kept Immich
        results when uploading is on, without holding up the page."""
        if not self.output_dir and not self.uploader:
            return

        def run():
            from . import progress
            from .export import export
            if progress.STATE is None:
                # full-size renders show on the page the way a run's stages do
                progress.STATE = self.actions_file.with_name("progress.json")
            if self.output_dir:
                try:
                    with self.export_lock:
                        export(self, self.output_dir, keys, log=lambda m: LOG.info("export %s", m))
                except Exception as err:
                    LOG.info(f"export failed: {err}")
            if self.uploader:
                # Leave time for an undo; the upload checks the decision again before sending.
                time.sleep(UPLOAD_DELAY_S)
                try:
                    self.uploader.run(keys)
                except Exception as err:
                    LOG.info(f"upload failed: {err}")

        threading.Thread(target=run, daemon=True).start()

    def render(self, body):
        set_id = body.get("set")
        set_dir = self.set_dir(set_id)
        projection = body.get("projection")
        orientation = body.get("orientation")
        if projection not in stitch.PROJECTIONS or orientation not in stitch.ORIENTATIONS:
            raise ValueError("unknown projection or orientation")
        with self.render_lock:
            info = stitch.render(set_dir, projection, orientation)
        info["rendered"] = self.rendered(set_id)
        info["versions"] = self.versions(set_id)
        return info

    def edit(self, body):
        """Edit: make a new version of a stitched result with other settings, as a job.

        The version on screen now is kept for the B compare. Until the job ends the item is
        being edited; then it is back in the queue to review."""
        set_id = body.get("set")
        if (body.get("kind") or "stitch") != "stitch":
            raise ValueError("loops, group shots and splats are edited on their own page")
        set_dir = self.set_dir(set_id)
        projection, orientation = body.get("projection"), body.get("orientation")
        if projection not in stitch.PROJECTIONS or orientation not in stitch.ORIENTATIONS:
            raise ValueError("unknown projection or orientation")
        shown = body.get("from") or f"{self.thumb_name(set_id)}.jpg"
        if not FILE_NAME.match(shown) or not (set_dir / "previews" / shown).is_file():
            raise ValueError("nothing rendered to edit yet")
        model = self.overrides().get(set_id, {}).get("model", "auto")
        used = (self.read_json(set_dir / "layout.json").get("model") or {}).get("chosen")
        realign = model if model != "auto" and model != used else None
        if realign == "sfm":
            raise ValueError("structure-from-motion stitching is not built yet; pick rotation or mosaic")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            last = self.last_decision(self.decisions(), set_id, "stitch")
            if last.get("decision") == EDITING and self.edit_state(set_id, last)["state"] in ("queued", "running"):
                raise ValueError(f"already being edited (task {last.get('task')})")
            edit_dir = set_dir / "edit"
            edit_dir.mkdir(exist_ok=True)
            write_atomic(edit_dir / "previous.jpg", (set_dir / "previews" / shown).read_bytes())
            batch = self.batch_record(set_id)
            write_atomic(edit_dir / "previous.json", json.dumps(
                {"file": shown, "at": at, "error_px": batch.get("error_px"), "model": used,
                 "coverage": batch.get("coverage"), "status": batch.get("status")}, indent=1))
            args = ["edit", set_id, "--projection", projection, "--orientation", orientation, "--queued", at]
            if realign:
                args += ["--model", realign]
            if body.get("fill"):
                args.append("--fill")
            try:
                task = jobs.runner().submit("edit", f"edit-{set_id}", args)
            except jobs.JobError as err:
                raise ValueError(str(err)) from None
            record = {"decision": EDITING, "task": task, "projection": projection, "orientation": orientation,
                      "model": realign, **view_choice(body),
                      "note": str(body.get("note") or "")[:500], "at": at}
            data = self.decisions()
            data.setdefault(set_id, []).append(record)
            write_atomic(self.decisions_file, json.dumps(data, indent=2))
        return {"record": record, "task": task}

    def edit_state(self, set_id, record):
        """Where an edit stands: queued or running, then done or failed."""
        found = {"task": record.get("task"), "at": record.get("at"),
                 "projection": record.get("projection"), "orientation": record.get("orientation"),
                 "crop": bool(record.get("crop")), "fill": bool(record.get("fill"))}
        done = self.read_json(self.work_dir / set_id / "edit" / "done.json")
        if done.get("queued") == record.get("at") and done.get("state") in ("done", "failed"):
            found.update(state=done["state"], error=done.get("error"), finished=done.get("finished"))
            return found
        try:
            state = jobs.runner().status(record.get("task"))
        except (jobs.JobError, TypeError, ValueError):
            state = None
        if state == "done":
            state = "failed"  # it ended without saying so
        found["state"] = state if state in ("queued", "running") else "failed"
        if found["state"] == "failed":
            found["error"] = "the job stopped before it finished; its log is in cache/jobs or Pueue"
        return found

    def previous_file(self, set_id, file_name, size=None):
        if file_name != "previous.jpg":
            raise KeyError(file_name)
        path = self.set_dir(set_id) / "edit" / file_name
        if not path.exists():
            raise KeyError(file_name)
        if size in SIZES:
            return scaled_copy(path, self.cache_dir / size / set_id / "edit" / file_name, SIZES[size])
        return path

    def stitched_table(self):
        decisions = self.decisions()
        actions, types = self.actions(), self.type_overrides()
        candidates = {c["set_id"]: c for c in self.candidates()}
        loops = set(self.loops())
        rows = []
        for set_id in self.sets():
            layout_file = self.work_dir / set_id / "layout.json"
            layout = json.loads(layout_file.read_text(encoding="utf-8")) if layout_file.exists() else {}
            last = self.last_decision(decisions, set_id, "stitch").get("decision") or "to review"
            model = layout.get("model", {})
            chosen = model.get("chosen") or ("mosaic" if layout.get("mosaic") else "rotation")
            detected = self.detected_type(set_id, candidates.get(set_id.split("-")[0], {}), actions)
            kind = self.type_select(set_id, detected, types)
            if set_id in loops:
                kind += f" <a href='{self.base}/action/{set_id}'>loop</a>"
            thumb = self.thumb_name(set_id)
            href = f"{self.base}/set/{set_id}"
            image = (f"<a href='{href}'><img class='thumb' loading='lazy' alt='' "
                     f"src='{self.base}/img/{set_id}/{thumb}.jpg?size=thumb'></a>") if thumb else ""
            rows.append(
                f"<tr><td>{image}</td><td><a href='{href}'>{set_id}</a></td><td>{layout.get('images', '?')}</td>"
                f"<td>{html.escape(str(layout.get('mean_error_px', '')))}</td>"
                f"<td>{len(layout.get('connected_groups', []))}</td>"
                f"<td>{html.escape(chosen)} <span class='dim'>{html.escape(model.get('reason', ''))}</span></td>"
                f"<td>{kind}</td><td>{last}</td></tr>"
            )
        if not rows:
            return ""
        return (
            "<h3>Stitched sets</h3><table><tr><th></th><th>Set</th><th>Images</th><th>Mean control point error (px)</th>"
            "<th>Image groups</th><th>Model used</th><th>Type</th><th>Decision</th></tr>" + "".join(rows) + "</table>"
        )

    def loops_table(self):
        keys = self.loops()
        if not keys:
            return ""
        decisions, actions, types = self.decisions(), self.actions(), self.type_overrides()
        candidates = {c["set_id"]: c for c in self.candidates()}
        rows = []
        for key in keys:
            info = json.loads((self.loops_dir / key / "action.json").read_text(encoding="utf-8"))
            c = self.loop_candidate(key, info, candidates)
            href = f"{self.base}/action/{key}"
            image = (f"<a href='{href}'><img class='thumb' loading='lazy' alt='' "
                     f"src='{self.base}/loop/{key}/poster.jpg?size=thumb'></a>")
            last = self.last_decision(decisions, key, "action").get("decision") or "to review"
            detected = self.detected_type(key, c, actions)
            rows.append(
                f"<tr><td>{image}</td><td><a href='{href}'>{key}</a></td>"
                f"<td>{html.escape((c.get('start_local') or '')[:16])}</td><td>{html.escape(c.get('camera', ''))}</td>"
                f"<td>{info['frames']}</td><td>{html.escape(actions.get(key, {}).get('reason', ''))}</td>"
                f"<td>{self.type_select(key, detected, types)}</td><td>{last}</td></tr>")
        return (
            "<h3 id='actions'>Action shots</h3><p class='dim'>Bursts from a still camera with something moving, "
            "registered on the background and played as a loop.</p><table><tr><th></th><th>Set</th><th>Taken</th>"
            "<th>Camera</th><th>Frames</th><th>Why</th><th>Type</th><th>Decision</th></tr>"
            + "".join(rows) + "</table>"
        )

    def loop_page(self, key):
        loop_dir = self.loop_dir(key)
        info = json.loads((loop_dir / "action.json").read_text(encoding="utf-8"))
        actions = self.actions()
        c = self.loop_candidate(key, info, {x["set_id"]: x for x in self.candidates()})
        check = actions.get(key, {})
        last = self.last_decision(self.decisions(), key, "action").get("decision")
        taken = (c.get("start_local") or "")[:16].replace("T", " ")
        subtitle = " \u00b7 ".join(x for x in (taken, c.get("camera", ""), f"{info['frames']} frames") if x)

        def row(icon, text, small=""):
            small = f"<small>{html.escape(small)}</small>" if small else ""
            return (f"<div class='p-row'><span data-icon='{icon}'></span>"
                    f"<div class='t'><span>{html.escape(str(text))}</span>{small}</div></div>")

        rows = row("frames", f"{info['frames']} frames", "registered on the background and played as a loop")
        if taken:
            rows += row("calendar", taken)
        if c.get("camera"):
            rows += row("camera", c["camera"])
        if check.get("reason"):
            rows += row("run", check["reason"], "why it looks like an action shot")
        link = c.get("best_link") or c.get("first_link")
        immich = (f"<a class='icon-btn' target='_blank' rel='noopener' href='{html.escape(link)}' "
                  "title='Open in Immich' data-icon='openInNew'></a>") if link else ""
        version = int((loop_dir / "animation.webp").stat().st_mtime) if (loop_dir / "animation.webp").exists() else 0
        downloads = "".join(
            f"<a href='{self.base}/loop/{key}/{name}?v={version}' target='_blank' data-icon='openInNew'>"
            f"<span>{'Composite' if name == 'composite.jpg' else name.split('.')[1].upper()}</span></a>"
            for name in ("animation.webp", "animation.mp4", "animation.gif", "composite.jpg") if (loop_dir / name).exists())
        details = {k: v for k, v in check.items() if k != "pairs"}
        static_version = max(int((WEB_DIR / name).stat().st_mtime) for name in WEB_FILES)
        return LOOP_PAGE.format(
            base=html.escape(self.base), base_script=base_script(self.base), key=html.escape(key),
            key_json=json.dumps(key), static_version=static_version, subtitle=html.escape(subtitle), immich=immich,
            type_select=self.type_select(key, self.detected_type(key, c, actions), self.type_overrides()),
            decision=html.escape(last or "to review"), decision_class=html.escape(last or "none"), rows=rows,
            version=version, downloads=downloads or "<span class='p-label'>none yet</span>",
            details=html.escape(json.dumps({"loop": info, "check": details}, indent=1)))

    def merges(self):
        if not self.groups_dir.exists():
            return []
        return sorted(p.parent.name for p in self.groups_dir.glob("*/group.json"))

    def group_dir(self, set_id):
        if not SET_ID.match(set_id or "") or not (self.groups_dir / set_id / "group.json").exists():
            raise KeyError(set_id)
        return self.groups_dir / set_id

    def group_file(self, set_id, parts, size=None):
        if len(parts) == 1 and parts[0] in GROUP_FILES:
            path = self.group_dir(set_id) / parts[0]
        elif len(parts) == 2 and parts[0] == "faces" and FACE_FILE.match(parts[1]):
            path = self.group_dir(set_id) / "faces" / parts[1]
        else:
            raise KeyError("/".join(parts))
        if not path.exists():
            raise KeyError("/".join(parts))
        if size in SIZES and parts[0] == "merged.jpg":
            return scaled_copy(path, self.cache_dir / size / "groups" / set_id / parts[0], SIZES[size])
        return path

    def group_choices(self, set_id):
        path = self.group_dir(set_id) / "choices.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def remerge_task(self, set_id):
        """The queued or running re-merge for a set, if there is one."""
        return jobs.runner().active(f"group-{set_id}")

    def choose_face(self, body):
        """Save a face picked by hand and queue a small CPU job to redo the merge."""
        set_id = body.get("set")
        group_dir = self.group_dir(set_id)
        record = json.loads((group_dir / "group.json").read_text(encoding="utf-8"))
        person = next((p for p in record["people"] if str(p["id"]) == str(body.get("person"))), None)
        if person is None:
            raise ValueError("unknown face")
        frame = body.get("frame")
        if not isinstance(frame, int) or frame not in [o["frame"] for o in person["options"]]:
            raise ValueError("that face is not in that frame")
        with self.lock:
            choices = self.group_choices(set_id)
            choices.setdefault("faces", {})[str(person["id"])] = frame
            write_atomic(group_dir / "choices.json", json.dumps(choices, indent=1))
            waiting = self.remerge_task(set_id)
            if waiting and waiting[1] != "running":
                return {"set": set_id, "choices": choices, "task": waiting[0]}
            try:
                task = jobs.runner().submit("group", f"group-{set_id}", ["group", set_id])
            except jobs.JobError as err:
                raise ValueError(str(err)) from None
        return {"set": set_id, "choices": choices, "task": task}

    def groups_table(self):
        found = self.groups()
        merged = self.merges()
        keys = sorted(set(merged) | {k for k, v in found.items() if v.get("group")})
        if not keys:
            return ""
        decisions, actions, types = self.decisions(), self.actions(), self.type_overrides()
        candidates = {c["set_id"]: c for c in self.candidates()}
        rows = []
        for key in keys:
            c, check = candidates.get(key, {}), found.get(key, {})
            if key in merged:
                info = json.loads((self.groups_dir / key / "group.json").read_text(encoding="utf-8"))
                href = f"{self.base}/group/{key}"
                image = (f"<a href='{href}'><img class='thumb' loading='lazy' alt='' "
                         f"src='{self.base}/groupfile/{key}/merged.jpg?size=thumb'></a>")
                name = f"<a href='{href}'>{key}</a>"
                people = f"{len(info['people'])} ({info['swapped']} swapped)"
            else:
                image, name, people = "", f"<a href='{self.base}/set/{key}'>{key}</a>", "not merged yet"
            last = self.last_decision(decisions, key, "group").get("decision") or "to review"
            rows.append(
                f"<tr><td>{image}</td><td>{name}</td>"
                f"<td>{html.escape((c.get('start_local') or '')[:16])}</td><td>{html.escape(c.get('camera', ''))}</td>"
                f"<td>{people}</td><td>{html.escape(check.get('reason', ''))}</td>"
                f"<td>{self.type_select(key, self.detected_type(key, c, actions), types)}</td><td>{last}</td></tr>")
        return (
            "<h3 id='groups'>Group shots</h3><p class='dim'>Several people from a still camera. Each becomes one "
            "composite with the best face of every person (eyes open, facing forward, sharp) moved onto the "
            "best base frame. Nothing is uploaded.</p><table><tr><th></th><th>Set</th><th>Taken</th>"
            "<th>Camera</th><th>People</th><th>Why</th><th>Type</th><th>Decision</th></tr>"
            + "".join(rows) + "</table>"
        )

    def group_page(self, set_id):
        group_dir = self.group_dir(set_id)
        record = json.loads((group_dir / "group.json").read_text(encoding="utf-8"))
        c = next((x for x in self.candidates() if x["set_id"] == set_id), {})
        check = self.groups().get(set_id, {})
        last = self.last_decision(self.decisions(), set_id, "group")
        decision = f"last decision: {last['decision']}" if last.get("decision") else "to review"
        rejected = sum(bool(p.get("rejected")) for p in record["people"])
        summary = (f"{len(record['people'])} people in {len(record['frames'])} frames, {record['swapped']} faces "
                   f"swapped in, {rejected} kept from the base. {html.escape(c.get('camera', ''))}, "
                   f"{html.escape((c.get('start_local') or '')[:16])}. {html.escape(check.get('reason', ''))}.")
        if c.get("best_link") or c.get("first_link"):
            summary += f" <a target='_blank' href='{html.escape(c.get('best_link') or c['first_link'])}'>Immich</a>"
        summary += (f" <a target='_blank' href='{self.base}/groupfile/{set_id}/merged.jpg'>full size</a>"
                    f" &middot; <a target='_blank' href='{self.base}/groupfile/{set_id}/overlay.jpg'>face sources</a>")
        waiting = self.remerge_task(set_id)
        queue = f"Re-merge {waiting[1].lower()} as task {waiting[0]}." if waiting else ""
        version = int((group_dir / "merged.jpg").stat().st_mtime)
        return GROUP_PAGE.format(
            base=self.base, base_script=base_script(self.base), set_id=html.escape(set_id),
            set_json=json.dumps(set_id), record_json=json.dumps(record).replace("</", "<\\/"),
            choices_json=json.dumps(self.group_choices(set_id)), version=version,
            type_select=self.type_select(set_id, self.detected_type(set_id, c, self.actions()),
                                         self.type_overrides()),
            decision=html.escape(decision), summary=summary, queue=queue, base_frame=record["base_frame"] + 1,
            details=html.escape(json.dumps({k: v for k, v in record.items() if k != "people"}, indent=1)))

    def splat_plan(self):
        path = self.work_dir / "splat_plan.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def splat_dir(self, set_id):
        if not SET_ID.match(set_id or "") or not (self.splats_dir / set_id / "frames.json").exists():
            raise KeyError(set_id)
        return self.splats_dir / set_id

    def splat_records(self, set_id):
        out = self.splats_dir / set_id
        read = lambda name: json.loads((out / name).read_text(encoding="utf-8")) if (out / name).exists() else {}
        return read("sfm.json"), read("splat.json")

    def splat_file(self, set_id, file_name):
        if file_name not in SPLAT_FILES:
            raise KeyError(file_name)
        path = self.splat_dir(set_id) / file_name
        if not path.exists():
            raise KeyError(file_name)
        return path

    def vendor_file(self, rel):
        path = assets.viewer_dir() / rel
        if rel not in VENDOR_FILES or not path.is_file():
            raise KeyError(rel)
        return path

    def splat_state(self, sfm, trained, skip):
        if trained.get("steps"):
            return "trained"
        if sfm.get("status") == "failed":
            return "failed the SfM check"
        if sfm.get("status") == "passed":
            return "posed, waiting for training"
        return "skipped" if skip else "waiting for SfM"

    def splats_table(self):
        plan = self.splat_plan()
        if not plan.get("sets"):
            return ""
        decisions, actions, types = self.decisions(), self.actions(), self.type_overrides()
        candidates = {c["set_id"]: c for c in self.candidates()}
        gate = plan.get("gate", {})
        rows, skipped = [], []
        for row in plan["sets"]:
            set_id = row["set_id"]
            sfm, trained = self.splat_records(set_id)
            if row.get("skip") and not sfm:
                skipped.append(f"{html.escape(set_id)} ({html.escape(row['skip'])})")
                continue
            state = self.splat_state(sfm, trained, row.get("skip"))
            name = html.escape(set_id)
            if trained.get("steps") and (self.splats_dir / set_id / "scene.splat").exists():
                name = f"<a href='{self.base}/splat/{name}'>{name}</a>"
            why = html.escape(sfm.get("reason", ""))
            if sfm.get("status") == "failed":
                why = f"<span style='color:#f88'>{why}</span>"
            registered = f"{sfm['registered']}/{sfm['images']}" if sfm.get("registered") is not None else ""
            error = sfm.get("mean_reprojection_error_px")
            minutes = f"{trained['train_seconds'] / 60:.0f} min" if trained.get("train_seconds") else ""
            size = (f"{trained['splat_mb']} MB, {trained['web_gaussians'] / 1e6:.2f}M splats"
                    if trained.get("splat_mb") else "")
            last = self.last_decision(decisions, set_id, "splat").get("decision") or "to review"
            detected = self.detected_type(set_id, candidates.get(set_id, {}), actions)
            rows.append(
                f"<tr><td>{name}</td><td>{html.escape(row['kind'])}</td><td>{html.escape(row.get('taken', ''))}</td>"
                f"<td>{html.escape(row.get('camera', ''))}</td><td>{row['usable']}</td><td>{state} "
                f"<span class='dim'>{why}</span></td><td>{registered}</td>"
                f"<td>{'' if error is None else f'{error:.2f}'}</td><td>{minutes}</td><td>{size}</td>"
                f"<td>{self.type_select(set_id, detected, types)}</td><td>{last}</td></tr>")
        extra = (f"<details><summary class='dim'>{len(skipped)} sets left out</summary><p class='dim'>"
                 + ", ".join(skipped) + "</p></details>") if skipped else ""
        return (
            "<h3 id='splats'>Splats</h3><p class='dim'>Object scans, then walks and mixed-motion sets with enough "
            f"parallax. COLMAP poses each set; it goes on to training only if at least "
            f"{gate.get('min_registered_ratio', 0.6):.0%} of the frames (and {gate.get('min_registered_images', 8)} or "
            f"more) register with a mean reprojection error of {gate.get('max_mean_reprojection_error_px', 1.5)} px "
            "or less.</p><table><tr><th>Set</th><th>Kind</th><th>Taken</th><th>Camera</th><th>Frames</th>"
            "<th>State</th><th>Registered</th><th>Error (px)</th><th>Training</th><th>Size</th><th>Type</th>"
            "<th>Decision</th></tr>" + "".join(rows) + "</table>" + extra
        )

    def splat_page(self, set_id):
        out = self.splat_dir(set_id)
        sfm, trained = self.splat_records(set_id)
        c = next((x for x in self.candidates() if x["set_id"] == set_id), {})
        scene = out / "scene.splat"
        last = self.last_decision(self.decisions(), set_id, "splat")
        decision = f"last decision: {last['decision']}" if last.get("decision") else "to review"
        summary = html.escape(f"{c.get('camera', '')}, {(c.get('start_local') or '')[:16]}. ")
        summary += html.escape(sfm.get("reason", "no SfM yet")) + "."
        if trained.get("steps"):
            summary += html.escape(f" {trained['steps']} training steps in {trained['train_seconds'] / 60:.0f} min, "
                                   f"{trained['web_gaussians']:,} splats, {trained['splat_mb']} MB.")
        if c.get("best_link") or c.get("first_link"):
            summary += f" <a target='_blank' href='{html.escape(c.get('best_link') or c['first_link'])}'>Immich</a>"
        version = int(scene.stat().st_mtime) if scene.exists() else 0
        url = f"{self.base}/splatfile/{set_id}/scene.splat?v={version}" if scene.exists() else None
        vendor = self.base + "/vendor/"
        import_map = json.dumps({"imports": {
            "three": vendor + "three/three.module.js", "three/addons/": vendor + "three/addons/",
            "spark": vendor + "spark/spark.module.min.js"}})
        details = {"sfm": {k: v for k, v in sfm.items() if k != "models"}, "training": trained}
        return SPLAT_PAGE.format(
            base=self.base, base_script=base_script(self.base), set_id=html.escape(set_id),
            set_id_json=json.dumps(set_id), import_map=import_map.replace("</", "<\\/"),
            type_select=self.type_select(set_id, self.detected_type(set_id, c, self.actions()), self.type_overrides()),
            decision=html.escape(decision), summary=summary, version=version,
            loading="loading..." if url else "not trained yet",
            view_json=json.dumps(sfm.get("view") or {}), url_json=json.dumps(url),
            details=html.escape(json.dumps(details, indent=1)))

    def candidates_table(self, query):
        candidates = self.candidates()
        if not candidates:
            return "<p>No candidates.json found. Run detect and verify first.</p>"
        q = {k: v[0] for k, v in parse_qs(query).items()}
        kind = q.get("type", "panoramas")
        try:
            min_conf = float(q.get("min", "0"))
        except ValueError:
            min_conf = 0.0
        sort = q.get("sort", "quality")
        page = max(0, int(q.get("page", "0")) if q.get("page", "0").isdigit() else 0)
        types = sorted({(c.get("verified_type") or "").split(" (+")[0] for c in candidates} - {""})
        actions = self.actions()
        rows = []
        for c in candidates:
            vt = (c.get("verified_type") or "").split(" (+")[0]
            if kind == "action shots":
                if actions.get(c["set_id"], {}).get("action"):
                    rows.append(c)
                continue
            if kind == "panoramas" and vt in ("", "not a panorama"):
                continue
            if kind == "unverified" and vt:
                continue
            if kind not in ("panoramas", "all", "unverified") and vt != kind:
                continue
            if (c.get("verified_confidence") or 0) < min_conf and kind not in ("unverified", "action shots"):
                continue
            rows.append(c)
        if sort == "date":
            rows.sort(key=lambda c: c["start_local"])
        else:
            rows.sort(key=lambda c: (-(c.get("quality") or 0), -(c.get("verified_confidence") or 0)))
        total = len(rows)
        rows = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        overrides = self.overrides()
        stitched = set(self.sets())
        options = ["panoramas", "all", "unverified", "action shots"] + types
        chosen_types = self.type_overrides()
        loops = set(self.loops())
        type_select = "".join(f"<option{' selected' if o == kind else ''}>{html.escape(o)}</option>" for o in options)
        sort_select = "".join(f"<option{' selected' if o == sort else ''}>{o}</option>" for o in ("quality", "date"))
        form = (
            f"<form action='{self.base}/tables'>Type <select name='type'>{type_select}</select> "
            f"Min confidence <input name='min' size='4' value='{min_conf:g}'> "
            f"Sort <select name='sort'>{sort_select}</select> <button>Filter</button> "
            f"<span class='dim'>{total} sets</span></form>"
        )
        body = []
        for c in rows:
            sid = html.escape(c["set_id"])
            name = f"<a href='{self.base}/set/{sid}'>{sid}</a>" if c["set_id"] in stitched else sid
            if c["set_id"] in loops:
                name += f" <a href='{self.base}/action/{sid}'>loop</a>"
            vt = html.escape(c.get("verified_type") or ("burst mode" if c.get("source") == "burst"
                                                        else "not checked yet"))
            guessed = html.escape(c["guessed_type"])
            changed = "" if vt.split(" (+")[0] == guessed else f" <span class='dim'>(was {guessed})</span>"
            link = c.get("best_link") or c.get("first_link")
            body.append(
                f"<tr><td>{name}</td><td>{html.escape(c['start_local'][:16])}</td>"
                f"<td>{html.escape(c['camera'])}</td><td>{c['count']}</td>"
                f"<td>{vt}{changed}</td><td>{c.get('verified_confidence', '')}</td>"
                f"<td>{coverage(c)}</td><td>{html.escape(str(c.get('motion_model', '')))}</td>"
                f"<td>{self.model_select(c['set_id'], overrides, c.get('stitch_plan', ''))}</td>"
                f"<td>{self.type_select(c['set_id'], self.detected_type(c['set_id'], c, actions), chosen_types)}</td>"
                f"<td>{html.escape(c.get('places') or '')}</td>"
                + (f"<td><a href='{html.escape(link)}' target='_blank'>Immich</a></td></tr>" if link
                   else f"<td>{html.escape(library_label(c))}</td></tr>")
            )
        nav = []
        base = f"type={html.escape(kind)}&min={min_conf:g}&sort={sort}"
        if page > 0:
            nav.append(f"<a href='{self.base}/tables?{base}&page={page - 1}'>previous</a>")
        if (page + 1) * PAGE_SIZE < total:
            nav.append(f"<a href='{self.base}/tables?{base}&page={page + 1}'>next</a>")
        return (
            "<h3>Candidate sets</h3>" + form +
            "<table><tr><th>Set</th><th>Taken</th><th>Camera</th><th>Shots</th><th>Verified type</th>"
            "<th>Conf.</th><th>Coverage</th><th>Motion</th><th>Stitch model</th><th>Type</th><th>Place</th><th></th></tr>"
            + "".join(body) + "</table><p>" + " &middot; ".join(nav) + "</p>"
        )

    def read_json(self, path, default=None):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {} if default is None else default

    def image_size(self, path):
        """Width and height of an image, read from its header and remembered by modification time."""
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return None
        known = self.sizes_cache.get(path)
        if known and known[0] == stamp:
            return known[1]
        from PIL import Image

        try:
            with Image.open(path) as im:
                size = list(im.size)
        except OSError:
            return None
        self.sizes_cache[path] = (stamp, size)
        return size

    def decision_fields(self, decisions, key, kind):
        last = self.last_decision(decisions, key, kind)
        fields = {"decision": last.get("decision"), "decided_at": last.get("at"), "note": last.get("note") or ""}
        if last.get("was"):
            fields["was"] = last["was"]
        if last.get("decision") == EDITING:
            state = self.edit_state(key, last)
            if state["state"] not in ("queued", "running"):
                fields["decision"] = None
            fields["edit"] = state
        return fields

    def stitch_item(self, set_id, decisions, candidates, actions, types, loops):
        set_dir = self.work_dir / set_id
        batch = self.batch_record(set_id)
        layout = self.read_json(set_dir / "layout.json")
        c = candidates.get(set_id, candidates.get(batch.get("set_id") or set_id.split("-")[0], {}))
        thumb = self.thumb_name(set_id)
        versions = self.versions(set_id)
        renders = self.read_json(set_dir / "previews" / "renders.json")
        size = (renders.get(thumb) or {}).get("size") or batch.get("preview")
        error = batch.get("error_px", layout.get("mean_error_px"))
        repair = batch.get("repair") or {}
        frames = batch.get("frames") or layout.get("images")
        used = batch.get("frames_used") or frames
        detected = self.detected_type(set_id, c, actions)
        chosen = types.get(set_id, {}).get("type") or detected
        model = layout.get("model", {})
        flags = []
        if not thumb:
            flags.append(["not rendered yet", 3])
        if batch.get("poor_fit") or batch.get("status") == "poor fit":
            flags.append(["poor fit", 3])
        if isinstance(error, (int, float)):
            if error >= stitch.POOR_ERROR:
                flags.append([f"high control point error ({error:.1f} px)", 2])
            elif error >= stitch.POOR_ERROR / 2:
                flags.append([f"control point error {error:.1f} px", 1])
        left_out = repair.get("left_out") or {}
        if left_out:
            flags.append([f"{len(left_out)} of {frames} frames left out", 2])
        elif len(layout.get("connected_groups") or []) > 1:
            flags.append([f"frames fall into {len(layout['connected_groups'])} groups", 2])
        if repair.get("parts"):
            flags.append([f"{len(repair['parts'])} loose part(s) split off", 1])
        if batch.get("part_of"):
            flags.append([f"loose part of {batch['part_of']}", 1])
        if batch.get("before_repair"):
            flags.append(["repaired automatically", 1])
        if chosen != "panorama":
            flags.append([f"type is {chosen}", 2])
        elif (c.get("verified_confidence") or 1) < 0.5:
            flags.append([f"overlap check confidence {c['verified_confidence']}", 1])
        image = f"{self.base}/img/{set_id}/{thumb}.jpg?v={versions.get(thumb, 0)}" if thumb else None
        manifest = self.read_json(set_dir / "manifest.json", [])
        taken = c.get("start_local") or (manifest[0].get("taken") if manifest else None)
        return {
            "key": f"stitch:{set_id}", "kind": "stitch", "id": set_id, "href": f"{self.base}/set/{set_id}",
            "thumb": image and image + "&size=row", "screen": image and image + "&size=screen", "full": image,
            "preview": thumb, "w": (size or [2, 1])[0], "h": (size or [2, 1])[1],
            "type": chosen, "detected": detected, "model": model.get("chosen") or batch.get("model"),
            "error": error, "frames": frames, "used": used, "taken": taken, "camera": c.get("camera"),
            "place": c.get("places"), "link": c.get("best_link") or c.get("first_link"), **library_fields(c),
            "coverage": batch.get("coverage"), "status": batch.get("status"), "part_of": batch.get("part_of"),
            "loop": set_id in loops, "before": bool(batch.get("before_repair")),
            "previous": (set_dir / "edit" / "previous.jpg").exists(),
            "flags": [f[0] for f in flags], "score": sum(f[1] for f in flags),
            **self.decision_fields(decisions, set_id, "stitch"),
        }

    def group_item(self, key, decisions, candidates, found):
        group_dir = self.groups_dir / key
        info = self.read_json(group_dir / "group.json")
        c = candidates.get(key, {})
        people = info.get("people") or []
        kept = sum(bool(p.get("rejected")) for p in people)
        flags = []
        if kept:
            flags.append([f"{kept} face(s) kept from the base frame", 1])
        if not info.get("swapped"):
            flags.append(["no faces swapped in", 1])
        merged = group_dir / "merged.jpg"
        version = int(merged.stat().st_mtime) if merged.exists() else 0
        size = self.image_size(merged) or [3, 2]
        image = f"{self.base}/groupfile/{key}/merged.jpg?v={version}"
        return {
            "key": f"group:{key}", "kind": "group", "id": key, "href": f"{self.base}/group/{key}",
            "thumb": image + "&size=thumb", "screen": image + "&size=screen", "full": image,
            "w": size[0], "h": size[1], "type": "group", "frames": len(info.get("frames") or []),
            "people": len(people), "swapped": info.get("swapped"), "taken": c.get("start_local"),
            "camera": c.get("camera"), "place": c.get("places"), "link": c.get("best_link") or c.get("first_link"), **library_fields(c),
            "reason": found.get(key, {}).get("reason"),
            "flags": [f[0] for f in flags], "score": sum(f[1] for f in flags),
            **self.decision_fields(decisions, key, "group"),
        }

    def loop_item(self, key, decisions, candidates, actions):
        loop_dir = self.loops_dir / key
        info = self.read_json(loop_dir / "action.json")
        c = self.loop_candidate(key, info, candidates)
        poster = loop_dir / "poster.jpg"
        size = self.image_size(poster) or [3, 2]
        animation = loop_dir / "animation.webp"
        version = int(animation.stat().st_mtime) if animation.exists() else 0
        flags = [["only a few frames", 1]] if (info.get("frames") or 0) < 6 and info.get("style") != "wiggle" else []
        return {
            "key": f"action:{key}", "kind": "action", "id": key, "href": f"{self.base}/action/{key}",
            "thumb": f"{self.base}/loop/{key}/poster.jpg?size=thumb&v={version}",
            "screen": f"{self.base}/loop/{key}/animation.webp?v={version}" if version else None,
            "full": f"{self.base}/loop/{key}/animation.webp?v={version}" if version else None,
            "w": size[0], "h": size[1], "type": "action", "frames": info.get("frames"),
            "taken": c.get("start_local"), "camera": c.get("camera"), "place": c.get("places"),
            "link": c.get("best_link") or c.get("first_link"), **library_fields(c),
            "reason": actions.get(key, {}).get("reason") or c.get("reason") or info.get("reason"),
            "name": c.get("name"), "style": info.get("style") or "loop",
            "composite": f"{self.base}/loop/{key}/composite.jpg?v={version}"
            if info.get("composite") and (loop_dir / "composite.jpg").exists() else None,
            "flags": [f[0] for f in flags], "score": sum(f[1] for f in flags),
            **self.decision_fields(decisions, key, "action"),
        }

    def clip_item(self, key, info, decisions):
        """A still whose clip passed the action check, offered as a loop before one is made:
        the page plays the clip itself."""
        c = self.motion_candidate(key)
        size = info.get("size") or [4, 3]
        still = f"{self.base}/clip/{key}/still.jpg"
        return {
            "key": f"action:{key}", "kind": "action", "id": key, "href": None,
            "thumb": still + "?size=thumb", "screen": still, "full": still, "clip": f"{self.base}/clip/{key}/video",
            "making": key in self.making, "w": size[0], "h": size[1], "type": "action", "frames": None,
            "name": c.get("name"), "taken": c.get("start_local"), "camera": c.get("camera"), "place": None, "link": None,
            **library_fields(c), "reason": info.get("reason"), "flags": [], "score": 0,
            **self.decision_fields(decisions, key, "action"),
        }

    def splat_item(self, row, decisions, candidates):
        set_id = row["set_id"]
        sfm, trained = self.splat_records(set_id)
        c = candidates.get(set_id, {})
        flags = []
        error = sfm.get("mean_reprojection_error_px")
        if trained.get("quality") not in (None, "good"):
            flags.append([f"training quality {trained['quality']}", 1])
        if sfm.get("registered") is not None and sfm.get("images") and sfm["registered"] < sfm["images"]:
            flags.append([f"{sfm['registered']} of {sfm['images']} frames posed", 1])
        return {
            "key": f"splat:{set_id}", "kind": "splat", "id": set_id, "href": f"{self.base}/splat/{set_id}",
            "thumb": None, "screen": None, "full": None, "w": 4, "h": 3, "type": "object scan",
            "frames": row.get("usable"), "taken": c.get("start_local") or row.get("taken"),
            "camera": c.get("camera") or row.get("camera"), "place": c.get("places"),
            "link": c.get("best_link") or c.get("first_link"), **library_fields(c), "reason": sfm.get("reason"), "error": error,
            "splats": trained.get("web_gaussians"), "splat_mb": trained.get("splat_mb"),
            "flags": [f[0] for f in flags], "score": sum(f[1] for f in flags),
            **self.decision_fields(decisions, set_id, "splat"),
        }

    def items(self):
        """Everything there is to review, as one list for the grid and viewer."""
        decisions, actions, types = self.decisions(), self.actions(), self.type_overrides()
        candidates = {c["set_id"]: c for c in self.candidates()}
        loops = set(self.loops())
        found = [self.stitch_item(s, decisions, candidates, actions, types, loops) for s in self.sets()]
        group_checks = self.groups()
        found += [self.group_item(k, decisions, candidates, group_checks) for k in self.merges()]
        found += [self.loop_item(k, decisions, candidates, actions) for k in loops]
        found += [self.clip_item(k, v, decisions) for k, v in sorted(self.motion_clips().items()) if k not in loops]
        found = self.found.carry(found, decisions) + self.found.items(candidates, decisions, actions, types)
        for row in self.splat_plan().get("sets", []):
            if self.splat_records(row["set_id"])[1].get("steps"):
                found.append(self.splat_item(row, decisions, candidates))
        return {"items": found, "types": {t: MAKES[t] for t in SET_TYPES}, "models": OVERRIDE_MODELS,
                "projections": list(stitch.PROJECTIONS), "orientations": list(stitch.ORIENTATIONS),
                "running": self.running(), "setup": not found and self.needs_setup()}

    def watch_changes(self, every=10.0, gap=3.0):
        """Start pushing changes to the list to open pages (GET /api/events)."""
        watch = {self.work_dir, self.candidates_file.parent if self.candidates_file else None, self.output_dir}
        self.changes = Changes(self.items, watch, [self.cache_dir], every, gap).start()
        return self.changes

    def running(self):
        """The stage a run is in now, from the progress.json it keeps up to date, or None."""
        try:
            state = json.loads(self.actions_file.with_name("progress.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or state.get("finished") or time.time() - state.get("at", 0) > 300:
            return None
        return {k: state.get(k) for k in ("stage", "done", "total", "unit", "seconds_left")}

    def crop_rects(self, set_dir, versions):
        """The clean rectangle of each view, as shares of the uncropped panorama."""
        renders = self.read_json(set_dir / "previews" / "renders.json")
        return {name: (renders.get(name) or {}).get("crop_rect") for name in versions}

    def pano_data(self, set_dir, name):
        """Where an equirectangular preview sits on the full sphere, read from the project it was rendered from."""
        pto = set_dir / "previews" / f"{name}.pto"
        if not name.startswith("equirectangular") or name.endswith("vertical") or not pto.exists():
            return None
        line = next((l for l in pto.read_text(encoding="utf-8", errors="replace").splitlines()
                     if l.startswith("p ")), "")
        fields = dict(re.findall(r"\b([fwhvS])([0-9.,-]+)", line))
        try:
            if int(fields.get("f", -1)) != 2:
                return None
            width, height, fov = int(fields["w"]), int(fields["h"]), float(fields["v"])
            left, right, top, bottom = (int(x) for x in fields.get("S", f"0,{width},0,{height}").split(","))
        except (KeyError, ValueError):
            return None
        full_width = round(width * 360 / min(fov, 360))
        full_height = full_width // 2
        if height > full_height:
            return None
        return {"fullWidth": full_width, "fullHeight": full_height, "croppedWidth": right - left,
                "croppedHeight": bottom - top, "croppedX": (full_width - width) // 2 + left,
                "croppedY": (full_height - height) // 2 + top}

    def frame_list(self, set_id):
        """The set's original frames, each marked used, left out (with why) or moved to a part."""
        set_dir = self.set_dir(set_id)
        batch = self.batch_record(set_id)
        repair = batch.get("repair") or {}
        left_out = repair.get("left_out") or {}
        moved = {}
        for part_key, part in zip(batch.get("parts") or [], repair.get("parts") or []):
            for index in part.get("frames", []):
                moved[str(index)] = part_key
        frames = []
        for entry in self.read_json(set_dir / "manifest.json", []):
            index = entry.get("index")
            name = re.split(r"[\\/]", entry.get("original") or "")[-1]
            if not isinstance(index, int) or not (set_dir / "originals" / name).is_file():
                continue
            frames.append({"index": index, "file": entry.get("file"), "taken": entry.get("taken"),
                           "url": f"{self.base}/frame/{set_id}/{index}?size=thumb",
                           "full": f"{self.base}/frame/{set_id}/{index}",
                           "left_out": left_out.get(str(index)), "part": moved.get(str(index))})
        return frames

    def frame_file(self, set_id, index, size=None):
        set_dir = self.set_dir(set_id)
        if not index.isdigit():
            raise KeyError(index)
        entry = next((e for e in self.read_json(set_dir / "manifest.json", []) if str(e.get("index")) == index), None)
        if entry is None:
            raise KeyError(index)
        path = set_dir / "originals" / re.split(r"[\\/]", entry.get("original") or "")[-1]
        if not path.is_file():
            raise KeyError(index)
        if size in SIZES:
            return scaled_copy(path, self.cache_dir / size / set_id / "frames" / f"{index}.jpg", SIZES[size])
        return path

    def before_file(self, set_id, file_name, size=None):
        if not FILE_NAME.match(file_name or ""):
            raise KeyError(file_name)
        path = self.set_dir(set_id) / "previews" / "before-repair" / file_name
        if not path.exists():
            raise KeyError(file_name)
        if size in SIZES:
            return scaled_copy(path, self.cache_dir / size / set_id / "before-repair" / file_name, SIZES[size])
        return path

    def item_detail(self, kind, key):
        """What the viewer's info panel shows for one item, beyond the grid's list."""
        if kind not in KINDS:
            raise KeyError(kind)
        c = next((x for x in self.candidates() if x["set_id"] == key.split("-")[0]), {})
        types = self.type_overrides()
        detail = {"type_choice": types.get(key, {}).get("type", "auto"),
                  "detected": self.detected_type(key, c, self.actions()),
                  "verify": {k: c.get(k) for k in ("verified_type", "verified_confidence", "verify_reason",
                                                   "motion_model", "coverage_h", "coverage_v", "count")}}
        if kind != "stitch":
            if kind == "group":
                self.group_dir(key)
            elif kind == "action":
                if key not in self.motion_clips():
                    self.loop_dir(key)
            else:
                self.splat_dir(key)
            return detail
        set_dir = self.set_dir(key)
        batch = self.batch_record(key)
        layout = self.read_json(set_dir / "layout.json")
        versions = self.versions(key)
        before = batch.get("before_repair") or {}
        before_name = Path(before.get("preview_file") or "").name
        before_dir = set_dir / "previews" / "before-repair"
        detail.update({
            "versions": versions, "fills": self.fills(key), "preview": self.thumb_name(key),
            "crops": [name for name in versions if (set_dir / "previews" / f"{name}_crop.jpg").exists()],
            "pano": {name: self.pano_data(set_dir, name) for name in versions},
            "crop_rects": self.crop_rects(set_dir, versions),
            "frames": self.frame_list(key),
            "model_choice": self.overrides().get(key, {}).get("model", "auto"),
            "stitch_plan": c.get("stitch_plan", ""),
            "model": layout.get("model", {}),
            "groups": len(layout.get("connected_groups") or []),
            "repair": {k: v for k, v in (batch.get("repair") or {}).items() if k != "kept"},
            "parts": batch.get("parts") or [],
            "previous": self.previous_info(key),
            "before": {"url": f"{self.base}/before/{key}/{before_name}", "error": before.get("error_px"),
                       "status": before.get("status"), "coverage": before.get("coverage"),
                       "v": int((before_dir / before_name).stat().st_mtime)}
            if before_name and (before_dir / before_name).exists() else None,
        })
        return detail

    def previous_info(self, set_id):
        """The version shown before the last Edit, for the B compare."""
        edit_dir = self.work_dir / set_id / "edit"
        if not (edit_dir / "previous.jpg").exists():
            return None
        info = self.read_json(edit_dir / "previous.json")
        return {"url": f"{self.base}/previous/{set_id}/previous.jpg", "error": info.get("error_px"),
                "status": info.get("status"), "coverage": info.get("coverage"), "file": info.get("file"),
                "at": info.get("at"), "v": int((edit_dir / "previous.jpg").stat().st_mtime)}

    def decide_many(self, body):
        """Save several decisions in one write; each item is what decide() takes."""
        entries = body.get("items")
        if not isinstance(entries, list) or not entries or len(entries) > 1000:
            raise ValueError("items must be a list of 1 to 1000 decisions")
        records = []
        for entry in entries:
            set_id = entry.get("set")
            kind = entry.get("kind") if entry.get("kind") in KINDS else "stitch"
            {"action": self.loop_dir, "group": self.group_dir, "splat": self.splat_dir,
             "found": self.found.check}.get(kind, self.set_dir)(set_id)
            decision = decision_name(entry.get("decision"))
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            note = str(entry.get("note") or "")[:500]
            if kind == "stitch":
                record = {"decision": decision, "projection": entry.get("projection"),
                          "orientation": entry.get("orientation"), **view_choice(entry),
                          "note": note, "at": at}
            else:
                record = {"kind": kind, "decision": decision, "note": note, "at": at}
            records.append((set_id, record))
        with self.lock:
            data = self.decisions()
            for set_id, record in records:
                data.setdefault(set_id, []).append(record)
            write_atomic(self.decisions_file, json.dumps(data, indent=2))
        self.after_decisions(records)
        return {"records": [{"set": s, "kind": r.get("kind", "stitch"), **r} for s, r in records]}

    def undo(self, body):
        """Take back decisions just made: each one goes only if it is still the latest of its kind for its set."""
        entries = body.get("records")
        if not isinstance(entries, list) or not entries:
            raise ValueError("records must be a non-empty list")
        undone = []
        with self.lock:
            data = self.decisions()
            for entry in entries:
                records = data.get(entry.get("set"), [])
                kind = entry.get("kind", "stitch")
                mine = [i for i, r in enumerate(records) if r.get("kind", "stitch") == kind]
                if mine and records[mine[-1]].get("at") == entry.get("at") and \
                        records[mine[-1]].get("decision") == LEGACY.get(entry.get("decision"), entry.get("decision")):
                    records.pop(mine[-1])
                    if not records:
                        data.pop(entry["set"])
                    undone.append(entry.get("set"))
            write_atomic(self.decisions_file, json.dumps(data, indent=2))
        return {"undone": undone}

    def app_page(self):
        vendor = self.base + "/vendor/"
        import_map = json.dumps({"imports": {"three": vendor + "three/three.module.js",
                                             "psv": vendor + "psv/index.module.js"}})
        version = max(int((WEB_DIR / name).stat().st_mtime) for name in WEB_FILES)
        page = (WEB_DIR / "review.html").read_text(encoding="utf-8")
        return (page.replace("__BASE__", html.escape(self.base)).replace("__V__", str(version))
                .replace("__BASE_SCRIPT__", base_script(self.base))
                .replace("__ENHANCE_HIDDEN__", "" if config.wants("enhance") else " hidden")
                .replace("__IMPORT_MAP__", import_map.replace("</", "<\\/")))

    def web_file(self, name):
        if name not in WEB_FILES:
            raise KeyError(name)
        return WEB_DIR / name

    def enhance_dir(self):
        return self.work_dir / "enhance"

    def enhance_page(self):
        """The photos restoration could help most, read-only, biggest expected gain first."""
        from . import enhance

        found = enhance.load(self.enhance_dir())
        esc = html.escape
        rows = "".join(
            f"<tr><td><img class='thumb' loading='lazy' alt='' src='{self.base}/enhancefile/{esc(r['id'])}.jpg'></td>"
            f"<td><b>{r['gain']:.0f}</b></td><td>{esc(r.get('file_name') or '')}<br><span class='dim'>"
            f"{esc((r.get('taken') or '')[:10])} {esc(r['id'])}</span></td>"
            f"<td>{'<br>'.join(esc(x) for x in r['reasons'])}</td><td>{esc(' > '.join(r['chain']))}</td></tr>"
            for r in found["candidates"])
        note = ("" if config.wants("enhance") else
                "<p>Enhance is turned off in <a href='" + self.base + "/settings'>Settings</a>.</p>")
        empty = "<p class='dim'>No candidates yet. Run <code>fuseshot enhance-scan</code>.</p>"
        return (
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Enhance candidates</title>" + INDEX_STYLE + base_script(self.base) +
            f"<h2>Enhance candidates</h2><p><a href='{self.base}/'>Back to the review grid</a></p>{note}"
            f"<p class='dim'>{found['scored']} photos scored, {found['fine']} look fine. Expected gain is out of "
            "100. This list only reads your photos; nothing is changed or uploaded.</p>"
            + (f"<table id='enhance'><tr><th></th><th>Gain</th><th>Photo</th><th>Why</th><th>Suggested steps</th></tr>"
               f"{rows}</table>" if rows else empty))

    def enhance_file(self, name):
        from . import enhance

        asset_id = name.removesuffix(".jpg")
        if not name.endswith(".jpg") or not asset_id.replace("-", "").isalnum():
            raise KeyError(name)
        path = enhance.thumb_path(self.enhance_dir(), asset_id)
        if not path.exists():
            raise KeyError(name)
        return path

    def index_page(self, query=""):
        return (
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Fuseshot review</title>" + INDEX_STYLE + base_script(self.base) +
            f"<h2>Fuseshot review</h2><p><a href='{self.base}/'>Back to the review grid</a></p><p>Decisions are saved to work/reviews.json, model overrides to "
            "work/overrides.json and type overrides to work/types.json only. Nothing is uploaded. Auto tries "
            "the models in the order shown and keeps the one that connects more images with the lower error. "
            "The type decides what a set becomes: panoramas are stitched, object scans become splats, "
            "action shots become loops and group shots become best-shot merges.</p>"
            + self.stitched_table() + self.groups_table() + self.loops_table() + self.splats_table() + self.candidates_table(query) + OVERRIDE_SCRIPT
        )

    def set_page(self, set_id):
        set_dir = self.set_dir(set_id)
        layout_file = set_dir / "layout.json"
        layout = json.loads(layout_file.read_text(encoding="utf-8")) if layout_file.exists() else {}
        summary = {k: v for k, v in layout.items() if k not in ("steps", "pair_points", "checkpto")}
        rendered = self.rendered(set_id)
        orientation = stitch.default_orientation(set_dir / "optimised.pto")
        projection = "cylindrical"
        chosen = self.default_preview(set_id)
        if chosen:
            projection, _, orientation = chosen.partition("-")
            orientation = orientation or "level"
        elif rendered and stitch.preview_name(projection, orientation) not in rendered:
            projection, _, orientation = rendered[0].partition("-")
            orientation = orientation or "level"
        last = self.last_decision(self.decisions(), set_id, "stitch")
        decision = f"last decision: {last['decision']}" if last.get("decision") else "to review"
        option = lambda values: "".join(f"<option>{v}</option>" for v in values)
        c = next((x for x in self.candidates() if x["set_id"] == set_id), {})
        model = layout.get("model", {})
        verified = ""
        if c.get("verified_type"):
            verified = (f"Overlap check: {html.escape(c['verified_type'])} {coverage(c)}, confidence "
                        f"{c['verified_confidence']}, motion {html.escape(c['motion_model'])}. "
                        f"{html.escape(c.get('verify_reason', ''))}. ")
        if model:
            verified += f"Stitched with {html.escape(model.get('chosen', ''))} ({html.escape(model.get('reason', ''))}). "
        verified += "Model for the next align: " + self.model_select(set_id, self.overrides(), c.get("stitch_plan", ""))
        actions = self.actions()
        verified += " Type: " + self.type_select(set_id, self.detected_type(set_id, c, actions), self.type_overrides())
        if set_id in self.loops():
            verified += f" <a href='{self.base}/action/{set_id}'>see the loop</a>"
        elif actions.get(set_id, {}).get("burst"):
            verified += f" <span class='dim'>{html.escape(actions[set_id]['reason'])}</span>"
        return PAGE.format(
            base=self.base,
            base_script=base_script(self.base),
            versions_json=json.dumps(self.versions(set_id)),
            fills_json=json.dumps(self.fills(set_id)),
            verified=verified,
            set_id=html.escape(set_id),
            set_id_json=json.dumps(set_id),
            projection_options=option(stitch.PROJECTIONS),
            orientation_options=option(stitch.ORIENTATIONS),
            default_projection_json=json.dumps(projection),
            default_orientation_json=json.dumps(orientation),
            decision=html.escape(decision),
            layout=html.escape(json.dumps(summary, indent=1)),
        )


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def fetch_viewer():
    """The sphere and splat viewers need three.js and friends; fetch them once, in the background."""
    try:
        assets.fetch_viewer()
    except (OSError, assets.AssetError) as err:
        LOG.warning(f"Could not fetch the viewer libraries ({err}); the sphere and splat views stay off "
                    "until `fuseshot fetch-assets viewer` works")
COOKIE = "fuseshot_token"
LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Fuseshot</title>
<body style="font:16px system-ui;max-width:32em;margin:4em auto;padding:0 1em">
<h1>Fuseshot</h1><p>This review page needs its access link. Open the link that was printed when it was
started (it ends in <code>?token=...</code>), or ask whoever runs it.</p></body>"""


def make_handler(review, redirect_from=(), token=None):
    old_bases = ["/" + p.strip("/") for p in redirect_from if p.strip("/")]

    class Handler(BaseHTTPRequestHandler):
        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

        def signed_in(self):
            """With a token set, let a request through only with the token cookie, or with ?token=
            in the address, which sets the cookie and sends the browser on without it."""
            if not token:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            if COOKIE in cookie and hmac.compare_digest(cookie[COOKIE].value, token):
                return True
            path, _, query = self.path.partition("?")
            q = parse_qs(query)
            given = (q.pop("token", None) or [""])[0]
            if self.command == "GET" and given and hmac.compare_digest(given, token):
                rest = urlencode(q, doseq=True)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                self.send_response(303)
                self.send_header("Set-Cookie", f"{COOKIE}={token}; Path={review.base or '/'}; HttpOnly; "
                                               f"SameSite=Strict; Max-Age=31536000{secure}")
                self.send_header("Location", path + ("?" + rest if rest else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            self.send(401, LOGIN_PAGE)
            return False

        def same_origin(self):
            """Changes come only from the review page itself: JSON bodies (which a form on another site
            cannot send without asking first) and, when the browser says where it came from, this site."""
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}
            return urlsplit(origin).netloc in hosts

        def may_change_settings(self):
            """Settings and the folder browser: with a token, whoever has it; without one, only
            someone on this computer, and not through a proxy that makes everyone look local."""
            return bool(token) or settings_web.local_request(self.client_address[0], self.headers)

        def settings_post(self, path, body):
            if not self.may_change_settings():
                return self.send_json(403, {"error": "settings can be changed only on this computer, or with "
                                                     "FUSESHOT_REVIEW_TOKEN set"})
            if path == "/settings":
                found = settings_web.save(body, review.cli_sources)
                review.settings_saved()
                return self.send_json(200, found)
            if path == "/settings/test":
                return self.send_json(200, settings_web.test_immich(body))
            if path == "/settings/browse":
                return self.send_json(200, settings_web.browse(body))
            if path == "/settings/scan":
                return self.send_json(200, settings_web.start_scan(review.running() is not None))
            return self.send_json(404, {"error": "not found"})

        def send(self, code, body, kind="text/html; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, code, obj):
            self.send(code, json.dumps(obj), "application/json")

        def send_file(self, path, versioned, kind="image/jpeg"):
            stat = path.stat()
            etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            # Safari asks for videos a range at a time and will not play them otherwise.
            span = byte_range(self.headers.get("Range"), stat.st_size) if kind.startswith("video/") else None
            start, end = span or (0, stat.st_size)
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(end - start))
            if kind.startswith("video/"):
                self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{stat.st_size}")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=31536000, immutable" if versioned else "no-cache")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start
                while left > 0:
                    data = f.read(min(CHUNK, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)

        def send_events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(text):
                self.wfile.write(text.encode("utf-8"))
                self.wfile.flush()

            review.changes.stream(write)

        def route(self):
            """Path below the base path, or None when the request is outside it."""
            path = unquote(self.path.split("?", 1)[0])
            if not review.base:
                return path
            if path == review.base:
                return ""
            if path.startswith(review.base + "/"):
                return path[len(review.base):]
            return None

        def moved(self, code=301):
            """Send a request under an old base path on to the same place under the current one."""
            path = self.path.split("?", 1)[0]
            for old in old_bases:
                if path == old or path.startswith(old + "/"):
                    rest = self.path[len(old):] or "/"
                    if rest.startswith("?"):
                        rest = "/" + rest
                    self.send_response(code)
                    self.send_header("Location", review.base + rest)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
            return False

        def do_GET(self):
            if self.moved() or not self.signed_in():
                return
            path = self.route()
            if path == "":
                self.send_response(301)
                self.send_header("Location", review.base + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            query = self.path.partition("?")[2]
            parts = [p for p in (path or "").split("/") if p]
            try:
                if path is None:
                    pass
                elif not parts or parts == ["settings"]:
                    return self.send(200, review.app_page())
                elif parts == ["api", "settings"]:
                    found = settings_web.state(review.cli_sources)
                    return self.send_json(200, {**found, "can_change": self.may_change_settings()})
                elif parts == ["api", "settings", "outputs"]:
                    return self.send_json(200, settings_web.output_status())
                elif parts == ["enhance"]:
                    return self.send(200, review.enhance_page())
                elif parts[0] == "enhancefile" and len(parts) == 2:
                    return self.send_file(review.enhance_file(parts[1]), False)
                elif parts == ["tables"]:
                    return self.send(200, review.index_page(query))
                elif parts == ["api", "items"]:
                    return self.send_json(200, review.items())
                elif parts == ["api", "events"] and review.changes:
                    return self.send_events()
                elif parts[0] == "api" and len(parts) == 4 and parts[1] == "item":
                    return self.send_json(200, review.item_detail(parts[2], parts[3]))
                elif parts[0] == "static" and len(parts) == 2:
                    return self.send_file(review.web_file(parts[1]), "v" in query, WEB_FILES[parts[1]])
                elif parts[0] in ("frame", "before", "previous") and len(parts) == 3:
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    found = {"frame": review.frame_file, "before": review.before_file,
                             "previous": review.previous_file}[parts[0]](parts[1], parts[2], q.get("size"))
                    return self.send_file(found, parts[0] == "frame" or "v" in q)
                elif parts[0] == "found" and len(parts) == 3:
                    return self.send_file(review.found.thumb_file(parts[1], parts[2]), True)
                elif parts[0] == "set" and len(parts) == 2:
                    return self.send(200, review.set_page(parts[1]))
                elif parts[0] == "img" and len(parts) == 3:
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    image = review.image_file(parts[1], parts[2], q.get("size"))
                    return self.send_file(image, "v" in q)
                elif parts[0] == "action" and len(parts) == 2:
                    return self.send(200, review.loop_page(parts[1]))
                elif parts[0] == "loop" and len(parts) == 3:
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    path = review.loop_file(parts[1], parts[2], q.get("size"))
                    return self.send_file(path, "v" in q, LOOP_FILES[parts[2]])
                elif parts[0] == "clip" and len(parts) == 3 and parts[2] in ("still.jpg", "video"):
                    if parts[2] == "video":
                        from .motion import CLIP_TYPES

                        path = review.clip_file(parts[1])
                        return self.send_file(path, False, CLIP_TYPES[path.suffix])
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    return self.send_file(review.clip_still(parts[1], q.get("size")), False)
                elif parts[0] == "group" and len(parts) == 2:
                    return self.send(200, review.group_page(parts[1]))
                elif parts[0] == "groupfile" and len(parts) in (3, 4):
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    return self.send_file(review.group_file(parts[1], parts[2:], q.get("size")), "v" in q)
                elif parts[0] == "splat" and len(parts) == 2:
                    return self.send(200, review.splat_page(parts[1]))
                elif parts[0] == "splatfile" and len(parts) == 3:
                    path = review.splat_file(parts[1], parts[2])
                    return self.send_file(path, "v=" in query, SPLAT_FILES[parts[2]])
                elif parts[0] == "vendor" and len(parts) > 1:
                    rel = "/".join(parts[1:])
                    return self.send_file(review.vendor_file(rel), False, VENDOR_FILES[rel])
            except KeyError:
                pass
            except (ConnectionError, TimeoutError):
                return
            except OSError as err:
                LOG.warning(f"could not answer {path}: {err}")
                return self.send(503, "busy, try again", "text/plain")
            self.send(404, "not found", "text/plain")

        def do_POST(self):
            if self.moved(308) or not self.signed_in():
                return
            if not self.same_origin():
                return self.send_json(403, {"error": "changes are only taken from the review page itself"})
            path = self.route()
            length = int(self.headers.get("Content-Length") or 0)
            if review.changes:
                review.changes.touch()
            try:
                body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
                if path == "/decision":
                    return self.send_json(200, review.decide(body))
                if path == "/decisions":
                    return self.send_json(200, review.decide_many(body))
                if path == "/undo":
                    return self.send_json(200, review.undo(body))
                if path == "/override":
                    return self.send_json(200, review.override(body))
                if path == "/type":
                    return self.send_json(200, review.set_type(body))
                if path == "/edit":
                    return self.send_json(200, review.edit(body))
                if path == "/render":
                    return self.send_json(200, review.render(body))
                if path == "/fill":
                    return self.send_json(200, review.queue_fill(body))
                if path == "/group/choose":
                    return self.send_json(200, review.choose_face(body))
                if path and (path == "/settings" or path.startswith("/settings/")):
                    return self.settings_post(path, body)
                self.send_json(404, {"error": "not found"})
            except KeyError:
                self.send_json(404, {"error": "unknown set"})
            except (ValueError, stitch.StitchError, tools.ToolMissing) as err:
                self.send_json(400, {"error": str(err)})

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(work_dir, host="127.0.0.1", port=8765, candidates_file=None, base_path="", cache_dir=None,
          redirect_from=(), output_dir=None, token=None, open_browser=False, upload_to=None, album=None,
          library=None, cli_sources=False, reconfigure=None):
    """Serve the review page. Anyone who can reach it can keep and discard, so on anything but
    this computer it asks for a token, made up here when none is given. With upload_to (an Immich
    client), kept Immich results are uploaded to it. The library (sources.Library) gives the
    thumbnails of sets found but not stitched yet; reconfigure makes it again after the settings
    page saves new sources."""
    review = Review(work_dir, candidates_file, base_path, cache_dir, output_dir, library)
    review.cli_sources, review.reconfigure = cli_sources, reconfigure
    review.migrate()
    review.watch_changes()
    if upload_to is not None:
        from .upload import Uploader

        review.uploader = Uploader(review, upload_to, album)
        LOG.info("Kept results will be uploaded to Immich" + (f" (album {album!r})" if album else ""))
    made_up = not token and host not in LOOPBACK
    if made_up:
        token = secrets.token_urlsafe(24)
        LOG.warning("Listening beyond this computer, so the page needs a token. Set FUSESHOT_REVIEW_TOKEN "
                    "to keep the same one across restarts.")
    server = ThreadingHTTPServer((host, port), make_handler(review, redirect_from, token))
    server.daemon_threads = True
    if not assets.viewer_ready():
        threading.Thread(target=fetch_viewer, daemon=True).start()
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    link = f"http://{shown}:{port}{review.base}/" + (f"?token={token}" if token else "")
    # A token you chose stays out of the log; a made-up one has to be shown to be any use.
    shown_link = link if made_up or not token else link.replace(token, "<your token>")
    LOG.info(f"Review page at {shown_link}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(link)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
