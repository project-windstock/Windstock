"""Audit the obfuscated APK: says what a reverse engineer can still read out of it.

    py project2/build/audit_apk.py project2/pokemon-go-emulated-obfuscated.apk

Reports on the shipped artifact rather than on the build directory:

  * dex class/field/method counts, how many classes still carry a SourceFile attribute, and how
    many code items still carry debug info (line numbers). Line numbers matter: with them a
    decompiled listing can be lined up against the upstream Java source, which is public;
  * every class descriptor that still carries a meaningful name (com.zettabridge/**, com.chucny/**)
    instead of a dictionary name, and every string residue naming the upstream module;
  * packaging residues: the R8 mapping/seeds/usage files, native symbol tables, DWARF, and the
    clang banner that names the toolchain.

Exit status is 1 when something that should never ship is present (mapping/seeds/symbol tables/DWARF/
compiler banner), so the build script can run this as a gate.
"""
import re
import struct
import sys
import zipfile

NO_INDEX = 0xFFFFFFFF
ENDIAN_CONSTANT = 0x12345678

# Names that must survive R8, and why: the manifest-declared components the framework instantiates by
# name, the JNI bridge class whose name is half of every exported Java_* symbol, and Stubs$, which
# GuestInstrumentation swaps in by name. Anything else under a real package name is a residue.
EXPECTED_KEEPERS = re.compile(
    r"^L(com/zettabridge/launcher/(ZbApplication|LibraryActivity|PluginSwitchActivity|"
    r"GuestLaunchActivity|SettingsActivity|LaunchFailureActivity|GuestSupervisor|"
    r"Stubs\$[A-Za-z]+|Stubs\$Base)|com/zettabridge/core/ZBridge);$")

# Never allowed inside the APK at all.
FORBIDDEN_ENTRY = re.compile(r"(mapping\.txt|seeds\.txt|usage\.txt|configuration\.txt|"
                             r"\.symbols$|\.dSYM|\.a$|\.o$|/debug/)")

# Names that must not appear in the APK's own entry names: with the runtime and the bundled client
# packed into encrypted containers, nothing in the listing may describe what is inside.
LEAKY_ENTRY_NAME = re.compile(
    r"(zbhost|libzb|zbridge|zbproxy|sysroot|base\.apk|bundled|pokemon|niantic|zb[-_/]|/zb/)", re.I)

# An ELF or zip signature anywhere inside a payload container means it shipped readable.
# An ELF, zip, dex or XML signature inside a *large* asset means a payload shipped readable. The
# bar is a size, not a name: the decoy layer is made of tiny files that are allowed to look like
# something (that is the point of it), while no small file could hide the 60 MB client or the
# runtime bundle. Anything at or above this is real data and must not show a format signature.
PLAINTEXT_MAGIC = (b"\x7fELF", b"PK\x03\x04", b"dex\n", b"<!DOCTYPE", b"<?xml")
REAL_ASSET_BYTES = 64 * 1024

# The decoy layer: what the build is expected to have filled assets/ with, and the ceiling on it.
# The directory floor is what makes it a maze rather than a listing: 1610 files in one flat directory
# would be noise a reader scrolls past, 1610 files under 1200 nested paths is a tree to walk.
MIN_DECOY_ENTRIES = 1500
MIN_DECOY_DIRS = 800
MAX_DECOY_BYTES = 1024 * 1024

# Below this share of text bytes an entry is opaque to a reader. See readable_text_bytes: this is a
# rate, because the absolute chance-run count grows with the size of the encrypted blob.
OPAQUE_SHARE = 0.01

TEXT_RUN = re.compile(rb"[\x20-\x7e\t\n\r]{8,}")


def readable_text_bytes(blob):
    """Bytes that sit inside a run of eight or more printable characters - what `strings` shows.

    Counting printable bytes alone says nothing: ciphertext is about 37% printable byte-wise, and
    so is a lot of an ELF. Runs of text are what a reader can actually use, and they are what
    encrypted data has almost none of (a six-character run happens by chance every few hundred
    bytes; an eight-character one essentially never). Regex matching keeps this at C speed over a
    60 MB payload.
    """
    return sum(len(run) for run in TEXT_RUN.findall(blob))


