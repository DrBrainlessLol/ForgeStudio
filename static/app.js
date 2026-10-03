"use strict";
// ============================================================ basics
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const ic = (n) => `<svg class="ic"><use href="#i-${n}"/></svg>`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

const hashArgs = new URLSearchParams(location.hash.slice(1));
if (hashArgs.get("token")) store.set("fs:token", hashArgs.get("token"));
if (hashArgs.get("login")) store.set("fs:login", hashArgs.get("login"));
const hashProject = hashArgs.get("project");
if (location.hash) history.replaceState(null, "", location.pathname);
let TOKEN = store.get("fs:token", "");
let LOGIN = store.get("fs:login", "");

class LoginNeeded extends Error {}
async function api(path, body) {
  const opts = { headers: { "X-Token": TOKEN, "X-Login": LOGIN } };
  if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.blob();
  if (r.status === 401 && data.login) { showLogin(); throw new LoginNeeded("Please sign in"); }
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const fileUrl = (path, dl) => `/api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(TOKEN)}&login=${encodeURIComponent(LOGIN)}${dl ? "&dl=1" : ""}`;
function toast(text, err) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), err ? 8000 : 3500);
}
// ---- notifications: a soft chime and (when Forge isn't in front) a system notification, per the user's settings
const NOTIFY_KINDS = { approval: "nApproval", done: "nDone", build: "nBuild", crash: "nCrash" };
let audioCtx = null;
function chime(kind) {
  try {
    audioCtx ??= new AudioContext();
    const t = audioCtx.currentTime, notes = kind === "approval" ? [880, 1175] : kind === "crash" || kind === "fail" ? [523, 392] : [659, 988];
    notes.forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0, t + i * 0.13);
      g.gain.linearRampToValueAtTime(0.16, t + i * 0.13 + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.13 + 0.35);
      o.connect(g).connect(audioCtx.destination);
      o.start(t + i * 0.13); o.stop(t + i * 0.13 + 0.4);
    });
  } catch {}
}
function alertUser(kind, title, body = "", onClick = null, failed = false) {
  const p = S.prefs || {};
  if (p[NOTIFY_KINDS[kind]] === false) return;
  const away = document.hidden || !document.hasFocus();
  if (p.sound !== false) chime(failed ? "fail" : kind);
  if (p.notify && away && "Notification" in window && Notification.permission === "granted") {
    const n = new Notification(title, { body, icon: "icon-192.png", tag: `${kind}:${title}`, renotify: true, requireInteraction: kind === "approval" });
    n.onclick = () => { window.focus(); n.close(); onClick?.(); };
  }
}
const act = (fn) => async (...a) => { try { await fn(...a); } catch (e) { if (!(e instanceof LoginNeeded)) toast(e.message, true); } };
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(t * 1000).toLocaleDateString();
};
const fmtSize = (n) => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

// ============================================================ state
const S = {
  profile: null, projects: [], tools: {}, previews: {}, procs: [], providers: [], agents: [], settings: {}, prefs: {}, web: {},
  cur: null, curChat: {}, items: {}, loaded: {}, meta: {}, running: {},
  procLogs: {}, consoleProc: null, lastGradle: null, viewProc: null, flutter: {}, fdevs: [],
  attachments: [],
  device: store.get("fs:device", "fill"), rotated: false,
};
const project = () => S.projects.find((p) => p.path === S.cur);
const isFlutter = () => !!project()?.flutter;
const BUILD_KINDS = new Set(["gradle", "flutter-task"]);  // jobs whose output goes to the Build view
const chatId = () => (S.cur ? S.curChat[S.cur] || null : null);
const pkey = (k) => `fs:${S.profile?.id}:${k}`;

// ============================================================ appearance
const ACCENTS = [["Blue", "#0A84FF"], ["Purple", "#BF5AF2"], ["Pink", "#FF375F"], ["Red", "#FF453A"], ["Orange", "#FF9F0A"],
  ["Yellow", "#FFD60A"], ["Green", "#30D158"], ["Teal", "#40C8E0"], ["Graphite", "#8E8E93"]];
function applyAppearance(p = {}) {
  const theme = p.theme || "dark", accent = p.accent || "#0A84FF", density = p.density || "regular";
  const resolved = theme === "system" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : theme;
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.density = density;
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--accent-fg", ["#FFD60A", "#40C8E0", "#30D158"].includes(accent) && resolved === "dark" ? "#000" : "#fff");
  document.querySelector('meta[name="theme-color"]').content = resolved === "light" ? "#f6f6f8" : "#000000";
  store.set("fs:theme", { theme, accent, density });
  $$("#themeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.theme === theme));
  $$("#densitySeg button").forEach((b) => b.classList.toggle("active", b.dataset.density === density));
  $$("#swatches .swatch").forEach((b) => b.classList.toggle("active", b.dataset.accent === accent));
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => applyAppearance(S.prefs));
async function setPref(patch) {
  S.prefs = { ...S.prefs, ...patch };
  applyAppearance(S.prefs);
  if (S.profile) S.prefs = await api("/api/prefs", patch).catch(() => S.prefs);
}

// ============================================================ markdown (small, safe)
function md(src) {
  const blocks = [];
  let s = String(src).replace(/```([\w+-]*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  s = esc(s);
  const inline = (t) => t
    .replace(/`([^`\n]+)`/g, (_, c) => /^(\/|~\/)[^\s]*\.[A-Za-z0-9]{1,6}$/.test(c)
      ? `<code>${c}</code><a class="dl" title="Download" href="${fileUrl(c.replace(/^~/, HOME()), true)}">${ic("download")}</a>` : `<code>${c}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const out = [];
  let list = null, para = [];
  const flushP = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; } };
  const flushL = () => { if (list) { out.push(`<${list.t}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; } };
  const lines = s.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) { flushP(); flushL(); out.push(line.trim()); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flushP(); flushL(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
    if ((m = line.match(/^\s*([-*]|\d+\.)\s+(.*)/))) {
      flushP(); const t = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.t !== t) { flushL(); list = { t, items: [] }; }
      list.items.push(m[2]); continue;
    }
    if (/^\|.*\|\s*$/.test(line) && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      flushP(); flushL();
      const row = (l, tag) => "<tr>" + l.trim().slice(1, -1).split("|").map((c) => `<${tag}>${inline(c.trim())}</${tag}>`).join("") + "</tr>";
      let html = "<table>" + row(line, "th"); i += 2;
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) html += row(lines[i++], "td");
      i--; out.push(html + "</table>"); continue;
    }
    if (!line.trim()) { flushP(); flushL(); continue; }
    flushL(); para.push(line);
  }
  flushP(); flushL();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[n]);
}
const HOME = () => (S.projects[0]?.path.match(/^\/home\/[^/]+/) || [""])[0];

// ============================================================ chat items
function toolArg(input = {}) {
  return input.command || input.file_path || input.path || input.pattern || input.url || input.query ||
    input.description || input.skill || (Object.keys(input).length ? JSON.stringify(input) : "");
}
const FILE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"];
function toolSection(label, cls, text) {
  return `<div class="tsec"><div class="tsec-h"><span>${esc(label)}</span><button class="tcopy" title="Copy">${ic("copy")}</button></div><pre class="${cls}">${esc(text)}</pre></div>`;
}
function toolBody(it) {
  let h = "";
  if (it.name === "Bash" && it.input?.command != null) {
    h += toolSection("Command", "t-cmd", it.input.command);
    if (it.input.description) h += `<div class="tdesc">${esc(it.input.description)}</div>`;
  } else if (it.input && (it.input.old_string != null || it.input.content != null)) {
    h += toolSection(it.input.content != null ? "New content" : "Change", "t-in",
      it.input.content ?? `- ${it.input.old_string}\n+ ${it.input.new_string}`);
  } else if (it.input && Object.keys(it.input).length) {
    h += toolSection("Input", "t-in", JSON.stringify(it.input, null, 2));
  }
  if (it.result != null) h += toolSection(it.error ? "Error output" : "Output", "t-out", it.result || "(no output)");
  return h;
}
function renderItem(it) {
  let el;
  if (it.k === "user") {
    el = document.createElement("div"); el.className = "msg user";
    if (it.files?.length) {
      const f = document.createElement("div"); f.className = "msg-files";
      f.innerHTML = it.files.map((x) => x.kind === "image"
        ? `<a href="${fileUrl(x.path)}" target="_blank"><img src="${fileUrl(x.path)}" alt="${esc(x.name)}" loading="lazy" decoding="async"></a>`
        : `<a class="file-chip" href="${fileUrl(x.path, true)}">${ic("file")}<span>${esc(x.name)}</span></a>`).join("");
      el.append(f);
    }
    if (it.text) { const b = document.createElement("div"); b.className = "bubble"; b.textContent = it.text; el.append(b); }
  } else if (it.k === "text") { el = document.createElement("div"); el.className = "msg assistant" + (it.sub ? " sub" : ""); el.innerHTML = md(it.text); }
  else if (it.k === "tool") {
    el = document.createElement("details"); el.className = "tool";
    if (it._open) el.open = true;
    const state = it.result == null ? ["run", "running"] : it.error ? ["err", "error"] : ["ok", "done"];
    const fp = FILE_TOOLS.includes(it.name) && it.input?.file_path;
    const inProj = fp && S.cur && (fp === S.cur || fp.startsWith(S.cur + "/") || !fp.startsWith("/"));
    const actions = fp && it.result != null && !it.error
      ? `<span class="tool-actions">${inProj ? `<a data-edit>${ic("code")}Edit</a>` : ""}<a href="${fileUrl(fp)}" target="_blank" onclick="event.stopPropagation()">${ic("external")}Open</a><a href="${fileUrl(fp, true)}" onclick="event.stopPropagation()">${ic("download")}Download</a></span>` : "";
    el.innerHTML = `<summary><span class="tname">${esc(it.name)}</span><span class="targ">${esc(toolArg(it.input).slice(0, 300))}</span>${actions}<span class="tstate ${state[0]}">${state[1]}</span></summary>`;
    // a long chat has hundreds of these; only an opened card pays for its command / output
    const fill = () => {
      if (el.dataset.filled) return;
      el.dataset.filled = "1";
      el.insertAdjacentHTML("beforeend", toolBody(it));
      el.querySelectorAll(".tcopy").forEach((b) => b.addEventListener("click", (e) => {
        e.stopPropagation(); e.preventDefault();
        const pre = b.closest(".tsec").querySelector("pre");
        navigator.clipboard.writeText(pre.textContent).then(() => toast("Copied")).catch(() => toast("Copy failed", true));
      }));
    };
    if (el.open) fill();
    el.ontoggle = () => { it._open = el.open; if (el.open) fill(); };
    el.querySelector("[data-edit]")?.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); if (typeof openInEditor === "function") openInEditor(fp); });
  } else if (it.k === "approval") el = renderApproval(it);
  else if (it.k === "meta") { el = document.createElement("div"); el.className = "meta" + (it.err ? " err" : ""); el.textContent = it.text; }
  it.el = el;
  return el;
}

const TOOL_VERBS = { Bash: "run a command", Write: "create or overwrite a file", Edit: "edit a file", MultiEdit: "edit a file",
  NotebookEdit: "edit a notebook", WebFetch: "fetch a web page", WebSearch: "search the web", AskUserQuestion: "ask you a question" };
function renderApproval(it) {
  const el = document.createElement("div");
  const danger = it.hints?.some((h) => h.level === "danger");
  const agentName = "Forge";
  if (it.decision) {
    const label = { allow: "Allowed", always: "Always allowed", deny: "Denied", cancelled: "Cancelled" }[it.decision] || it.decision;
    el.className = "approval done";
    el.innerHTML = `<div class="approval-head">${ic(it.decision === "deny" ? "x" : it.decision === "cancelled" ? "info" : "check")} ${label}: ${esc(it.tool)} <span class="dim">${esc(toolArg(it.input)).slice(0, 90)}</span></div>`;
    return el;
  }
  el.className = "approval" + (danger ? " danger" : "");
  if (it.tool === "AskUserQuestion") {
    const qs = it.input?.questions || [];
    const chosen = {};
    el.innerHTML = `<div class="approval-head">${ic("info")} ${esc(agentName)} has a question</div>` +
      qs.map((q, qi) => `<div class="q"><b>${esc(q.question)}</b><div class="q-opts">${(q.options || []).map((o, oi) =>
        `<button class="btn small" data-q="${qi}" data-o="${oi}" title="${esc(o.description || "")}">${esc(o.label)}</button>`).join("")}</div></div>`).join("") +
      `<div class="approval-actions"><button class="btn" data-a="deny">Skip</button><button class="btn primary" data-a="answer">Answer</button></div>`;
    el.querySelectorAll(".q-opts button").forEach((b) => b.onclick = () => {
      const q = qs[b.dataset.q];
      if (!q.multiSelect) el.querySelectorAll(`[data-q="${b.dataset.q}"]`).forEach((x) => x.classList.remove("sel"));
      b.classList.toggle("sel");
      const sel = [...el.querySelectorAll(`[data-q="${b.dataset.q}"].sel`)].map((x) => q.options[x.dataset.o].label);
      chosen[q.question] = sel.join(", ");
    });
    el.querySelector('[data-a="answer"]').onclick = act(() => api("/api/chat/approve", { id: it.id, decision: "allow", answers: chosen }));
    el.querySelector('[data-a="deny"]').onclick = act(() => api("/api/chat/approve", { id: it.id, decision: "deny", note: "The user skipped the question." }));
    return el;
  }
  const verb = TOOL_VERBS[it.tool] || `use ${it.tool}`;
  const detail = it.tool === "Bash" ? it.input?.command : it.input?.file_path ? it.input.file_path + (it.input.content ? `\n\n${String(it.input.content).slice(0, 1200)}` : it.input.old_string != null ? `\n\n- ${String(it.input.old_string).slice(0, 600)}\n+ ${String(it.input.new_string).slice(0, 600)}` : "") : JSON.stringify(it.input, null, 2);
  el.innerHTML = `<div class="approval-head">${ic(danger ? "alert" : "shield")} ${esc(agentName)} wants to ${esc(verb)}</div>
    ${it.description ? `<div class="dim">${esc(it.description)}</div>` : ""}
    <pre>${esc(detail)}</pre>
    ${it.hints?.length ? `<ul class="hints">${it.hints.map((h) => `<li class="${h.level}">${ic(h.level === "info" ? "info" : "alert")} ${esc(h.text)}</li>`).join("")}</ul>` : ""}
    <div class="approval-actions"><button class="btn" data-a="deny">Deny</button><button class="btn" data-a="always" title="Don't ask again for this kind of action in this chat">Always Allow</button><button class="btn ${danger ? "danger" : "primary"}" data-a="allow">Allow</button></div>`;
  el.querySelectorAll("[data-a]").forEach((b) => b.onclick = act(() => api("/api/chat/approve", { id: it.id, decision: b.dataset.a })));
  return el;
}

// apply one chat event to an item list; returns {pushed, changed} for live rendering
// id → item index per chat, so replaying a long log isn't quadratic
const itemIdx = (items) => items._idx || Object.defineProperty(items, "_idx", { value: new Map() })._idx;
function applyEvent(items, ev) {
  const idx = itemIdx(items);
  const last = items[items.length - 1];
  const pushed = [], changed = [];
  const push = (it) => { items.push(it); pushed.push(it); return it; };
  switch (ev.type) {
    case "user": push({ k: "user", text: ev.text, files: ev.files }); break;
    case "text": push({ k: "text", text: ev.text, sub: ev.sub }); break;
    case "text_start": push({ k: "text", text: "", sub: ev.sub }); break;
    case "delta": {
      let it = last && last.k === "text" && !!last.sub === !!ev.sub ? last : push({ k: "text", text: "", sub: ev.sub });
      it.text += ev.text;
      if (!pushed.includes(it)) changed.push(it);
      break;
    }
    case "tools":
      for (const b of ev.blocks) if (!idx.has("tool:" + b.id)) idx.set("tool:" + b.id, push({ k: "tool", id: b.id, name: b.name, input: b.input, result: null }));
      break;
    case "tool_results":
      for (const r of ev.results) {
        const it = idx.get("tool:" + r.id);
        if (it) { it.result = r.content; it.error = r.error; changed.push(it); }
      }
      break;
    case "approval":
      if (!idx.has("approval:" + ev.id)) idx.set("approval:" + ev.id, push({ k: "approval", id: ev.id, tool: ev.tool, input: ev.input, description: ev.description, hints: ev.hints }));
      break;
    case "approval_done": {
      const it = idx.get("approval:" + ev.id);
      if (it) { it.decision = ev.decision; changed.push(it); }
      break;
    }
    case "result": {
      const bits = [];
      if (ev.duration) bits.push((ev.duration / 1000).toFixed(1) + "s");
      if (ev.turns) bits.push(ev.turns + " turns");
      if (ev.cost) bits.push("$" + ev.cost.toFixed(3));
      if (ev.tokens) bits.push(ev.tokens.toLocaleString() + " tokens");
      push({ k: "meta", text: (ev.error ? "Stopped with an error" : "Done") + (bits.length ? " · " + bits.join(" · ") : "") });
      if (ev.denials?.length) {
        const names = [...new Set(ev.denials.map((d) => d.tool_name))].join(", ");
        push({ k: "meta", err: true, text: `Not allowed in this permission mode: ${names}. Change the mode under the message box and ask again.` });
      }
      break;
    }
    case "ui_raw": push({ k: "meta", text: ev.text, err: !!ev.err }); break;
    case "retry": {
      const text = ev.max ? `Connection problem (${ev.status || ev.error}) — retrying ${ev.attempt}/${ev.max}…` : `Reconnecting… ${ev.error}`;
      if (last?.k === "meta" && last.retry) { last.text = text; changed.push(last); } else push({ k: "meta", text, retry: true });
      break;
    }
    case "ui_end":
      for (const it of items) {
        if (it.k === "tool" && it.result == null) { it.result = "(interrupted)"; changed.push(it); }
        if (it.k === "approval" && !it.decision) { it.decision = "cancelled"; changed.push(it); }
      }
      break;
  }
  return { pushed, changed };
}

