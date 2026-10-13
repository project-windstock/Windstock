"""Packs everything this build ships as data into two encrypted payload blobs.

Why: unzipping an APK used to be enough. `assets/app/base.apk` was the client APK sitting
uncompressed and readable, `assets/zb/**` was the whole 32-bit runtime with its real file names
(`zbhost`, `libzbproxy.so`, the bionic sysroot), and `lib/arm64-v8a/libzbridge.so` was a plain ELF.
Anyone could lift the runtime out and drop it into their own app.

What this writes instead, all of it under `build/payloads/`:

  assets/<name>.bin                  the arm32 runtime bundle: every file, deflated, encrypted
  assets/<name>.bin                  the bundled client APK plus its descriptor, encrypted
  assets/<gibberish>/**              the decoy layer: ~1650 files in ~1260 directories of random
                                     bytes named like a build's leftovers, plus bait that carries a
                                     fake ELF/dex/zip/XML header, under a fixed byte budget (see
                                     write_decoy_layer)
  jniLibs/arm64-v8a/lib<name>.so     the arm64 bridge, under a name that says nothing
  src/com/zettabridge/launcher/Keys.java   the container names, the library name and the key

The names and the key are freshly random on every build (set PAYLOAD_SEED for a reproducible one),
so two APKs built from this source share no name and no key. `Keys.java` is generated into
`build/payloads/src` and wired in as a source directory by the Gradle file: no source file in the
repository is rewritten.

Container layout (little-endian), read by launcher/Payloads.java:

    [0,16)   IV, in the clear (an IV is not a secret)
    [16,..)  AES-128-CTR ciphertext of
                 u32 version = 1
                 u32 metaLen, meta bytes          runtime: the bundle version; client: the descriptor
                 u32 count
                 count x { u16 nameLen, name, u64 dataOff, u32 storedLen, u32 rawLen, u8 flags }
                 the entry data, at the offsets above, relative to the start of the ciphertext

flags bit 0 = the entry is zlib-deflated. Deflating before encrypting is what keeps this inside the
size budget: the payload is incompressible to aapt afterwards, but it was compressed first.

What this does not do, said here because the README repeats it: the key is in the dex, so a person
who reads the launcher's code can unpack all of this. What it removes is the free ride - "unzip and
take the .so" - and the file names that told a reader what they were looking at.
"""
import hashlib
import os
import random
import shutil
import struct
import sys
import zlib
from pathlib import Path

# The decoy layer: names that look like a build left its guts behind, in directories that nest like a
# real asset tree, with contents that are random bytes - and, in a minority of them, a header that
# claims to be an ELF, a dex, a zip or an XML. Android never enumerates assets, so none of this is
# looked at by the platform or by the app: it exists so that "unzip and find the interesting file"
# stops being a five second job. Kept to a fixed byte budget (the budgets are env-tunable).
TRASH_FILES = int(os.environ.get("TRASH_FILES", "1600"))
TRASH_DIRS = int(os.environ.get("TRASH_DIRS", "420"))
TRASH_CONTENT_BUDGET = int(os.environ.get("TRASH_BUDGET", str(192 * 1024)))
# Bait: decoys that lie about what they are (see BAIT_HEADERS). Sizes stay under the audit's
# REAL_ASSET_BYTES bar, so the two real containers remain the only large assets in the archive, and
# TRASH_CONTENT_BUDGET + TRASH_BAIT_BUDGET must stay under MAX_DECOY_BYTES (1 MB):
# 192 kB of noise (1610 files use ~137 kB of it) + 640 kB of bait = under 800 kB, checked by the
# audit on every build.
TRASH_BAIT_FILES = int(os.environ.get("TRASH_BAIT_FILES", "52"))
# 320 kB, not 640: the spread-out payload chunks and the .so noise below both draw on the same 1 MB
# decoy budget the audit enforces, and the bait is the least valuable part of it.
TRASH_BAIT_BUDGET = int(os.environ.get("TRASH_BAIT_BUDGET", str(320 * 1024)))
# Junk libraries: files named like a library, carrying an ELF header, in the layouts a reader scans
# for one. 100+ of them means "find the .so" has no single answer. Each one costs a packaging page
# (see decoy_extension), which is why their contents stay small.
TRASH_SO_FILES = int(os.environ.get("TRASH_SO_FILES", "110"))
# The payloads are not one file each any more: the ciphertext is split into this many pieces, each
# written into its own junk directory under its own junk name. Order lives in the generated Keys
# class, so the pieces are useless one by one and the archive has nothing that looks like a payload.
# Chunks must stay at or above audit_apk.py's REAL_ASSET_BYTES bar (64 kB) so they are counted as
# payload rather than as decoys - which also keeps them out of the decoy byte budget.
RUNTIME_CHUNKS = int(os.environ.get("RUNTIME_CHUNKS", "20"))
CLIENT_CHUNKS = int(os.environ.get("CLIENT_CHUNKS", "80"))
# Whether this build carries the bundled client at all. "none" produces the same launcher with an
# empty client container (an empty CLIENT_ASSET in the generated Keys class), which is the build for
# someone who wants to bring their own app: the runtime payload, the junk layer, R8 and the signing
# key are all identical, so the two APKs differ in exactly one thing.
BUNDLED_APP = os.environ.get("BUNDLED_APP", "bundled").strip().lower()
TRASH_EXTENSIONS = (".so", ".bin", ".xml", ".dat", ".json", ".db", ".pkg", ".map", ".sig",
                   ".idx", ".cfg", ".ko")
