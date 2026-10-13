"""Removes every piece of debug metadata a dex still carries after R8.

R8 renames, shrinks and inlines, but it will not drop the two things it was asked to drop: a
`-keepattributes !LineNumberTable,!SourceFile` rule is parsed and then ignored (measured - see
README-OBFUSCATION.md), which is why the shipped dex still had line numbers on 10 of 270 methods and
a source-file reference on all 56 classes. This rewrites the dex directly instead:

  * every `code_item.debug_info_off` -> 0, deleting the debug_info_item table (line numbers, local
    variable names, source positions) from the file, so no method has a line number at all;
  * every `class_def_item.source_file_idx` -> NO_INDEX, so no class names a source file;
  * the dex header's SHA-1 signature and adler32 checksum are recomputed, because the platform's
    dex verifier fails the whole file on a checksum mismatch.

None of that is metadata the runtime needs: ART loads and verifies without any of it. Nothing is
added, moved or re-indexed, which is what keeps this safe - the string/type/method tables the code
actually uses are byte-identical, so the layout the compiled code depends on cannot shift.

Usage:  py build/strip_dex_debug.py --in app.apk --out app.stripped.apk
The output is repacked (so lib/*.so alignment is lost) and must be zipalign'ed and signed again;
build-obfuscated.sh does both.
"""
import argparse
import hashlib
import struct
import sys
import zipfile
import zlib

NO_INDEX = 0xFFFFFFFF
DEX_MAGIC = b"dex\n"


def uleb128(data, offset):
    """Reads one LEB128 value, returning (value, next offset)."""
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def strip_dex(data, path):
    """Returns (new bytes, methods_cleared, classes_cleared)."""
    if data[:4] != DEX_MAGIC:
        raise SystemExit(f"{path}: not a dex file")
    class_defs_size, class_defs_off = struct.unpack_from("<II", data, 96)

    buf = bytearray(data)
    methods = classes = 0

    for index in range(class_defs_size):
        base = class_defs_off + index * 32
        source_file_idx = struct.unpack_from("<I", buf, base + 16)[0]
        if source_file_idx != NO_INDEX:
            struct.pack_into("<I", buf, base + 16, NO_INDEX)
            classes += 1
        class_data_off = struct.unpack_from("<I", buf, base + 24)[0]
        if class_data_off == 0:
            continue
        cursor = class_data_off
        static_fields, cursor = uleb128(buf, cursor)
        instance_fields, cursor = uleb128(buf, cursor)
        direct_methods, cursor = uleb128(buf, cursor)
        virtual_methods, cursor = uleb128(buf, cursor)
        for _ in range(static_fields + instance_fields):
            _idx, cursor = uleb128(buf, cursor)
            _flags, cursor = uleb128(buf, cursor)
        for _ in range(direct_methods + virtual_methods):
            _idx, cursor = uleb128(buf, cursor)
            _flags, cursor = uleb128(buf, cursor)
            code_off, cursor = uleb128(buf, cursor)
            if code_off:
                struct.pack_into("<I", buf, code_off + 8, 0)
                methods += 1

    # The header's own integrity fields: SHA-1 over everything after the signature, adler32 over
    # everything after the checksum. A dex whose checksum is stale is rejected as corrupt.
    struct.pack_into("<20s", buf, 12, hashlib.sha1(bytes(buf[32:])).digest())
    struct.pack_into("<I", buf, 8, zlib.adler32(bytes(buf[12:])) & 0xFFFFFFFF)
    return bytes(buf), methods, classes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", dest="target", required=True)
    args = parser.parse_args()

    with zipfile.ZipFile(args.source) as source, zipfile.ZipFile(args.target, "w") as target:
        stripped = 0
        for info in source.infolist():
            blob = source.read(info.filename)
            if info.filename.startswith("classes") and info.filename.endswith(".dex"):
                blob, methods, classes = strip_dex(blob, info.filename)
                stripped += 1
                print(f"{info.filename}: {methods} methods lost their debug info, "
                      f"{classes} classes lost their source file")
            target.writestr(info, blob)

    if not stripped:
        sys.exit("no dex entries found: nothing was stripped")
    print(f"wrote {args.target} (repack: zipalign and sign it before shipping)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
