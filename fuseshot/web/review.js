// Review grid and one-at-a-time viewer, laid out like the Immich web app.
// Decisions are kept by the server in work/reviews.json; nothing here needs a build step.
import { ICONS } from "./icons.js";
import { FOUND, byPromise, foundBadge, foundStage, foundStrip, foundWhat, listen } from "./found.js";

const $ = id => document.getElementById(id);
const DECISION_LABEL = { kept: "Kept", discarded: "Discarded", editing: "Being edited" };
const DECISION_ICON = { kept: "check", discarded: "close", editing: "pencil" };
const KIND = {
  stitch: { label: "Panoramas", one: "Panorama", icon: "panorama" },
  group: { label: "Groups", one: "Group shot", icon: "group" },
  action: { label: "Loops", one: "Loop", icon: "run" },
  splat: { label: "Splats", one: "Object scan", icon: "cube" },
  found: FOUND,
};
const STATUSES = [
  ["todo", "To review", "inbox"], ["kept", "Kept", "checkCircle"], ["discarded", "Discarded", "closeCircle"],
  ["editing", "Being edited", "pencil"], ["all", "Everything", "all"],
];
const SWIPE = { arm: 10, tint: 40, decide: 88, tap: 14 };
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const narrow = matchMedia("(max-width: 899px)");

function setting(name) {
  return localStorage.getItem("fuseshot." + name);
}