function renderChat() {
  const box = $("#messages");
  box.innerHTML = "";
  const cid = chatId();
  $("#chatTitle").textContent = cid ? S.meta[cid]?.title || "Chat" : "New chat";
  const ag = cid && S.meta[cid]?.agent;
  $("#agentPill").hidden = !ag || ag === "claude";
  $("#agentPill").textContent = S.agents.find((a) => a.id === ag)?.name || "";
  if (!S.cur) {
    $("#chatPane").classList.remove("home"); $("#homeChips").hidden = true;
    box.innerHTML = `<div class="empty"><img src="icon.svg" alt=""><h2>Welcome to Forge Studio</h2><p>Pick a project in the sidebar, or start one below. Switch between <b>Agent</b>, <b>Editor</b> and <b>Android</b> / <b>Flutter</b> at the top — the agent comes with you.</p>
      <div class="tiles" id="welcomeTiles">
        <div class="tile" data-w="open">${ic("folder")}<span class="label">Open Project</span></div>
        <div class="tile" data-w="clone">${ic("clone")}<span class="label">Clone Repo</span></div>
        <div class="tile" data-w="new">${ic("folder-plus")}<span class="label">New Project</span></div>
        <div class="tile hot" data-w="android">${ic("phone")}<span class="label">New Android App</span></div>
        <div class="tile hot" data-w="flutter">${ic("zap")}<span class="label">New Flutter App</span></div>
      </div></div>`;
    box.querySelector('[data-w="android"]').onclick = openNewApp;
    box.querySelector('[data-w="flutter"]').onclick = openNewFlutter;
    box.querySelector('[data-w="open"]').onclick = () => $("#btnAdd").click();
    box.querySelector('[data-w="clone"]').onclick = () => openProjectDialog("clone");
    box.querySelector('[data-w="new"]').onclick = () => openProjectDialog("new");
    updateBusy(); return;
  }
  const items = cid ? S.items[cid] || [] : [];
  const home = !items.length && S.mode === "agent";
  $("#chatPane").classList.toggle("home", home);
  $("#homeChips").hidden = !home;
  if (home) { renderHome(box); updateBusy(); return; }
  if (!items.length) box.innerHTML = `<div class="empty"><h2>${esc(project()?.name)}</h2><p>Ask the agent to build, fix or explain something. Attach screenshots, designs or files with the clip.</p><p class="dim">Earlier conversations are in the menu at the top.</p></div>`;
  const start = Math.max(0, items.length - (S.chatWindow || CHAT_WINDOW));
  if (start) {
    const more = document.createElement("button");
    more.className = "btn small plain more-msgs";
    more.textContent = `Show ${Math.min(start, CHAT_WINDOW)} earlier messages (${start} hidden)`;
    more.onclick = () => {
      const fromBottom = box.scrollHeight - box.scrollTop;
      S.chatWindow = (S.chatWindow || CHAT_WINDOW) + CHAT_WINDOW;
      renderChat();
      box.scrollTop = box.scrollHeight - fromBottom;  // keep the view where it was
    };
    box.append(more);
  }
  const frag = document.createDocumentFragment();
  for (let i = start; i < items.length; i++) frag.append(renderItem(items[i]));
  box.append(frag);
  updateBusy();
  box.scrollTop = box.scrollHeight;
}
const CHAT_WINDOW = 150;  // items rendered at once; long chats grow on demand
const pending = new Set();
function onChat(cid, projectPath, ev) {
  if (!S.items[cid]) {
    if (ev.type !== "user") { if (ev.type === "ui_start") S.running[cid] = projectPath; if (ev.type === "ui_end") delete S.running[cid]; renderProjects(); return; }
    S.items[cid] = [];
  }
  if (ev.type === "user" && !S.meta[cid]) S.meta[cid] = { id: cid, project: projectPath, agent: ev.agent, title: (ev.text || ev.files?.[0]?.name || "Chat").replace(/\s+/g, " ").slice(0, 70) };
  if (ev.type === "ui_start") S.running[cid] = projectPath;
  if (ev.type === "ui_end") delete S.running[cid];
  const { pushed, changed } = applyEvent(S.items[cid], ev);
  const visible = cid === chatId();
  if (visible) {
    const box = $("#messages");
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
    if (pushed.length && $("#chatPane").classList.contains("home")) { $("#chatPane").classList.remove("home"); $("#homeChips").hidden = true; box.innerHTML = ""; }
    if (pushed.length) box.querySelector(".empty")?.remove();
    for (const it of pushed) box.insertBefore(renderItem(it), $("#typing"));
    for (const it of changed) pending.add(it);
    if (pending.size && !S.raf) S.raf = requestAnimationFrame(() => {
      S.raf = 0;
      for (const it of pending) if (it.el?.isConnected) { const open = it.el.open; it.el.replaceWith(renderItem(it)); if (open) it.el.open = true; }
      pending.clear();
      if (box.scrollHeight - box.scrollTop - box.clientHeight < 400) box.scrollTop = box.scrollHeight;
    });
    if (stick || ev.type === "approval") box.scrollTop = box.scrollHeight;
  }
  const openIt = () => act(async () => { if (S.cur !== projectPath) await selectProject(projectPath); await openChat(cid); })();
  if (ev.type === "approval") {
    if (!visible || document.hidden) toast(`${S.meta[cid]?.title || "A chat"} needs your approval`);
    alertUser("approval", "Approval needed", `${S.meta[cid]?.title || "The agent"} wants to ${TOOL_VERBS[ev.tool] || "use " + ev.tool}`, openIt);
  }
  if (ev.type === "ui_start" || ev.type === "ui_end") { if (visible) updateBusy(); else renderProjects(); }
  if (ev.type === "ui_end" && !visible && S.meta[cid]) toast(`Finished: ${S.meta[cid].title}`);
  if (ev.type === "ui_end" && S.meta[cid] && (!visible || document.hidden || !document.hasFocus())) alertUser("done", "Agent finished", S.meta[cid].title, openIt);
  if (ev.type === "ui_end") { S.done = [{ cid }, ...(S.done || []).filter((d) => d.cid !== cid)].slice(0, 8); renderTasks(); }
  // live editor/git refresh when the agent edits files in the open project
  if (projectPath === S.cur && typeof codeAgentTouched === "function") {
    if (ev.type === "tool_results" && ev.results?.length) {
      const items = S.items[cid] || [];
      const paths = ev.results.map((r) => items.find((i) => i.k === "tool" && i.id === r.id))
        .filter((it) => it && FILE_TOOLS.includes(it.name) && !it.error).map((it) => it.input?.file_path).filter(Boolean);
      if (paths.length) { codeAgentTouched(paths); codeRefresh(); }
    }
    if (ev.type === "ui_end") codeRefresh();
  }
}
function updateBusy() {
  const cid = chatId();
  const busy = !!(cid && S.running[cid]);
  $("#btnStop").hidden = !busy;
  $("#btnSend").disabled = !S.cur || busy;
  $("#typing")?.remove();
  if (busy) { const t = document.createElement("div"); t.id = "typing"; t.className = "typing"; t.textContent = "Working…"; $("#messages").append(t); }
  renderProjects();
}
async function openChat(cid) {
  if (!S.cur) return;
  S.chatWindow = 0;
  S.curChat[S.cur] = cid;
  if (cid && !S.loaded[cid]) {
    const d = await api("/api/chat/log?id=" + cid);
    const items = [];
    for (const ev of d.events) applyEvent(items, ev);
    for (const ap of d.pending || []) applyEvent(items, ap);
    S.items[cid] = items; S.loaded[cid] = true; S.meta[cid] = d.chat;
    if (d.running) S.running[cid] = d.chat.project; else delete S.running[cid];
  }
  if (cid && S.meta[cid]?.agent && $("#agent").value !== S.meta[cid].agent && S.agents.some((a) => a.id === S.meta[cid].agent)) { $("#agent").value = S.meta[cid].agent; agentChanged(false); }
  renderChat();
  api("/api/chat/select", { project: S.cur, chat: cid }).catch(() => {});
}

// ============================================================ agents & models
const CLAUDE_MODELS = [
  ["", "Default model"], ["claude-fable-5-1", "Fable 5.1 — most capable"], ["claude-opus-5-5", "Opus 5.5"],
  ["claude-sonnet-5-5", "Sonnet 5.5"], ["claude-haiku-4-5", "Haiku 4.5 — fastest"], ["claude-opus-5", "Opus 5"],
  ["claude-sonnet-5", "Sonnet 5"], ["claude-fable-5", "Fable 5"], ["claude-opus-4-8", "Opus 4.8"],
];
const CODEX_MODELS = [["", "Default (from ~/.codex/config.toml)"]];  // available models depend on the OpenAI plan; use "Custom model ID…"
const currentAgent = () => S.agents.find((a) => a.id === $("#agent").value) || S.agents[0];
function renderAgents() {
  const sel = $("#agent"), prev = sel.value || S.prefs.agent || "claude";
  sel.innerHTML = "";
  for (const a of S.agents.filter((a) => a.installed)) sel.append(new Option(a.name, a.id));
  sel.value = [...sel.options].some((o) => o.value === prev) ? prev : sel.options[0]?.value;
  agentChanged(false);
}
function agentChanged(save = true) {
  const a = currentAgent();
  if (save && a) setPref({ agent: a.id });
  // only Claude Code can pause for approvals; Codex runs sandboxed, other CLIs use their own flags
  const kind = a?.kind;
  $$("#mode option").forEach((o) => (o.hidden = kind === "codex" && ["ask", "auto"].includes(o.value)));
  if (kind === "codex" && ["ask", "auto"].includes($("#mode").value)) $("#mode").value = "acceptEdits";
  $("#mode").disabled = kind === "generic";
  renderModels();
}
function renderModels() {
  const sel = $("#model"), a = currentAgent(), prev = store.get(pkey("model:" + a?.id), "local::");
  sel.innerHTML = "";
  const group = (label, prov, models) => {
    const g = document.createElement("optgroup"); g.label = label;
    for (const [id, name] of models) g.append(new Option(name, `${prov}::${id}`));
    g.append(new Option("Custom model ID…", `${prov}::__custom`));
    sel.append(g);
  };
  if (a?.kind === "claude") {
    group("Claude · this computer's login", "local", CLAUDE_MODELS);
    for (const p of S.providers) {
      if (p.type === "anthropic") group(`Claude · ${p.name}`, p.id, CLAUDE_MODELS);
      else group(p.name, p.id, p.models.length ? p.models.map((m) => [m, m]) : [["", "Default"]]);
    }
  } else if (a?.kind === "builtin") {
    if (!S.providers.length) sel.append(new Option("Add an API key in Settings → Models & Keys", "local::"));
    for (const p of S.providers) {
      if (p.type === "anthropic") group(`Claude · ${p.name}`, p.id, CLAUDE_MODELS);
      else group(p.name, p.id, p.models.length ? p.models.map((m) => [m, m]) : [["", "Default"]]);
    }
  } else if (a?.kind === "codex") group("Codex (your OpenAI login)", "local", CODEX_MODELS);
  else group(a?.name || "Agent", "local", [["", "Agent's default"]]);
  sel.value = [...sel.options].some((o) => o.value === prev) ? prev : sel.options[0].value;
  modelChanged();
}
function modelChanged() {
  store.set(pkey("model:" + currentAgent()?.id), $("#model").value);
  const custom = $("#model").value.endsWith("::__custom");
  $("#customModel").hidden = !custom;
  if (custom) { $("#customModel").value = store.get(pkey("customModel"), ""); $("#customModel").focus(); }
}
function modelChoice() {
  let [provider, model] = $("#model").value.split("::");
  if (model === "__custom") { model = $("#customModel").value.trim(); store.set(pkey("customModel"), model); }
  return { provider, model };
}

// ============================================================ attachments
function renderAttachments() {
  const box = $("#attachments");
  box.hidden = !S.attachments.length;
  box.innerHTML = "";
  S.attachments.forEach((a, i) => {
    const d = document.createElement("div");
    d.className = "att" + (a.uploading ? " up" : "");
    d.innerHTML = `${a.preview ? `<img src="${a.preview}" alt="">` : ic("file")}<span title="${esc(a.name)}">${esc(a.name)}</span><button type="button" title="Remove">${ic("x")}</button>`;
    d.querySelector("button").onclick = () => { S.attachments.splice(i, 1); renderAttachments(); };
    box.append(d);
  });
}
async function addFiles(list) {
  for (const f of list) {
    if (f.size > 100 * 1024 * 1024) { toast(`${f.name} is larger than 100 MB`, true); continue; }
    const a = { name: f.name || "pasted.png", uploading: true, preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : null };
    S.attachments.push(a); renderAttachments();
    try {
      const r = await fetch(`/api/upload?name=${encodeURIComponent(a.name)}`, { method: "POST", headers: { "X-Token": TOKEN, "X-Login": LOGIN }, body: f });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error);
      Object.assign(a, d, { uploading: false });
    } catch (e) { S.attachments.splice(S.attachments.indexOf(a), 1); toast(`Upload failed: ${e.message}`, true); }
    renderAttachments();
  }
}

function showAgent() {
  if (!$("#layout").classList.contains("no-agent")) return;
  $("#layout").classList.remove("no-agent"); store.set("fs:noagent", false); $("#btnAgent").classList.add("on");
}
// anything that puts text in the message box (Ask agent, Ask, Ask to fix…) brings a hidden agent chat back
{ const box = $("#prompt"), focus = box.focus.bind(box); box.focus = (...a) => { showAgent(); focus(...a); }; }
async function send(text) {
  showAgent();
  text = (text ?? $("#prompt").value).trim();
  const files = S.attachments.filter((a) => !a.uploading).map(({ name, path, size, kind, mime }) => ({ name, path, size, kind, mime }));
  if ((!text && !files.length) || !S.cur) return;
  if (S.attachments.some((a) => a.uploading)) throw new Error("Wait for the attachments to finish uploading");
  const p = S.cur, cid = chatId();
  if (cid && S.running[cid]) throw new Error("The agent is still working on this chat");
  const { provider, model } = modelChoice();
  const r = await api("/api/chat/send", { project: p, chat: cid, prompt: text, mode: $("#mode").value, model, provider, files, agent: $("#agent").value });
  S.loaded[r.chat] = true;
  S.items[r.chat] ??= [];
  if (!cid) { S.curChat[p] = r.chat; if (S.cur === p) renderChat(); }
  $("#prompt").value = ""; $("#prompt").style.height = ""; S.attachments = []; renderAttachments();
}

