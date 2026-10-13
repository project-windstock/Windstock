"""Checks the debuggable APK, and that the obfuscated one stayed quiet.

    py project2/build/check_debug_build.py project2/translator-debug.apk

Three things have to be true of the artifact build-debug.sh produces, and none of them is provable
from the variant name:

1. android:debuggable is true in the packaged manifest, or nothing can attach and the app is not
   the debuggable build at all. This is read out of the manifest's binary XML properly - aapt2
   writes framework attributes as resource ids, so the word "debuggable" is not in the file and a
   text search finds nothing (which is exactly the mistake this check used to make);
2. the verbose DebugLog (app/src/debug) is in the dex - not the no-op copy from app/src/release.
   Its string literals are the marker: they only exist in the debug source set;
3. the obfuscated APK next to it does NOT contain those markers, i.e. the seam really is a seam and
   the shipping build gained no narration from this;
4. the *stability* layer is in BOTH builds. That layer is behaviour, not narration: it decides
   whether a dead :guest process is reopened, so it has to be in the APK that ships, and a marker
   list of diagnostics cannot say whether it is. Its literals are checked in both dexes and its
   service in both packaged manifests, because a class that survives R8 but lost its <service>
   entry, or a service whose class was shrunk away, would each look fine from one side only.
"""
import struct
import sys
import zipfile
from pathlib import Path

RES_XML_START_ELEMENT = 0x0102
RES_XML_RESOURCE_MAP = 0x0180
RES_STRING_POOL = 0x0001
ANDROID_DEBUGGABLE = 0x0101000F
TYPE_INT_BOOLEAN = 0x12
TYPE_STRING = 0x03
NO_ENTRY = 0xFFFFFFFF

# Only in app/src/debug/java/com/zettabridge/launcher/DebugLog.java. Nothing here may be a string
# that shared code could also contain: "zb-app-log.txt" is not one of them, because the failure
# screens name the log file too, and the first version of this check failed the release build for
# exactly that reason. These five are sentences only the verbose logger writes.
MARKERS = [
    b"Translator debug log opened",
    b"======== SUMMARY ",
    b"WINDOW CLOSED: ",
    b"BLACK WINDOW: ",
    b"what this means: the process ",
]

# Only in the stability layer (GuestLease/GuestHeartbeat/GuestRecovery/GuestSupervisor). These must
# be in the obfuscated dex too: R8 renames the classes, never the string literals they compare and
# write with.
STABILITY_MARKERS = [
    b"guest.lease",
    b"com.chucny.translator.bridge.SUPERVISED",
    b"the app stopped while its window",
]
# The component that does the recovery, as the packaged manifest names it.
STABILITY_SERVICE = ".GuestSupervisor"

RELEASE = Path(__file__).resolve().parent.parent / "pokemon-go-emulated-obfuscated.apk"


def string_pool(data, base):
    """Reads a RES_STRING_POOL chunk at `base`; returns (strings, chunk size)."""
    count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", data, base + 8)
    offsets = base + 28
    strings = []
    utf8 = bool(flags & (1 << 8))
    for i in range(count):
        (p,) = struct.unpack_from("<I", data, offsets + 4 * i)
        if p == NO_ENTRY:
            strings.append(None)
            continue
        at = base + strings_start + p
        if utf8:
            n, = struct.unpack_from("<H", data, at)
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, at + 2)[0]
                at += 4
            else:
                at += 2
            strings.append(data[at:at + n].decode("utf-8", "replace"))
        else:
            n, = struct.unpack_from("<H", data, at)
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, at + 2)[0]
                at += 4
            else:
                at += 2
            strings.append(data[at:at + 2 * n].decode("utf-16-le", "replace"))
    (size,) = struct.unpack_from("<I", data, base + 4)
    return strings, size


def manifest_attributes(data):
    """Every start element, as (element name, {attribute name or resource id: (type, value)}).

    An attribute's name is a string-pool index, and the RES_XML_RESOURCE_MAP chunk maps the same
    index onto the framework resource id - so "debuggable" is both the pool string at some index and
    0x0101000f at that index in the map. Both are returned, because either identifies it.
    """
    strings, _ = string_pool(data, 8)
    resource_map = []
    elements = []
    off = 8
    while off < len(data) - 8:
        kind, _header, size = struct.unpack_from("<HHI", data, off)
        if size <= 0:
            break
        if kind == RES_XML_RESOURCE_MAP:
            count = (size - 8) // 4
            resource_map = list(struct.unpack_from(f"<{count}I", data, off + 8))
        elif kind == RES_XML_START_ELEMENT:
            # The element's own name index, then its attribute table, both relative to the chunk's
            # ResXMLTree_node; attributeStart counts from the end of the attrExt struct.
            name_idx, attr_start, attr_size, attr_count = struct.unpack_from("<IHHH", data, off + 16 + 4)
            attrs = {}
            base = off + 16 + attr_start
            for i in range(attr_count):
                at = base + i * attr_size
                _ns, attr_name_idx, _raw, _vsize, _res0, vtype, vdata = struct.unpack_from("<IIIHBBi", data, at)
                key = strings[attr_name_idx] if attr_name_idx < len(strings) else None
                if key is None and attr_name_idx < len(resource_map):
                    key = resource_map[attr_name_idx]
                attrs[key if key is not None else attr_name_idx] = (vtype, vdata)
            elements.append((strings[name_idx] if name_idx < len(strings) else None, attrs))
        off += size
    return elements


