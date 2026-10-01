# Forge Studio

A desktop studio for AI coding agents, built around Claude Code and working with any CLI agent. It includes chat with approvals, attachments and history, a live website preview, and Android build, run and live-debug tools.
It uses only the Python standard library and runs as a desktop web app (Chromium app window, installable PWA, optional LAN access).

## Launch
- Desktop / app menu: **Forge Studio**
- Terminal: `forge-studio` or `forge-studio ~/path/to/project`
- Android Studio: **Tools → External Tools → Open in Forge Studio**
- Other devices: **Settings → Web App → Use from phones and other computers** (port 8766, needs the link with its token)

## Workspaces
Switch at the top of the window (or **Ctrl+1 / 2 / 3**). The agent chat comes with you in every mode.
- **Agent**: projects and running/finished agent tasks on the left, the chat in the middle (a "What should we build?" home screen with suggestions when it's empty), and Preview / Processes on the right.
- **Editor**: a familiar IDE layout. Explorer, search and source control on the left, tabbed code editor in the middle, the agent docked on the right.
- **Android**: a run bar (module, debug/release, device, Run / Restart / Stop / Build), then **Run & Logcat** (screen mirror + app logcat), **Build** output, **Gradle** tasks and toolchain, and **Devices** (emulators, Wi-Fi pairing, port forwarding).
  - **New app** creates a Compose or Views app with Kotlin DSL, a version catalog, Git and a real Gradle wrapper (AGP 8.7.3 / Gradle 8.10.2 / Kotlin 2.0.21).
  - Projects without `gradlew` get an **Add Gradle wrapper** button.
  - Gradle runs on a JDK it supports, picked automatically (e.g. JDK 17 for Gradle 8.10 even if Android Studio's bundled JDK is newer).

The status bar shows the Git branch, the project, the agent's state and (in Android mode) the device and Gradle version.

## Agents
| Agent | Support |
|---|---|
| **Claude Code** (recommended) | Streaming, approval prompts, image input, chat resume, model providers |
| **Built-in (API key, no CLI)** | No CLI needed — talks to an Anthropic-style API directly with your own key. Read/Write/Edit/Bash tools, approvals, images, resume. |
| **Codex** | Native adapter: shell/file-change cards, image input, resume |
| Gemini CLI, Qwen Code, opencode, Cursor Agent, Aider | Detected automatically when installed; output streams into the chat |
| Custom | Settings → Agents → any command, with `{prompt}` and `{model}` placeholders |

The **Built-in** engine is what makes Forge Studio work standalone: pick it as the agent and select one of your own API-key providers in the model menu. It runs the whole agent loop in-process (Read/Write/Edit/Bash tools + approvals), so no coding CLI has to be installed. It speaks **both** API shapes:
- **OpenAI Chat Completions** — OpenAI, OpenRouter (GPT, Gemini, Llama…), DeepSeek, Groq, Ollama, or any OpenAI-compatible endpoint.
- **Anthropic Messages** — Anthropic, Kimi, GLM, or any Anthropic-compatible endpoint.

Each provider has an **API style** setting in Settings → Models & Keys; the presets set it for you.

## Code & Git

The right panel has a **Code** tab — a built-in editor and Git client, no external tools bundled:
- **Explorer**: lazy-loading file tree with Git status colors, new file/folder, rename (F2), delete (to Trash), drag-to-move, and a right-click menu (copy path, open/download, ask the agent).
- **Editor**: multi-tab, per-file undo, syntax highlighting for common languages, auto-indent, Tab/Shift+Tab, Ctrl+/ to comment, Ctrl+S to save (with a stale-file conflict prompt), and a status bar (line/column, language, branch).
- **Search** (Ctrl+Shift+F): project-wide, case/regex toggles, grouped results.
- **Quick open** (Ctrl+P): fuzzy file finder.
- **Source Control** (Ctrl+Shift+G): stage/unstage/discard, commit (uses your profile name/email when Git has no identity), branch switch/create, pull/push/fetch, diffs, and recent commits. Initialize or clone a repo from here.
- **Clone / New project**: from the sidebar ＋ (right-click) or the welcome tiles.
- When the agent edits files in the open project, the editor reloads them live and the Git panel updates.

## Install / packaging
Prebuilt packages are produced by `packaging/build.sh` into `packaging/dist/`:
- **Debian / Ubuntu / Deepin:** `sudo apt install ./forge-studio_1.0.0_all.deb`
- **Arch:** `sudo pacman -U forge-studio-1.0.0-1-any.pkg.tar.zst` (or `makepkg -si` with the `PKGBUILD`)
- **Fedora / openSUSE:** build the RPM with `forge-studio.spec`
- **Any distro:** extract `forge-studio-1.0.0.tar.gz` and run `sudo ./install.sh` (or `./install.sh --user`)

See `packaging/README.md` for details.

## Access on other devices
- The token lives in `~/.config/forge-studio/token`. Settings → Web App shows it and a copy button.
- On this computer the page authorises itself automatically (loopback only), so a bookmark or the installed app opens straight to sign-in.
- On another device: turn on network access in Settings → Web App, open the link it shows, or paste the token on the first screen. Give every profile a PIN first.

## Permissions (Claude Code)
- **Ask before acting**: every non-read action becomes a card in the chat.
- **Auto-edit, ask for commands**, **Auto**, **Full access — never ask**, **Plan only**.
- Approval cards highlight risky actions: recursive deletes, sudo, force-push, `curl | sh`, installs, changes outside the project, and so on.
- Each card offers **Allow**, **Always Allow** or **Deny**.

## Chat
- Attach images and files with the clip, by pasting, or by drag & drop. Images go to the model directly; other files are passed as paths.
- Files the agent writes get **Open / Download** buttons, and file paths in replies get a download link.
- **History** lets you reopen, rename and delete conversations. They're per profile.

## Profiles & settings
- **Profiles**: local (optional PIN) or Google sign-in (one-time Desktop-app OAuth client setup).
- **Settings** sections:
  - General: light / dark (AMOLED) / auto, accent color, density, default permissions.
  - Profile & Account.
  - Agents.
  - Models & Keys: your own Anthropic key, DeepSeek, OpenRouter, Kimi, GLM, Ollama, or custom.
  - Web App.
  - Android.

## Preview & Android
- **Preview**: static server with live reload, or any dev command (URL auto-detected, `npm install` on first run). Device sizes are available.
- **Android** (Android Studio need not be open):
  - Run / Restart / Build.
  - Live debug: the phone's screen next to app-only logcat.
  - Crash detection with **Ask to fix**.
  - scrcpy smooth mirror.
  - Wi-Fi pairing and `adb reverse`.

## Notes
- Data lives in `~/.config/forge-studio/`: token, config, and `profiles/<id>/` with chats, uploads and keys (mode 600).
- Server log: `~/.config/forge-studio/server.log`. Port: `FORGE_STUDIO_PORT` (default 8765; LAN uses +1).
