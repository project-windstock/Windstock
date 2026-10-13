#!/bin/bash
# Install / refresh the Windstock root module on a rooted phone (APatch or Magisk):
# the CA in both system trust stores, and the Niantic/PTC hostnames pinned to this
# PC. See windstock-ca/README.md for why, and ../APATCH-ROOT.md for the full run.
#
#   ./deploy.sh                 # use the PC's current LAN IP
#   IP=192.168.1.50 ./deploy.sh # pin a specific address
#   CERT=/path/ca.crt ./deploy.sh   # non-default certificate
#
# Re-run it whenever the PC's LAN IP changes -- the /system/etc/hosts pins are an
# address, not a name. The certificate mounts are address-independent.
#
# On Windows/Git Bash, run it as MSYS_NO_PATHCONV=1 ./deploy.sh: otherwise MSYS
# rewrites the /data/... device paths into C:\... and every adb call fails.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"          # src/server/root
SERVER="$(cd "$HERE/.." && pwd)"               # src/server
ADB="${ADB:-adb}"
TARGET="${1:+-s $1}"
CERT="${CERT:-$SERVER/certs/ca.crt}"

# adb.exe is a native Windows binary: it cannot read MSYS paths like /c/..., so
# local paths are converted explicitly whenever they are handed to adb.
win() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# Resolve the interpreter once. (A shell function named `py` that runs `py` would
# recurse into itself and hang -- which is exactly what the first version did.)
PYTHON="$(command -v py || command -v python3 || command -v python || true)"
[ -n "$PYTHON" ] || { echo "no python interpreter on PATH" >&2; exit 1; }

[ -f "$CERT" ] || { echo "no certificate at $CERT -- run: py windstock/tools/gen_certs.py" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required (gen_certs.py needs it too)" >&2; exit 1; }
command -v "$ADB" >/dev/null 2>&1 || { echo "adb not found; set ADB=/path/to/adb" >&2; exit 1; }

IP="${IP:-$("$PYTHON" -c "import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(('8.8.8.8', 80))
print(s.getsockname()[0])")}"
# openssl is a native binary too: hand it a Windows path on Windows/Git Bash.
HASH="$(openssl x509 -in "$(win "$CERT")" -subject_hash_old -noout)"
echo "certificate : $CERT  -> $HASH.0"
echo "redirect IP : $IP"

# Hostnames the Windstock server intercepts (windstock/net/dns_redirect.py).
HOSTNAMES="pgorelease.nianticlabs.com sso.pokemon.com holo.nianticlabs.com www.nianticlabs.com nianticlabs.com pokemon.com pokemongo.zendesk.com"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. hosts: the phone's own file plus our pins, so nothing already mapped is
# lost. Re-running this must not stack a second block: while the module is
# installed, pulling /system/etc/hosts returns OUR mounted file, so any earlier
# pins are stripped before the new ones go on.
$ADB $TARGET pull /system/etc/hosts "$(win "$WORK/hosts")" >/dev/null 2>&1 \
    || printf '127.0.0.1 localhost\n::1 ip6-localhost\n' > "$WORK/hosts"
awk -v hosts="$HOSTNAMES" '
    BEGIN { n = split(hosts, h, " ") }
    /^# Windstock private server/ { next }
    {
        last = $NF
        for (i = 1; i <= n; i++) if (last == h[i]) next
        print
    }
' "$WORK/hosts" > "$WORK/hosts.clean"
mv "$WORK/hosts.clean" "$WORK/hosts"
{
    echo
    echo "# Windstock private server (removed by deleting the windstock-ca module)"
    for h in $HOSTNAMES; do echo "$IP $h"; done
} >> "$WORK/hosts"

# 2. stage the module on the phone
$ADB $TARGET shell 'rm -rf /data/local/tmp/windstock-mod && mkdir -p /data/local/tmp/windstock-mod/cert'
$ADB $TARGET push "$(win "$HERE/windstock-ca/post-fs-data.sh")" /data/local/tmp/windstock-mod/post-fs-data.sh >/dev/null
$ADB $TARGET push "$(win "$HERE/windstock-ca/module.prop")"    /data/local/tmp/windstock-mod/module.prop    >/dev/null
$ADB $TARGET push "$(win "$CERT")" "/data/local/tmp/windstock-mod/cert/$HASH.0" >/dev/null
$ADB $TARGET push "$(win "$WORK/hosts")" /data/local/tmp/windstock-mod/hosts >/dev/null

$ADB $TARGET push "$(win "$HERE/windstock-ca/install.sh")" /data/local/tmp/windstock-mod/install.sh >/dev/null

# 3. install it as root. The root-side work lives in install.sh (pushed above)
# rather than in a nested su -c string, which is where quoting bugs hide.
$ADB $TARGET shell 'su -c "sh /data/local/tmp/windstock-mod/install.sh"'

echo
echo "Reboot so the mounts exist before Zygote starts, then verify:"
echo "  adb reboot"
echo "  ./verify.sh"
echo
echo "Before playing, prepare-phone.sh keeps the game from hanging on a phone"
echo "that is short of RAM:"
echo "  ./prepare-phone.sh"