TRASH_WORDS = ("runtime", "cache", "driver", "shader", "texture", "audio", "video", "codec",
               "kernel", "module", "crypto", "payload", "update", "stream", "session", "binder",
               "socket", "layout", "resource", "asset", "config", "layer", "surface", "buffer",
               "device", "profiler", "trace", "symbol", "stack", "heap", "thread", "jni", "gpu",
               "hal", "vendor", "system", "overlay", "patch", "delta", "block", "segment",
               "frame", "packet", "route", "token", "vector", "matrix")
# Paths the bait is laid out in, seen from the assets root. These are the layouts an unwrapping
# script or a human scans for - the loader's library directory, the dex/odex/oat of a runtime, a
# signature directory, resource and asset trees - so every one of them has several members that are
# garbage. Every template stays under assets/, where the platform never looks, and none of them may
# contain a forbidden token.
TRASH_BAIT_TREES = (
    "{a}/lib/arm64-v8a/lib{b}.so",
    "{a}/lib/armeabi-v7a/lib{b}.so",
    "{a}/split_config.arm64_v8a/{b}.dex",
    "{a}/dexopt/{b}.odex",
    "{a}/oat/arm64/{b}.odex",
    "{a}/META-INF/{b}.RSA",
    "{a}/res/raw/{b}.bin",
    "{a}/resource/{b}.xml",
    "{a}/flutter_assets/assets/{b}.dat",
    "{a}/profiling/{b}.cfg",
    "{a}.bin",
    "{a}.so",
)
# What the bait claims to be, by the extension its path ends in: a header a scanner recognises, then
# random bytes. Nothing here is a real file, and none of it is loadable - the point is that "find the
# .so", "find the dex" and "find the container" stop having one obvious answer.
BAIT_HEADERS = {
    ".so": (b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + b"\x02\x00\xb7\x00",
            b"\x7fELF\x01\x01\x01\x00" + b"\x00" * 8 + b"\x03\x00\x28\x00"),
    ".dex": (b"dex\n035\x00", b"dex\n039\x00"),
    ".odex": (b"dey\n036\x00", b"dex\n035\x00"),
    ".bin": (b"PK\x03\x04\x14\x00\x00\x00\x08\x00", b"\x1f\x8b\x08\x00\x00\x00\x00\x00"),
    ".RSA": (b"\x30\x82\x02\x1a\x06\x09\x2a\x86\x48\x86\xf7\x0d",),
    ".xml": (b'<?xml version="1.0" encoding="utf-8"?>\n<',),
    ".dat": (b"PK\x03\x04\x14\x00\x00\x00\x08\x00", b"\x1f\x8b\x08\x00",),
    ".cfg": (b'<?xml version="1.0" encoding="utf-8"?>\n<', b"PK\x03\x04\x14\x00"),
}

