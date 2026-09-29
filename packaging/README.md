# Forge Studio — packaging

Run `./build.sh` to produce everything in `dist/`:

| File | For | Install |
|---|---|---|
| `forge-studio_1.0.0_all.deb` | Debian, Ubuntu, Deepin, Mint, Pop!_OS… | `sudo apt install ./forge-studio_1.0.0_all.deb` |
| `forge-studio-1.0.0.tar.gz` | **Any** Linux distro | extract, then `sudo ./install.sh` (or `./install.sh --user`) |
| `forge-studio-1.0.0-1-any.pkg.tar.zst` | Arch, Manjaro, EndeavourOS | `sudo pacman -U forge-studio-1.0.0-1-any.pkg.tar.zst` |
| `PKGBUILD` | Arch (from source, recommended) | put next to the `.tar.gz`, run `makepkg -si` |
| `forge-studio.spec` | Fedora, RHEL, openSUSE | `rpmbuild -tb forge-studio-1.0.0.tar.gz` (uses this spec) |

The package is architecture-independent (pure Python + static files). It installs to
`/usr/lib/forge-studio`, a launcher at `/usr/bin/forge-studio`, a desktop entry, and
hicolor icons (16–512 px + SVG). Per-user data stays in `~/.config/forge-studio`.

## Dependencies
- **Required:** `python3` (≥ 3.8), `curl`.
- **Recommended:** `chromium` / `google-chrome` (app window; otherwise it opens in your default browser).
- **Optional:** `adb` (Android), a JDK (build Android apps), `nodejs`/`npm` (npm dev-server previews), `scrcpy` (real-time phone mirror).

The names above are Debian's; the `PKGBUILD` and `.spec` list each distro's equivalents.

## Notes
- `build.sh` needs `dpkg-deb`, `tar`, `zstd` (all standard). It does **not** need root.
- The prebuilt `.pkg.tar.zst` has no `.MTREE`; `pacman` installs it fine and regenerates one. For a "proper" Arch build, use the `PKGBUILD`.
- To rebuild at a new version: `VERSION=1.2.3 ./build.sh`.