function icon(name, cls = "i") {
  const d = ICONS[name];
  return d ? `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>` : "";
}
function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function fillIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach(el => {
    if (!el.querySelector("svg")) el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon));
  });
}
async function post(path, body) {
  const r = await fetch(BASE + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
function when(text) {
  if (!text) return "";
  const d = new Date(text.replace(" ", "T"));
  if (isNaN(d)) return text;
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
}
function month(text) {
  const d = new Date((text || "").replace(" ", "T"));
  return isNaN(d) ? "No date" : d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

const state = {
  items: [], byKey: new Map(), types: {}, models: [], projections: [], orientations: [],
  status: "todo", kind: "all", sort: "queue", q: "",
  shown: [], selected: new Set(), anchor: null,
  undo: [], details: new Map(), gone: [],
};

// filtering and the grid

function matches(item) {
  if (state.kind !== "all" && item.kind !== state.kind) return false;
  if (state.status === "todo" ? item.decision : state.status !== "all" && item.decision !== state.status) return false;
  if (!state.q) return true;
  const hay = [item.id, item.camera, item.place, item.type, item.model, KIND[item.kind].one, ...(item.flags || [])].join(" ").toLowerCase();
  return state.q.toLowerCase().split(/\s+/).every(word => hay.includes(word));
}
function sections() {
  const list = state.items.filter(matches);
  const byDate = (a, b) => (b.taken || "").localeCompare(a.taken || "") || a.id.localeCompare(b.id);
  if (state.sort === "date") {
    list.sort(byDate);
    const out = [];
    for (const item of list) {
      const title = month(item.taken);
      if (!out.length || out[out.length - 1].title !== title) out.push({ title, items: [] });
      out[out.length - 1].items.push(item);
    }
    return out;
  }
  const found = list.filter(i => i.kind === "found").sort(byPromise);
  const rest = list.filter(i => i.kind !== "found");
  const look = rest.filter(i => i.score > 0).sort((a, b) => b.score - a.score || byDate(a, b));
  const clean = rest.filter(i => !i.score).sort(byDate);
  return [
    { title: "Found while scanning", hint: "not stitched yet, most promising first", items: found },
    { title: "Needs a look", hint: "flagged by the checks, worst first", items: look },
    { title: "Looks clean", hint: "nothing flagged", items: clean, clean: true },
  ].filter(s => s.items.length);
}

function justify(items, width, height, gap) {
  const rows = [];
  let row = [], sum = 0;
  for (const item of items) {
    const aspect = Math.min(6, Math.max(0.5, (item.w || 3) / (item.h || 2)));
    row.push({ item, aspect });
    sum += aspect;
    if (sum * height + gap * (row.length - 1) >= width) {
      const h = (width - gap * (row.length - 1)) / sum;
      rows.push({ h, tiles: row });
      row = []; sum = 0;
    }
  }
  if (row.length) rows.push({ h: Math.min(height, (width - gap * (row.length - 1)) / sum), tiles: row, last: true });
  return rows;
}

function tileHtml(item, w, h) {
  const flags = item.flags || [];
  const badge = item.kind === "stitch" || item.kind === "found"
    ? (item.frames ? `<span>${item.used && item.used !== item.frames ? `${item.used}/` : ""}${item.frames}</span>${icon("frames")}` : "")
    : icon(KIND[item.kind].icon);
  const media = item.strip?.length ? foundStrip(item, esc) : item.thumb
    ? `<img loading="lazy" decoding="async" src="${esc(item.thumb)}" alt="">`
    : `<div class="placeholder">${icon(KIND[item.kind].icon)}</div>`;
  const chosen = item.decision ? `<span class="bl ${item.decision}">${icon(DECISION_ICON[item.decision])}${DECISION_LABEL[item.decision]}</span>`
    : item.edit?.state === "failed" ? `<span class="bl edit-failed">${icon("alert")}Edit failed</span>`
    : item.edit?.state === "done" ? `<span class="bl edited">${icon("pencil")}New version</span>`
    : item.note ? `<span class="bl edited">${icon("note")}Note</span>`
    : item.kind === "found" ? foundBadge(item, { icon, esc }) : "";
  return `<div class="tile${state.selected.has(item.key) ? " selected" : ""}" tabindex="0" data-key="${esc(item.key)}"
    style="width:${w.toFixed(2)}px;height:${h.toFixed(2)}px" title="${esc(item.id + (flags.length ? " - " + flags.join("; ") : ""))}">
    ${media}<div class="shade"></div>
    <button class="check" data-check title="Select">${icon(state.selected.has(item.key) ? "circleChecked" : "circle")}</button>
    <div class="tr">${flags.length ? `<span class="flag">${icon("alert")}</span>` : ""}${badge}</div>
    ${chosen}<span class="name">${esc(item.id)}</span></div>`;
}

let layoutWidth = 0;
function renderGrid() {
  const grid = $("grid");
  const width = grid.clientWidth || window.innerWidth;
  layoutWidth = width;
  const height = narrow.matches ? 120 : 220, gap = 4;
  const secs = sections();
  state.shown = secs.flatMap(s => s.items);
  grid.innerHTML = secs.map((sec, n) => {
    const keys = sec.items.map(i => i.key);
    const all = keys.length && keys.every(k => state.selected.has(k));
    const keepAll = sec.clean && state.status === "todo"
      ? `<button class="pill keep" data-keep-section="${n}">${icon("checkAll")}<span>Keep all ${sec.items.length}</span></button>` : "";
    const rows = justify(sec.items, width, height, gap).map(r =>
      `<div class="row">${r.tiles.map(t => tileHtml(t.item, t.aspect * r.h, r.h)).join("")}</div>`).join("");
    return `<section class="section" data-section="${n}"><div class="section-head">
      <button class="icon-btn sel${all ? " on" : ""}" data-select-section="${n}" title="Select all in this group">${icon(all ? "circleChecked" : "circle")}</button>
      <span>${esc(sec.title)}</span><span class="n">${sec.items.length}</span>${sec.hint ? `<span class="hint">${esc(sec.hint)}</span>` : ""}
      <div class="spacer"></div>${keepAll}</div><div class="rows">${rows}</div></section>`;
  }).join("");
  grid._sections = secs;
  const empty = $("empty");
  empty.hidden = state.shown.length > 0;
  if (!state.shown.length) {
    empty.textContent = state.status === "todo" && !state.q
      ? "Nothing left to review here. Everything has a decision." : "Nothing matches.";
  }
  renderChrome();
}

function counts() {
  const out = { todo: 0, kept: 0, discarded: 0, editing: 0, all: 0 };
  for (const item of state.items) {
    if (state.kind !== "all" && item.kind !== state.kind) continue;
    out.all++;
    if (item.decision in out || !item.decision) out[item.decision || "todo"]++;
  }
  return out;
}

function renderChrome() {
  const c = counts();
  $("statuslinks").innerHTML = STATUSES.filter(([key]) => key !== "editing" || c.editing || state.status === key).map(([key, label, ic]) =>
    `<button class="side-link${state.status === key ? " on" : ""}" data-status="${key}">${icon(ic)}<span>${label}</span><span class="n">${c[key]}</span></button>`).join("");
  const kindCount = k => state.items.filter(i => (k === "all" || i.kind === k) &&
    (state.status === "todo" ? !i.decision : state.status === "all" || i.decision === state.status)).length;
  $("kindchips").innerHTML = [["all", "All", "all"], ...Object.entries(KIND).map(([k, v]) => [k, v.label, v.icon])]
    .filter(([k]) => k === "all" || state.items.some(i => i.kind === k))
    .map(([k, label, ic]) => `<button class="chip${state.kind === k ? " on" : ""}" data-kind="${k}">${icon(ic)}${label} <span class="count">${kindCount(k)}</span></button>`).join("");
  document.querySelectorAll("#sortseg button").forEach(b => b.classList.toggle("on", b.dataset.sort === state.sort));
  $("title").textContent = STATUSES.find(s => s[0] === state.status)[1];
  $("viewcount").textContent = `${state.shown.length} item${state.shown.length === 1 ? "" : "s"}`;
  $("start").hidden = !state.shown.length;
  const done = c.all - c.todo - c.editing;
  $("usagebar").style.width = c.all ? `${(100 * done / c.all).toFixed(1)}%` : "0";
  $("usagetext").textContent = `${done} of ${c.all} have a decision`;
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  $("selectbar").hidden = !n;
  $("navbar").hidden = !!n;
  $("selcount").textContent = n;
  document.body.classList.toggle("selecting", n > 0);
}

function syncTiles() {
  document.querySelectorAll(".tile").forEach(t => {
    const on = state.selected.has(t.dataset.key);
    if (t.classList.contains("selected") !== on) {
      t.classList.toggle("selected", on);
      t.querySelector(".check").innerHTML = icon(on ? "circleChecked" : "circle");
    }
  });
  const secs = $("grid")._sections || [];
  document.querySelectorAll("[data-select-section]").forEach(b => {
    const keys = secs[+b.dataset.selectSection].items.map(i => i.key);
    const all = keys.every(k => state.selected.has(k));
    b.classList.toggle("on", all);
    b.innerHTML = icon(all ? "circleChecked" : "circle");
  });
  renderSelection();
}

function toggleSelect(key, range) {
  if (range && state.anchor) {
    const keys = state.shown.map(i => i.key);
    const [a, b] = [keys.indexOf(state.anchor), keys.indexOf(key)].sort((x, y) => x - y);
    if (a >= 0 && b >= 0) keys.slice(a, b + 1).forEach(k => state.selected.add(k));
  } else if (state.selected.has(key)) {
    state.selected.delete(key);
  } else {
    state.selected.add(key);
  }
  state.anchor = key;
  syncTiles();
}
function clearSelection() { state.selected.clear(); state.anchor = null; syncTiles(); }

// decisions and undo

function viewChoice(item) {
  const v = viewer.view && viewer.item === item ? viewer.view : { name: item.preview, crop: CROP, fill: false };
  const [projection, orientation = "level"] = (v.name || "").split("-");
  return { projection: projection || null, orientation: v.name ? orientation : null, crop: !!v.crop, fill: !!v.fill };
}

async function decide(items, decision, { note } = {}) {
  if (!items.length) return;
  const prev = items.map(i => ({ key: i.key, decision: i.decision, decided_at: i.decided_at, note: i.note }));
  const body = items.map(i => ({
    set: i.id, kind: i.kind, decision, note: (note ?? i.note) || "",
    ...(i.kind === "stitch" ? viewChoice(i) : {}),
  }));
  items.forEach(i => { i.decision = decision; i.decided_at = new Date().toISOString(); if (note != null) i.note = note; });
  const entry = { prev, records: null, queue: viewer.open ? viewer.queue.slice() : null };
  entry.pending = post("/decisions", { items: body }).then(r => { entry.records = r.records; }).catch(err => {
    restore(entry);
    state.undo = state.undo.filter(e => e !== entry);
    toast(`Could not save: ${err.message}`, { error: true });
  });
  state.undo.push(entry);
  if (state.undo.length > 100) state.undo.shift();
  const what = items.length === 1 ? items[0].id : `${items.length} items`;
  toast(`${DECISION_LABEL[decision]}: ${what}`, { undo: true });
  if (viewer.open) { renderGridLater(); } else { renderGrid(); }
}

function restore(entry) {
  for (const p of entry.prev) {
    const item = state.byKey.get(p.key);
    if (item) Object.assign(item, { decision: p.decision, decided_at: p.decided_at, note: p.note });
  }
}

async function undo() {
  const entry = state.undo.pop();
  if (!entry) { toast("Nothing to undo"); return; }
  restore(entry);
  const key = entry.prev[entry.prev.length - 1].key;
  if (viewer.open) {
    let at = viewer.queue.indexOf(key);
    if (at < 0) { viewer.queue.splice(viewer.index, 0, key); at = viewer.index; }
    viewer.show(at, -1);
    renderGridLater();
  } else if (entry.queue) {
    // decided in the viewer, which has closed since (the end of the list): open it again there
    const queue = entry.queue.filter(k => state.byKey.has(k));
    if (!queue.includes(key)) queue.push(key);
    writeUrl(key, false);
    viewer.start(queue, queue.indexOf(key));
    renderGridLater();
  } else {
    renderGrid();
  }
  try {
    await entry.pending;
    if (!entry.records) return;
    await post("/undo", { records: entry.records.map(r => ({ set: r.set, kind: r.kind, at: r.at, decision: r.decision })) });
    toast(entry.prev.length === 1 ? `Undone: ${entry.prev[0].key.split(":")[1]}` : `Undone for ${entry.prev.length} items`);
  } catch (err) {
    toast(`Could not undo: ${err.message}`, { error: true });
  }
}

let gridTimer = 0;
function renderGridLater() { clearTimeout(gridTimer); gridTimer = setTimeout(renderGrid, 400); }

function toast(text, { undo: canUndo = false, error = false } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span>${esc(text)}</span>${canUndo ? "<button>Undo</button>" : ""}`;
  if (canUndo) el.querySelector("button").onclick = () => { el.remove(); undo(); };
  const box = $("toasts");
  box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  setTimeout(() => el.remove(), canUndo ? 6000 : 3500);
}

// the viewer

const viewer = {
  open: false, queue: [], index: 0, item: null, view: null, detail: null,
  s: 1, x: 0, y: 0, fit: 1, w: 1, h: 1, sphere: null, compare: false, frame: null,

  start(keys, index) {
    this.queue = keys;
    this.open = true;
    $("viewer").hidden = false;
    document.body.style.overflow = "hidden";
    if (!narrow.matches && window.innerWidth >= 1100 && setting("info") !== "0") $("vpanel").hidden = false;
    if (narrow.matches) $("vpanel").hidden = true;
    this.show(index, 0);
  },
  close(fromHistory) {
    if (!this.open) return;
    this.open = false;
    this.leaveSphere();
    $("viewer").hidden = true;
    document.body.style.overflow = "";
    const key = this.item?.key;
    this.item = null;
    if (!fromHistory) {
      if (history.state?.view) history.back(); else writeUrl(null, true);
    }
    if (state.gone.length) merge({}); else renderGrid();
    const tile = key && document.querySelector(`.tile[data-key="${CSS.escape(key)}"]`);
    if (tile) { tile.scrollIntoView({ block: "center" }); tile.focus({ preventScroll: true }); }
  },
  show(index, direction) {
    if (index < 0) { toast("This is the first one"); return; }
    if (index >= this.queue.length) { this.finished(); return; }
    if (!state.byKey.has(this.queue[index])) { this.queue.splice(index, 1); this.show(index, direction); return; }
    this.index = index;
    this.item = state.byKey.get(this.queue[index]);
    this.view = null; this.frame = null; this.compare = false; this.detail = null; this.wantEdit = false;
    this.leaveSphere();
    writeUrl(this.item.key, true);
    this.paint(direction);
    this.loadDetail();
    this.prefetch();
  },
  finished() {
    const done = this.queue.filter(k => state.byKey.get(k)?.decision).length;
    this.close();
    toast(`End of the list: ${done} of ${this.queue.length} decided`);
  },
  paint(direction = 0) {
    const item = this.item;
    $("vname").textContent = item.id;
    $("vsub").textContent = [`${this.index + 1} of ${this.queue.length}`, KIND[item.kind].one, item.type !== "panorama" && item.kind === "stitch" ? `type: ${item.type}` : item.kind === "found" ? foundWhat(item) : "",
      item.frames ? `${item.frames} frames` : "", when(item.taken), item.camera].filter(Boolean).join(" · ");
    $("vprogress").style.width = `${(100 * (this.index + 1) / this.queue.length).toFixed(1)}%`;
    $("vimmich").hidden = !item.link;
    if (item.link) $("vimmich").href = item.link;
    $("vedit").hidden = !item.href;
    if (item.href) $("vedit").href = item.href;
    $("vdoedit").hidden = item.kind === "found";
    $("vframes").hidden = item.kind !== "stitch";
    $("vselect").innerHTML = icon(state.selected.has(item.key) ? "circleChecked" : "circle");
    $("vselect").classList.toggle("on", state.selected.has(item.key));
    document.querySelectorAll("[data-decide]").forEach(b => b.classList.toggle("chosen", b.dataset.decide === item.decision));
    $("vundo").disabled = !state.undo.length;
    $("vcompare").hidden = !(item.before || item.previous);
    $("vdoedit").classList.toggle("chosen", item.decision === "editing");
    $("vsphere").hidden = true;
    $("vstrip").hidden = !(setting("strip") === "1" && item.kind === "stitch");
    $("vstrip").innerHTML = "";
    $("vbefore").hidden = true;
    $("vbadge").hidden = true;
    const card = $("vcard");
    card.classList.remove("fling");
    card.style.transform = "";
    card.style.opacity = "";
    const ph = $("vplaceholder");
    ph.classList.toggle("found", item.kind === "found");
    if (item.kind === "found") {
      card.hidden = true;
      ph.hidden = false;
      ph.innerHTML = foundStage(item, { icon, esc });
      return;
    }
    if (!item.screen) {
      card.hidden = true;
      ph.hidden = false;
      ph.innerHTML = `${icon(KIND[item.kind].icon)}<div>${item.kind === "splat"
        ? `3D object scan${item.splats ? `, ${item.splats.toLocaleString()} splats` : ""}` : "Not rendered yet"}</div>
        <a class="pill primary" href="${esc(item.href)}">${icon(item.kind === "splat" ? "sphere" : "pencil")}<span>${item.kind === "splat" ? "Open the 3D viewer" : "Open the editor to render it"}</span></a>`;
      return;
    }
    $("vplaceholder").hidden = true;
    card.hidden = false;
    this.setImage(item.screen, item.w, item.h, item.full);
    if (direction && !reducedMotion.matches) {
      card.animate([{ opacity: 0, transform: `translateX(${direction * 24}px)` }, { opacity: 1, transform: "none" }], { duration: 160, easing: "ease-out" });
    }
  },
  setImage(src, w, h, full) {
    const img = $("vimg");
    this.full = full || null;
    this.fullLoaded = !full || full === src;
    img.onload = null;
    if (!w || !h) {
      img.style.visibility = "hidden";
      img.onload = () => { img.style.visibility = ""; this.w = img.naturalWidth; this.h = img.naturalHeight; this.applySize(); this.fitView(); };
    } else {
      img.style.visibility = "";
      this.w = w; this.h = h;
      this.applySize();
      this.fitView();
    }
    img.src = src;
  },
  applySize() { const img = $("vimg"); img.style.width = `${this.w}px`; img.style.height = `${this.h}px`; },
  fitView() {
    const stage = $("vstage").getBoundingClientRect();
    const top = narrow.matches ? 56 : 64, bottom = narrow.matches ? 92 : 84;
    const availW = stage.width - (narrow.matches ? 0 : 128), availH = stage.height - top - bottom;
    this.fit = Math.min(availW / this.w, availH / this.h);
    this.s = this.fit;
    this.x = (stage.width - this.w * this.s) / 2;
    this.y = top + (availH - this.h * this.s) / 2;
    this.transform();
  },
  transform() {
    $("vimg").style.transform = `translate(${this.x}px, ${this.y}px) scale(${this.s})`;
    if (this.s > this.fit * 1.3 && !this.fullLoaded && this.full) {
      this.fullLoaded = true;
      const pre = new Image();
      pre.onload = () => { if ($("vimg").src.endsWith(this.full) || true) $("vimg").src = this.full; };
      pre.src = this.full;
    }
    const zoomed = this.zoomed();
    $("vstage").style.cursor = zoomed ? "grab" : "";
    if (zoomed) this.badge(`${Math.round(this.s * 100)}%`, 900);
  },
  zoomed() { return this.s > this.fit * 1.02; },
  zoomAt(factor, cx, cy) {
    const s = Math.min(Math.max(this.s * factor, this.fit), Math.max(4, this.fit * 2));
    this.x = cx - (cx - this.x) * (s / this.s);
    this.y = cy - (cy - this.y) * (s / this.s);
    this.s = s;
    if (s === this.fit) { this.fitView(); return; }
    this.transform();
  },
  toggleZoom(cx, cy) {
    const stage = $("vstage").getBoundingClientRect();
    if (this.zoomed()) this.fitView();
    else this.zoomAt(Math.max(1, this.fit * 2.5) / this.s, cx ?? stage.width / 2, cy ?? stage.height / 2);
  },
  badge(text, ms) {
    const b = $("vbadge");
    b.textContent = text;
    b.hidden = false;
    clearTimeout(this.badgeTimer);
    if (ms) this.badgeTimer = setTimeout(() => { b.hidden = true; }, ms);
  },
  prefetch() {
    for (let i = 1; i <= 2; i++) {
      const next = state.byKey.get(this.queue[this.index + i]);
      if (next?.screen) new Image().src = next.screen;
    }
  },
  async loadDetail() {
    const item = this.item;
    renderPanel(item, null);
    if (item.kind === "found") return;
    let detail = state.details.get(item.key);
    if (!detail) {
      try {
        const r = await fetch(`${BASE}/api/item/${item.kind}/${encodeURIComponent(item.id)}`);
        if (!r.ok) throw new Error(r.statusText);
        detail = await r.json();
        state.details.set(item.key, detail);
      } catch (err) {
        if (this.item === item) $("pbody").insertAdjacentHTML("beforeend", `<p class="p-flag">${icon("alert")}Could not load details</p>`);
        return;
      }
    }
    if (this.item !== item) return;
    this.detail = detail;
    this.view = item.kind === "stitch" ? { name: item.preview, crop: CROP && detail.crops.includes(item.preview), fill: false, show: false } : null;
    const edited = item.edit?.state === "done" ? item.edit : null;
    if (edited && this.view && (edited.crop || edited.fill)) {
      this.view.crop = !!edited.crop && detail.crops.includes(item.preview);
      this.view.fill = !!edited.fill && detail.fills?.[item.preview]?.state === "done";
    }
    if (this.view && (this.view.crop || this.view.fill)) this.changeView();
    $("vsphere").hidden = !this.pano();
    renderPanel(item, detail);
    if (!$("vstrip").hidden) renderStrip();
    if (this.wantEdit) { this.wantEdit = false; this.editCurrent(); }
  },
  pano() { return this.detail?.pano?.[this.view?.name] || null; },
  viewUrl(size) {
    const item = this.item, v = this.view, d = this.detail;
    const fill = v.fill && d.fills?.[v.name]?.state === "done";
    const suffix = fill ? (v.show ? "_fillshow" : "_fill") : (v.crop && d.crops.includes(v.name) ? "_crop" : "");
    const version = fill ? d.fills[v.name].v : d.versions[v.name];
    return `${BASE}/img/${encodeURIComponent(item.id)}/${v.name}${suffix}.jpg?v=${version}${size ? `&size=${size}` : ""}`;
  },
  changeView() {
    const same = this.view.name === this.item.preview && !this.view.crop && !this.view.fill;
    if (same) this.setImage(this.item.screen, this.item.w, this.item.h, this.item.full);
    else this.setImage(this.viewUrl("screen"), 0, 0, this.viewUrl());
    $("vsphere").hidden = !this.pano();
    if (this.sphere) { this.leaveSphere(); this.enterSphere(); }
  },
  async enterSphere() {
    const pano = this.pano();
    if (!pano) return;
    if (!document.querySelector("link[data-psv]")) {
      document.head.insertAdjacentHTML("beforeend", `<link rel="stylesheet" data-psv href="${BASE}/vendor/psv/index.css">`);
    }
    const holder = $("vpsv");
    holder.hidden = false;
    $("vcard").hidden = true;
    $("vsphere").classList.add("on");
    const item = this.item;
    try {
      const { Viewer } = await import("psv");
      if (this.item !== item || holder.hidden) return;
      this.sphere = new Viewer({
        container: holder,
        panorama: this.viewUrl(),
        panoData: image => {
          const k = image.width / pano.croppedWidth;
          return Object.fromEntries(Object.entries(pano).map(([key, value]) => [key, Math.round(value * k)]));
        },
        navbar: false,
        defaultZoomLvl: 10,
        mousewheelCtrlKey: false,
        touchmoveTwoFingers: false,
        loadingTxt: "",
      });
      this.badge("Drag to look around", 1600);
    } catch (err) {
      toast(`Sphere viewer failed: ${err.message}`, { error: true });
      this.leaveSphere();
    }
  },
  leaveSphere() {
    if (this.sphere) { this.sphere.destroy(); this.sphere = null; }
    $("vpsv").hidden = true;
    $("vpsv").innerHTML = "";
    $("vsphere").classList.remove("on");
    if (this.item?.screen) $("vcard").hidden = false;
  },
  toggleSphere() { if (this.sphere || !$("vpsv").hidden) this.leaveSphere(); else this.enterSphere(); },
  setCompare(on) {
    const before = this.detail?.previous || this.detail?.before;
    if (!before) return;
    const what = this.detail.previous ? "The version before the edit" : "Before the repair";
    this.compare = on;
    const img = $("vbefore");
    if (on) {
      img.src = `${before.url}?size=screen&v=${before.v}`;
      img.style.cssText = "position:absolute;inset:64px 64px 84px;width:calc(100% - 128px);height:calc(100% - 148px);object-fit:contain;transform:none;background:#000";
      img.hidden = false;
      this.badge(`${what}${before.error != null ? ` · ${before.error.toFixed(1)} px error` : ""}${before.coverage ? ` · ${Math.round(before.coverage[0])}°` : ""}`);
    } else {
      img.hidden = true;
      $("vbadge").hidden = true;
    }
    $("vcompare").classList.toggle("on", on);
  },
  showFrame(frame) {
    if (this.frame === frame.index) { this.frame = null; this.changeView(); $("vbadge").hidden = true; renderStrip(); return; }
    this.frame = frame.index;
    this.setImage(frame.full, 0, 0);
    const why = frame.left_out ? ` · left out: ${frame.left_out}` : frame.part ? ` · moved to ${frame.part}` : "";
    this.badge(`Original frame ${frame.index}${why}`);
    renderStrip();
  },
  decideCurrent(decision) {
    const item = this.item;
    if (!item) return;
    const note = $("pnote") ? $("pnote").value.trim() : undefined;
    decide([item], decision, { note });
    this.fling(decision === "kept" ? 1 : -1, 0);
  },
  skip() {
    if (!this.item) return;
    this.fling(0, 1);
  },
  editCurrent() {
    const item = this.item;
    if (!item) return;
    if (item.kind === "found") { toast("A found set can be edited once it is stitched"); return; }
    if (item.kind !== "stitch") { location.href = item.href; return; }
    if ($("vpanel").hidden) togglePanel(true);
    const box = $("pedit");
    if (!box) { this.wantEdit = true; return; }
    box.scrollIntoView({ block: "nearest", behavior: reducedMotion.matches ? "auto" : "smooth" });
    box.classList.remove("flash");
    void box.offsetWidth;
    box.classList.add("flash");
    $("pproj")?.focus({ preventScroll: true });
  },
  fling(dx, dy) {
    const card = $("vcard");
    const advance = () => this.show(this.index + 1, 1);
    if (reducedMotion.matches || card.hidden) { advance(); return; }
    card.classList.add("fling");
    card.style.transform = `translate(${dx * window.innerWidth}px, ${dy * window.innerHeight}px) rotate(${dx * 8}deg)`;
    card.style.opacity = "0";
    setTimeout(advance, 180);
  },
};

function renderStrip() {
  const strip = $("vstrip");
  const frames = viewer.detail?.frames || [];
  strip.innerHTML = frames.length ? frames.map(f => `<figure data-frame="${f.index}" class="${f.left_out ? "out" : f.part ? "part" : ""}${viewer.frame === f.index ? " on" : ""}"
      title="${esc(`Frame ${f.index}${f.left_out ? `: left out (${f.left_out})` : f.part ? `: moved to ${f.part}` : ""}`)}">
      <img loading="lazy" src="${esc(f.url)}" alt=""><figcaption>${f.index}</figcaption></figure>`).join("")
    : `<span style="padding:8px;color:#bbb">No original frames kept for this set</span>`;
}

function row(ic, main, sub) {
  return main || sub ? `<div class="p-row">${icon(ic)}<div class="t"><span>${main || ""}</span>${sub ? `<small>${sub}</small>` : ""}</div></div>` : "";
}
function select(id, options, value, labels = {}) {
  return `<select id="${id}">${options.map(o => `<option value="${esc(o)}"${o === value ? " selected" : ""}>${esc(labels[o] || o)}</option>`).join("")}</select>`;
}

function renderPanel(item, detail) {
  const flags = item.flags || [];
  const edit = item.edit || {};
  const chip = item.decision
    ? `<span class="p-chip ${item.decision}">${DECISION_LABEL[item.decision]}</span> <small style="color:#9ca3af">${ago(item.decided_at)}</small>`
    : `<span class="p-chip none">To review</span>`;
  const editLine = item.decision === "editing" ? `Making a new version (${edit.state === "running" ? "running" : "waiting in the queue"}${edit.task != null ? `, task ${esc(edit.task)}` : ""})`
    : edit.state === "done" ? "New version from your edit. Hold B to see the one before."
    : edit.state === "failed" ? `The edit failed: ${esc(edit.error || "no reason given")}. The earlier version is still here.` : "";
  const was = item.was === "needs-work" ? `<div class="p-was">Marked "needs work" in an older version of Fuseshot, so it is back here with its note.</div>` : "";
  let html = `<div class="p-section">${chip}${editLine ? `<div class="p-was" style="color:#9ec1f0">${editLine}</div>` : ""}${was}</div>
    <div class="p-section"><div class="p-flags">${flags.length ? flags.map(f => `<div class="p-flag">${icon("alert")}${esc(f)}</div>`).join("")
      : `<div class="p-ok">${icon("checkCircle")}Nothing flagged by the checks</div>`}</div></div>
    <div class="p-section"><textarea id="pnote" rows="1" placeholder="Add a note (saved with the decision)">${esc(item.note)}</textarea></div>
    <div class="p-section"><div class="p-label">Details</div>
    ${row("calendar", when(item.taken))}
    ${item.kind === "found" ? row("magnify", esc(foundWhat(item))) : ""}
    ${row("frames", item.frames ? `${item.frames} frames${item.used && item.used !== item.frames ? `, ${item.used} used` : ""}` : "",
      item.kind === "group" ? `${item.people || 0} people, ${item.swapped || 0} faces swapped in` : item.reason || "")}
    ${item.kind === "stitch" ? row("stitch", [item.model && `${item.model} model`, item.error != null && `${(+item.error).toFixed(1)} px error`].filter(Boolean).join(" · "),
      item.coverage ? `covers ${Math.round(item.coverage[0])}° × ${Math.round(item.coverage[1])}°` : "") : ""}
    ${item.kind === "splat" ? row("cube", item.splats ? `${item.splats.toLocaleString()} splats` : "", item.splat_mb ? `${item.splat_mb} MB` : "") : ""}
    ${row("camera", esc(item.camera))}
    ${row("map", esc(item.place))}
    ${item.library === "folder"
      ? row("folder", "From a folder", esc(item.library_label.replace(/^Folder /, "")))
      : row("image", "From Immich")}</div>`;
  if (detail && item.kind === "stitch") {
    const names = Object.keys(detail.versions).sort();
    const v = viewer.view || { name: item.preview };
    const fill = detail.fills?.[v.name] || {};
    html += `<div class="p-section"><div class="p-label">Preview</div>
      <label class="p-field"><span>View</span>${select("pview", names, v.name)}</label>
      <div class="p-toggles">
        <label title="Crop away the empty border"><input type="checkbox" id="pcrop"${v.crop ? " checked" : ""}${detail.crops.includes(v.name) ? "" : " disabled"}> Crop</label>
        <label title="Border painted in by the local AI model"><input type="checkbox" id="pfill"${v.fill ? " checked" : ""}${fill.state === "done" ? "" : " disabled"}> AI fill</label>
        <label><input type="checkbox" id="pshow"${v.show ? " checked" : ""}${v.fill ? "" : " disabled"}> Show filled areas</label>
      </div>
      <small style="color:#9ca3af">${fill.state === "done" ? `AI fill ready (${Math.round((fill.share || 0) * 1000) / 10}% painted)${fill.stale ? ", from an older render" : ""}`
        : fill.state ? `AI fill ${fill.state}${fill.task != null ? ` (GPU task ${fill.task})` : ""}` : "Render other views or queue an AI fill from the editor."}</small></div>`;
  }
  if (detail) {
    const typeLabels = Object.fromEntries(Object.entries(state.types).map(([t, makes]) => [t, `${t} (${makes})`]));
    typeLabels.auto = `auto (${detail.detected})`;
    const v = viewer.view || { name: item.preview || "" };
    const [proj, orient = "level"] = (v.name || "").split("-");
    const busy = item.decision === "editing";
    html += `<div class="p-section" id="pedit"><div class="p-label">${item.kind === "stitch" ? "Edit" : "Processing"}</div>
      <label class="p-field"><span>Treat as</span>${select("ptype", ["auto", ...Object.keys(state.types)], detail.type_choice, typeLabels)}</label>
      ${item.kind === "stitch" ? `<label class="p-field"><span>Stitch model</span>${select("pmodel", state.models, detail.model_choice)}</label>
      <label class="p-field"><span>Projection</span>${select("pproj", state.projections, proj || state.projections[0])}</label>
      <label class="p-field"><span>Orientation</span>${select("porient", state.orientations, orient)}</label>
      <div class="p-toggles">
        <label title="Keep the cropped version of the new render"><input type="checkbox" id="pecrop"${v.crop ? " checked" : ""}> Crop</label>
        <label title="Paint in the empty border with the local AI model afterwards"><input type="checkbox" id="pefill"${v.fill ? " checked" : ""}> AI fill</label>
      </div>
      <div class="p-actions"><button class="pill primary" id="prerender"${busy ? " disabled" : ""}>${icon("pencil")}<span>Make the new version</span></button>
        <a href="${esc(item.href)}" style="font-size:13px">Full editor page</a></div>
      <small style="color:#9ca3af">Runs as a background job. The result comes back to To review, and B shows the version before it.
        A different stitch model re-aligns the photos first, which takes longer.</small>` : ""}</div>`;
    if (detail.before) {
      html += `<div class="p-section"><div class="p-label">Repair</div>
        ${row("history", `Before: ${detail.before.status || ""}${detail.before.error != null ? `, ${(+detail.before.error).toFixed(1)} px error` : ""}`,
          detail.before.coverage ? `covered ${Math.round(detail.before.coverage[0])}° × ${Math.round(detail.before.coverage[1])}°. Hold B to compare.` : "Hold B to compare.")}
        ${detail.parts?.length ? row("frames", `Split off: ${detail.parts.map(p => `<a href="${BASE}/set/${esc(p)}">${esc(p)}</a>`).join(", ")}`) : ""}</div>`;
    }
    const verify = detail.verify || {};
    if (verify.verified_type || verify.verify_reason) {
      html += `<div class="p-section"><div class="p-label">Overlap check</div>
        ${row("magic", esc([verify.verified_type, verify.verified_confidence != null && `confidence ${verify.verified_confidence}`].filter(Boolean).join(", ")), esc(verify.verify_reason))}</div>`;
    }
  }
  if (item.link || item.href || item.full) html += `<div class="p-section p-links"><div class="p-label">Open</div>
    ${item.link ? `<a href="${esc(item.link)}" target="_blank" rel="noopener">${icon("openInNew")}In Immich</a>` : ""}
    ${item.href ? `<a href="${esc(item.href)}">${icon("pencil")}${item.kind === "stitch" ? "Editor: render, AI fill, alignment" : item.kind === "group" ? "Face choices" : item.kind === "splat" ? "3D viewer" : "Loop page"}</a>` : ""}
    ${item.full ? `<a href="${esc(item.full)}" target="_blank">${icon("image")}Full size image</a>` : ""}</div>`;
  $("pbody").innerHTML = html;
  const note = $("pnote");
  note.addEventListener("input", () => { item.note = note.value; });
  if (!detail) return;
  const saveSelect = (id, path, field) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("change", async () => {
      try {
        await post(path, { set: item.id, [field]: el.value });
        el.classList.add("saved");
        detail[field === "type" ? "type_choice" : "model_choice"] = el.value;
        toast(field === "type" ? `${item.id} will be treated as ${el.value}` : `${item.id}: stitch model ${el.value} on the next run`);
      } catch (err) { el.classList.add("failed"); toast(err.message, { error: true }); }
    });
  };
  saveSelect("ptype", "/type", "type");
  saveSelect("pmodel", "/override", "model");
  if ($("prerender")) $("prerender").onclick = () => startEdit(item, detail);
  if (item.kind === "stitch") {
    const update = () => {
      viewer.view = { name: $("pview").value, crop: $("pcrop").checked, fill: $("pfill").checked, show: $("pshow").checked };
      viewer.changeView();
      renderPanel(item, detail);
    };
    ["pview", "pcrop", "pfill", "pshow"].forEach(id => $(id).addEventListener("change", update));
  }
}

function togglePanel(force) {
  const panel = $("vpanel");
  const show = force ?? panel.hidden;
  panel.hidden = !show;
  $("vinfo").classList.toggle("on", show);
  if (!narrow.matches) localStorage.setItem("fuseshot.info", show ? "1" : "0");
  requestAnimationFrame(() => viewer.fitView());
}

function toggleStrip() {
  if (viewer.item?.kind !== "stitch") return;
  const strip = $("vstrip");
  strip.hidden = !strip.hidden;
  localStorage.setItem("fuseshot.strip", strip.hidden ? "0" : "1");
  $("vframes").classList.toggle("on", !strip.hidden);
  if (!strip.hidden) renderStrip();
}

// gestures on the viewer stage

function setupGestures() {
  const stage = $("vstage");
  const pointers = new Map();
  let start = null, pinch = null, lastTap = 0;
  const hint = $("vhint"), card = $("vcard");
  const local = e => { const r = stage.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

  stage.addEventListener("pointerdown", e => {
    if (e.target.closest(".v-psv, a, button") || !viewer.item?.screen) return;
    stage.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, local(e));
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), s: viewer.s };
      start = null;
      resetCard();
    } else {
      const [x, y] = local(e);
      start = { x, y, vx: viewer.x, vy: viewer.y, zoomed: viewer.zoomed(), moved: false };
    }
  });
  stage.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, local(e));
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      viewer.zoomAt((pinch.s * d / pinch.d) / viewer.s, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
      return;
    }
    if (!start) return;
    const [x, y] = local(e);
    const dx = x - start.x, dy = y - start.y;
    if (Math.hypot(dx, dy) > SWIPE.arm) start.moved = true;
    if (start.zoomed) {
      viewer.x = start.vx + dx; viewer.y = start.vy + dy;
      viewer.transform();
      return;
    }
    if (!start.moved) return;
    const dir = swipeDir(dx, dy);
    const far = Math.max(Math.abs(dx), Math.abs(dy));
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${Math.max(-8, Math.min(8, dx / 40))}deg)`;
    if (far >= SWIPE.tint) {
      hint.className = `v-hint ${dir}`;
      hint.textContent = { right: "Keep", left: "Discard", up: "Edit", down: "Skip" }[dir];
      hint.style.opacity = String(Math.min(1, (far - SWIPE.tint) / (SWIPE.decide - SWIPE.tint) * 0.6 + 0.4));
    } else {
      hint.style.opacity = "0";
    }
  });
  const end = e => {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (pinch) { if (pointers.size < 2) pinch = null; return; }
    if (!start) return;
    const [x, y] = local(e);
    const dx = x - start.x, dy = y - start.y;
    const was = start;
    start = null;
    hint.style.opacity = "0";
    if (!was.moved || Math.hypot(dx, dy) < SWIPE.tap) {
      resetCard();
      const now = Date.now();
      if (now - lastTap < 300) { viewer.toggleZoom(x, y); lastTap = 0; } else lastTap = now;
      return;
    }
    if (was.zoomed) return;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE.decide) { resetCard(true); return; }
    const dir = swipeDir(dx, dy);
    if (dir === "down") { resetCard(); viewer.show(viewer.index + 1, 1); return; }
    if (dir === "up") { resetCard(true); viewer.editCurrent(); return; }
    viewer.decideCurrent(dir === "right" ? "kept" : "discarded");
  };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", e => { pointers.delete(e.pointerId); pinch = null; start = null; resetCard(true); hint.style.opacity = "0"; });
  stage.addEventListener("wheel", e => {
    if (e.target.closest(".v-psv") || !viewer.item?.screen) return;
    e.preventDefault();
    const [x, y] = local(e);
    viewer.zoomAt(Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), x, y);
  }, { passive: false });

  function resetCard(animate) {
    if (animate && !reducedMotion.matches) {
      card.classList.add("fling");
      setTimeout(() => card.classList.remove("fling"), 180);
    }
    card.style.transform = "";
  }
}
function swipeDir(dx, dy) {
  return Math.abs(dx) >= Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
}

