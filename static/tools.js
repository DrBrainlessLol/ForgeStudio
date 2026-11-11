// Tools that came from the phone app: a message menu, sharing a chat, the chat-files browser, locking the computer, system info.
"use strict";

// ---- share / export a chat as Markdown
function chatMarkdown() {
  const cid = chatId();
  const items = (cid && S.items[cid]) || [];
  const title = (cid && S.meta[cid]?.title) || "Chat";
  const out = [`# ${title}`, ""];
  for (const it of items) {
    if (it.k === "user") {
      out.push("**You**", "");
      if (it.text) out.push(it.text);
      for (const f of it.files || []) out.push(`- 📎 ${f.name}`);
      out.push("");
    } else if (it.k === "text" && it.text.trim() && !it.sub) {
      out.push("**Forge**", "", it.text, "");
    } else if (it.k === "files") {
      for (const f of it.files) out.push(`- 📎 ${f.name}`);
      out.push("");
    }
  }
  return { title, text: out.join("\n").trim() + "\n" };
}
function downloadText(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = name.replace(/[^\w .-]+/g, "_").slice(0, 60) + ".md";
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
function shareMenu(x, y) {
  if (!chatId()) return toast("Open a conversation first", true);
  showMenu(x, y, [
    { label: "Download as Markdown (.md)", icon: "download", fn: () => { const c = chatMarkdown(); downloadText(c.title, c.text); } },
    { label: "Copy the conversation", icon: "copy", fn: () => { copyText(chatMarkdown().text); toast("Conversation copied"); } },
    { label: "Files from this project's chats", icon: "files", fn: openChatFiles },
  ]);
}

// ---- right-click a message
function quoteInPrompt(text) {
  const box = $("#prompt");
  box.value = (box.value.trim() ? box.value.trimEnd() + "\n\n" : "") + text.trim().split("\n").map((l) => "> " + l).join("\n") + "\n\n";
  box.dispatchEvent(new Event("input")); box.focus();
}
function msgMenu(e, it) {
  e.preventDefault(); e.stopPropagation();
  const items = [{ label: "Copy", icon: "copy", fn: () => { copyText(it.text); toast("Copied"); } },
    { label: "Quote in reply", icon: "edit", fn: () => quoteInPrompt(it.text) }];
  if (it.k === "user") items.push(
    { label: "Edit and resend", icon: "edit", fn: () => { const b = $("#prompt"); b.value = it.text; b.dispatchEvent(new Event("input")); b.focus(); } },
    { label: "Send again", icon: "refresh", fn: act(() => send(it.text)) });
  items.push("sep", { label: "Share or export this chat…", icon: "download", fn: () => shareMenu(e.clientX, e.clientY) });
  showMenu(e.clientX, e.clientY, items);
}
function attachMsgMenu(el, it) { if (it.text && it.text.trim()) el.addEventListener("contextmenu", (e) => msgMenu(e, it)); }

// ---- files from a project's chats: made by the agent, attached by you, and its screenshots
const CF = { project: null, files: [], source: "all", q: "" };
async function openChatFiles() {
  CF.project = S.cur; CF.source = "all"; CF.q = "";
  $("#cfSearch").value = "";
  $("#cfProject").innerHTML = (S.projects || []).map((p) => `<option value="${esc(p.path)}"${p.path === CF.project ? " selected" : ""}>${esc(p.name)}</option>`).join("");
  $("#dlgChatFiles").showModal();
  await loadChatFiles();
}
async function loadChatFiles() {
  $("#cfBody").innerHTML = '<p class="dim">Loading…</p>';
  try { CF.files = (await api("/api/chat/files?project=" + encodeURIComponent(CF.project || ""))).files; }
  catch (e) { CF.files = []; toast(e.message, true); }
  renderChatFiles();
}
function renderChatFiles() {
  const by = (s) => CF.files.filter((f) => s === "all" || f.source === s).length;
  const names = [["all", "All"], ["made", "Made by the agent"], ["attached", "Attached"], ["screenshot", "Screenshots"]];
  $("#cfFilters").innerHTML = names.map(([k, n]) => `<button class="chip${CF.source === k ? " on" : ""}" data-s="${k}">${n} <small>${by(k)}</small></button>`).join("");
  $$("#cfFilters .chip").forEach((b) => (b.onclick = () => { CF.source = b.dataset.s; renderChatFiles(); }));
  const q = CF.q.trim().toLowerCase();
  const shown = CF.files.filter((f) => (CF.source === "all" || f.source === CF.source) && (!q || f.name.toLowerCase().includes(q)));
  const ago = (t) => new Date(t * 1000).toLocaleDateString();
  const imgs = shown.filter((f) => f.kind === "image"), rest = shown.filter((f) => f.kind !== "image");
  $("#cfBody").innerHTML = !shown.length ? `<p class="dim">${CF.files.length ? "Nothing matches." : "No files have been made or attached in this project's chats yet."}</p>`
    : (imgs.length ? `<h4>Images <small>${imgs.length}</small></h4><div class="cf-grid">${imgs.map((f) => `<a href="${fileUrl(f.path)}" target="_blank" rel="noopener" title="${esc(f.name)} · ${esc(f.chatTitle)}"><img src="${fileUrl(f.path)}" loading="lazy" alt=""><span>${esc(f.name)}</span></a>`).join("")}</div>` : "")
    + (rest.length ? `<h4>Files <small>${rest.length}</small></h4>` + rest.map((f) => `<div class="out-file"><span class="out-ext">${esc((f.name.split(".").pop() || "file").slice(0, 4))}</span>
        <span class="grow"><b title="${esc(f.path)}">${esc(f.name)}</b><small>${fmtSize(f.size)} · ${esc(f.chatTitle.slice(0, 40))} · ${ago(f.mtime)}</small></span>
        <a class="btn small" href="${fileUrl(f.path)}" target="_blank" rel="noopener">${ic("external")} Open</a>
        <a class="btn small primary" href="${fileUrl(f.path, true)}">${ic("download")} Download</a></div>`).join("") : "");
}
$("#cfProject").onchange = () => { CF.project = $("#cfProject").value; loadChatFiles(); };
$("#cfSearch").oninput = () => { CF.q = $("#cfSearch").value; renderChatFiles(); };
$("#cfClose").onclick = () => $("#dlgChatFiles").close();
$("#btnChatFiles").onclick = act(openChatFiles);
$("#btnShareChat").onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); shareMenu(r.right + 6, r.top); };