// ============================================================ history
async function showHistory() {
  if (!S.cur) return;
  const d = await api("/api/chats?project=" + encodeURIComponent(S.cur));
  const ul = $("#historyList");
  ul.innerHTML = "";
  if (!d.chats.length) ul.innerHTML = '<li class="dim">No conversations in this project yet.</li>';
  for (const c of d.chats) {
    S.meta[c.id] = c;
    if (c.running) S.running[c.id] = c.project;
    const li = document.createElement("li");
    if (c.id === chatId()) li.className = "active";
    const agent = c.agent && c.agent !== "claude" ? S.agents.find((a) => a.id === c.agent)?.name : "";
    const prov = c.provider && c.provider !== "local" ? S.providers.find((p) => p.id === c.provider)?.name : "";
    li.innerHTML = `<div class="h-main"><span class="h-title">${esc(c.title)}</span><span class="h-meta">${ago(c.updated || c.created)}${[agent, c.model, prov].filter(Boolean).map((x) => " · " + esc(x)).join("")}${c.running ? " · working" : ""}</span></div>
      <button class="icon-btn" title="Rename">${ic("edit")}</button><button class="icon-btn" title="Delete">${ic("trash")}</button>`;
    li.onclick = act(async () => { $("#historyPanel").hidden = true; await openChat(c.id); });
    const [bRename, bDel] = li.querySelectorAll("button");
    bRename.onclick = act(async (e) => {
      e.stopPropagation();
      const t = li.querySelector(".h-title");
      const inp = document.createElement("input"); inp.value = c.title;
      t.replaceWith(inp); inp.focus(); inp.select();
      inp.onclick = (ev) => ev.stopPropagation();
      inp.onkeydown = act(async (ev) => {
        if (ev.key === "Escape") return showHistory();
        if (ev.key !== "Enter") return;
        await api("/api/chat/rename", { chat: c.id, title: inp.value });
        S.meta[c.id].title = inp.value; if (c.id === chatId()) $("#chatTitle").textContent = inp.value;
        showHistory();
      });
    });
    bDel.onclick = act(async (e) => {
      e.stopPropagation();
      if (!bDel.classList.contains("confirm")) { bDel.classList.add("confirm"); bDel.textContent = "Delete"; return; }
      await api("/api/chat/delete", { chat: c.id });
      delete S.items[c.id]; delete S.loaded[c.id]; delete S.meta[c.id];
      if (chatId() === c.id) { S.curChat[S.cur] = null; renderChat(); }
      showHistory();
    });
    ul.append(li);
  }
  $("#historyPanel").hidden = false;
}

// ============================================================ projects
function renderProjects() {
  renderTasks(); renderStatusBar();
  const f = $("#projFilter").value.toLowerCase();
  const ul = $("#projList");
  ul.innerHTML = "";
  const busyProjects = new Set(Object.values(S.running));
  for (const p of S.projects) {
    if (f && !p.path.toLowerCase().includes(f)) continue;
    const li = document.createElement("li");
    if (p.path === S.cur) li.className = "active";
    const tags = [];
    if (p.android) tags.push('<span class="tag">Android</span>');
    if (p.flutter) tags.push('<span class="tag">Flutter</span>');
    if (p.web?.length) tags.push('<span class="tag">Web</span>');
    if (busyProjects.has(p.path)) tags.push('<span class="tag busy"><i class="dot"></i>working</span>');
    if (S.previews[p.path]) tags.push('<span class="tag live"><i class="dot"></i>live</span>');
    li.innerHTML = `<span class="pname">${esc(p.name)}</span><span class="ppath">${esc(p.path.replace(/^\/home\/[^/]+/, "~"))}</span><span class="tags">${tags.join("")}</span><button class="icon-btn rm" title="Remove from list">${ic("x")}</button>`;
    li.onclick = act(() => selectProject(p.path));
    li.querySelector(".rm").onclick = act(async (e) => {
      e.stopPropagation();
      await api("/api/projects/remove", { path: p.path });
      if (S.cur === p.path) S.cur = null;
      await loadState(); selectProject(S.cur);
    });
    ul.append(li);
  }
}
function renderTools() {
  const t = S.tools;
  const row = (name, ok) => `<span class="tool-row ${ok ? "ok" : "bad"}">${ic(ok ? "check" : "x")} ${name}</span>`;
  $("#toolStatus").innerHTML = [
    ...S.agents.filter((a) => a.installed).map((a) => row(a.name, true)),
    row("Git", t.git), row("Flutter", t.flutter), row("Android SDK / adb", t.adb), row("Android Studio", t.studio), row("Java", t.java), row("Node.js", t.node), row("scrcpy", t.scrcpy),
  ].join("");
}
async function selectProject(path) {
  S.cur = path || null;
  store.set(pkey("cur"), S.cur);
  const p = project();
  $("#crumb").textContent = p ? p.name : "Open a project";
  document.title = p ? `${p.name} — Forge Studio` : "Forge Studio";
  $("#historyPanel").hidden = true;
  if (innerWidth < 900) $("#layout").classList.remove("show-sidebar");
  renderProjects(); setupPreview(); setupAndroid(); renderProcs();
  if (typeof codeProjectChanged === "function") codeProjectChanged();
  // Android mode only exists for Android and Flutter apps; websites and other projects don't get the tab
  $('#modes [data-mode="android"]').hidden = !!p && !p.android && !p.flutter;
  $('#modes [data-mode="android"]').title = p?.flutter ? "Flutter — run with hot reload, debug and build your app (Ctrl+3)"
    : "Android — build, run and debug apps with Gradle (Ctrl+3)";
  if (p && !p.android && !p.flutter && S.mode === "android") setMode(store.get("fs:lastMode", "editor"));
  if (S.mode === "android") { loadToolchain(); act(refreshDevices)(); }  // Flutter and Android projects list different devices
  renderStatusBar();
  await openChat(chatId()).catch((e) => { S.curChat[S.cur] = null; renderChat(); toast(e.message, true); });
}
async function loadState() {
  const st = await api("/api/state");
  Object.assign(S, { profile: st.profile, projects: st.projects, tools: st.tools, previews: st.previews, procs: st.procs,
    providers: st.providers, settings: st.settings, agents: st.agents, plugins: st.plugins || [], prefs: st.prefs || {}, web: st.web,
    flutter: st.flutter || {} });
  for (const [proj, r] of Object.entries(S.flutter)) if (r.device === "web-server") S.previews[proj] = { url: r.url, flutter: true };
  for (const [proj, cid] of Object.entries(st.current)) if (!(proj in S.curChat)) S.curChat[proj] = cid;
  for (const cid of st.running) S.running[cid] ??= "";
  if (S.cur && !project()) S.cur = null;
  applyAppearance(S.prefs);
  renderProfile(); renderProjects(); renderTools(); renderAgents();
}

// ============================================================ sign-in
function avatarHtml(p) {
  return p?.picture ? `<img src="${esc(p.picture)}" alt="" referrerpolicy="no-referrer">` : esc((p?.name || "?").trim()[0]?.toUpperCase() || "?");
}
function renderProfile() {
  $("#avatar").innerHTML = avatarHtml(S.profile);
  $("#profileName").textContent = S.profile?.name || "";
}
async function showLogin() {
  const box = $("#login");
  if (!box.hidden && S.loginShown) return;
  S.loginShown = true;
  box.hidden = false;
  $("#loginMain").hidden = false; $("#pinForm").hidden = $("#googleSetup").hidden = true;
  const d = await fetch("/api/auth/profiles", { headers: { "X-Token": TOKEN } }).then((r) => r.json());
  $("#btnGoogle").hidden = !d.google;
  $("#btnGoogleSetup").textContent = d.google ? "Change Google sign-in setup" : "Set up Google sign-in";
  const list = $("#profileList");
  list.innerHTML = "";
  const tests = d.profiles.filter((p) => p.test);
  const shown = d.hideTest && !S.revealTest ? d.profiles.filter((p) => !p.test) : d.profiles;
  if (!shown.length && !tests.length) list.innerHTML = '<p class="dim">No profiles yet — create one below.</p>';
  const addBtn = (p) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "profile-item";
    b.innerHTML = `<span class="avatar">${avatarHtml(p)}</span><span class="grow"><b>${esc(p.name)}${p.test ? ' <span class="badge">test</span>' : ""}</b><br><span class="dim">${p.kind === "google" ? esc(p.email || "Google") : p.hasPin ? "PIN protected" : "Local profile"}</span></span>${p.hasPin ? ic("lock") : ""}`;
    b.onclick = act(async () => {
      if (p.kind === "google") return googleSignIn();
      if (p.hasPin) { S.pinFor = p; $("#pinName").textContent = p.name; $("#loginMain").hidden = true; $("#pinForm").hidden = false; $("#pinInput").value = ""; $("#pinInput").focus(); return; }
      finishLogin((await authPost("/api/auth/login", { id: p.id })).login);
    });
    list.append(b);
  };
  shown.forEach(addBtn);
  if (d.hideTest && tests.length && !S.revealTest) {
    const link = document.createElement("button");
    link.type = "button"; link.className = "link"; link.textContent = `Show ${tests.length} test profile${tests.length > 1 ? "s" : ""}`;
    link.onclick = () => { S.revealTest = true; S.loginShown = false; showLogin(); };
    list.append(link);
  }
}
async function authPost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "X-Token": TOKEN, "X-Login": LOGIN, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
function googleSignIn() {
  if (!["127.0.0.1", "localhost"].includes(location.hostname)) return toast("Google sign-in only works on the computer running Forge Studio — use a profile with a PIN here.", true);
  location.href = "/auth/google?token=" + encodeURIComponent(TOKEN);
}
function finishLogin(tok) { LOGIN = tok; store.set("fs:login", tok); location.reload(); }
function showTokenGate() {
  $("#login").hidden = false;
  $("#loginMain").hidden = $("#pinForm").hidden = $("#googleSetup").hidden = true;
  $("#tokenGate").hidden = false;
  $("#tokenInput").focus();
}
$("#tokenGate").onsubmit = (e) => {
  e.preventDefault();
  const v = $("#tokenInput").value.trim();
  const tok = v.includes("token=") ? new URLSearchParams(v.split("#")[1] || v.split("?")[1] || "").get("token") : v;
  if (!tok) return toast("Paste the token or the full link", true);
  store.set("fs:token", tok); location.reload();
};
$("#btnGoogle").onclick = googleSignIn;
$("#btnGoogleSetup").onclick = () => { $("#loginMain").hidden = true; $("#googleSetup").hidden = false; };
$("#gCancel").onclick = () => { $("#googleSetup").hidden = true; $("#loginMain").hidden = false; };
$("#googleSetup").onsubmit = act(async (e) => {
  e.preventDefault();
  await authPost("/api/auth/google-setup", { clientId: $("#gClientId").value, clientSecret: $("#gSecret").value });
  toast("Google sign-in is ready"); S.loginShown = false; showLogin();
});
$("#pinBack").onclick = () => { $("#pinForm").hidden = true; $("#loginMain").hidden = false; };
$("#pinForm").onsubmit = act(async (e) => { e.preventDefault(); finishLogin((await authPost("/api/auth/login", { id: S.pinFor.id, pin: $("#pinInput").value })).login); });
$("#newLocal").onsubmit = act(async (e) => { e.preventDefault(); finishLogin((await authPost("/api/auth/local", { name: $("#newName").value, pin: $("#newPin").value })).login); });

// ============================================================ settings window
// ---- setup checklist (Settings → Setup): what's installed, one-click installers, download links for the rest
async function loadSetup() {
  S.setup = await api("/api/setup/status");
  renderSetup(); renderSetupHint();
  return S.setup;
}
function renderSetup() {
  const box = $("#setupList");
  if (!box || !S.setup) return;
  $("#setupOs").textContent = `Detected: ${S.setup.os} (${S.setup.arch}).`;
  const groups = [...new Set(S.setup.items.map((i) => i.group))];
  box.innerHTML = groups.map((g) => `<h4>${esc(g)}</h4><div class="form-group setup-group">${S.setup.items.filter((i) => i.group === g).map((i) => `
    <div class="setup-row ${i.ok ? "ok" : "missing"}" data-item="${i.id}">
      <span class="setup-ic">${ic(i.ok ? "check" : i.optional ? "minus" : "alert")}</span>
      <div class="grow"><b>${esc(i.name)}</b>${i.optional && !i.ok ? ' <span class="badge">optional</span>' : ""}
        <div class="dim small-text">${esc(i.ok ? i.detail || "Installed" : i.note || "")}</div>
        ${!i.ok && i.cmd ? `<div class="copyline setup-cmd"><code class="grow">${esc(i.cmd)}</code><button class="icon-btn" data-copy="${esc(i.cmd)}" title="Copy">${ic("copy")}</button></div>` : ""}
        <div class="setup-progress dim small-text mono" ${i.running ? "" : "hidden"}>Starting…</div>
      </div>
      <div class="setup-acts">
        ${i.link ? `<a class="btn small plain" href="${esc(i.link)}" target="_blank" rel="noopener" title="Official download page">${ic("external")} ${i.install ? "Website" : "Download"}</a>` : ""}
        ${i.install ? `<button class="btn small ${i.ok ? "" : "primary"}" data-install="${i.id}" ${i.running ? "disabled" : ""}>${i.running ? "Installing…" : i.ok ? "Update" : "Install"}</button>` : ""}
      </div>
    </div>`).join("")}</div>`).join("");
  box.querySelectorAll("[data-install]").forEach((b) => (b.onclick = act(async () => {
    b.disabled = true; b.textContent = "Installing…";
    b.closest(".setup-row").querySelector(".setup-progress").hidden = false;
    await api("/api/setup/install", { item: b.dataset.install });
  })));
  box.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = act(async () => { await navigator.clipboard.writeText(b.dataset.copy); toast("Command copied, paste it in a terminal"); })));
}
function renderSetupHint() {
  const f = project()?.flutter;
  const groups = f ? ["Flutter", ...(f.platforms.includes("android") ? ["Android"] : [])] : ["Android"];
  const miss = (S.setup?.items || []).filter((i) => groups.includes(i.group) && !i.ok && !i.optional);
  $("#setupHint").hidden = !miss.length;
  $("#setupHintText").textContent = f ? `To run Flutter apps, install: ${miss.map((i) => i.name).join(", ")}.`
    : `To build Android apps, install: ${miss.map((i) => i.name).join(", ")}. No Android Studio needed.`;
}
$("#btnSetupHint").onclick = () => openSettings("setup");
$("#setupRecheck").onclick = act(loadSetup);
$("#setupAndroidAll").onclick = act(async () => {
  const todo = S.setup.items.filter((i) => i.group === "Android" && !i.ok && i.install && !i.running);
  if (!todo.length) return toast("The Android tools are already installed");
  // the SDK installer brings its own JDK when none is found, so one job covers both
  const ids = todo.map((i) => i.id).filter((id) => !(id === "jdk" && todo.some((t) => t.id === "android-sdk")));
  for (const id of ids) await api("/api/setup/install", { item: id });
  toast("Installing the Android tools… progress is shown here and in Processes");
});
$("#setupFlutterAll").onclick = act(async () => {
  const todo = S.setup.items.filter((i) => ["Flutter", "Android"].includes(i.group) && !i.ok && i.install && !i.running);
  if (!todo.length) return toast("Flutter and the Android tools are already installed");
  const ids = todo.map((i) => i.id).filter((id) => !(id === "jdk" && todo.some((t) => t.id === "android-sdk")));
  for (const id of ids) await api("/api/setup/install", { item: id });
  toast("Installing Flutter and the Android tools… progress is shown here and in Processes");
});

function openSettings(sec = "general") {
  showSection(sec);
  if (sec === "setup" || !S.setup) act(loadSetup)();
  $("#swatches").innerHTML = ACCENTS.map(([n, c]) => `<button class="swatch" title="${n}" data-accent="${c}" style="background:${c}"></button>`).join("");
  $$("#swatches .swatch").forEach((b) => (b.onclick = () => setPref({ accent: b.dataset.accent })));
  applyAppearance(S.prefs);
  $("#setMode").value = S.prefs.mode || "ask";
  renderNotifySettings();
  $("#setHideTest").checked = !!S.settings?.hideTest;
  fillProfile(); renderAgentList(); renderPluginList(); renderProviders(); renderWeb();
  $("#setStudio").value = S.settings?.studio_path || "";
  const t = S.tools;
  $("#setTools").innerHTML = `<div class="form-group">${[["Android Studio", t.studio], ["SDK", t.sdk], ["adb", t.adb], ["Java", t.java], ["Node.js", t.node], ["scrcpy", t.scrcpy]]
    .map(([k, v]) => `<div class="form-row"><span>${k}</span><span class="mono dim">${esc(v || "not found")}</span></div>`).join("")}</div>`;
  if (!$("#dlgSettings").open) $("#dlgSettings").showModal();
}
function showSection(sec) {
  $$("#settingsNav button").forEach((b) => b.classList.toggle("active", b.dataset.sec === sec));
  $$(".settings-body section").forEach((s) => (s.hidden = s.dataset.sec !== sec));
}
$$("#settingsNav button").forEach((b) => (b.onclick = () => showSection(b.dataset.sec)));
$("#setClose").onclick = () => $("#dlgSettings").close();
$("#btnSettings").onclick = () => openSettings("general");
$("#btnProfile").onclick = () => openSettings("profile");
$$("#themeSeg button").forEach((b) => (b.onclick = () => setPref({ theme: b.dataset.theme })));
$$("#densitySeg button").forEach((b) => (b.onclick = () => setPref({ density: b.dataset.density })));
function renderNotifySettings() {
  const p = S.prefs, perm = "Notification" in window ? Notification.permission : "unsupported";
  $("#setNotify").checked = !!p.notify && perm === "granted";
  $("#setSound").checked = p.sound !== false;
  for (const k of Object.values(NOTIFY_KINDS)) $("#" + k).checked = p[k] !== false;
  $("#notifState").textContent = perm === "unsupported" ? "Not available here (needs the app on this computer or HTTPS)"
    : perm === "denied" ? "Blocked — allow notifications for this site in the browser's site settings" : "Shown when Forge Studio isn't the active window";
}
$("#setNotify").onchange = act(async () => {
  if ($("#setNotify").checked) {
    if (!("Notification" in window)) { $("#setNotify").checked = false; throw new Error("Notifications aren't available here"); }
    const perm = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    if (perm !== "granted") { $("#setNotify").checked = false; renderNotifySettings(); throw new Error("Notifications are blocked — allow them in the browser's site settings (the icon left of the address)"); }
  }
  await setPref({ notify: $("#setNotify").checked }); renderNotifySettings();
});
$("#setSound").onchange = () => { setPref({ sound: $("#setSound").checked }); if ($("#setSound").checked) chime("done"); };
for (const k of Object.values(NOTIFY_KINDS)) $("#" + k).onchange = () => setPref({ [k]: $("#" + k).checked });
$("#btnTestNotify").onclick = () => {
  chime("approval");
  if (S.prefs.notify && Notification.permission === "granted") new Notification("Forge Studio", { body: "Notifications are working.", icon: "icon-192.png" });
  else toast("Sound played. Turn on desktop notifications above to get system notifications too.");
};
$("#setMode").onchange = () => { setPref({ mode: $("#setMode").value }); $("#mode").value = $("#setMode").value; };
$("#setHideTest").onchange = act(async () => { await api("/api/settings", { hideTest: $("#setHideTest").checked }); S.settings.hideTest = $("#setHideTest").checked; toast($("#setHideTest").checked ? "Test profiles will be hidden on the sign-in screen" : "Test profiles will be shown"); });