def uleb128(data, offset):
    result = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, offset
        shift += 7


def parse_dex(data):
    """Just enough of the dex format for a name and debug-info audit.

    Layout used here (all little-endian): the header holds the sizes/offsets, a class_def_item is
    32 bytes with the source_file_idx at +16 and class_data_off at +24, a class_data_item starts
    with four uleb128 counts, and each encoded_method ends with the code_off whose code_item has
    debug_info_off at +8.
    """
    if data[:4] != b"dex\n":
        raise ValueError("not a dex file")
    if struct.unpack_from("<I", data, 40)[0] != ENDIAN_CONSTANT:
        raise ValueError("reverse-endian dex is not supported")
    string_ids_size, string_ids_off = struct.unpack_from("<II", data, 56)
    type_ids_off = struct.unpack_from("<I", data, 68)[0]
    field_ids_size = struct.unpack_from("<I", data, 80)[0]
    method_ids_size = struct.unpack_from("<I", data, 88)[0]
    class_defs_size, class_defs_off = struct.unpack_from("<II", data, 96)

    def string(index):
        offset = struct.unpack_from("<I", data, string_ids_off + index * 4)[0]
        _utf16_size, pos = uleb128(data, offset)
        return data[pos:data.index(b"\x00", pos)].decode("utf-8", "replace")

    def descriptor(index):
        return string(struct.unpack_from("<I", data, type_ids_off + index * 4)[0])

    def method_counts(class_data_off):
        pos = class_data_off
        static_fields, pos = uleb128(data, pos)
        instance_fields, pos = uleb128(data, pos)
        direct_methods, pos = uleb128(data, pos)
        virtual_methods, pos = uleb128(data, pos)
        for _ in range(static_fields + instance_fields):
            _field_idx, pos = uleb128(data, pos)
            _access, pos = uleb128(data, pos)
        methods = with_debug = 0
        for _ in range(direct_methods + virtual_methods):
            _method_idx, pos = uleb128(data, pos)
            _access, pos = uleb128(data, pos)
            code_off, pos = uleb128(data, pos)
            if code_off == 0:
                continue                      # abstract or native: no code item
            methods += 1
            if struct.unpack_from("<I", data, code_off + 8)[0] != 0:
                with_debug += 1
        return methods, with_debug

    classes = []
    for i in range(class_defs_size):
        base = class_defs_off + i * 32
        class_idx, source_idx, data_off = (
            struct.unpack_from("<I", data, base)[0],
            struct.unpack_from("<I", data, base + 16)[0],
            struct.unpack_from("<I", data, base + 24)[0])
        methods, with_debug = method_counts(data_off) if data_off else (0, 0)
        classes.append((descriptor(class_idx),
                        None if source_idx == NO_INDEX else string(source_idx),
                        methods, with_debug))
    return {"classes": classes, "strings": [string(i) for i in range(string_ids_size)],
            "fields": field_ids_size, "methods": method_ids_size}