// ---- lock this computer (Forge Studio keeps running)
function renderLockButton() {
  const b = $("#btnLockPc"), l = S.web?.lock;
  b.hidden = !l?.supported;
  if (l?.supported) b.lastChild.textContent = l.locked ? " This computer is locked" : " Lock this computer";
}
$("#btnLockPc").onclick = act(async () => { S.web = await api("/api/lock", { action: "lock" }); renderLockButton(); toast("Locked. Forge Studio keeps running"); });

// ---- system information (CPU, memory, swap, disks), refreshed while the panel is open
const gb = (n) => (n / 1073741824).toFixed(n >= 1.07e10 ? 0 : 1) + " GB";
async function refreshSys() {
  try {
    const s = await api("/api/sysinfo");
    const bar = (label, pct, text) => `<div class="sys-row"><span>${label}</span><span class="sys-bar"><i style="width:${Math.min(100, pct)}%"></i></span><b>${text}</b></div>`;
    $("#sysInfo").innerHTML = [
      s.cpu != null ? bar("CPU", s.cpu, Math.round(s.cpu) + "%") : "",
      s.mem ? bar("Memory", 100 * s.mem.used / s.mem.total, `${gb(s.mem.used)} / ${gb(s.mem.total)}`) : "",
      s.swap?.total ? bar("Swap", 100 * s.swap.used / s.swap.total, `${gb(s.swap.used)} / ${gb(s.swap.total)}`) : "",
      ...(s.disks || []).map((d) => bar(esc(d.mount), 100 * d.used / d.total, `${gb(d.used)} / ${gb(d.total)}`)),
    ].join("");
  } catch {}
}
let sysTimer = 0;
$("#sideSystem").addEventListener("toggle", () => {
  clearInterval(sysTimer);
  if ($("#sideSystem").open) { refreshSys(); sysTimer = setInterval(refreshSys, 4000); }
});

setTimeout(renderLockButton, 1500); setInterval(renderLockButton, 5000);