// keyboard

const SHORTCUTS = [
  ["Viewer", [
    [["K"], "Keep and go on"], [["D"], "Discard and go on"], [["E"], "Edit: type, model, view, crop, AI fill"],
    [["Space"], "Skip, decide later"], [["U"], "Undo the last decision"], [["←", "→"], "Previous or next"],
    [["I"], "Show or hide info"], [["O"], "Show the original frames"], [["B"], "Hold to compare with the earlier version"],
    [["V"], "View as a sphere"], [["Z"], "Zoom in or out"], [["X"], "Select"], [["Esc"], "Close"],
  ]],
  ["Grid", [
    [["Enter"], "Review one by one"], [["Ctrl", "A"], "Select all shown"], [["Shift", "click"], "Select a range"],
    [["K", "D"], "Keep or discard the selection"], [["Ctrl", "Z"], "Undo"], [["/"], "Search"], [["Esc"], "Clear the selection"],
  ]],
  ["Touch", [
    [["Swipe →"], "Keep"], [["Swipe ←"], "Discard"], [["Swipe ↑"], "Edit"], [["Swipe ↓"], "Skip"],
    [["Long press"], "Select in the grid"], [["Pinch"], "Zoom"], [["Double tap"], "Zoom in or out"],
  ]],
];
function showShortcuts() {
  $("modalbody").innerHTML = SHORTCUTS.map(([title, list]) => `<div><h3>${title}</h3>${list.map(([keys, what]) =>
    `<div class="sc"><div class="keys">${keys.map(k => `<span class="kbd">${esc(k)}</span>`).join("")}</div><span>${esc(what)}</span></div>`).join("")}</div>`).join("");
  $("modal").hidden = false;
}

