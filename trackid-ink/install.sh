#!/usr/bin/env bash
# TrackID Ink installer - any Raspberry Pi with a 2.13" e-paper:
#   Waveshare 2.13" e-Paper HAT V4   or   Adafruit 2.13" Monochrome E-Ink Bonnet (#4687)
# Raspberry Pi OS Lite: 32-bit on a Zero W / WH, 32- or 64-bit on a Zero 2W.
# Usage:  sudo bash install.sh            (asks which display)
#         sudo bash install.sh waveshare  |  sudo bash install.sh adafruit
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo:  sudo bash install.sh"; exit 1; fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-pi}"
NEED_REBOOT=0
echo "==> TrackID Ink install for $USER_NAME ($APP_DIR)"

PANEL="${1:-}"
if [ -z "$PANEL" ]; then
  echo "Which e-paper is on this Pi?"
  echo "  1) Waveshare 2.13\" e-Paper HAT V4"
  echo "  2) Adafruit 2.13\" Monochrome E-Ink Bonnet"
  read -r -p "1 or 2: " ans
  [ "$ans" = "2" ] && PANEL=adafruit || PANEL=waveshare
fi
case "$PANEL" in adafruit*) PANEL=adafruit ;; *) PANEL=waveshare_v4 ;; esac
echo "==> Display: $PANEL"

echo "==> Packages"
apt-get update
apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio python3-venv fonts-dejavu-core

echo "==> SPI"
if [ ! -e /dev/spidev0.0 ]; then
  raspi-config nonint do_spi 0
  NEED_REBOOT=1
fi
for g in gpio spi; do getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"; done

PY=/usr/bin/python3
if [ "$PANEL" = adafruit ]; then
  echo "==> Adafruit Blinka + EPD library (in $APP_DIR/.venv)"
  sudo -u "$USER_NAME" python3 -m venv --system-site-packages "$APP_DIR/.venv"
  sudo -u "$USER_NAME" "$APP_DIR/.venv/bin/pip" install --upgrade adafruit-blinka adafruit-circuitpython-epd
  PY="$APP_DIR/.venv/bin/python"
fi

echo "==> Config"
[ -f "$APP_DIR/config.ini" ] || cp "$APP_DIR/config.example.ini" "$APP_DIR/config.ini"
sed -i "s/^panel *=.*/panel = $PANEL/" "$APP_DIR/config.ini"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"

echo "==> Service"
sed -e "s#__USER__#$USER_NAME#g" -e "s#__APP_DIR__#$APP_DIR#g" -e "s#/usr/bin/python3#$PY#" \
  "$APP_DIR/trackid-ink.service.in" > /etc/systemd/system/trackid-ink.service
systemctl daemon-reload
systemctl enable trackid-ink.service

echo
if [ "$NEED_REBOOT" -eq 1 ]; then
  echo "SPI turned on. Reboot to finish:  sudo reboot"
else
  systemctl restart trackid-ink.service
  echo "TrackID Ink is running.  Logs:  journalctl -u trackid-ink -f"
fi
echo "It follows $(grep -E '^url' "$APP_DIR/config.ini" | cut -d= -f2-). Edit config.ini if TrackID lives elsewhere."
