#!/usr/bin/env python3
"""Forge Studio - a local graphical front-end for the Claude Code CLI.

Standard library only. Serves a web UI on 127.0.0.1 and exposes a token-protected
JSON API for: per-profile chats with `claude` (history, external model providers),
website dev previews, and building / running / live-debugging Android projects via
Gradle + adb and Flutter projects via the flutter tool (hot reload over its daemon protocol). Profiles sign in with Google (optional) or a local name + PIN.
"""
import base64
import hashlib
import json
import mimetypes
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
            "kind": p["kind"], "hasPin": bool(p.get("pin")), "test": bool(p.get("test"))}


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

IS_MAC = sys.platform == "darwin"
TOOLS_DIR = CONFIG_DIR / "tools"  # JDK, Node.js… that Forge installs for the user (no root needed)
# where the Android SDK lives by default (also what Android Studio uses, so both share one SDK)
DEFAULT_SDK = HOME / "Library" / "Android" / "sdk" if IS_MAC else HOME / "Android" / "Sdk"
MAC_STUDIO = Path("/Applications/Android Studio.app/Contents")


def find_sdk():
    for p in [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), DEFAULT_SDK, HOME / "Android" / "Sdk"]:
        if p and Path(p).is_dir():
            return Path(p)
    return None


def find_studio():
    custom = CONFIG["settings"].get("studio_path")
    candidates = [custom, shutil.which("studio"), shutil.which("studio.sh"), shutil.which("android-studio"),
                  "/opt/apps/cn.android.studio/files/bin/studio", "/opt/android-studio/bin/studio.sh",
                  str(HOME / "android-studio" / "bin" / "studio.sh"), "/snap/bin/android-studio",
                  str(MAC_STUDIO / "MacOS" / "studio")]
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
        for sub in ("jbr", "jre", "jbr/Contents/Home"):
            if (root / sub / "bin" / "java").exists():
                return str(root / sub)
    if (TOOLS_DIR / "jdk" / "bin" / "java").exists():
        return str(TOOLS_DIR / "jdk")
    return None


def adb_path():
    sdk = find_sdk()
    if sdk and (sdk / "platform-tools" / "adb").exists():
        return str(sdk / "platform-tools" / "adb")
    return shutil.which("adb")


def scrcpy_path():
    for c in (HOME / ".local" / "scrcpy" / "scrcpy", shutil.which("scrcpy", path=tool_env()["PATH"])):
        if c and Path(c).exists():
            return str(c)
    return None


def emulator_path():
    sdk = find_sdk()
    if sdk and (sdk / "emulator" / "emulator").exists():
        return str(sdk / "emulator" / "emulator")
    return shutil.which("emulator")


FLUTTER_DIR = HOME / ".local" / "flutter"  # where Setup installs the Flutter SDK


def flutter_path():
    root = os.environ.get("FLUTTER_ROOT")
    for c in (root and Path(root) / "bin" / "flutter", FLUTTER_DIR / "bin" / "flutter", shutil.which("flutter"),
              HOME / "flutter" / "bin" / "flutter", HOME / "development" / "flutter" / "bin" / "flutter",
              HOME / "snap" / "flutter" / "common" / "flutter" / "bin" / "flutter", "/snap/bin/flutter",
              "/opt/flutter/bin/flutter", "/usr/local/flutter/bin/flutter", "/opt/homebrew/bin/flutter"):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def flutter_version():
    """{'version', 'channel', 'dart', 'root'} of the Flutter SDK, read from its cache (no slow `flutter --version`)."""
    fl = flutter_path()
    if not fl:
        return None
    root = Path(fl).resolve().parent.parent
    info = read_json(root / "bin" / "cache" / "flutter.version.json", {})
    ver = info.get("frameworkVersion") or info.get("flutterVersion")
    if not ver and (root / "version").is_file():
        ver = (root / "version").read_text().strip()
    return {"version": ver, "channel": info.get("channel"), "dart": info.get("dartSdkVersion"), "root": str(root), "path": fl}


