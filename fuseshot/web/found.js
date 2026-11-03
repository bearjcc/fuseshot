// Sets the scan found that are not stitched yet, shown as a strip of their frames, and the
// changes to the list the server pushes while a run goes on.

export const FOUND = { label: "Found", one: "Found set", icon: "magnify" };
const STATUS = {
  found: ["Found", "magnify", "Found by the scan. Discard it now and it is never stitched."],
  queued: ["Queued", "history", "Waiting to be stitched. Discard it now and it comes off the queue."],
  stitching: ["Stitching", "stitch", "Being stitched now. Discard it and the result stays out of review."],
};

export function foundStatus(item, icon) {
  const [label, ic] = STATUS[item.status] || STATUS.found;
  return `<span class="found-status ${item.status}">${icon(ic)}${label}</span>`;
}

export function foundStrip(item, esc) {
  return `<div class="found-strip">${item.strip.map(src =>
    `<img loading="lazy" decoding="async" src="${esc(src)}" alt="" draggable="false">`).join("")}</div>`;
}

export function foundWhat(item) {
  const sure = item.confidence != null ? `, ${Math.round(item.confidence * 100)}% sure` : "";
  return item.checked ? `${item.type}${sure}` : `looks like a ${item.guess || item.type}${sure}, not checked yet`;
}

export function foundStage(item, { icon, esc }) {
  return `${foundStrip(item, esc)}
    <div class="found-line">${foundStatus(item, icon)}<span>${esc(foundWhat(item))} · ${item.frames} frames</span></div>
    <small>${(STATUS[item.status] || STATUS.found)[2]}</small>`;
}

export function byPromise(a, b) {
  return a.order - b.order || (b.confidence ?? 0) - (a.confidence ?? 0) || a.id.localeCompare(b.id);
}

// Server-sent events: "hello" when the stream (re)starts, then "items" with what changed.
// A page that fell behind gets "reset" and loads everything again.
export function listen(base, { refresh, apply }) {
  if (!window.EventSource) return null;
  const source = new EventSource(`${base}/api/events`);
  source.addEventListener("hello", () => refresh());
  source.addEventListener("reset", () => refresh());
  source.addEventListener("items", e => {
    try { apply(JSON.parse(e.data)); } catch { refresh(); }
  });
  return source;
}

export function foundBadge(item, { icon, esc }) {
  const [label, ic] = STATUS[item.status] || STATUS.found;
  return `<span class="bl found ${item.status}">${icon(ic)}${label} · ${esc(item.checked ? item.type : item.guess || item.type)}</span>`;
}
