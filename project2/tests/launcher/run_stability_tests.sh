#!/bin/sh
# Host-side tests for the stability layer.
#
#   sh project2/tests/launcher/run_stability_tests.sh
#
# No device, no emulator, no Android SDK: the four classes these tests cover (the recovery decision
# table, the restart history, the crash policy, the guest lease) deliberately import nothing from
# Android, so they can be exercised on the machine that builds them. What that buys is the part of
# this layer that has to be right rather than merely present - and the lease test goes further than a
# unit test usually can, by taking the lock in a second JVM and killing it (see GuestRecoveryTest).
#
# Uses the project's own JDK: the machine's default here is newer than the toolchain this project
# builds with, and a test compiled against a different language level than the app is a test that can
# pass on code the app cannot build.
#
# Everything is compiled and run through paths relative to the repository root, so the same script
# works with a POSIX shell and with a Windows javac that would not understand /c/... paths.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
REPO=$(cd "$ROOT/.." && pwd)
OUT_REL="project2/build/stability-tests"
OUT="$ROOT/build/stability-tests"

JDK="${ZB_JDK:-$ROOT/../toolchain/jdk-17.0.20.1+1}"
if [ -x "$JDK/bin/javac.exe" ]; then
    JAVAC="$JDK/bin/javac.exe"
    JAVA="$JDK/bin/java.exe"
elif [ -x "$JDK/bin/javac" ]; then
    JAVAC="$JDK/bin/javac"
    JAVA="$JDK/bin/java"
else
    echo "no JDK at $JDK; set ZB_JDK to one" >&2
    exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"

# Relative paths on purpose: the JDK here is a Windows program and the shell is a POSIX one, and a
# relative path is the one spelling both of them agree on.
SOURCE="project2/launcher/app/src/main/java/com/zettabridge/launcher"

cd "$REPO"
"$JAVAC" -encoding UTF-8 -d "$OUT_REL" \
    "$SOURCE/GuestLease.java" \
    "$SOURCE/GuestRecovery.java" \
    "$SOURCE/CrashPolicy.java" \
    "project2/tests/launcher/LeaseHolder.java" \
    "project2/tests/launcher/GuestRecoveryTest.java"

"$JAVA" -ea -cp "$OUT_REL" com.zettabridge.launcher.GuestRecoveryTest
