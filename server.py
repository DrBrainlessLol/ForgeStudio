#!/usr/bin/env python3
"""Forge Studio - a local graphical front-end for the Claude Code CLI.

Standard library only. Serves a web UI on 127.0.0.1 and exposes a token-protected
JSON API for: per-profile chats with `claude` (history, external model providers),
website dev previews, and building / running / live-debugging Android projects via
Gradle + adb. Profiles sign in with Google (optional) or a local name + PIN.
"""
import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = Path.home()
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_DIR = HOME / ".config" / "forge-studio"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token"
PROFILES_DIR = CONFIG_DIR / "profiles"
PORT = int(os.environ.get("FORGE_STUDIO_PORT", "8765"))
ORIGIN = f"http://127.0.0.1:{PORT}"

SCAN_ROOTS = [HOME / "projects", HOME / "AndroidStudioProjects", HOME / "Documents", HOME / "Desktop"]
SKIP_DIRS = {"node_modules", ".git", "build", "dist", ".gradle", ".next", "__pycache__", ".idea", "out"}

# ---------------------------------------------------------------- json files

_io_lock = threading.RLock()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data, private=False):
    with _io_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        if private:
            tmp.chmod(0o600)
        tmp.replace(path)


CONFIG = read_json(CONFIG_FILE, {})
for k, v in {"projects": [], "hidden": [], "settings": {}, "profiles": {}, "logins": {}}.items():
    CONFIG.setdefault(k, v)
CONFIG.pop("sessions", None)  # pre-profile format


def save_config():
    write_json(CONFIG_FILE, CONFIG, private=True)


def get_token():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok)
    TOKEN_FILE.chmod(0o600)
    return tok


TOKEN = get_token()

# ---------------------------------------------------------------- profiles


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def check_pin(pin, stored):
    salt = stored.split("$", 1)[0]
    return secrets.compare_digest(hash_pin(pin, salt), stored)


def public_profile(pid):
    p = CONFIG["profiles"][pid]
    return {"id": pid, "name": p["name"], "email": p.get("email"), "picture": p.get("picture"),
            "kind": p["kind"], "hasPin": bool(p.get("pin"))}


def create_profile(name, kind, **extra):
    pid = uuid.uuid4().hex[:12]
    CONFIG["profiles"][pid] = {"name": name, "kind": kind, "created": time.time(), **extra}
    save_config()
    return pid


def issue_login(pid):
    tok = secrets.token_urlsafe(32)
    CONFIG["logins"][sha(tok)] = {"profile": pid, "created": time.time()}
    save_config()
    return tok


def login_profile(tok):
    rec = CONFIG["logins"].get(sha(tok or ""))
    if rec and rec["profile"] in CONFIG["profiles"]:
        return rec["profile"]
    return None


class Profile:
    """Per-profile state: chats index, current chat per project, model providers."""
    _cache = {}

    def __init__(self, pid):
        self.pid = pid
        self.dir = PROFILES_DIR / pid
        self.file = self.dir / "state.json"
        self.data = read_json(self.file, {})
        for k, v in {"chats": {}, "current": {}, "providers": [], "settings": {}}.items():
            self.data.setdefault(k, v)

    @classmethod
    def get(cls, pid):
        if pid not in cls._cache:
            cls._cache[pid] = Profile(pid)
        return cls._cache[pid]

    def save(self):
        write_json(self.file, self.data, private=True)

    def chat_log(self, cid):
        return self.dir / "chats" / f"{cid}.jsonl"

    def append(self, cid, ev):
        with _io_lock:
            f = self.chat_log(cid)
            f.parent.mkdir(parents=True, exist_ok=True)
            with f.open("a") as fh:
                fh.write(json.dumps(ev) + "\n")

    def provider(self, prov_id):
        return next((p for p in self.data["providers"] if p["id"] == prov_id), None)


# ---------------------------------------------------------------- tooling


def find_sdk():
    for p in [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), HOME / "Android" / "Sdk"]:
        if p and Path(p).is_dir():
            return Path(p)
    return None


