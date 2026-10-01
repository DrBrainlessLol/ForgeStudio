#!/usr/bin/env bash
# Build distributable packages for Forge Studio.
# Produces (into ./dist): a .deb, a universal .tar.gz + install.sh, an Arch .pkg.tar.zst,
# and copies of PKGBUILD / forge-studio.spec for building on those distros.
set -euo pipefail
VERSION="${VERSION:-1.0.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DIST="$HERE/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
rm -rf "$DIST"; mkdir -p "$DIST"

# ---- assemble the payload as it will be installed under a prefix ----
stage_app() {  # $1 = prefix dir (e.g. .../usr)
  local u="$1"
  install -d "$u/lib/forge-studio"
  cp "$ROOT/server.py" "$u/lib/forge-studio/"
  cp -r "$ROOT/static" "$u/lib/forge-studio/"
  cp "$ROOT/forge-studio" "$u/lib/forge-studio/"; chmod 755 "$u/lib/forge-studio/forge-studio"
  find "$u/lib/forge-studio" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  install -d "$u/bin"; ln -sf /usr/lib/forge-studio/forge-studio "$u/bin/forge-studio"
  install -Dm644 "$HERE/forge-studio.desktop" "$u/share/applications/forge-studio.desktop"
  install -Dm644 "$HERE/icons/scalable.svg" "$u/share/icons/hicolor/scalable/apps/forge-studio.svg"
  local s; for s in 16 24 32 48 64 128 256 512; do
    install -Dm644 "$HERE/icons/$s.png" "$u/share/icons/hicolor/${s}x${s}/apps/forge-studio.png"
  done
  install -Dm644 "$ROOT/README.md" "$u/share/doc/forge-studio/README.md"
  install -Dm644 "$ROOT/LICENSE" "$u/share/doc/forge-studio/LICENSE"
  install -Dm644 "$ROOT/THIRD_PARTY_NOTICES.md" "$u/share/doc/forge-studio/THIRD_PARTY_NOTICES.md"
}

# ============================================================ 1. .deb
echo "==> Building .deb"
DEB="$WORK/deb"
stage_app "$DEB/usr"
install -d "$DEB/DEBIAN"
INSTALLED_KB=$(du -ks "$DEB/usr" | cut -f1)
cat > "$DEB/DEBIAN/control" <<EOF
Package: forge-studio
Version: $VERSION
Section: devel
Priority: optional
Architecture: all
Depends: python3 (>= 3.8), curl
Recommends: chromium | chromium-browser | google-chrome-stable
Suggests: adb, default-jre, nodejs, npm, scrcpy
Installed-Size: $INSTALLED_KB
Maintainer: Forge Studio <forge@localhost>
Description: AI coding agents with live website preview and Android tools
 Forge Studio is a local desktop studio for AI coding agents (Claude Code, a
 built-in API-key engine, Codex and any other CLI) with a live website preview
 and Android build, run and live-debug tools. It runs entirely on this computer.
EOF
cat > "$DEB/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true; fi
exit 0
EOF
cat > "$DEB/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true; fi
exit 0
EOF
chmod 755 "$DEB/DEBIAN/postinst" "$DEB/DEBIAN/postrm"
dpkg-deb --root-owner-group -Zxz --build "$DEB" "$DIST/forge-studio_${VERSION}_all.deb" >/dev/null
echo "    $(basename "$DIST"/forge-studio_${VERSION}_all.deb)"

# ============================================================ 2. universal tarball + install.sh
echo "==> Building universal tarball"
SRC="$WORK/forge-studio-$VERSION"
mkdir -p "$SRC"
cp "$ROOT/server.py" "$ROOT/forge-studio" "$ROOT/README.md" "$SRC/"
cp -r "$ROOT/static" "$SRC/"
find "$SRC" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp -r "$HERE/icons" "$SRC/icons"
cp "$HERE/forge-studio.desktop" "$HERE/PKGBUILD" "$HERE/forge-studio.spec" "$SRC/"
cat > "$SRC/install.sh" <<'EOF'
#!/usr/bin/env bash
# Universal installer for Forge Studio. Works on any Linux distro.
#   sudo ./install.sh            # system-wide, into /usr
#   ./install.sh --user          # just for you, into ~/.local (no root)
#   sudo ./install.sh --uninstall / ./install.sh --user --uninstall
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/usr; USERMODE=0; UNINSTALL=0
for a in "$@"; do case "$a" in
  --user) USERMODE=1; PREFIX="$HOME/.local";;
  --uninstall) UNINSTALL=1;;
  --prefix=*) PREFIX="${a#*=}";;
