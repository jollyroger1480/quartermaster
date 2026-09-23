#!/bin/bash
# bluez-alsa installer for callscoot — run ONCE with sudo:
#   sudo bash ~/grok-work/callscoot/vendor/install-bluealsa.sh
# Builds bluez-alsa v5.0.0 from the vendored source, installs the daemon,
# D-Bus policy and systemd service, and enables it.
set -e

SRC="$HOME/.local/src/bluez-alsa"
REAL_USER="${SUDO_USER:-$USER}"

echo "=== [1/6] installing build dependencies ==="
apt-get update -qq
apt-get install -y autoconf automake libtool pkg-config \
    libglib2.0-dev libdbus-1-dev libasound2-dev \
    libbluetooth-dev libsbc-dev libspandsp-dev

echo "=== [2/6] configuring bluez-alsa ==="
cd "$SRC"
autoreconf --install
./configure --enable-msbc --enable-ctl --enable-aplay --enable-rfcomm --enable-systemd

echo "=== [3/6] compiling ($(nproc) threads) ==="
make -j"$(nproc)"

echo "=== [4/6] installing ==="
make install
ldconfig

echo "=== [5/6] D-Bus policy + systemd service ==="
if [ -f src/org.bluealsa.conf ]; then
    cp src/org.bluealsa.conf /etc/dbus-1/system.d/
elif [ -f src/org.bluealsa.conf.in ]; then
    cp src/org.bluealsa.conf.in /etc/dbus-1/system.d/org.bluealsa.conf
fi
if [ -f misc/systemd/bluealsa.service ]; then
    cp misc/systemd/bluealsa.service /etc/systemd/system/bluealsa.service
else
    cp misc/systemd/bluealsa.service.in /etc/systemd/system/bluealsa.service
fi
systemctl daemon-reload

echo "=== [6/6] starting bluealsa ==="
systemctl enable --now bluealsa
sleep 2
systemctl --no-pager --lines=3 status bluealsa || true

echo
echo "=== done. switching wireplumber to the clean config ==="
if [ -n "$SUDO_USER" ]; then
    runuser -u "$SUDO_USER" -- systemctl --user restart wireplumber || true
    sleep 3
    runuser -u "$SUDO_USER" -- systemctl --user is-active wireplumber || true
else
    echo "restart your user wireplumber: systemctl --user restart wireplumber"
fi
echo
echo "ALL SET — bluez-alsa is installed. Continue with the README config steps."