def find_studio():
    custom = CONFIG["settings"].get("studio_path")
    candidates = [custom, shutil.which("studio"), shutil.which("studio.sh"), shutil.which("android-studio"),
                  "/opt/apps/cn.android.studio/files/bin/studio", "/opt/android-studio/bin/studio.sh",
                  str(HOME / "android-studio" / "bin" / "studio.sh"), "/snap/bin/android-studio"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def find_java_home():
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    studio = find_studio()
    if studio:
        root = Path(studio).resolve().parent.parent
        for sub in ("jbr", "jre"):
            if (root / sub / "bin" / "java").exists():
                return str(root / sub)
    return None


def adb_path():
    sdk = find_sdk()
    if sdk and (sdk / "platform-tools" / "adb").exists():
        return str(sdk / "platform-tools" / "adb")
    return shutil.which("adb")


def scrcpy_path():
    for c in (HOME / ".local" / "scrcpy" / "scrcpy", shutil.which("scrcpy")):
        if c and Path(c).exists():
            return str(c)
    return None


def emulator_path():
    sdk = find_sdk()
    if sdk and (sdk / "emulator" / "emulator").exists():
        return str(sdk / "emulator" / "emulator")
    return shutil.which("emulator")


def tool_env():
    env = dict(os.environ)
    sdk = find_sdk()
    if sdk:
        env.setdefault("ANDROID_HOME", str(sdk))
        env["PATH"] = f"{sdk}/platform-tools:{env.get('PATH', '')}"
    jh = find_java_home()
    if jh:
        env["JAVA_HOME"] = jh
    env["PATH"] = f"{HOME}/.local/node/bin:{HOME}/.local/bin:{env.get('PATH', '')}"
    env.pop("CLAUDECODE", None)  # let nested claude runs start normally
    return env


# ---------------------------------------------------------------- projects


def android_info(path: Path):
    if not ((path / "settings.gradle").exists() or (path / "settings.gradle.kts").exists()):
        return None
    info = {"gradlew": (path / "gradlew").exists(), "modules": []}
    for child in sorted(path.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue
        for f in ("build.gradle.kts", "build.gradle"):
            bf = child / f
            if bf.exists():
                text = bf.read_text(errors="ignore")
                if "com.android.application" in text or "android.application" in text:
                    m = re.search(r'applicationId\s*=?\s*["\']([\w.]+)["\']', text)
                    info["modules"].append({"name": child.name, "applicationId": m.group(1) if m else None})
                break
    return info


def web_roots(path: Path):
    roots = []

    def check(d: Path):
        rel = str(d.relative_to(path)) if d != path else "."
        pkg = d / "package.json"
        if pkg.exists():
            try:
                scripts = json.loads(pkg.read_text()).get("scripts", {})
            except Exception:
                scripts = {}
            cmd = next((f"npm run {s}" for s in ("dev", "start", "serve", "preview") if s in scripts), "npm start")
            roots.append({"dir": str(d), "rel": rel, "kind": "node", "command": cmd,
                          "installed": (d / "node_modules").is_dir()})
        elif (d / "index.html").exists():
            roots.append({"dir": str(d), "rel": rel, "kind": "static", "command": None})

    check(path)
    try:
        for child in sorted(path.iterdir()):
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                check(child)
    except PermissionError:
        pass
    return roots


def describe(path_str):
    p = Path(path_str)
    if not p.is_dir():
        return None
    return {"path": str(p), "name": p.name, "android": android_info(p), "web": web_roots(p)}


def project_paths():
    found = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            markers = ["settings.gradle", "settings.gradle.kts", "package.json", "index.html", ".git", "CLAUDE.md"]
            if any((child / m).exists() for m in markers):
                found.append(str(child))
    paths = []
    for p in CONFIG["projects"] + found:
        if p not in paths and p not in CONFIG["hidden"]:
            paths.append(p)
    return paths


def discover():
    return [d for d in (describe(p) for p in project_paths()) if d]


# ---------------------------------------------------------------- events

_subs = []  # (queue, profile id)
_subs_lock = threading.Lock()


def emit(event, profile=None):
    """Send an event to every connected UI; profile-scoped events only reach that profile."""
    with _subs_lock:
        for q, pid in list(_subs):
            if profile and pid != profile:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# ---------------------------------------------------------------- processes

PROCS = {}


def start_proc(cmd, cwd, kind, project, label, shell=False, env=None, on_line=None, on_exit=None, meta=None):
    pid = uuid.uuid4().hex[:10]
    popen = subprocess.Popen(cmd, cwd=cwd, shell=shell, env=env or tool_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                             errors="replace", start_new_session=True)
    rec = {"id": pid, "kind": kind, "project": project, "label": label, "popen": popen,
           "log": deque(maxlen=5000), "started": time.time(), "code": None, "meta": meta or {}}
    PROCS[pid] = rec
    emit({"type": "proc_start", "proc": pid, "kind": kind, "project": project, "label": label, "meta": rec["meta"]})

    def pump():
        for line in popen.stdout:
            line = line.rstrip("\n")
            rec["log"].append(line)
            emit({"type": "log", "proc": pid, "kind": kind, "project": project, "line": line})
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
        rec["code"] = popen.wait()
        emit({"type": "proc_exit", "proc": pid, "kind": kind, "project": project, "code": rec["code"],
              "label": label})
        if on_exit:
            on_exit(rec["code"])

    threading.Thread(target=pump, daemon=True).start()
    return rec


def kill_proc(pid):
    rec = PROCS.get(pid)
    if rec and rec["popen"].poll() is None:
        try:
            os.killpg(rec["popen"].pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        threading.Timer(4, lambda: _hard_kill(rec)).start()


def _hard_kill(rec):
    if rec["popen"].poll() is None:
        try:
            os.killpg(rec["popen"].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def running(kind, project):
    for rec in PROCS.values():
        if rec["kind"] == kind and rec["project"] == project and rec["popen"].poll() is None:
            return rec
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- agents (CLI engines)

# Claude Code is the first-class engine (streaming, approvals, images, providers). Codex has a
# native adapter. Anything else runs through a command template where {prompt} is the message.
KNOWN_AGENTS = [
    {"id": "claude", "name": "Claude Code", "bin": "claude", "kind": "claude"},
    {"id": "builtin", "name": "Built-in (API key, no CLI)", "bin": None, "kind": "builtin"},
    {"id": "codex", "name": "Codex", "bin": "codex", "kind": "codex"},
    {"id": "gemini", "name": "Gemini CLI", "bin": "gemini", "kind": "generic", "template": "gemini --yolo -p {prompt}"},
    {"id": "qwen", "name": "Qwen Code", "bin": "qwen", "kind": "generic", "template": "qwen --yolo -p {prompt}"},
    {"id": "opencode", "name": "opencode", "bin": "opencode", "kind": "generic", "template": "opencode run {prompt}"},
    {"id": "cursor-agent", "name": "Cursor Agent", "bin": "cursor-agent", "kind": "generic", "template": "cursor-agent -p --force {prompt}"},
    {"id": "aider", "name": "Aider", "bin": "aider", "kind": "generic", "template": "aider --yes-always --no-pretty --message {prompt}"},
]


def list_agents():
    path = tool_env()["PATH"]
    out = [dict(a, installed=(True if not a["bin"] else bool(shutil.which(a["bin"], path=path)))) for a in KNOWN_AGENTS]
    for a in CONFIG["settings"].get("agents", []):
        out.append({"id": a["id"], "name": a["name"], "kind": "generic", "template": a["template"], "custom": True,
                    "installed": bool(shutil.which(shlex.split(a["template"])[0], path=path))})
    return out


def agent_by_id(aid):
    return next((a for a in list_agents() if a["id"] == aid), None)


# ---------------------------------------------------------------- risk hints for approvals

RISKY = [
    (r"\brm\s+(-\w*r\w*f|-\w*f\w*r|-r|-R)\b", "danger", "Deletes files or folders recursively"),
    (r"\brm\s", "caution", "Deletes files"),
    (r"\bsudo\b", "danger", "Runs as administrator (sudo)"),
    (r"\bgit\s+push\b.*(--force|-f)\b", "danger", "Force-pushes and can overwrite remote history"),
    (r"\bgit\s+push\b", "caution", "Pushes commits to a remote repository"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-\w*f|checkout\s+--\s|restore\s)", "danger", "Discards uncommitted changes"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "danger", "Downloads and runs a script from the internet"),
    (r"\b(mkfs|dd\s+if=|fdisk|parted)\b", "danger", "Low-level disk operation"),
    (r"\bchmod\s+(-R\s+)?777\b|\bchown\s+-R\b", "caution", "Changes permissions broadly"),
    (r"\b(shutdown|reboot|poweroff|systemctl\s+(stop|disable))\b", "danger", "Affects the whole system"),
    (r"\b(kill|pkill|killall)\b", "caution", "Stops running processes"),
    (r"\b(npm|yarn|pnpm)\s+publish\b|\btwine\s+upload\b", "danger", "Publishes a package publicly"),
    (r"\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\b", "danger", "Deletes database data"),
    (r"\b(apt|apt-get|dnf|pacman|snap)\s+(install|remove|purge)\b|\bpip\s+install\b|\bnpm\s+(i|install)\s+-g\b", "caution", "Installs or removes software"),
    (r"\badb\s+(uninstall|shell\s+pm\s+clear|reboot)\b", "caution", "Changes the connected phone"),
]


def risk_hints(tool, inp, project):
    hints = []
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        for rx, level, text in RISKY:
            if re.search(rx, cmd, re.I) and not any(h["text"] == text for h in hints):
                hints.append({"level": level, "text": text})
        if hints and any(h["text"] == "Deletes files or folders recursively" for h in hints):
            hints = [h for h in hints if h["text"] != "Deletes files"]
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if path and tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        try:
            outside = not Path(path).resolve().is_relative_to(Path(project).resolve())
        except Exception:
            outside = False
        if outside:
            hints.append({"level": "caution", "text": "Changes a file outside this project"})
        if Path(path).exists() and tool == "Write":
            hints.append({"level": "info", "text": "Overwrites an existing file"})
    if tool in ("WebFetch", "WebSearch"):
        hints.append({"level": "info", "text": "Accesses the internet"})
    return hints


# ---------------------------------------------------------------- chat

RUNS = {}       # chat id -> Run
APPROVALS = {}  # approval id -> {"run", "request_id", "input", "suggestions", "chat"}


def provider_env(prov):
    """Environment for `claude` so it talks to the chosen account / provider."""
    env = tool_env()
    if not prov or prov.get("type") == "local":
        return env
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    if prov["type"] == "anthropic":
        env["ANTHROPIC_API_KEY"] = prov.get("apiKey", "")
    else:  # Anthropic-compatible endpoint (DeepSeek, OpenRouter, Kimi, GLM, Ollama, custom)
        env["ANTHROPIC_BASE_URL"] = prov["baseUrl"].rstrip("/")
        env["ANTHROPIC_AUTH_TOKEN"] = prov.get("apiKey") or "none"
        env["ANTHROPIC_API_KEY"] = ""
        models = [m for m in prov.get("models", []) if m]
        if models:
            main, fast = models[0], models[-1]
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = main
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = main
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = main
    return env


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def attachment_note(files):
    others = [f for f in files if f["kind"] != "image"]
    if not others:
        return ""
    lines = "\n".join(f"- {f['name']}: {f['path']}" for f in others)
    return f"\n\nAttached files (read them from these paths):\n{lines}"


class Run:
    """One agent turn: spawns the CLI, streams slim events to the UI and the chat log."""

    def __init__(self, prof, chat, project, prompt, files, agent, model, mode, prov):
        self.prof, self.chat, self.project = prof, chat, project
        self.prompt, self.files, self.agent, self.model, self.mode, self.prov = prompt, files, agent, model, mode, prov
        self.cid = chat["id"]
        self.popen = None
        self.stopped = False
        self.finished = False
        self.always = set()
        self.lock = threading.Lock()
        self.text_buf = {"text": "", "sub": False}

    # -- plumbing
    def send(self, ev, log=True):
        emit({"type": "chat", "chat": self.cid, "project": self.project, "event": ev}, profile=self.prof.pid)
        if log:
            self.prof.append(self.cid, ev)

    def flush(self):
        if self.text_buf["text"]:
            self.prof.append(self.cid, {"type": "text", "text": self.text_buf["text"], "sub": self.text_buf["sub"]})
            self.text_buf["text"] = ""

    def delta(self, text, sub=False):
        if self.text_buf["text"] and self.text_buf["sub"] != sub:
            self.flush()
        self.text_buf["text"] += text
        self.text_buf["sub"] = sub
        self.send({"type": "delta", "text": text, "sub": sub}, log=False)

    def event(self, ev):
        self.flush()
        self.send(ev, log=ev["type"] not in ("text_start", "retry"))

    def write(self, obj):
        with self.lock:
            try:
                self.popen.stdin.write(json.dumps(obj) + "\n")
                self.popen.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def running(self):
        if self.agent["kind"] == "builtin":
            return not self.finished
        return self.popen is not None and self.popen.poll() is None

    def stop(self):
        self.stopped = True
        if self.popen and self.popen.poll() is None:
            try:
                os.killpg(self.popen.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        for ap in list(APPROVALS.values()):  # unblock a built-in run waiting on approval
            if ap.get("run") is self and ap.get("event"):
                ap["result"] = "deny"
                ap["event"].set()

    def set_session(self, sid):
        if sid and self.chat.get("session") != sid:
            self.chat["session"] = sid
            self.prof.save()

    # -- lifecycle
    def start(self):
        self.send({"type": "user", "text": self.prompt, "files": self.files, "agent": self.agent["id"], "ts": time.time()})
        self.send({"type": "ui_start"}, log=False)
        kind = self.agent["kind"]
        target = {"claude": self.run_claude, "codex": self.run_codex, "builtin": self.run_builtin}.get(kind, self.run_generic)
        threading.Thread(target=self.guard, args=(target,), daemon=True).start()

    def guard(self, target):
        code, err = 1, ""
        try:
            code, err = target()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self.flush()
        for aid in [k for k, v in APPROVALS.items() if v["run"] is self]:
            APPROVALS.pop(aid, None)
            self.send({"type": "approval_done", "id": aid, "decision": "cancelled"}, log=False)
        self.finished = True
        self.chat["updated"] = time.time()
        self.prof.save()
        if code:
            self.send({"type": "ui_raw", "err": True, "text": (err or "").strip()[-2000:] or f"{self.agent['name']} exited with code {code}"})
        self.send({"type": "ui_end", "code": code}, log=False)

    def spawn(self, cmd, env, stdin=subprocess.PIPE):
        self.popen = subprocess.Popen(cmd, cwd=self.project, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1, errors="replace",
                                      start_new_session=True)
        err_lines = deque(maxlen=60)
        threading.Thread(target=lambda: [err_lines.append(l) for l in self.popen.stderr], daemon=True).start()
        return err_lines

    # -- Claude Code (native: streaming, approvals, images, providers)
    def run_claude(self):
        cmd = [shutil.which("claude", path=tool_env()["PATH"]), "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        mode = self.mode or "acceptEdits"
        if mode == "ask":
            cmd += ["--permission-mode", "default", "--permission-prompt-tool", "stdio"]
        elif mode in ("acceptEdits", "auto", "plan"):
            cmd += ["--permission-mode", mode, "--permission-prompt-tool", "stdio"]
        else:
            cmd += ["--permission-mode", "bypassPermissions"]
        if self.model:
            cmd += ["--model", self.model]
        if self.chat.get("session"):
            cmd += ["--resume", self.chat["session"]]
        if self.files:
            cmd += ["--add-dir", str(self.prof.dir / "uploads")]
        err_lines = self.spawn(cmd, provider_env(self.prov))
        content = [{"type": "text", "text": self.prompt + attachment_note(self.files)}]
        for f in self.files:
            mt = IMAGE_TYPES.get(Path(f["path"]).suffix.lower())
            if f["kind"] == "image" and mt and Path(f["path"]).stat().st_size < 5_000_000:
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                            "data": base64.b64encode(Path(f["path"]).read_bytes()).decode()}})
        self.write({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})
        self.write({"type": "user", "message": {"role": "user", "content": content}})
        for line in self.popen.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                self.event({"type": "ui_raw", "text": line})
                continue
            self.set_session(ev.get("session_id"))
            if ev.get("type") == "control_request":
                self.on_control(ev)
                continue
            slim = slim_event(ev)
            if not slim:
                continue
            if slim["type"] == "delta":
                self.delta(slim["text"], slim["sub"])
            elif slim["type"] == "retry" and slim["status"] in (401, 403) and slim["attempt"] >= 2:
                who = self.prov["name"] if self.prov else "Claude"
                self.event({"type": "ui_raw", "err": True, "text": f"{who} rejected the login / API key (HTTP {slim['status']}). "
                            "Check the key in Settings → Models & keys, or pick another model."})
                self.stop()
            else:
                self.event(slim)
                if slim["type"] == "result":
                    with self.lock:
                        try:
                            self.popen.stdin.close()  # turn finished: let the CLI exit
                        except OSError:
                            pass
        code = self.popen.wait()
        return code, "\n".join(err_lines) if code else ""

    def on_control(self, ev):
        req = ev.get("request", {})
        if req.get("subtype") != "can_use_tool":
            self.write({"type": "control_response", "response": {"subtype": "error", "request_id": ev.get("request_id"),
                                                                  "error": "unsupported"}})
            return
        aid = uuid.uuid4().hex[:10]
        tool, inp = req.get("tool_name"), req.get("input") or {}
        APPROVALS[aid] = {"run": self, "request_id": ev["request_id"], "input": inp,
                          "suggestions": req.get("permission_suggestions") or [], "chat": self.cid, "tool": tool}
        self.event({"type": "approval", "id": aid, "tool": tool, "input": inp,
                    "description": req.get("description") or "", "hints": risk_hints(tool, inp, self.project)})

    def answer(self, aid, decision, answers=None, note=None):
        ap = APPROVALS.get(aid)
        if not ap:
            return
        if ap.get("event"):  # built-in run: hand the decision back to the waiting loop thread
            ap["result"] = decision
            ap["answers"] = answers
            self.send({"type": "approval_done", "id": aid, "decision": decision})
            ap["event"].set()
            return
        APPROVALS.pop(aid, None)
        if decision in ("allow", "always"):
            inp = dict(ap["input"])
            if answers is not None:  # AskUserQuestion
                inp["answers"] = answers
            resp = {"behavior": "allow", "updatedInput": inp}
            if decision == "always" and ap["suggestions"]:
                resp["updatedPermissions"] = ap["suggestions"]
        else:
            resp = {"behavior": "deny", "message": note or "The user declined this action. Ask them how to proceed instead."}
        self.write({"type": "control_response", "response": {"subtype": "success", "request_id": ap["request_id"], "response": resp}})
        self.send({"type": "approval_done", "id": aid, "decision": decision})

    # -- Codex CLI
    def run_codex(self):
        flags = ["--json", "--skip-git-repo-check"]
        if self.model:
            flags += ["-m", self.model]
        flags += {"plan": ["-s", "read-only"], "bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"]}.get(
            self.mode, ["-s", "workspace-write"])
        for f in self.files:
            if f["kind"] == "image":
                flags += ["-i", f["path"]]
        prompt = self.prompt + attachment_note(self.files)
        base = [shutil.which("codex", path=tool_env()["PATH"]), "exec"]
        cmd = base + flags + (["resume", self.chat["session"]] if self.chat.get("session") else ["-C", self.project]) + ["--", prompt]
        err_lines = self.spawn(cmd, tool_env(), stdin=subprocess.DEVNULL)
        failed = None
        for line in self.popen.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t, item = ev.get("type"), ev.get("item") or {}
            it = item.get("type")
            if t == "thread.started":
                self.set_session(ev.get("thread_id"))
            elif t == "item.started" and it == "command_execution":
                self.event({"type": "tools", "blocks": [{"id": item["id"], "name": "Shell", "input": {"command": item.get("command")}}], "sub": False})
            elif t == "item.completed":
                if it == "agent_message":
                    self.flush()
                    self.event({"type": "text", "text": item.get("text", ""), "sub": False})
                elif it == "reasoning" and item.get("text"):
                    self.event({"type": "text", "text": item["text"], "sub": True})
                elif it == "command_execution":
                    self.event({"type": "tools", "blocks": [{"id": item["id"], "name": "Shell", "input": {"command": item.get("command")}}], "sub": False})
                    self.event({"type": "tool_results", "results": [{"id": item["id"], "content": clip(item.get("aggregated_output") or ""),
                                                                      "error": (item.get("exit_code") or 0) != 0}]})
                elif it == "file_change":
                    for i, ch in enumerate(item.get("changes", [])):
                        tid = f"{item['id']}-{i}"
                        self.event({"type": "tools", "blocks": [{"id": tid, "name": "Edit", "input": {"file_path": ch.get("path"), "kind": ch.get("kind")}}], "sub": False})
                        self.event({"type": "tool_results", "results": [{"id": tid, "content": ch.get("kind", "changed"), "error": False}]})
                elif it == "error":
                    self.event({"type": "ui_raw", "text": item.get("message", "")})
            elif t == "error":
                self.event({"type": "retry", "attempt": 0, "max": 0, "status": "", "error": ev.get("message", "")[:200]})
            elif t == "turn.failed":
                failed = (ev.get("error") or {}).get("message", "Codex failed")
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                self.event({"type": "result", "tokens": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)})
        code = self.popen.wait()
        if failed:
            return 1, failed
        return code, "\n".join(err_lines) if code else ""

    # -- any other CLI: command template, plain text output
    def run_generic(self):
        prompt = self.prompt + attachment_note(self.files)
        cmd = [prompt if part == "{prompt}" else part.replace("{prompt}", prompt) for part in shlex.split(self.agent["template"])]
        if "{prompt}" not in self.agent["template"]:
            cmd.append(prompt)
        if self.model and "{model}" in self.agent["template"]:
            cmd = [p.replace("{model}", self.model) for p in cmd]
        err_lines = self.spawn(cmd, tool_env(), stdin=subprocess.DEVNULL)
        self.event({"type": "text_start", "sub": False})
        for line in self.popen.stdout:
            self.delta(ANSI_RE.sub("", line))
        code = self.popen.wait()
        self.event({"type": "result"})
        return code, "\n".join(err_lines) if code else ""

    # -- Built-in engine: talks to an Anthropic-style API directly, no CLI needed
    def ask_approval(self, tool, inp, description=""):
        """Block this run's thread until the user allows or denies (built-in only)."""
        if tool in self.always or self.mode == "bypassPermissions":
            return "allow"
        aid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        APPROVALS[aid] = {"run": self, "chat": self.cid, "tool": tool, "input": inp, "event": ev, "result": None}
        self.event({"type": "approval", "id": aid, "tool": tool, "input": inp,
                    "description": description, "hints": risk_hints(tool, inp, self.project)})
        ev.wait()
        ap = APPROVALS.pop(aid, None)
        decision = (ap or {}).get("result") or "deny"
        if decision == "always":
            self.always.add(tool)
        return decision

    def needs_ask(self, tool, inp):
        if tool == "Read":
            return "allow"
        if self.mode == "plan":
            return "deny"
        if self.mode == "bypassPermissions":
            return "allow"
        danger = any(h["level"] == "danger" for h in risk_hints(tool, inp, self.project))
        if self.mode == "auto":
            return "ask" if danger else "allow"
        if self.mode == "acceptEdits":
            return "ask" if tool == "Bash" else "allow"
        return "ask"

    def run_tool(self, tool, inp):
        """Execute one built-in tool inside the project. Returns (text, is_error)."""
        try:
            if tool == "Read":
                p = Path(inp["file_path"]).expanduser()
                if not p.is_absolute():
                    p = Path(self.project) / p
                text = p.read_text(errors="replace")
                lines = text.splitlines()
                off = int(inp.get("offset", 0) or 0)
                lim = int(inp.get("limit", 2000) or 2000)
                return "\n".join(lines[off:off + lim])[:20000], False
            if tool == "Write":
                p = Path(inp["file_path"]).expanduser()
                if not p.is_absolute():
                    p = Path(self.project) / p
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(inp.get("content", ""))
                return f"Wrote {len(inp.get('content', ''))} bytes to {p}", False
            if tool == "Edit":
                p = Path(inp["file_path"]).expanduser()
                if not p.is_absolute():
                    p = Path(self.project) / p
                text = p.read_text()
                old, new = inp["old_string"], inp.get("new_string", "")
                if old not in text:
                    return "old_string not found in the file", True
                text = text.replace(old, new) if inp.get("replace_all") else text.replace(old, new, 1)
                p.write_text(text)
                return "Edited " + str(p), False
            if tool == "Bash":
                r = subprocess.run(inp["command"], shell=True, cwd=self.project, env=tool_env(),
                                   capture_output=True, text=True, errors="replace", timeout=int(inp.get("timeout", 120)))
                out = (r.stdout + r.stderr).strip()
                return clip(out or "(no output)"), r.returncode != 0
            return f"Unknown tool {tool}", True
        except subprocess.TimeoutExpired:
            return "Command timed out", True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True

    def run_builtin(self):
        if not self.prov:
            return 1, "Pick a model provider (your own API key) in the model menu — the built-in engine needs one."
        prov = self.prov
        wire = "openai" if prov.get("api") == "openai" else "anthropic"
        if self.chat.get("wire") and self.chat["wire"] != wire:
            return 1, "This chat was started with a different kind of provider. Start a new chat to switch."
        self.chat["wire"] = wire
        base = "https://api.anthropic.com" if prov["type"] == "anthropic" else prov["baseUrl"].rstrip("/")
        key = prov.get("apiKey") or ""
        model = self.model or (prov.get("models") or ["claude-opus-4-8"])[0]
        if wire == "openai":
            url = base + "/chat/completions"
            headers = {"content-type": "application/json", "authorization": f"Bearer {key}",
                       "http-referer": "http://127.0.0.1", "x-title": "Forge Studio"}  # last two help with OpenRouter
        else:
            url = base + "/v1/messages"
            headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                       "x-api-key": key, "authorization": f"Bearer {key}"}
        api_file = self.prof.dir / "chats" / f"{self.cid}.api.json"
        messages = read_json(api_file, [])
        messages.append(self.user_message(wire))
        totals = {"in": 0, "out": 0}
        for _ in range(40):  # tool-use loop
            if self.stopped:
                break
            req = self.req_openai if wire == "openai" else self.req_anthropic
            assistant, calls, more = req(url, headers, model, messages, totals)
            if assistant is None:
                return 1, more  # `more` carries the error text
            messages.append(assistant)
            write_json(api_file, messages)
            if not more or not calls or self.stopped:
                break
            tool_msgs = []
            for c in calls:
                tool, inp = c["name"], c.get("input") or {}
                gate = self.needs_ask(tool, inp)
                decision = "deny" if gate == "deny" else "allow" if gate == "allow" else self.ask_approval(tool, inp)
                if decision == "deny" or self.stopped:
                    out, err = ("Stopped by the user." if self.stopped else "The user declined this action."), True
                else:
                    self.event({"type": "tools", "blocks": [{"id": c["id"], "name": tool, "input": inp}]})
                    out, err = self.run_tool(tool, inp)
                    self.event({"type": "tool_results", "results": [{"id": c["id"], "content": out, "error": err}]})
                if wire == "openai":
                    tool_msgs.append({"role": "tool", "tool_call_id": c["id"], "content": ("Error: " if err else "") + out})
                else:
                    tool_msgs.append({"type": "tool_result", "tool_use_id": c["id"], "content": out, "is_error": err})
            if wire == "openai":
                messages += tool_msgs
            else:
                messages.append({"role": "user", "content": tool_msgs})
            write_json(api_file, messages)
        self.done = True
        self.event({"type": "result", "tokens": totals["in"] + totals["out"]})
        return 0, ""

    def user_message(self, wire):
        text = self.prompt + (attachment_note(self.files) if self.files else "")
        imgs = []
        for f in self.files:
            mt = IMAGE_TYPES.get(Path(f["path"]).suffix.lower())
            if f["kind"] == "image" and mt and Path(f["path"]).stat().st_size < 5_000_000:
                b64 = base64.b64encode(Path(f["path"]).read_bytes()).decode()
                if wire == "openai":
                    imgs.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
                else:
                    imgs.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
        if wire == "openai":
            content = ([{"type": "text", "text": text}] if text else []) + imgs
            return {"role": "user", "content": content if imgs else (text or "(no message)")}
        content = ([{"type": "text", "text": text}] if text else []) + imgs
        return {"role": "user", "content": content or [{"type": "text", "text": "(no message)"}]}

    def _open_stream(self, url, headers, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=600), None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code in (401, 403):
                return None, f"{self.prov['name']} rejected the API key (HTTP {e.code}). Check it in Settings → Models & Keys."
            return None, f"API error {e.code}: {detail}"
        except Exception as e:
            return None, f"Could not reach the API: {e}"

    def _sse_lines(self, resp):
        for raw in resp:
            if self.stopped:
                break
            line = raw.decode(errors="replace").strip()
            if line.startswith("data:"):
                yield line[5:].strip()

    def req_anthropic(self, url, headers, model, messages, totals):
        body = {"model": model, "max_tokens": 8192, "stream": True, "system": BUILTIN_SYSTEM,
                "tools": ANTHROPIC_TOOLS, "messages": messages}
        resp, err = self._open_stream(url, headers, body)
        if err:
            return None, None, err
        blocks, cur, stop_reason = [], None, None
        for data in self._sse_lines(resp):
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "content_block_start":
                cb = ev.get("content_block", {})
                cur = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "text": "", "json": ""}
                blocks.append(cur)
            elif t == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    self.delta(d["text"])
                    if cur:
                        cur["text"] += d["text"]
                elif d.get("type") == "input_json_delta" and cur:
                    cur["json"] += d.get("partial_json", "")
            elif t == "message_delta":
                stop_reason = ev.get("delta", {}).get("stop_reason", stop_reason)
                totals["out"] += ev.get("usage", {}).get("output_tokens", 0)
            elif t == "message_start":
                totals["in"] += ev.get("message", {}).get("usage", {}).get("input_tokens", 0)
            elif t == "error":
                return None, None, ev.get("error", {}).get("message", "stream error")
        out = []
        for b in blocks:
            if b["type"] == "text":
                out.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                try:
                    inp = json.loads(b["json"] or "{}")
                except json.JSONDecodeError:
                    inp = {}
                out.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": inp})
        calls = [{"id": b["id"], "name": b["name"], "input": b["input"]} for b in out if b.get("type") == "tool_use"]
        return {"role": "assistant", "content": out}, calls, stop_reason == "tool_use"

    def req_openai(self, url, headers, model, messages, totals):
        body = {"model": model, "max_tokens": 8192, "stream": True, "tools": OPENAI_TOOLS, "tool_choice": "auto",
                "stream_options": {"include_usage": True},
                "messages": [{"role": "system", "content": BUILTIN_SYSTEM}] + messages}
        resp, err = self._open_stream(url, headers, body)
        if err:
            return None, None, err
        text, tcs, finish = "", {}, None
        for data in self._sse_lines(resp):
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("usage"):
                totals["in"] += ev["usage"].get("prompt_tokens", 0)
                totals["out"] += ev["usage"].get("completion_tokens", 0)
            for choice in ev.get("choices", []):
                d = choice.get("delta", {})
                if d.get("content"):
                    self.delta(d["content"])
                    text += d["content"]
                for tc in d.get("tool_calls", []):
                    slot = tcs.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function", {})
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
        ordered = [tcs[i] for i in sorted(tcs)]
        assistant = {"role": "assistant", "content": text or None}
        if ordered:
            assistant["tool_calls"] = [{"id": s["id"] or f"call_{i}", "type": "function",
                                        "function": {"name": s["name"], "arguments": s["args"] or "{}"}} for i, s in enumerate(ordered)]
        calls = []
        for i, s in enumerate(ordered):
            try:
                inp = json.loads(s["args"] or "{}")
            except json.JSONDecodeError:
                inp = {}
            calls.append({"id": s["id"] or f"call_{i}", "name": s["name"], "input": inp})
        return assistant, calls, finish == "tool_calls" and bool(calls)


