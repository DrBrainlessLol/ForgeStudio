Name:           forge-studio
Version:        1.0.0
Release:        1%{?dist}
Summary:        AI coding agents with live website preview and Android tools
License:        Proprietary
URL:            https://localhost
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz
Requires:       python3, curl
Recommends:     chromium
Recommends:     android-tools
%description
Forge Studio is a local desktop studio for AI coding agents (Claude Code, a
built-in API-key engine, Codex and others) with a live website preview and
Android build, run and live-debug tools. It runs entirely on this computer.
%prep
%setup -q
%install
mkdir -p %{buildroot}/usr/lib/forge-studio
cp -r server.py static forge-studio %{buildroot}/usr/lib/forge-studio/
chmod 755 %{buildroot}/usr/lib/forge-studio/forge-studio
mkdir -p %{buildroot}/usr/bin
ln -s /usr/lib/forge-studio/forge-studio %{buildroot}/usr/bin/forge-studio
install -Dm644 forge-studio.desktop %{buildroot}/usr/share/applications/forge-studio.desktop
install -Dm644 icons/scalable.svg %{buildroot}/usr/share/icons/hicolor/scalable/apps/forge-studio.svg
for s in 16 24 32 48 64 128 256 512; do
  install -Dm644 icons/${s}.png %{buildroot}/usr/share/icons/hicolor/${s}x${s}/apps/forge-studio.png
done
install -Dm644 README.md %{buildroot}/usr/share/doc/forge-studio/README.md
%files
/usr/lib/forge-studio
/usr/bin/forge-studio
/usr/share/applications/forge-studio.desktop
/usr/share/icons/hicolor/*/apps/forge-studio.*
/usr/share/doc/forge-studio/README.md
