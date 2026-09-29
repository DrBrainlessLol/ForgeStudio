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
  procLogs: {}, consoleProc: null, lastGradle: null, viewProc: null,
  attachments: [],
  device: store.get("fs:device", "fill"), rotated: false,
};
const project = () => S.projects.find((p) => p.path === S.cur);
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
        ? `<a href="${fileUrl(x.path)}" target="_blank"><img src="${fileUrl(x.path)}" alt="${esc(x.name)}"></a>`
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
    el.innerHTML = `<summary><span class="tname">${esc(it.name)}</span><span class="targ">${esc(toolArg(it.input))}</span>${actions}<span class="tstate ${state[0]}">${state[1]}</span></summary>${toolBody(it)}`;
    el.ontoggle = () => (it._open = el.open);
    el.querySelector("[data-edit]")?.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); if (typeof openInEditor === "function") openInEditor(fp); });
    el.querySelectorAll(".tcopy").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation(); e.preventDefault();
      const pre = b.closest(".tsec").querySelector("pre");
      navigator.clipboard.writeText(pre.textContent).then(() => toast("Copied")).catch(() => toast("Copy failed", true));
    }));
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
function applyEvent(items, ev) {
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
      for (const b of ev.blocks) if (!items.some((i) => i.k === "tool" && i.id === b.id)) push({ k: "tool", id: b.id, name: b.name, input: b.input, result: null });
      break;
    case "tool_results":
      for (const r of ev.results) {
        const it = items.find((i) => i.k === "tool" && i.id === r.id);
        if (it) { it.result = r.content; it.error = r.error; changed.push(it); }
      }
      break;
    case "approval":
      if (!items.some((i) => i.k === "approval" && i.id === ev.id)) push({ k: "approval", id: ev.id, tool: ev.tool, input: ev.input, description: ev.description, hints: ev.hints });
      break;
    case "approval_done": {
      const it = items.find((i) => i.k === "approval" && i.id === ev.id);
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
    box.innerHTML = `<div class="empty"><img src="icon.svg" alt=""><h2>Welcome to Forge Studio</h2><p>Pick a project in the sidebar, or start one below. The agent works inside the folder; the editor, previews and Android tools are on the right.</p>
      <div class="tiles" id="welcomeTiles">
        <div class="tile" data-w="open">${ic("folder")}<span class="label">Open Project</span></div>
        <div class="tile" data-w="clone">${ic("clone")}<span class="label">Clone Repo</span></div>
        <div class="tile hot" data-w="new">${ic("folder-plus")}<span class="label">New Project</span></div>
      </div></div>`;
    box.querySelector('[data-w="open"]').onclick = () => $("#btnAdd").click();
    box.querySelector('[data-w="clone"]').onclick = () => openProjectDialog("clone");
    box.querySelector('[data-w="new"]').onclick = () => openProjectDialog("new");
    updateBusy(); return;
  }
  const items = cid ? S.items[cid] || [] : [];
  if (!items.length) box.innerHTML = `<div class="empty"><h2>${esc(project()?.name)}</h2><p>Ask the agent to build, fix or explain something. Attach screenshots, designs or files with the clip.</p><p class="dim">Earlier conversations are in the menu at the top.</p></div>`;
  for (const it of items) box.append(renderItem(it));
  updateBusy();
  box.scrollTop = box.scrollHeight;
}
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
  if (ev.type === "approval" && (!visible || document.hidden)) toast(`${S.meta[cid]?.title || "A chat"} needs your approval`);
  if (ev.type === "ui_start" || ev.type === "ui_end") { if (visible) updateBusy(); else renderProjects(); }
  if (ev.type === "ui_end" && !visible && S.meta[cid]) toast(`Finished: ${S.meta[cid].title}`);
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

