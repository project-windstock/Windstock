"""Checks the packed payload containers the way the app reads them, before anything is installed.

    py project2/build/verify_payloads.py project2/pokemon-go-emulated-obfuscated.apk

The launcher decrypts two containers out of its assets (launcher/Payloads.java, packed by
build/pack_payloads.py). This walks the same layout with the key the build generated and compares
the result against the staged inputs:

  * runtime entries must come back byte-identical to project2/build/launcher/assets/zb/**;
  * the bundled client must inflate to a file that is byte-identical to the staged base.apk;
  * the descriptor's package/label/displayPackage must survive the round trip;
  * every offset must land exactly where the index says.

This exists because a one-off offset bug in the packer (data offsets relative to the data section
instead of to the ciphertext) built a container that parsed perfectly and failed on the device with
"incorrect header check". The device is the final proof, but this catches the class of bug in a
second instead - and the audit gate in build-obfuscated.sh runs it, so a broken payload cannot be
packaged.
"""
import re
import struct
import sys
import zlib
import zipfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover
    sys.exit("the cryptography package is required (pip install cryptography)")

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
KEYS = PROJECT / "build" / "payloads" / "src" / "com" / "zettabridge" / "launcher" / "Keys.java"
RUNTIME = PROJECT / "build" / "launcher" / "assets" / "zb"
CLIENT = PROJECT / "build" / "bundled-assets" / "app" / "base.apk"


def read_keys():
    if not KEYS.is_file():
        sys.exit(f"no generated keys at {KEYS} - run build/pack_payloads.py first")
    text = KEYS.read_text(encoding="utf-8")
    # The key is read out of its own declaration, not out of the whole file: a generated chunk name
    # can contain "0x" followed by two hex digits by chance ("...id0x3ab..."), and a scan over the
    # whole file then picks those up as extra key bytes - which is an AES key of the wrong length and
    # a gate that fails on a build that is perfectly fine. Measured: one build in a few, before this.
    declaration = re.search(r"PAYLOAD_KEY\s*=\s*\{([^}]*)\}", text)
    if not declaration:
        sys.exit("the generated Keys class has no PAYLOAD_KEY array")
    key = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-f]{2})", declaration.group(1)))
    if len(key) != 16:
        sys.exit(f"the generated payload key is {len(key)} bytes, expected 16")
    return {
        "key": key,
        "runtime": chunks_of(text, "RUNTIME_ASSET"),
        "client": chunks_of(text, "CLIENT_ASSET", allow_empty=True),
        "library": re.search(r'BRIDGE_LIBRARY = "([^"]+)"', text).group(1),
    }


def chunks_of(text, name, allow_empty=False):
    """The ordered asset paths of one container, as the generated Keys class lists them.

    An empty array is a statement, not a mistake, for the client: it is what a build with no
    bundled app writes (BUNDLED_APP=none), and the app reads it as "there is no bundled app".
    """
    match = re.search(name + r"\s*=\s*\{(.*?)\};\n", text, re.S)
    if not match:
        sys.exit(f"the generated Keys class has no {name} array")
    paths = re.findall(r'"([^"]+)"', match.group(1))
    if not paths and not allow_empty:
        sys.exit(f"the generated Keys class has an empty {name} array")
    return paths


def assemble(apk, chunks, label):
    """Rebuilds one container from its pieces, in the order Keys lists them."""
    available = set(apk.namelist())
    missing = [path for path in chunks if "assets/" + path not in available]
    if missing:
        sys.exit(f"{label}: {len(missing)} of {len(chunks)} pieces are not in the APK, "
                 f"first {missing[0]}")
    blob = b"".join(apk.read("assets/" + path) for path in chunks)
    print(f"{label:<10}: {len(chunks)} pieces, {len(blob):,} bytes reassembled from junk directories")
    return blob