def audit(path):
    apk = zipfile.ZipFile(path)
    names = apk.namelist()
    dex_names = sorted(n for n in names if re.match(r"classes\d*\.dex$", n))
    problems = []

    forbidden = [n for n in names if FORBIDDEN_ENTRY.search(n)]
    if forbidden:
        problems.append("forbidden entries: " + ", ".join(forbidden))

    classes = methods = with_debug = with_source = 0
    fields = method_ids = 0
    named = []
    kept = []
    residue = set()
    for name in dex_names:
        info = parse_dex(apk.read(name))
        fields += info["fields"]
        method_ids += info["methods"]
        for descriptor, source, method_count, debug_count in info["classes"]:
            classes += 1
            methods += method_count
            with_debug += debug_count
            with_source += 1 if source is not None else 0
            simple = descriptor[1:-1] if descriptor.startswith("L") else descriptor
            if EXPECTED_KEEPERS.match(descriptor):
                kept.append(descriptor)
            elif "/" in simple:
                named.append(descriptor)
        for value in info["strings"]:
            if "zettabridge" in value.lower():
                residue.add(value if len(value) < 90 else value[:87] + "...")

    print(f"APK            : {path}")
    print(f"entries        : {len(names)}   dex: {', '.join(dex_names)}")
    print(f"classes        : {classes} classes, {fields} fields, {methods} methods with code, "
          f"{method_ids} method ids")
    print(f"SourceFile     : {with_source} of {classes} classes name a source file")
    print(f"line numbers   : {with_debug} of {methods} methods carry debug info")
    # Both must be zero, and this is a gate rather than a reported number because R8 cannot produce
    # that: it keeps source files and line numbers whatever the keep rules ask for. Zero means the
    # dex post-pass (build/strip_dex_debug.py) ran on the packaged APK; anything else means it was
    # skipped or a later edit undid it, which is exactly the regression worth failing the build on.
    if with_source:
        problems.append(f"{with_source} classes still name a source file: the dex post-pass did not run")
    if with_debug:
        problems.append(f"{with_debug} methods still carry line numbers: the dex post-pass did not run")
    print(f"meaningful name: {len(named)} classes outside the keep list, "
          f"{len(set(kept))} kept on purpose")

    symbols = banners = 0
    native = []
    for entry in names:
        if not (entry.startswith("lib/") or entry.startswith("assets/zb/")):
            continue
        if not (entry.endswith(".so") or entry.endswith("zbhost")):
            continue
        native.append(entry)
        blob = apk.read(entry)
        if b".symtab\x00" in blob:
            symbols += 1
            problems.append("symbol table shipped: " + entry)
        if b".comment\x00" in blob and b"clang version" in blob:
            banners += 1
            problems.append("compiler banner shipped: " + entry)
        if b".debug_info\x00" in blob or b".debug_str\x00" in blob:
            problems.append("DWARF shipped: " + entry)
    print(f"native objects : {len(native)}   symbol tables: {symbols}   clang banners: {banners}")

    # What is left readable, and what is not: the point of the payload containers is that the bulk of
    # the archive is neither a known file type nor text.
    #
    # The share that is *enforced* is the one under assets/, not the one over the whole archive.
    # Assets are what a container exists to cover: the bundled client used to sit there as a plain
    # APK, and a reader's first move is still "unzip and look at the assets". classes.dex and
    # resources.arsc are readable in any Android app and no container can hide them, so a build that
    # carries no bundled client (BUNDLED_APP=none, ~5 MB of mostly dex) would fail a whole-archive
    # share test while hiding nothing but the app's own code. The whole-archive figure is still
    # printed, so a build that suddenly became mostly readable is still visible in the output.
    readable_bytes = opaque_bytes = 0
    asset_bytes = asset_opaque_bytes = 0
    decoy_entries = decoy_bytes = 0
    decoy_names = []
    payload_names = []
    readable_entries = []
    for entry in names:
        blob = apk.read(entry)
        text = readable_text_bytes(blob)
        share = text / len(blob) if blob else 0.0
        if entry.startswith("assets/") and len(blob) >= REAL_ASSET_BYTES:
            payload_names.append(entry)
            for magic in PLAINTEXT_MAGIC:
                if magic in blob:
                    problems.append(f"plaintext {magic!r} inside the large asset {entry}")
            # A container that is really encrypted has almost no text. The bar is a share, not an
            # absolute count: 60 MB of ciphertext contains ~150 kB of eight-character runs by pure
            # chance, while any readable format sits above 1% (the dex here: 22%, the bridge: 6%).
            if share > OPAQUE_SHARE:
                problems.append(
                    f"large asset {entry} is not opaque: {100*share:.2f}% of it is readable text")
            opaque_bytes += len(blob)
            asset_bytes += len(blob)
            asset_opaque_bytes += len(blob)  # the opacity of this one was just asserted
            continue
        if entry.startswith("assets/") and not entry.endswith("/"):
            decoy_entries += 1
            decoy_bytes += len(blob)
            decoy_names.append(entry)
        if LEAKY_ENTRY_NAME.search(entry):
            problems.append("entry name gives the contents away: " + entry)
        readable_entries.append((entry, len(blob), text))
        if share >= OPAQUE_SHARE:
            readable_bytes += len(blob)
        else:
            opaque_bytes += len(blob)
            if entry.startswith("assets/") and not entry.endswith("/"):
                asset_opaque_bytes += len(blob)
        if entry.startswith("assets/") and not entry.endswith("/"):
            asset_bytes += len(blob)
    total = readable_bytes + opaque_bytes
    shown = ", ".join(payload_names[:3]) + (f" (+{len(payload_names) - 3} more)"
                                            if len(payload_names) > 3 else "")
    print(f"payloads       : {len(payload_names)} encrypted containers -> {shown}")
    print(f"readable       : {readable_bytes:,} bytes of {total:,} ({100*readable_bytes/total:.1f}%)"
          f":")
    for entry, size, text in sorted(readable_entries, key=lambda row: -row[1])[:6]:
        print(f"                 {100*text/size:>6.1f}% text  {size:>10,}  {entry}")
    print(f"opaque         : {opaque_bytes:,} bytes ({100*opaque_bytes/total:.1f}%) of the whole archive"
          f" cannot be read without this app's key; the rest is the dex and the resources")
    if asset_bytes:
        print(f"assets         : {asset_opaque_bytes:,} bytes of {asset_bytes:,} "
              f"({100*asset_opaque_bytes/asset_bytes:.1f}%) cannot be read without this app's key")
    if not payload_names:
        problems.append("no payload containers: the runtime and the bundled client would ship as assets")
    if not asset_bytes:
        problems.append("the archive has no assets at all: nothing is hidden where a reader looks")
    elif asset_opaque_bytes < asset_bytes * 0.5:
        problems.append(f"only {100*asset_opaque_bytes/asset_bytes:.1f}% of the assets are opaque"
                        " - a payload container that is not covering the assets is not doing its job")

    # aapt2 records only files, so an empty directory is not an archive entry at all; what a reader
    # gets after unzip is the tree the entry paths imply - every ancestor of every path, not just the
    # immediate parent - and these two counts are that tree.
    def directories(paths):
        found = set()
        for path in paths:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                found.add("/".join(parts[:depth]))
        return found

    decoy_dirs = directories(decoy_names)
    unpacked_dirs = directories(name for name in names if not name.endswith("/"))
    print(f"decoy layer    : {decoy_entries} files in {len(decoy_dirs)} nested directories "
          f"({len(unpacked_dirs)} directories in the unpacked APK), {decoy_bytes:,} bytes")
    if decoy_entries < MIN_DECOY_ENTRIES:
        problems.append(f"only {decoy_entries} decoy entries, expected at least {MIN_DECOY_ENTRIES}")
    if len(decoy_dirs) < MIN_DECOY_DIRS:
        problems.append(f"the decoy layer sits in only {len(decoy_dirs)} directories, expected at least "
                        f"{MIN_DECOY_DIRS}")
    if decoy_bytes > MAX_DECOY_BYTES:
        problems.append(f"the decoy layer costs {decoy_bytes:,} bytes, over the {MAX_DECOY_BYTES:,} budget")

    print(f"name residues  : {len(residue)}")
    for value in sorted(residue):
        print(f"                 {value!r}")

    print()
    if problems:
        print("AUDIT FAILED")
        for problem in sorted(set(problems)):
            print("  - " + problem)
        return 1
    print("AUDIT CLEAN: no mapping/seeds, no symbol tables, no DWARF, no compiler banner")
    return 0


if __name__ == "__main__":
    sys.exit(audit(sys.argv[1] if len(sys.argv) > 1
                   else "project2/pokemon-go-emulated-obfuscated.apk"))