async function send(text) {
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
  $("#prompt").value = ""; S.attachments = []; renderAttachments();
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
    row("Git", t.git), row("Android SDK / adb", t.adb), row("Android Studio", t.studio), row("Java", t.java), row("Node.js", t.node), row("scrcpy", t.scrcpy),
  ].join("");
}
async function selectProject(path) {
  S.cur = path || null;
  store.set(pkey("cur"), S.cur);
  const p = project();
  $("#crumb").textContent = p ? p.name : "";
  document.title = p ? `${p.name} — Forge Studio` : "Forge Studio";
  $("#historyPanel").hidden = true;
  if (innerWidth < 900) $("#layout").classList.remove("show-sidebar");
  renderProjects(); setupPreview(); setupAndroid(); renderProcs();
  if (typeof codeProjectChanged === "function") codeProjectChanged();
  if (p && !p.web?.length && p.android && activeTab() === "preview") showTab("android");
  await openChat(chatId()).catch((e) => { S.curChat[S.cur] = null; renderChat(); toast(e.message, true); });
}
async function loadState() {
  const st = await api("/api/state");
  Object.assign(S, { profile: st.profile, projects: st.projects, tools: st.tools, previews: st.previews, procs: st.procs,
    providers: st.providers, settings: st.settings, agents: st.agents, prefs: st.prefs || {}, web: st.web });
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
function openSettings(sec = "general") {
  showSection(sec);
  $("#swatches").innerHTML = ACCENTS.map(([n, c]) => `<button class="swatch" title="${n}" data-accent="${c}" style="background:${c}"></button>`).join("");
  $$("#swatches .swatch").forEach((b) => (b.onclick = () => setPref({ accent: b.dataset.accent })));
  applyAppearance(S.prefs);
  $("#setMode").value = S.prefs.mode || "ask";
  $("#setHideTest").checked = !!S.settings?.hideTest;
  fillProfile(); renderAgentList(); renderProviders(); renderWeb();
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
    li.innerHTML = `${ic(a.kind === "claude" ? "sparkles" : "terminal")}<span class="grow"><b>${esc(a.name)}</b> ${badge}<small class="mono">${esc(a.template || a.bin)}</small></span>
      <span class="badge ${a.installed ? "ok" : ""}">${a.installed ? "Installed" : "Not installed"}</span>${a.custom ? `<button class="icon-btn" title="Remove">${ic("trash")}</button>` : ""}`;
    li.querySelector("button")?.addEventListener("click", act(async () => { await api("/api/agents/delete", { id: a.id }); await loadState(); renderAgentList(); }));
    ul.append(li);
  }
}
$("#agAdd").onclick = act(async () => {
  await api("/api/agents/save", { name: $("#agName").value.trim(), template: $("#agTpl").value.trim() });
  $("#agName").value = $("#agTpl").value = ""; await loadState(); renderAgentList(); toast("Agent added — pick it under the message box");
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
  ul.innerHTML = `<li>${ic("sparkles")}<span class="grow"><b>This computer's Claude login</b><small>Always available</small></span></li>`;
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
  const roots = p?.web?.length ? p.web : p ? [{ dir: p.path, rel: ".", kind: "static", command: null }] : [];
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
  const a = p?.android;
  const mods = $("#moduleSel");
  mods.innerHTML = "";
  (a?.modules || []).forEach((m) => mods.append(new Option(`${m.name}${m.applicationId ? "  ·  " + m.applicationId : ""}`, m.name)));
  $("#moduleRow").hidden = !a?.modules?.length;
  $("#androidInfo").innerHTML = !p ? "Select a project." : a
    ? `Gradle project${a.gradlew ? "" : " — <b>no gradlew wrapper</b>, open it in Android Studio once"}.`
    : "This folder isn't an Android Gradle project. You can still open it in Android Studio, or add your app folder with the + button.";
  for (const id of ["#btnRun", "#btnBuild", "#btnStopApp", "#btnRestart", "#btnTask"]) $(id).disabled = !a;
  $("#btnStudio").disabled = !p;
  $("#andLog").textContent = ""; $("#btnFix").hidden = true; $("#btnConsoleStop").hidden = true;
  S.consoleProc = null;
  const g = p && S.procs.filter((x) => x.kind === "gradle" && x.project === p.path).pop();
  if (g) { S.consoleProc = g.id; $("#consoleTitle").textContent = g.label; $("#btnConsoleStop").hidden = !g.running; loadLog(g.id, $("#andLog")); }
  else $("#consoleTitle").textContent = "Build output";
  LC.proc = null; LC.lines = []; $("#crashBar").hidden = true;
  const l = p && S.procs.filter((x) => x.kind === "logcat" && x.project === p.path).pop();
  if (l) { attachLogcat(l.id, l.running); api("/api/proc/log?id=" + l.id).then((d) => { d.lines.forEach((x) => lcAdd(x, true)); lcRender(); }).catch(() => {}); }
  else { lcSetRunning(false); lcRender(); }
}
const appId = () => project()?.android?.modules?.find((m) => m.name === $("#moduleSel").value)?.applicationId;
const serial = () => $("#deviceSel").value || null;
async function refreshDevices() {
  const d = await api("/api/android/devices");
  const sel = $("#deviceSel"), prev = sel.value || store.get("fs:serial", "");
  sel.innerHTML = "";
  if (!d.devices.length) sel.append(new Option("No device — plug in USB (debugging on) or connect over Wi-Fi", ""));
  for (const x of d.devices) sel.append(new Option(`${x.emulator ? "Emulator" : x.wifi ? "Phone (Wi-Fi)" : "Phone (USB)"} · ${x.model || x.serial}  (${x.state === "device" ? x.serial : x.state})`, x.serial));
  const ready = d.devices.filter((x) => x.state === "device").map((x) => x.serial);
  if (ready.includes(prev)) sel.value = prev; else if (ready.length) sel.value = ready[0];
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
  if (d.error) toast(d.error, true);
}
async function gradleTask(task) {
  $("#andLog").textContent = ""; $("#btnFix").hidden = true;
  await api("/api/android/gradle", { project: S.cur, task, serial: serial() });
}

// logcat viewer
const LC = { proc: null, lines: [], paused: false, crash: null };
const LVL = { V: 0, D: 1, I: 2, W: 3, E: 4, F: 5, A: 5 };
const LC_RE = /^(\d\d-\d\d)\s+(\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEFA])\s+(.*?)\s*: ?(.*)$/;
function lcParse(raw) {
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
  if (!vis.length) box.innerHTML = `<div class="dim lc-empty">${LC.proc ? "Waiting for log lines…" : "Press <b>Run app</b> (logcat starts automatically) or <b>Start logcat</b>."}</div>`;
  const frag = document.createDocumentFragment();
  vis.forEach((l) => frag.append(lcRow(l)));
  box.append(frag);
  box.scrollTop = box.scrollHeight;
}
function lcAdd(raw, bulk) {
  const l = lcParse(raw);
  LC.lines.push(l);
  if (LC.lines.length > 6000) LC.lines.splice(0, 1000);
  if (/FATAL EXCEPTION|ANR in /.test(l.msg) || (l.tag === "AndroidRuntime" && l.lvl === "E" && !LC.crash)) {
    if (!LC.crash || Date.now() - LC.crash.t > 3000) LC.crash = { t: Date.now(), lines: [] };
  }
  if (LC.crash && LC.crash.lines.length < 80 && Date.now() - LC.crash.t < 3000 && (l.lvl === "E" || l.lvl === "F")) {
    LC.crash.lines.push(l.raw);
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

// device screen mirror
const M = { on: false, t: 0, frames: 0 };
async function mirrorLoop() {
  while (M.on) {
    const t0 = performance.now();
    try {
      const blob = await api("/api/android/screenshot?serial=" + encodeURIComponent(serial() || ""));
      const img = $("#mirrorImg"), old = img.src;
      img.src = URL.createObjectURL(blob);
      if (old) URL.revokeObjectURL(old);
      M.frames++;
      if (performance.now() - M.t > 2000) { $("#mirrorStat").textContent = `${(M.frames / ((performance.now() - M.t) / 1000)).toFixed(1)} fps`; M.t = performance.now(); M.frames = 0; }
    } catch (e) { if (!(e instanceof LoginNeeded)) toast(e.message, true); stopMirror(); break; }
    await new Promise((r) => setTimeout(r, Math.max(0, 150 - (performance.now() - t0))));
  }
}
function stopMirror() {
  M.on = false;
  $("#btnMirror").innerHTML = ic("cast") + " Show screen";
  $("#mirrorWrap").hidden = $("#typeRow").hidden = true;
  $("#mirrorStat").textContent = "";
}
const input = (body) => api("/api/android/input", { serial: serial(), ...body }).catch((e) => toast(e.message, true));
function devicePoint(e) {
  const img = $("#mirrorImg"), r = img.getBoundingClientRect();
  return { x: ((e.clientX - r.left) / r.width) * img.naturalWidth, y: ((e.clientY - r.top) / r.height) * img.naturalHeight };
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
        if (ev.project === S.cur && ev.kind === "gradle") {
          S.consoleProc = ev.proc; $("#andLog").textContent = ""; $("#btnFix").hidden = true;
          $("#consoleTitle").textContent = ev.label; $("#btnConsoleStop").hidden = false;
        }
        if (ev.project === S.cur && ev.kind === "logcat") { attachLogcat(ev.proc, true); $("#crashBar").hidden = true; LC.crash = null; }
        if (ev.project === S.cur && ev.kind === "preview") $("#prevLog").textContent = "";
        renderProcs(); break;
      case "log":
        if (ev.kind !== "logcat") {
          (S.procLogs[ev.proc] ??= []).push(ev.line);
          if (S.procLogs[ev.proc].length > 4000) S.procLogs[ev.proc].shift();
        }
        if (ev.project === S.cur && ev.kind === "preview") appendLog($("#prevLog"), ev.line);
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
        if (ev.kind === "preview" && S.previews[ev.project]) {
          delete S.previews[ev.project];
          if (ev.project === S.cur) { $("#btnPrevStart").hidden = false; $("#btnPrevStop").hidden = true; $("#prevLogBox").open = true; }
          renderProjects();
        }
        if (ev.kind === "gradle" && ev.project !== S.cur) toast(`${ev.label} ${ev.code === 0 ? "finished" : "failed"} in ${ev.project.split("/").pop()}`, ev.code !== 0);
        renderProcs(); break;
      }
      case "preview_url":
        S.previews[ev.project] = { url: ev.url };
        if (S.profile) store.set(pkey("url:" + ev.project), ev.url);
        if (ev.project === S.cur) { setUrl(ev.url, true); $("#btnPrevStart").hidden = true; $("#btnPrevStop").hidden = false; }
        renderProjects(); break;
      case "reload":
        if (ev.project === S.cur && $("#autoReload").checked && $("#iframe").src) {
          clearTimeout(S.reloadT); S.reloadT = setTimeout(() => { try { $("#iframe").contentWindow.location.reload(); } catch { $("#iframe").src = $("#iframe").src; } }, 150);
        }
        break;
      case "toast": toast(ev.text, ev.err); break;
      case "projects_changed":
        if (ev.text) toast(ev.text);
        act(async () => { await loadState(); if (ev.select) await selectProject(ev.select); })();
        break;
    }
  };
}