def parse(blob, key):
    """Decrypts a container and returns its metadata, its index and its plaintext body."""
    if len(blob) < 32:
        raise ValueError("container is too short")
    iv, body = blob[:16], blob[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    plain = decryptor.update(body) + decryptor.finalize()

    offset = 0
    version, = struct.unpack_from("<I", plain, offset)
    offset += 4
    if version != 1:
        raise ValueError(f"unexpected container version {version}")
    meta_length, = struct.unpack_from("<I", plain, offset)
    offset += 4
    meta = plain[offset:offset + meta_length].decode("utf-8")
    offset += meta_length
    count, = struct.unpack_from("<I", plain, offset)
    offset += 4
    entries = []
    for _ in range(count):
        name_length, = struct.unpack_from("<H", plain, offset)
        offset += 2
        name = plain[offset:offset + name_length].decode("utf-8")
        offset += name_length
        data_offset, stored, raw, flags = struct.unpack_from("<QIIB", plain, offset)
        offset += 17
        entries.append((name, data_offset, stored, raw, flags))
    header_end = offset
    for name, data_offset, stored, raw, flags in entries:
        if data_offset < header_end:
            raise ValueError(f"{name} points inside the header")
        if data_offset + stored > len(plain):
            raise ValueError(f"{name} runs past the end of the container")
    return meta, entries, plain


def data_of(entry, plain):
    name, data_offset, stored, raw, flags = entry
    blob = plain[data_offset:data_offset + stored]
    if flags & 1:
        blob = zlib.decompress(blob)
    if len(blob) != raw:
        raise ValueError(f"{name}: {len(blob)} bytes after inflating, index says {raw}")
    return blob


def main():
    apk_path = Path(sys.argv[1] if len(sys.argv) > 1
                    else PROJECT / "pokemon-go-emulated-obfuscated.apk")
    keys = read_keys()
    apk = zipfile.ZipFile(apk_path)
    problems = []

    meta, entries, plain = parse(assemble(apk, keys["runtime"], "runtime"), keys["key"])
    # Paired by name, not by position: the packer sorts Path objects, which on this platform compares
    # case-folded, so its order is a valid ascending-offset order but not the one a string sort gives.
    staged = {path.relative_to(RUNTIME).as_posix(): path
              for path in RUNTIME.rglob("*") if path.is_file()}
    if len(entries) != len(staged):
        problems.append(f"runtime index lists {len(entries)} files, {len(staged)} are staged")
    previous_offset = -1
    for entry in entries:
        name, offset = entry[0], entry[1]
        path = staged.get(name)
        if path is None:
            problems.append(f"the index ships a file that is not staged: {name}")
            continue
        if offset < previous_offset:
            problems.append(f"the index is not in ascending offset order at {name} "
                            "- the launcher reads entries in index order")
        previous_offset = offset
        if data_of(entry, plain) != path.read_bytes():
            problems.append(f"runtime file differs after the round trip: {name}")
    missing = sorted(set(staged) - {entry[0] for entry in entries})
    if missing:
        problems.append("staged runtime files are not in the payload: " + ", ".join(missing))
    print(f"runtime   : {len(entries)} entries, meta={meta.strip()[:16]}, all byte-identical "
          f"to {RUNTIME.relative_to(PROJECT)}")

    if keys["client"]:
        meta, entries, plain = parse(assemble(apk, keys["client"], "client"), keys["key"])
        if len(entries) != 1:
            problems.append(f"client container holds {len(entries)} entries, expected 1")
        else:
            bundled = data_of(entries[0], plain)
            if bundled[:2] != b"PK":
                problems.append("the bundled client does not come back as a zip")
            if bundled != CLIENT.read_bytes():
                problems.append("the bundled client differs after the round trip")
            print(f"client    : {len(bundled):,} bytes back as {bundled[:4].hex()}, identical to "
                  f"{CLIENT.name}")
        if "package=" not in meta:
            problems.append("the descriptor did not survive the round trip")
        else:
            package = [line for line in meta.splitlines() if line.startswith("package=")][0]
            print(f"descriptor: {package}")
    else:
        # This build advertises no client pieces at all. There is no container to decrypt; what the
        # app does with the empty array is SeedPlugin.read()'s business (it answers "no bundled
        # app"), and the library screen then offers the file picker instead of installing anything.
        if CLIENT.is_file():
            print("client    : none - this build bundles no app (an APK is staged but unused)")
        else:
            print("client    : none - this build bundles no app")

    # The bridge library has to stay where the loader looks for it; every other .so in the archive is
    # junk in assets/, which is the point of the junk libraries (see pack_payloads.py).
    real = f"lib/arm64-v8a/lib{keys['library']}.so"
    libraries = [name for name in apk.namelist() if name.endswith(".so")]
    loadable = [name for name in libraries if name.startswith("lib/")]
    if real not in loadable:
        problems.append(f"the bridge library the keys name is not at {real}: loadable .so are {loadable}")
    else:
        print(f"library   : {real} is what ZBridge loads, among {len(loadable)} loadable .so and "
              f"{len(libraries) - len(loadable)} junk ones in assets/")

    if problems:
        print("\nPAYLOAD VERIFICATION FAILED")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("\npayload verification: the containers decrypt to exactly what was staged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
