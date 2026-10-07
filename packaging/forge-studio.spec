Name:           forge-studio
Version:        1.2.0
Release:        1%{?dist}
Summary:        Lightweight studio for AI coding agents with an editor, website preview and Android tools
License:        AGPL-3.0-only
URL:            https://github.com/DrBrainlessLol/ForgeStudio
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz
Requires:       python3, curl
Suggests:       chromium
Suggests:       android-tools
%description
Forge Studio is a local desktop studio for AI coding agents (Claude Code, a
built-in API-key engine, Codex and others) with a live website preview and
Android build, run and live-debug tools, plus a built-in editor with Git.
It runs entirely on this computer.
%prep
%setup -q
%install
mkdir -p %{buildroot}/usr/lib/forge-studio
cp -r server.py static forge-studio %{buildroot}/usr/lib/forge-studio/
chmod 755 %{buildroot}/usr/lib/forge-studio/forge-studio
mkdir -p %{buildroot}/usr/bin
ln -s /usr/lib/forge-studio/forge-studio %{buildroot}/usr/bin/forge-studio
install -Dm644 forge-studio.desktop %{buildroot}/usr/share/applications/forge-studio.desktop
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
%license LICENSE
%doc THIRD_PARTY_NOTICES.md
