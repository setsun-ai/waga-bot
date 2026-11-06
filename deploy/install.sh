#!/bin/bash
# Installs or updates weight-bot (run as root, next to weight_bot.py). Safe to run again: settings and data stay.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE=$HERE/weight_bot.py
[ -f "$SOURCE" ] || SOURCE=$HERE/../weight_bot.py
SERVICE=$HERE/weight-bot.service
ENV_FILE=/etc/weight-bot.env
DIR=/opt/weight-bot

[ "$(id -u)" = 0 ] || { echo "Uruchom jako root (sudo)."; exit 1; }

echo "Instaluję Pillow, czcionkę i strefy czasowe..."
apt-get install -y -qq python3-pil fonts-dejavu-core tzdata >/dev/null

install -d "$DIR"
install -m 755 "$SOURCE" "$DIR/weight_bot.py.new"
if ! python3 "$DIR/weight_bot.py.new" --selftest; then
  rm -f "$DIR/weight_bot.py.new"
  echo "Selftest nie przeszedł, nic nie zmieniam."
  exit 1
fi
mv "$DIR/weight_bot.py.new" "$DIR/weight_bot.py"
install -m 644 "$SERVICE" /etc/systemd/system/weight-bot.service

if [ ! -f "$ENV_FILE" ]; then
  read -rsp "Token bota od @BotFather (nie będzie widoczny): " token
  echo
  read -rp "Napisz teraz cokolwiek do swojego nowego bota na Telegramie i naciśnij Enter... " _
  owner=$(TG_TOKEN="$token" python3 "$DIR/weight_bot.py" --whoami)
  case "$owner" in
    ''|*[!0-9]*) echo "Nie widzę Twojej wiadomości albo token jest zły: $owner"; exit 1 ;;
  esac
  ( umask 077; printf 'TG_TOKEN=%s\nTG_OWNER_ID=%s\n' "$token" "$owner" > "$ENV_FILE" )
  echo "Bot będzie odpowiadał tylko Tobie (ID $owner)."
fi

systemctl daemon-reload
systemctl enable -q weight-bot
systemctl restart weight-bot
sleep 3
if systemctl is-active --quiet weight-bot; then
  echo "Gotowe. Wersja $(python3 "$DIR/weight_bot.py" --version). Wyślij botowi /start."
else
  journalctl -u weight-bot -n 15 --no-pager || true
  exit 1
fi