// ============================================================ tabs & layout
const activeTab = () => document.querySelector(".tabs .active").dataset.tab;
function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
  store.set("fs:tab", name);
  $("#layout").classList.toggle("code-left", name === "code");
  if (name === "android") act(refreshDevices)();
  if (name === "procs") renderProcs();
}
// the code editor is its own pane on the left of the chat, independent of the Preview / Android / Processes tabs
function showCode(on = $("#tab-code").hidden) {
  $("#tab-code").hidden = $("#codeSplit").hidden = !on;
  $("#btnCode").classList.toggle("on", on);
  store.set("fs:code", on);
  if (on && typeof codeTabShown === "function") codeTabShown();
}
$("#btnCode").onclick = () => showCode();
$$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
$("#btnSidebar").onclick = () => {
  const l = $("#layout");
  if (innerWidth < 900) l.classList.toggle("show-sidebar");
  else { l.classList.toggle("no-sidebar"); store.set("fs:nosidebar", l.classList.contains("no-sidebar")); }
};
if (store.get("fs:nosidebar", false)) $("#layout").classList.add("no-sidebar");
$("#btnTogglePanel").onclick = () => { $("#layout").classList.toggle("no-panel"); store.set("fs:nopanel", $("#layout").classList.contains("no-panel")); };
if (store.get("fs:nopanel", false)) $("#layout").classList.add("no-panel");
(() => {
  const r = $("#resizer"), chat = $("#chatPane");
  const w = store.get("fs:chatw", null); if (w) chat.style.width = w + "px";
  r.onpointerdown = (e) => {
    r.setPointerCapture(e.pointerId); r.classList.add("drag");
    $$("iframe").forEach((f) => (f.style.pointerEvents = "none"));
    const box = chat.getBoundingClientRect(), right = $("#layout").classList.contains("code-left");
    // chat on the left grows as the divider moves right; docked on the right (Code tab) it grows as it moves left
    r.onpointermove = (m) => { chat.style.width = Math.max(340, right ? box.right - m.clientX : m.clientX - box.left) + "px"; };
    r.onpointerup = () => {
      r.onpointermove = null; r.classList.remove("drag");
      $$("iframe").forEach((f) => (f.style.pointerEvents = ""));
      store.set("fs:chatw", parseInt(chat.style.width));
    };
  };
})();
(() => {
  const r = $("#codeSplit"), pane = $("#tab-code");
  const w = store.get("fs:codew", null); if (w) pane.style.width = w + "px";
  r.onpointerdown = (e) => {
    r.setPointerCapture(e.pointerId); r.classList.add("drag");
    $$("iframe").forEach((f) => (f.style.pointerEvents = "none"));
    const left = pane.getBoundingClientRect().left;
    r.onpointermove = (m) => { pane.style.width = Math.max(360, m.clientX - left) + "px"; };
    r.onpointerup = () => {
      r.onpointermove = null; r.classList.remove("drag");
      $$("iframe").forEach((f) => (f.style.pointerEvents = ""));
      store.set("fs:codew", parseInt(pane.style.width));
    };
  };
})();

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
  await api("/api/preview/stop", { project: S.cur });
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
$("#deviceSel").onchange = () => store.set("fs:serial", $("#deviceSel").value);
$("#btnDevRefresh").onclick = act(refreshDevices);
$("#btnAvd").onclick = act(async () => { await api("/api/android/emulator", { avd: $("#avdSel").value, project: S.cur }); toast("Emulator starting — press refresh in a moment"); setTimeout(act(refreshDevices), 15000); });
$("#btnWifi").onclick = act(async () => {
  const r = await api("/api/android/connect", { address: $("#wifiAddr").value, code: $("#wifiCode").value });
  toast(r.output || "done"); $("#wifiCode").value = ""; await refreshDevices();
});
$("#btnReverse").onclick = act(async () => { const r = await api("/api/android/reverse", { port: $("#revPort").value, serial: serial() }); toast(`Device localhost:${$("#revPort").value} → this computer (${r.output})`); });
$("#btnStudio").onclick = act(async () => { await api("/api/android/studio", { project: S.cur }); toast("Opening in Android Studio…"); });
const needDevice = () => { if (!serial()) throw new Error("Connect your phone (USB or Wi-Fi) or launch an emulator first"); };
$("#btnRun").onclick = act(async () => {
  needDevice();
  $("#andLog").textContent = ""; $("#btnFix").hidden = true;
  await api("/api/android/run", { project: S.cur, serial: serial(), module: $("#moduleSel").value, applicationId: appId(), logcat: true });
  toast("Building and installing… logcat starts when the app launches");
});
$("#btnRestart").onclick = act(async () => { needDevice(); if (!appId()) throw new Error("No applicationId found"); await api("/api/android/restart", { project: S.cur, serial: serial(), applicationId: appId() }); });
$("#btnBuild").onclick = act(() => gradleTask(`:${$("#moduleSel").value || "app"}:assembleDebug`));
$("#btnTask").onclick = act(() => $("#gradleTask").value.trim() && gradleTask($("#gradleTask").value.trim()));
$("#gradleTask").onkeydown = (e) => { if (e.key === "Enter") $("#btnTask").click(); };
$("#btnStopApp").onclick = act(async () => { if (!appId()) throw new Error("No applicationId found"); await api("/api/android/stop", { project: S.cur, serial: serial(), applicationId: appId() }); toast("App stopped"); });
$("#btnConsoleStop").onclick = act(() => S.consoleProc && api("/api/proc/kill", { id: S.consoleProc }));
$("#btnConsoleClear").onclick = () => ($("#andLog").textContent = "");
$("#btnShot").onclick = act(async () => {
  const blob = await api("/api/android/screenshot?serial=" + encodeURIComponent(serial() || ""));
  $("#shotImg").src = URL.createObjectURL(blob); $("#shotBox").hidden = false;
});
$("#btnShotClose").onclick = () => ($("#shotBox").hidden = true);
$("#btnFix").onclick = act(async () => {
  const lines = S.procLogs[S.lastGradle?.proc] || [];
  const idx = lines.findIndex((l) => /FAILURE:|What went wrong|^e: |error:/.test(l));
  const excerpt = lines.slice(Math.max(0, idx < 0 ? lines.length - 120 : idx - 30)).slice(0, 160).join("\n");
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
  $("#prompt").value = `Here is recent logcat output from my Android app${appId() ? ` (${appId()})` : ""}. What's going wrong and how do I fix it?\n\n\`\`\`\n${logcatText(vis)}\n\`\`\``;
  $("#prompt").focus(); $("#prompt").setSelectionRange(0, 0); $("#prompt").scrollTop = 0;
  toast("Log added to the message box — add your question and press Send");
};
$("#btnCrashClose").onclick = () => ($("#crashBar").hidden = true);
$("#btnCrashFix").onclick = act(async () => {
  const trace = LC.crash?.lines.join("\n") || logcatText(LC.lines.filter((l) => LVL[l.lvl] >= 4).slice(-80));
  await send(`My Android app${appId() ? ` (${appId()})` : ""} crashed while I was using it. Here is the crash from logcat. Find the cause in the code and fix it, then tell me what you changed.\n\n\`\`\`\n${trace}\n\`\`\``);
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
$("#mirrorImg").onpointerdown = (e) => {
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
  showTab(["preview", "android", "procs"].includes(tab) ? tab : "preview");
  showCode(store.get("fs:code", false) || tab === "code");
  let start = store.get(pkey("cur"), null);
  if (hashProject) {
    try { start = (await api("/api/projects/add", { path: hashProject })).path; await loadState(); } catch (e) { toast(e.message, true); }
  }
  await selectProject(S.projects.some((p) => p.path === start) ? start : null);
})();
