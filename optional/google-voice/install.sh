#!/usr/bin/env bash
# One-time setup. Everything goes inside this folder: a private copy of Python
# (runtime/), the packages, and later your settings (data/). Nothing is installed
# system-wide and it never asks for sudo. Safe to run again; it resumes.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/app"
RT="$ROOT/runtime"
PY="$RT/python/bin/python3"
REL="https://github.com/astral-sh/python-build-standalone/releases/download/20261003"

fail() { printf '\n\033[31m%s\033[0m\n' "$1"; exit 1; }
step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }

echo "Shop Assistant setup"
[ "$(uname -s)" = "Linux" ] || fail "This installer is for Linux. On Windows, use Install.cmd from the Windows download."
[ "$(id -u)" != "0" ] || fail "Run this as your normal user, not with sudo or as root."
case "$(uname -m)" in
  x86_64)  ARCH=x86_64;  SHA=731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91 ;;
  aarch64) ARCH=aarch64; SHA=6541297dd1798dec8b98c3ad7492808a5b9d1c126801ceb2011e7754cd20d1ce ;;
  *) fail "Unsupported processor: $(uname -m). This needs a 64-bit Intel/AMD or ARM computer." ;;
esac
FREE_KB=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
[ "${FREE_KB:-0}" -gt 4000000 ] || fail "Not enough free disk space here; about 4 GB is needed."

step "1 of 4  Setting up a private copy of Python (inside this folder)"
if [ ! -x "$PY" ]; then
  mkdir -p "$RT"
  TGZ="$APP/python-runtime-linux-$ARCH.tar.gz"
  if [ ! -f "$TGZ" ]; then
    TGZ="$RT/python-runtime.tar.gz"
    URL="$REL/cpython-3.12.15%2B20261003-$ARCH-unknown-linux-gnu-install_only_stripped.tar.gz"
    echo "downloading Python..."
    if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$TGZ" "$URL" || fail "Could not download Python. Check the internet connection and run ./install.sh again."
    elif command -v wget >/dev/null 2>&1; then wget -q -O "$TGZ" "$URL" || fail "Could not download Python. Check the internet connection and run ./install.sh again."
    else fail "Install curl or wget first, then run ./install.sh again."; fi
  fi
  GOT=$(sha256sum "$TGZ" | cut -d' ' -f1)
  [ "$GOT" = "$SHA" ] || fail "The Python package is damaged (checksum mismatch). Download the app again."
  tar -xzf "$TGZ" -C "$RT" || fail "Could not unpack Python."
fi
"$PY" -c "import ssl, sqlite3, struct; assert struct.calcsize('P') == 8" || fail "The private Python did not start. Delete the runtime folder and run ./install.sh again."
echo ok

step "2 of 4  Downloading the speech, voice and browser parts (about 1 GB, 5 to 20 minutes)"
PIP=("$PY" -m pip install --disable-pip-version-check --no-warn-script-location)
# the CPU build of PyTorch; the default Linux build drags in several GB of GPU libraries
"${PIP[@]}" torch --index-url https://download.pytorch.org/whl/cpu || fail "A download failed. Check the internet connection and run ./install.sh again; it continues from here."
"${PIP[@]}" -r "$APP/requirements.txt" || fail "A download failed. Check the internet connection and run ./install.sh again; it continues from here."

step "3 of 4  Adding it to your applications menu"
chmod +x "$ROOT/shop-assistant.sh" 2>/dev/null
DESK="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$DESK"
ESC=$(printf '%s' "$ROOT/shop-assistant.sh" | sed 's/[\\"$`]/\\&/g')
cat > "$DESK/shop-assistant.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Shop Assistant
Comment=AI phone receptionist control panel
Exec="$ESC"
Icon=$APP/shopassistant/panel/favicon.png
Terminal=false
Categories=Office;
DESKTOP
echo ok

BROWSER=""
for b in google-chrome-stable google-chrome microsoft-edge-stable microsoft-edge chromium chromium-browser brave-browser; do
  if command -v "$b" >/dev/null 2>&1; then BROWSER="$b"; break; fi
done
if [ -z "$BROWSER" ]; then
  printf '\n\033[33m%s\033[0m\n' "No supported browser found. Install Google Chrome, Microsoft Edge or Chromium before turning the assistant on."
fi

step "4 of 4  Opening the control panel"
"$ROOT/shop-assistant.sh"
echo
echo "Done. Next time, open Shop Assistant from your applications menu or run ./shop-assistant.sh"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  echo "No desktop session detected: the control panel is running at http://127.0.0.1:8765/ on this machine."
fi
