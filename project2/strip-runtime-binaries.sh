#!/bin/sh
# Removes every name this build can remove from the runtime binaries it ships.
#
# Why this is safe: dynamic loading uses .dynsym / DT_SYMTAB, which `strip` keeps. ZettaBridge's own
# loader reads the dynamic table only (`grep -r SHT_|sh_type|Elf.*Shdr` over core/src is empty, and
# core/src/elf_symbols.cpp works from DT_SYMTAB), and no Java in the launcher reads an ELF symbol
# table. The one thing that cannot be hidden is what the dynamic tables themselves have to name:
# ART resolves the Java_com_zettabridge_core_ZBridge_* entry points of libzbridge.so by name.
#
# What is covered, all of it inside project2/ (nothing outside is written):
#
#   guest/, guest/lib/*.so     the arm32 guest runtime. This is where the win is: ~1650 named
#                              functions of the translator's own runtime (zbhost 34, libGLESv2 518,
#                              libandroid 368, libzbjni 333, libGLESv1_CM 290, libEGL 92,
#                              libzbcompat 11, libjnigraphics 6) were the first thing a reader saw.
#   host/libzbproxy.so         the arm64 proxy the guest's libraries are routed through.
#   sysroot/system/lib/*.so    the arm32 bionic the guest links against. Those names are AOSP's and
#                              therefore public, but leaving 3573 of them in the APK while every
#                              other binary is anonymous only does the reader's work for them, so
#                              they go as well. .dynsym keeps everything the guest's code links to.
#   jniLibs/arm64-v8a/libzbridge.so   already stripped by the NDK; checked here all the same.
#
# Afterwards the compiler banner (.comment, which names the exact clang and LLD build) and the
# build-id note are dropped from all of them: pure fingerprint, nothing reads either.
#
# Run before build-obfuscated.sh. Safe to run twice. The device run in README-OBFUSCATION.md is what
# shows the guest still starts with the sysroot stripped.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT="$HERE/build/launcher"
TOOLS="$HERE/../toolchain/sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/windows-x86_64/bin"
STRIP="$TOOLS/llvm-strip.exe"
READELF="$TOOLS/llvm-readelf.exe"

for tool in "$STRIP" "$READELF"; do
    if [ ! -x "$tool" ]; then
        echo "missing NDK tool: $tool" >&2
        exit 1
    fi
done
if [ ! -d "$ROOT" ]; then
    echo "no staged runtime at $ROOT" >&2
    exit 1
fi

# Found rather than listed, so a runtime file added upstream is stripped without anyone having to
# remember this script. Paths under the staged runtime contain no spaces; `find` output is therefore
# safe to word-split, which keeps the loop in this shell so the final check can fail the build.
FILES=$(find "$ROOT" -type f \( -name '*.so' -o -name 'zbhost' \) | sort)
if [ -z "$FILES" ]; then
    echo "no runtime binaries under $ROOT" >&2
    exit 1
fi

for file in $FILES; do
    before=$(wc -c < "$file")
    "$STRIP" --strip-unneeded "$file"
    after=$(wc -c < "$file")
    printf 'stripped %-48s %9d -> %9d bytes\n' "${file#"$ROOT"/}" "$before" "$after"
done

for file in $FILES; do
    "$STRIP" --remove-section=.comment --remove-section=.note.gnu.build-id "$file" 2>/dev/null || true
done

failed=0
for file in $FILES; do
    if "$READELF" -S "$file" 2>/dev/null | grep -q '\.symtab'; then
        echo "STILL HAS .symtab: ${file#"$ROOT"/}" >&2
        failed=1
    fi
done
if [ "$failed" -ne 0 ]; then
    echo "refusing to build with symbol tables in the shipped runtime" >&2
    exit 1
fi

count=$(printf '%s\n' $FILES | wc -l)
echo "done: $count runtime binaries stripped of symbol tables and compiler banners"