function fillProfile() {
  const p = S.profile;
  $("#pfAvatar").innerHTML = avatarHtml(p); $("#pfTitle").textContent = p.name;
  $("#pfSub").textContent = p.kind === "google" ? `Google · ${p.email}` : p.hasPin ? "Local profile · PIN protected" : "Local profile · no PIN";
  $("#pfName").value = p.name; $("#pfPin").value = "";
  $("#pfTest").checked = !!p.test;
  $("#pfPinRow").hidden = p.kind === "google";
  $("#pfPin").placeholder = p.hasPin ? "•••• (type to change, clear to remove)" : "No PIN";
  $("#pfInfo").textContent = p.kind === "google" ? "Your Google account protects this profile." : "A PIN stops other people on this computer (or your network) from opening your profile.";
  $("#pfDelete").innerHTML = ic("trash") + " Delete…"; $("#pfDelete").classList.remove("confirm");
  const g = S.settings?.google?.clientId;
  $("#setGoogleState").innerHTML = g ? `Configured (client ${esc(g.slice(0, 12))}…). To change it, sign out and use “Change Google sign-in setup”.` : "Not set up. Sign out and choose “Set up Google sign-in” on the sign-in screen.";
}
$("#pfSave").onclick = act(async () => {
  const body = { name: $("#pfName").value, test: $("#pfTest").checked };
  if (S.profile.kind !== "google" && ($("#pfPin").value || $("#pfPin").dataset.clear)) body.pin = $("#pfPin").value;
  S.profile = await api("/api/profile", body); renderProfile(); fillProfile(); toast("Profile saved");
});
$("#pfPin").oninput = () => { $("#pfPin").dataset.clear = S.profile.hasPin && !$("#pfPin").value ? "1" : ""; };
$("#pfSwitch").onclick = () => { store.del("fs:login"); location.reload(); };
$("#pfLogout").onclick = act(async () => { await authPost("/api/auth/logout", {}); store.del("fs:login"); location.reload(); });
$("#pfDelete").onclick = act(async () => {
  const b = $("#pfDelete");
  if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Click again to delete everything"; return; }
  await api("/api/profile/delete", {}); store.del("fs:login"); location.reload();
});

// agents
function renderAgentList() {
  const ul = $("#agentList");
  ul.innerHTML = "";
  for (const a of S.agents) {
    const li = document.createElement("li");
    const badge = a.id === "claude" ? '<span class="badge star">Recommended</span>' : a.kind === "codex" ? '<span class="badge">Native</span>' : "";
    li.innerHTML = `${ic(a.kind === "claude" ? "bot" : "terminal")}<span class="grow"><b>${esc(a.name)}</b> ${badge}<small class="mono">${esc(a.template || a.bin)}</small></span>
      <span class="badge ${a.installed ? "ok" : ""}">${a.installed ? "Installed" : "Not installed"}</span>${a.custom ? `<button class="icon-btn" title="Remove">${ic("trash")}</button>` : ""}`;
    li.querySelector("button")?.addEventListener("click", act(async () => { await api("/api/agents/delete", { id: a.id }); await loadState(); renderAgentList(); }));
    ul.append(li);
  }
}
$("#agAdd").onclick = act(async () => {
  await api("/api/agents/save", { name: $("#agName").value.trim(), template: $("#agTpl").value.trim() });
  $("#agName").value = $("#agTpl").value = ""; await loadState(); renderAgentList(); toast("Agent added — pick it under the message box");
});

// plugins
function renderPluginList() {
  const ul = $("#pluginList");
  ul.innerHTML = S.plugins?.length ? "" : '<li class="dim">No plugins installed yet.</li>';
  for (const p of S.plugins || []) {
    const parts = p.error ? [] : [[p.commands.length, "command"], [p.agents.length, "agent"], [p.skills.length, "skill"], [p.mcp.length, "MCP server"]]
      .filter(([n]) => n).map(([n, w]) => `${n} ${w}${n > 1 ? "s" : ""}`);
    const li = document.createElement("li");
    li.innerHTML = `${ic("plug")}<span class="grow"><b>${esc(p.name)}</b> ${p.version ? `<span class="badge">${esc(p.version)}</span>` : ""}
      <small>${p.error ? `<span style="color:var(--err,#e5484d)">${esc(p.error)}</span>` : esc(p.description || "No description")}</small>
      ${parts.length ? `<small>${esc(parts.join(" · "))}${p.commands.length ? " — " + esc(p.commands.map((c) => "/" + c.name).join(" ")) : ""}</small>` : ""}</span>
      ${p.error ? "" : `<label class="badge ${p.enabled ? "ok" : ""}" style="cursor:pointer"><input type="checkbox" ${p.enabled ? "checked" : ""} hidden>${p.enabled ? "Enabled" : "Disabled"}</label>`}
      <button class="icon-btn" title="Remove">${ic("trash")}</button>`;
    li.querySelector("input")?.addEventListener("change", act(async (e) => { await api("/api/plugins/toggle", { id: p.dir, enabled: e.target.checked }); await loadState(); renderPluginList(); }));
    li.querySelector("button").addEventListener("click", act(async () => {
      if (!confirm(`Remove plugin "${p.name}"?`)) return;
      await api("/api/plugins/remove", { id: p.dir }); await loadState(); renderPluginList();
    }));
    ul.append(li);
  }
}
$("#plInstall").onclick = act(async () => {
  const r = await api("/api/plugins/install", { source: $("#plSource").value.trim() });
  $("#plSource").value = ""; await loadState(); renderPluginList(); toast(`Installed ${r.name}`);
});
$("#plCreate").onclick = act(async () => {
  const r = await api("/api/plugins/create", { name: $("#plNewName").value.trim(), description: $("#plNewDesc").value.trim() });
  $("#plNewName").value = $("#plNewDesc").value = ""; await loadState(); renderPluginList(); toast(`Created ${r.name} in ~/.config/forge-studio/plugins/${r.name}`);
});

// providers
const PRESETS = [
  { key: "anthropic", type: "anthropic", api: "anthropic", name: "My Anthropic API key", hint: "Create a key at console.anthropic.com → API keys. Usage is billed to your own Anthropic account." },
  { key: "openai", type: "compatible", api: "openai", name: "OpenAI", baseUrl: "https://api.openai.com/v1", models: ["gpt-5", "gpt-5-mini", "o3"], hint: "Key from platform.openai.com. Uses the OpenAI Chat Completions API." },
  { key: "openrouter", type: "compatible", api: "openai", name: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", models: ["openai/gpt-5", "google/gemini-2.5-pro", "anthropic/claude-sonnet-4"], hint: "Key from openrouter.ai/keys. Any model id from openrouter.ai/models (GPT, Gemini, Llama, Qwen…)." },
  { key: "deepseek", type: "compatible", api: "openai", name: "DeepSeek", baseUrl: "https://api.deepseek.com/v1", models: ["deepseek-chat", "deepseek-reasoner"], hint: "Key from platform.deepseek.com." },
  { key: "groq", type: "compatible", api: "openai", name: "Groq", baseUrl: "https://api.groq.com/openai/v1", models: ["llama-3.3-70b-versatile"], hint: "Fast inference. Key from console.groq.com." },
  { key: "kimi", type: "compatible", api: "anthropic", name: "Moonshot Kimi", baseUrl: "https://api.moonshot.ai/anthropic", models: ["kimi-k2-turbo-preview"], hint: "Key from platform.moonshot.ai (Anthropic-compatible endpoint)." },
  { key: "glm", type: "compatible", api: "anthropic", name: "Z.ai GLM", baseUrl: "https://api.z.ai/api/anthropic", models: ["glm-4.6"], hint: "Key from z.ai (Anthropic-compatible endpoint)." },
  { key: "ollama", type: "compatible", api: "openai", name: "Ollama (local)", baseUrl: "http://localhost:11434/v1", models: ["qwen3-coder", "llama3.1"], hint: "Free and runs on this computer. Install a model with `ollama pull`; no key needed." },
  { key: "custom", type: "compatible", api: "openai", name: "Custom endpoint", baseUrl: "", models: [], hint: "Any OpenAI- or Anthropic-compatible endpoint. Set the API style below to match." },
];
function renderProviders() {
  const ul = $("#provList");
  ul.innerHTML = `<li>${ic("bot")}<span class="grow"><b>This computer's Claude login</b><small>Always available</small></span></li>`;
  for (const p of S.providers) {
    const li = document.createElement("li");
    li.innerHTML = `${ic("key")}<span class="grow"><b>${esc(p.name)}</b><small>${p.type === "anthropic" ? "Anthropic API key" : esc(p.baseUrl)} · key ${p.apiKey ? esc(p.apiKey) : "not set"}${p.models?.length ? " · " + esc(p.models.join(", ")) : ""}</small></span>
      <button class="icon-btn" title="Edit">${ic("edit")}</button><button class="icon-btn" title="Remove">${ic("trash")}</button>`;
    const [bEdit, bDel] = li.querySelectorAll("button");
    bEdit.onclick = () => editProvider(p);
    bDel.onclick = act(async () => {
      if (!bDel.classList.contains("confirm")) { bDel.classList.add("confirm"); bDel.textContent = "Remove"; return; }
      await api("/api/providers/delete", { id: p.id }); await loadState(); renderProviders();
    });
    ul.append(li);
  }
  const sel = $("#provPreset");
  if (!sel.options.length) PRESETS.forEach((p) => sel.append(new Option(p.name, p.key)));
}
function editProvider(p, preset) {
  const src = p || preset;
  $("#peId").value = p?.id || ""; $("#peType").value = src.type;
  $("#peName").value = src.name; $("#peUrl").value = src.baseUrl || "";
  $("#peKey").value = p?.apiKey || ""; $("#peKey").placeholder = src.type === "anthropic" ? "sk-ant-…" : "API key";
  $("#peModels").value = (src.models || []).join("\n");
  $("#peApi").value = src.api || "openai";
  $("#peUrlWrap").hidden = $("#peModelsWrap").hidden = $("#peApiWrap").hidden = src.type === "anthropic";
  $("#peHint").textContent = preset?.hint || PRESETS.find((x) => x.baseUrl && x.baseUrl === p?.baseUrl)?.hint || "";
  $("#provEdit").hidden = false; $("#peName").focus();
}
$("#provAdd").onclick = () => editProvider(null, PRESETS.find((p) => p.key === $("#provPreset").value));
$("#peCancel").onclick = () => ($("#provEdit").hidden = true);
$("#peSave").onclick = act(async () => {
  await api("/api/providers/save", { provider: { id: $("#peId").value || null, type: $("#peType").value, name: $("#peName").value,
    api: $("#peType").value === "anthropic" ? "anthropic" : $("#peApi").value,
    baseUrl: $("#peUrl").value.trim(), apiKey: $("#peKey").value.trim(), models: $("#peModels").value.split("\n") } });
  $("#provEdit").hidden = true; await loadState(); renderProviders(); toast("Saved — pick it in the model menu");
});

// web app
let installPrompt = null;
addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installPrompt = e; });
function renderWeb() {
  $("#lanToggle").checked = !!S.web?.lan;
  $("#lanInfo").hidden = !S.web?.lan;
  $("#lanUrl").value = S.web?.url || "";
  $("#tokenView").value = TOKEN;
  $("#btnInstall").textContent = matchMedia("(display-mode: standalone)").matches ? "Installed" : "Install…";
}
$("#tokenReveal").onclick = () => { const i = $("#tokenView"); i.type = i.type === "password" ? "text" : "password"; };
$("#tokenCopy").onclick = act(async () => { await navigator.clipboard.writeText(TOKEN); toast("Token copied — keep it secret"); });
$("#localCopy").onclick = act(async () => { await navigator.clipboard.writeText(S.web?.local || location.origin + "/#token=" + TOKEN); toast("Link copied"); });
$("#btnInstall").onclick = act(async () => {
  if (installPrompt) { installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; return; }
  toast("Use the browser's menu → “Install Forge Studio…” (or “Add to Home screen” on a phone).");
});
$("#lanToggle").onchange = act(async () => { S.web = await api("/api/web-access", { enabled: $("#lanToggle").checked }); renderWeb(); });
$("#lanCopy").onclick = act(async () => { await navigator.clipboard.writeText($("#lanUrl").value); toast("Link copied"); });
$("#setStudioSave").onclick = act(async () => { await api("/api/settings", { studio_path: $("#setStudio").value.trim() || null }); await loadState(); toast("Saved"); });

