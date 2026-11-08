// Blind A/B: which side is enhanced is chosen at random for each photo.
import { ICONS } from "./icons.js";

document.querySelectorAll("[data-icon]").forEach(el => {
  const d = ICONS[el.dataset.icon];
  if (d) el.insertAdjacentHTML("afterbegin", `<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`);
});

const left = document.getElementById("left"), right = document.getElementById("right");
const count = document.getElementById("count");
let queue = [], current = null, enhancedSide = "left";

function toast(text, error) {
  const t = document.createElement("div");
  t.className = "toast" + (error ? " error" : "");
  t.textContent = text;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

function show() {
  current = queue.shift() || null;
  count.textContent = current ? `${queue.length + 1} to compare` : "Nothing left to compare";
  document.querySelectorAll(".act").forEach(b => { b.disabled = !current; });
  if (!current) { left.removeAttribute("src"); right.removeAttribute("src"); return; }
  enhancedSide = Math.random() < 0.5 ? "left" : "right";
  const file = side => `${BASE}/enhanceresult/${current.id}/view-${side === enhancedSide ? "enhanced" : "original"}.jpg`;
  left.src = file("left");
  right.src = file("right");
}

async function answer(pick) {
  if (!current) return;
  const r = await fetch(BASE + "/enhance/decide", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: current.id, pick, enhanced_side: enhancedSide})});
  const body = await r.json().catch(() => ({}));
  if (!r.ok) return toast(body.error || "Could not save", true);
  const words = {enhanced: "Kept the enhanced photo", original: "Kept the original", face: "Kept the original; face marked"};
  toast(`${words[pick]} (A was the ${enhancedSide === "left" ? "enhanced" : "original"} one)`);
  show();
}

document.querySelectorAll("[data-side]").forEach(b => b.onclick = () =>
  answer(b.dataset.side === enhancedSide ? "enhanced" : "original"));
document.querySelector("[data-pick=face]").onclick = () => answer("face");
addEventListener("keydown", e => {
  if (e.key === "1") answer(enhancedSide === "left" ? "enhanced" : "original");
  else if (e.key === "2") answer(enhancedSide === "right" ? "enhanced" : "original");
  else if (e.key === "f" || e.key === "F") answer("face");
  else if (e.key === "Escape") location.href = BASE + "/enhance";
});

const r = await fetch(BASE + "/api/enhance/queue");
queue = (await r.json()).items || [];
show();