# Tokens the audit treats as "this entry name gives something away"; a decoy must never trip it.
TRASH_FORBIDDEN = ("zb", "zettabridge", "bridge", "proxy", "sysroot", "host", "base", "bundled",
                   "pokemon", "niantic", "chucny", "translator", "libz")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
STAGED = PROJECT / "build" / "launcher"
BUNDLED = PROJECT / "build" / "bundled-assets" / "app" / "base.apk"
DESCRIPTOR = PROJECT / "bundled-app.txt"
OUT = PROJECT / "build" / "payloads"

VERSION = 1
FLAG_DEFLATED = 1


def random_name(rng, length=12):
    """A name with no vowel and no digits-only shape: nothing to read into it."""
    alphabet = "bcdfghjklmnpqrstvwxyz0123456789"
    name = rng.choice("bcdfghjklmnpqrstvwxyz") + "".join(rng.choice(alphabet) for _ in range(length - 1))
    return name


def decoy_extension(rng):
    """Mostly cheap extensions, and a few .so files.

    Packaging aligns every uncompressed `.so` in the assets tree to a 4096-byte page so the loader can
    mmap it, so a decoy named `.so` costs a page even though its contents are 40 random bytes. A few
    hundred of those turned a 145 kB decoy layer into a 2.1 MB one, which is why the extension is
    drawn by weight: `.so` shows up often enough to be part of the noise, not often enough to pay for
    hundreds of pages.
    """
    if rng.random() < 0.012:
        return ".so"
    return rng.choice(tuple(ext for ext in TRASH_EXTENSIONS if ext != ".so"))


def decoy_name(rng, taken, extension=None):
    """A name a build system might plausibly have produced, and nothing more.

    Two shapes: a long run of random letters and digits (10-32 characters), or two or three words
    from a build vocabulary joined by underscores, sometimes with random characters appended. Never
    a token that would name something real in this app (see TRASH_FORBIDDEN).
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    for _ in range(64):
        if rng.random() < 0.55:
            stem = "".join(rng.choice(alphabet) for _ in range(rng.randint(10, 32)))
        else:
            stem = "_".join(rng.choice(TRASH_WORDS) for _ in range(rng.randint(2, 3)))
            if rng.random() < 0.5:
                stem += "_" + "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 8)))
        stem = stem[:32]
        name = stem + (extension or decoy_extension(rng))
        lowered = name.lower()
        if name in taken or any(bad in lowered for bad in TRASH_FORBIDDEN):
            continue
        taken.add(name)
        return name
    raise RuntimeError("cannot generate a decoy name that passes the filter")


def decoy_content(rng, size):
    """Random bytes for the plain decoys, which claim nothing about themselves at all."""
    return bytes(rng.getrandbits(8) for _ in range(size))


def bait_content(rng, name, size):
    """Random bytes wearing a header, for the bait: this is the decoy that lies about its format."""
    extension = os.path.splitext(name)[1]
    headers = BAIT_HEADERS.get(extension)
    body = bytes(rng.getrandbits(8) for _ in range(size))
    if not headers:
        return body
    header = rng.choice(headers)
    return header + body[len(header):] if len(header) < len(body) else body


def bait_stem(rng):
    """A bare name with no extension, for the path templates above."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(10, 20)))


def bait_path(rng, taken, template):
    """Fills a template with fresh names, refusing anything the audit would call a give-away."""
    for _ in range(64):
        relative = template.format(a=bait_stem(rng), b=bait_stem(rng))
        name = relative.rsplit("/", 1)[-1]
        if name in taken or any(bad in relative.lower() for bad in TRASH_FORBIDDEN):
            continue
        taken.add(name)
        return relative
    raise RuntimeError("cannot generate a bait path that passes the filter")


def junk_directory(rng, assets, taken, depth=None):
    """Creates one nested junk directory under assets/ and returns it.

    Separate from the decoy layer's own tree so the payload chunks land in directories that are not
    distinguishable from the thousands of others: same vocabulary, same nesting, same lengths.
    """
    depth = depth or rng.randint(1, 4)
    for _ in range(64):
        chain = [decoy_name(rng, taken)[:24].split(".")[0] for _ in range(depth)]
        if all(not any(bad in part.lower() for bad in TRASH_FORBIDDEN) for part in chain):
            path = assets.joinpath(*chain)
            path.mkdir(parents=True, exist_ok=True)
            return path
    raise RuntimeError("cannot generate a junk directory that passes the filter")