// ============================================================ preview
function setupPreview() {
  const p = project();
  const sel = $("#webRoot");
  sel.innerHTML = "";
  const roots = p?.flutter ? [{ dir: p.path, rel: ".", kind: "Flutter web", command: null }]
    : p?.web?.length ? p.web : p ? [{ dir: p.path, rel: ".", kind: "static", command: null }] : [];
  $("#devCmd").disabled = !!p?.flutter;
  for (const r of roots) sel.append(new Option(`${r.rel === "." ? p.name : r.rel} (${r.kind})`, r.dir));
  const saved = p && store.get(pkey("prev:" + p.path), null);
  if (saved?.dir && roots.some((r) => r.dir === saved.dir)) sel.value = saved.dir;
  const root = roots.find((r) => r.dir === sel.value);
  $("#devCmd").value = saved?.dir === sel.value ? saved.command ?? "" : root?.command || "";
  const live = p && S.previews[p.path];
  $("#btnPrevStart").hidden = !!live; $("#btnPrevStop").hidden = !live;
  $("#btnPrevStart").disabled = !p;
  $("#prevLog").textContent = "";
  const proc = p && S.procs.filter((x) => x.kind === "preview" && x.project === p.path).pop();
  if (proc) loadLog(proc.id, $("#prevLog"));
  setUrl(live?.url || (p && store.get(pkey("url:" + p.path), "")) || "", !!live);
}
function setUrl(url, load) {
  $("#prevUrl").value = url;
  if (load && url) { $("#iframe").src = url; $("#stageEmpty").hidden = true; }
  else if (!url) { $("#iframe").removeAttribute("src"); $("#stageEmpty").hidden = false; }
}
function applyDevice() {
  const f = $("#frame"), d = S.device;
  $("#device").value = d;
  if (d === "fill") { f.className = "frame fill"; f.style.width = f.style.height = ""; return; }
  let [w, h] = d.split("x").map(Number);
  if (S.rotated) [w, h] = [h, w];
  f.className = "frame" + (w < 900 ? " device" : "");
  f.style.width = w + "px"; f.style.height = h + "px";
}
function appendLog(pre, line) {
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const span = document.createElement("span");
  if (/\b(error|failed|failure|exception|fatal)\b|^\s*e: /i.test(line)) span.className = "l-err";
  else if (/\bwarn(ing)?\b/i.test(line)) span.className = "l-warn";
  else if (/BUILD SUCCESSFUL|ready in|compiled successfully|Serving HTTP/i.test(line)) span.className = "l-ok";
  span.textContent = line.replace(/\x1b\[[0-9;]*m/g, "") + "\n";
  pre.append(span);
  while (pre.childNodes.length > 3000) pre.firstChild.remove();
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
async function loadLog(id, pre) {
  const { lines } = await api("/api/proc/log?id=" + id);
  pre.textContent = "";
  S.procLogs[id] = lines;
  lines.forEach((l) => appendLog(pre, l));
}

// ============================================================ android
function setupAndroid() {
  const p = project();
  const a = p?.android, f = p?.flutter;
  $("#droidPane").classList.toggle("flutter", !!f);
  $("#modeDroidLabel").textContent = f ? "Flutter" : "Android";
  $("#tasksTabLabel").textContent = f ? "Flutter" : "Gradle";
  setVariants(!!f);
  $("#btnRun").title = f ? "Build and launch with hot reload (the app's output shows on the right)" : "Build, install and launch (logcat starts automatically)";
  $("#btnRestart").title = f ? "Hot restart (resets app state)" : "Restart the app without rebuilding";
  $("#btnBuild").title = f ? "Build for the selected device's platform" : "Build APK";
  const mods = $("#moduleSel");
  mods.innerHTML = "";
  (a?.modules || []).forEach((m) => mods.append(new Option(`${m.name}${m.applicationId ? "  ·  " + m.applicationId : ""}`, m.name)));
  const mod = a?.modules?.[0];
  $("#androidInfo").textContent = f ? [f.applicationId || f.name, f.version && `v${f.version}`, f.platforms.join(" · "),
    S.tools.flutter?.version && `Flutter ${S.tools.flutter.version}`].filter(Boolean).join(" · ") : !a ? "" : [mod?.applicationId, mod?.minSdk && `minSdk ${mod.minSdk}`,
    mod?.compileSdk && `compileSdk ${mod.compileSdk}`, a.gradle && `Gradle ${a.gradle}`, mod && (mod.compose ? "Compose" : "Views")].filter(Boolean).join(" · ");
  $("#droidEmpty").hidden = !!(a || f);
  $("#droidEmptyText").textContent = !p ? "Pick a project, or create a new Android or Flutter app." : `${p.name} isn't an Android Gradle or Flutter project.`;
  $("#droidPane").classList.toggle("no-app", !a && !f);
  $("#wrapperHint").hidden = !a || a.gradlew;
  for (const id of ["#btnRun", "#btnBuild", "#btnStopApp", "#btnRestart", "#btnTask"]) $(id).disabled = f ? !S.tools.flutter : !a || !a.gradlew;
  $("#btnFlTask").disabled = $("#btnFlPkg").disabled = !S.tools.flutter;
  if (!f) $("#btnRun").innerHTML = ic("play") + " Run";
  renderTaskChips(); renderFlutterRun();
  $("#btnStudio").disabled = !p;
  $("#andLog").textContent = ""; $("#btnFix").hidden = true; $("#btnConsoleStop").hidden = true;
  S.consoleProc = null;
  const g = p && S.procs.filter((x) => BUILD_KINDS.has(x.kind) && x.project === p.path).pop();
  if (g) { S.consoleProc = g.id; $("#consoleTitle").textContent = g.label; $("#btnConsoleStop").hidden = !g.running; loadLog(g.id, $("#andLog")); }
  else $("#consoleTitle").textContent = "Build output";
  $('#lcScope option[value="flutter"]').hidden = !f;
  $("#lcScope").value = f ? store.get("fs:flscope", "flutter") : $("#lcScope").value === "flutter" ? "app" : $("#lcScope").value;
  attachRunLog();
}
// the log column of Run & Logcat: a Flutter project's `flutter run` output, or logcat
function attachRunLog() {
  const p = project();
  LC.proc = null; LC.lines = []; LC.crash = null; LC.flErr = false; $("#crashBar").hidden = true;
  LC.flutter = !!p?.flutter && $("#lcScope").value === "flutter";
  $("#btnLogcat").hidden = LC.flutter;
  const kind = LC.flutter ? "flutter" : "logcat";
  const l = p && S.procs.filter((x) => x.kind === kind && x.project === p.path).pop();
  if (l) { attachLogcat(l.id, l.running); api("/api/proc/log?id=" + l.id).then((d) => { if (LC.proc === l.id) { d.lines.forEach((x) => lcAdd(x, true)); lcRender(); } }).catch(() => {}); }
  else { lcSetRunning(false); lcRender(); }
}
// ---- flutter
const flRun = () => S.procs.find((x) => x.kind === "flutter" && x.project === S.cur && x.running);
const fdev = () => S.fdevs.find((x) => x.id === $("#deviceSel").value);
const isAndroidDev = (d) => d?.platform?.startsWith("android");
function setVariants(flutter) {
  const sel = $("#variantSel"), want = flutter ? "flutter" : "android";
  if (sel.dataset.kind === want) return;
  sel.dataset.kind = want;
  sel.innerHTML = flutter ? '<option value="debug">debug</option><option value="profile">profile</option><option value="release">release</option>'
    : '<option value="Debug">debug</option><option value="Release">release</option>';
  sel.value = store.get(flutter ? "fs:flmode" : "fs:variant", flutter ? "debug" : "Debug");
  if (!sel.value) sel.selectedIndex = 0;
}
function renderFlutterRun() {
  if (!isFlutter()) return;
  const on = !!flRun(), started = !!S.flutter[S.cur]?.started;
  $("#btnHotReload").disabled = $("#btnRestart").disabled = !started;
  $("#btnStopApp").disabled = !on;
  // the Web server device has no Dart VM service, so no debug toggles or DevTools there
  const vm = started && S.flutter[S.cur]?.device !== "web-server";
  $$("#flDebug [data-ext]").forEach((b) => (b.disabled = !vm));
  $("#btnDevtools").disabled = !vm || variant() === "release";
  $("#flDebug").title = started && !vm ? "Debug tools need Android, desktop or Chrome — the Web server device has no Dart VM service" : "";
  $("#btnRun").innerHTML = ic("play") + (on ? " Rerun" : " Run");
  $("#btnRun").disabled = !S.tools.flutter;
  const d = fdev();
  $("#flNoMirror").hidden = !d || isAndroidDev(d);
  $("#flNoMirror").textContent = d?.platform?.startsWith("web") ? (d.id === "web-server" ? "Web apps on the Web server device open in Agent mode → Preview." : "Web apps open in their own browser window.")
    : "Desktop and iOS apps open in their own window; the screen mirror is for Android devices.";
}
async function flutterRun() {
  const dev = $("#deviceSel").value;
  if (!dev) throw new Error("Pick a device: connect a phone, launch an emulator, or choose Web server / desktop");
  if (flRun()) {  // rerun: stop the old session first so the new build is what you see
    await api("/api/flutter/stop", { project: S.cur });
    for (let i = 0; i < 40 && flRun(); i++) await new Promise((r) => setTimeout(r, 250));
    if (flRun()) throw new Error("The previous run is still stopping — try again in a moment");
  }
  $("#lcScope").value = "flutter"; store.set("fs:flscope", "flutter"); attachRunLog(); setDroidView("run");
  $$("#flDebug [data-ext]").forEach((b) => b.classList.toggle("on", b.dataset.ext === "debugBanner"));
  await api("/api/flutter/run", { project: S.cur, device: dev, mode: variant(), auto: $("#flAuto").checked });
  toast(dev === "web-server" ? "Building… the app opens in Agent mode → Preview" : "Building and launching… the app's output shows on the right");
}
const FL_BUILD = { android: "apk", web: "web", linux: "linux", darwin: "macos", ios: "ios", windows: "windows" };
function flutterBuildTarget() {
  const plat = (fdev()?.platform || "android").split("-")[0];
  return FL_BUILD[plat] || "apk";
}
async function flutterTask(args) {
  $("#andLog").textContent = ""; $("#btnFix").hidden = true; setDroidView("build");
  await api("/api/flutter/task", { project: S.cur, args });
}
function openNewFlutter() {
  if (!S.tools.flutter) { toast("Install the Flutter SDK first (Settings → Setup, one click)", true); return openSettings("setup"); }
  $("#nfName").value = ""; $("#nfOrg").value = store.get("fs:nforg", "com.example");
  $("#nfParent").value = store.get("fs:nfparent", "~/projects");
  const host = /Mac/.test(navigator.platform) ? "macos" : "linux";
  $$("#nfPlatforms input").forEach((c) => (c.checked = ["android", "ios", "web", host].includes(c.value)));
  $("#dlgNewFlutter").showModal(); $("#nfName").focus();
}
function setDroidView(v) {
  $$("#droidTabs button").forEach((b) => b.classList.toggle("active", b.dataset.dv === v));
  $$("#droidPane .droid-view").forEach((el) => (el.hidden = el.dataset.dv !== v));
  if (v === "build") $("#buildDot").hidden = true;
  store.set("fs:droidView", v);
}
const variant = () => $("#variantSel").value || (isFlutter() ? "debug" : "Debug");
const GRADLE_CHIPS = ["clean", "assemble{V}", "bundle{V}", "test{V}UnitTest", "lint{V}", "connected{V}AndroidTest", "dependencies", "signingReport", "tasks"];
const FLUTTER_CHIPS = ["pub get", "pub upgrade", "pub outdated", "analyze", "test", "dart format .", "dart fix --apply", "clean",
  "build apk", "build appbundle", "build web", "build {desktop}", "gen-l10n", "dart run build_runner build -d", "doctor -v"];
function renderTaskChips() {
  if (isFlutter()) return renderFlutterChips();
  const m = $("#moduleSel").value || "app";
  $("#taskChips").innerHTML = GRADLE_CHIPS.map((t) => {
    const task = t.replace("{V}", variant());
    const full = ["clean", "tasks", "signingReport"].includes(task) ? task : `:${m}:${task}`;
    return `<button class="chip mono" data-task="${esc(full)}">${esc(full)}</button>`;
  }).join("");
  $$("#taskChips .chip").forEach((c) => (c.onclick = act(() => { $("#gradleTask").value = c.dataset.task; return gradleTask(c.dataset.task); })));
}
function renderFlutterChips() {
  const f = project().flutter, desktop = /Mac/.test(navigator.platform) ? "macos" : "linux";
  const mode = variant() === "debug" ? "" : ` --${variant()}`;
  $("#flChips").innerHTML = FLUTTER_CHIPS.map((t) => {
    let cmd = t.replace("{desktop}", desktop);
    if (/^build (apk|appbundle|web|linux|macos)$/.test(cmd) && mode) cmd += mode;
    return `<button class="chip mono" data-cmd="${esc(cmd)}">${esc(cmd)}</button>`;
  }).join("");
  $$("#flChips .chip").forEach((c) => (c.onclick = act(() => { $("#flTask").value = c.dataset.cmd; return flutterTask(c.dataset.cmd); })));
  const missing = ["android", "ios", "web", "linux", "macos", "windows"].filter((x) => !f.platforms.includes(x));
  $("#flPlatInfo").textContent = `This app targets ${f.platforms.join(", ") || "no platforms yet"}.` + (missing.length ? " Add another:" : "");
  $("#flPlatforms").innerHTML = missing.map((x) => `<button class="chip" data-plat="${x}">${ic("plus")} ${x}</button>`).join("");
  $$("#flPlatforms .chip").forEach((c) => (c.onclick = act(() => flutterTask(`create --platforms=${c.dataset.plat} .`))));
}
async function loadToolchain() {
  if (isFlutter()) {
    const t = S.tools, fv = t.flutter;
    const row = (k, v) => `<div><span class="dim">${k}</span><span class="mono">${esc(v || "—")}</span></div>`;
    $("#toolchain").innerHTML = row("Flutter", fv && `${fv.version || "?"}${fv.channel ? " · " + fv.channel : ""}`) + row("Dart", fv?.dart) +
      row("Flutter SDK", fv?.root) + row("Android SDK", t.sdk) + row("JDK", t.java) + row("adb", t.adb);
    return;
  }
  try {
    S.sdk ??= await api("/api/android/sdk");
    const a = project()?.android, t = S.tools;
    const row = (k, v) => `<div><span class="dim">${k}</span><span class="mono">${esc(v || "—")}</span></div>`;
    $("#toolchain").innerHTML = row("Project Gradle", a ? (a.gradlew ? a.gradle : "no wrapper") : "") + row("Android SDK", t.sdk) +
      row("SDK platforms", S.sdk.platforms.map((x) => "API " + x).join(", ")) + row("JDK", t.java) +
      row("New apps use", `AGP ${S.sdk.agp} · Gradle ${S.sdk.gradleVersion} · Kotlin ${S.sdk.kotlin}`) + row("adb", t.adb);
  } catch {}
}
function openNewApp() {
  $("#naName").value = ""; $("#naPkg").value = ""; delete $("#naPkg").dataset.edited;
  $("#naParent").value = "~/AndroidStudioProjects";
  $("#dlgNewApp").showModal(); $("#naName").focus();
}
const appId = () => project()?.flutter ? project().flutter.applicationId : project()?.android?.modules?.find((m) => m.name === $("#moduleSel").value)?.applicationId;
// the adb serial for the screen mirror, input and logcat (a Flutter device only has one when it's an Android device)
const serial = () => isFlutter() ? (isAndroidDev(fdev()) ? fdev().id : null) : $("#deviceSel").value || null;
const FL_KIND = { android: "Android", web: "Web", linux: "Desktop", darwin: "macOS", windows: "Desktop", ios: "iOS" };
async function refreshDevices() {
  const fl = isFlutter() && S.tools.flutter;
  const sel = $("#deviceSel");
  if (fl && !S.fdevs.length) { sel.innerHTML = ""; sel.append(new Option("Looking for devices…", "")); }
  const [d, fd] = await Promise.all([api("/api/android/devices"), fl ? api("/api/flutter/devices") : null]);
  if (fd) S.fdevs = fd.devices;
  const prev = sel.value || store.get(fl ? "fs:fdev" : "fs:serial", "");
  sel.innerHTML = "";
  const ready = d.devices.filter((x) => x.state === "device").map((x) => x.serial);
  if (fl) {
    for (const x of S.fdevs) {
      const kind = x.emulator ? "Emulator" : FL_KIND[x.platform.split("-")[0]] || x.platform;
      sel.append(new Option(`${kind} · ${x.name}  (${x.sdk || x.id})`, x.id));
    }
    const ids = S.fdevs.map((x) => x.id);
    sel.value = ids.includes(prev) ? prev : S.fdevs.find(isAndroidDev)?.id || S.fdevs.find((x) => x.id !== "web-server")?.id || ids[0] || "";
    if (fd.error) toast(fd.error, true);
  } else {
    if (!d.devices.length) sel.append(new Option("No device — plug in USB (debugging on) or connect over Wi-Fi", ""));
    for (const x of d.devices) sel.append(new Option(`${x.emulator ? "Emulator" : x.wifi ? "Phone (Wi-Fi)" : "Phone (USB)"} · ${x.model || x.serial}  (${x.state === "device" ? x.serial : x.state})`, x.serial));
    if (ready.includes(prev)) sel.value = prev; else if (ready.length) sel.value = ready[0];
  }
  const bad = ready.length ? null : d.devices.find((x) => x.state !== "device");
  $("#devHint").hidden = !bad;
  if (bad?.state === "no permissions") $("#devHint").innerHTML = `Linux is blocking USB access to <b>${esc(bad.serial)}</b>. Run once in a terminal, then replug the phone:<pre>sudo apt install android-sdk-platform-tools-common\nsudo usermod -aG plugdev $USER   # then log out and back in</pre>Or connect over Wi-Fi below.`;
  else if (bad?.state === "unauthorized") $("#devHint").textContent = "Unlock the phone and tap “Allow USB debugging”, then press refresh.";
  else if (bad) $("#devHint").textContent = `Device ${bad.serial} is ${bad.state}.`;
  const avd = $("#avdSel");
  avd.innerHTML = "";
  d.avds.forEach((n) => avd.append(new Option(n, n)));
  $("#avdRow").hidden = !d.avds.length;
  $("#btnScrcpy").hidden = !S.tools.scrcpy;
  renderFlutterRun(); renderStatusBar();
  if (d.error && !isFlutter()) toast(d.error, true);
}
async function gradleTask(task) {
  $("#andLog").textContent = ""; $("#btnFix").hidden = true; setDroidView("build");
  await api("/api/android/gradle", { project: S.cur, task, serial: serial() });
}

// logcat viewer
const LC = { proc: null, lines: [], paused: false, crash: null };
const LVL = { V: 0, D: 1, I: 2, W: 3, E: 4, F: 5, A: 5 };
const LC_RE = /^(\d\d-\d\d)\s+(\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEFA])\s+(.*?)\s*: ?(.*)$/;
// `flutter run` output: print()s, status lines and framework error blocks (═══ Exception caught by … ═══)
const FL_ERR_START = /^═+╡? ?(EXCEPTION CAUGHT BY|Exception caught by)/i, FL_ERR_END = /^═{8,}\s*$/;
function flParse(raw) {
  const start = FL_ERR_START.test(raw), inErr = LC.flErr || start;
  if (start) LC.flErr = true; else if (LC.flErr && FL_ERR_END.test(raw)) LC.flErr = false;
  const lvl = inErr || /^(✗|E\/|\[ERROR|Error: |Exception|Unhandled exception|FAILURE:)|^lib\/.*:\d+:\d+: Error/.test(raw) ? "E"
    : /^(W\/|Warning: )|: Warning: /.test(raw) ? "W" : "I";
  return { raw, time: new Date().toTimeString().slice(0, 8), lvl, tag: "", msg: raw, flStart: start };
}
function lcParse(raw) {
  if (LC.flutter) return flParse(raw);
  const m = raw.match(LC_RE);
  if (!m) return { raw, time: "", lvl: "I", tag: "", msg: raw };
  return { raw, time: m[2], pid: m[3], lvl: m[5], tag: m[6], msg: m[7] };
}
function lcVisible(l) {
  if (LVL[l.lvl] < LVL[$("#lcLevel").value]) return false;
  const q = $("#lcSearch").value.trim().toLowerCase();
  return !q || l.raw.toLowerCase().includes(q);
}
function lcRow(l) {
  const d = document.createElement("div");
  d.className = "lc lv-" + l.lvl;
  d.innerHTML = `<span class="t">${esc(l.time)}</span><span class="lv">${esc(l.lvl)}</span><span class="tag">${esc(l.tag)}</span><span class="m">${esc(l.msg)}</span>`;
  return d;
}
function lcRender() {
  const box = $("#logcat");
  box.innerHTML = "";
  const vis = LC.lines.filter(lcVisible).slice(-2000);
  if (!vis.length) box.innerHTML = `<div class="dim lc-empty">${LC.proc ? "Waiting for log lines…" : LC.flutter ? "Press <b>Run</b>: the app's output (print, errors, hot reload) shows here."
    : "Press <b>Run app</b> (logcat starts automatically) or <b>Start logcat</b>."}</div>`;
  const frag = document.createDocumentFragment();
  vis.forEach((l) => frag.append(lcRow(l)));
  box.append(frag);
  box.scrollTop = box.scrollHeight;
}
function lcAdd(raw, bulk) {
  const l = lcParse(raw);
  LC.lines.push(l);
  if (LC.lines.length > 6000) LC.lines.splice(0, 1000);
  if (LC.flutter) {  // a framework error: collect the whole block for "Ask to fix"
    if (l.flStart) {
      LC.crash = { t: Date.now(), lines: [], fl: true };
      if (!bulk) {
        alertUser("crash", "Flutter error", project()?.name || "", () => { setMode("android"); setDroidView("run"); });
        $("#crashText").textContent = "Your app threw an error."; $("#crashBar").hidden = false;
      }
    }
    if (LC.crash?.fl && LC.crash.lines.length < 150 && (l.lvl === "E" || LC.flErr)) LC.crash.lines.push(l.raw);
  } else if (/FATAL EXCEPTION|ANR in /.test(l.msg) || (l.tag === "AndroidRuntime" && l.lvl === "E" && !LC.crash)) {
    if (!LC.crash || Date.now() - LC.crash.t > 3000) LC.crash = { t: Date.now(), lines: [] };
  }
  if (LC.crash && !LC.flutter && LC.crash.lines.length < 80 && Date.now() - LC.crash.t < 3000 && (l.lvl === "E" || l.lvl === "F")) {
    LC.crash.lines.push(l.raw);
    if (!bulk && LC.crash.lines.length === 1) alertUser("crash", /ANR/.test(l.msg) ? "App not responding" : "App crashed", appId() || project()?.name || "", () => { setMode("android"); setDroidView("run"); });
    if (!bulk) { $("#crashText").textContent = /ANR/.test(LC.crash.lines.join("\n")) ? "The app stopped responding (ANR)." : "The app crashed."; $("#crashBar").hidden = false; }
  }
  if (bulk || !lcVisible(l)) return;
  const box = $("#logcat");
  box.querySelector(".lc-empty")?.remove();
  box.append(lcRow(l));
  while (box.childElementCount > 2000) box.firstChild.remove();
  if (!LC.paused) box.scrollTop = box.scrollHeight;
}
function lcSetRunning(on) { $("#btnLogcat").innerHTML = on ? ic("stop") + " <span>Stop logcat</span>" : ic("play") + " <span>Start logcat</span>"; }
function attachLogcat(id, isRunning) { LC.proc = id; LC.lines = []; lcSetRunning(isRunning); lcRender(); }
const logcatText = (lines) => lines.map((l) => l.raw).join("\n");

// device screen mirror: a live H.264 stream (scrcpy's on-device encoder, decoded here with WebCodecs) at up to
// 120 fps; plain screenshots are the fallback when scrcpy or WebCodecs isn't available
const M = { on: false, t: 0, frames: 0, abort: null, dec: null, live: false, dev: null };
const canStream = () => S.tools.stream && "VideoDecoder" in window;
function mirrorTick(kind) {
  M.frames++;
  const dt = performance.now() - M.t;
  if (dt > 1000) { $("#mirrorStat").textContent = `${Math.round(M.frames / (dt / 1000))} fps · ${kind}`; M.t = performance.now(); M.frames = 0; }
}
async function mirrorLoop() {
  if (canStream()) {
    try { return await streamLoop(); }
    catch (e) { if (!M.on || e.name === "AbortError") return; toast(`Live stream unavailable (${e.message}) — falling back to screenshots`, true); }
  }
  shotLoop();
}
async function shotLoop() {
  M.live = false; $("#mirrorImg").hidden = false; $("#mirrorCanvas").hidden = true;
  $("#mirrorNote").textContent = "Screenshot view (refreshes every 1–3 s). Install scrcpy for a smooth live stream.";
  while (M.on) {
    const t0 = performance.now();
    try {
      const blob = await api("/api/android/screenshot?serial=" + encodeURIComponent(serial() || ""));
      const img = $("#mirrorImg"), old = img.src;
      img.src = URL.createObjectURL(blob);
      if (old) URL.revokeObjectURL(old);
      mirrorTick("screenshots");
    } catch (e) { if (!(e instanceof LoginNeeded)) toast(e.message, true); stopMirror(); break; }
    await new Promise((r) => setTimeout(r, Math.max(0, 150 - (performance.now() - t0))));
  }
}
// "avc1.PPCCLL" from the SPS in scrcpy's config packet
function avcCodec(data) {
  for (let i = 0; i + 7 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 0x1f) === 7) {
      return "avc1." + [data[i + 4], data[i + 5], data[i + 6]].map((b) => b.toString(16).padStart(2, "0")).join("");
    }
  }
  return null;
}
async function streamLoop() {
  M.abort = new AbortController();
  const r = await fetch(`/api/android/stream?serial=${encodeURIComponent(serial() || "")}&fps=120`,
    { headers: { "X-Token": TOKEN, "X-Login": LOGIN }, signal: M.abort.signal });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  M.dev = (r.headers.get("X-Device-Size") || "").split("x").map(Number).filter(Boolean);
  const canvas = $("#mirrorCanvas"), ctx = canvas.getContext("2d");
  let pending = null, raf = 0;
  const draw = () => {
    raf = 0;
    const f = pending; pending = null;
    if (!f) return;
    if (canvas.width !== f.displayWidth || canvas.height !== f.displayHeight) { canvas.width = f.displayWidth; canvas.height = f.displayHeight; }
    ctx.drawImage(f, 0, 0);
    f.close();
  };
  M.dec = new VideoDecoder({
    output: (f) => { pending?.close(); pending = f; mirrorTick("live H.264"); if (!raf) raf = requestAnimationFrame(draw); },
    error: (e) => { if (M.on) toast("Video decoder: " + e.message, true); },
  });
  M.live = true; canvas.hidden = false; $("#mirrorImg").hidden = true;
  $("#mirrorNote").textContent = "Live stream from the phone's video encoder, up to 120 fps.";
  // scrcpy stream: 4-byte codec id, then 12-byte headers. Top bit = session packet (new video size);
  // otherwise bit 62 = codec config (SPS/PPS), bit 61 = keyframe, low bits = pts (µs), then a 4-byte payload size.
  const reader = r.body.getReader();
  let buf = new Uint8Array(0);
  const need = async (n) => {
    while (buf.length < n) {
      const { value, done } = await reader.read();
      if (done) throw new Error("the stream ended");
      const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
    }
  };
  const take = (n) => { const out = buf.subarray(0, n); buf = buf.subarray(n); return out; };
  await need(4); take(4);
  let config = null;
  while (M.on) {
    await need(12);
    const h = take(12), dv = new DataView(h.buffer, h.byteOffset, 12);
    if (h[0] & 0x80) continue;
    const hi = dv.getUint32(0), size = dv.getUint32(8);
    await need(size);
    const data = take(size);
    if (hi & 0x40000000) {
      config = data.slice();
      const codec = avcCodec(config);
      if (codec) M.dec.configure({ codec, optimizeForLatency: true });
      continue;
    }
    if (M.dec.state !== "configured") continue;
    const key = !!(hi & 0x20000000);
    let chunk = data;
    if (key && config) { chunk = new Uint8Array(config.length + data.length); chunk.set(config); chunk.set(data, config.length); }
    M.dec.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: (hi & 0x1fffffff) * 4294967296 + dv.getUint32(4), data: chunk }));
  }
}
function stopMirror() {
  M.on = false;
  M.abort?.abort(); M.abort = null;
  if (M.dec && M.dec.state !== "closed") M.dec.close();
  M.dec = null;
  $("#btnMirror").innerHTML = ic("cast") + " Show screen";
  $("#mirrorWrap").hidden = $("#typeRow").hidden = true;
  $("#mirrorStat").textContent = "";
}
const input = (body) => api("/api/android/input", { serial: serial(), ...body }).catch((e) => toast(e.message, true));
function devicePoint(e) {
  const el = M.live ? $("#mirrorCanvas") : $("#mirrorImg"), r = el.getBoundingClientRect();
  let w = M.live ? el.width : el.naturalWidth, h = M.live ? el.height : el.naturalHeight;
  // the stream is scaled down; taps go to adb in real screen pixels (swap for landscape)
  if (M.live && M.dev?.length === 2) [w, h] = (w > h) === (M.dev[0] > M.dev[1]) ? M.dev : [M.dev[1], M.dev[0]];
  return { x: ((e.clientX - r.left) / r.width) * w, y: ((e.clientY - r.top) / r.height) * h };
}

