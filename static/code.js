"use strict";
// ============================================================ Forge Studio — Code & Git panel
// Uses the same globals as app.js: $, $$, ic, esc, api, act, toast, store, pkey, S, project, fileUrl,
// showTab, selectProject, loadState. Everything no-ops when !S.cur.

const C = { tabs: [], active: null, expanded: new Set(), git: null, view: "files",
            filesCache: null, sideW: 240, treeSel: null, searchT: 0 };

const EXT_LANG = {
  js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js", json: "js",
  py: "py", pyw: "py", rb: "rb", php: "php", lua: "lua",
  html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml", svelte: "xml",
  css: "css", scss: "css", sass: "css", less: "css",
  java: "c", kt: "c", kts: "c", c: "c", h: "c", cpp: "c", cc: "c", hpp: "c", cs: "c",
  go: "c", rs: "c", swift: "c", dart: "dart", scala: "c", gradle: "c",
  sh: "sh", bash: "sh", zsh: "sh", yml: "sh", yaml: "sh", toml: "sh", ini: "sh", env: "sh", conf: "sh",
  md: "md", markdown: "md",
};
const IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif"]);
const ext = (p) => (p.split(".").pop() || "").toLowerCase();
const langOf = (p) => {
  const base = p.split("/").pop();
  if (/^(dockerfile)$/i.test(base)) return "sh";
  if (/^(makefile)$/i.test(base)) return "sh";
  return EXT_LANG[ext(p)] || "";
};
const langLabel = (p) => {
  const e = ext(p), base = p.split("/").pop().toLowerCase();
  if (base === "dockerfile") return "Dockerfile";
  if (base === "makefile") return "Makefile";
  return e ? e.toUpperCase() : "Plain Text";
};
const fileIcon = (name, dir) => dir ? "folder" : IMG_EXT.has(ext(name)) ? "image" : langOf(name) ? "code" : "file";
const baseName = (p) => p.split("/").pop();
const dirName = (p) => p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "";
const relToAbs = (rel) => S.cur + (rel ? "/" + rel : "");

// ---- git decoration maps built from C.git.files ----
function gitMaps() {
  const status = {}, dirty = new Set();
  for (const f of (C.git?.files || [])) {
    status[f.path] = f;
    let d = dirName(f.path);
    while (d) { dirty.add(d); d = dirName(d); }
    dirty.add("");
  }
  return { status, dirty };
}
function gitClass(f) {
  if (!f) return "";
  const x = f.x, y = f.y;
  if (x === "D" || y === "D") return "git-deleted";
  if (y === "?" || x === "A" || y === "A") return "git-added";
  return "git-modified";
}