function onKey(e) {
  if (!$("modal").hidden) { if (e.key === "Escape" || e.key === "?") { $("modal").hidden = true; e.preventDefault(); } return; }
  const typing = e.target.closest?.("input, textarea, select");
  if (typing) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.altKey || e.metaKey) return;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (e.ctrlKey) {
    if (key === "z") { e.preventDefault(); undo(); }
    else if (key === "a" && !viewer.open) { e.preventDefault(); state.shown.forEach(i => state.selected.add(i.key)); syncTiles(); }
    return;
  }
  if (key === "?") { showShortcuts(); return; }
  if (viewer.open) {
    const map = {
      ArrowLeft: () => viewer.show(viewer.index - 1, -1), ArrowRight: () => viewer.show(viewer.index + 1, 1),
      " ": () => viewer.skip(),
      k: () => viewer.decideCurrent("kept"), d: () => viewer.decideCurrent("discarded"),
      u: undo, i: () => togglePanel(), o: toggleStrip, v: () => viewer.toggleSphere(), z: () => viewer.toggleZoom(),
      e: () => viewer.editCurrent(), x: () => { toggleSelect(viewer.item.key); viewer.paint(); },
      b: () => { if (!e.repeat) viewer.setCompare(!viewer.compare); },
      Escape: () => {
        if (viewer.compare) viewer.setCompare(false);
        else if (viewer.sphere || !$("vpsv").hidden) viewer.leaveSphere();
        else if (viewer.zoomed()) viewer.fitView();
        else if (narrow.matches && !$("vpanel").hidden) togglePanel(false);
        else viewer.close();
      },
    };
    if (map[key]) { e.preventDefault(); map[key](); }
    return;
  }
  if (key === "Escape") { if (state.selected.size) clearSelection(); else document.body.classList.remove("menu-open"); return; }
  if (key === "/") { e.preventDefault(); $("search").focus(); return; }
  if (key === "Enter" && !e.target.closest(".tile")) { startReview(); return; }
  if (key === "u") { undo(); return; }
  const decision = { k: "kept", d: "discarded" }[key];
  if (decision && state.selected.size) {
    decide([...state.selected].map(k => state.byKey.get(k)), decision);
    clearSelection();
  }
}
function onKeyUp(e) {
  if (viewer.open && e.key.toLowerCase() === "b" && viewer.compare && performance.now() - (viewer.compareAt || 0) > 250) viewer.setCompare(false);
}

