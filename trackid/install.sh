#!/usr/bin/env bash
# TrackID v2.2 installer - Raspberry Pi OS Lite 64-bit on a Pi Zero 2W with the PiSugar Whisplay HAT
# Usage:  sudo bash install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash install.sh"; exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-pi}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
NEED_REBOOT=0

echo "==> TrackID v2 install for user $USER_NAME ($APP_DIR)"

echo "==> Packages"
apt-get update
apt-get install -y git alsa-utils python3-pil python3-numpy python3-requests \
  python3-spidev python3-libgpiod fonts-dejavu-core ffmpeg

echo "==> Whisplay driver"
if [ ! -d "$HOME_DIR/Whisplay" ]; then
  sudo -u "$USER_NAME" git clone --depth 1 https://github.com/PiSugar/Whisplay.git "$HOME_DIR/Whisplay"
fi
if grep -qiE "whisplaysound|wm8960|es8389" /proc/asound/cards 2>/dev/null; then
  echo "    sound card already present, skipping driver install"
else
  (cd "$HOME_DIR/Whisplay" && bash install_driver.sh)
  NEED_REBOOT=1
fi

echo "==> Permissions"
for g in gpio spi audio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"
done

echo "==> Config"
if [ ! -f "$APP_DIR/config.ini" ]; then
  cp "$APP_DIR/config.example.ini" "$APP_DIR/config.ini"
else
  # older installs: add the new sections, keep everything already set
  for sec in auto local export; do
    grep -q "^\[$sec\]" "$APP_DIR/config.ini" || \
      awk -v s="[$sec]" '$0==s{p=1;print "";print;next} /^\[/{p=0} p' "$APP_DIR/config.example.ini" >> "$APP_DIR/config.ini"
  done
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/library"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"

echo "==> Service"
sed -e "s#__USER__#$USER_NAME#g" -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__HOME__#$HOME_DIR#g" \
  "$APP_DIR/trackid.service.in" > /etc/systemd/system/trackid.service
systemctl daemon-reload
systemctl enable trackid.service

echo
if ! grep -qE '^token *= *[^ ]' "$APP_DIR/config.ini"; then
  echo "!! Put your AudD token in $APP_DIR/config.ini ([audd] token = ...)"
fi
if [ "$NEED_REBOOT" -eq 1 ]; then
  echo "Driver installed. Reboot to finish:  sudo reboot"
  echo "TrackID starts on its own after the reboot."
else
  systemctl restart trackid.service
  echo "TrackID v2 is running.  Logs:  journalctl -u trackid -f"
fi