def tool_env():
    env = dict(os.environ)
    sdk = find_sdk()
    if sdk:
        env.setdefault("ANDROID_HOME", str(sdk))
        env["PATH"] = f"{sdk}/platform-tools:{env.get('PATH', '')}"
    jh = find_java_home()
    if jh:
        env["JAVA_HOME"] = jh
    extra = [f"{HOME}/.local/node/bin", f"{HOME}/.local/bin"]
    fl = flutter_path()
    if fl:
        extra = [str(Path(fl).parent), f"{HOME}/.pub-cache/bin"] + extra
    if not IS_MAC and not env.get("CHROME_EXECUTABLE") and not shutil.which("google-chrome"):
        browser = next((b for b in map(shutil.which, ("chromium", "chromium-browser", "microsoft-edge", "brave-browser")) if b), None)
        if browser:  # Flutter only looks for google-chrome by itself
            env["CHROME_EXECUTABLE"] = browser
    if IS_MAC:  # apps started from the Dock don't get Homebrew's PATH
        extra += ["/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    env.pop("CLAUDECODE", None)  # let nested claude runs start normally
    return env


# ---------------------------------------------------------------- projects


def android_info(path: Path):
    if not ((path / "settings.gradle").exists() or (path / "settings.gradle.kts").exists()):
        return None
    info = {"gradlew": (path / "gradlew").exists(), "gradle": wrapper_version(path), "modules": []}
    for child in sorted(path.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue
        for f in ("build.gradle.kts", "build.gradle"):
            bf = child / f
            if bf.exists():
                text = bf.read_text(errors="ignore")
                if "com.android.application" in text or "android.application" in text:
                    m = re.search(r'applicationId\s*=?\s*["\']([\w.]+)["\']', text)
                    num = lambda k: (re.search(k + r'\s*=?\s*(\d+)', text) or [None, None])[1]  # noqa: E731
                    info["modules"].append({"name": child.name, "applicationId": m.group(1) if m else None,
                                            "minSdk": num("minSdk"), "targetSdk": num("targetSdk"), "compileSdk": num("compileSdk"),
                                            "compose": "compose = true" in text or "kotlin.compose" in text})
                break
    return info


def flutter_info(path: Path):
    """Flutter app/package facts from pubspec.yaml, or None for anything that doesn't depend on the Flutter SDK."""
    spec = path / "pubspec.yaml"
    if not spec.is_file():
        return None
    text = spec.read_text(errors="ignore")
    if not re.search(r"^\s+sdk:\s*['\"]?flutter\b", text, re.M):
        return None  # a plain Dart package
    app_id = None
    for f in ("build.gradle.kts", "build.gradle"):
        bf = path / "android" / "app" / f
        if bf.is_file():
            m = re.search(r'applicationId\s*=?\s*["\']([\w.]+)["\']', bf.read_text(errors="ignore"))
            app_id = m and m.group(1)
            break
    field = lambda k: (m := re.search(rf"^{k}:\s*['\"]?([^'\"\n#]+)", text, re.M)) and m.group(1).strip()  # noqa: E731
    return {"name": field("name"), "version": field("version"), "applicationId": app_id,
            "platforms": [p for p in FLUTTER_PLATFORMS if (path / p).is_dir()],
            "app": (path / "lib" / "main.dart").is_file(), "pubGet": (path / ".dart_tool" / "package_config.json").is_file(),
            "example": (path / "example" / "pubspec.yaml").is_file()}


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
    flutter = flutter_info(p)
    return {"path": str(p), "name": p.name, "android": None if flutter else android_info(p), "flutter": flutter,
            "web": [] if flutter else web_roots(p)}


def project_paths():
    found = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            markers = ["settings.gradle", "settings.gradle.kts", "pubspec.yaml", "package.json", "index.html", ".git", "CLAUDE.md"]
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


def start_proc(cmd, cwd, kind, project, label, shell=False, env=None, on_line=None, on_exit=None, meta=None,
               stdin=False, transform=None):
    """Run a background job whose output streams to the UI. `transform(line)` may rewrite each raw line into zero or
    more display lines (e.g. flutter's JSON daemon protocol); `stdin=True` keeps a pipe open to talk to the job."""
    pid = uuid.uuid4().hex[:10]
    popen = subprocess.Popen(cmd, cwd=cwd, shell=shell, env=env or tool_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, text=True,
                             bufsize=1, errors="replace", start_new_session=True)
    rec = {"id": pid, "kind": kind, "project": project, "label": label, "popen": popen,
           "log": deque(maxlen=5000), "started": time.time(), "code": None, "meta": meta or {}}
    PROCS[pid] = rec
    emit({"type": "proc_start", "proc": pid, "kind": kind, "project": project, "label": label, "meta": rec["meta"]})

    def pump():
        for raw in popen.stdout:
            raw = raw.rstrip("\n")
            try:
                lines = transform(raw) if transform else [raw]
            except Exception:
                lines = [raw]
            for line in lines:
                rec["log"].append(line)
                emit({"type": "log", "proc": pid, "kind": kind, "project": project, "line": line})
            if on_line:
                try:
                    on_line(raw)
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


# ---------------------------------------------------------------- plugins
# Standard layout (same as Claude Code plugins):
#   plugin-name/.claude-plugin/plugin.json  (required)   .mcp.json  commands/  agents/  skills/  README.md

PLUGINS_DIR = CONFIG_DIR / "plugins"
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def frontmatter(text):
    """Split a markdown file into ({key: value}, body). Only flat `key: value` lines are read."""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, sep, v = line.partition(":")
                if sep and k.strip() and not line.startswith((" ", "\t")):
                    meta[k.strip()] = v.strip().strip("\"'")
            body = text[end + 4:].lstrip("\n")
    return meta, body


def plugin_md_items(d: Path, skill=False):
    """Commands/agents (*.md, nested folders allowed) or skills (*/SKILL.md) under one plugin folder."""
    out = []
    if not d.is_dir():
        return out
    files = sorted(d.glob("*/SKILL.md")) if skill else sorted(d.rglob("*.md"))
    for f in files:
        try:
            meta, body = frontmatter(f.read_text(errors="replace"))
        except OSError:
            continue
        name = f.parent.name if skill else "/".join(f.relative_to(d).with_suffix("").parts)
        out.append({"name": meta.get("name") or name, "description": meta.get("description", ""),
                    "file": str(f), "body": body})
    return out


def read_plugin(d: Path):
    """Validate and describe one plugin folder. Raises ValueError with a readable message."""
    manifest = d / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        raise ValueError("Not a plugin: .claude-plugin/plugin.json is missing")
    try:
        meta = json.loads(manifest.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"plugin.json isn't valid JSON ({e})")
    if not isinstance(meta, dict):
        raise ValueError("plugin.json must be a JSON object")
    name = meta.get("name")
    if not isinstance(name, str) or not PLUGIN_NAME_RE.match(name):
        raise ValueError('plugin.json needs a "name" (lowercase letters, digits, - . _)')
    mcp, mcp_file = [], d / ".mcp.json"
    if mcp_file.is_file():
        try:
            m = json.loads(mcp_file.read_text())
            mcp = sorted((m.get("mcpServers") if isinstance(m.get("mcpServers"), dict) else m).keys())
        except (json.JSONDecodeError, AttributeError):
            raise ValueError(".mcp.json isn't valid JSON")
    return {"id": name, "name": name, "version": str(meta.get("version", "")), "description": str(meta.get("description", "")),
            "author": (meta.get("author") or {}).get("name", "") if isinstance(meta.get("author"), dict) else str(meta.get("author") or ""),
            "path": str(d), "readme": (d / "README.md").is_file(), "mcp": mcp,
            "commands": plugin_md_items(d / "commands"), "agents": plugin_md_items(d / "agents"),
            "skills": plugin_md_items(d / "skills", skill=True)}


def list_plugins(full=False):
    """Installed plugins. Broken ones are listed with an `error` so they can be removed from the UI."""
    off = set(CONFIG["settings"].get("plugins_off", []))
    out = []
    for d in sorted(PLUGINS_DIR.glob("*")) if PLUGINS_DIR.is_dir() else []:
        if not d.is_dir():
            continue
        try:
            p = read_plugin(d)
            p["dir"] = d.name
        except ValueError as e:
            p = {"id": d.name, "dir": d.name, "name": d.name, "error": str(e), "path": str(d), "commands": [],
                 "agents": [], "skills": [], "mcp": [], "version": "", "description": "", "author": "", "readme": False}
        p["enabled"] = d.name not in off and "error" not in p
        if not full:  # the UI only needs names and counts
            for k in ("commands", "agents", "skills"):
                p[k] = [{"name": i["name"], "description": i["description"]} for i in p[k]]
        out.append(p)
    return out


def enabled_plugins():
    return [p for p in list_plugins(full=True) if p["enabled"]]


def install_plugin(source):
    source = (source or "").strip()
    if not source:
        raise ValueError("Enter a plugin folder or a git URL")
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PLUGINS_DIR / (".incoming-" + uuid.uuid4().hex[:8])
    try:
        if re.match(r"^(https?://|git@|ssh://)", source) or source.endswith(".git"):
            r = subprocess.run(["git", "clone", "--depth", "1", "--", source, str(tmp)], capture_output=True, text=True,
                               timeout=180, env=dict(tool_env(), GIT_TERMINAL_PROMPT="0"))
            if r.returncode:
                raise ValueError("git clone failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
            shutil.rmtree(tmp / ".git", ignore_errors=True)
        else:
            src = Path(source).expanduser().resolve()
            if not src.is_dir():
                raise ValueError(f"{src} isn't a folder")
            if PLUGINS_DIR.resolve() in src.parents or src == PLUGINS_DIR.resolve():
                raise ValueError("That plugin is already installed")
            read_plugin(src)  # validate before copying anything
            if src in PLUGINS_DIR.resolve().parents:
                raise ValueError("That folder contains Forge Studio's own settings — pick the plugin's folder itself")
            shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"), symlinks=True)
        name = read_plugin(tmp)["name"]
        dest = PLUGINS_DIR / name
        if dest.exists():
            raise ValueError(f'A plugin named "{name}" is already installed — remove it first to reinstall')
        tmp.rename(dest)
        return name
    except subprocess.TimeoutExpired:
        raise ValueError("git clone timed out")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def plugin_dir_of(pid):
    if not PLUGIN_NAME_RE.match(pid or "") or not (PLUGINS_DIR / pid).is_dir():
        raise ValueError("Plugin not found")
    return PLUGINS_DIR / pid


def create_plugin(name, description=""):
    name = (name or "").strip().lower()
    if not PLUGIN_NAME_RE.match(name):
        raise ValueError("Use lowercase letters, digits, - . _ for the plugin name")
    d = PLUGINS_DIR / name
    if d.exists():
        raise ValueError(f'A plugin named "{name}" already exists')
    (d / ".claude-plugin").mkdir(parents=True)
    for sub in ("commands", "agents", "skills"):
        (d / sub).mkdir()
    write_json(d / ".claude-plugin" / "plugin.json", {"name": name, "version": "0.1.0", "description": description or f"The {name} plugin"})
    (d / "commands" / "hello.md").write_text("---\ndescription: Say hello (example command)\n---\nGreet the user and mention that this command came from the "
                                             f"{name} plugin. Extra input: $ARGUMENTS\n")
    (d / "README.md").write_text(f"# {name}\n\n{description or 'Describe what this plugin does.'}\n\n"
                                 "```\n.claude-plugin/plugin.json   metadata (required)\n.mcp.json                   MCP servers (optional)\n"
                                 "commands/                   slash commands (optional)\nagents/                     agent definitions (optional)\n"
                                 "skills/<skill>/SKILL.md     skills (optional)\n```\n")
    return name


def plugin_command(prompt):
    """Expand `/command args` (or `/plugin:command args`) from an enabled plugin. Used by the built-in engine;
    Claude Code does this itself. Returns the expanded prompt or None."""
    m = re.match(r"^/([\w.:/-]+)(?:\s+(.*))?$", prompt.strip(), re.S)
    if not m:
        return None
    want, args = m.group(1), (m.group(2) or "").strip()
    for p in enabled_plugins():
        for c in p["commands"]:
            if want in (c["name"], f"{p['name']}:{c['name']}"):
                body = c["body"]
                body = body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in body else (body + (f"\n\n{args}" if args else ""))
                return body
    return None


def plugin_system_note():
    """Skills and agents the built-in engine can read on demand (progressive disclosure, like Claude Code)."""
    lines = []
    for p in enabled_plugins():
        for s in p["skills"]:
            lines.append(f"- skill {s['name']} ({p['name']}): {s['description']} — read {s['file']} when relevant")
        for a in p["agents"]:
            lines.append(f"- agent {a['name']} ({p['name']}): {a['description']} — read {a['file']} and follow it as a role when relevant")
    return ("\n\nInstalled plugins provide these; read the file only when the task matches:\n" + "\n".join(lines)) if lines else ""


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
        self.opts = {"effort": None, "fast": False, "style": "agent"}
        self.text_buf = {"text": "", "sub": False}
        self.think_buf = {"text": "", "sub": False, "t0": None}

    # -- plumbing
    def send(self, ev, log=True):
        emit({"type": "chat", "chat": self.cid, "project": self.project, "event": ev}, profile=self.prof.pid)
        if log:
            self.prof.append(self.cid, ev)

    def flush(self):
        if self.text_buf["text"]:
            self.prof.append(self.cid, {"type": "text", "text": self.text_buf["text"], "sub": self.text_buf["sub"]})
            self.text_buf["text"] = ""

    def outputs_note(self):
        return "\n\n" + OUTPUTS_PROMPT.format(dir=chat_outputs(self.prof, self.cid))

    def think(self, text, sub=False):
        tb = self.think_buf
        if tb["t0"] is None:
            tb["t0"], tb["sub"] = time.time(), sub
        tb["text"] += text
        self.send({"type": "think_delta", "text": text, "sub": sub}, log=False)

    def flush_think(self):
        tb = self.think_buf
        if tb["t0"] is not None:
            ms = int((time.time() - tb["t0"]) * 1000)
            if tb["text"].strip():
                self.prof.append(self.cid, {"type": "thinking", "text": tb["text"], "sub": tb["sub"], "ms": ms})
            self.send({"type": "think_end", "ms": ms, "sub": tb["sub"]}, log=False)
        tb.update(text="", t0=None)

    def delta(self, text, sub=False):
        self.flush_think()
        if self.text_buf["text"] and self.text_buf["sub"] != sub:
            self.flush()
        self.text_buf["text"] += text
        self.text_buf["sub"] = sub
        self.send({"type": "delta", "text": text, "sub": sub}, log=False)

    def event(self, ev):
        self.flush_think()
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
        started = time.time() - 1
        try:
            code, err = target()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self.flush_think()
        self.flush()
        try:
            made = new_outputs(self.prof, self.cid, self.project, started)
            if made:
                self.send({"type": "outputs", "files": made})
        except Exception:
            pass
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
        env = dict(env, FORGE_OUTPUT_DIR=str(chat_outputs(self.prof, self.cid)))
        self.popen = subprocess.Popen(cmd, cwd=self.project, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1, errors="replace",
                                      start_new_session=True)
        err_lines = deque(maxlen=60)
        threading.Thread(target=lambda: [err_lines.append(l) for l in self.popen.stderr], daemon=True).start()
        return err_lines

    # -- Claude Code (native: streaming, approvals, images, providers)
    def run_claude(self):
        exe = shutil.which("claude", path=tool_env()["PATH"])
        cmd = [exe, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if claude_supports(exe, "--thinking-display"):
            cmd += ["--thinking-display", "summarized"]  # headless mode otherwise sends empty thinking blocks
        mode = self.mode or "acceptEdits"
        style = self.opts.get("style", "agent")
        if style == "plan":  # research and propose, no edits
            mode = "plan"
        elif style == "chat":  # just talk: no built-in tools and no MCP servers (incl. account connectors)
            cmd += ["--tools", "", "--strict-mcp-config"]
        if self.opts.get("effort") and claude_supports(exe, "--effort"):
            cmd += ["--effort", self.opts["effort"]]
        if self.opts.get("fast"):
            cmd += ["--settings", json.dumps({"fastMode": True})]
        out_dir = chat_outputs(self.prof, self.cid)
        extra = OUTPUTS_PROMPT.format(dir=out_dir)
        if style == "chat":
            extra = ("Chat mode: in this conversation you have no tools at all. Don't try to call or simulate tools (no "
                     "function-call markup); answer directly from what you know and what the user shares, and if you'd need to "
                     "look at files or run something, say so and suggest switching to Agent mode.")
        cmd += ["--add-dir", str(out_dir), "--append-system-prompt", extra]
        if mode == "ask":
            cmd += ["--permission-mode", "default", "--permission-prompt-tool", "stdio"]
        elif mode in ("acceptEdits", "auto", "plan"):
            cmd += ["--permission-mode", mode, "--permission-prompt-tool", "stdio"]
        else:
            cmd += ["--permission-mode", "bypassPermissions"]
        for p in (enabled_plugins() if style != "chat" else []):  # commands, agents, skills and .mcp.json load natively
            cmd += ["--plugin-dir", p["path"]]
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
            elif slim["type"] == "think_delta":
                self.think(slim["text"], slim["sub"])
            elif slim["type"] == "think_start":
                self.flush_think()
                self.flush()
                self.think_buf.update(t0=time.time(), sub=slim["sub"])
                self.send(slim, log=False)
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
        text = (plugin_command(self.prompt) or self.prompt) + (attachment_note(self.files) if self.files else "")
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
        body = {"model": model, "max_tokens": 8192, "stream": True, "system": BUILTIN_SYSTEM + plugin_system_note() + self.outputs_note(),
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
                "messages": [{"role": "system", "content": BUILTIN_SYSTEM + plugin_system_note() + self.outputs_note()}] + messages}
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
               files=None, agent_id="claude", opts=None):
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
    o = opts or {}
    run.opts = {"effort": o.get("effort") if o.get("effort") in ("low", "medium", "high", "xhigh", "max") else None,
                "fast": bool(o.get("fast")), "style": o.get("style") if o.get("style") in ("agent", "plan", "chat") else "agent"}
    RUNS[chat_id] = run
    run.start()
    emit({"type": "chats_changed", "project": project}, profile=prof.pid)
    return chat_id


TOOL_OUT_LIMIT = 60000  # keep tool output readable in the chat without unbounded log growth


def clip(s, n=TOOL_OUT_LIMIT):
    s = str(s)
    return s if len(s) <= n else s[:n] + f"\n… (+{len(s) - n:,} more characters — truncated)"


UI_TEXT_LIMIT = 16_000  # per text field when sending a chat log to the UI (the log on disk stays complete)


def trim_event(ev):
    """Shorten huge tool inputs/outputs for display: a 2.7 MB chat log becomes a fraction of that to load."""
    def cut(v):
        if isinstance(v, str) and len(v) > UI_TEXT_LIMIT:
            half = UI_TEXT_LIMIT // 2
            return f"{v[:half]}\n\n… {len(v) - UI_TEXT_LIMIT:,} characters not shown …\n\n{v[-half:]}"
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        if isinstance(v, list):
            return [cut(x) for x in v]
        return v
    return cut(ev) if ev.get("type") in ("tools", "tool_results", "approval") else ev


OUTPUTS_PROMPT = ("You are running inside Forge Studio. When the user asks for a file to download or keep (a zip, PDF, Word "
                  "or other document, spreadsheet, image, audio, an exported or converted file, etc.), save the finished file in "
                  "{dir} — Forge Studio shows everything there to the user with Download and Open buttons. You may use any "
                  "tool or script to produce it (e.g. Python's zipfile, pandoc, LibreOffice in headless mode). Mention the "
                  "file name in your reply.")
DELIVERABLE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".pdf", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods",
                   ".csv", ".pptx", ".odp", ".epub", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".mp3", ".wav",
                   ".ogg", ".mp4", ".webm", ".apk", ".aab", ".ipa", ".dmg", ".deb", ".exe", ".msi"}


APP_VERSION = "1.0.0"
ZIP_SKIP = {".git", "node_modules", "__pycache__", ".gradle", ".dart_tool", ".idea", ".kotlin"}


def zip_folder(d: Path, limit=1 << 30):
    """A folder as a .zip (in memory, up to 1 GB), skipping VCS data and dependency/cache folders."""
    import io
    import zipfile
    buf, total = io.BytesIO(), 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, names in os.walk(d):
            dirs[:] = [x for x in dirs if x not in ZIP_SKIP]
            for n in names:
                f = Path(base) / n
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
                if total > limit:
                    raise ValueError("That folder is over 1 GB; zip a smaller folder")
                z.write(f, f.relative_to(d.parent))
    return buf.getvalue()


IDE_ACTIONS = {"ask": "", "explain": "Explain this code: what it does, how it works and anything surprising.",
               "fix": "Find and fix the problems in this code. Explain what was wrong.",
               "tests": "Write tests for this code that cover the important behaviour and edge cases.", "open": None}


def ide_context(b):
    """Selection / file sent from a JetBrains IDE: register the project and hand the UI a ready-to-send prompt."""
    project = str(Path(b.get("project") or "").expanduser().resolve())
    if not b.get("project") or not Path(project).is_dir():
        raise ValueError("project folder not found")
    action = b.get("action") if b.get("action") in IDE_ACTIONS else "ask"
    if project not in project_paths():
        CONFIG["projects"].insert(0, project)
        CONFIG["hidden"] = [h for h in CONFIG["hidden"] if h != project]
        save_config()
    prompt = None
    if action != "open":
        f, sel = b.get("file"), (b.get("selection") or "")[:60000]
        where = ""
        if f:
            rel = os.path.relpath(f, project) if str(f).startswith(project) else f
            lines = f" (lines {b['startLine']}–{b['endLine']})" if b.get("startLine") and b.get("endLine") else ""
            where = f"In `{rel}`{lines}"
        lang = re.sub(r"[^\w+#.-]", "", (b.get("language") or "").lower())
        parts = [IDE_ACTIONS[action]] if IDE_ACTIONS[action] else []
        if sel.strip():
            parts.append(f"{where}:\n\n```{lang}\n{sel}\n```" if where else f"```{lang}\n{sel}\n```")
        elif where:
            parts.append(where + " (the whole file).")
        prompt = "\n\n".join(parts) + ("\n\n" if action == "ask" else "")
    with _subs_lock:
        windows = len(_subs)
    emit({"type": "ide_context", "project": project, "prompt": prompt, "send": False, "ide": b.get("ide") or "JetBrains IDE"})
    return {"ok": True, "delivered": windows}


def chat_outputs(prof, cid):
    d = prof.dir / "outputs" / cid
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_outputs(prof, cid, project, since):
    """Files made for the user during a turn: everything new in the chat's outputs folder, plus deliverable-type files
    (archives, documents, images, media, app packages) created or changed in the project."""
    found = []
    for f in sorted(chat_outputs(prof, cid).rglob("*")):
        if f.is_file() and f.stat().st_mtime >= since:
            found.append(f)
    root, seen = Path(project), 0
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            seen += 1
            f = Path(base) / n
            if f.suffix.lower() in DELIVERABLE_EXT:
                try:
                    if f.stat().st_mtime >= since:
                        found.append(f)
                except OSError:
                    pass
        if seen > 20000:
            break
    out = []
    for f in found[:40]:
        st = f.stat()
        out.append({"name": f.name, "path": str(f), "size": st.st_size, "kind": "image" if f.suffix.lower() in IMAGE_TYPES else "file",
                    "mime": mimetypes.guess_type(f.name)[0] or "application/octet-stream"})
    return out


_claude_flags = {}


def claude_supports(exe, flag):
    """Whether this Claude Code build knows a CLI flag (some aren't listed in --help). Cached per binary version."""
    try:
        real = Path(exe).resolve()
        key = (str(real), real.stat().st_mtime_ns, flag)
    except (OSError, TypeError):
        return False
    if key not in _claude_flags:
        needle, found, tail = flag.encode(), False, b""
        try:
            with open(real, "rb") as f:
                while chunk := f.read(1 << 22):
                    if needle in tail + chunk:
                        found = True
                        break
                    tail = chunk[-len(needle):]
        except OSError:
            pass
        _claude_flags[key] = found
    return _claude_flags[key]


def slim_event(ev):
    t = ev.get("type")
    sub = bool(ev.get("parent_tool_use_id"))
    if t == "stream_event":
        e = ev.get("event", {})
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
            return {"type": "delta", "text": e["delta"]["text"], "sub": sub}
        if e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "text":
            return {"type": "text_start", "sub": sub}
        # extended thinking: streamed like text, shown as a collapsible "Thinking…" block in the chat
        if e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "thinking":
            return {"type": "think_start", "sub": sub}
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "thinking_delta":
            return {"type": "think_delta", "text": e["delta"].get("thinking", ""), "sub": sub}
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


# ---- live screen stream: scrcpy's on-device H.264 encoder, relayed to the browser (decoded there with WebCodecs)

_scrcpy_version = {}


def scrcpy_server():
    """(server jar path, version) of an installed scrcpy, or (None, None)."""
    exe = scrcpy_path()
    cands = [os.environ.get("SCRCPY_SERVER_PATH"), exe and str(Path(exe).resolve().parent / "scrcpy-server"),
             "/usr/share/scrcpy/scrcpy-server", "/usr/local/share/scrcpy/scrcpy-server", "/opt/homebrew/share/scrcpy/scrcpy-server"]
    jar = next((c for c in cands if c and Path(c).is_file()), None)
    if not jar or not exe:
        return None, None
    if exe not in _scrcpy_version:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"scrcpy (\d+\.\d+(?:\.\d+)?)", r.stdout)
        _scrcpy_version[exe] = m.group(1) if m else None
    return jar, _scrcpy_version[exe]


def device_size(serial):
    out = adb("shell", "wm", "size", serial=serial).stdout.decode(errors="replace")
    m = re.findall(r"(\d+)x(\d+)", out)
    return m[-1] if m else None  # the override size, if any, is listed last


def open_screen_stream(serial, max_fps=120, max_size=1600, bitrate=8_000_000):
    """Start scrcpy-server on the device and return (socket, cleanup). The socket carries scrcpy's video stream."""
    jar, version = scrcpy_server()
    if not jar or not version:
        raise ValueError("scrcpy isn't installed — the live stream uses its on-device encoder")
    a = adb_path()
    target = ["-s", serial] if serial else []
    r = adb("push", jar, "/data/local/tmp/scrcpy-server.jar", serial=serial, timeout=30)
    if r.returncode:
        raise ValueError((r.stderr or r.stdout).decode(errors="replace")[:300] or "adb push failed")
    scid = secrets.randbelow(0x7FFFFFFF)
    port = free_port()
    adb("forward", f"tcp:{port}", f"localabstract:scrcpy_{scid:08x}", serial=serial)
    proc = subprocess.Popen([a, *target, "shell", "CLASSPATH=/data/local/tmp/scrcpy-server.jar", "app_process", "/",
                             "com.genymobile.scrcpy.Server", version, f"scid={scid:08x}", "tunnel_forward=true",
                             "audio=false", "control=false", f"max_fps={max_fps}", f"max_size={max_size}",
                             f"video_bit_rate={bitrate}", "video_codec=h264", "send_device_meta=false", "log_level=warn"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=tool_env())

    def cleanup():
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        adb("forward", "--remove", f"tcp:{port}", serial=serial)
    # the forward accepts before the server listens; scrcpy sends one dummy byte once it's really connected
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            cleanup()
            raise ValueError("The device refused to start the screen stream")
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            if s.recv(1):
                s.settimeout(None)
                return s, cleanup
            s.close()
        except OSError:
            pass
        time.sleep(0.1)
    cleanup()
    raise ValueError("Timed out starting the screen stream")


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
        raise ValueError("This project has no Gradle wrapper yet - press “Add Gradle wrapper” in Android mode.")
    os.chmod(p / "gradlew", 0o755)
    if running("gradle", project):
        raise ValueError("A Gradle task is already running for this project.")
    env = gradle_env(wrapper_version(p))
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


# ---------------------------------------------------------------- android: new apps & gradle wrapper

# A known-good toolchain for new apps: AGP 9 with its built-in Kotlin (needs Gradle 9 and JDK 17+).
GRADLE_VERSION = "9.6.0"
AGP_VERSION = "9.4.1"
KOTLIN_VERSION = "2.2.10"  # the Compose compiler plugin version; matches the Kotlin that AGP 9 bundles


def java_major(home):
    m = re.search(r'JAVA_VERSION="(?:1\.)?(\d+)', (Path(home) / "release").read_text()) if (Path(home) / "release").exists() else None
    return int(m.group(1)) if m else None


def jdk_homes():
    """Installed JDKs as {home: major}, from JAVA_HOME, Android Studio's JBR and the usual install folders."""
    cands = [os.environ.get("JAVA_HOME"), find_java_home(), str(TOOLS_DIR / "jdk")]
    for pattern in ("/usr/lib/jvm/*", "/opt/*/jbr", "/opt/apps/*/files/jbr", str(HOME / ".jdks/*"),
                    str(HOME / ".sdkman/candidates/java/*"), str(HOME / "android-studio/jbr"),
                    # macOS: system / user JDKs, Homebrew, and Android Studio's bundled JBR
                    "/Library/Java/JavaVirtualMachines/*/Contents/Home", str(HOME / "Library/Java/JavaVirtualMachines/*/Contents/Home"),
                    "/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home", "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
                    str(MAC_STUDIO / "jbr/Contents/Home")):
        cands += [str(p) for p in Path("/").glob(pattern.lstrip("/"))]
    out = {}
    for c in cands:
        if c and (Path(c) / "bin" / "java").exists():
            home = str(Path(c).resolve())
            if home not in out and (v := java_major(home)):
                out[home] = v
    return out


def java_for_gradle(gradle_version):
    """Newest JDK (17+) that this Gradle version can run on; Studio's bundled JBR is often too new for older Gradle."""
    v = tuple(int(x) for x in re.findall(r"\d+", gradle_version or GRADLE_VERSION)[:2]) or (8, 10)
    limit = 25 if v >= (9, 1) else 24 if v >= (8, 14) else 23 if v >= (8, 10) else 22 if v >= (8, 8) else 21 if v >= (8, 5) else 17
    ok = {h: m for h, m in jdk_homes().items() if 17 <= m <= limit}
    return max(ok, key=ok.get) if ok else find_java_home()


def gradle_env(gradle_version):
    env = tool_env()
    jh = java_for_gradle(gradle_version)
    if jh:
        env["JAVA_HOME"] = jh
        env["PATH"] = f"{jh}/bin:{env['PATH']}"
    return env


def gradle_launcher(want=GRADLE_VERSION):
    """A Gradle binary to generate wrappers with: an unpacked wrapper distribution (prefer `want`) or a system gradle."""
    dists = sorted((HOME / ".gradle" / "wrapper" / "dists").glob("gradle-*-bin/*/gradle-*/bin/gradle"))
    exact = [d for d in dists if d.parent.parent.name == f"gradle-{want}"]
    for cand in exact + dists:
        if (cand.parent.parent.parent / f"gradle-{cand.parent.parent.name[7:]}-bin.zip.ok").exists():
            return str(cand)
    return shutil.which("gradle", path=tool_env()["PATH"])


def android_platforms():
    sdk = find_sdk()
    found = []
    for p in (sdk / "platforms").glob("android-*") if sdk else []:
        m = re.fullmatch(r"android-(\d+)", p.name)
        if m and (p / "android.jar").exists():
            found.append(int(m.group(1)))
    return sorted(found)


def wrapper_version(project: Path):
    props = project / "gradle" / "wrapper" / "gradle-wrapper.properties"
    m = re.search(r"gradle-([\d.]+(?:-rc-\d+)?)-(?:bin|all)\.zip", props.read_text()) if props.exists() else None
    return m.group(1) if m else GRADLE_VERSION


def add_wrapper(project, version=None, then=None, label=None):
    """Add gradlew + gradle/wrapper to a project. Runs as a background step (see setup_wrapper): it downloads the
    wrapper files (~50 KB, checksum-verified) and falls back to a local Gradle when offline."""
    p = Path(project)
    version = version or wrapper_version(p)
    return start_proc([sys.executable, "-u", str(APP_DIR / "server.py"), "--setup", "wrapper", str(p), version], str(p),
                      "gradle", str(p), label or f"gradle wrapper {version}", on_exit=then)


def wrapper_from_local_gradle(p: Path, version):
    """Offline fallback: run a local Gradle's `wrapper` task in a scratch dir (so the Android build isn't configured)."""
    launcher = gradle_launcher(version)
    if not launcher:
        raise RuntimeError("Couldn't download the Gradle wrapper and no local Gradle was found. Check your internet connection.")
    scratch = Path(tempfile.mkdtemp(prefix="forge-wrapper-"))
    try:
        (scratch / "settings.gradle").write_text("rootProject.name = 'wrapper'\n")
        m = re.search(r"/gradle-([\d.]+)/bin/gradle$", launcher)
        r = subprocess.run([launcher, "wrapper", "--gradle-version", version, "--distribution-type", "bin", "--offline",
                            "--console=plain", "--no-daemon"], cwd=scratch, env=gradle_env(m.group(1) if m else version))
        if r.returncode:
            raise RuntimeError("the local Gradle couldn't create the wrapper")
        for rel in ("gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/gradle-wrapper.properties"):
            dst = p / rel
            if rel.endswith(".properties") and dst.exists():
                continue  # keep the project's own distribution settings
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scratch / rel, dst)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def new_android_app(parent, name, package, template="compose", min_sdk=24):
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z][\w -]{0,60}", name):
        raise ValueError("App name: letters, numbers, spaces, - or _ (start with a letter)")
    package = (package or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", package):
        raise ValueError("Package name like com.example.myapp (lowercase, at least two parts)")
    plats = android_platforms()
    if not plats:
        raise ValueError("The Android SDK isn't installed yet. Open Settings → Setup and install it (one click).")
    compile_sdk = max(plats)  # newest installed platform
    min_sdk = max(21, min(int(min_sdk or 24), compile_sdk))
    dest = Path(parent or HOME / "AndroidStudioProjects").expanduser().resolve() / re.sub(r"\s+", "", name)
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    compose = template != "views"
    files = android_template(name, package, compose, compile_sdk, min_sdk)
    for rel, text in files.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    sdk = find_sdk()
    (dest / "local.properties").write_text(f"sdk.dir={sdk}\n")
    if shutil.which("git"):
        git(dest, "init", "-b", "main", check=False)
    CONFIG["projects"].insert(0, str(dest))
    CONFIG["hidden"] = [h for h in CONFIG["hidden"] if h != str(dest)]
    save_config()

    def ready(code):
        if code == 0:
            emit({"type": "projects_changed", "select": str(dest), "text": f"{name} is ready — press Run to build it"})
        else:
            emit({"type": "toast", "text": f"Created {name}, but the Gradle wrapper step failed — see the build log", "err": True})
    add_wrapper(str(dest), GRADLE_VERSION, then=ready, label=f"new app {name}: gradle wrapper")
    return {"path": str(dest), "compileSdk": compile_sdk}


def android_template(name, package, compose, compile_sdk, min_sdk):
    pkg_dir = "app/src/main/java/" + package.replace(".", "/")
    theme = "Theme.Material3.DayNight.NoActionBar" if not compose else "android:Theme.Material.Light.NoActionBar"
    libs = f"""[versions]
agp = "{AGP_VERSION}"
kotlin = "{KOTLIN_VERSION}"
coreKtx = "1.12.0"
lifecycle = "2.7.0"
activityCompose = "1.8.2"
composeBom = "2024.05.00"
appcompat = "1.6.1"
material = "1.10.0"

[libraries]
androidx-core-ktx = {{ group = "androidx.core", name = "core-ktx", version.ref = "coreKtx" }}
androidx-lifecycle-runtime-ktx = {{ group = "androidx.lifecycle", name = "lifecycle-runtime-ktx", version.ref = "lifecycle" }}
androidx-activity-compose = {{ group = "androidx.activity", name = "activity-compose", version.ref = "activityCompose" }}
androidx-compose-bom = {{ group = "androidx.compose", name = "compose-bom", version.ref = "composeBom" }}
androidx-ui = {{ group = "androidx.compose.ui", name = "ui" }}
androidx-ui-graphics = {{ group = "androidx.compose.ui", name = "ui-graphics" }}
androidx-ui-tooling = {{ group = "androidx.compose.ui", name = "ui-tooling" }}
androidx-ui-tooling-preview = {{ group = "androidx.compose.ui", name = "ui-tooling-preview" }}
androidx-material3 = {{ group = "androidx.compose.material3", name = "material3" }}
androidx-appcompat = {{ group = "androidx.appcompat", name = "appcompat", version.ref = "appcompat" }}
material = {{ group = "com.google.android.material", name = "material", version.ref = "material" }}

[plugins]
android-application = {{ id = "com.android.application", version.ref = "agp" }}
kotlin-compose = {{ id = "org.jetbrains.kotlin.plugin.compose", version.ref = "kotlin" }}
"""
    root_build = """plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
"""
    settings = f"""pluginManagement {{
    repositories {{
        google()
        mavenCentral()
        gradlePluginPortal()
    }}
}}
dependencyResolutionManagement {{
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {{
        google()
        mavenCentral()
    }}
}}

rootProject.name = "{name}"
include(":app")
"""
    compose_bits = ("    alias(libs.plugins.kotlin.compose)\n", "\n    buildFeatures {\n        compose = true\n    }",
                    """    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    debugImplementation(libs.androidx.ui.tooling)
""") if compose else ("", "", "    implementation(libs.androidx.appcompat)\n    implementation(libs.material)\n")
    app_build = f"""plugins {{
    alias(libs.plugins.android.application)
{compose_bits[0]}}}

android {{
    namespace = "{package}"
    compileSdk = {compile_sdk}

    defaultConfig {{
        applicationId = "{package}"
        minSdk = {min_sdk}
        targetSdk = {compile_sdk}
        versionCode = 1
        versionName = "1.0"
    }}

    buildTypes {{
        release {{
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }}
    }}
    compileOptions {{
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }}{compose_bits[1]}
}}

dependencies {{
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
{compose_bits[2]}}}
"""
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <application
        android:allowBackup="true"
        android:icon="@mipmap/ic_launcher"
        android:label="@string/app_name"
        android:roundIcon="@mipmap/ic_launcher"
        android:supportsRtl="true"
        android:theme="@style/Theme.App">
        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>

</manifest>
"""
    if compose:
        activity = f"""package {package}

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview

class MainActivity : ComponentActivity() {{
    override fun onCreate(savedInstanceState: Bundle?) {{
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {{
            MaterialTheme {{
                Scaffold(modifier = Modifier.fillMaxSize()) {{ padding ->
                    Greeting(Modifier.padding(padding))
                }}
            }}
        }}
    }}
}}

@Composable
fun Greeting(modifier: Modifier = Modifier) {{
    var taps by remember {{ mutableIntStateOf(0) }}
    Column(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {{
        Text("Hello from {name}!", style = MaterialTheme.typography.headlineMedium)
        Button(onClick = {{ taps++ }}) {{ Text("Tapped $taps times") }}
    }}
}}

@Preview(showBackground = true)
@Composable
fun GreetingPreview() {{
    MaterialTheme {{ Greeting() }}
}}
"""
    else:
        activity = f"""package {package}

import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {{
    private var taps = 0

    override fun onCreate(savedInstanceState: Bundle?) {{
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        val label = findViewById<TextView>(R.id.label)
        findViewById<Button>(R.id.button).setOnClickListener {{
            taps++
            label.text = getString(R.string.tapped, taps)
        }}
    }}
}}
"""
    files = {
        "settings.gradle.kts": settings,
        "build.gradle.kts": root_build,
        "gradle/libs.versions.toml": libs,
        "gradle.properties": "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\nandroid.useAndroidX=true\n"
                             "kotlin.code.style=official\nandroid.nonTransitiveRClass=true\n",
        ".gitignore": "*.iml\n.gradle/\n/local.properties\n/.idea/\n.DS_Store\n/build/\n/captures/\n.externalNativeBuild/\n.cxx/\n",
        "app/.gitignore": "/build\n",
        "app/build.gradle.kts": app_build,
        "app/proguard-rules.pro": "# Add project specific ProGuard rules here.\n",
        "app/src/main/AndroidManifest.xml": manifest,
        f"{pkg_dir}/MainActivity.kt": activity,
        "app/src/main/res/values/strings.xml": f'<resources>\n    <string name="app_name">{name}</string>\n'
                                                 '    <string name="tapped">Tapped %1$d times</string>\n</resources>\n',
        "app/src/main/res/values/themes.xml": f'<resources>\n    <style name="Theme.App" parent="{theme}" />\n</resources>\n',
        "app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml":
            '<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <background android:drawable="@color/ic_launcher_background" />\n'
            '    <foreground android:drawable="@drawable/ic_launcher_foreground" />\n</adaptive-icon>\n',
        "app/src/main/res/values/colors.xml": '<resources>\n    <color name="ic_launcher_background">#0A84FF</color>\n</resources>\n',
        "app/src/main/res/drawable/ic_launcher_foreground.xml":
            '<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="108dp" android:height="108dp"\n'
            '    android:viewportWidth="108" android:viewportHeight="108">\n'
            '    <path android:fillColor="#FFFFFF" android:pathData="M40,72V42a4,4 0,0 1,4 -4h22v8H48v8h14v8H48v10z" />\n</vector>\n',
        "README.md": f"# {name}\n\nCreated with Forge Studio. Build with `./gradlew assembleDebug`, install with `./gradlew installDebug`.\n",
    }
    if min_sdk < 26:  # adaptive icons need API 26; give older devices a plain fallback
        files["app/src/main/res/drawable/ic_launcher_legacy.xml"] = files["app/src/main/res/drawable/ic_launcher_foreground.xml"]
        files["app/src/main/res/mipmap-anydpi/ic_launcher.xml"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n<layer-list xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <item android:drawable="@color/ic_launcher_background" />\n'
            '    <item android:drawable="@drawable/ic_launcher_legacy" />\n</layer-list>\n')
    if not compose:
        files["app/src/main/res/layout/activity_main.xml"] = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:gravity="center"
    android:orientation="vertical"
    android:padding="24dp">

    <TextView
        android:id="@+id/label"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:text="@string/app_name"
        android:textAppearance="?attr/textAppearanceHeadlineMedium" />

    <Button
        android:id="@+id/button"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_marginTop="16dp"
        android:text="Tap me" />
</LinearLayout>
"""
    return files


# ---------------------------------------------------------------- flutter
# `flutter run --machine` speaks the Flutter daemon protocol (one JSON message per line) on stdin/stdout, the same way
# IDEs drive it. We translate its events into readable log lines and send it hot reload / restart / stop commands.

FLUTTER_PLATFORMS = ("android", "ios", "web", "linux", "macos", "windows")
FLUTTER_RUNS = {}  # project -> run state (see flutter_run)
FLUTTER_EXTS = {"debugPaint": "ext.flutter.debugPaint", "performanceOverlay": "ext.flutter.showPerformanceOverlay",
                "debugBanner": "ext.flutter.debugAllowBanner", "inspector": "ext.flutter.inspector.show",
                "slowAnimations": "ext.flutter.timeDilation"}


def need_flutter():
    fl = flutter_path()
    if not fl:
        raise ValueError("The Flutter SDK isn't installed yet. Open Settings → Setup and install it (one click).")
    return fl


def flutter_env(project):
    """Flutter builds Android apps with Gradle, so give it a JDK that the project's Gradle version supports."""
    android = Path(project) / "android"
    return gradle_env(wrapper_version(android)) if android.is_dir() else tool_env()


def flutter_devices():
    fl = flutter_path()
    if not fl:
        return {"devices": [], "error": None, "installed": False}
    try:
        r = subprocess.run([fl, "devices", "--machine"], capture_output=True, text=True, timeout=120, env=tool_env())
    except subprocess.TimeoutExpired:
        return {"devices": [], "error": "`flutter devices` timed out", "installed": True}
    m = re.search(r"^\[", r.stdout, re.M)  # a first-run banner may come before the JSON
    try:
        found = json.loads(r.stdout[m.start():]) if m else []
    except ValueError:
        found = []
    if not m and r.returncode:
        return {"devices": [], "error": (r.stderr or r.stdout).strip()[-400:], "installed": True}
    devices = [{"id": d["id"], "name": d.get("name") or d["id"], "platform": d.get("targetPlatform", ""),
                "emulator": bool(d.get("emulator")), "sdk": d.get("sdk"), "supported": d.get("isSupported", True)}
               for d in found]
    if not any(d["id"] == "web-server" for d in devices):  # hidden by `flutter devices`, but always available
        devices.append({"id": "web-server", "name": "Web server (opens in Preview)", "platform": "web-javascript",
                        "emulator": False, "sdk": "Flutter web", "supported": True})
    return {"devices": devices, "error": None, "installed": True}


def flutter_event(st, raw):
    """Turn one line of `flutter run --machine` output into display lines (and act on the events we care about)."""
    s = raw.strip()
    if not (s.startswith("[{") and s.endswith("}]")):
        return [raw]
    try:
        msg = json.loads(s)[0]
    except ValueError:
        return [raw]
    ev, p, project = msg.get("event"), msg.get("params") or {}, st["project"]
    if not ev and "id" in msg:  # the reply to one of our commands
        what = st["pending"].pop(msg["id"], "command")
        res, err = msg.get("result"), msg.get("error")
        if isinstance(res, dict) and res.get("code") not in (0, None):
            err = res.get("message") or f"{what} failed"
        if err:
            emit({"type": "flutter_state", "project": project, "running": True, "event": "error", "text": f"{what} failed"})
            return [f"✗ {what} failed: {err}" if isinstance(err, str) else f"✗ {what} failed: {json.dumps(err)}"]
        if what in ("Hot reload", "Hot restart"):
            emit({"type": "flutter_state", "project": project, "running": True, "event": "reloaded", "text": f"{what} done"})
            return [f"↻ {what} done" + (f" — {res['message']}" if isinstance(res, dict) and res.get("message") else "")]
        return []
    if ev == "app.start":
        st["app"] = p.get("appId")
        return [f"Launching {Path(p.get('directory') or project).name} on {p.get('deviceId')} ({p.get('mode', 'debug')} mode)…"]
    if ev == "app.progress":
        return [p["message"]] if p.get("message") and not p.get("finished") else []
    if ev == "app.log":
        return (p.get("log") or "").splitlines() + (p.get("stackTrace") or "").splitlines()
    if ev == "daemon.logMessage":
        return [] if p.get("level") == "trace" else (p.get("message") or "").splitlines()
    if ev == "app.debugPort":
        st["vm"] = p.get("wsUri")
        return [f"Dart VM service: {p.get('wsUri')}"]
    if ev == "app.started":
        st["started"] = True
        emit({"type": "flutter_state", "project": project, "running": True, "event": "started", "device": st["device"]})
        return ["✓ App running. Hot reload with ⚡ or by saving a .dart file; hot restart with ⟳."]
    if ev == "app.webLaunchUrl":
        st["url"] = p.get("url")
        if st["url"]:
            emit({"type": "preview_url", "project": project, "url": st["url"], "flutter": True})
        return [f"Web app: {st['url']}"]
    if ev == "app.stop":
        st["started"] = False
        return ["Application stopped." + (f" {p['error']}" if p.get("error") else "")]
    if ev in ("daemon.connected", "app.dtd"):
        return []
    if p.get("uri"):
        return [f"{ev}: {p['uri']}"]
    return []


def flutter_send(project, method, params=None, what=None):
    st = FLUTTER_RUNS.get(project)
    if not st or st["rec"]["popen"].poll() is not None:
        raise ValueError("The app isn't running — press Run first.")
    if not st["app"]:
        raise ValueError("The app is still starting — try again in a moment.")
    with st["lock"]:
        st["seq"] += 1
        st["pending"][st["seq"]] = what or method
        st["rec"]["popen"].stdin.write(json.dumps([{"id": st["seq"], "method": method,
                                                    "params": {"appId": st["app"], **(params or {})}}]) + "\n")
        st["rec"]["popen"].stdin.flush()


def flutter_reload(project, full=False, reason="manual"):
    if not FLUTTER_RUNS.get(project, {}).get("started"):
        raise ValueError("The app is still starting — try again in a moment.")
    flutter_send(project, "app.restart", {"fullRestart": bool(full), "pause": False, "reason": reason},
                 "Hot restart" if full else "Hot reload")


def flutter_watch(st):
    """Hot reload when a .dart file under lib/ changes — saved in the editor or written by the agent."""
    lib = Path(st["project"]) / "lib"
    prev = snapshot(lib)
    while st["rec"]["popen"].poll() is None:
        time.sleep(0.7)
        cur = snapshot(lib)
        if cur == prev:
            continue
        changed = [f for f in set(cur) | set(prev) if cur.get(f) != prev.get(f)]
        prev = cur
        if st["auto"] and st["started"] and any(f.endswith(".dart") for f in changed):
            try:
                flutter_reload(st["project"], reason="save")
            except Exception:
                pass


def flutter_run(project, device, mode="debug", auto=True):
    fl = need_flutter()
    if running("flutter", project):
        raise ValueError("The app is already running — use hot reload or restart, or stop it first.")
    if not device:
        raise ValueError("Pick a device first")
    mode = mode if mode in ("debug", "profile", "release") else "debug"
    cmd = [fl, "run", "--machine", "-d", device, f"--{mode}"]
    if device == "web-server":
        cmd += ["--web-hostname", "127.0.0.1", "--web-port", str(free_port())]
    st = {"project": project, "device": device, "app": None, "started": False, "seq": 0, "pending": {}, "auto": bool(auto),
          "vm": None, "url": None, "lock": threading.Lock()}

    def done(code):
        if FLUTTER_RUNS.get(project) is st:
            FLUTTER_RUNS.pop(project, None)
        emit({"type": "flutter_state", "project": project, "running": False, "code": code})
    st["rec"] = start_proc(cmd, project, "flutter", project, f"flutter run · {device}", env=flutter_env(project), stdin=True,
                           transform=lambda line: flutter_event(st, line), on_exit=done, meta={"device": device, "mode": mode})
    FLUTTER_RUNS[project] = st
    if (Path(project) / "lib").is_dir():
        threading.Thread(target=flutter_watch, args=(st,), daemon=True).start()


def flutter_stop(project):
    st = FLUTTER_RUNS.get(project)
    rec = st and st["rec"]
    if not rec or rec["popen"].poll() is not None:
        return
    try:
        if st["app"]:
            flutter_send(project, "app.stop", what="Stop")
            threading.Timer(6, lambda: kill_proc(rec["id"])).start()  # in case the device doesn't answer
            return
    except Exception:
        pass
    kill_proc(rec["id"])


def flutter_ext(project, name, enabled):
    """Toggle a Flutter debug service extension (debug paint, performance overlay, …) in the running app."""
    if name not in FLUTTER_EXTS:
        raise ValueError("unknown debug option")
    if FLUTTER_RUNS.get(project, {}).get("device") == "web-server":
        raise ValueError("Debug tools need a device with a Dart VM service (Android, desktop or Chrome), not the Web server device.")
    params = {"timeDilation": "5.0" if enabled else "1.0"} if name == "slowAnimations" else {"enabled": "true" if enabled else "false"}
    flutter_send(project, "app.callServiceExtension", {"methodName": FLUTTER_EXTS[name], "params": params}, name)


def flutter_task(project, args):
    """`flutter …` or `dart …` in the project, shown in the Build output."""
    fl = need_flutter()
    argv = shlex.split(args or "")
    tool = fl
    if argv and argv[0] in ("flutter", "dart"):
        tool = fl if argv[0] == "flutter" else str(Path(fl).parent / "dart")
        argv = argv[1:]
    if not argv:
        raise ValueError("Type a flutter command, e.g. pub get or build apk")
    if running("flutter-task", project):
        raise ValueError("A Flutter task is already running for this project.")
    name = "dart" if tool != fl else "flutter"
    then = None
    if argv[0] in ("create", "pub"):  # new platform folders / dependencies change what the UI shows
        then = lambda code: code == 0 and emit({"type": "projects_changed"})  # noqa: E731
    return start_proc([tool, *argv], project, "flutter-task", project, f"{name} {' '.join(argv)}", env=flutter_env(project),
                      on_exit=then)


_devtools = {}


def flutter_devtools(project):
    """URL of Dart DevTools connected to the running app (starts one DevTools server per session)."""
    st = FLUTTER_RUNS.get(project)
    if st and st["device"] == "web-server":
        raise ValueError("DevTools needs a device with a Dart VM service (Android, desktop or Chrome), not the Web server device.")
    if not st or not st.get("vm"):
        raise ValueError("Run the app in debug or profile mode first — DevTools connects to it.")
    rec = _devtools.get("rec")
    if not rec or rec["popen"].poll() is not None:
        _devtools.clear()
        ready = threading.Event()

        def seen(line):
            m = re.search(r"(https?://[\w.:\[\]-]+/?)", line)
            if m and "DevTools" in line:
                _devtools["url"] = m.group(1).rstrip("/.")
                ready.set()
        _devtools["rec"] = start_proc([str(Path(need_flutter()).parent / "dart"), "devtools", "--no-launch-browser"], str(HOME),
                                      "devtools", "", "Dart DevTools", on_line=seen)
        if not ready.wait(60):
            raise ValueError("DevTools didn't start — see Processes for its log")
    return {"url": f"{_devtools['url']}/?uri={urllib.parse.quote(st['vm'], safe='')}"}


def new_flutter_app(parent, name, org, template="app", platforms=None):
    fl = need_flutter()
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z][\w -]{0,60}", name):
        raise ValueError("App name: letters, numbers, spaces, - or _ (start with a letter)")
    pname = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")
    org = (org or "com.example").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", org):
        raise ValueError("Organization like com.example (lowercase, at least two parts)")
    plats = [p for p in FLUTTER_PLATFORMS if p in (platforms or [])] or ["android", "ios", "web"]
    dest = Path(parent or HOME / "projects").expanduser().resolve() / pname
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [fl, "create", "--org", org, "--project-name", pname, "--platforms", ",".join(plats), "--no-pub"]
    if template == "empty":
        cmd.append("--empty")
    cmd.append(str(dest))

    def ready(code):
        if code or not (dest / "pubspec.yaml").exists():
            return emit({"type": "toast", "text": f"Creating {name} failed — see Processes for the log", "err": True})
        flutter_label(dest, pname, name)
        if shutil.which("git"):
            git(dest, "init", "-b", "main", check=False)
        CONFIG["projects"].insert(0, str(dest))
        CONFIG["hidden"] = [h for h in CONFIG["hidden"] if h != str(dest)]
        save_config()
        emit({"type": "projects_changed", "select": str(dest), "text": f"{name} is ready — fetching packages, then press Run"})
        flutter_task(str(dest), "pub get")
    start_proc(cmd, str(dest.parent), "flutter-task", str(dest), f"new Flutter app {name}", env=tool_env(), on_exit=ready)
    return {"path": str(dest), "platforms": plats}


def flutter_label(dest: Path, pname, label):
    """`flutter create` names the app after the package (my_app); show the real name on the home screen / tab."""
    if label == pname:
        return
    edits = {"android/app/src/main/AndroidManifest.xml": (f'android:label="{pname}"', f'android:label="{label}"'),
             "web/index.html": (f"<title>{pname}</title>", f"<title>{label}</title>"),
             "web/manifest.json": (f'"name": "{pname}"', f'"name": "{label}"'),
             "linux/runner/my_application.cc": (f'"{pname}"', f'"{label}"')}
    for rel, (old, new) in edits.items():
        f = dest / rel
        if f.is_file():
            f.write_text(f.read_text().replace(old, new))
    plist = dest / "ios" / "Runner" / "Info.plist"
    if plist.is_file():
        plist.write_text(re.sub(r"(<key>CFBundleDisplayName</key>\s*<string>)[^<]*", lambda m: m.group(1) + label, plist.read_text()))


# ---------------------------------------------------------------- toolchain setup (first-launch checklist)
# Everything here installs into the user's home (no root) from official sources, resolving the *latest* version at
# install time and verifying the published checksum. Each installer runs as `server.py --setup <item>` so its output
# streams into Processes like any other job.

def cpu_arch():
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64") else m


def http_get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ForgeStudio"}), timeout=timeout)


def fetch_json(url):
    with http_get(url) as r:
        return json.loads(r.read())


def fetch_text(url):
    with http_get(url) as r:
        return r.read().decode()


def download(url, dest, digest=None, algo="sha256"):
    """Stream a download to `dest` with progress lines; verify the checksum when the publisher provides one."""
    h, done, last = hashlib.new(algo), 0, 0.0
    with http_get(url, timeout=120) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        while chunk := r.read(1 << 16):
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if time.time() - last > 2:
                last = time.time()
                print(f"  {Path(dest).name}: {done >> 20} MB" + (f" of {total >> 20} MB" if total else ""), flush=True)
    if digest and h.hexdigest().lower() != digest.strip().lower():
        raise RuntimeError(f"checksum mismatch for {Path(dest).name}; the download may be corrupted, please try again")
    print(f"  downloaded {Path(dest).name} ({done / 1048576:.1f} MB)" + (", checksum verified" if digest else ""), flush=True)


def extract(archive, dest):
    """Unpack .zip / .tar.* keeping executable bits (zipfile drops them) and symlinks."""
    import tarfile
    import zipfile
    dest.mkdir(parents=True, exist_ok=True)
    if str(archive).endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = z.extract(info, dest)
                mode = info.external_attr >> 16
                if mode & 0o777:
                    os.chmod(path, mode & 0o777)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, **({"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}))
    return dest


def replace_dir(src: Path, dest: Path):
    if dest.exists() or dest.is_symlink():
        shutil.rmtree(dest) if dest.is_dir() and not dest.is_symlink() else dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def setup_wrapper(project, version):
    """Gradle wrapper files without installing Gradle: the jar comes from Gradle's own repository at the release tag
    and must match the official checksum services.gradle.org publishes for that wrapper."""
    p = Path(project)
    try:
        raw = f"https://raw.githubusercontent.com/gradle/gradle/v{version}"
        with http_get(f"{raw}/gradle/wrapper/gradle-wrapper.jar") as r:
            jar = r.read()
        props = fetch_text(f"{raw}/gradle/wrapper/gradle-wrapper.properties")
        jar_ver = re.search(r"gradle-([\w.\-]+?)-(?:bin|all)\.zip", props).group(1)
        want = fetch_text(f"https://services.gradle.org/distributions/gradle-{jar_ver}-wrapper.jar.sha256").strip()
        if hashlib.sha256(jar).hexdigest() != want:
            raise RuntimeError("the Gradle wrapper jar didn't match its official checksum")
        scripts = {n: fetch_text(f"{raw}/{n}") for n in ("gradlew", "gradlew.bat")}
    except Exception as e:  # offline, GitHub blocked, unknown tag…
        print(f"Downloading the wrapper failed ({e}); trying a local Gradle instead", flush=True)
        return wrapper_from_local_gradle(p, version)
    (p / "gradle" / "wrapper").mkdir(parents=True, exist_ok=True)
    (p / "gradle" / "wrapper" / "gradle-wrapper.jar").write_bytes(jar)
    props_file = p / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if not props_file.exists():
        props_file.write_text("distributionBase=GRADLE_USER_HOME\ndistributionPath=wrapper/dists\n"
                              f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n"
                              "networkTimeout=10000\nvalidateDistributionUrl=true\n"
                              "zipStoreBase=GRADLE_USER_HOME\nzipStorePath=wrapper/dists\n")
    for name, text in scripts.items():
        (p / name).write_text(text)
    os.chmod(p / "gradlew", 0o755)
    print(f"Gradle wrapper {version} added (jar checksum verified). Gradle itself downloads on the first build.", flush=True)


def setup_jdk():
    """Latest Eclipse Temurin JDK 21 (LTS) from Adoptium into ~/.config/forge-studio/tools/jdk."""
    os_name = "mac" if IS_MAC else "linux"
    rel = fetch_json(f"https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture={cpu_arch()}"
                     f"&image_type=jdk&os={os_name}&vendor=eclipse")
    if not rel:
        raise RuntimeError(f"Adoptium has no JDK 21 build for {os_name}/{cpu_arch()}")
    pkg = rel[0]["binary"]["package"]
    print(f"Installing {rel[0]['release_name']} ({os_name} {cpu_arch()})", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / pkg["name"]
        download(pkg["link"], f, pkg["checksum"])
        top = next(extract(f, Path(tmp) / "x").iterdir())
        home = top / "Contents" / "Home" if (top / "Contents" / "Home").is_dir() else top
        replace_dir(home, TOOLS_DIR / "jdk")
    print(f"JDK installed in {TOOLS_DIR / 'jdk'}", flush=True)


def tools_java():
    """A JDK 17+ for the Android SDK tools (sdkmanager), newest first."""
    ok = {h: m for h, m in jdk_homes().items() if m >= 17}
    return max(ok, key=ok.get) if ok else None


def setup_android_sdk():
    """Google's command-line tools (latest), then the newest stable platform-tools, build-tools and Android platform."""
    import xml.etree.ElementTree as ET
    jh = tools_java()
    if not jh:
        print("No JDK 17+ found, so installing one first (the SDK tools need Java)", flush=True)
        setup_jdk()
        jh = str(TOOLS_DIR / "jdk")
    sdk = find_sdk() or DEFAULT_SDK
    sdk.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, JAVA_HOME=jh, ANDROID_HOME=str(sdk), PATH=f"{jh}/bin:{os.environ.get('PATH', '')}")
    sm = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if not sm.exists():
        base = "https://dl.google.com/android/repository/"
        repo = ET.fromstring(fetch_text(base + "repository2-3.xml"))
        host = "macosx" if IS_MAC else "linux"
        archives = [(a.findtext("complete/url"), a.findtext("complete/checksum")) for pk in repo.iter("remotePackage")
                    if pk.get("path") == "cmdline-tools;latest" for a in pk.iter("archive") if a.findtext("host-os") == host]
        if IS_MAC and len(archives) > 1:  # separate Apple Silicon / Intel builds
            want = "arm64" if cpu_arch() == "aarch64" else "x86_64"
            archives = [a for a in archives if want in a[0]] or archives
        if not archives:
            raise RuntimeError(f"Google's repository has no command-line tools for {host}")
        url, sha1 = archives[0]
        print(f"Installing Android command-line tools ({url})", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / url
            download(base + url, f, sha1, algo="sha1")
            replace_dir(extract(f, Path(tmp) / "x") / "cmdline-tools", sdk / "cmdline-tools" / "latest")
    run = lambda *a, **kw: subprocess.run([str(sm), f"--sdk_root={sdk}", *a], env=env, text=True, **kw)  # noqa: E731
    print("Accepting the Android SDK licenses (https://developer.android.com/studio/terms)", flush=True)
    run("--licenses", input="y\n" * 60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    listing = run("--list", capture_output=True).stdout
    # older sdkmanager prints "build-tools;36.0.0 | …", newer ones "build-tools/36.0.0   …"; install names use ";"
    names = {n.replace("/", ";") for n in re.findall(r"^\s*((?:build-tools|platforms)[;/][\w.\-]+)\s", listing, re.M)}
    tools = sorted((tuple(map(int, n.split(";")[1].split("."))), n) for n in names
                   if re.fullmatch(r"build-tools;\d+\.\d+\.\d+", n))
    plats = sorted((int(n.split("-")[1]), n) for n in names if re.fullmatch(r"platforms;android-\d+", n))
    if not tools or not plats:
        raise RuntimeError("couldn't read the list of SDK packages from sdkmanager")
    pkgs = ["platform-tools", tools[-1][1], plats[-1][1]]
    print("Installing " + ", ".join(pkgs), flush=True)
    if run(*pkgs).returncode:
        raise RuntimeError("sdkmanager failed to install the packages")
    print(f"Android SDK ready in {sdk}", flush=True)


def setup_scrcpy():
    """Latest scrcpy release from GitHub into ~/.local/scrcpy (live phone screen + Smooth window)."""
    rel = fetch_json("https://api.github.com/repos/Genymobile/scrcpy/releases/latest")
    prefix = f"scrcpy-{'macos' if IS_MAC else 'linux'}-{'aarch64' if cpu_arch() == 'aarch64' else 'x86_64'}-"
    asset = next((a for a in rel["assets"] if a["name"].startswith(prefix) and a["name"].endswith(".tar.gz")), None)
    if not asset:
        raise RuntimeError(f"scrcpy has no prebuilt release for this computer; install it with your package manager")
    sums = next((a for a in rel["assets"] if a["name"] == "SHA256SUMS.txt"), None)
    m = sums and re.search(rf"^([0-9a-f]{{64}})\s+\*?{re.escape(asset['name'])}\s*$", fetch_text(sums["browser_download_url"]), re.M)
    print(f"Installing scrcpy {rel['tag_name']}", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / asset["name"]
        download(asset["browser_download_url"], f, m and m.group(1))
        replace_dir(next(extract(f, Path(tmp) / "x").iterdir()), HOME / ".local" / "scrcpy")
    print(f"scrcpy installed in {HOME / '.local' / 'scrcpy'}", flush=True)


def setup_node():
    """Latest Node.js LTS from nodejs.org into ~/.local/node (already on Forge's PATH)."""
    rel = next(r for r in fetch_json("https://nodejs.org/dist/index.json") if r.get("lts"))
    v, plat = rel["version"], f"{'darwin' if IS_MAC else 'linux'}-{'arm64' if cpu_arch() == 'aarch64' else 'x64'}"
    name = f"node-{v}-{plat}.tar.gz"
    m = re.search(rf"^([0-9a-f]{{64}})\s+{re.escape(name)}$", fetch_text(f"https://nodejs.org/dist/{v}/SHASUMS256.txt"), re.M)
    if not m:
        raise RuntimeError(f"no Node.js {v} build for {plat}")
    print(f"Installing Node.js {v} LTS", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / name
        download(f"https://nodejs.org/dist/{v}/{name}", f, m.group(1))
        replace_dir(next(extract(f, Path(tmp) / "x").iterdir()), HOME / ".local" / "node")
    print(f"Node.js installed in {HOME / '.local' / 'node'}", flush=True)


def setup_npm_cli(package, binary):
    """Install an agent CLI from npm into ~/.local (no root), installing Node.js first if needed."""
    path = tool_env()["PATH"]
    if not shutil.which("npm", path=path):
        print("Node.js isn't installed, so installing it first", flush=True)
        setup_node()
        path = tool_env()["PATH"]
    npm = shutil.which("npm", path=path)
    print(f"Installing {package} (latest)", flush=True)
    # --allow-scripts lets this one package run its own postinstall on npm versions that block scripts by default
    r = subprocess.run([npm, "install", "-g", "--prefix", str(HOME / ".local"), f"--allow-scripts={package}", f"{package}@latest"],
                       env=dict(tool_env(), PATH=path))
    if r.returncode or not shutil.which(binary, path=path):
        raise RuntimeError(f"npm couldn't install {package}")
    print(f"{binary} installed in {HOME / '.local' / 'bin'}", flush=True)


def setup_flutter():
    """Latest stable Flutter SDK into ~/.local/flutter, from Google's release index (checksum-verified), with `flutter`
    and `dart` linked into ~/.local/bin. Linux on ARM has no prebuilt archive, so it's a clone of the stable branch."""
    git_bin = shutil.which("git", path=tool_env()["PATH"])
    if not git_bin:
        raise RuntimeError("Flutter needs Git. Install Git first (see the Git row above), then try again.")
    staging = Path(tempfile.mkdtemp(prefix=".flutter-install-", dir=FLUTTER_DIR.parent))  # same disk: no 2 GB copy
    try:
        if not IS_MAC and cpu_arch() == "aarch64":
            print("No prebuilt Flutter for Linux on ARM; cloning the stable branch from GitHub", flush=True)
            if subprocess.run([git_bin, "clone", "-b", "stable", "https://github.com/flutter/flutter.git", str(staging / "flutter")]).returncode:
                raise RuntimeError("git clone failed")
        else:
            index = fetch_json(f"https://storage.googleapis.com/flutter_infra_release/releases/releases_{'macos' if IS_MAC else 'linux'}.json")
            want, arch = index["current_release"]["stable"], "arm64" if cpu_arch() == "aarch64" else "x64"
            rel = next((r for r in index["releases"] if r["hash"] == want and r.get("dart_sdk_arch", "x64") == arch), None)
            if not rel:
                raise RuntimeError(f"no stable Flutter build for {arch}")
            print(f"Installing Flutter {rel['version']} (stable, {arch}); about 1 GB, this takes a while", flush=True)
            f = staging / Path(rel["archive"]).name
            download(f"{index['base_url']}/{rel['archive']}", f, rel["sha256"])
            print("  unpacking…", flush=True)
            if IS_MAC:  # the macOS zip holds framework symlinks, which Python's zipfile can't restore
                subprocess.run(["ditto", "-x", "-k", str(f), str(staging)], check=True)
            else:
                extract(f, staging)
            f.unlink()
        replace_dir(staging / "flutter", FLUTTER_DIR)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    bin_dir = HOME / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("flutter", "dart"):  # so terminals and agents find them too (the scripts follow their symlinks)
        link = bin_dir / tool
        if not link.exists() or (link.is_symlink() and str(FLUTTER_DIR) in os.readlink(link)):
            link.unlink(missing_ok=True)
            link.symlink_to(FLUTTER_DIR / "bin" / tool)
    print("First run: Flutter sets up its own tool (a minute or two)…", flush=True)
    env = tool_env()
    env["PATH"] = f"{FLUTTER_DIR / 'bin'}:{env['PATH']}"
    subprocess.run([str(FLUTTER_DIR / "bin" / "flutter"), "--version"], env=env)
    print(f"Flutter installed in {FLUTTER_DIR} (also on your PATH as ~/.local/bin/flutter)", flush=True)


SETUP_TASKS = {
    "jdk": setup_jdk, "android-sdk": setup_android_sdk, "scrcpy": setup_scrcpy, "node": setup_node, "flutter": setup_flutter,
    "claude": lambda: setup_npm_cli("@anthropic-ai/claude-code", "claude"),
    "codex": lambda: setup_npm_cli("@openai/codex", "codex"),
}


def run_setup(args):
    """Entry point for `server.py --setup <item> [args]` (runs in its own process; output goes to Processes)."""
    try:
        if args[0] == "wrapper":
            setup_wrapper(args[1], args[2])
        else:
            SETUP_TASKS[args[0]]()
        return 0
    except Exception as e:
        print(f"Error: {e}", flush=True)
        return 1


def setup_status():
    path = tool_env()["PATH"]
    which = lambda b: shutil.which(b, path=path)  # noqa: E731
    sdk = find_sdk()
    plats = android_platforms()
    build_tools = sorted(p.name for p in (sdk / "build-tools").iterdir()) if sdk and (sdk / "build-tools").is_dir() else []
    adb_ok = bool(sdk and (sdk / "platform-tools" / "adb").exists()) or bool(shutil.which("adb", path=path))
    jdk = tools_java()
    git_cmd = "xcode-select --install" if IS_MAC else "sudo apt install git    # Fedora: sudo dnf install git · Arch: sudo pacman -S git"
    items = [
        {"id": "claude", "group": "Agents", "name": "Claude Code", "ok": bool(which("claude")), "detail": which("claude"),
         "install": True, "link": "https://docs.claude.com/en/docs/claude-code/setup",
         "note": "Recommended agent. After installing, run `claude` once in a terminal to sign in."},
        {"id": "codex", "group": "Agents", "name": "Codex CLI", "ok": bool(which("codex")), "detail": which("codex"),
         "install": True, "optional": True, "link": "https://github.com/openai/codex",
         "note": "Optional. No CLI at all? Pick the Built-in agent and add an API key in Models & Keys."},
        {"id": "git", "group": "Code", "name": "Git", "ok": bool(which("git")), "detail": which("git"), "install": False,
         "link": "https://git-scm.com/downloads", "cmd": git_cmd, "note": "For Source Control, cloning and the Git status bar."},
        {"id": "node", "group": "Code", "name": "Node.js (LTS)", "ok": bool(which("node")), "detail": which("node"),
         "install": True, "optional": True, "link": "https://nodejs.org/en/download",
         "note": "For npm dev servers in Preview. Installs to ~/.local/node."},
        {"id": "jdk", "group": "Android", "name": "Java (JDK 17+)", "ok": bool(jdk),
         "detail": jdk and f"JDK {jdk_homes()[jdk]} · {jdk}", "install": True, "link": "https://adoptium.net/temurin/releases/",
         "note": "Builds Android apps. Installs Temurin 21 (LTS) to ~/.config/forge-studio/tools/jdk."},
        {"id": "android-sdk", "group": "Android", "name": "Android SDK", "ok": bool(adb_ok and plats and build_tools),
         "detail": sdk and f"{sdk}" + (f" · API {max(plats)} · build-tools {build_tools[-1]}" if plats and build_tools else " · incomplete"),
         "install": True, "link": "https://developer.android.com/studio#command-line-tools-only",
         "note": f"Command-line tools, platform-tools (adb), build-tools and the newest platform, into {sdk or DEFAULT_SDK}. "
                 "Android Studio isn't needed. Installing accepts the Android SDK License (developer.android.com/studio/terms)."},
        {"id": "gradle", "group": "Android", "name": "Gradle", "ok": True, "install": False,
         "detail": "Nothing to install: each project's Gradle wrapper downloads it on the first build"},
        {"id": "scrcpy", "group": "Android", "name": "scrcpy (live phone screen)", "ok": bool(scrcpy_server()[1]),
         "detail": scrcpy_path(), "install": not (sys.platform.startswith("linux") and cpu_arch() == "aarch64"), "optional": True,
         "link": "https://github.com/Genymobile/scrcpy/releases/latest", "note": "Smooth 120 fps phone screen in Android mode."},
    ]
    fv = flutter_version()
    items.append({"id": "flutter", "group": "Flutter", "name": "Flutter SDK", "ok": bool(fv), "install": True,
                  "detail": fv and f"Flutter {fv['version'] or '?'}" + (f" ({fv['channel']})" if fv.get("channel") else "") + f" · {fv['root']}",
                  "link": "https://docs.flutter.dev/get-started/install",
                  "note": f"Latest stable release into {FLUTTER_DIR} (a 1.5 GB download, about 2.5 GB on disk). Android builds also use the JDK and "
                          "Android SDK above. Needs Git."})
    if not IS_MAC:
        have = all(which(b) for b in ("clang++", "cmake", "ninja", "pkg-config"))
        gtk = have and subprocess.run(["pkg-config", "--exists", "gtk+-3.0"], env=tool_env()).returncode == 0
        items.append({"id": "flutter-linux", "group": "Flutter", "name": "Linux desktop toolchain", "ok": bool(gtk),
                      "install": False, "optional": True, "link": "https://docs.flutter.dev/platform-integration/linux/setup",
                      "detail": "clang, CMake, Ninja, GTK 3" if gtk else None,
                      "cmd": "sudo apt install clang cmake ninja-build pkg-config libgtk-3-dev liblzma-dev    "
                             "# Fedora: sudo dnf install clang cmake ninja-build gtk3-devel · Arch: sudo pacman -S clang cmake ninja gtk3",
                      "note": "Only to run Flutter apps as Linux desktop apps."})
    chrome = tool_env().get("CHROME_EXECUTABLE") or which("google-chrome") or which("google-chrome-stable")
    if IS_MAC:
        chrome = chrome or next((a for a in ("/Applications/Google Chrome.app", "/Applications/Chromium.app") if Path(a).exists()), None)
    items.append({"id": "flutter-chrome", "group": "Flutter", "name": "Chrome / Chromium", "ok": bool(chrome), "install": False,
                  "optional": True, "detail": chrome, "link": "https://www.google.com/chrome/",
                  "note": "To run Flutter web apps in a browser window. Without it, pick the “Web server” device: it opens in Preview."})
    running = {rec["meta"].get("setup"): rec["id"] for rec in PROCS.values()
               if rec["kind"] == "setup" and rec["popen"].poll() is None}
    for it in items:
        it["running"] = running.get(it["id"])
    return {"items": items, "os": "macOS" if IS_MAC else "Linux", "arch": cpu_arch()}


def setup_install(item):
    if item not in SETUP_TASKS:
        raise ValueError("That can't be installed automatically")
    if any(r["kind"] == "setup" and r["meta"].get("setup") == item and r["popen"].poll() is None for r in PROCS.values()):
        raise ValueError("Already installing")
    name = next(i["name"] for i in setup_status()["items"] if i["id"] == item)

    def done(code):
        _scrcpy_version.clear()
        emit({"type": "setup_done", "item": item, "ok": code == 0,
              "text": f"{name} installed" if code == 0 else f"Installing {name} failed. See Processes for the log"})
    start_proc([sys.executable, "-u", str(APP_DIR / "server.py"), "--setup", item], str(HOME), "setup", "",
               f"Install {name}", meta={"setup": item}, on_exit=done)


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
    roots = [(prof.dir / "uploads").resolve(), (prof.dir / "outputs").resolve()] + [Path(p).resolve() for p in project_paths()]
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
    if IS_MAC and (HOME / ".Trash").is_dir():
        dest = HOME / ".Trash" / f.name
        if dest.exists():
            dest = HOME / ".Trash" / f"{f.stem} {time.strftime('%H.%M.%S')}{f.suffix}"
        shutil.move(str(f), str(dest))
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
            if u.path == "/api/ide/ping":  # JetBrains plugin: is Forge up?
                return self._send(200, {"ok": True, "version": APP_VERSION, "windows": len(_subs)})
            if u.path == "/api/auth/profiles":
                return self._send(200, {"profiles": [public_profile(p) for p in CONFIG["profiles"]],
                                        "google": bool(google_cfg()), "redirect": ORIGIN,
                                        "hideTest": bool(CONFIG["settings"].get("hideTest"))})
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
                events = [trim_event(json.loads(l)) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
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
            if u.path == "/api/setup/status":
                return self._send(200, setup_status())
            if u.path == "/api/android/sdk":
                return self._send(200, {"platforms": android_platforms(), "gradle": bool(gradle_launcher()),
                                        "agp": AGP_VERSION, "gradleVersion": GRADLE_VERSION, "kotlin": KOTLIN_VERSION})
            if u.path == "/api/android/devices":
                return self._send(200, list_devices())
            if u.path == "/api/flutter/devices":
                return self._send(200, flutter_devices())
            if u.path == "/api/android/stream":
                return self.screen_stream(qs.get("serial", [None])[0] or None, int(qs.get("fps", ["120"])[0]))
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
            if u.path == "/api/code/zip":
                root, d = project_file(arg("project"), arg("path"), real=True)
                if not d.is_dir():
                    raise ValueError("Pick a folder")
                data = zip_folder(d)
                name = (d.name or root.name) + ".zip"
                return self._send(200, data, "application/zip",
                                  {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})
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
                      "scrcpy": scrcpy_path(), "stream": bool(scrcpy_server()[1]), "node": shutil.which("node", path=env_path),
                      "sdk": str(find_sdk() or ""), "git": shutil.which("git", path=env_path), "flutter": flutter_version()},
            "previews": {k: {"url": v["url"], "dir": v["dir"]} for k, v in PREVIEWS.items()},
            "running": [cid for cid, r in RUNS.items() if r.running() and cid in prof.data["chats"]],
            "agents": list_agents(),
            "plugins": list_plugins(),
            "prefs": prof.data["settings"],
            "web": web_access_info(),
            "current": prof.data["current"],
            "providers": providers,
            "procs": [{"id": r["id"], "kind": r["kind"], "project": r["project"], "label": r["label"],
                       "running": r["popen"].poll() is None, "meta": r["meta"]} for r in PROCS.values()],
            "flutter": {k: {"started": v["started"], "device": v["device"], "auto": v["auto"], "url": v["url"]}
                        for k, v in FLUTTER_RUNS.items()},
            "settings": {"studio_path": CONFIG["settings"].get("studio_path"),
                         "hideTest": bool(CONFIG["settings"].get("hideTest")),
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
        if u.path == "/api/ide/context":  # JetBrains plugin: selection / file → Forge's chat box
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, ide_context(b))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
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
            if "test" in b:
                p["test"] = bool(b["test"])
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
            if "hideTest" in b:
                CONFIG["settings"]["hideTest"] = bool(b["hideTest"])
            save_config()
            return None
        # ---- chats
        if path == "/api/chat/send":
            cid = start_chat(prof, project, b.get("prompt", ""), b.get("chat"), b.get("model"), b.get("mode"),
                             b.get("provider"), b.get("files"), b.get("agent") or "claude",
                             {"effort": b.get("effort"), "fast": b.get("fast"), "style": b.get("style")})
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
            prof.data["settings"].update({k: v for k, v in b.items() if k in ("theme", "accent", "density", "mode", "agent", "notify", "sound",
                                                                                 "nApproval", "nDone", "nBuild", "nCrash")})
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
        if path == "/api/plugins/install":
            return {"name": install_plugin(b.get("source"))}
        if path == "/api/plugins/create":
            return {"name": create_plugin(b.get("name"), b.get("description"))}
        if path == "/api/plugins/toggle":
            d = plugin_dir_of(b.get("id"))
            off = set(CONFIG["settings"].get("plugins_off", []))
            (off.discard if b.get("enabled") else off.add)(d.name)
            CONFIG["settings"]["plugins_off"] = sorted(off)
            save_config()
            return None
        if path == "/api/plugins/remove":
            shutil.rmtree(plugin_dir_of(b.get("id")))
            CONFIG["settings"]["plugins_off"] = [n for n in CONFIG["settings"].get("plugins_off", []) if n != b["id"]]
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
            subprocess.Popen(["open" if IS_MAC else "xdg-open", target], start_new_session=True, stdout=subprocess.DEVNULL,
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
        if path == "/api/setup/install":
            setup_install(b.get("item"))
            return None
        if path == "/api/android/new":
            return new_android_app(b.get("parent"), b.get("name"), b.get("package"), b.get("template"), b.get("minSdk"))
        if path == "/api/android/wrapper":
            add_wrapper(project, then=lambda code: emit({"type": "projects_changed", "text": "Gradle wrapper added" if code == 0
                                                           else "Adding the Gradle wrapper failed — see the build log"}))
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
            variant = "Release" if b.get("variant") == "Release" else "Debug"
            gradle(project, f":{module}:install{variant}", serial, then=after)
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
        # ---- flutter
        if path == "/api/flutter/new":
            return new_flutter_app(b.get("parent"), b.get("name"), b.get("org"), b.get("template"), b.get("platforms"))
        if path == "/api/flutter/run":
            flutter_run(project, b.get("device"), b.get("mode", "debug"), b.get("auto", True))
            return None
        if path == "/api/flutter/reload":
            flutter_reload(project, b.get("full"))
            return None
        if path == "/api/flutter/stop":
            flutter_stop(project)
            return None
        if path == "/api/flutter/auto":
            if project in FLUTTER_RUNS:
                FLUTTER_RUNS[project]["auto"] = bool(b.get("on"))
            return None
        if path == "/api/flutter/ext":
            flutter_ext(project, b.get("name"), b.get("enabled"))
            return None
        if path == "/api/flutter/task":
            flutter_task(project, b.get("args"))
            return None
        if path == "/api/flutter/devtools":
            return flutter_devtools(project)
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

    def screen_stream(self, serial, fps):
        """Relay scrcpy's raw video stream; the browser parses the packets and decodes H.264 with WebCodecs."""
        sock, cleanup = open_screen_stream(serial, max_fps=max(10, min(fps, 144)))
        size = device_size(serial)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if size:
                self.send_header("X-Device-Size", "x".join(size))
            self.end_headers()
            self.close_connection = True
            while True:
                data = sock.recv(256 * 1024)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sock.close()
            cleanup()

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
    if len(sys.argv) > 2 and sys.argv[1] == "--setup":  # a toolchain installer, started by setup_install / add_wrapper
        sys.exit(run_setup(sys.argv[2:]))
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