// URL state

function writeUrl(view, replace) {
  const params = new URLSearchParams();
  if (state.status !== "todo") params.set("status", state.status);
  if (state.kind !== "all") params.set("kind", state.kind);
  if (state.sort !== "queue") params.set("sort", state.sort);
  if (state.q) params.set("q", state.q);
  if (view) params.set("view", view);
  const url = `${location.pathname}${params.size ? "?" + params : ""}`;
  if (replace) history.replaceState(view ? { view } : {}, "", url);
  else history.pushState(view ? { view } : {}, "", url);
}
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.status = STATUSES.some(s => s[0] === p.get("status")) ? p.get("status") : "todo";
  state.kind = KIND[p.get("kind")] ? p.get("kind") : "all";
  state.sort = p.get("sort") === "date" ? "date" : "queue";
  state.q = p.get("q") || "";
  $("search").value = state.q;
  return p.get("view");
}

function openItem(key) {
  const keys = state.shown.map(i => i.key);
  let index = keys.indexOf(key);
  const queue = index >= 0 ? keys : [key];
  if (index < 0) index = 0;
  writeUrl(key, false);
  viewer.start(queue, index);
}
function startReview() {
  if (!state.shown.length) return;
  openItem(state.shown[0].key);
}

// wiring

function wire() {
  $("grid").addEventListener("click", e => {
    const sel = e.target.closest("[data-select-section]");
    if (sel) {
      const keys = $("grid")._sections[+sel.dataset.selectSection].items.map(i => i.key);
      const all = keys.every(k => state.selected.has(k));
      keys.forEach(k => all ? state.selected.delete(k) : state.selected.add(k));
      syncTiles();
      return;
    }
    const keepAll = e.target.closest("[data-keep-section]");
    if (keepAll) {
      decide($("grid")._sections[+keepAll.dataset.keepSection].items, "kept");
      return;
    }
    const tile = e.target.closest(".tile");
    if (!tile) return;
    if (e.target.closest("[data-check]") || state.selected.size || e.shiftKey || e.ctrlKey || e.metaKey) {
      toggleSelect(tile.dataset.key, e.shiftKey);
    } else {
      openItem(tile.dataset.key);
    }
  });
  let press = null, pressed = false;
  $("grid").addEventListener("pointerdown", e => {
    const tile = e.target.closest(".tile");
    if (!tile || e.pointerType === "mouse") return;
    press = { x: e.clientX, y: e.clientY, timer: setTimeout(() => {
      pressed = true;
      navigator.vibrate?.(15);
      toggleSelect(tile.dataset.key);
    }, 450) };
  });
  const cancelPress = e => {
    if (!press) return;
    if (e.type === "pointermove" && Math.hypot(e.clientX - press.x, e.clientY - press.y) < 10) return;
    clearTimeout(press.timer);
    press = null;
  };
  ["pointermove", "pointerup", "pointercancel"].forEach(t => $("grid").addEventListener(t, cancelPress));
  $("grid").addEventListener("contextmenu", e => { if (e.target.closest(".tile") && e.pointerType !== "mouse" && pressed) e.preventDefault(); });
  $("grid").addEventListener("click", e => { if (pressed) { pressed = false; e.stopImmediatePropagation(); e.preventDefault(); } }, true);
  $("grid").addEventListener("error", e => {
    const img = e.target;
    if (img.tagName !== "IMG") return;
    const tries = Number(img.dataset.tries || 0);
    if (tries < 3) {
      img.dataset.tries = tries + 1;
      setTimeout(() => { img.src = `${img.src.split("&retry=")[0]}&retry=${tries + 1}`; }, 1500 * (tries + 1));
    } else {
      const item = state.byKey.get(img.closest(".tile")?.dataset.key);
      img.outerHTML = `<div class="placeholder">${icon(KIND[item?.kind || "stitch"].icon)}</div>`;
    }
  }, true);
  $("grid").addEventListener("keydown", e => {
    const tile = e.target.closest(".tile");
    if (tile && e.key === "Enter") { e.preventDefault(); openItem(tile.dataset.key); }
  });
  $("sidebar").addEventListener("click", e => {
    const b = e.target.closest("[data-status]");
    if (!b) return;
    state.status = b.dataset.status;
    clearSelection();
    document.body.classList.remove("menu-open");
    writeUrl(null, true);
    renderGrid();
    window.scrollTo(0, 0);
  });
  $("kindchips").addEventListener("click", e => {
    const b = e.target.closest("[data-kind]");
    if (!b) return;
    state.kind = b.dataset.kind;
    writeUrl(null, true);
    renderGrid();
  });
  $("sortseg").addEventListener("click", e => {
    const b = e.target.closest("[data-sort]");
    if (!b) return;
    state.sort = b.dataset.sort;
    writeUrl(null, true);
    renderGrid();
  });
  let searchTimer = 0;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = $("search").value.trim(); writeUrl(null, true); renderGrid(); }, 150);
  });
  $("start").onclick = startReview;
  $("menu").onclick = () => document.body.classList.toggle("menu-open");
  $("main").addEventListener("click", () => document.body.classList.remove("menu-open"), true);
  $("help").onclick = showShortcuts;
  $("modalclose").onclick = () => { $("modal").hidden = true; };
  $("modal").addEventListener("click", e => { if (e.target === $("modal")) $("modal").hidden = true; });
  $("clearsel").onclick = clearSelection;
  $("selall").onclick = () => { state.shown.forEach(i => state.selected.add(i.key)); syncTiles(); };
  document.querySelectorAll("[data-bulk]").forEach(b => b.onclick = () => {
    decide([...state.selected].map(k => state.byKey.get(k)), b.dataset.bulk);
    clearSelection();
  });

  $("vclose").onclick = () => viewer.close();
  $("vprev").onclick = () => viewer.show(viewer.index - 1, -1);
  $("vnext").onclick = () => viewer.show(viewer.index + 1, 1);
  $("vinfo").onclick = () => togglePanel();
  $("pclose").onclick = () => togglePanel(false);
  $("vframes").onclick = toggleStrip;
  $("vsphere").onclick = () => viewer.toggleSphere();
  $("vundo").onclick = undo;
  $("vskip").onclick = () => viewer.skip();
  $("vdoedit").onclick = () => viewer.editCurrent();
  $("vselect").onclick = () => { toggleSelect(viewer.item.key); viewer.paint(); };
  const compare = $("vcompare");
  compare.addEventListener("pointerdown", () => { viewer.compareAt = performance.now(); viewer.setCompare(true); });
  compare.addEventListener("pointerup", () => { if (performance.now() - viewer.compareAt > 250) viewer.setCompare(false); });
  compare.addEventListener("pointerleave", () => { if (viewer.compare && performance.now() - viewer.compareAt > 250) viewer.setCompare(false); });
  document.querySelectorAll("[data-decide]").forEach(b => b.onclick = () => viewer.decideCurrent(b.dataset.decide));
  $("vstrip").addEventListener("click", e => {
    const fig = e.target.closest("[data-frame]");
    if (!fig) return;
    const frame = viewer.detail.frames.find(f => f.index === +fig.dataset.frame);
    if (frame) viewer.showFrame(frame);
  });
  setupGestures();
  document.addEventListener("keydown", e => { if (e.key.toLowerCase() === "b" && !e.repeat) viewer.compareAt = performance.now(); onKey(e); });
  document.addEventListener("keyup", onKeyUp);
  window.addEventListener("popstate", () => {
    const view = new URLSearchParams(location.search).get("view");
    if (!view) viewer.close(true);
    else if (state.byKey.has(view) && !viewer.open) openFromUrl(view);
  });
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (viewer.open && viewer.item?.screen) viewer.fitView();
      if (Math.abs($("grid").clientWidth - layoutWidth) > 2) renderGrid();
    }, 120);
  });
}

