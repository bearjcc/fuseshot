// Settings, and the first-run setup in its place when nothing is set up yet: where the photos
// come from (Immich, folders) and which results to make. The Immich key is sent once and
// never shown again; the server only says whether one is saved.
const $ = id => document.getElementById(id);

let ui = null;
let current = null;
let folders = [];

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function call(path, body) {
  const r = await fetch(ui.base + path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toggle(id, on, label, disabled) {
  return `<label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""} ${disabled ? "disabled" : ""}>
    <span class="track"><span class="thumb"></span></span><span class="sr">${esc(label)}</span></label>`;
}

function page(s, first) {
  const ro = !s.can_change;
  const dis = ro ? "disabled" : "";
  return `<div class="toolbar"><div class="title-row"><h1>${first ? "Set up Fuseshot" : "Settings"}</h1></div></div>
  <div class="settings-body">
    ${first ? `<p class="lead">Tell Fuseshot where your photos are and what to make from them. It only reads your photos;
      what it makes goes to its own folder until you keep it.</p>` : ""}
    ${ro ? `<p class="banner">${ui.icon("alert")}<span>These settings can be changed only on the computer Fuseshot runs on,
      or from anywhere once the review page has a token (FUSESHOT_REVIEW_TOKEN).</span></p>` : ""}
    ${s.cli_sources ? `<p class="banner">${ui.icon("info")}<span>Sources were given on the command line, so those are
      used while this page is open; what you save here is used next time.</span></p>` : ""}
    ${s.unknown.length ? `<p class="banner">${ui.icon("alert")}<span>Not read from ${esc(s.settings_file)}:
      ${s.unknown.map(esc).join(", ")}. A typo?</span></p>` : ""}
    <section class="set-card">
      <div class="set-head">${ui.icon("serverNetwork")}<div><h2>Immich</h2><p>Look through an Immich library</p></div>
        ${toggle("simmich", s.immich.on, "Use Immich", ro)}</div>
      <div class="set-fields" id="simmichfields" ${s.immich.on ? "" : "hidden"}>
        <label class="field"><span>Server address</span>
          <input id="surl" type="url" inputmode="url" placeholder="http://192.168.1.10:2283" value="${esc(s.immich.url)}" ${dis}></label>
        <label class="field"><span>API key</span>
          <input id="skey" type="password" autocomplete="off" spellcheck="false" ${dis}
            placeholder="${s.immich.key_saved ? "Saved. Type a new one to replace it" : "From Account Settings > API Keys in Immich"}"></label>
        <div class="set-row"><button class="pill" id="stest" ${dis}>${ui.icon("link")}<span>Test connection</span></button>
          <span class="set-note" id="stestout" aria-live="polite">${s.immich.key_saved ? ui.icon("key") + "Key saved" : ""}</span></div>
      </div>
    </section>
    <section class="set-card">
      <div class="set-head">${ui.icon("folder")}<div><h2>Folders</h2><p>Photo folders on this computer</p></div></div>
      <div id="sfolders" class="set-list"></div>
      <div class="set-row add">
        <input id="snewfolder" class="grow" placeholder="Folder path, or browse" ${dis} spellcheck="false">
        <button class="pill" id="sbrowse" ${dis}>${ui.icon("folderOpen")}<span>Browse</span></button>
        <button class="pill" id="saddfolder" ${dis}>${ui.icon("plus")}<span>Add</span></button>
      </div>
      <div id="sbrowser" class="browser" hidden></div>
    </section>
    <section class="set-card">
      <div class="set-head">${ui.icon("image")}<div><h2>Kept results</h2><p>The folder kept results from folder sources are copied to</p></div></div>
      <label class="field"><span>Output folder</span><input id="soutput" value="${esc(s.output)}" ${dis} spellcheck="false"></label>
    </section>
    <section class="set-card">
      <div class="set-head">${ui.icon("magic")}<div><h2>What to make</h2><p>Each needs its own tools; missing ones say how to add them</p></div></div>
      <div class="set-list">${Object.entries(s.labels).map(([name, label]) => `
        <div class="set-item"><div class="grow"><b>${esc(label)}</b><div class="set-status" id="sstat-${name}">Checking…</div></div>
          ${toggle("sout-" + name, s.outputs[name], label, ro)}</div>`).join("")}</div>
    </section>
    <section class="set-card">
      <div class="set-head">${ui.icon("download")}<div><h2>Optional models</h2><p>Not part of Fuseshot. Each has its own licence, shown before anything is downloaded</p></div></div>
      <div id="spacks" class="set-list"><p class="set-note">Checking…</p></div>
    </section>
    <section class="set-card" id="spipeline">
      <div class="set-head">${ui.icon("play")}<div><h2>Scan</h2><p>Reads your sources, finds sets, stitches previews. Progress is saved so you can pause and resume.</p></div></div>
      <div class="set-row" id="spipebtns">
        <button class="pill primary" id="sscan" ${first || ro ? "disabled" : ""}>${ui.icon("play")}<span>Start</span></button>
        <button class="pill" id="spause" hidden ${ro ? "disabled" : ""}>${ui.icon("pause")}<span>Pause</span></button>
        <button class="pill" id="sresume" hidden ${ro ? "disabled" : ""}>${ui.icon("play")}<span>Resume</span></button>
      </div>
      <p class="set-note" id="spipestate" aria-live="polite">Not running.</p>
      <pre class="log-tail" id="spipelog" hidden></pre>
    </section>
    <div class="set-actions">
      <button class="pill primary" id="ssave" ${dis}>${ui.icon("check")}<span>Save</span></button>
      <span class="set-note" id="ssaved" aria-live="polite"></span>
    </div>
  </div>`;
}

function renderFolders() {
  const ro = !current.can_change;
  $("sfolders").innerHTML = folders.length ? folders.map((f, i) => `
    <div class="set-item"><span class="grow path" title="${esc(f.path)}">${esc(f.path)}</span>
      <label class="small-switch">Subfolders ${toggle("srec-" + i, f.recursive, "Include subfolders", ro)}</label>
      <button class="icon-btn" data-remove="${i}" title="Remove" ${ro ? "disabled" : ""}>${ui.icon("delete")}</button></div>`).join("")
    : `<p class="set-note">No folders yet.</p>`;
  folders.forEach((f, i) => { $("srec-" + i).onchange = e => { f.recursive = e.target.checked; }; });
  $("sfolders").querySelectorAll("[data-remove]").forEach(b => b.onclick = () => {
    folders.splice(+b.dataset.remove, 1);
    renderFolders();
  });
}

function addFolder(path) {
  path = path.trim();
  if (!path || folders.some(f => f.path === path)) return;
  folders.push({ path, recursive: true });
  $("snewfolder").value = "";
  $("sbrowser").hidden = true;
  renderFolders();
}

async function browse(path) {
  const box = $("sbrowser");
  box.hidden = false;
  try {
    const b = await call("/settings/browse", { path });
    box.innerHTML = `<div class="browser-bar">
        <button class="icon-btn" id="bup" title="Up" ${b.parent ? "" : "disabled"}>${ui.icon("arrowUp")}</button>
        <span class="grow path">${esc(b.path || "Pick a place to start")}</span>
        ${b.path ? `<button class="pill primary" id="buse">${ui.icon("check")}<span>Use this folder</span></button>` : ""}</div>
      <div class="chips">${b.roots.map(r => `<button class="chip" data-go="${esc(r)}">${esc(r)}</button>`).join("")}</div>
      <div class="browser-list">${b.folders.map(n => `<button class="browser-row" data-into="${esc(n)}">${ui.icon("folder")}<span>${esc(n)}</span></button>`).join("")
        || (b.path ? `<p class="set-note">No folders inside.</p>` : "")}
        ${b.more ? `<p class="set-note">${b.more} more not shown; type the path instead.</p>` : ""}</div>`;
    const sep = b.path.includes("\\") ? "\\" : "/";
    box.querySelectorAll("[data-go]").forEach(el => el.onclick = () => browse(el.dataset.go));
    box.querySelectorAll("[data-into]").forEach(el => el.onclick = () => browse(b.path.replace(/[\\/]$/, "") + sep + el.dataset.into));
    if (b.parent) $("bup").onclick = () => browse(b.parent);
    if (b.path) $("buse").onclick = () => addFolder(b.path);
  } catch (err) {
    box.innerHTML = `<p class="set-note error">${esc(err.message)}</p>`;
  }
}

function gather() {
  const outputs = {};
  for (const name of Object.keys(current.labels)) outputs[name] = $("sout-" + name).checked;
  const immich = { on: $("simmich").checked, url: $("surl").value.trim() };
  if ($("skey").value.trim()) immich.key = $("skey").value.trim();
  return { immich, folders, output: $("soutput").value.trim(), outputs };
}

async function showOutputs() {
  try {
    const found = await call("/api/settings/outputs");
    for (const [name, o] of Object.entries(found)) {
      const el = $("sstat-" + name);
      if (!el) continue;
      el.innerHTML = (o.ready ? `<span class="ok">${ui.icon("checkCircle")}Ready</span>`
        : `<span class="missing">${ui.icon("alert")}Needs ${esc(o.missing.join(", "))}</span>`)
        + o.hints.map(h => `<code>${esc(h)}</code>`).join("") + (o.note ? `<span class="hint">${esc(o.note)}</span>` : "");
    }
  } catch { /* the switches still work without it */ }
}

function packRow(name, p) {
  const ro = !current.can_change;
  const state = p.installed ? `<span class="ok">${ui.icon("checkCircle")}Installed</span>`
    : `<span class="set-note">${p.size_mb.toLocaleString()} MB download</span>`;
  return `<div class="set-item pack"><div class="grow"><b>${esc(p.title)}</b><div class="set-status">${esc(p.what)}</div>
      <div class="set-status">${state}${p.needs ? `<code>${esc(p.needs)}</code>` : ""}</div>
      <div class="licence" id="slic-${name}" hidden>
        <p><b>${esc(p.licence)}.</b> ${esc(p.plain)} <a href="${esc(p.link)}" target="_blank" rel="noopener">Read the licence</a></p>
        <label class="agree"><input type="checkbox" id="sagree-${name}"> I have read the licence and agree to it</label>
        <div class="set-row"><button class="pill primary" id="sinstall-${name}" disabled>${ui.icon("download")}<span>Agree and install</span></button>
          <button class="pill" id="scancel-${name}">Cancel</button><span class="set-note" id="spackout-${name}" aria-live="polite"></span></div>
      </div></div>
    ${p.installed ? "" : `<button class="pill" id="sopen-${name}" ${ro ? "disabled" : ""}>${esc(p.button)}</button>`}</div>`;
}

async function showPacks() {
  let packs;
  try { packs = await call("/api/settings/packs"); } catch { $("spacks").innerHTML = ""; return; }
  $("spacks").innerHTML = Object.entries(packs).map(([name, p]) => packRow(name, p)).join("");
  for (const [name, p] of Object.entries(packs)) {
    if (p.installed) continue;
    const box = $("slic-" + name), agree = $("sagree-" + name), go = $("sinstall-" + name);
    $("sopen-" + name).onclick = () => { box.hidden = false; $("sopen-" + name).hidden = true; };
    $("scancel-" + name).onclick = () => { box.hidden = true; agree.checked = false; go.disabled = true; $("sopen-" + name).hidden = false; };
    agree.onchange = () => { go.disabled = !agree.checked; };
    go.onclick = async () => {
      go.disabled = true;
      $("spackout-" + name).textContent = "Downloading and checking…";
      try {
        await call("/settings/install", { pack: name, agree: agree.checked });
        showPacks();
      } catch (err) {
        go.disabled = false;
        $("spackout-" + name).innerHTML = `<span class="missing">${ui.icon("alert")}${esc(err.message)}</span>`;
      }
    };
  }
}

function wire(first) {
  const ro = !current.can_change;
  $("simmich").onchange = e => { $("simmichfields").hidden = !e.target.checked; };
  $("stest").onclick = async () => {
    const out = $("stestout");
    out.textContent = "Testing…";
    try {
      const r = await call("/settings/test", { url: $("surl").value.trim(), key: $("skey").value.trim() });
      out.innerHTML = r.ok ? `<span class="ok">${ui.icon("checkCircle")}Signed in as ${esc(r.user)}, ${Number(r.assets || 0).toLocaleString()} photos and videos</span>`
        : `<span class="missing">${ui.icon("alert")}${esc(r.error)}</span>`;
    } catch (err) {
      out.innerHTML = `<span class="missing">${ui.icon("alert")}${esc(err.message)}</span>`;
    }
  };
  $("sbrowse").onclick = () => browse($("snewfolder").value.trim());
  $("saddfolder").onclick = () => addFolder($("snewfolder").value);
  $("snewfolder").onkeydown = e => { if (e.key === "Enter") addFolder(e.target.value); };
  $("ssave").onclick = async () => {
    $("ssave").disabled = true;
    try {
      current = { ...current, ...(await call("/settings", gather())) };
      $("skey").value = "";
      $("skey").placeholder = "Saved. Type a new one to replace it";
      $("ssaved").innerHTML = `<span class="ok">${ui.icon("checkCircle")}Saved</span>`;
      $("sscan").disabled = false;
      if (first) $("sscan").classList.add("primary");
    } catch (err) {
      $("ssaved").innerHTML = `<span class="missing">${ui.icon("alert")}${esc(err.message)}</span>`;
    } finally {
      $("ssave").disabled = false;
    }
  };
  const pipeStart = $("sscan");
  const pipePause = $("spause");
  const pipeResume = $("sresume");
  const pipeState = $("spipestate");
  const pipeLog = $("spipelog");

  function showPipeline(p) {
    if (!p) return;
    const run = p.active;
    pipeStart.hidden = run || p.can_resume;
    pipePause.hidden = !p.can_pause;
    pipeResume.hidden = !p.can_resume;
    pipeStart.disabled = ro || p.needs_setup || run;
    let msg = "Not running.";
    if (p.state === "paused") msg = `Paused${p.paused_at ? ` after ${p.paused_at}` : ""}.`;
    else if (run) msg = p.progress?.stage ? `Running: ${p.progress.stage}` : "Running…";
    else if (p.state === "done") msg = "Last scan finished. Start again when you have new photos.";
    else if (p.state === "failed") msg = `Last scan failed: ${p.error || "see log"}`;
    pipeState.textContent = msg;
    if (p.log_tail) {
      pipeLog.hidden = false;
      pipeLog.textContent = p.log_tail;
    }
  }

  async function refreshPipeline() {
    try { showPipeline(await call("/api/pipeline")); } catch { /* optional */ }
  }

  pipeStart.onclick = async () => {
    pipeStart.disabled = true;
    try {
      await call("/settings/scan", {});
      ui.toast("Scan started: new sets show up in review as they are found");
      await refreshPipeline();
    } catch (err) {
      ui.toast(`Could not start the scan: ${err.message}`, { error: true });
    } finally {
      pipeStart.disabled = false;
    }
  };
  pipePause.onclick = async () => {
    try {
      await call("/settings/pipeline/pause", {});
      ui.toast("Pausing after the current step…");
      await refreshPipeline();
    } catch (err) {
      ui.toast(err.message, { error: true });
    }
  };
  pipeResume.onclick = async () => {
    try {
      await call("/settings/pipeline/resume", {});
      ui.toast("Scan resumed");
      await refreshPipeline();
    } catch (err) {
      ui.toast(err.message, { error: true });
    }
  };
  refreshPipeline();
  setInterval(refreshPipeline, 5000);
}

// Show the settings in place of the grid. first: nothing is set up yet.
export async function openSettings(options) {
  ui = options;
  const root = $("settings");
  $("main").hidden = true;
  root.hidden = false;
  document.title = "Settings - Fuseshot";
  try {
    current = await call("/api/settings");
  } catch (err) {
    root.innerHTML = `<p class="empty">Could not load the settings: ${esc(err.message)}</p>`;
    return;
  }
  const first = options.first || current.needs_setup;
  folders = current.folders.map(f => ({ ...f }));
  root.innerHTML = page(current, first);
  renderFolders();
  wire(first);
  showOutputs();
  showPacks();
}