// ============================================================ processes
function renderProcs() {
  const ul = $("#procList");
  ul.innerHTML = "";
  if (!S.procs.length) ul.innerHTML = '<li class="dim">No processes yet. Dev servers, Gradle builds, logcat and emulators show up here.</li>';
  for (const pr of [...S.procs].reverse()) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="dot ${pr.running ? "on" : ""}"></span><span class="plabel">${esc(pr.label)}</span><span class="dim">${esc((pr.project || "").split("/").pop())}</span>${pr.running ? '<button class="btn small danger">Stop</button>' : ""}`;
    li.onclick = act(() => { S.viewProc = pr.id; return loadLog(pr.id, $("#procLog")); });
    li.querySelector("button")?.addEventListener("click", act(async (e) => { e.stopPropagation(); await api("/api/proc/kill", { id: pr.id }); }));
    ul.append(li);
  }
}

// ============================================================ events
function connect() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}&login=${encodeURIComponent(LOGIN)}`);
  es.onopen = () => { $("#conn").classList.add("on"); $("#conn").title = "Connected"; };
  es.onerror = () => { $("#conn").classList.remove("on"); $("#conn").title = "Disconnected — is the server running?"; };
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    switch (ev.type) {
      case "chat": onChat(ev.chat, ev.project, ev.event); break;
      case "chats_changed": if (!$("#historyPanel").hidden && ev.project === S.cur) act(showHistory)(); break;
      case "proc_start":
        S.procs.push({ id: ev.proc, kind: ev.kind, project: ev.project, label: ev.label, running: true, meta: ev.meta });
        S.procLogs[ev.proc] = [];
        if (ev.project === S.cur && BUILD_KINDS.has(ev.kind)) {
          S.consoleProc = ev.proc; $("#andLog").textContent = ""; $("#btnFix").hidden = true;
          $("#consoleTitle").textContent = ev.label; $("#btnConsoleStop").hidden = false;
        }
        if (ev.project === S.cur && ev.kind === (LC.flutter ? "flutter" : "logcat")) { attachLogcat(ev.proc, true); $("#crashBar").hidden = true; LC.crash = null; LC.flErr = false; }
        if (ev.project === S.cur && ev.kind === "preview") $("#prevLog").textContent = "";
        if (ev.project === S.cur && BUILD_KINDS.has(ev.kind) && $('[data-dv="build"]').hidden) $("#buildDot").hidden = false;
        if (ev.project === S.cur && ["logcat", "flutter"].includes(ev.kind) && S.mode === "android") setDroidView("run");
        if (ev.project === S.cur && ev.kind === "flutter") renderFlutterRun();
        renderProcs(); renderStatusBar(); break;
      case "log":
        if (ev.kind === "setup") {
          const pr = S.procs.find((x) => x.id === ev.proc), row = pr && $(`.setup-row[data-item="${pr.meta?.setup}"] .setup-progress`);
          if (row) { row.hidden = false; row.textContent = ev.line.trim(); }
        }
        if (ev.kind !== "logcat") {
          (S.procLogs[ev.proc] ??= []).push(ev.line);
          if (S.procLogs[ev.proc].length > 4000) S.procLogs[ev.proc].shift();
        }
        if (ev.project === S.cur && (ev.kind === "preview" || (ev.kind === "flutter" && S.previews[ev.project]?.flutter))) appendLog($("#prevLog"), ev.line);
        if (ev.proc === S.consoleProc) appendLog($("#andLog"), ev.line);
        if (ev.proc === LC.proc) lcAdd(ev.line);
        if (ev.proc === S.viewProc) appendLog($("#procLog"), ev.line);
        break;
      case "proc_exit": {
        const pr = S.procs.find((x) => x.id === ev.proc); if (pr) pr.running = false;
        if (ev.proc === S.consoleProc) {
          $("#btnConsoleStop").hidden = true;
          appendLog($("#andLog"), ev.code === 0 ? "── finished ──" : `── failed (exit ${ev.code}) ──`);
          if (ev.code !== 0) { S.lastGradle = { proc: ev.proc, label: ev.label }; $("#btnFix").hidden = false; }
        }
        if (ev.proc === LC.proc) lcSetRunning(false);
        if (ev.kind === "flutter") {
          delete S.flutter[ev.project];
          if (S.previews[ev.project]?.flutter) {
            delete S.previews[ev.project];
            if (ev.project === S.cur) { $("#btnPrevStart").hidden = false; $("#btnPrevStop").hidden = true; }
            renderProjects();
          }
          if (ev.project === S.cur) {
            renderFlutterRun();
            if (ev.code > 0 && ev.proc === LC.proc) {  // build or launch failed: offer the same fix flow as a crash
              LC.crash = { t: Date.now(), lines: (S.procLogs[ev.proc] || []).slice(-150), fl: true, run: true };
              $("#crashText").textContent = "flutter run failed."; $("#crashBar").hidden = false;
            }
          }
        }
        if (ev.kind === "preview" && S.previews[ev.project]) {
          delete S.previews[ev.project];
          if (ev.project === S.cur) { $("#btnPrevStart").hidden = false; $("#btnPrevStop").hidden = true; $("#prevLogBox").open = true; }
          renderProjects();
        }
        renderStatusBar();
        if (BUILD_KINDS.has(ev.kind)) alertUser("build", `${ev.label} ${ev.code === 0 ? "finished" : "failed"}`, ev.project.split("/").pop(),
          () => act(async () => { if (S.cur !== ev.project) await selectProject(ev.project); setMode("android"); setDroidView("build"); })(), ev.code !== 0);
        if (BUILD_KINDS.has(ev.kind) && ev.project !== S.cur) toast(`${ev.label} ${ev.code === 0 ? "finished" : "failed"} in ${ev.project.split("/").pop()}`, ev.code !== 0);
        renderProcs(); break;
      }
      case "preview_url":
        S.previews[ev.project] = { url: ev.url, flutter: ev.flutter };
        if (S.profile) store.set(pkey("url:" + ev.project), ev.url);
        if (ev.project === S.cur) { setUrl(ev.url, true); $("#btnPrevStart").hidden = true; $("#btnPrevStop").hidden = false; }
        renderProjects(); break;
      case "reload":
        if (ev.project === S.cur && $("#autoReload").checked && $("#iframe").src) {
          clearTimeout(S.reloadT); S.reloadT = setTimeout(() => { try { $("#iframe").contentWindow.location.reload(); } catch { $("#iframe").src = $("#iframe").src; } }, 150);
        }
        break;
      case "flutter_state":
        if (ev.running) S.flutter[ev.project] = { ...(S.flutter[ev.project] || {}), started: true, ...(ev.device ? { device: ev.device } : {}) };
        if (ev.event === "started" && ev.project === S.cur) toast(ev.device === "web-server" ? "App running — open Agent mode → Preview to use it" : "App running — hot reload with ⚡ or by saving");
        if (ev.event === "reloaded" && ev.project === S.cur) { S.flMsg = ev.text; clearTimeout(S.flMsgT); S.flMsgT = setTimeout(() => { S.flMsg = null; renderStatusBar(); }, 2500); }
        if (ev.event === "error" && ev.project === S.cur) toast(ev.text + " — see the app output", true);
        if (ev.project === S.cur) { renderFlutterRun(); renderStatusBar(); }
        break;
      case "toast": toast(ev.text, ev.err); break;
      case "setup_done":
        toast(ev.text, !ev.ok);
        act(async () => { await loadSetup(); await loadState(); if (S.cur) setupAndroid(); if (S.mode === "android") { loadToolchain(); await refreshDevices(); } })();
        break;
      case "projects_changed":
        if (ev.text) toast(ev.text);
        act(async () => {
          await loadState();
          if (ev.select) {
            const newApp = S.pendingSelect === ev.select;
            await selectProject(ev.select);
            if (newApp) { S.pendingSelect = null; setMode("android"); }
          } else setupAndroid();
        })();
        break;
    }
  };
}