function openFromUrl(view) {
  const keys = state.shown.map(i => i.key);
  const index = keys.indexOf(view);
  viewer.start(index >= 0 ? keys : [view], Math.max(index, 0));
}

async function load() {
  fillIcons();
  const view = readUrl();
  wire();
  try {
    const r = await fetch(`${BASE}/api/items`);
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    state.items = data.items;
    state.types = data.types;
    state.models = data.models;
    state.projections = data.projections || [];
    state.orientations = data.orientations || [];
    state.byKey = new Map(state.items.map(i => [i.key, i]));
    showRunning(data.running);
  } catch (err) {
    $("empty").hidden = false;
    $("empty").textContent = `Could not load the sets: ${err.message}`;
    return;
  }
  renderGrid();
  if (view && state.byKey.has(view)) openFromUrl(view);
  if (!listen(BASE, { refresh, apply: merge })) {
    setInterval(() => { if (state.running || state.items.some(i => i.decision === "editing")) refresh(); }, 15000);
  }
}

function timeLeft(seconds) {
  if (seconds == null) return "time left not known yet";
  if (seconds < 90) return `about ${Math.round(seconds)} s left`;
  if (seconds < 5400) return `about ${Math.round(seconds / 60)} min left`;
  return `about ${Math.floor(seconds / 3600)} h ${String(Math.round(seconds % 3600 / 60)).padStart(2, "0")} min left`;
}

