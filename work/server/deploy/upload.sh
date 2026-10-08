#!/usr/bin/env bash
# Run this from your PC (Git Bash) to copy the server up to the VM.
# It skips build junk, logs, and local caches so only what the VM needs goes up.
# Player saves (data/saves, data/research_progress), gym holders (data/gyms.json)
# and player-added places are never sent or deleted: re-running this to push
# code updates leaves the live players' progress alone.
#
#   bash deploy/upload.sh ubuntu@YOUR_PUBLIC_IP ~/.ssh/your_oracle_key
#
set -euo pipefail

DEST="${1:?usage: upload.sh user@public_ip [ssh_key_path]}"
KEY="${2:-}"
SSH_OPTS=()
[ -n "$KEY" ] && SSH_OPTS=(-i "$KEY")

# This script lives in work/server/deploy — the server root is its parent.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Uploading $HERE  ->  $DEST:/opt/pogo"
# Create /opt/pogo owned by the login user so scp can write without sudo.
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$DEST" 'sudo mkdir -p /opt/pogo && sudo chown "$USER" /opt/pogo'

if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete \
    --exclude '__pycache__/' \
    --exclude 'build/' --exclude 'dist/' \
    --exclude '*.spec' \
    --exclude 'server-log.txt*' \
    --exclude '/data/saves/' --exclude '/data/saves-*/' \
    --exclude '/data/research_progress/' \
    --exclude '/data/ip_users.json' \
    --exclude '/data/secret.key' \
    --exclude '/data/devices.json' --exclude '/data/nominations.json' \
    --exclude '/data/gyms.json' --exclude '/data/site_gone.json' \
    --exclude '/data/move_picks.json' \
    --exclude '/data/world/' --exclude '/data/osm_forts*.json' \
    --exclude '/data/osm_biomes.json' \
    --exclude '/certs/' \
    --exclude 'photos/' --exclude '_probe.js' --exclude '_hook.js' \
    --exclude 'sectest.py' \
    -e "ssh ${SSH_OPTS[*]:-}" \
    "$HERE"/ "$DEST":/opt/pogo/
else
  echo "    (rsync not found; using scp — slower, no exclude)"
  scp ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -r "$HERE"/* "$DEST":/opt/pogo/
fi

# Map data. The VM reads forts and biomes from a cell-indexed SQLite pack
# (worlddb.py), not osm_forts.json + osm_biomes.json: holding those in Python
# cost ~1.2 GB of RAM, multi-100 ms GC freezes, and an OOM kill whenever the
# file was rewritten. So the JSON never goes up (excluded above); it is packed
# here whenever it is newer than the pack, and the pack goes up in its place.
PACK="$HERE/build/world.sqlite"
if [ -f "$HERE/data/osm_forts.json" ] && command -v rsync >/dev/null 2>&1; then
  if [ ! -f "$PACK" ] || [ "$HERE/data/osm_forts.json" -nt "$PACK" ] \
      || [ "$HERE/data/osm_biomes.json" -nt "$PACK" ]; then
    echo "==> Packing data/osm_forts.json + osm_biomes.json into build/world.sqlite"
    mkdir -p "$HERE/build"
    python3 "$HERE/../../ios-launcher/tools/build_world_db.py" "$HERE/data" "$PACK"
  fi
  echo "==> Uploading the world pack"
  # rsync writes a temp file and renames it: worlddb opens packs immutable,
  # so one must never be rewritten in place.
  ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$DEST" 'mkdir -p /opt/pogo/data/world'
  rsync -az -e "ssh ${SSH_OPTS[*]:-}" "$PACK" "$DEST":/opt/pogo/data/world/world.sqlite
fi

# Certificates. NEVER the ones in certs/: that server.key ships inside every
# launcher (the server-on-this-phone needs it), so anyone with the app could
# impersonate a public server that used it. The public box gets its own set,
# made once with   python3 gen_certs.py ../vps-certs   -- and its CA key stays
# on this computer.
VPS_CERTS="$HERE/../vps-certs"
if [ -f "$VPS_CERTS/server.key" ] && command -v rsync >/dev/null 2>&1; then
  echo "==> Uploading this server's own certificate (work/vps-certs)"
  # --delete-excluded: a CA key left on the public box by an older upload goes too.
  rsync -az --delete --delete-excluded \
    --exclude 'ca.key' --exclude '*.csr' --exclude '*.srl' \
    -e "ssh ${SSH_OPTS[*]:-}" "$VPS_CERTS"/ "$DEST":/opt/pogo/certs/
else
  echo "!! No work/vps-certs -- the server keeps whatever certificate it has."
  echo "   Make one:  python3 gen_certs.py ../vps-certs   (then run this again)"
fi

# The server runs as the unprivileged `pogo` user (see pogo-server.service);
# once setup-vm.sh has created it, hand the new files over.
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$DEST" \
  'if id pogo >/dev/null 2>&1; then sudo chown -R pogo:pogo /opt/pogo; fi'

echo "==> Uploaded. Now on the VM run:"
echo "    ssh ${SSH_OPTS[*]:+${SSH_OPTS[*]} }$DEST"
echo "    sudo bash /opt/pogo/deploy/setup-vm.sh"