def write_chunks(rng, assets, blob, count, taken, directories):
    """Splits one container into `count` pieces in junk directories, and returns their asset paths.

    Order is the only thing that makes the pieces a payload, and that order is not in the archive:
    it is written into the generated Keys class (see main). A reader who unpacks the APK sees
    `count` more opaque files with meaningless names inside meaningless directories - which is
    exactly what the decoy layer already trains them to ignore.
    """
    count = max(1, min(count, max(1, len(blob) // (70 * 1024))))
    size, remainder = divmod(len(blob), count)
    paths = []
    at = 0
    for index in range(count):
        length = size + (1 if index < remainder else 0)
        directory = rng.choice(directories) if rng.random() < 0.5 else junk_directory(rng, assets, taken)
        name = decoy_name(rng, taken, rng.choice((".bin", ".dat", ".pkg", ".idx", ".sig", ".cfg")))
        (directory / name).write_bytes(blob[at:at + length])
        paths.append((directory / name).relative_to(assets).as_posix())
        at += length
    return paths


def write_decoy_layer(rng, assets, reserved, extra_directories=()):
    """Fills assets/ with files and directories that mean nothing, under a byte budget.

    Two kinds: plain noise (tiny, random, and silent about what it is) and bait (large enough to
    rank above that noise, wearing the header of a format worth looking for).
    """
    taken = set(reserved)
    directories = [path for path in extra_directories if path.is_dir()]
    for depth in range(1, 6):
        for _ in range(max(1, TRASH_DIRS // 5)):
            chain = [decoy_name(rng, taken)[:24].split(".")[0] for _ in range(depth)]
            directories.append(assets.joinpath(*chain))
    # Explicit directory entries, so the empty ones show up as directories rather than vanishing.
    empty = 0
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        if rng.random() < 0.25:
            empty += 1

    written = 0
    content = 0
    while written < TRASH_FILES and content < TRASH_CONTENT_BUDGET:
        directory = rng.choice(directories)
        roll = rng.random()
        if roll < 0.7:
            size = rng.randint(8, 48)
        elif roll < 0.95:
            size = rng.randint(49, 256)
        else:
            size = rng.randint(257, 1024)
        size = min(size, TRASH_CONTENT_BUDGET - content)
        name = decoy_name(rng, taken, decoy_extension(rng))
        (directory / name).write_bytes(decoy_content(rng, size))
        written += 1
        content += size

    # aapt drops directories that stay empty, and a directory that is not there is one less thing for
    # a reader to walk: give every one of them at least one file, so all of them survive packaging.
    filled = 0
    for directory in directories:
        if not any(path.is_file() for path in directory.iterdir()):
            size = min(rng.randint(8, 48), TRASH_CONTENT_BUDGET - content)
            (directory / decoy_name(rng, taken)).write_bytes(decoy_content(rng, size))
            written += 1
            content += size
            filled += 1

    # The bait: decoys that are big enough to sort above the noise and that carry the header of a
    # format a scanner or a reader is looking for. Nothing in the archive now answers "where is the
    # library / the dex / the container" with one file - each of those questions has five to ten
    # candidates, in trees that look like the real layouts, and every one of them is garbage.
    # The bait has its own budget rather than sharing the noise's: the noise ceiling is what keeps
    # 1600 tiny files cheap, and the bait is what makes a size/format listing useless. Their sum is
    # what has to stay under the audit's MAX_DECOY_BYTES (1 MB), which is why neither may grow freely.
    bait = bait_bytes = 0
    while bait < TRASH_BAIT_FILES:
        room = TRASH_BAIT_BUDGET - bait_bytes
        size = min(rng.randint(3 * 1024, 24 * 1024), room)
        if size < 512:
            break
        relative = bait_path(rng, taken, rng.choice(TRASH_BAIT_TREES))
        path = assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bait_content(rng, relative, size))
        bait += 1
        bait_bytes += size
        content += size
        written += 1

    # The junk libraries: a library name, a real ELF header for the architecture its path claims,
    # and random bytes behind it, in the exact trees a reader greps for a .so. None of them loads.
    libs = lib_bytes = 0
    for _ in range(TRASH_SO_FILES):
        size = rng.randint(512, 3072)
        relative = bait_path(rng, taken, rng.choice((
            "{a}/lib/arm64-v8a/lib{b}.so",
            "{a}/lib/armeabi-v7a/lib{b}.so",
            "{a}/jni/arm64-v8a/lib{b}.so",
            "{a}/vendor/lib64/lib{b}.so",
            "lib{b}.so",
        )))
        relative = relative.replace("/" + relative.rsplit("/", 1)[-1], "/lib" + relative.rsplit("/", 1)[-1])
        path = assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bait_content(rng, relative, size))
        libs += 1
        lib_bytes += size
        content += size
        written += 1
    return written, len(directories), filled, content, bait, bait_bytes, libs, lib_bytes


def deflate(data, level):
    packed = zlib.compress(data, level)
    return packed if len(packed) < len(data) else None


def build_container(meta, entries, key, iv):
    """One container holding `meta` and `entries` = [(name, raw bytes)], deflated where it pays.

    Entry offsets are relative to the start of the ciphertext, i.e. to the end of the IV, because
    that is the coordinate system the reader has when it opens the asset: it has consumed the IV and
    is about to consume the header. Getting that wrong once made the payload undecodable on the
    device, which is why build/verify_payloads.py now checks the offsets against the staged tree.
    """
    meta_bytes = meta.encode("utf-8")
    header_length = 4 + 4 + len(meta_bytes) + 4
    for name, _raw in entries:
        header_length += 2 + len(name.encode("utf-8")) + 17

    index = bytearray()
    data_section = bytearray()
    index += struct.pack("<I", len(entries))
    for name, raw in entries:
        packed = deflate(raw, 6 if len(raw) > 4_000_000 else 9)
        flags = FLAG_DEFLATED if packed else 0
        stored = packed if packed else raw
        offset = header_length + len(data_section)
        data_section += stored
        encoded = name.encode("utf-8")
        index += struct.pack("<H", len(encoded)) + encoded
        index += struct.pack("<QIIB", offset, len(stored), len(raw), flags)

    body = bytearray()
    body += struct.pack("<I", VERSION)
    body += struct.pack("<I", len(meta_bytes)) + meta_bytes
    body += index
    body += data_section
    if len(body) != header_length + len(data_section):
        sys.exit("container header length is not what the index assumed")

    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(bytes(body)) + encryptor.finalize()
    return bytes(iv) + ciphertext


def runtime_entries():
    root = STAGED / "assets" / "zb"
    if not root.is_dir():
        sys.exit(f"missing the staged runtime at {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append((path.relative_to(root).as_posix(), path.read_bytes()))
    if not entries:
        sys.exit(f"no runtime files under {root}")
    return entries


def main():
    seed = os.environ.get("PAYLOAD_SEED")
    rng = random.Random(seed) if seed else random.SystemRandom()
    key = (hashlib.sha256(("key:" + seed).encode()).digest()[:16] if seed else os.urandom(16))

    if BUNDLED_APP not in ("bundled", "none"):
        sys.exit(f"BUNDLED_APP must be 'bundled' or 'none', not {BUNDLED_APP!r}")
    if BUNDLED_APP == "bundled":
        if not BUNDLED.is_file():
            sys.exit(f"missing the bundled client at {BUNDLED} "
                     f"(set BUNDLED_APP=none to build without one)")
        if not DESCRIPTOR.is_file():
            sys.exit(f"missing the bundled app descriptor at {DESCRIPTOR}")

    version_file = STAGED / "assets" / "zb-version.txt"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else "0"

    OUT.mkdir(parents=True, exist_ok=True)
    assets = OUT / "assets"
    assets.mkdir(exist_ok=True)
    # A stale container (or the whole decoy tree) from an earlier build would otherwise ship next to
    # the new one.
    for old in assets.iterdir():
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()

    # The junk directories the chunks will live in are made first, so the decoy layer can fill and
    # count them like any other directory it owns.
    taken = set()
    payload_dirs = [junk_directory(rng, assets, taken) for _ in range(6)]

    entries = runtime_entries()
    runtime_blob = build_container(version, entries, key, os.urandom(16))
    runtime_chunks = write_chunks(rng, assets, runtime_blob, RUNTIME_CHUNKS, taken, payload_dirs)

    # A build without the bundled client writes no client container at all, rather than an empty
    # one: an empty container would still be 32 bytes of ciphertext and an IV to carry, and the
    # reader treats "no pieces" and "no app" as the same thing anyway.
    client_blob = b""
    client_chunks = []
    if BUNDLED_APP == "bundled":
        descriptor = DESCRIPTOR.read_text(encoding="utf-8")
        client_blob = build_container(descriptor, [("app/base.apk", BUNDLED.read_bytes())], key,
                                      os.urandom(16))
        client_chunks = write_chunks(rng, assets, client_blob, CLIENT_CHUNKS, taken, payload_dirs)

    decoys = write_decoy_layer(rng, assets, taken, payload_dirs + [
        assets / path.rsplit("/", 1)[0] for path in runtime_chunks + client_chunks if "/" in path])

    library = "lib" + random_name(rng, 9) + ".so"
    jni = OUT / "jniLibs" / "arm64-v8a"
    jni.mkdir(parents=True, exist_ok=True)
    for old in jni.iterdir():
        old.unlink()
    source = STAGED / "jniLibs" / "arm64-v8a" / "libzbridge.so"
    if not source.is_file():
        sys.exit(f"missing the bridge library at {source}")
    (jni / library).write_bytes(source.read_bytes())

    if client_chunks:
        client_block = (
            "\n    /** The pieces of the bundled client container, in the order they have to be read. */\n"
            "    public static final String[] CLIENT_ASSET = {\n"
            + "".join(f'        "{path}",\n' for path in client_chunks)
            + "    };\n")
    else:
        client_block = (
            "\n    /** Empty on purpose: this build bundles no client. SeedPlugin reads an empty array\n"
            "     * as \"this build has no bundled app\", so the library starts empty and offers the\n"
            "     * file picker instead of installing something. */\n"
            "    public static final String[] CLIENT_ASSET = {};\n")

    generated = OUT / "src" / "com" / "zettabridge" / "launcher"
    generated.mkdir(parents=True, exist_ok=True)
    key_bytes = ", ".join(f"(byte) 0x{byte:02x}" for byte in key)
    (generated / "Keys.java").write_text(
        "package com.zettabridge.launcher;\n\n"
        "/** Generated by build/pack_payloads.py - do not edit, do not commit. Randomised per build. */\n"
        "public final class Keys {\n"
        f'    public static final String BRIDGE_LIBRARY = "{library[3:-3]}";\n'
        f'    public static final byte[] PAYLOAD_KEY = {{ {key_bytes} }};\n'
        "\n    /** The pieces of the runtime container, in the order they have to be read. */\n"
        "    public static final String[] RUNTIME_ASSET = {\n"
        + "".join(f'        "{path}",\n' for path in runtime_chunks)
        + "    };\n"
        + client_block
        + "\n    private Keys() {}\n"
        + "}\n",
        encoding="utf-8", newline="\n")

    print(f"runtime payload : {len(runtime_chunks)} chunks, {len(runtime_blob):,} bytes "
          f"from {sum(len(raw) for _, raw in entries):,} bytes of runtime")
    if client_chunks:
        print(f"client payload  : {len(client_chunks)} chunks, {len(client_blob):,} bytes")
    else:
        print("client payload  : none - this build bundles no app")
    print(f"decoy layer     : {decoys[0]} files in {decoys[1]} directories ({decoys[2]} of them empty), "
          f"{decoys[3]:,} bytes of content, of which {decoys[4]} files carrying a fake format header "
          f"({decoys[5]:,} bytes) and {decoys[6]} junk libraries ({decoys[7]:,} bytes)")
    print(f"bridge library  : jniLibs/arm64-v8a/{library}")
    print(f"generated       : src/com/zettabridge/launcher/Keys.java")
    return 0


if __name__ == "__main__":
    sys.exit(main())