// A run going on in the background: its stage, how far along, and the time left
function showRunning(run) {
  state.running = run || null;
  $("running").hidden = !run;
  if (!run) return;
  const share = run.total ? run.done / run.total : 0;
  $("runstage").textContent = run.stage.charAt(0).toUpperCase() + run.stage.slice(1);
  $("runbar").style.width = `${Math.round(share * 100)}%`;
  $("runtext").textContent = `${run.done.toLocaleString()} of ${(run.total || 0).toLocaleString()} ${run.unit}, ${timeLeft(run.seconds_left)}`;
}

// Edit: a new version made by a background job, back in To review when it is done

async function startEdit(item, detail) {
  const button = $("prerender");
  if (button) button.disabled = true;
  const from = viewer.view && viewer.item === item ? viewer.viewUrl().split("/").pop().split("?")[0] : undefined;
  const body = {
    set: item.id, kind: item.kind, projection: $("pproj").value, orientation: $("porient").value,
    crop: $("pecrop").checked, fill: $("pefill").checked, note: $("pnote")?.value.trim() || "", from,
  };
  try {
    const r = await post("/edit", body);
    Object.assign(item, { decision: "editing", decided_at: r.record.at, edit: { state: "queued", task: r.task }, previous: true });
    state.details.delete(item.key);
    toast(`Editing ${item.id}: it comes back to review when the new version is ready`);
    if (viewer.open && viewer.item === item) viewer.fling(0, -1);
    renderGridLater();
  } catch (err) {
    if (button) button.disabled = false;
    toast(`Could not start the edit: ${err.message}`, { error: true });
  }
}