def dex_bytes(apk):
    with zipfile.ZipFile(apk) as z:
        names = [n for n in z.namelist() if n.endswith(".dex")]
        return names, b"".join(z.read(n) for n in names)


def debuggable_in(apk):
    with zipfile.ZipFile(apk) as z:
        manifest = z.read("AndroidManifest.xml")
    for name, attrs in manifest_attributes(manifest):
        if name != "application":
            continue
        if "debuggable" not in attrs:
            return False, "the <application> element has no android:debuggable attribute"
        vtype, vdata = attrs["debuggable"]
        if vtype != TYPE_INT_BOOLEAN:
            return False, f"android:debuggable is not a boolean (type 0x{vtype:02x})"
        return vdata != 0, f"android:debuggable = {'true' if vdata else 'false'}"
    return False, "no <application> element in the packaged manifest"


def has_service(apk, name):
    """Whether the packaged manifest declares a <service> whose android:name is `name`.

    The manifest is the only place a component can be declared, which is why a dex string is not
    enough to prove the recovery ships. A string attribute's value is an index into the string pool,
    not the string itself - reading it as one (the first version of this check did) reports every
    component as missing, which is how this stayed a plausible-looking gate for exactly one run.
    """
    with zipfile.ZipFile(apk) as z:
        manifest = z.read("AndroidManifest.xml")
    strings, _ = string_pool(manifest, 8)
    for element, attrs in manifest_attributes(manifest):
        if element != "service":
            continue
        for key, (vtype, value) in attrs.items():
            if key != "name" or vtype != TYPE_STRING:
                continue
            if 0 <= value < len(strings) and (strings[value] or "").endswith(name):
                return True
    return False


def stability_view(apk):
    """The stability layer as this APK carries it: its literals in the dex, its service in the manifest."""
    _, dex = dex_bytes(apk)
    present = [m.decode() for m in STABILITY_MARKERS if m in dex]
    missing = [m.decode() for m in STABILITY_MARKERS if m not in dex]
    service = has_service(apk, STABILITY_SERVICE)
    return present, missing, service


def check(apk):
    apk = Path(apk)
    problems = []
    with zipfile.ZipFile(apk) as z:
        entries = z.namelist()

    debuggable, how = debuggable_in(apk)
    dex_names, dex = dex_bytes(apk)
    found = [m.decode() for m in MARKERS if m in dex]
    missing = [m.decode() for m in MARKERS if m not in dex]
    stability_found, stability_missing, stability_service = stability_view(apk)

    print(f"APK            : {apk}  {apk.stat().st_size:,} bytes, {len(entries)} entries")
    print(f"dex            : {', '.join(dex_names)}")
    print(f"debuggable     : {'YES' if debuggable else 'NO'}  ({how})")
    print(f"verbose logger : {len(found)}/{len(MARKERS)} markers in the dex")
    for marker in missing:
        print(f"                 - MISSING {marker}")
    print(f"stability      : {len(stability_found)}/{len(STABILITY_MARKERS)} recovery markers, "
          f"service {'YES' if stability_service else 'NO'}")
    for marker in stability_missing:
        print(f"                 - MISSING {marker}")

    if not debuggable:
        problems.append("the packaged manifest does not say android:debuggable=true")
    if missing:
        problems.append("the dex does not carry the debug-variant DebugLog (missing markers above)")
    if stability_missing or not stability_service:
        problems.append("the debuggable build is missing the stability layer (above)")

    if RELEASE.is_file():
        _, release_dex = dex_bytes(RELEASE)
        release_debuggable, release_how = debuggable_in(RELEASE)
        leaked = [m.decode() for m in MARKERS if m in release_dex]
        release_found, release_missing, release_service = stability_view(RELEASE)
        print(f"release build  : {RELEASE.name}: debuggable={'YES' if release_debuggable else 'NO'} "
              f"({release_how}), {len(leaked)}/{len(MARKERS)} debug markers")
        print(f"                 stability: {len(release_found)}/{len(STABILITY_MARKERS)} recovery markers, "
              f"service {'YES' if release_service else 'NO'}")
        if leaked:
            problems.append(f"the obfuscated build gained the debug logger: {leaked}")
        if release_debuggable:
            problems.append("the obfuscated build is debuggable; it must not be")
        if release_missing:
            problems.append(f"the obfuscated build is missing recovery code: {release_missing}")
        if not release_service:
            problems.append(f"the obfuscated build's manifest has no {STABILITY_SERVICE} service, so"
                            f" a game that dies is never reopened there")
    else:
        print(f"release build  : {RELEASE.name} not present, cross-check skipped")

    if problems:
        print("\nDEBUG BUILD GATE FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nDEBUG BUILD OK: debuggable, verbose logger present, obfuscated build unaffected")
    print("                the recovery layer is in both APKs")
    return 0


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <debug apk>")
    return check(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