// ============================================================ views & layout
function setView(v) {
  C.view = v;
  $$(".code-rail button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$('.code-view').forEach((s) => (s.hidden = s.dataset.view !== v));
  if (v === "files") loadTree();
  else if (v === "git") loadGit();
  else if (v === "search") $("#codeQ").focus();
}
function showSide(on) {
  $("#codeWrap").classList.toggle("side-hidden", !on);
  $("#btnShowSide").hidden = on;
  store.set(pkey("codeSideOn"), on);
}

// ---- side resizer ----
(function sideResize() {
  const r = $("#codeResizer");
  if (!r) return;
  r.onpointerdown = (e) => {
    r.setPointerCapture(e.pointerId); r.classList.add("drag");
    const startX = e.clientX, startW = $("#codeSide").offsetWidth;
    r.onpointermove = (m) => {
      const w = Math.max(160, Math.min(480, startW + m.clientX - startX));
      $("#codeSide").style.width = w + "px"; C.sideW = w;
    };
    r.onpointerup = () => { r.onpointermove = null; r.classList.remove("drag"); store.set("fs:codeSideW", C.sideW); };
  };
})();

// ============================================================ exported hooks (called from app.js)
function codeProjectChanged() {
  // persist current tabs of the OLD project already happened on edits; load the new project's state
  C.tabs = []; C.active = null; C.git = null; C.filesCache = null; C.dir = {}; C.treeSel = null;
  C.expanded = new Set(store.get(pkey("tree:" + S.cur), []));
  $("#edTabs").innerHTML = ""; $("#edBody").innerHTML = "";
  $("#tree").innerHTML = ""; $("#searchResults").innerHTML = ""; $("#gitBody").innerHTML = "";
  $("#codeSide").style.width = (C.sideW = store.get("fs:codeSideW", 240)) + "px";
  showSide(store.get(pkey("codeSideOn"), true));
  if (!S.cur) { renderTabs(); renderEditor(); return; }
  $("#treeTitle").textContent = project()?.name || "Explorer";
  restoreTabs();
  loadGit();
  if (C.view !== "files") setView(C.view); else loadTree();
}
function codeTabShown() {
  if (!S.cur) return;
  if (!C.git) loadGit();
  if (!$("#tree").children.length) loadTree();
  const t = edActive(); if (t) t.ta?.focus();
}
function codeRefresh() { if (S.cur) { loadTree(); loadGit(); C.filesCache = null; } }
async function codeAgentTouched(paths) {
  // reload open, non-dirty tabs whose file changed; flag dirty tabs that changed underneath
  const open = C.tabs.filter((t) => t.kind === "file");
  if (!open.length) return;
  try {
    const rels = open.map((t) => t.path).join("\n");
    const { mtimes } = await api("/api/code/stat?project=" + encodeURIComponent(S.cur) + "&paths=" + encodeURIComponent(rels));
    for (const t of open) {
      const m = mtimes[t.path];
      if (m == null || m === t.mtime) continue;
      if (isDirty(t)) { t.changedOnDisk = true; renderTabs(); }
      else await reloadTab(t);
    }
  } catch {}
}

// ============================================================ explorer
C.dir = {};  // rel -> entries[]
class StaleProject extends Error {}
async function loadDir(rel) {
  const proj = S.cur;
  const d = await api(`/api/code/tree?project=${encodeURIComponent(proj)}&path=${encodeURIComponent(rel)}`);
  if (proj !== S.cur) throw new StaleProject();  // the user switched projects while this was loading
  C.dir[rel] = d.entries;
  return d.entries;
}
async function loadTree() {
  if (!S.cur) return;
  try {
    if (!C.dir[""]) await loadDir("");
    for (const rel of [...C.expanded]) if (!C.dir[rel]) { try { await loadDir(rel); } catch (e) { if (e instanceof StaleProject) throw e; C.expanded.delete(rel); } }
    renderTree();
  } catch (e) { if (!(e instanceof StaleProject)) toast(e.message, true); }
}
function renderTree() {
  const ul = $("#tree");
  ul.innerHTML = "";
  const { status, dirty } = gitMaps();
  const walk = (rel, depth) => {
    for (const e of C.dir[rel] || []) {
      const cr = rel ? rel + "/" + e.name : e.name;
      const li = document.createElement("li");
      const open = e.dir && C.expanded.has(cr);
      const gf = status[cr];
      li.className = "tree-row" + (e.dir ? " folder" : " file") + (open ? " open" : "")
        + (e.ignored ? " ignored" : "") + (cr === C.treeSel ? " active" : "") + (gf ? " " + gitClass(gf) : "");
      li.style.setProperty("--depth", depth);
      li.dataset.rel = cr; li.dataset.dir = e.dir ? "1" : "";
      const gLetter = gf ? `<span class="gd ${gitClass(gf)}">${esc((gf.y !== " " && gf.y !== "?" ? gf.y : gf.x).replace("?", "U"))}</span>` : "";
      const folderDot = e.dir && !open && dirty.has(cr) ? '<span class="dot"></span>' : "";
      li.innerHTML = `<span class="tw">${e.dir ? ic("chevron-right") : ""}</span>`
        + `<svg class="ic fi"><use href="#i-${fileIcon(e.name, e.dir)}"/></svg>`
        + `<span class="nm">${esc(e.name)}</span>${gLetter || folderDot}`;
      li.onclick = (ev) => { ev.stopPropagation(); e.dir ? toggleFolder(cr) : openInEditor(cr); C.treeSel = cr; markSel(); };
      li.oncontextmenu = (ev) => { ev.preventDefault(); ev.stopPropagation(); C.treeSel = cr; markSel(); treeMenu(ev, cr, e.dir); };
      li.draggable = true;
      li.ondragstart = (ev) => { ev.dataTransfer.setData("text/plain", cr); ev.dataTransfer.effectAllowed = "move"; };
      li.ondragover = (ev) => { if (e.dir) { ev.preventDefault(); li.classList.add("drop"); } };
      li.ondragleave = () => li.classList.remove("drop");
      li.ondrop = (ev) => { ev.preventDefault(); li.classList.remove("drop"); moveInto(ev.dataTransfer.getData("text/plain"), cr); };
      ul.append(li);
      if (open) walk(cr, depth + 1);
    }
  };
  walk("", 0);
  ul.oncontextmenu = (ev) => { if (ev.target === ul) { ev.preventDefault(); treeMenu(ev, "", true); } };
  ul.ondragover = (ev) => ev.preventDefault();
  ul.ondrop = (ev) => { if (ev.target === ul) moveInto(ev.dataTransfer.getData("text/plain"), ""); };
}
function markSel() { $$("#tree .tree-row").forEach((r) => r.classList.toggle("active", r.dataset.rel === C.treeSel)); }
async function toggleFolder(rel) {
  if (C.expanded.has(rel)) C.expanded.delete(rel);
  else { C.expanded.add(rel); if (!C.dir[rel]) { try { await loadDir(rel); } catch (e) { toast(e.message, true); C.expanded.delete(rel); } } }
  store.set(pkey("tree:" + S.cur), [...C.expanded]);
  renderTree();
}
async function moveInto(src, destDir) {
  if (!src || src === destDir) return;
  const to = (destDir ? destDir + "/" : "") + baseName(src);
  if (to === src || (destDir + "/").startsWith(src + "/")) return;
  await act(async () => {
    await api("/api/code/rename", { project: S.cur, from: src, to });
    renameOpenTabs(src, to); invalidateFiles();
    C.dir = {}; if (destDir) C.expanded.add(destDir); await loadTree(); loadGit();
  })();
}

// inline new file / folder
function newEntry(dir) {  // dir = false → file
  let target = C.treeSel && C.dir[C.treeSel] !== undefined ? C.treeSel : (C.treeSel ? dirName(C.treeSel) : "");
  if (C.treeSel && !$(`#tree .tree-row[data-rel="${cssq(C.treeSel)}"]`)?.dataset.dir) target = dirName(C.treeSel);
  if (target && !C.expanded.has(target)) { C.expanded.add(target); }
  const ul = $("#tree");
  const li = document.createElement("li");
  li.className = "tree-row"; li.style.setProperty("--depth", target ? target.split("/").length : 0);
  li.innerHTML = `<span class="tw"></span><svg class="ic fi"><use href="#i-${dir ? "folder" : "file"}"/></svg>`;
  const inp = document.createElement("input");
  inp.className = "tree-input"; inp.placeholder = dir ? "folder name" : "file name";
  li.append(inp);
  const anchor = target ? $(`#tree .tree-row[data-rel="${cssq(target)}"]`) : null;
  anchor ? anchor.after(li) : ul.prepend(li);
  inp.focus();
  const done = act(async (commit) => {
    const name = inp.value.trim();
    li.remove();
    if (!commit || !name) return;
    const rel = (target ? target + "/" : "") + name;
    await api("/api/code/create", { project: S.cur, path: rel, dir });
    invalidateFiles(); C.dir = {}; await loadTree();
    if (!dir) openInEditor(rel);
  });
  inp.onkeydown = (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  inp.onblur = () => done(true);
}
const cssq = (s) => s.replace(/"/g, '\\"');

// ============================================================ context menu
function showMenu(x, y, items) {
  const m = $("#ctxMenu");
  m.innerHTML = "";
  for (const it of items) {
    if (it === "sep") { m.append(document.createElement("hr")); continue; }
    const b = document.createElement("button");
    if (it.danger) b.className = "danger";
    b.innerHTML = `${ic(it.icon || "file")}<span>${esc(it.label)}</span>${it.sc ? `<span class="sc">${esc(it.sc)}</span>` : ""}`;
    b.onclick = () => { m.hidden = true; it.fn(); };
    m.append(b);
  }
  m.hidden = false;
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(x, innerWidth - w - 8) + "px";
  m.style.top = Math.min(y, innerHeight - h - 8) + "px";
}
document.addEventListener("click", () => ($("#ctxMenu").hidden = true));
document.addEventListener("contextmenu", (e) => { if (!e.target.closest(".tree, #ctxMenu")) $("#ctxMenu").hidden = true; }, true);

function treeMenu(ev, rel, isDir) {
  const abs = relToAbs(rel);
  const items = [
    { label: "New File", icon: "file-plus", fn: () => newEntry(false) },
    { label: "New Folder", icon: "folder-plus", fn: () => newEntry(true) },
    "sep",
  ];
  if (rel) items.push(
    { label: "Rename", icon: "edit", sc: "F2", fn: () => renameEntry(rel) },
    { label: "Delete", icon: "trash", sc: "Del", danger: true, fn: () => deleteEntry(rel) },
    "sep",
    { label: "Copy Path", icon: "copy", fn: () => copyText(abs) },
    { label: "Copy Relative Path", icon: "copy", fn: () => copyText(rel) },
  );
  if (rel && !isDir) items.push(
    { label: "Open in Browser", icon: "external", fn: () => open(fileUrl(abs), "_blank") },
    { label: "Download", icon: "download", fn: () => open(fileUrl(abs, true), "_blank") },
    { label: "Ask agent about this", icon: "bot", fn: () => askAgentAbout(rel) },
    { label: "Reveal in Source Control", icon: "branch", fn: () => { setView("git"); } },
  );
  showMenu(ev.clientX, ev.clientY, items);
}
function renameEntry(rel) {
  const row = $(`#tree .tree-row[data-rel="${cssq(rel)}"]`);
  if (!row) return;
  const nm = row.querySelector(".nm");
  const inp = document.createElement("input");
  inp.className = "tree-input"; inp.value = baseName(rel);
  nm.replaceWith(inp); inp.focus();
  const dot = rel.lastIndexOf("."); inp.setSelectionRange(0, dot > 0 ? dot : inp.value.length);
  const done = act(async (commit) => {
    const name = inp.value.trim();
    if (!commit || !name || name === baseName(rel)) return loadTree();
    const to = (dirName(rel) ? dirName(rel) + "/" : "") + name;
    await api("/api/code/rename", { project: S.cur, from: rel, to });
    renameOpenTabs(rel, to); invalidateFiles(); C.dir = {}; C.treeSel = to; await loadTree(); loadGit();
  });
  inp.onkeydown = (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  inp.onblur = () => done(true);
}
function deleteEntry(rel) {
  showMenu(0, -9999, []); $("#ctxMenu").hidden = true;
  const isDir = $(`#tree .tree-row[data-rel="${cssq(rel)}"]`)?.dataset.dir;
  if (!confirm(`Delete ${baseName(rel)}${isDir ? " and everything inside it" : ""}?`)) return;
  act(async () => {
    const r = await api("/api/code/delete", { project: S.cur, path: rel });
    toast(r.trashed ? "Moved to Trash" : "Deleted");
    for (const t of C.tabs.filter((t) => t.path === rel || t.path.startsWith(rel + "/"))) closeTab(t, true);
    invalidateFiles(); C.dir = {}; await loadTree(); loadGit();
  })();
}
async function copyText(t) { try { await navigator.clipboard.writeText(t); toast("Copied"); } catch { toast("Copy failed", true); } }

// ============================================================ editor: tabs
const edActive = () => C.tabs.find((t) => t.path === C.active);
const tabByPath = (p) => C.tabs.find((t) => t.path === p);
const isDirty = (t) => t.kind === "file" && t.ta && t.ta.value !== t.saved;
function persistTabs() {
  store.set(pkey("tabs:" + S.cur), { open: C.tabs.filter((t) => t.kind === "file").map((t) => t.path), active: C.active });
}
function renderTabs() {
  const row = $("#edTabs");
  row.innerHTML = "";
  for (const t of C.tabs) {
    const el = document.createElement("div");
    el.className = "ed-tab" + (t.path === C.active ? " active" : "");
    const dirty = isDirty(t);
    const warn = t.changedOnDisk ? '<span class="dotc" style="color:var(--warn)" title="Changed on disk"></span>' : "";
    el.innerHTML = `<svg class="ic fi"><use href="#i-${t.kind === "diff" ? "branch" : fileIcon(t.title, false)}"/></svg>`
      + `<span class="nm">${esc(t.title)}</span>${warn}`
      + `<button class="x ${dirty ? "dirty" : ""}" title="Close">${ic("x")}</button>`;
    el.onclick = () => activate(t.path);
    el.onmousedown = (e) => { if (e.button === 1) { e.preventDefault(); closeTab(t); } };
    el.querySelector(".x").onclick = (e) => { e.stopPropagation(); closeTab(t); };
    row.append(el);
  }
}
function renderEditor() {
  const body = $("#edBody");
  const cur = edActive();
  for (const t of C.tabs) if (t.el) t.el.hidden = t !== cur;
  $("#welcomeEd")?.remove();
  if (!cur) { body.append(welcomeEl()); renderCrumbs(null); renderStatus(null); return; }
  if (cur.el && cur.el.parentNode !== body) body.append(cur.el);
  renderCrumbs(cur); renderStatus(cur);
  if (cur.ta) setTimeout(() => cur.ta.focus(), 0);
}
function activate(path) { C.active = path; persistTabs(); renderTabs(); renderEditor(); }

// ============================================================ editor: open / save / close
function normalizeRel(p) {
  if (!p) return "";
  if (p === S.cur) return "";
  if (p.startsWith(S.cur + "/")) return p.slice(S.cur.length + 1);
  return p.replace(/^\.?\//, "");
}
async function openInEditor(pathOrAbs, line) {
  if (!S.cur) return;
  showCode(true);
  const rel = normalizeRel(pathOrAbs);
  let t = tabByPath(rel);
  if (!t) { try { t = await openTab(rel); } catch (e) { return toast(e.message, true); } }
  else activate(rel);
  if (line && t?.ta) gotoLine(t, line);
}
async function openTab(rel) {
  const d = await api(`/api/code/read?project=${encodeURIComponent(S.cur)}&path=${encodeURIComponent(rel)}`);
  const abs = relToAbs(rel);
  let t;
  if (IMG_EXT.has(ext(rel))) {
    const el = document.createElement("div"); el.className = "ed-msg";
    el.innerHTML = `<img src="${fileUrl(abs)}" alt="${esc(baseName(rel))}">`;
    t = { path: rel, kind: "image", title: baseName(rel), el, mtime: d.mtime };
  } else if (d.binary || d.tooLarge || d.content == null) {
    const el = document.createElement("div"); el.className = "ed-msg";
    el.innerHTML = `<div>${d.tooLarge ? "This file is too large to edit here." : "This looks like a binary file."}</div>`
      + `<div class="row"><button class="btn" data-o>${ic("external")} Open</button><button class="btn" data-d>${ic("download")} Download</button></div>`;
    el.querySelector("[data-o]").onclick = () => open(fileUrl(abs), "_blank");
    el.querySelector("[data-d]").onclick = () => open(fileUrl(abs, true), "_blank");
    t = { path: rel, kind: "binary", title: baseName(rel), el, mtime: d.mtime };
  } else {
    t = makeFileEditor(rel, d.content, d.mtime);
  }
  C.tabs.push(t); activate(rel); persistTabs();
  return t;
}
async function reloadTab(t) {
  try {
    const d = await api(`/api/code/read?project=${encodeURIComponent(S.cur)}&path=${encodeURIComponent(t.path)}`);
    if (t.kind === "file" && d.content != null) {
      const atBottom = t.el.scrollTop; t.ta.value = d.content; t.saved = d.content; t.mtime = d.mtime;
      t.changedOnDisk = false; highlight(t); syncGutter(t); t.el.scrollTop = atBottom;
      if (t === edActive()) renderStatus(t);
    }
    renderTabs();
  } catch {}
}
async function saveTab(t) {
  if (!t || t.kind !== "file") return;
  await act(async () => {
    const content = t.ta.value;
    let r = await api("/api/code/write", { project: S.cur, path: t.path, content, mtime: t.mtime });
    if (r.conflict) {
      if (!confirm("This file changed on disk since you opened it.\n\nOK = overwrite with your version, Cancel = reload from disk.")) return reloadTab(t);
      r = await api("/api/code/write", { project: S.cur, path: t.path, content, mtime: t.mtime, force: true });
    }
    t.mtime = r.mtime; t.saved = content; t.changedOnDisk = false;
    renderTabs(); renderStatus(t); loadGit();
  })();
}
function closeTab(t, skipConfirm) {
  if (!skipConfirm && isDirty(t) && !confirm(`${t.title} has unsaved changes. Close without saving?`)) return;
  const i = C.tabs.indexOf(t);
  if (i < 0) return;
  t.el?.remove(); C.tabs.splice(i, 1);
  if (C.active === t.path) C.active = (C.tabs[i] || C.tabs[i - 1] || {}).path || null;
  persistTabs(); renderTabs(); renderEditor();
}
function renameOpenTabs(from, to) {
  for (const t of C.tabs) {
    if (t.path === from) { t.path = to; t.title = baseName(to); }
    else if (t.path.startsWith(from + "/")) { t.path = to + t.path.slice(from.length); t.title = baseName(t.path); }
  }
  if (C.active === from) C.active = to;
  persistTabs(); renderTabs(); renderEditor();
}
async function restoreTabs() {
  const saved = store.get(pkey("tabs:" + S.cur), null);
  if (!saved?.open?.length) { renderTabs(); renderEditor(); return; }
  for (const rel of saved.open) { try { await openTab(rel); } catch {} }
  if (saved.active && tabByPath(saved.active)) activate(saved.active);
  else if (C.tabs[0]) activate(C.tabs[0].path);
}

// ---- breadcrumbs, status, welcome ----
function renderCrumbs(t) {
  const c = $("#edCrumbs");
  if (!t) { c.innerHTML = ""; return; }
  const parts = t.path.replace(/^diff:/, "").split("/");  // diff tabs use a "diff:" key; show the real path
  c.innerHTML = [project()?.name, ...parts].map((p, i) => `<span>${esc(p)}</span>${i < parts.length ? `<span class="sep">${ic("chevron-right")}</span>` : ""}`).join("");
}
function renderStatus(t) {
  const s = $("#edStatus");
  const branch = C.git?.repo ? `${ic("branch")} ${esc(C.git.branch || "…")}` : "";
  if (!t || t.kind !== "file") {
    s.innerHTML = `<span class="seg btn-like" data-git>${branch}</span><span class="right"><span class="seg">${t ? esc(langLabel(t.path)) : "Forge Studio"}</span></span>`;
  } else {
    const v = t.ta.value, pos = t.ta.selectionStart;
    const before = v.slice(0, pos), ln = before.split("\n").length, col = pos - before.lastIndexOf("\n");
    s.innerHTML = `<span class="seg btn-like" data-git>${branch}</span>`
      + `<span class="seg">Ln ${ln}, Col ${col}</span>`
      + `<span class="right"><span class="seg">Spaces: 2</span><span class="seg">UTF-8</span><span class="seg">LF</span>`
      + `<span class="seg">${esc(langLabel(t.path))}</span><span class="seg">${isDirty(t) ? "● Unsaved" : "Saved"}</span></span>`;
  }
  s.querySelector("[data-git]")?.addEventListener("click", () => C.git?.repo && setView("git"));
}
function welcomeEl() {
  const el = document.createElement("div"); el.className = "welcome"; el.id = "welcomeEd";
  const tiles = [
    ["file-plus", "New File", () => newEntry(false)],
    ["search", "Search", () => setView("search")],
    ["branch", "Source Control", () => setView("git")],
    ["bot", "Ask the agent", () => { $("#prompt")?.focus(); }, true],
  ];
  el.innerHTML = `<img src="logo.png" alt=""><h1>Forge Studio</h1><div class="sub">${esc(project()?.name || "")}</div>`;
  const grid = document.createElement("div"); grid.className = "tiles";
  for (const [icn, label, fn, hot] of tiles) {
    const tl = document.createElement("div"); tl.className = "tile" + (hot ? " hot" : "");
    tl.innerHTML = `${ic(icn)}<span class="label">${label}</span>`; tl.onclick = fn; grid.append(tl);
  }
  el.append(grid);
  const pill = document.createElement("button"); pill.className = "pill";
  pill.innerHTML = `${ic("bot")} Ask the agent to walk you through this project ${ic("external")}`;
  pill.onclick = () => { $("#prompt").value = `Give me a tour of this project (${project()?.name}): what it does, the main files, and how to run it.`; $("#prompt").focus(); };
  el.append(pill);
  return el;
}
function askAgentAbout(rel) {
  $("#prompt").value = `Tell me about \`${rel}\` in this project.`;
  $("#prompt").focus(); toast("Added to the message box — press Send");
}
function askAgent() {
  const t = edActive();
  if (!t || t.kind !== "file") return toast("Open a file first", true);
  const ta = t.ta, sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
  const ln = (v, i) => v.slice(0, i).split("\n").length;
  let ctx;
  if (sel.trim()) ctx = `In \`${t.path}\` (lines ${ln(ta.value, ta.selectionStart)}–${ln(ta.value, ta.selectionEnd)}):\n\n\`\`\`${langOf(t.path)}\n${sel}\n\`\`\``;
  else ctx = `About the file \`${t.path}\`:\n\n`;
  $("#prompt").value = ctx + "\n"; $("#prompt").focus();
  toast("Added to the message box — add your question and press Send");
}

// ============================================================ editor: DOM + keys
function invalidateFiles() { C.filesCache = null; }
function makeFileEditor(rel, content, mtime) {
  const el = document.createElement("div"); el.className = "ed";
  const gutter = document.createElement("pre"); gutter.className = "gutter";
  const hl = document.createElement("pre"); hl.className = "hl";
  const ta = document.createElement("textarea");
  ta.spellcheck = false; ta.wrap = "off"; ta.autocapitalize = "off"; ta.autocomplete = "off"; ta.value = content;
  el.append(gutter, hl, ta);
  const t = { path: rel, kind: "file", title: baseName(rel), el, ta, hl, gutter, saved: content, mtime };
  let raf = 0;
  const rehl = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; highlight(t); syncGutter(t); }); };
  ta.addEventListener("input", () => { rehl(); renderTabs(); if (t === edActive()) renderStatus(t); });
  ta.addEventListener("scroll", () => { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; });
  const upd = () => { if (t === edActive()) renderStatus(t); currentLine(t); };
  ta.addEventListener("keyup", upd); ta.addEventListener("click", upd); ta.addEventListener("select", upd);
  ta.addEventListener("keydown", (e) => edKey(e, t));
  highlight(t); syncGutter(t);
  return t;
}
function syncGutter(t) {
  const n = t.ta.value.split("\n").length;
  const cur = t.gutter.childElementCount;
  if (n === t._gn) return;
  t._gn = n;
  let s = ""; for (let i = 1; i <= n; i++) s += i + "\n";
  t.gutter.textContent = s;
}
function currentLine() { /* light-touch: current-line highlight handled by caret; kept simple for reliability */ }
function gotoLine(t, line, len) {
  const lines = t.ta.value.split("\n");
  let pos = 0; for (let i = 0; i < line - 1 && i < lines.length; i++) pos += lines[i].length + 1;
  t.ta.focus();
  t.ta.setSelectionRange(pos, pos + (len || (lines[line - 1] || "").length));
  const lineH = 12.5 * 1.55;
  t.el.scrollTop = Math.max(0, (line - 4) * lineH);
  renderStatus(t);
}
const INDENT = "  ";
function replaceSel(ta, text) { ta.focus(); document.execCommand("insertText", false, text); }
function edKey(e, t) {
  const ta = t.ta, mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); return saveTab(t); }
  if (mod && e.key.toLowerCase() === "w") { e.preventDefault(); return closeTab(t); }
  if (mod && e.key.toLowerCase() === "p" && !e.shiftKey) { e.preventDefault(); return openQuick(); }
  if (mod && e.key.toLowerCase() === "g") { e.preventDefault(); const n = prompt("Go to line:"); if (n) gotoLine(t, parseInt(n) || 1); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === "f") { e.preventDefault(); return setView("search"); }
  if (mod && e.shiftKey && e.key.toLowerCase() === "e") { e.preventDefault(); return setView("files"); }
  if (mod && e.shiftKey && e.key.toLowerCase() === "g") { e.preventDefault(); return setView("git"); }
  if (mod && e.key === "/") { e.preventDefault(); return toggleComment(t); }
  if (e.key === "Tab") {
    e.preventDefault();
    const { selectionStart: a, selectionEnd: b, value: v } = ta;
    if (a === b && !e.shiftKey) return replaceSel(ta, INDENT);
    // selection-aware indent/outdent
    const ls = v.lastIndexOf("\n", a - 1) + 1, le = b;
    const block = v.slice(ls, le);
    const out = e.shiftKey
      ? block.replace(/^( {1,2}|\t)/gm, "")
      : block.replace(/^/gm, INDENT);
    ta.setSelectionRange(ls, le); replaceSel(ta, out);
    ta.setSelectionRange(ls, ls + out.length);
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    const v = ta.value, a = ta.selectionStart;
    const ls = v.lastIndexOf("\n", a - 1) + 1;
    const indent = (v.slice(ls, a).match(/^[ \t]*/) || [""])[0];
    const prevCh = v[a - 1];
    const extra = /[{[(:]/.test(prevCh || "") ? INDENT : "";
    replaceSel(ta, "\n" + indent + extra);
    return;
  }
}
function toggleComment(t) {
  const line = { js: "//", c: "//", css: "/*", py: "#", rb: "#", php: "//", sh: "#", lua: "--", xml: "<!--", md: "" }[langOf(t.path)] ?? "//";
  if (!line) return;
  const ta = t.ta, v = ta.value, a = ta.selectionStart, b = ta.selectionEnd;
  const ls = v.lastIndexOf("\n", a - 1) + 1, le = v.indexOf("\n", b) === -1 ? v.length : v.indexOf("\n", b);
  const block = v.slice(ls, le);
  const allCommented = block.split("\n").every((l) => !l.trim() || l.trimStart().startsWith(line));
  const out = block.split("\n").map((l) => {
    if (!l.trim()) return l;
    if (allCommented) return l.replace(new RegExp("^(\\s*)" + line.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + " ?"), "$1");
    return l.replace(/^(\s*)/, "$1" + line + " ");
  }).join("\n");
  ta.setSelectionRange(ls, le); replaceSel(ta, out); ta.setSelectionRange(ls, ls + out.length);
}

// ============================================================ syntax highlighting
const KW = {
  js: "abstract arguments await async break case catch class const continue debugger default delete do else enum export extends false finally for from function get if implements import in instanceof interface let new null of package private protected public return set static super switch this throw true try typeof var void while with yield undefined as namespace type readonly keyof declare",
  py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self match case",
  c: "abstract as async await base bool break byte case catch char class const continue default do double else enum export extern false final finally float fn for func fun goto if impl import in int interface internal is let long mut namespace new null override package private protected public return sealed short static string struct super switch this throw throws trait true try typealias typeof union unsafe use val var virtual void volatile when where while yield var val fun suspend data object companion",
  css: "important inherit initial unset none auto flex grid block inline absolute relative fixed sticky hover active focus before after not import media keyframes supports font-face root var calc rgb rgba hsl url",
  sh: "if then elif else fi for while do done case esac function in return break continue local export source echo cd exit set unset readonly declare true false null trap alias eval",
  rb: "def end class module if elsif else unless while until for do begin rescue ensure return yield self nil true false and or not then case when require require_relative attr_accessor attr_reader attr_writer puts print new lambda proc",
  php: "abstract and array as break case catch class clone const continue declare default do echo else elseif empty endfor endforeach endif endswitch endwhile enum extends final finally fn for foreach function global if implements include instanceof interface isset list namespace new null or print private protected public return static switch throw trait try unset use var while yield true false",
  lua: "and break do else elseif end false for function goto if in local nil not or repeat return then true until while self",
  dart: "abstract as assert async await base break case catch class const continue covariant default deferred do dynamic else enum export extends extension external factory false final finally for Function get hide if implements import in interface is late library mixin new null of on operator part required rethrow return sealed set show static super switch sync this throw true try typedef var void when while with yield bool int double num String List Map Set Future Stream Object Never",
};
function scan(code, rules) {
  let out = "", i = 0, n = code.length, guard = 0;
  while (i < n && guard++ < 500000) {
    let matched = false;
    for (const [cls, re] of rules) {
      re.lastIndex = 0;
      const m = re.exec(code.slice(i));
      if (m && m.index === 0 && m[0]) {
        out += cls ? `<span class="${cls}">${esc(m[0])}</span>` : esc(m[0]);
        i += m[0].length; matched = true; break;
      }
    }
    if (!matched) { out += esc(code[i]); i++; }
  }
  return out + esc(code.slice(i));
}
function clikeRules(lang) {
  const kw = new Set((KW[lang] || "").split(" ").filter(Boolean));
  const line = lang === "py" || lang === "sh" || lang === "rb" ? "#" : lang === "lua" ? "--" : "//";
  const rules = [];
  if (lang !== "py" && lang !== "sh" && lang !== "rb")
    rules.push(["t-com", /\/\*[\s\S]*?(\*\/|$)/y]);
  rules.push(["t-com", new RegExp(line.replace(/[/*]/g, "\\$&") + ".*", "y")]);
  if (lang === "py") rules.push(["t-str", /[frbu]*("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))/y]);
  if (lang === "dart") rules.push(["t-str", /r?("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))/y], ["t-kw", /@\w+/y]);
  if (lang === "js" || lang === "c") rules.push(["t-str", /`(?:\\.|[^`\\])*`/y]);
  rules.push(
    ["t-str", /"(?:\\.|[^"\\])*"/y],
    ["t-str", /'(?:\\.|[^'\\])*'/y],
    ["t-num", /\b0[xX][0-9a-fA-F]+|\b\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?[a-zA-Z%]*/y],
    ["", /\s+/y],
    ["__word__", /[A-Za-z_$][\w$]*/y],
    ["t-fn", /[^\w\s]+/y],
  );
  // custom word handling: keyword / type / function
  return (code) => {
    let out = "", i = 0, guard = 0;
    while (i < code.length && guard++ < 500000) {
      let done = false;
      for (const [cls, re] of rules) {
        re.lastIndex = i;
        const m = re.exec(code);
        if (m && m.index === i && m[0]) {
          let piece;
          if (cls === "__word__") {
            const w = m[0];
            const after = code[i + w.length] === "(" || (code.slice(i + w.length).match(/^\s*\(/));
            const c = kw.has(w) ? "t-kw" : after ? "t-fn" : /^[A-Z]/.test(w) && lang !== "sh" ? "t-type" : "";
            piece = c ? `<span class="${c}">${esc(w)}</span>` : esc(w);
          } else if (cls === "t-fn") {
            piece = esc(m[0]);  // punctuation: leave plain
          } else piece = cls ? `<span class="${cls}">${esc(m[0])}</span>` : esc(m[0]);
          out += piece; i += m[0].length; done = true; break;
        }
      }
      if (!done) { out += esc(code[i]); i++; }
    }
    return out;
  };
}
function xmlHighlight(code) {
  return scan(code, [
    ["t-com", /<!--[\s\S]*?(-->|$)/y],
    ["t-str", /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/y],
    ["t-tag", /<\/?[A-Za-z][\w:-]*/y],
    ["t-tag", /\/?>/y],
    ["t-attr", /[A-Za-z_:][\w:.-]*(?==)/y],
  ]);
}
function highlight(t) {
  const code = t.ta.value;
  if (code.length > 300000) { t.hl.textContent = code + "\n "; return; }
  const lang = langOf(t.path);
  let html;
  if (lang === "xml") html = xmlHighlight(code);
  else if (lang === "md") html = esc(code);
  else if (lang) html = clikeRules(lang)(code);
  else html = esc(code);
  t.hl.innerHTML = html + "\n ";
}

// ============================================================ search in files
function runSearch() {
  clearTimeout(C.searchT);
  const q = $("#codeQ").value;
  if (!q.trim()) { $("#searchResults").innerHTML = ""; $("#searchInfo").textContent = ""; return; }
  C.searchT = setTimeout(act(async () => {
    const cs = $("#qCase").classList.contains("on") ? 1 : 0, rx = $("#qRegex").classList.contains("on") ? 1 : 0;
    const d = await api(`/api/code/search?project=${encodeURIComponent(S.cur)}&q=${encodeURIComponent(q)}&case=${cs}&regex=${rx}`);
    renderSearch(d, q, cs, rx);
  }), 220);
}
function matchRe(q, cs, rx) {
  try { return new RegExp(rx ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), cs ? "g" : "gi"); }
  catch { return null; }
}
function renderSearch(d, q, cs, rx) {
  const box = $("#searchResults");
  box.innerHTML = "";
  const groups = new Map();
  for (const r of d.results) { if (!groups.has(r.path)) groups.set(r.path, []); groups.get(r.path).push(r); }
  $("#searchInfo").textContent = d.results.length
    ? `${d.results.length}${d.truncated ? "+" : ""} results in ${groups.size} files` : "No results";
  const re = matchRe(q, cs, rx);
  for (const [path, rows] of groups) {
    const head = document.createElement("div");
    head.className = "res-file";
    head.innerHTML = `<svg class="ic fi"><use href="#i-${fileIcon(path, false)}"/></svg><span class="nm">${esc(baseName(path))}</span><span class="dim">${esc(dirName(path))}</span><span class="cnt">${rows.length}</span>`;
    head.onclick = () => openInEditor(path, rows[0].line);
    box.append(head);
    for (const r of rows) {
      const line = document.createElement("div");
      line.className = "res-line";
      let txt = esc(r.text);
      if (re) { re.lastIndex = 0; txt = esc(r.text).replace(new RegExp(re.source, re.flags), (m) => `<mark>${m}</mark>`); }
      line.innerHTML = `<span class="ln">${r.line}</span><span>${txt}</span>`;
      line.onclick = () => openMatch(path, r.line, q, cs, rx);
      box.append(line);
    }
  }
}
async function openMatch(path, line, q, cs, rx) {
  await openInEditor(path, line);
  const t = tabByPath(normalizeRel(path));
  if (!t?.ta) return;
  const re = matchRe(q, cs, rx);
  const lines = t.ta.value.split("\n");
  let pos = 0; for (let i = 0; i < line - 1; i++) pos += lines[i].length + 1;
  const lt = lines[line - 1] || "";
  let col = 0, len = lt.length;
  if (re) { re.lastIndex = 0; const m = re.exec(lt); if (m) { col = m.index; len = m[0].length; } }
  t.ta.focus(); t.ta.setSelectionRange(pos + col, pos + col + len);
  renderStatus(t);
}

// ============================================================ quick open
async function openQuick() {
  if (!S.cur) return;
  const dlg = $("#dlgQuick");
  $("#quickQ").value = "";
  try { if (!C.filesCache) C.filesCache = (await api("/api/code/files?project=" + encodeURIComponent(S.cur))).files; }
  catch (e) { return toast(e.message, true); }
  renderQuick("");
  dlg.showModal(); $("#quickQ").focus();
}
function fuzzy(q, s) {
  if (!q) return 1;
  q = q.toLowerCase(); s = s.toLowerCase();
  let qi = 0, score = 0, streak = 0, base = s.lastIndexOf("/") + 1;
  for (let i = 0; i < s.length && qi < q.length; i++) {
    if (s[i] === q[qi]) { qi++; streak++; score += streak + (i >= base ? 3 : 0); }
    else streak = 0;
  }
  return qi === q.length ? score : 0;
}
function renderQuick(q) {
  const list = $("#quickList");
  const ranked = (C.filesCache || []).map((f) => [f, fuzzy(q, f)]).filter((x) => x[1] > 0)
    .sort((a, b) => b[1] - a[1]).slice(0, 200);
  list.innerHTML = "";
  ranked.forEach(([f], i) => {
    const li = document.createElement("li");
    li.className = "quick-item" + (i === 0 ? " sel" : "");
    li.innerHTML = `<svg class="ic fi"><use href="#i-${fileIcon(f, false)}"/></svg><span class="nm">${esc(baseName(f))}</span><span class="pth">${esc(dirName(f))}</span>`;
    li.onclick = () => { $("#dlgQuick").close(); openInEditor(f); };
    list.append(li);
  });
  C.quickSel = 0;
}

// ============================================================ source control
async function loadGit() {
  if (!S.cur) return;
  const proj = S.cur;
  let git;
  try { git = await api("/api/git/status?project=" + encodeURIComponent(proj)); }
  catch { git = { repo: false, git: true }; }
  if (proj !== S.cur) return;  // switched projects meanwhile
  C.git = git;
  const n = C.git.files?.length || 0;
  const badge = $("#gitBadge"); badge.hidden = !n; badge.textContent = n;
  if ($("#tree").children.length) renderTree();
  const cur = edActive(); if (cur) renderStatus(cur);
  if (C.view === "git") renderGit();
  if (typeof renderStatusBar === "function") renderStatusBar();
}
async function gitDo(action, body, quiet) {
  await act(async () => {
    const r = await api("/api/git/" + action, { project: S.cur, ...body });
    if (r?.output && !quiet) toast(r.output.split("\n")[0].slice(0, 120));
    if (r?.trashed) toast("Moved to Trash");
    invalidateFiles(); await loadGit(); loadTree();
    for (const t of C.tabs.filter((t) => t.kind === "file" && !isDirty(t))) reloadTab(t);
  })();
}
function renderGit() {
  const box = $("#gitBody");
  const g = C.git;
  if (!g) { box.innerHTML = ""; return; }
  if (!g.git) { box.innerHTML = `<div class="git-empty">${ic("branch")}<div>Git isn't installed.<br><span class="dim">Install it (e.g. <code>sudo apt install git</code>) to use Source Control.</span></div></div>`; return; }
  if (!g.repo) {
    box.innerHTML = `<div class="git-empty">${ic("branch")}<div>This folder isn't a Git repository.</div>
      <button class="btn primary" id="gInit">${ic("commit")} Initialize Repository</button>
      <button class="btn" id="gClone">${ic("clone")} Clone a repo…</button></div>`;
    $("#gInit").onclick = () => gitDo("init", {});
    $("#gClone").onclick = () => openProjectDialog("clone");
    return;
  }
  const staged = g.files.filter((f) => f.x !== " " && f.x !== "?");
  const changes = g.files.filter((f) => f.y !== " " || f.x === "?");
  const wrap = document.createElement("div"); wrap.className = "git-scroll";
  // branch + track
  const branchSel = `<div class="git-branchline">${ic("branch")}<select id="gBranch">`
    + g.branches.map((b) => `<option${b === g.branch ? " selected" : ""}>${esc(b)}</option>`).join("")
    + `<option value="__new">＋ Create new branch…</option></select></div>`;
  const track = (g.ahead || g.behind || g.upstream)
    ? `<div class="git-track">${g.behind ? `<span>${ic("arrow-down")} ${g.behind}</span>` : ""}${g.ahead ? `<span>${ic("arrow-up")} ${g.ahead}</span>` : ""}<span class="up">${esc(g.upstream || "no upstream")}</span></div>` : "";
  wrap.innerHTML = branchSel + track
    + `<div class="git-commit"><div class="git-msg-wrap"><textarea id="gMsg" placeholder="Message (Ctrl+Enter to commit)"></textarea>`
    + `<button class="git-ai" id="gAI" title="Generate a commit message from your changes">${ic("bot")}</button></div>`
    + `<label class="check"><input type="checkbox" id="gAmend"> Amend last commit</label>`
    + `<button class="btn primary" id="gCommit" style="width:100%;margin-top:6px">${ic("commit")} Commit${staged.length ? "" : " All"}</button></div>`;
  wrap.append(gitSection("Staged Changes", staged, true));
  wrap.append(gitSection("Changes", changes, false));
  if (!g.remotes.length) {
    const rm = document.createElement("div"); rm.className = "git-branchline"; rm.style.marginTop = "8px";
    rm.innerHTML = `<input id="gRemote" class="mono grow" placeholder="git@github.com:you/app.git"><button class="btn" id="gAddRemote">Add remote</button>`;
    wrap.append(rm);
  }
  const log = document.createElement("div"); log.className = "git-log";
  log.innerHTML = `<div class="git-sec-head">Recent Commits</div>`;
  wrap.append(log);
  box.innerHTML = ""; box.append(wrap);
  loadGitLog(log);
  // wiring
  $("#gBranch").onchange = (e) => {
    const v = e.target.value;
    if (v === "__new") { const name = prompt("New branch name:"); e.target.value = g.branch; if (name) gitDo("checkout", { branch: name, create: true }); }
    else gitDo("checkout", { branch: v });
  };
  const commit = () => {
    const message = $("#gMsg").value.trim(), amend = $("#gAmend").checked;
    if (!staged.length && !amend && changes.length) { if (!confirm("Nothing is staged. Commit ALL changes?")) return; }
    gitDo("commit", { message, amend, all: !staged.length, authorName: S.profile?.name, authorEmail: S.profile?.email });
  };
  $("#gCommit").onclick = commit;
  $("#gMsg").onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); commit(); } };
  $("#gAI").onclick = act(async () => {
    const b = $("#gAI"); b.classList.add("busy"); b.disabled = true;
    try {
      // use the chat's selected provider+model (Anthropic OR OpenAI-shaped) when it's your own key;
      // otherwise the computer's Claude login on fast Haiku
      const ch = typeof modelChoice === "function" ? modelChoice() : {};
      const useProv = ch.provider && ch.provider !== "local" && (S.providers || []).some((p) => p.id === ch.provider);
      const r = await api("/api/git/suggest-commit", useProv
        ? { project: S.cur, provider: ch.provider, model: ch.model }
        : { project: S.cur, provider: "local" });
      if (r.message) { $("#gMsg").value = r.message; $("#gMsg").focus(); $("#gMsg").setSelectionRange(0, 0); }
    } finally { b.classList.remove("busy"); b.disabled = false; }
  });
  $("#gAddRemote") && ($("#gAddRemote").onclick = () => { const url = $("#gRemote").value.trim(); if (url) gitDo("remote", { url }); });
}
function gitSection(title, files, staged) {
  const sec = document.createElement("div"); sec.className = "git-sec";
  const head = document.createElement("div"); head.className = "git-sec-head";
  head.innerHTML = `<span>${title}</span><span class="cnt">${files.length}</span>`;
  if (files.length) {
    const b = document.createElement("button"); b.className = "icon-btn"; b.title = staged ? "Unstage all" : "Stage all";
    b.innerHTML = ic(staged ? "minus" : "plus");
    b.onclick = () => gitDo(staged ? "unstage" : "stage", { paths: files.map((f) => f.path) });
    head.append(b);
  }
  sec.append(head);
  for (const f of files) {
    const st = staged ? f.x : (f.y === " " ? f.x : f.y);
    const letter = st === "?" ? "U" : st;
    const row = document.createElement("div"); row.className = "git-file";
    row.innerHTML = `<span class="st ${letter}">${esc(letter)}</span>`
      + `<svg class="ic fi"><use href="#i-${fileIcon(f.path, false)}"/></svg>`
      + `<span class="nm">${esc(baseName(f.path))}${dirName(f.path) ? ` <span class="dir">${esc(dirName(f.path))}</span>` : ""}</span>`
      + `<span class="acts"></span>`;
    row.onclick = (e) => { if (!e.target.closest(".acts")) openDiff(f.path, staged, f.y === "?" || f.x === "?"); };
    const acts = row.querySelector(".acts");
    const btn = (icn, title, fn) => { const b = document.createElement("button"); b.className = "icon-btn"; b.title = title; b.innerHTML = ic(icn); b.onclick = (e) => { e.stopPropagation(); fn(); }; acts.append(b); };
    if (staged) btn("minus", "Unstage", () => gitDo("unstage", { paths: [f.path] }));
    else { btn("plus", "Stage", () => gitDo("stage", { paths: [f.path] }));
      btn("undo", "Discard changes", () => { if (confirm(`Discard changes to ${baseName(f.path)}? Untracked files go to Trash.`)) gitDo("discard", { paths: [f.path] }); }); }
    sec.append(row);
  }
  return sec;
}
async function loadGitLog(el) {
  try {
    const { commits } = await api("/api/git/log?project=" + encodeURIComponent(S.cur));
    for (const c of commits) {
      const row = document.createElement("div"); row.className = "git-log-row";
      row.innerHTML = `<span class="h">${esc(c.hash)}</span><span class="s">${esc(c.subject)}</span><span class="a">${esc(c.author)}</span>`;
      el.append(row);
    }
  } catch {}
}
async function openDiff(rel, staged, untracked) {
  const id = "diff:" + rel;
  if (tabByPath(id)) return activate(id);
  await act(async () => {
    const d = await api(`/api/git/diff?project=${encodeURIComponent(S.cur)}&path=${encodeURIComponent(rel)}&staged=${staged ? 1 : 0}&untracked=${untracked ? 1 : 0}`);
    const el = document.createElement("div"); el.className = "diff";
    el.append(diffOpenBar(rel));
    let a = 0, b = 0;
    for (const ln of d.diff.split("\n")) {
      const row = document.createElement("div"); row.className = "dl";
      let cls = "", ga = "", gb = "";
      if (ln.startsWith("@@")) { const m = ln.match(/-(\d+).*\+(\d+)/); a = m ? +m[1] : a; b = m ? +m[2] : b; cls = "hunk"; }
      else if (ln.startsWith("+") && !ln.startsWith("+++")) { cls = "add"; gb = b++; }
      else if (ln.startsWith("-") && !ln.startsWith("---")) { cls = "del"; ga = a++; }
      else if (ln.startsWith("diff ") || ln.startsWith("index ") || ln.startsWith("+++") || ln.startsWith("---")) cls = "meta";
      else { ga = a++; gb = b++; }
      row.className = "dl " + cls;
      row.innerHTML = `<span class="g">${ga || ""}</span><span class="g">${gb || ""}</span><span class="c">${esc(ln)}</span>`;
      el.append(row);
    }
    C.tabs.push({ path: id, kind: "diff", title: baseName(rel) + " (diff)", el }); activate(id);
  })();
}
function diffOpenBar(rel) {
  const bar = document.createElement("div"); bar.className = "row"; bar.style.padding = "6px 10px";
  const b = document.createElement("button"); b.className = "btn small"; b.innerHTML = ic("file") + " Open file";
  b.onclick = () => openInEditor(rel); bar.append(b);
  return bar;
}

// ============================================================ new / clone project dialog
function openProjectDialog(kind) {
  const dlg = $("#dlgProject");
  setProjKind(kind || "clone");
  $("#pjParent").value = $("#pjParent").value || "~/projects";
  $("#pjUrl").value = ""; $("#pjName").value = "";
  dlg.showModal();
  ($("#pjUrl").offsetParent ? $("#pjUrl") : $("#pjName")).focus();
}
function setProjKind(kind) {
  C.projKind = kind;
  $$("#projSeg button").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
  const clone = kind === "clone";
  $("#pjUrlWrap").hidden = !clone; $("#pjGitWrap").hidden = clone;
  $("#pjOk").textContent = clone ? "Clone" : "Create";
  $("#pjHint").innerHTML = clone
    ? 'Private repos need an SSH key or a Git credential helper (e.g. <code>gh auth login</code>) on this computer. Progress shows in Processes.'
    : 'Creates an empty folder in your projects.';
}

// ============================================================ init & wiring
function initCode() {
  $$(".code-rail button[data-view]").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
  $("#btnCodeSide").onclick = () => showSide(false);
  $("#btnShowSide").onclick = () => showSide(true);
  $("#btnNewFile").onclick = () => newEntry(false);
  $("#btnNewFolder").onclick = () => newEntry(true);
  $("#btnTreeRefresh").onclick = () => { C.dir = {}; loadTree(); loadGit(); };
  $("#btnTreeCollapse").onclick = () => { C.expanded.clear(); store.set(pkey("tree:" + S.cur), []); renderTree(); };
  $("#codeQ").oninput = runSearch;
  $("#codeQ").onkeydown = (e) => { if (e.key === "Enter") runSearch(); };
  const tog = (id) => { $(id).classList.toggle("on"); runSearch(); };
  $("#qCase").onclick = () => tog("#qCase");
  $("#qRegex").onclick = () => tog("#qRegex");
  $("#btnGitPull").onclick = () => gitDo("pull", {});
  $("#btnGitPush").onclick = () => gitDo("push", {});
  $("#btnGitRefresh").onclick = () => gitDo("fetch", {}, true);
  $("#btnEdSave").onclick = () => saveTab(edActive());
  $("#btnEdAsk").onclick = askAgent;

  // quick open palette
  $("#quickQ").oninput = () => renderQuick($("#quickQ").value);
  $("#quickQ").onkeydown = (e) => {
    const items = $$("#quickList .quick-item");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      C.quickSel = Math.max(0, Math.min(items.length - 1, (C.quickSel || 0) + (e.key === "ArrowDown" ? 1 : -1)));
      items.forEach((el, i) => el.classList.toggle("sel", i === C.quickSel));
      items[C.quickSel]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") { e.preventDefault(); items[C.quickSel || 0]?.click(); }
    else if (e.key === "Escape") $("#dlgQuick").close();
  };

  // project dialog
  $$("#projSeg button").forEach((b) => (b.onclick = () => setProjKind(b.dataset.kind)));
  $("#pjUrl").oninput = () => { if (!$("#pjName").value) { const m = $("#pjUrl").value.trim().replace(/\.git$/, "").match(/[/:]([^/:]+)\/?$/); if (m) $("#pjName").placeholder = m[1]; } };
  $("#pjOk").onclick = (e) => {
    e.preventDefault();
    act(async () => {
      const parent = $("#pjParent").value.trim() || "~/projects", name = $("#pjName").value.trim();
      if (C.projKind === "clone") {
        await api("/api/git/clone", { url: $("#pjUrl").value.trim(), parent, name });
        toast("Cloning… progress is in Processes"); $("#dlgProject").close();
      } else {
        const p = await api("/api/projects/new", { parent, name, git: $("#pjGit").checked });
        $("#dlgProject").close(); await loadState(); await selectProject(p.path); showCode(true);
      }
    })();
  };

  // global keys when the Code tab is open
  document.addEventListener("keydown", (e) => {
    if ($("#tab-code").hidden) return;
    const mod = e.ctrlKey || e.metaKey, inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if (mod && e.key.toLowerCase() === "p" && !e.shiftKey) { e.preventDefault(); openQuick(); }
    else if (mod && e.shiftKey && "fegFEG".includes(e.key)) { e.preventDefault(); setView({ f: "search", e: "files", g: "git" }[e.key.toLowerCase()]); }
    else if (!inField && e.key === "F2" && C.treeSel) { e.preventDefault(); renameEntry(C.treeSel); }
    else if (!inField && (e.key === "Delete") && C.treeSel) { e.preventDefault(); deleteEntry(C.treeSel); }
  });
  addEventListener("beforeunload", (e) => { if (C.tabs.some(isDirty)) { e.preventDefault(); e.returnValue = ""; } });
}
initCode();