async function refresh() {
  try {
    const r = await fetch(`${BASE}/api/items`);
    if (!r.ok) return;
    const data = await r.json();
    const keys = new Set(data.items.map(i => i.key));
    merge({ changed: data.items, removed: state.items.filter(i => !keys.has(i.key)).map(i => i.key), running: data.running });
  } catch { /* the next change tries again */ }
}

// Changes from the server: new and changed items, and items gone (a found set once it is stitched).
// The one open in the viewer stays until the viewer moves on.
function merge({ changed = [], removed = [], running }) {
  if (running !== undefined) showRunning(running);
  for (const fresh of changed) {
    const item = state.byKey.get(fresh.key);
    if (!item) { state.items.push(fresh); state.byKey.set(fresh.key, fresh); continue; }
    const finished = item.decision === "editing" && fresh.decision !== "editing";
    if (viewer.open && viewer.item === item && !finished) continue;
    Object.assign(item, fresh);
    if (finished) {
      state.details.delete(item.key);
      toast(fresh.edit?.state === "failed" ? `${item.id}: the edit failed` : `${item.id}: new version ready to review`, { error: fresh.edit?.state === "failed" });
    }
  }
  const held = viewer.open ? viewer.item?.key : null;
  const gone = new Set([...state.gone, ...removed]);
  state.gone = held && gone.delete(held) ? [held] : [];
  if (gone.size) {
    state.items = state.items.filter(i => !gone.has(i.key));
    gone.forEach(k => { state.byKey.delete(k); state.selected.delete(k); state.details.delete(k); });
  }
  if (viewer.open) renderGridLater(); else renderGrid();
}

load();