BUILTIN_SYSTEM = ("You are a coding agent working inside the user's project directory. Use the tools to read, "
                  "write and edit files and run shell commands. Keep replies concise. When you finish, briefly say what you did.")
_TOOL_DEFS = [
    ("Read", "Read a file. Optional offset/limit (line numbers).",
     {"type": "object", "properties": {"file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["file_path"]}),
    ("Write", "Create or overwrite a file with the given content.",
     {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}),
    ("Edit", "Replace old_string with new_string in a file. Set replace_all to change every occurrence.",
     {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["file_path", "old_string", "new_string"]}),
    ("Bash", "Run a shell command in the project directory and return its output.",
     {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}),
]
ANTHROPIC_TOOLS = [{"name": n, "description": d, "input_schema": s} for n, d, s in _TOOL_DEFS]
OPENAI_TOOLS = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, d, s in _TOOL_DEFS]


def start_chat(prof: Profile, project, prompt, chat_id=None, model=None, mode="acceptEdits", provider_id=None,
               files=None, agent_id="claude"):
    chats = prof.data["chats"]
    if chat_id and chat_id not in chats:
        raise ValueError("That chat no longer exists")
    if chat_id and chat_id in RUNS and RUNS[chat_id].running():
        raise ValueError("The agent is still working on this chat - stop it first.")
    agent = agent_by_id(agent_id or "claude")
    if not agent or not agent["installed"]:
        raise ValueError(f"The {agent['name'] if agent else agent_id} CLI isn't installed")
    if chat_id and chats[chat_id].get("agent", "claude") != agent["id"]:
        raise ValueError(f"This chat uses {agent_by_id(chats[chat_id].get('agent', 'claude'))['name']}. "
                         "Start a new chat to switch agents.")
    prov = None
    if agent["kind"] in ("claude", "builtin") and provider_id and provider_id != "local":
        prov = prof.provider(provider_id)
        if not prov:
            raise ValueError("That model provider was removed - pick another in the model menu.")
    if agent["kind"] == "builtin" and not prov:
        raise ValueError("The built-in engine needs your own API key. Add one in Settings → Models & Keys, then pick it in the model menu.")
    files = [f for f in (files or []) if Path(f.get("path", "")).is_file()
             and Path(f["path"]).resolve().is_relative_to((prof.dir / "uploads").resolve())]
    now = time.time()
    if not chat_id:
        chat_id = uuid.uuid4().hex[:12]
        title = re.sub(r"\s+", " ", prompt).strip()[:70] or (files[0]["name"] if files else "New chat")
        chats[chat_id] = {"id": chat_id, "project": project, "title": title, "session": None, "created": now,
                          "agent": agent["id"]}
    chat = chats[chat_id]
    chat.update({"updated": now, "model": model or None, "provider": provider_id or "local"})
    prof.data["current"][project] = chat_id
    prof.save()
    run = Run(prof, chat, project, prompt, files, agent, model, mode, prov)
    RUNS[chat_id] = run
    run.start()
    emit({"type": "chats_changed", "project": project}, profile=prof.pid)
    return chat_id


TOOL_OUT_LIMIT = 60000  # keep tool output readable in the chat without unbounded log growth


def clip(s, n=TOOL_OUT_LIMIT):
    s = str(s)
    return s if len(s) <= n else s[:n] + f"\n… (+{len(s) - n:,} more characters — truncated)"


def slim_event(ev):
    t = ev.get("type")
    sub = bool(ev.get("parent_tool_use_id"))
    if t == "stream_event":
        e = ev.get("event", {})
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
            return {"type": "delta", "text": e["delta"]["text"], "sub": sub}
        if e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "text":
            return {"type": "text_start", "sub": sub}
        return None
    if t == "assistant":
        blocks = [{"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
                  for b in ev.get("message", {}).get("content", []) if b.get("type") == "tool_use"]
        return {"type": "tools", "blocks": blocks, "sub": sub} if blocks else None
    if t == "user":
        content = ev.get("message", {}).get("content")
        out = []
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append({"id": b.get("tool_use_id"), "content": clip(c), "error": b.get("is_error", False)})
        return {"type": "tool_results", "results": out} if out else None
    if t == "system" and ev.get("subtype") == "api_retry":
        return {"type": "retry", "attempt": ev.get("attempt"), "max": ev.get("max_retries"),
                "status": ev.get("error_status"), "error": ev.get("error")}
    if t == "system" and ev.get("subtype") == "init":
        return {"type": "init", "session": ev.get("session_id"), "model": ev.get("model"), "cwd": ev.get("cwd")}
    if t == "result" or ("total_cost_usd" in ev and "duration_ms" in ev):
        return {"type": "result", "cost": ev.get("total_cost_usd"), "duration": ev.get("duration_ms"),
                "turns": ev.get("num_turns"), "error": ev.get("is_error"),
                "denials": ev.get("permission_denials") or []}
    return None


# ---------------------------------------------------------------- google sign-in

GOOGLE_PENDING = {}  # state -> {"verifier", "created"}


def google_cfg():
    g = CONFIG["settings"].get("google") or {}
    return g if g.get("clientId") and g.get("clientSecret") else None


def google_auth_url():
    g = google_cfg()
    if not g:
        raise ValueError("Google sign-in isn't set up yet")
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    GOOGLE_PENDING[state] = {"verifier": verifier, "created": time.time()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": g["clientId"], "redirect_uri": ORIGIN, "response_type": "code",
         "scope": "openid email profile", "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "prompt": "select_account"}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)


def google_finish(code, state):
    pend = GOOGLE_PENDING.pop(state, None)
    if not pend or time.time() - pend["created"] > 600:
        raise ValueError("Sign-in expired, please try again")
    g = google_cfg()
    body = urllib.parse.urlencode({"code": code, "client_id": g["clientId"], "client_secret": g["clientSecret"],
                                   "redirect_uri": ORIGIN, "grant_type": "authorization_code",
                                   "code_verifier": pend["verifier"]}).encode()
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=body, timeout=20) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ValueError("Google rejected the sign-in: " + e.read().decode(errors="replace")[:300])
    # The id_token came straight from Google's token endpoint over TLS, so its claims can be trusted.
    payload = tok["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("aud") != g["clientId"] or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise ValueError("Unexpected Google token")
    sub = claims["sub"]
    pid = next((k for k, p in CONFIG["profiles"].items() if p.get("google") == sub), None)
    if not pid:
        pid = create_profile(claims.get("name") or claims.get("email", "Google user"), "google", google=sub,
                             email=claims.get("email"), picture=claims.get("picture"))
    else:
        CONFIG["profiles"][pid].update(email=claims.get("email"), picture=claims.get("picture"))
        save_config()
    return issue_login(pid)


# ---------------------------------------------------------------- preview

PREVIEWS = {}
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\])(?::\d+)?[^\s\x1b'\"]*")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def start_preview(project, directory, command=None):
    stop_preview(project)
    d = Path(directory)
    port = free_port()
    env = tool_env()
    env["PORT"] = str(port)
    env["BROWSER"] = "none"
    state = {"url": None, "dir": str(d), "stop": threading.Event()}
    PREVIEWS[project] = state

    def set_url(url):
        url = url.replace("0.0.0.0", "127.0.0.1").replace("[::]", "127.0.0.1").replace("[::1]", "127.0.0.1")
        if not state["url"]:
            state["url"] = url
            emit({"type": "preview_url", "project": project, "url": url})

    if command:
        tool = command.split()[0]
        if tool in ("npm", "npx", "yarn", "pnpm") and not shutil.which(tool, path=env["PATH"]):
            raise ValueError(f"`{tool}` is not installed.")
        if tool in ("npm", "yarn", "pnpm") and (d / "package.json").exists() and not (d / "node_modules").is_dir():
            command = f"{tool} install && {command}"  # first run: fetch dependencies
        rec = start_proc(command, str(d), "preview", project, f"preview: {command}", shell=True, env=env,
                         on_line=lambda l: (m := URL_RE.search(ANSI_RE.sub("", l))) and set_url(m.group(0)))
    else:
        rec = start_proc([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "-d", str(d)],
                         str(d), "preview", project, "preview: static", env=env)
        set_url(f"http://127.0.0.1:{port}/")
        threading.Thread(target=watch_files, args=(project, d, state["stop"]), daemon=True).start()
    state["proc"] = rec["id"]
    return state


def stop_preview(project):
    st = PREVIEWS.pop(project, None)
    if st:
        st["stop"].set()
        kill_proc(st.get("proc"))


def snapshot(d: Path):
    sig, count = {}, 0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            try:
                sig[os.path.join(root, f)] = os.stat(os.path.join(root, f)).st_mtime_ns
            except OSError:
                pass
            count += 1
            if count > 5000:
                return sig
    return sig


def watch_files(project, d, stop):
    prev = snapshot(d)
    while not stop.wait(0.8):
        cur = snapshot(d)
        if cur != prev:
            prev = cur
            emit({"type": "reload", "project": project})


# ---------------------------------------------------------------- android


def adb(*args, serial=None, timeout=20):
    a = adb_path()
    if not a:
        raise ValueError("adb not found - install Android SDK platform-tools")
    cmd = [a] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, timeout=timeout, env=tool_env())


def list_devices():
    out = []
    try:
        r = adb("devices", "-l")
    except Exception as e:
        return {"error": str(e), "devices": [], "avds": []}
    for line in r.stdout.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            attrs = dict(p.split(":", 1) for p in parts[2:] if ":" in p and "//" not in p)
            state = "no permissions" if "no permissions" in line else parts[1]
            out.append({"serial": parts[0], "state": state, "model": attrs.get("model", "").replace("_", " "),
                        "emulator": parts[0].startswith("emulator-"), "wifi": ":" in parts[0]})
    avds = []
    em = emulator_path()
    if em:
        try:
            avds = [x for x in subprocess.run([em, "-list-avds"], capture_output=True, text=True, timeout=10,
                                              env=tool_env()).stdout.splitlines() if x and not x.startswith("INFO")]
        except Exception:
            pass
    return {"devices": out, "avds": avds}


def gradle(project, task, serial=None, then=None):
    p = Path(project)
    if not (p / "gradlew").exists():
        raise ValueError("No gradlew wrapper in this project - open it once in Android Studio to generate it.")
    os.chmod(p / "gradlew", 0o755)
    if running("gradle", project):
        raise ValueError("A Gradle task is already running for this project.")
    env = tool_env()
    if serial:
        env["ANDROID_SERIAL"] = serial
    return start_proc(["./gradlew", *task.split(), "--console=plain"], project, "gradle", project,
                      f"gradle {task}", env=env, on_exit=then)


def launch_app(app_id, serial):
    r = adb("shell", "monkey", "-p", app_id, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
    emit({"type": "toast", "text": f"Launched {app_id}" if r.returncode == 0 else r.stderr.decode()[:300]})


def start_logcat(project, serial, app_id=None, scope="app"):
    """Stream logcat. scope=app follows the app by uid, so it survives restarts and crashes."""
    old = running("logcat", project)
    if old:
        kill_proc(old["id"])
    cmd = [adb_path()] + (["-s", serial] if serial else []) + ["logcat", "-v", "threadtime", "-T", "300"]
    label = "logcat: all"
    if scope == "app" and app_id:
        r = adb("shell", "pm", "list", "packages", "-U", app_id, serial=serial)
        m = re.search(rf"package:{re.escape(app_id)} uid:(\d+)", r.stdout.decode(errors="replace"))
        if not m:
            raise ValueError(f"{app_id} isn't installed on this device yet - press Run app first.")
        cmd += [f"--uid={m.group(1)}"]
        label = f"logcat: {app_id}"
    return start_proc(cmd, project, "logcat", project, label, meta={"scope": scope, "app": app_id})


# ---------------------------------------------------------------- files, uploads, LAN access


def save_upload(prof, name, data):
    name = re.sub(r"[^\w.\- ()]+", "_", Path(name).name)[:120] or "file"
    d = prof.dir / "uploads" / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(data)
    kind = "image" if f.suffix.lower() in IMAGE_TYPES else "file"
    return {"name": name, "path": str(f), "size": len(data), "kind": kind,
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"}


def allowed_file(prof, path):
    """Files the UI may open/download: inside a known project or this profile's uploads."""
    f = Path(path).expanduser().resolve()
    roots = [(prof.dir / "uploads").resolve()] + [Path(p["path"]).resolve() for p in discover()]
    if not f.is_file() or not any(f.is_relative_to(r) for r in roots):
        raise ValueError("File not found or not inside one of your projects")
    return f


LAN = {"server": None}


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outward interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def start_lan():
    if LAN["server"]:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", PORT + 1), Handler)
    srv.daemon_threads = True
    srv.lan = True
    LAN["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def stop_lan():
    srv = LAN.pop("server", None)
    LAN["server"] = None
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()


def web_access_info():
    on = bool(LAN["server"])
    return {"lan": on, "url": f"http://{lan_ip()}:{PORT + 1}/#token={TOKEN}" if on else None, "local": f"{ORIGIN}/#token={TOKEN}"}


# ---------------------------------------------------------------- code editor

EDIT_LIMIT = 4 * 1024 * 1024  # bigger files open read-only as "too large"


def project_root(project):
    root = Path(project or "").expanduser().resolve()
    if not project or str(root) not in {str(Path(p).resolve()) for p in project_paths()}:
        raise ValueError("Not one of your projects")
    return root


def project_file(project, rel, change=False, real=False):
    """Resolve `rel` inside a project. `change` refuses the root and .git; `real` also follows symlinks."""
    root = project_root(project)
    f = Path(os.path.normpath(root / (rel or ".")))
    if f != root and not f.is_relative_to(root):
        raise ValueError("That path is outside the project")
    if real and not (f.resolve() == root or f.resolve().is_relative_to(root)):
        raise ValueError("That file links outside the project")
    if change and (f == root or ".git" in f.relative_to(root).parts):
        raise ValueError("That path can't be changed from the editor")
    return root, f


def rel_to(root, f):
    return str(f.relative_to(root)) if f != root else ""


def code_list(project, rel):
    root, d = project_file(project, rel, real=True)
    if not d.is_dir():
        raise ValueError("Folder not found")
    entries = []
    for c in d.iterdir():
        if c.name == ".git":
            continue
        try:
            is_dir = c.is_dir()
            entries.append({"name": c.name, "dir": is_dir, "size": 0 if is_dir else c.stat().st_size, "link": c.is_symlink()})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    entries = entries[:5000]
    if git_prefix(root) is not None and entries:  # grey out ignored entries like an IDE does
        base = rel_to(root, d)
        names = [os.path.join(base, e["name"]) for e in entries]
        r = subprocess.run(["git", "check-ignore", "--stdin", "-z"], cwd=root, input="\0".join(names), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15)
        ignored = set(r.stdout.split("\0"))
        for e, n in zip(entries, names):
            e["ignored"] = n in ignored
    return {"path": rel_to(root, d), "entries": entries}


def code_read(project, rel):
    root, f = project_file(project, rel, real=True)
    if not f.is_file():
        raise ValueError("File not found")
    st = f.stat()
    out = {"path": rel_to(root, f), "size": st.st_size, "mtime": st.st_mtime_ns, "content": None, "binary": False, "tooLarge": False}
    if st.st_size > EDIT_LIMIT:
        out["tooLarge"] = True
        return out
    data = f.read_bytes()
    try:
        if b"\0" in data[:8192]:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "nul byte")
        out["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        out["binary"] = True
    return out


def code_write(project, rel, content, expect=None, force=False):
    root, f = project_file(project, rel, change=True, real=True)
    if f.is_dir():
        raise ValueError("That's a folder")
    if f.exists() and expect and not force and f.stat().st_mtime_ns != int(expect):
        return {"conflict": True, "mtime": f.stat().st_mtime_ns}
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return {"mtime": f.stat().st_mtime_ns}


def trash(f: Path):
    """Move to the desktop trash when possible (recoverable); otherwise delete."""
    if shutil.which("gio") and subprocess.run(["gio", "trash", "--", str(f)], capture_output=True, timeout=30).returncode == 0:
        return True
    if f.is_dir() and not f.is_symlink():
        shutil.rmtree(f)
    else:
        f.unlink()
    return False


def code_files(project):
    """Every file in the project (for quick open), respecting .gitignore in repos."""
    root = project_root(project)
    if git_prefix(root) is not None:
        out = git(root, "ls-files", "-co", "--exclude-standard", "-z", "--", ".", timeout=30)
        return [p for p in out.split("\0") if p][:50000]
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        rel = os.path.relpath(base, root)
        files.extend(n if rel == "." else os.path.join(rel, n) for n in sorted(names))
        if len(files) > 20000:
            break
    return files


def code_search(project, q, case=False, regex=False):
    root = project_root(project)
    if not q:
        return {"results": [], "truncated": False}
    flags = ["-n", "-I"] + ([] if case else ["-i"]) + (["-E"] if regex else ["-F"])
    if git_prefix(root) is not None:
        cmd = ["git", "grep", "--untracked", "--no-color", "--max-count=100", *flags, "-e", q, "--", "."]
    else:
        cmd = ["grep", "-r", "--max-count=100", *flags, *(f"--exclude-dir={d}" for d in SKIP_DIRS | {".*"}), "-e", q, "--", "."]
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode > 1:
        raise ValueError(r.stderr.strip()[:300] or "search failed")
    results = []
    for line in r.stdout.splitlines():
        m = re.match(r"^(?:\./)?(.+?):(\d+):(.*)$", line)
        if m:
            results.append({"path": m.group(1), "line": int(m.group(2)), "text": m.group(3)[:300]})
        if len(results) >= 2000:
            break
    return {"results": results, "truncated": len(results) >= 2000}


# ---------------------------------------------------------------- git

def git(root, *args, timeout=60, check=True, stdin=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
    if check and r.returncode:
        raise ValueError((r.stderr or r.stdout).strip()[:800] or f"git {args[0]} failed")
    return r.stdout if check else r


def git_prefix(root):
    """Path of the project inside its repository ("" at the top), or None when it isn't in a git repo."""
    if not shutil.which("git"):
        return None
    r = git(root, "rev-parse", "--show-prefix", check=False, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


def git_status(project):
    root = project_root(project)
    prefix = git_prefix(root)
    if prefix is None:
        return {"repo": False, "git": bool(shutil.which("git"))}
    parts = git(root, "status", "--porcelain=v1", "-b", "-z", "--untracked-files=all").split("\0")
    head = parts[0][3:] if parts and parts[0].startswith("## ") else ""
    files, i = [], 1
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        x, y, p, orig = e[0], e[1], e[3:], None
        if x in "RC":
            orig, i = parts[i], i + 1
        if p.startswith(prefix) and len(files) < 3000:
            files.append({"path": p[len(prefix):], "x": x, "y": y, "orig": orig and orig[len(prefix):]})
    m = re.match(r"(?:No commits yet on |Initial commit on )?(.+?)(?:\.\.\.(\S+))?(?: \[(.*)\])?$", head) if head else None
    track = (m and m.group(3)) or ""
    branches = [b for b in git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines() if b]
    remotes = [b for b in git(root, "remote").splitlines() if b]
    return {"repo": True, "git": True, "branch": m.group(1) if m else "", "upstream": m and m.group(2),
            "ahead": int((re.search(r"ahead (\d+)", track) or [0, 0])[1]),
            "behind": int((re.search(r"behind (\d+)", track) or [0, 0])[1]),
            "noCommits": head.startswith(("No commits yet", "Initial commit")),
            "files": files, "branches": branches, "remotes": remotes}


def git_has_head(root):
    return git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def git_diff(project, rel, staged=False, untracked=False):
    root, f = project_file(project, rel)
    p = rel_to(root, f)
    if untracked:
        out = git(root, "diff", "--no-color", "--no-index", "--", "/dev/null", p, check=False).stdout
    else:
        out = git(root, "diff", "--no-color", *(["--cached"] if staged else []), "--", p)
        if not out and not staged:
            out = git(root, "diff", "--no-color", "--cached", "--", p)
    return {"diff": out[:1_000_000]}


def git_log(project):
    root = project_root(project)
    if not git_has_head(root):
        return {"commits": []}
    out = git(root, "log", "-n", "60", "--format=%h%x1f%s%x1f%an%x1f%ct%x1f%D%x1e")
    commits = []
    for rec in out.split("\x1e"):
        f = rec.strip("\n").split("\x1f")
        if len(f) == 5:
            commits.append({"hash": f[0], "subject": f[1], "author": f[2], "time": int(f[3]), "refs": f[4]})
    return {"commits": commits}


def git_action(project, action, b):
    root = project_root(project)
    paths = [project_file(project, p)[1] for p in b.get("paths") or []]
    rels = [rel_to(root, p) or "." for p in paths]
    if action == "init":
        git(root, "init", "-b", "main")
        return None
    if git_prefix(root) is None:
        raise ValueError("This project isn't a git repository yet")
    if action == "stage":
        git(root, "add", "-A", "--", *(rels or ["."]))
    elif action == "unstage":
        if git_has_head(root):
            git(root, "restore", "--staged", "--", *(rels or ["."]))
        else:
            git(root, "rm", "--cached", "-r", "-q", "--", *(rels or ["."]))
    elif action == "discard":
        trashed = 0
        for rel, f in zip(rels, paths):
            if git(root, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0:
                git(root, "restore", "--worktree", "--", rel)
            elif f.exists():
                trash(f)
                trashed += 1
        return {"trashed": trashed}
    elif action == "commit":
        msg = (b.get("message") or "").strip()
        if not msg and not b.get("amend"):
            raise ValueError("Write a commit message first")
        if b.get("all"):
            git(root, "add", "-A", "--", ".")
        ident = git_identity(root, b)
        args = ident + ["commit"] + (["--amend"] if b.get("amend") else []) + (["-m", msg] if msg else ["--no-edit"])
        return {"output": git(root, *args).strip()}
    elif action == "push":
        st = git_status(project)
        if st["upstream"]:
            return {"output": git_net(root, "push")}
        if not st["remotes"]:
            raise ValueError("No remote configured. Add one with: git remote add origin <url>")
        return {"output": git_net(root, "push", "-u", st["remotes"][0], "HEAD")}
    elif action in ("pull", "fetch"):
        return {"output": git_net(root, *(["pull", "--ff-only"] if action == "pull" else ["fetch", "--all", "--prune"]))}
    elif action == "checkout":
        name = (b.get("branch") or "").strip()
        if not name or name.startswith("-"):
            raise ValueError("Enter a branch name")
        git(root, "switch", *(["-c"] if b.get("create") else []), name)
    elif action == "remote":
        url = (b.get("url") or "").strip()
        if not url or url.startswith("-"):
            raise ValueError("Enter the remote URL")
        git(root, "remote", "add", "origin", url)
    else:
        raise ValueError(f"unknown git action {action}")
    return None


def git_identity(root, b):
    """Return `-c user.name/email` flags when the repo has no identity, using the signed-in profile."""
    if git(root, "config", "user.email", check=False).returncode == 0:
        return []
    name = (b.get("authorName") or "").strip() or "Forge Studio user"
    email = (b.get("authorEmail") or "").strip() or re.sub(r"[^\w.-]+", ".", name.lower()) + "@forge.local"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def git_net(root, *args):
    r = git(root, *args, timeout=180, check=False)
    text = (r.stderr + r.stdout).strip()
    if r.returncode:
        if "could not read Username" in text or "terminal prompts disabled" in text:
            text += "\n\nGit needs credentials. Set up an SSH key or a credential helper (e.g. `gh auth login`) and try again."
        raise ValueError(text[:1200] or f"git {args[0]} failed")
    return text or "Done"


COMMIT_SYSTEM = ("You write concise git commit messages. Use the imperative mood. First line: a summary under "
                 "72 characters (a conventional-commit prefix such as feat:/fix:/refactor:/docs: is welcome when it "
                 "fits). If it helps, add a blank line then 1–4 short bullet points. Output ONLY the commit message "
                 "— no code fences, quotes or preamble.")


def model_complete(prov, model, system, user):
    """One non-streaming completion via an Anthropic- or OpenAI-shaped provider. Returns the text."""
    wire = "openai" if prov.get("api") == "openai" else "anthropic"
    base = "https://api.anthropic.com" if prov["type"] == "anthropic" else prov["baseUrl"].rstrip("/")
    key = prov.get("apiKey") or ""
    if wire == "openai":
        url = base + "/chat/completions"
        headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        body = {"model": model, "max_tokens": 400,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    else:
        url = base + "/v1/messages"
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                   "x-api-key": key, "authorization": f"Bearer {key}"}
        body = {"model": model, "max_tokens": 400, "system": system, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403):
            raise ValueError(f"{prov['name']} rejected the API key (HTTP {e.code}).")
        raise ValueError(f"{prov['name']} error {e.code}: {detail}")
    except Exception as e:
        raise ValueError(f"Could not reach {prov['name']}: {e}")
    if wire == "openai":
        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def suggest_commit(project, prof, provider_id=None, model=None):
    """Draft a commit message from the current staged + unstaged changes using the model."""
    root = project_root(project)
    if git_prefix(root) is None:
        raise ValueError("This project isn't a git repository yet")
    staged = git(root, "diff", "--cached", check=False).stdout
    unstaged = git(root, "diff", check=False).stdout
    untracked = [p for p in git(root, "ls-files", "--others", "--exclude-standard", check=False).stdout.splitlines() if p]
    diff = ""
    if staged:
        diff += "# Staged changes\n" + staged + "\n"
    if unstaged:
        diff += "# Unstaged changes\n" + unstaged + "\n"
    if untracked:
        diff += "# New untracked files:\n" + "\n".join(untracked[:60]) + "\n"
    diff = diff.strip()
    if not diff:
        raise ValueError("No changes to describe — edit or stage some files first.")
    user = "Changes:\n```diff\n" + diff[:12000] + "\n```"
    prov = prof.provider(provider_id) if provider_id and provider_id != "local" else None
    if prov:  # user's own key: Anthropic OR OpenAI-shaped
        m = model or (prov.get("models") or [None])[0]
        if not m:
            raise ValueError(f"Add a model to “{prov['name']}” in Settings → Models & Keys first.")
        text = model_complete(prov, m, COMMIT_SYSTEM, user)
    else:  # this computer's Claude login, via the CLI
        claude = shutil.which("claude", path=tool_env()["PATH"])
        if not claude:
            raise ValueError("Sign in with the `claude` CLI or add an API-key provider to generate messages.")
        r = subprocess.run([claude, "-p", COMMIT_SYSTEM + "\n\n" + user, "--model", model or "haiku"], cwd=project,
                           env=provider_env(None), capture_output=True, text=True, errors="replace", timeout=90)
        if r.returncode != 0:
            raise ValueError((r.stderr or "Could not generate a message").strip()[:300])
        text = r.stdout
    msg = re.sub(r"^\s*```[a-zA-Z]*\n?|\n?```\s*$", "", text.strip()).strip().strip('"').strip()
    if not msg:
        raise ValueError("The model returned an empty message — try again.")
    return {"message": msg[:2000]}


def clone_repo(url, parent, name=None):
    url = (url or "").strip()
    if not url or url.startswith("-"):
        raise ValueError("Enter a repository URL")
    name = (name or "").strip() or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    if not name or name in (".", "..") or "/" in name:
        raise ValueError("Choose a folder name for the clone")
    parent = Path(parent or HOME / "projects").expanduser().resolve()
    dest = parent / name
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    parent.mkdir(parents=True, exist_ok=True)

    def done(code):
        if code == 0:
            if str(dest) not in CONFIG["projects"]:
                CONFIG["projects"].insert(0, str(dest))
            save_config()
            emit({"type": "projects_changed", "select": str(dest), "text": f"Cloned {name}"})
        else:
            emit({"type": "toast", "text": f"Cloning {name} failed — see Processes for the log", "err": True})
    env = dict(tool_env(), GIT_TERMINAL_PROMPT="0")
    start_proc(["git", "clone", "--progress", "--", url, str(dest)], str(parent), "git", str(dest), f"git clone {name}",
               env=env, on_exit=done)
    return {"path": str(dest)}


def new_project(parent, name, init_git=False):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name:
        raise ValueError("Enter a folder name")
    dest = Path(parent or HOME / "projects").expanduser().resolve() / name
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    dest.mkdir(parents=True)
    if init_git and shutil.which("git"):
        git(dest, "init", "-b", "main")
    CONFIG["projects"].insert(0, str(dest))
    CONFIG["hidden"] = [h for h in CONFIG["hidden"] if h != str(dest)]
    save_config()
    return describe(str(dest))


# ---------------------------------------------------------------- http

LOGIN_PAGE = """<!doctype html><meta charset=utf-8><title>Forge Studio</title>
<body style="background:#121216;color:#e6e4df;font:15px system-ui;display:grid;place-items:center;height:90vh">
<div><h2>Sign-in problem</h2><p>{msg}</p><p><a style="color:#e8906f" href="/">Back to Forge Studio</a></p></div>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost"):
            return True
        return getattr(self.server, "lan", False) and host == lan_ip()

    def _app_auth(self, qs):
        return secrets.compare_digest(self.headers.get("X-Token", "") or qs.get("token", [""])[0], TOKEN)

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url):
        self._send(302, b"", "text/plain", {"Location": url})

    # ------------------------------------------------ GET
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "") and "state" in qs:  # Google redirects back here
            try:
                if "error" in qs:
                    raise ValueError(qs["error"][0])
                login = google_finish(qs["code"][0], qs["state"][0])
                return self._redirect(f"/#token={TOKEN}&login={login}")
            except Exception as e:
                return self._send(400, LOGIN_PAGE.format(msg=str(e).replace("<", "&lt;")), "text/html; charset=utf-8")
        if u.path == "/api/bootstrap":
            # Hand the app token to pages opened on this computer only: loopback socket, localhost Host
            # header, not the LAN listener, and a same-origin request (other websites can't read it).
            local = self.client_address[0] in ("127.0.0.1", "::1") and not getattr(self.server, "lan", False)
            same_origin = self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"
            if local and same_origin:
                return self._send(200, {"token": TOKEN})
            return self._send(403, {"error": "Open the link from Settings → Web App on this device"})
        if u.path == "/auth/google":
            if not self._app_auth(qs):
                return self._send(401, {"error": "unauthorized"})
            try:
                return self._redirect(google_auth_url())
            except ValueError as e:
                return self._send(400, LOGIN_PAGE.format(msg=str(e)), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (STATIC_DIR / name).resolve()
            if STATIC_DIR not in f.parents or not f.is_file():
                return self._send(404, {"error": "not found"})
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
                     "png": "image/png", "webmanifest": "application/manifest+json", "json": "application/json"}.get(f.suffix[1:], "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        if not self._app_auth(qs):
            return self._send(401, {"error": "unauthorized"})
        try:
            if u.path == "/api/auth/profiles":
                return self._send(200, {"profiles": [public_profile(p) for p in CONFIG["profiles"]],
                                        "google": bool(google_cfg()), "redirect": ORIGIN})
            pid = login_profile(self.headers.get("X-Login") or qs.get("login", [""])[0])
            if not pid:
                return self._send(401, {"error": "login required", "login": True})
            prof = Profile.get(pid)
            if u.path == "/api/events":
                return self.sse(pid)
            if u.path == "/api/state":
                return self._send(200, self.state(prof))
            if u.path == "/api/chats":
                project = qs.get("project", [""])[0]
                chats = sorted((c for c in prof.data["chats"].values() if not project or c["project"] == project),
                               key=lambda c: c.get("updated", 0), reverse=True)
                return self._send(200, {"chats": [dict(c, running=c["id"] in RUNS and RUNS[c["id"]].running())
                                                  for c in chats]})
            if u.path == "/api/chat/log":
                cid = qs.get("id", [""])[0]
                if cid not in prof.data["chats"]:
                    return self._send(404, {"error": "chat not found"})
                f = prof.chat_log(cid)
                events = [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
                pend = [{"type": "approval", "id": k, "tool": v["tool"], "input": v["input"], "hints": risk_hints(v["tool"], v["input"], prof.data["chats"][cid]["project"])}
                        for k, v in APPROVALS.items() if v["chat"] == cid]
                return self._send(200, {"chat": prof.data["chats"][cid], "events": events, "pending": pend,
                                        "running": cid in RUNS and RUNS[cid].running()})
            if u.path == "/api/file":
                f = allowed_file(prof, qs.get("path", [""])[0])
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                hdr = {"Content-Disposition": ("attachment" if qs.get("dl") else "inline") + f"; filename*=UTF-8''{urllib.parse.quote(f.name)}"}
                return self._send(200, f.read_bytes(), ctype, hdr)
            if u.path == "/api/proc/log":
                rec = PROCS.get(qs.get("id", [""])[0])
                return self._send(200, {"lines": list(rec["log"]) if rec else []})
            if u.path == "/api/android/devices":
                return self._send(200, list_devices())
            if u.path == "/api/android/screenshot":
                r = adb("exec-out", "screencap", "-p", serial=qs.get("serial", [None])[0] or None, timeout=30)
                if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
                    return self._send(500, {"error": (r.stderr or r.stdout).decode(errors="replace")[:500] or "screencap failed"})
                return self._send(200, r.stdout, "image/png")
            if u.path == "/api/fs/list":
                d = Path(qs.get("path", [str(HOME)])[0]).expanduser()
                dirs = sorted([c.name for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")])
                return self._send(200, {"path": str(d), "parent": str(d.parent), "dirs": dirs})
            # ---- code editor & git (read-only)
            arg = lambda k, d="": qs.get(k, [d])[0]  # noqa: E731
            if u.path == "/api/code/tree":
                return self._send(200, code_list(arg("project"), arg("path")))
            if u.path == "/api/code/read":
                return self._send(200, code_read(arg("project"), arg("path")))
            if u.path == "/api/code/stat":
                root = project_root(arg("project"))
                out = {}
                for rel in filter(None, arg("paths").split("\n")):
                    f = project_file(arg("project"), rel)[1]
                    out[rel] = f.stat().st_mtime_ns if f.is_file() else None
                return self._send(200, {"mtimes": out, "root": str(root)})
            if u.path == "/api/code/files":
                return self._send(200, {"files": code_files(arg("project"))})
            if u.path == "/api/code/search":
                return self._send(200, code_search(arg("project"), arg("q"), arg("case") == "1", arg("regex") == "1"))
            if u.path == "/api/git/status":
                return self._send(200, git_status(arg("project")))
            if u.path == "/api/git/diff":
                return self._send(200, git_diff(arg("project"), arg("path"), arg("staged") == "1", arg("untracked") == "1"))
            if u.path == "/api/git/log":
                return self._send(200, git_log(arg("project")))
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def state(self, prof):
        env_path = tool_env()["PATH"]
        providers = [{k: (("•" * 8 + v[-4:]) if k == "apiKey" and v else v) for k, v in p.items()}
                     for p in prof.data["providers"]]
        return {
            "profile": public_profile(prof.pid),
            "projects": discover(),
            "tools": {"claude": bool(shutil.which("claude", path=env_path)), "adb": adb_path(),
                      "studio": find_studio(), "java": find_java_home(), "emulator": emulator_path(),
                      "scrcpy": scrcpy_path(), "node": shutil.which("node", path=env_path),
                      "sdk": str(find_sdk() or ""), "git": shutil.which("git", path=env_path)},
            "previews": {k: {"url": v["url"], "dir": v["dir"]} for k, v in PREVIEWS.items()},
            "running": [cid for cid, r in RUNS.items() if r.running() and cid in prof.data["chats"]],
            "agents": list_agents(),
            "prefs": prof.data["settings"],
            "web": web_access_info(),
            "current": prof.data["current"],
            "providers": providers,
            "procs": [{"id": r["id"], "kind": r["kind"], "project": r["project"], "label": r["label"],
                       "running": r["popen"].poll() is None, "meta": r["meta"]} for r in PROCS.values()],
            "settings": {"studio_path": CONFIG["settings"].get("studio_path"),
                         "google": {"clientId": (CONFIG["settings"].get("google") or {}).get("clientId", "")}},
        }

    # ------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if not self._app_auth(parse_qs(u.query)):
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        if u.path == "/api/upload":
            pid = login_profile(self.headers.get("X-Login"))
            if not pid:
                return self._send(401, {"error": "login required", "login": True})
            if n > 100 * 1024 * 1024:
                return self._send(413, {"error": "File is larger than 100 MB"})
            return self._send(200, save_upload(Profile.get(pid), parse_qs(u.query).get("name", ["file"])[0], self.rfile.read(n)))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        try:
            if u.path.startswith("/api/auth/"):
                return self._send(200, self.auth_route(u.path, body) or {"ok": True})
            pid = login_profile(self.headers.get("X-Login"))
            if not pid:
                return self._send(401, {"error": "login required", "login": True})
            return self._send(200, self.route(u.path, body, Profile.get(pid)) or {"ok": True})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def auth_route(self, path, b):
        if path == "/api/auth/local":  # create a local profile
            name = (b.get("name") or "").strip()[:40]
            if not name:
                raise ValueError("Enter a name")
            extra = {"pin": hash_pin(b["pin"])} if b.get("pin") else {}
            return {"login": issue_login(create_profile(name, "local", **extra))}
        if path == "/api/auth/login":
            p = CONFIG["profiles"].get(b.get("id"))
            if not p:
                raise ValueError("Profile not found")
            if p["kind"] == "google":
                raise ValueError("Use “Sign in with Google” for this profile")
            if p.get("pin") and not check_pin(b.get("pin") or "", p["pin"]):
                raise ValueError("Wrong PIN")
            return {"login": issue_login(b["id"])}
        if path == "/api/auth/logout":
            CONFIG["logins"].pop(sha(self.headers.get("X-Login") or ""), None)
            save_config()
            return None
        if path == "/api/auth/google-setup":
            cid, secret = (b.get("clientId") or "").strip(), (b.get("clientSecret") or "").strip()
            if not cid.endswith(".apps.googleusercontent.com") or not secret:
                raise ValueError("Paste the Client ID (…apps.googleusercontent.com) and the Client secret")
            CONFIG["settings"]["google"] = {"clientId": cid, "clientSecret": secret}
            save_config()
            return None
        raise ValueError(f"unknown endpoint {path}")

    def route(self, path, b, prof: Profile):
        project = b.get("project")
        if project and not Path(project).is_dir():
            raise ValueError("project folder not found")
        serial = b.get("serial") or None
        # ---- profile & providers
        if path == "/api/profile":
            p = CONFIG["profiles"][prof.pid]
            if "name" in b and b["name"].strip():
                p["name"] = b["name"].strip()[:40]
            if "pin" in b:
                if b["pin"]:
                    p["pin"] = hash_pin(b["pin"])
                else:
                    p.pop("pin", None)
            save_config()
            return public_profile(prof.pid)
        if path == "/api/profile/delete":
            for k in [k for k, v in CONFIG["logins"].items() if v["profile"] == prof.pid]:
                CONFIG["logins"].pop(k)
            CONFIG["profiles"].pop(prof.pid, None)
            save_config()
            shutil.rmtree(prof.dir, ignore_errors=True)
            Profile._cache.pop(prof.pid, None)
            return None
        if path == "/api/providers/save":
            p = b["provider"]
            if p.get("type") not in ("anthropic", "compatible"):
                raise ValueError("bad provider type")
            if p["type"] == "compatible" and not re.match(r"https?://", p.get("baseUrl", "")):
                raise ValueError("Base URL must start with http:// or https://")
            old = prof.provider(p.get("id")) if p.get("id") else None
            key = p.get("apiKey", "")
            if old and (not key or key.startswith("•")):
                key = old.get("apiKey", "")  # masked value sent back unchanged
            rec = {"id": p.get("id") or uuid.uuid4().hex[:10], "type": p["type"], "name": p.get("name") or "Provider",
                   "baseUrl": p.get("baseUrl", ""), "apiKey": key, "api": "openai" if p.get("api") == "openai" else "anthropic",
                   "models": [m.strip() for m in p.get("models", []) if m.strip()]}
            prof.data["providers"] = [x for x in prof.data["providers"] if x["id"] != rec["id"]] + [rec]
            prof.save()
            return {"id": rec["id"]}
        if path == "/api/providers/delete":
            prof.data["providers"] = [x for x in prof.data["providers"] if x["id"] != b["id"]]
            prof.save()
            return None
        # ---- projects & settings
        if path == "/api/projects/add":
            p = str(Path(b["path"]).expanduser().resolve())
            if not Path(p).is_dir():
                raise ValueError("Not a folder")
            if p not in CONFIG["projects"]:
                CONFIG["projects"].insert(0, p)
            if p in CONFIG["hidden"]:
                CONFIG["hidden"].remove(p)
            save_config()
            return describe(p)
        if path == "/api/projects/remove":
            if b["path"] in CONFIG["projects"]:
                CONFIG["projects"].remove(b["path"])
            if b["path"] not in CONFIG["hidden"]:
                CONFIG["hidden"].append(b["path"])
            save_config()
            return None
        if path == "/api/settings":
            if "studio_path" in b:
                CONFIG["settings"]["studio_path"] = b["studio_path"] or None
            save_config()
            return None
        # ---- chats
        if path == "/api/chat/send":
            cid = start_chat(prof, project, b.get("prompt", ""), b.get("chat"), b.get("model"), b.get("mode"),
                             b.get("provider"), b.get("files"), b.get("agent") or "claude")
            return {"chat": cid}
        if path == "/api/chat/stop":
            run = RUNS.get(b.get("chat"))
            if run:
                run.stop()
            return None
        if path == "/api/chat/approve":
            ap = APPROVALS.get(b["id"])
            if not ap or ap["chat"] not in prof.data["chats"]:
                raise ValueError("That request has already been answered")
            ap["run"].answer(b["id"], b["decision"], b.get("answers"), b.get("note"))
            return None
        if path == "/api/prefs":
            prof.data["settings"].update({k: v for k, v in b.items() if k in ("theme", "accent", "density", "mode", "agent")})
            prof.save()
            return prof.data["settings"]
        if path == "/api/agents/save":
            tpl = (b.get("template") or "").strip()
            if not tpl or not b.get("name"):
                raise ValueError("Enter a name and a command")
            agents = [a for a in CONFIG["settings"].get("agents", []) if a["id"] != b.get("id")]
            agents.append({"id": b.get("id") or "custom-" + uuid.uuid4().hex[:6], "name": b["name"][:40], "template": tpl})
            CONFIG["settings"]["agents"] = agents
            save_config()
            return None
        if path == "/api/agents/delete":
            CONFIG["settings"]["agents"] = [a for a in CONFIG["settings"].get("agents", []) if a["id"] != b["id"]]
            save_config()
            return None
        if path == "/api/web-access":
            if b.get("enabled"):
                start_lan()
            else:
                stop_lan()
            CONFIG["settings"]["lan"] = bool(b.get("enabled"))
            save_config()
            return web_access_info()
        if path == "/api/chat/select":
            if b.get("chat"):
                prof.data["current"][project] = b["chat"]
            else:
                prof.data["current"].pop(project, None)
            prof.save()
            return None
        if path == "/api/chat/rename":
            chat = prof.data["chats"].get(b["chat"])
            if chat:
                chat["title"] = b["title"].strip()[:100] or chat["title"]
                prof.save()
            return None
        if path == "/api/chat/delete":
            run = RUNS.get(b["chat"])
            if run:
                run.stop()
            chat = prof.data["chats"].pop(b["chat"], None)
            if chat:
                if prof.data["current"].get(chat["project"]) == b["chat"]:
                    prof.data["current"].pop(chat["project"], None)
                prof.save()
                prof.chat_log(b["chat"]).unlink(missing_ok=True)
                (prof.dir / "chats" / f"{b['chat']}.api.json").unlink(missing_ok=True)
            return None
        # ---- code editor & git
        if path == "/api/code/write":
            return code_write(project, b.get("path"), b.get("content", ""), b.get("mtime"), b.get("force"))
        if path == "/api/code/create":
            root, f = project_file(project, b.get("path"), change=True)
            if f.exists() or f.is_symlink():
                raise ValueError(f"{f.name} already exists")
            if b.get("dir"):
                f.mkdir(parents=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.touch()
            return {"path": rel_to(root, f)}
        if path == "/api/code/rename":
            root, src = project_file(project, b.get("from"), change=True)
            dst = project_file(project, b.get("to"), change=True)[1]
            if not (src.exists() or src.is_symlink()):
                raise ValueError("Nothing to rename")
            if dst.exists():
                raise ValueError(f"{dst.name} already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return {"path": rel_to(root, dst)}
        if path == "/api/code/delete":
            f = project_file(project, b.get("path"), change=True)[1]
            if not (f.exists() or f.is_symlink()):
                raise ValueError("Already deleted")
            return {"trashed": trash(f)}
        if path.startswith("/api/git/"):
            if path == "/api/git/clone":
                return clone_repo(b.get("url"), b.get("parent"), b.get("name"))
            if path == "/api/git/suggest-commit":
                return suggest_commit(project, prof, b.get("provider"), b.get("model"))
            return git_action(project, path.rsplit("/", 1)[1], b)
        if path == "/api/projects/new":
            return new_project(b.get("parent"), b.get("name"), b.get("git"))
        # ---- preview
        if path == "/api/preview/start":
            st = start_preview(project, b.get("dir") or project, (b.get("command") or "").strip() or None)
            return {"url": st["url"]}
        if path == "/api/preview/stop":
            stop_preview(project)
            return None
        if path == "/api/proc/kill":
            kill_proc(b["id"])
            return None
        if path == "/api/open":
            target = b.get("url") or b.get("path")
            subprocess.Popen(["xdg-open", target], start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return None
        # ---- android
        if path == "/api/android/studio":
            studio = find_studio()
            if not studio:
                raise ValueError("Android Studio not found - set its path in Settings.")
            subprocess.Popen([studio, project], start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=tool_env())
            return None
        if path == "/api/android/gradle":
            gradle(project, b["task"], serial)
            return None
        if path == "/api/android/run":
            app_id, module = b.get("applicationId"), b.get("module") or "app"

            def after(code):
                if code == 0 and app_id:
                    launch_app(app_id, serial)
                    if b.get("logcat", True):
                        try:
                            start_logcat(project, serial, app_id, "app")
                        except Exception as e:
                            emit({"type": "toast", "text": str(e), "err": True})
            gradle(project, f":{module}:installDebug", serial, then=after)
            return None
        if path == "/api/android/stop":
            adb("shell", "am", "force-stop", b["applicationId"], serial=serial)
            return None
        if path == "/api/android/restart":
            adb("shell", "am", "force-stop", b["applicationId"], serial=serial)
            launch_app(b["applicationId"], serial)
            return None
        if path == "/api/android/logcat":
            start_logcat(project, serial, b.get("applicationId"), b.get("scope", "app"))
            return None
        if path == "/api/android/logcat/clear":
            adb("logcat", "-c", serial=serial)
            return None
        if path == "/api/android/connect":
            addr = b["address"].strip()
            r = adb("pair", addr, b["code"].strip(), timeout=30) if b.get("code") else adb("connect", addr, timeout=30)
            return {"output": (r.stdout + r.stderr).decode(errors="replace").strip()}
        if path == "/api/android/emulator":
            em = emulator_path()
            if not em:
                raise ValueError("Emulator not installed (SDK Manager → Android Emulator)")
            start_proc([em, "-avd", b["avd"]], str(HOME), "emulator", project or "", f"emulator {b['avd']}")
            return None
        if path == "/api/android/reverse":
            r = adb("reverse", f"tcp:{int(b['port'])}", f"tcp:{int(b['port'])}", serial=serial)
            return {"output": (r.stdout + r.stderr).decode(errors="replace").strip() or "ok"}
        if path == "/api/android/input":
            kind = b.get("kind")
            if kind == "tap":
                args = ["tap", str(int(b["x"])), str(int(b["y"]))]
            elif kind == "swipe":
                args = ["swipe", *(str(int(b[k])) for k in ("x", "y", "x2", "y2")), str(int(b.get("ms", 250)))]
            elif kind == "key":
                args = ["keyevent", str(int(b["key"]))]
            elif kind == "text":
                args = ["text", re.sub(r"([\\\"'`$&|;<>()*?!#~ ])", r"\\\1", b["text"]).replace("\\ ", "%s")]
            else:
                raise ValueError("bad input kind")
            r = adb("shell", "input", *args, serial=serial)
            err = (r.stdout + r.stderr).decode(errors="replace")
            if "INJECT_EVENTS" in err:
                raise ValueError("The phone blocked the tap. On Xiaomi/Redmi: Settings → Additional settings → "
                                 "Developer options → turn on \"USB debugging (Security settings)\", then try again.")
            if r.returncode or "Exception" in err:
                raise ValueError(err.strip()[:300] or "input failed")
            return None
        if path == "/api/android/scrcpy":
            sc = scrcpy_path()
            if not sc:
                raise ValueError("scrcpy is not installed")
            env = tool_env()
            env["ADB"] = adb_path() or "adb"
            start_proc([sc, "--window-title", "Forge Studio device", "--stay-awake"] + (["-s", serial] if serial else []),
                       str(HOME), "scrcpy", project or "", "scrcpy mirror", env=env)
            return None
        raise ValueError(f"unknown endpoint {path}")

    def sse(self, pid):
        q = queue.Queue(maxsize=5000)
        entry = (q, pid)
        with _subs_lock:
            _subs.append(entry)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _subs_lock:
                _subs.remove(entry)
            self.close_connection = True


def shutdown(*_):
    for rec in list(PROCS.values()):
        if rec["kind"] not in ("emulator", "scrcpy"):
            kill_proc(rec["id"])
    for r in RUNS.values():
        r.stop()
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    if CONFIG["settings"].get("lan"):
        try:
            start_lan()
        except OSError as e:
            print("LAN access unavailable:", e, flush=True)
    print(f"Forge Studio running at {ORIGIN}/#token={TOKEN}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
