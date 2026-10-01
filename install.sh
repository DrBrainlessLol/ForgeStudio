#!/bin/sh
# Forge Studio one-line installer: installs the latest release for the current user (no root needed).
#
#   curl -fsSL https://raw.githubusercontent.com/DrBrainlessLol/ForgeStudio/main/install.sh | sh
#   curl -fsSL …/install.sh | sh -s -- --uninstall      # remove it again (your data is kept)
#
# Installs into ~/.local (app in ~/.local/lib/forge-studio, command ~/.local/bin/forge-studio, a menu
# entry and icons). Nothing else is installed: Forge Studio opens in the browser you already have.
# Options (environment): FORGE_VERSION=1.0.1 to pick a release; FORGE_DOWNLOAD_BASE to use a mirror.
set -eu

REPO="DrBrainlessLol/ForgeStudio"
PREFIX="$HOME/.local"
APP="$PREFIX/lib/forge-studio"

if [ -t 1 ]; then B=$(printf '\033[1m'); R=$(printf '\033[31m'); N=$(printf '\033[0m'); else B=""; R=""; N=""; fi
say() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
die() { printf '%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

uninstall() {
  rm -rf "$APP" "$PREFIX/bin/forge-studio" "$PREFIX/share/applications/forge-studio.desktop" "$PREFIX/share/doc/forge-studio"
  find "$PREFIX/share/icons/hicolor" -name 'forge-studio.*' -delete 2>/dev/null || true
  say "Forge Studio removed. Your chats, settings and keys in ~/.config/forge-studio were kept."
}

case "${1:-}" in
  --uninstall) uninstall; exit 0 ;;
  "" ) ;;
  *) die "unknown option: $1 (use --uninstall, or nothing to install/update)" ;;
esac

# ---- requirements
[ "$(uname -s)" = "Linux" ] || die "Forge Studio currently supports Linux only."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v tar >/dev/null 2>&1 || die "tar is required."
command -v python3 >/dev/null 2>&1 || die "Python 3.8 or newer is required (e.g. sudo apt install python3)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "Python 3.8 or newer is required (found $(python3 -V 2>&1))."

# ---- which release
if [ -n "${FORGE_VERSION:-}" ]; then
  VERSION="${FORGE_VERSION#v}"
else
  say "Finding the latest release"
  # /releases/latest redirects to …/tag/vX.Y.Z, which avoids the GitHub API and its rate limits
  LATEST=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null) \
    || die "no published release found at github.com/$REPO/releases (or GitHub can't be reached)."
  VERSION="${LATEST##*/v}"
  case "$VERSION" in [0-9]*) ;; *) die "couldn't find a published release at github.com/$REPO/releases" ;; esac
fi
BASE="${FORGE_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download/v$VERSION}"
TARBALL="forge-studio-$VERSION.tar.gz"

# ---- download and verify
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
say "Downloading Forge Studio $VERSION"
curl -fsSL "$BASE/$TARBALL" -o "$TMP/$TARBALL" || die "download failed: $BASE/$TARBALL"
if curl -fsSL "$BASE/SHA256SUMS" -o "$TMP/SHA256SUMS" 2>/dev/null; then
  WANT=$(grep " $TARBALL\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)
  GOT=$( (sha256sum "$TMP/$TARBALL" 2>/dev/null || shasum -a 256 "$TMP/$TARBALL") | cut -d' ' -f1)
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || die "checksum mismatch for $TARBALL. The download may be corrupted; please try again."
  say "Checksum verified"
else
  say "No SHA256SUMS published for this release; skipping verification"
fi

# ---- install (reuses the installer shipped inside the release)
tar -xzf "$TMP/$TARBALL" -C "$TMP"
[ -x "$TMP/forge-studio-$VERSION/install.sh" ] || die "the release archive looks incomplete"
UPDATE=""; [ -d "$APP" ] && UPDATE=1
"$TMP/forge-studio-$VERSION/install.sh" --user >/dev/null

echo
if [ -n "$UPDATE" ]; then say "Forge Studio updated to $VERSION."; else say "Forge Studio $VERSION installed."; fi
if [ -n "$UPDATE" ] && curl -fs -o /dev/null "http://127.0.0.1:${FORGE_STUDIO_PORT:-8765}/" 2>/dev/null; then
  echo "    Forge Studio is running. Restart it to use the new version:"
  echo "    pkill -f forge-studio/server.py; forge-studio"
fi
echo "    Start it from your app menu, or run:  forge-studio"
echo "    It opens in your browser. You'll also want an agent: Claude Code, Codex, another CLI, or an API key."
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "    Note: add ~/.local/bin to your PATH to use the 'forge-studio' command:"
     echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile" ;;
esac