// ============================================================ modes & layout
// Three workspaces share one agent chat:
//   Agent   — projects + tasks | chat | Preview / Processes
//   Editor  — explorer / search / git + code editor | agent docked on the right
//   Android — run bar, logcat, Gradle, devices       | agent docked on the right
const MODES = ["agent", "editor", "android"];
const activeTab = () => document.querySelector(".tabs .active")?.dataset.tab || "preview";
function showTab(name) {
  if (!["preview", "procs"].includes(name)) name = "preview";
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel .tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
  store.set("fs:tab", name);
  if (name === "procs") renderProcs();
}
$$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

function setMode(mode, save = true) {
  if (!MODES.includes(mode)) mode = "agent";
  const prev = S.mode;
  S.mode = mode;
  document.body.dataset.mode = mode;
  $$("#modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#tab-code").hidden = mode !== "editor";
  $("#droidPane").hidden = mode !== "android";
  $("#layout").classList.toggle("no-sidebar", !sideOpen());
  applyWidths();
  if (save) store.set("fs:mode", mode);
  if (mode !== "android") store.set("fs:lastMode", mode);
  if (mode === "editor" && typeof codeTabShown === "function") codeTabShown();
  if (mode === "android" && prev !== "android") { act(refreshDevices)(); loadToolchain(); act(loadSetup)(); }
  if (prev === "android" && mode !== "android" && M.on) stopMirror();  // don't keep streaming video in the background
  if (prev !== mode) renderChat();
  renderStatusBar();
}
// compatibility for callers that used to toggle the old Code pane
const showCode = (on = true) => setMode(on ? "editor" : "agent");
$$("#modes button").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && ["1", "2", "3"].includes(e.key)) {
    e.preventDefault();
    if (!$(`#modes [data-mode="${MODES[+e.key - 1]}"]`).hidden) setMode(MODES[+e.key - 1]);
  }
});

// the projects sidebar is open by default in Agent mode and tucked away in the IDE modes
const sideOpen = () => store.get("fs:side:" + (S.mode || "agent"), S.mode === "agent" || !S.mode);
$("#btnSidebar").onclick = () => {
  const l = $("#layout");
  if (innerWidth < 900) return l.classList.toggle("show-sidebar");
  store.set("fs:side:" + S.mode, !sideOpen());
  l.classList.toggle("no-sidebar", !sideOpen());
};
// Agent mode: the chat-header button hides the Preview panel. Editor / Android: it (and the title-bar button) hides the docked agent chat.
function toggleRight() {
  const l = $("#layout"), cls = S.mode === "agent" ? "no-panel" : "no-agent";
  l.classList.toggle(cls);
  store.set(cls === "no-panel" ? "fs:nopanel" : "fs:noagent", l.classList.contains(cls));
  $("#btnAgent").classList.toggle("on", !l.classList.contains("no-agent"));
}
$("#btnTogglePanel").onclick = toggleRight;
$("#btnAgent").onclick = toggleRight;
if (store.get("fs:nopanel", false)) $("#layout").classList.add("no-panel");
if (store.get("fs:noagent", false)) $("#layout").classList.add("no-agent");
$("#btnAgent").classList.toggle("on", !$("#layout").classList.contains("no-agent"));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "j" && S.mode !== "agent") { e.preventDefault(); toggleRight(); }
});

// one divider: in Agent mode it sizes the Preview panel, in the IDE modes the docked agent chat (both sit right of it)
function applyWidths() {
  const docked = S.mode !== "agent";
  $("#chatPane").style.width = docked ? store.get("fs:dockw", 400) + "px" : "";
  $("#panel").style.width = docked ? "" : (store.get("fs:panelw", null) ? store.get("fs:panelw") + "px" : "");
}
(() => {
  const r = $("#resizer");
  r.onpointerdown = (e) => {
    r.setPointerCapture(e.pointerId); r.classList.add("drag");
    $$("iframe").forEach((f) => (f.style.pointerEvents = "none"));
    const docked = S.mode !== "agent", el = docked ? $("#chatPane") : $("#panel");
    const right = el.getBoundingClientRect().right, min = docked ? 320 : 340;
    const max = right - $(".work").getBoundingClientRect().left - 360;
    r.onpointermove = (m) => { el.style.width = Math.min(max, Math.max(min, right - m.clientX)) + "px"; };
    r.onpointerup = () => {
      r.onpointermove = null; r.classList.remove("drag");
      $$("iframe").forEach((f) => (f.style.pointerEvents = ""));
      store.set(docked ? "fs:dockw" : "fs:panelw", parseInt(el.style.width));
    };
  };
})();

// ---- project switcher (title bar)
$("#btnProj").onclick = (e) => {
  e.stopPropagation();
  const r = $("#btnProj").getBoundingClientRect();
  const items = S.projects.slice(0, 14).map((p) => ({
    label: p.name + (p.path === S.cur ? "  ✓" : ""), icon: p.flutter ? "zap" : p.android ? "phone" : p.web?.length ? "globe" : "folder",
    fn: act(() => selectProject(p.path)),
  }));
  showMenu(r.left, r.bottom + 4, [...items, ...(items.length ? ["sep"] : []),
    { label: "Open folder…", icon: "folder", fn: () => $("#btnAdd").click() },
    { label: "Clone repository…", icon: "clone", fn: () => openProjectDialog("clone") },
    { label: "New project…", icon: "folder-plus", fn: () => openProjectDialog("new") },
    { label: "New Android app…", icon: "phone", fn: openNewApp },
    { label: "New Flutter app…", icon: "zap", fn: openNewFlutter }]);
};

// ---- tasks: agent runs in progress across projects, and the ones that finished this session
function renderTasks() {
  const ul = $("#taskList");
  if (!ul) return;
  const running = Object.keys(S.running).filter((cid) => S.meta[cid]);
  const done = (S.done || []).filter((d) => !S.running[d.cid] && S.meta[d.cid]).slice(0, 5);
  ul.innerHTML = !running.length && !done.length ? '<li class="dim empty-task">Agent tasks you start show up here.</li>' : "";
  const row = (cid, state) => {
    const m = S.meta[cid], li = document.createElement("li");
    li.className = "task " + state;
    li.innerHTML = `<span class="task-ic"></span><span class="task-main"><span class="task-title">${esc(m.title)}</span>
      <span class="task-sub">${state === "run" ? "Working" : "Ready"} · ${esc((m.project || "").split("/").pop())}</span></span>`;
    li.onclick = act(async () => { if (S.cur !== m.project) await selectProject(m.project); await openChat(cid); });
    ul.append(li);
  };
  running.forEach((cid) => row(cid, "run"));
  done.forEach((d) => row(d.cid, "ok"));
}
$("#btnSideNew").onclick = act(async () => { if (!S.cur) return $("#btnAdd").click(); await openChat(null); $("#prompt").focus(); });

// ---- agent home (empty chat in Agent mode): greeting, centred composer and suggestions
function homeChips() {
  const p = project();
  if (p?.flutter) return ["Add a settings screen with a dark mode switch", "Fix the problems flutter analyze reports", "Write widget tests for the home screen", "Explain how this app is structured"];
  if (p?.android) return ["Add a settings screen to my app", "Find what's slowing down app startup", "Write unit tests for the main screen", "Explain how this app is structured"];
  if (p?.web?.length) return ["Add a dark mode toggle", "Make the layout responsive on phones", "Find and fix accessibility issues", "Explain how this site is structured"];
  return ["Explain how this project is structured", "Find and fix bugs", "Write tests for the main module", "Review my uncommitted changes"];
}
function renderHome(box) {
  const first = (S.profile?.name || "").split(" ")[0];
  box.innerHTML = `<div class="home-hero"><img src="icon.svg" alt=""><h1>Hello${first ? " " + esc(first) : ""}, welcome back!</h1>
    <h2>What should we build in <b>${esc(project()?.name)}</b>?</h2></div>`;
  const chips = $("#homeChips");
  chips.innerHTML = homeChips().map((t) => `<button class="chip">${ic("bot")}<span>${esc(t)}</span></button>`).join("");
  chips.querySelectorAll(".chip").forEach((c) => (c.onclick = () => { $("#prompt").value = c.textContent.trim(); $("#prompt").focus(); }));
}

// ---- status bar
function renderStatusBar() {
  const bar = $("#statusbar");
  if (!bar) return;
  const g = typeof C !== "undefined" && S.cur ? C.git : null, p = project();
  const busy = S.cur && Object.entries(S.running).some(([, proj]) => proj === S.cur);
  const left = [];
  if (g?.repo) left.push(`<button class="sb" data-sb="git">${ic("branch")}${esc(g.branch || "HEAD")}${g.files?.length ? "*" : ""}${g.ahead || g.behind ? ` <span class="dim">${g.behind ? "↓" + g.behind : ""}${g.ahead ? "↑" + g.ahead : ""}</span>` : ""}</button>`);
  if (p) left.push(`<button class="sb" data-sb="proj">${ic("folder")}${esc(p.name)}</button>`);
  left.push(`<span class="sb ${busy ? "busy" : ""}">${ic("bot")}${busy ? "Agent working…" : "Agent ready"}</span>`);
  const right = [];
  if (S.mode === "android") {
    const dev = $("#deviceSel")?.value && $("#deviceSel").selectedOptions[0]?.textContent.split("  (")[0];
    const g2 = p && S.procs.find((x) => BUILD_KINDS.has(x.kind) && x.project === p.path && x.running);
    if (g2) right.push(`<span class="sb busy">${ic("package")}${esc(g2.label)}</span>`);
    if (p?.android?.gradle) right.push(`<span class="sb">Gradle ${esc(p.android.gradle)}</span>`);
    if (p?.flutter) {
      if (S.flMsg) right.push(`<span class="sb ok">${ic("zap")}${esc(S.flMsg)}</span>`);
      else if (flRun()) right.push(`<span class="sb ${S.flutter[p.path]?.started ? "ok" : "busy"}">${ic("zap")}${S.flutter[p.path]?.started ? "App running" : "Launching…"}</span>`);
      if (S.tools.flutter?.version) right.push(`<span class="sb">Flutter ${esc(S.tools.flutter.version)}</span>`);
    }
    right.push(`<span class="sb">${ic("phone")}${esc(dev || "No device")}</span>`);
  }
  const live = p && S.previews[p.path];
  if (live) right.push(`<button class="sb ok" data-sb="preview">${ic("globe")}Preview live</button>`);
  right.push(`<span class="sb">${{ agent: "Agent", editor: "Editor", android: p?.flutter ? "Flutter" : "Android" }[S.mode || "agent"]} mode</span>`);
  bar.innerHTML = `${left.join("")}<div class="spacer"></div>${right.join("")}`;
  bar.querySelector('[data-sb="git"]')?.addEventListener("click", () => { setMode("editor"); setView("git"); });
  bar.querySelector('[data-sb="proj"]')?.addEventListener("click", (e) => $("#btnProj").onclick(e));
  bar.querySelector('[data-sb="preview"]')?.addEventListener("click", () => { setMode("agent"); showTab("preview"); });
}

// ============================================================ wiring: chat
$("#composer").onsubmit = (e) => { e.preventDefault(); act(send)(); };
$("#prompt").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); act(send)(); } };
$("#prompt").oninput = () => { const t = $("#prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, innerHeight * 0.4) + "px"; };
$("#prompt").onpaste = (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); act(addFiles)(files); }
};
$("#btnAttach").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = () => { act(addFiles)([...$("#fileInput").files]); $("#fileInput").value = ""; };
const comp = $("#composer");
let dragDepth = 0;
$("#chatPane").addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); dragDepth++; comp.classList.add("drag"); } });
$("#chatPane").addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
$("#chatPane").addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; comp.classList.remove("drag"); } });
$("#chatPane").addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; comp.classList.remove("drag"); if (e.dataTransfer.files.length) act(addFiles)([...e.dataTransfer.files]); });
$("#btnStop").onclick = act(() => api("/api/chat/stop", { chat: chatId() }));
$("#btnNew").onclick = act(() => openChat(null));
$("#btnHistory").onclick = act(() => ($("#historyPanel").hidden ? showHistory() : ($("#historyPanel").hidden = true)));
$("#agent").onchange = () => { if (chatId() && S.meta[chatId()]?.agent && S.meta[chatId()].agent !== $("#agent").value) { S.curChat[S.cur] = null; renderChat(); toast("Started a new chat for this agent"); } agentChanged(); };
$("#model").onchange = modelChanged;
$("#customModel").onchange = () => store.set(pkey("customModel"), $("#customModel").value.trim());
$("#mode").onchange = () => { if ($("#mode").value === "bypassPermissions") toast("Full access: the agent will run commands and change files without asking."); };
$("#projFilter").oninput = renderProjects;

