#!/bin/sh
# Builds the debuggable APK with the verbose app log.
#
#   sh project2/build-debug.sh
#
# This is the same launcher as build-obfuscated.sh ships, built for diagnosis instead of hiding:
# real class and method names, real line numbers, android:debuggable="true", no R8, and the
# DebugLog from app/src/debug, which writes everything to logcat *and* to
# /sdcard/Android/data/com.chucny.translator.bridge/files/zb-app-log.txt (see
# README-DEBUG-BUILD.md for how to read it and what it says).
#
# It is signed with the same key as the obfuscated build and keeps the same applicationId, so it
# installs over the installed app as an upgrade: `adb install -r project2/translator-debug.apk`.
# The imported plugin and everything under /data/data/com.chucny.translator.bridge survive, which
# is the point - the crash being investigated stays reproducible.
#
# It deliberately skips everything slow: no payload repack (the containers staged by the last
# build are reused), no dex rewrite, no zipalign/apksigner post-pass on a 66 MB archive. Gradle
# signs it with the same key it already has.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
LAUNCHER="$HERE/launcher"

# Gradle 8.9 predates this machine's default JDK 26 and dies on it with a bare version number, so
# the toolchain JDK is selected explicitly; the Windows gradle launcher needs a Windows path.
JDK="$ROOT/toolchain/jdk-17.0.20.1+1"
if command -v cygpath >/dev/null 2>&1; then
    JAVA_HOME=$(cygpath -w "$JDK")
else
    JAVA_HOME=$JDK
fi
export JAVA_HOME

# The two encrypted containers, the generated Keys class and the bridge library under its
# generated name. Only built when they are missing: repacking 66 MB buys this build nothing, and
# the runtime they carry is exactly the one the obfuscated build ships.
#
# The one case that is not reused is the staged payloads of a build made with --no-bundled-app:
# those carry no client at all, and reusing them would quietly turn this build into one with no
# bundled app too (its Keys and its assets would still agree, so nothing would look broken).
KEYS="$HERE/build/payloads/src/com/zettabridge/launcher/Keys.java"
if [ ! -f "$KEYS" ] || grep -q "CLIENT_ASSET = {};" "$KEYS"; then
    sh "$HERE/strip-runtime-binaries.sh"
    BUNDLED_APP=bundled py "$HERE/build/pack_payloads.py"
fi

# assembleDebug only: no R8, no shrinking, no lintVital. Fast on purpose.
"$ROOT/toolchain/gradle-8.9/bin/gradle" -p "$LAUNCHER" assembleDebug --offline

BUILT="$LAUNCHER/app/build/outputs/apk/debug/app-debug.apk"
cp "$BUILT" "$HERE/translator-debug.apk"

echo
echo "built $HERE/translator-debug.apk"
wc -c < "$HERE/translator-debug.apk"

# Read back what was actually packaged, rather than trusting the variant name: the debuggable flag
# is the whole point of this artifact, and the verbose logger has to be in the dex.
#
# `py` is a Windows program, so it needs Windows paths even when this shell is told to stop
# converting them (MSYS_NO_PATHCONV=1, which adb callers often set) - hence cygpath here rather than
# relying on the shell's own conversion.
CHECK="$HERE/build/check_debug_build.py"
APK="$HERE/translator-debug.apk"
if command -v cygpath >/dev/null 2>&1; then
    CHECK=$(cygpath -w "$CHECK")
    APK=$(cygpath -w "$APK")
fi
py "$CHECK" "$APK"
