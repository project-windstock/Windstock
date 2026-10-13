#!/bin/bash
# Check, from the host, that the phone really trusts the Windstock CA and really
# pins the Niantic hosts -- in the namespaces apps use, not just as root. Safe to
# run any time: it only reads.
#
#   ./verify.sh            # first device
#   ./verify.sh <serial>
#
# On Windows/Git Bash run it as MSYS_NO_PATHCONV=1 ./verify.sh (see deploy.sh).
set -eu

ADB="${ADB:-adb}"
TARGET="${1:+-s $1}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# adb.exe is a native Windows binary and cannot read MSYS paths like /c/...
win() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

$ADB $TARGET push "$(win "$HERE/windstock-ca/verify-device.sh")" /data/local/tmp/windstock-verify.sh >/dev/null
$ADB $TARGET shell 'su -c "sh /data/local/tmp/windstock-verify.sh"'
