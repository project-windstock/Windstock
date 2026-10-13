#!/bin/bash
# Put the phone into the state the game needs in order to run instead of hanging.
#
# The symptom this exists for (APATCH-ROOT.md is the full write-up): a guest game
# built into a phone that is short on RAM parks its Unity main thread on a futex
# inside the bridge -- zero frames presented, no crash, and the server sees its
# last request already answered. Reclaiming the cached background apps is what
# cleared it, so it is done here BEFORE a session rather than after a freeze.
#
# This is the phone's runtime hygiene only. Whether the phone TRUSTS the CA and
# PINS the hosts -- the other half of "it will not log in" -- is ../verify.sh.
#
# On Windows/Git Bash: MSYS_NO_PATHCONV=1 ./prepare-phone.sh
set -eu

ADB="${ADB:-adb}"
PKG="${PKG:-com.chucny.translator.bridge}"            # launcher + its :guest process
GAME="${GAME:-com.nianticlabs.pokemongo.classic}"
ACTIVITY="${ACTIVITY:-$PKG/com.zettabridge.launcher.GuestLaunchActivity}"

START=0
DROP=0
DRY=0
SERIAL=""
for arg in "$@"; do
    case "$arg" in
        --start)       START=1 ;;
        --drop-caches) DROP=1 ;;
        --dry-run)     DRY=1 ;;
        -h|--help)
            cat <<'EOF'
  ./prepare-phone.sh                 reclaim RAM, clear a stale guest
  ./prepare-phone.sh --start         ...and reopen the game cold
  ./prepare-phone.sh --drop-caches   also purge the page cache (root; opt-in)
  ./prepare-phone.sh --dry-run       print what would run, change nothing
  ./prepare-phone.sh <serial> ...    a specific device
EOF
            exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *)  SERIAL="$arg" ;;
    esac
done
TARGET=""
[ -n "$SERIAL" ] && TARGET="-s $SERIAL"

command -v "$ADB" >/dev/null 2>&1 || { echo "adb not found -- set ADB=/path/to/adb" >&2; exit 1; }
[ "$($ADB $TARGET get-state 2>/dev/null)" = "device" ] || {
    echo "no phone on USB -- nothing to prepare"
    exit 0
}

# Every state change goes through here, so --dry-run prints the real command
# instead of running it.
run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

mem() { $ADB $TARGET shell 'grep -E "^(MemAvailable|SwapFree)" /proc/meminfo' | sed 's/^/  /'; }

echo "=== free RAM before"
mem

echo "=== reclaiming RAM from cached background apps (this is the freeze fix)"
run "$ADB" $TARGET shell am kill-all

echo "=== clearing a stale guest (the launcher and its :guest process)"
run "$ADB" $TARGET shell am force-stop "$PKG"

if [ "$DROP" = 1 ]; then
    echo "=== dropping the page cache (root)"
    run "$ADB" $TARGET shell 'su -c "sync; echo 3 > /proc/sys/vm/drop_caches"' \
        || echo "  (skipped -- root refused)"
fi

echo "=== free RAM after"
mem

if [ "$START" = 1 ]; then
    echo "=== reopening $GAME"
    # The data URI and the clear-task flag are load-bearing: Android matches an
    # existing task against the intent that created it and ignores extras, so a
    # plain start revives whatever task is already there.
    run "$ADB" $TARGET shell am start -a android.intent.action.MAIN \
        --activity-clear-task -n "$ACTIVITY" \
        -d "translator://guest/$GAME" \
        --es "$PKG.PACKAGE" "$GAME"
fi