// preview
$("#webRoot").onchange = () => { const r = project()?.web?.find((x) => x.dir === $("#webRoot").value); $("#devCmd").value = r?.command || ""; };
$("#btnPrevStart").onclick = act(async () => {
  if (isFlutter()) {  // Flutter web: `flutter run -d web-server`, with hot reload / restart from Flutter mode
    if (!project().flutter.platforms.includes("web")) throw new Error("This Flutter app has no web platform yet — add it in Flutter mode → Flutter tab → Platforms");
    if (flRun()) throw new Error("The app is already running — stop it in Flutter mode first");
    $("#prevLog").textContent = "";
    await api("/api/flutter/run", { project: S.cur, device: "web-server", mode: "debug", auto: $("#flAuto").checked });
    S.previews[S.cur] = { url: null, flutter: true };
    $("#btnPrevStart").hidden = true; $("#btnPrevStop").hidden = false; $("#prevLogBox").open = true;
    return toast("Building the Flutter web app… the first build takes a minute");
  }
  const dir = $("#webRoot").value, command = $("#devCmd").value.trim();
  store.set(pkey("prev:" + S.cur), { dir, command });
  $("#prevLog").textContent = "";
  const r = await api("/api/preview/start", { project: S.cur, dir, command });
  S.previews[S.cur] = { url: r.url };
  $("#btnPrevStart").hidden = true; $("#btnPrevStop").hidden = false;
  if (r.url) setUrl(r.url, true); else { toast("Starting the dev server (the first run installs packages)…"); $("#prevLogBox").open = true; }
  renderProjects();
});
$("#btnPrevStop").onclick = act(async () => {
  await api(S.previews[S.cur]?.flutter ? "/api/flutter/stop" : "/api/preview/stop", { project: S.cur });
  delete S.previews[S.cur];
  $("#btnPrevStart").hidden = false; $("#btnPrevStop").hidden = true; renderProjects();
});
const go = () => {
  let u = $("#prevUrl").value.trim(); if (!u) return;
  if (!/^https?:\/\//.test(u)) u = "http://" + u;
  if (S.cur) store.set(pkey("url:" + S.cur), u);
  setUrl(u, true);
};
$("#prevUrl").onkeydown = (e) => { if (e.key === "Enter") go(); };
$("#btnReload").onclick = () => { const f = $("#iframe"); if (f.src) f.src = f.src; else go(); };
$("#device").onchange = () => { S.device = $("#device").value; store.set("fs:device", S.device); applyDevice(); };
$("#btnRotate").onclick = () => { S.rotated = !S.rotated; applyDevice(); };
$("#btnExt").onclick = () => { const u = $("#prevUrl").value; if (u) open(u, "_blank"); };
$("#autoReload").checked = store.get("fs:live", true);
$("#autoReload").onchange = () => store.set("fs:live", $("#autoReload").checked);

// android
$("#deviceSel").onchange = () => {
  store.set(isFlutter() ? "fs:fdev" : "fs:serial", $("#deviceSel").value);
  if (M.on && !serial()) stopMirror();
  renderFlutterRun(); renderStatusBar();
};
$("#btnDevRefresh").onclick = act(refreshDevices);
$("#btnAvd").onclick = act(async () => { await api("/api/android/emulator", { avd: $("#avdSel").value, project: S.cur }); toast("Emulator starting — press refresh in a moment"); setTimeout(act(refreshDevices), 15000); });
$("#btnWifi").onclick = act(async () => {
  const r = await api("/api/android/connect", { address: $("#wifiAddr").value, code: $("#wifiCode").value });
  toast(r.output || "done"); $("#wifiCode").value = ""; await refreshDevices();
});
$("#btnReverse").onclick = act(async () => { const r = await api("/api/android/reverse", { port: $("#revPort").value, serial: serial() }); toast(`Device localhost:${$("#revPort").value} → this computer (${r.output})`); });
$("#btnStudio").onclick = act(async () => { await api("/api/android/studio", { project: S.cur }); toast("Opening in Android Studio…"); });
const needDevice = () => {
  if (serial()) return;
  throw new Error(isFlutter() ? "Pick an Android device for this (phone or emulator)" : "Connect your phone (USB or Wi-Fi) or launch an emulator first");
};
$("#btnRun").onclick = act(async () => {
  if (isFlutter()) return flutterRun();
  needDevice();
  $("#andLog").textContent = ""; $("#btnFix").hidden = true; setDroidView("build");
  await api("/api/android/run", { project: S.cur, serial: serial(), module: $("#moduleSel").value, applicationId: appId(), logcat: true, variant: variant() });
  toast("Building and installing… logcat starts when the app launches");
});
$("#btnHotReload").onclick = act(() => api("/api/flutter/reload", { project: S.cur }));
$("#flAuto").checked = store.get("fs:flauto", true);
$("#flAuto").onchange = act(async () => { store.set("fs:flauto", $("#flAuto").checked); if (flRun()) await api("/api/flutter/auto", { project: S.cur, on: $("#flAuto").checked }); });
$$("#flDebug [data-ext]").forEach((b) => (b.onclick = act(async () => {
  await api("/api/flutter/ext", { project: S.cur, name: b.dataset.ext, enabled: !b.classList.contains("on") });
  b.classList.toggle("on");
})));
$("#btnDevtools").onclick = act(async () => {
  toast("Starting DevTools…");
  const r = await api("/api/flutter/devtools", { project: S.cur });
  open(r.url, "_blank");
});
$("#btnFlTask").onclick = act(() => $("#flTask").value.trim() && flutterTask($("#flTask").value.trim()));
$("#flTask").onkeydown = (e) => { if (e.key === "Enter") $("#btnFlTask").click(); };
$("#btnFlPkg").onclick = act(async () => {
  const pkgs = $("#flPkg").value.trim().split(/\s+/).filter(Boolean);
  if (!pkgs.length) return;
  if (pkgs.some((x) => !/^(dev:)?[a-z][a-z0-9_]*(:[\w.^<>=+-]+)?$/.test(x))) throw new Error("Package names look like http or dev:mockito (lowercase letters, digits and _)");
  await flutterTask("pub add " + pkgs.join(" "));
  $("#flPkg").value = "";
});
$("#flPkg").onkeydown = (e) => { if (e.key === "Enter") $("#btnFlPkg").click(); };
$("#lcScope").onchange = () => { if (isFlutter()) store.set("fs:flscope", $("#lcScope").value); attachRunLog(); };
$("#tileNewFlutter").onclick = openNewFlutter;
$$("#nfTemplates .tpl").forEach((b) => (b.onclick = () => $$("#nfTemplates .tpl").forEach((x) => x.classList.toggle("active", x === b))));
$("#newFlutterForm").onsubmit = (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  act(async () => {
    const platforms = $$("#nfPlatforms input").filter((c) => c.checked).map((c) => c.value);
    if (!platforms.length) throw new Error("Pick at least one platform");
    const r = await api("/api/flutter/new", { name: $("#nfName").value, org: $("#nfOrg").value.trim(), parent: $("#nfParent").value.trim(),
      template: $("#nfTemplates .tpl.active").dataset.tpl, platforms });
    store.set("fs:nforg", $("#nfOrg").value.trim()); store.set("fs:nfparent", $("#nfParent").value.trim());
    $("#dlgNewFlutter").close();
    S.pendingSelect = r.path;
    toast(`Creating ${$("#nfName").value} with flutter create…`);
  })();
};
$("#btnRestart").onclick = act(async () => {
  if (isFlutter()) return api("/api/flutter/reload", { project: S.cur, full: true });
  needDevice(); if (!appId()) throw new Error("No applicationId found"); await api("/api/android/restart", { project: S.cur, serial: serial(), applicationId: appId() }); });
$("#btnBuild").onclick = act(() => isFlutter() ? flutterTask(`build ${flutterBuildTarget()} --${variant()}`) : gradleTask(`:${$("#moduleSel").value || "app"}:assemble${variant()}`));
$$("#droidTabs button").forEach((b) => (b.onclick = () => setDroidView(b.dataset.dv)));
$("#variantSel").onchange = () => { store.set(isFlutter() ? "fs:flmode" : "fs:variant", variant()); renderTaskChips(); renderFlutterRun(); };
$("#moduleSel").onchange = renderTaskChips;
$("#btnNewApp").onclick = () => (isFlutter() ? openNewFlutter() : openNewApp());
$("#tileNewApp").onclick = openNewApp;
$("#tileOpenApp").onclick = () => $("#btnAdd").click();
$("#btnWrapper").onclick = act(async () => { setDroidView("build"); await api("/api/android/wrapper", { project: S.cur }); toast("Generating the Gradle wrapper…"); });
$$("#naTemplates .tpl").forEach((b) => (b.onclick = () => $$("#naTemplates .tpl").forEach((x) => x.classList.toggle("active", x === b))));
$("#naName").oninput = () => {
  if (!$("#naPkg").dataset.edited) $("#naPkg").value = "com.example." + ($("#naName").value.toLowerCase().replace(/[^a-z0-9]/g, "") || "myapp").replace(/^\d+/, "");
};
$("#naPkg").oninput = () => ($("#naPkg").dataset.edited = "1");
$("#newAppForm").onsubmit = (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  act(async () => {
    const r = await api("/api/android/new", { name: $("#naName").value, package: $("#naPkg").value, parent: $("#naParent").value.trim(),
      minSdk: +$("#naMin").value, template: $(".tpl.active").dataset.tpl });
    $("#dlgNewApp").close();
    S.pendingSelect = r.path;
    toast(`Creating ${$("#naName").value} (compileSdk ${r.compileSdk}) — setting up the Gradle wrapper…`);
  })();
};
$("#btnTask").onclick = act(() => $("#gradleTask").value.trim() && gradleTask($("#gradleTask").value.trim()));
$("#gradleTask").onkeydown = (e) => { if (e.key === "Enter") $("#btnTask").click(); };
$("#btnStopApp").onclick = act(async () => {
  if (isFlutter()) { await api("/api/flutter/stop", { project: S.cur }); return toast("Stopping the app…"); }
  if (!appId()) throw new Error("No applicationId found"); await api("/api/android/stop", { project: S.cur, serial: serial(), applicationId: appId() }); toast("App stopped"); });
$("#btnConsoleStop").onclick = act(() => S.consoleProc && api("/api/proc/kill", { id: S.consoleProc }));
$("#btnConsoleClear").onclick = () => ($("#andLog").textContent = "");
$("#btnShot").onclick = act(async () => {
  const blob = await api("/api/android/screenshot?serial=" + encodeURIComponent(serial() || ""));
  $("#shotImg").src = URL.createObjectURL(blob); $("#shotBox").hidden = false;
});
$("#btnShotClose").onclick = () => ($("#shotBox").hidden = true);
$("#btnFix").onclick = act(async () => {
  const lines = S.procLogs[S.lastGradle?.proc] || [];
  const idx = lines.findIndex((l) => /FAILURE:|What went wrong|^e: |error:|^\s*error •|Error: /.test(l));
  const excerpt = lines.slice(Math.max(0, idx < 0 ? lines.length - 120 : idx - 30)).slice(0, 160).join("\n");
  if (/^(flutter|dart) /.test(S.lastGradle.label)) {
    await send(`The Flutter command \`${S.lastGradle.label}\` failed. Please find the cause and fix it, then tell me what you changed.\n\n\`\`\`\n${excerpt}\n\`\`\``);
    return ($("#btnFix").hidden = true);
  }
  await send(`The Android Gradle task \`${S.lastGradle.label}\` failed. Please find the cause and fix it, then tell me what you changed.\n\n\`\`\`\n${excerpt}\n\`\`\``);
  $("#btnFix").hidden = true;
});
$("#btnLogcat").onclick = act(async () => {
  const pr = S.procs.find((x) => x.id === LC.proc);
  if (pr?.running) return api("/api/proc/kill", { id: LC.proc });
  const scope = $("#lcScope").value;
  if (scope === "app" && !appId()) throw new Error("No app detected in this project — switch to “Whole device”");
  needDevice();
  await api("/api/android/logcat", { project: S.cur, serial: serial(), applicationId: appId(), scope });
});
$("#lcLevel").value = store.get("fs:lclevel", "V");
$("#lcLevel").onchange = () => { store.set("fs:lclevel", $("#lcLevel").value); lcRender(); };
$("#lcSearch").oninput = () => { clearTimeout(LC.st); LC.st = setTimeout(lcRender, 150); };
$("#btnLcPause").onclick = () => { LC.paused = !LC.paused; $("#btnLcPause").classList.toggle("on", LC.paused); $("#btnLcPause").innerHTML = ic(LC.paused ? "play" : "pause"); };
$("#btnLcClear").onclick = act(async () => { LC.lines = []; lcRender(); if (serial()) await api("/api/android/logcat/clear", { serial: serial() }); });
$("#btnLcAsk").onclick = () => {
  const vis = LC.lines.filter(lcVisible).slice(-150);
  if (!vis.length) return toast("No log lines to send yet", true);
  $("#prompt").value = `Here is recent ${LC.flutter ? "output from my Flutter app" : `logcat output from my ${isFlutter() ? "Flutter" : "Android"} app`}${appId() ? ` (${appId()})` : ""}. What's going wrong and how do I fix it?\n\n\`\`\`\n${logcatText(vis)}\n\`\`\``;
  $("#prompt").focus(); $("#prompt").setSelectionRange(0, 0); $("#prompt").scrollTop = 0;
  toast("Log added to the message box — add your question and press Send");
};
$("#btnCrashClose").onclick = () => ($("#crashBar").hidden = true);
$("#btnCrashFix").onclick = act(async () => {
  const trace = LC.crash?.lines.join("\n") || logcatText(LC.lines.filter((l) => LVL[l.lvl] >= 4).slice(-80));
  if (LC.crash?.run) await send(`\`flutter run\` failed for my Flutter app. Here is the end of its output. Find the cause and fix it, then tell me what you changed.\n\n\`\`\`\n${trace}\n\`\`\``);
  else if (LC.flutter) await send(`My Flutter app threw this error while I was using it. Find the cause in the code and fix it, then tell me what you changed.\n\n\`\`\`\n${trace}\n\`\`\``);
  else await send(`My Android app${appId() ? ` (${appId()})` : ""} crashed while I was using it. Here is the crash from logcat. Find the cause in the code and fix it, then tell me what you changed.\n\n\`\`\`\n${trace}\n\`\`\``);
  $("#crashBar").hidden = true;
});
$("#btnMirror").onclick = () => {
  if (M.on) return stopMirror();
  if (!serial()) return toast("Connect your phone first", true);
  M.on = true; M.t = performance.now(); M.frames = 0;
  $("#btnMirror").innerHTML = ic("stop") + " Hide screen";
  $("#mirrorWrap").hidden = $("#typeRow").hidden = false;
  mirrorLoop();
};
$("#mirrorImg").onpointerdown = $("#mirrorCanvas").onpointerdown = (e) => {
  e.preventDefault();
  const img = e.currentTarget; img.setPointerCapture(e.pointerId);
  const start = devicePoint(e), t0 = performance.now();
  img.onpointerup = (u) => {
    img.onpointerup = null;
    const end = devicePoint(u), ms = Math.round(performance.now() - t0);
    const dist = Math.hypot(end.x - start.x, end.y - start.y);
    if (dist < 12 && ms < 500) input({ kind: "tap", ...start });
    else if (dist < 12) input({ kind: "swipe", x: start.x, y: start.y, x2: start.x, y2: start.y, ms });
    else input({ kind: "swipe", x: start.x, y: start.y, x2: end.x, y2: end.y, ms: Math.min(Math.max(ms, 80), 1500) });
  };
};
for (const [id, key] of Object.entries({ kBack: 4, kHome: 3, kRecents: 187, kPower: 26, kVolUp: 24, kVolDown: 25 })) $("#" + id).onclick = () => input({ kind: "key", key });
const sendText = () => { const t = $("#typeText").value; if (!t) return; input({ kind: "text", text: t }).then(() => input({ kind: "key", key: 66 })); $("#typeText").value = ""; };
$("#btnType").onclick = sendText;
$("#typeText").onkeydown = (e) => { if (e.key === "Enter") sendText(); };
$("#btnScrcpy").onclick = act(async () => { needDevice(); await api("/api/android/scrcpy", { serial: serial(), project: S.cur }); toast("Opening the mirror window…"); });

// add project dialog
async function browse(path) {
  const d = await api("/api/fs/list?path=" + encodeURIComponent(path));
  $("#addPath").value = d.path; S.browseParent = d.parent;
  const ul = $("#addDirs"); ul.innerHTML = "";
  d.dirs.forEach((n) => { const li = document.createElement("li"); li.innerHTML = ic("folder") + " " + esc(n); li.onclick = act(() => browse(d.path.replace(/\/$/, "") + "/" + n)); ul.append(li); });
}
$("#btnAdd").onclick = act(async () => { await browse(S.cur ? S.cur.replace(/\/[^/]+$/, "") : "~"); $("#dlgAdd").showModal(); });
$("#btnAdd").oncontextmenu = (e) => {
  e.preventDefault();
  if (typeof showMenu !== "function") return;
  showMenu(e.clientX, e.clientY, [
    { label: "Add existing folder…", icon: "folder", fn: () => $("#btnAdd").click() },
    { label: "Clone repo…", icon: "clone", fn: () => openProjectDialog("clone") },
    { label: "New project…", icon: "folder-plus", fn: () => openProjectDialog("new") },
  ]);
};
$("#addUp").onclick = act(() => browse(S.browseParent));
$("#addPath").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); act(() => browse($("#addPath").value))(); } };
$("#dlgAdd").onclose = act(async () => {
  if ($("#dlgAdd").returnValue !== "ok") return;
  const p = await api("/api/projects/add", { path: $("#addPath").value });
  await loadState(); selectProject(p.path);
});

// ============================================================ boot
(async () => {
  try {
    const svg = await fetch("icons.svg").then((r) => r.text());
    $("#sprite").outerHTML = svg.replace("<svg ", '<svg style="position:absolute;width:0;height:0" aria-hidden="true" ');
  } catch {}
  if ("serviceWorker" in navigator && isSecureContext) navigator.serviceWorker.register("sw.js").catch(() => {});
  applyAppearance(store.get("fs:theme", {}));
  if (!TOKEN) {  // opened without the link: ask the local server for its token (this computer only)
    try {
      const r = await fetch("/api/bootstrap");
      if (r.ok) { TOKEN = (await r.json()).token; store.set("fs:token", TOKEN); }
    } catch {}
  }
  if (!TOKEN) return showTokenGate();
  if (!LOGIN) return showLogin();
  try { await loadState(); } catch (e) { if (!(e instanceof LoginNeeded)) toast("Can't reach server: " + e.message, true); return; }
  $("#mode").value = S.prefs.mode || "ask";
  connect();
  applyDevice();
  const tab = store.get("fs:tab", "preview");
  showTab(tab);
  setDroidView(store.get("fs:droidView", "run"));
  setMode(store.get("fs:mode", tab === "code" || store.get("fs:code", false) ? "editor" : "agent"), false);
  let start = store.get(pkey("cur"), null);
  if (hashProject) {
    try { start = (await api("/api/projects/add", { path: hashProject })).path; await loadState(); } catch (e) { toast(e.message, true); }
  }
  await selectProject(S.projects.some((p) => p.path === start) ? start : null);
  // first launch for this profile: open the setup checklist if something important is missing
  if (!store.get(pkey("setupSeen"), false)) {
    store.set(pkey("setupSeen"), true);
    act(async () => { const st = await loadSetup(); if (st.items.some((i) => !i.ok && !i.optional)) openSettings("setup"); })();
  }
})();