esac; done
if [ "$USERMODE" = 0 ] && [ "$(id -u)" != 0 ]; then echo "Run with sudo, or pass --user for a home install." >&2; exit 1; fi
APP="$PREFIX/lib/forge-studio"
uninstall() {
  rm -rf "$APP" "$PREFIX/bin/forge-studio" "$PREFIX/share/applications/forge-studio.desktop" \
         "$PREFIX/share/doc/forge-studio"
  find "$PREFIX/share/icons/hicolor" -name 'forge-studio.*' -delete 2>/dev/null || true
  echo "Forge Studio removed. Your data in ~/.config/forge-studio was kept."
}
[ "$UNINSTALL" = 1 ] && { uninstall; exit 0; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
install -d "$APP"
cp "$SRC/server.py" "$APP/"; cp -r "$SRC/static" "$APP/"
cp "$SRC/forge-studio" "$APP/"; chmod 755 "$APP/forge-studio"
install -d "$PREFIX/bin"; ln -sf "$APP/forge-studio" "$PREFIX/bin/forge-studio"
install -Dm644 "$SRC/forge-studio.desktop" "$PREFIX/share/applications/forge-studio.desktop"
install -Dm644 "$SRC/icons/scalable.svg" "$PREFIX/share/icons/hicolor/scalable/apps/forge-studio.svg"
for s in 16 24 32 48 64 128 256 512; do
  install -Dm644 "$SRC/icons/$s.png" "$PREFIX/share/icons/hicolor/${s}x${s}/apps/forge-studio.png"
done
install -Dm644 "$SRC/README.md" "$PREFIX/share/doc/forge-studio/README.md"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" 2>/dev/null || true
echo "Installed. Launch 'Forge Studio' from your menu, or run: forge-studio"
[ "$USERMODE" = 1 ] && case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to PATH to use the 'forge-studio' command.";; esac || true
EOF
chmod 755 "$SRC/install.sh"
TARBALL="$DIST/forge-studio-${VERSION}.tar.gz"
tar -C "$WORK" -czf "$TARBALL" "forge-studio-$VERSION"
echo "    $(basename "$TARBALL")"

# ---- fill the tarball checksum into a copy of PKGBUILD for convenience ----
SUM=$(sha256sum "$TARBALL" | cut -d' ' -f1)
sed "s/sha256sums=('SKIP')/sha256sums=('$SUM')/" "$HERE/PKGBUILD" > "$DIST/PKGBUILD"
cp "$HERE/forge-studio.spec" "$DIST/forge-studio.spec"

# ============================================================ 3. Arch .pkg.tar.zst (hand-built)
echo "==> Building Arch package"
PKG="$WORK/arch"
stage_app "$PKG/usr"
# fix the bin symlink to be relative-free absolute (already /usr/lib/...)
SIZE=$(du -sb "$PKG" | cut -f1)
BUILDDATE=$(date +%s)
cat > "$PKG/.PKGINFO" <<EOF
pkgname = forge-studio
pkgver = $VERSION-1
pkgdesc = AI coding agents with live website preview and Android tools
url = https://localhost
builddate = $BUILDDATE
packager = Forge Studio
size = $SIZE
arch = any
license = AGPL-3.0-only
depend = python
depend = curl
optdepend = chromium: run in an app window
optdepend = android-tools: adb device/build tools
optdepend = nodejs: npm dev-server previews
optdepend = scrcpy: real-time phone mirror
EOF
# .MTREE is optional for install; pacman regenerates. Build the zst tarball with .PKGINFO first.
( cd "$PKG" && tar --format=gnu -cf - .PKGINFO $(ls -A | grep -v '^\.PKGINFO$') | zstd -q -19 -o "$DIST/forge-studio-${VERSION}-1-any.pkg.tar.zst" )
echo "    forge-studio-${VERSION}-1-any.pkg.tar.zst (install: sudo pacman -U <file>)"

echo "==> Done. Artifacts in $DIST:"
ls -1sh "$DIST"
