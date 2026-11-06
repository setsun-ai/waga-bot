#!/bin/bash
# Installs or updates waga-bot (run as root, next to waga.py). Safe to run again: settings and data stay.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE=$HERE/waga.py
[ -f "$SOURCE" ] || SOURCE=$HERE/../waga.py
SERVICE=$HERE/waga-bot.service
ENV_FILE=/etc/waga-bot.env
DIR=/opt/waga-bot

[ "$(id -u)" = 0 ] || { echo "Uruchom jako root (sudo)."; exit 1; }

echo "Instaluję Pillow, czcionkę i strefy czasowe..."
apt-get install -y -qq python3-pil fonts-dejavu-core tzdata >/dev/null

install -d "$DIR"
install -m 755 "$SOURCE" "$DIR/waga.py.new"
if ! python3 "$DIR/waga.py.new" --selftest; then
  rm -f "$DIR/waga.py.new"
  echo "Selftest nie przeszedł, nic nie zmieniam."
  exit 1
fi
mv "$DIR/waga.py.new" "$DIR/waga.py"
install -m 644 "$SERVICE" /etc/systemd/system/waga-bot.service

if [ ! -f "$ENV_FILE" ]; then
  read -rsp "Token bota od @BotFather (nie będzie widoczny): " token
  echo
  read -rp "Napisz teraz cokolwiek do swojego nowego bota na Telegramie i naciśnij Enter... " _
  owner=$(TG_TOKEN="$token" python3 "$DIR/waga.py" --whoami)
  case "$owner" in
    ''|*[!0-9]*) echo "Nie widzę Twojej wiadomości albo token jest zły: $owner"; exit 1 ;;
  esac
  ( umask 077; printf 'TG_TOKEN=%s\nTG_OWNER_ID=%s\n' "$token" "$owner" > "$ENV_FILE" )
  echo "Bot będzie odpowiadał tylko Tobie (ID $owner)."
fi

systemctl daemon-reload
systemctl enable -q waga-bot
systemctl restart waga-bot
sleep 3
if systemctl is-active --quiet waga-bot; then
  echo "Gotowe. Wersja $(python3 "$DIR/waga.py" --version). Wyślij botowi /start."
else
  journalctl -u waga-bot -n 15 --no-pager || true
  exit 1
fi
