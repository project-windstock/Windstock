#!/usr/bin/env bash
# Run ON the VPS as root, after deploy/upload.sh has copied /opt/pogo/windbot up.
# Safe to re-run: it only (re)builds the bot's venv and installs its unit.
#
#   bash /opt/pogo/windbot/install.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c 'import ensurepip' 2>/dev/null; then
  apt-get install -y python3-venv
fi
[ -x /opt/windbot-venv/bin/python ] || python3 -m venv /opt/windbot-venv
/opt/windbot-venv/bin/pip install --quiet --upgrade -r "$HERE/requirements.txt"

# The token file. Created empty (root-only); the bot won't start until it's filled in.
if [ ! -f /etc/windbot.env ]; then
  install -m 600 /dev/null /etc/windbot.env
  echo "DISCORD_TOKEN=" > /etc/windbot.env
fi

# Trading (/link, /trade) asks the website which trainer a Discord user is, with
# the website's GAME_KEY. Copied over once, if the website on this box has one.
SITE_ENV=/opt/windstock-site/.env
if ! grep -q '^SITE_GAME_KEY=' /etc/windbot.env && [ -f "$SITE_ENV" ]; then
  KEY="$(grep '^GAME_KEY=' "$SITE_ENV" | tail -1 | cut -d= -f2-)"
  if [ -n "$KEY" ]; then
    echo "SITE_GAME_KEY=$KEY" >> /etc/windbot.env
    echo "==> trading switched on (SITE_GAME_KEY from the website)"
  fi
fi

# Its own root-owned copy: the bot's throwaway user can't read /opt/pogo and shouldn't.
install -d -m 755 /opt/windbot
install -m 644 "$HERE/windbot.py" /opt/windbot/windbot.py
install -m 644 "$HERE/windbot.service" /etc/systemd/system/windbot.service
systemctl daemon-reload
systemctl enable windbot >/dev/null 2>&1

if grep -q '^DISCORD_TOKEN=.\+' /etc/windbot.env; then
  systemctl restart windbot
  echo "==> windbot restarted"
else
  echo "==> Installed. Put the bot token in /etc/windbot.env, then: systemctl restart windbot"
fi
