"""
unitypy_fix.py -- the ONLY sanctioned way to import UnityPy in this project.

    import sys, os
    sys.path.insert(0, os.path.join(<repo>, "src", "tools"))
    from unitypy_fix import UnityPy, load, save_bundle, clone, tmp_path

Never `import UnityPy` directly. Plain UnityPy corrupts Unity 5.3 files on save,
in two separate ways. Both are reproduced below, both are fixed here, and both
guards are asserted by `selftest()`.


WHY (the two traps, verbatim from README-unity-modding-android.md section 6)
----------------------------------------------------------------------------

1. SCRIPT INDICES.
   "In Unity 5.3 files every object record carries its own `script_type_index`
   (which MonoScript a MonoBehaviour runs). UnityPy stores it on a type record
   that many objects share, so a plain save rebinds MonoBehaviours to the wrong
   scripts. Symptom: splash screen, then grey, and the game never talks to the
   server."

   The bug is in UnityPy itself, in `ObjectReader.from_reader`:

       if 11 <= header.version < 17:
           script_type_index = reader.read_short()
           if serialized_type:
               serialized_type.script_type_index = script_type_index   # <-- clobbers

   `serialized_type` is an entry in the FILE-WIDE type table that every object of
   the same class shares. So the last object read wins, and `save()` then writes
   that one value for all of them -- every MonoBehaviour in the bundle silently
   rebinds to whichever script happened to be read last.

   THE FIX. Two halves, and both are needed.

   (a) On read, `from_reader` is patched to stop clobbering the shared record and
       instead remember the value per object, keyed by path_id, on the
       SerializedFile. Nothing else about the read changes: the type table is left
       exactly as UnityPy built it.

   (b) On write, `ObjectReader.write` is patched. For a format 11..16 file it
       lends the shared SerializedType record that object's own index for the
       duration of that one object's write and puts the old value back
       immediately afterwards. The two bytes come out as they went in.

   Why not rebuild the type table instead -- one SerializedType per distinct
   (class_id, script_type_index), each object repointed at its own? It was tried,
   and it is wrong: `ObjectReader.type_id` is a CLASS index (5 = Mesh, 21 =
   Material, 114 = MonoBehaviour), not a row in the type table, so a table rebuilt
   that way collapses to two rows and leaves every other object with no
   serialized_type at all -- 259 of 261 objects unreadable in pm0025. The table
   Unity writes is genuinely shared between objects; only the two bytes in each
   object record are per-object, so only those need restoring.

   `selftest()` measures this the only way that counts: a no-op round trip whose
   re-serialized metadata must be BYTE-IDENTICAL. Run with the guard removed it
   fails on exactly one 2-byte short per MonoBehaviour -- 10 of them in pm0025 --
   which is the corruption, reproduced on demand.

2. STREAMING.
   "UnityPy reads object data from the source file until `save()` returns, so
   `open(path, "wb").write(sf.save())` truncates the file first and writes a
   broken, much smaller one. The tools write to `*.tmp` and rename."

   THE FIX. `save_bundle()` never opens the source for writing. It builds the
   bytes with the source still intact, writes them to a temp file whose name
   carries this process's PID, fsyncs, and only then `os.replace()`s it into
   position. A crash mid-write therefore leaves the previous good file in place,
   and two concurrent builds on one machine cannot collide on the temp name
   (README section 2: "Never run two builds at the same time on the same machine
   without the unique temp files -- an old version collided and could put iOS
   textures in an Android bundle").

   `save_bundle()` also refuses to touch a source it cannot still read, and
   `open()` is never used in write mode on a bundle.


WHAT ELSE THIS MODULE DOES
--------------------------

* `selftest()` -- round-trips a bundle with no edits at all and asserts the
  re-serialized file metadata comes back BYTE-IDENTICAL, which is the only check
  that can see a clobbered script index. Run it after touching anything here:
      py src/tools/unitypy_fix.py src/server/assets/pm0025
* `clone()` -- copies an object into another file. A clone inherits its
  template's script index, so an injected object can never arrive with an
  unrelated MonoScript bound to it.
* `close_env()` -- releases UnityPy's file handles. Windows will not let
  `os.replace()` rename onto a file that is still open, so this is what makes
  save_bundle() work there at all.
* `compress_formats()` -- the texture formats this client ships, and which of
  them we can re-encode. UnityPy DECODES all of them; it can only RE-ENCODE the
  uncompressed ones, which is why every write here sets RGBA32 (format 4) the way
  `make_shiny_bundle.py` does.
* `open_bundle(env)` / `bundle_of(env)` -- the single BundleFile inside an
  environment, so callers stop guessing at `list(env.files.values())[0]`.
* `serialized_files(env)` / `serialized_bytes(env)` -- the SerializedFiles
  inside an environment, and the exact bytes a save would write for each.

Requires: UnityPy==1.25.2, Pillow, pycryptodome, numpy, crcmod, lz4
    py -m pip install --user UnityPy==1.25.2 Pillow pycryptodome numpy crcmod lz4
"""
import copy
import os
import sys
import tempfile

__all__ = ["UnityPy", "load", "save_bundle", "save_serialized", "clone",
           "tmp_path", "open_bundle", "bundle_of", "selftest", "have_unitypy",
           "RGBA32", "texture_formats", "missing_reason", "mesh_handler",
           "close_env", "serialized_files", "serialized_bytes",
           "UNREENCODABLE", "format_name"]


# --------------------------------------------------------------------------
# Lazy, failure-tolerant import. The GAME SERVER must keep working on a machine
# with none of this installed -- only the asset editor and the build tools need
# it -- so a missing UnityPy is a clean "not available", never an ImportError at
# server start.
# --------------------------------------------------------------------------
UnityPy = None
_IMPORT_ERROR = None


def _import_unitypy():
    global UnityPy, _IMPORT_ERROR
    if UnityPy is not None or _IMPORT_ERROR is not None:
        return UnityPy
    try:
        import UnityPy as _u           # noqa: PLC0415  (deliberately late)
    except Exception as e:              # pragma: no cover - depends on the host
        _IMPORT_ERROR = e
        return None
    UnityPy = _patch(_u)
    return UnityPy


def have_unitypy():
    """True when UnityPy is importable and patched. Probe this before offering
    the user any editing feature."""
    return _import_unitypy() is not None


def missing_reason():
    """Why UnityPy is unavailable, as a sentence for the UI. None when it is."""
    if _import_unitypy() is not None:
        return None
    return ("UnityPy could not be imported: %s. Install the asset-editing "
            "extras with:  py -m pip install --user UnityPy==1.25.2 Pillow "
            "pycryptodome numpy crcmod lz4" % (_IMPORT_ERROR,))


# --------------------------------------------------------------------------
# GUARD 1 -- per-object script_type_index
# --------------------------------------------------------------------------
# ObjectReader is an attrs class with slots=True, so a per-object value cannot be
# added to the instance. SerializedFile is a plain class with a __dict__, so the
# side table hangs off the file. Keyed by path_id, which is unique per file.
_SIDETABLE = "_windstock_script_type_index"


def _patch(mod):
    """Install the script-index guard into an imported UnityPy module."""
    # `UnityPy.files` re-exports the CLASSES, not the modules.
    from UnityPy.files import ObjectReader as _OR
    from UnityPy.files import SerializedFile as _SF

    if getattr(_OR, "_windstock_patched", False):
        return mod

    @classmethod
    def from_reader(cls, assets_file, reader):
        """UnityPy's own reader, with the per-object script_type_index KEPT.

        Everything below is a verbatim copy of UnityPy 1.25.2's
        ObjectReader.from_reader; the only change is the four lines that stash
        the value instead of letting it overwrite the shared type record. If a
        future UnityPy changes this function, `selftest()` fails loudly rather
        than corrupting a bundle.
        """
        header = assets_file.header
        types = assets_file.types

        if assets_file.big_id_enabled:
            path_id = reader.read_long()
        elif header.version < 14:
            path_id = reader.read_int()
        else:
            reader.align_stream()
            path_id = reader.read_long()

        if header.version >= 22:
            byte_start = reader.read_long()
        else:
            byte_start = reader.read_u_int()

        byte_start += header.data_offset
        byte_size = reader.read_u_int()

        type_id = reader.read_int()

        serialized_type = None
        if header.version < 16:
            class_id = reader.read_u_short()
            for typ in types:
                if typ.class_id == type_id:
                    serialized_type = typ
                    break
        else:
            typ = types[type_id]
            serialized_type = typ
            class_id = typ.class_id

        from UnityPy.enums import ClassIDType
        clz_type = ClassIDType(class_id)

        is_destroyed = None
        if header.version < 11:
            is_destroyed = reader.read_u_short()

        script_type_index = None
        if 11 <= header.version < 17:
            # >>> THE FIX, part 1 of 2. UnityPy 1.25.2 does this next:
            #
            #     if serialized_type:
            #         serialized_type.script_type_index = script_type_index
            #
            # ...which is the corruption. `serialized_type` is an entry in the
            # FILE-WIDE type table that every object of the class shares, and in
            # a format 11..16 file UnityPy never even READS that field off the
            # record (it is only serialised for version >= 17), so it stays at
            # its class default of -1. The value that matters -- the one Unity
            # wrote, and the one ObjectReader.write() emits -- is this
            # per-object short. We read it, keep it, and touch nothing else.
            # Part 2 is the `write` patch below.
            script_type_index = reader.read_short()

        is_stripped = None
        if header.version == 15 or header.version == 16:
            is_stripped = reader.read_byte()

        obj = cls(assets_file, reader, path_id, type_id, serialized_type,
                  class_id, clz_type, byte_start, byte_size, is_destroyed,
                  is_stripped)
        if script_type_index is not None:
            _remember(assets_file, path_id, script_type_index)
        return obj

    _OR.from_reader = from_reader

    _orig_write = _OR.write

    def write(self, header, meta_writer, data_writer):
        """UnityPy's own writer, except that each object emits ITS OWN
        script_type_index.

        Stock UnityPy 1.25.2 writes `self.serialized_type.script_type_index`,
        which for a 5.3 file is permanently -1 (see above) -- so a plain save
        rewrites every MonoBehaviour in the bundle as script-less. Unity then
        cannot resolve the class, the scene comes up empty, and the game never
        reaches the server: "splash screen, then grey". The object-record layout
        is untouched; only the two bytes of an index that was already there
        change, back to what they were.
        """
        if 11 <= header.version < 17:
            own = _recall(self.assets_file, self.path_id)
            st = self.serialized_type
            if own is not None and st is not None:
                previous = st.script_type_index
                st.script_type_index = own
                try:
                    return _orig_write(self, header, meta_writer, data_writer)
                finally:
                    st.script_type_index = previous
        return _orig_write(self, header, meta_writer, data_writer)

    _OR.write = write
    _OR._windstock_patched = True
    return mod


def _remember(assets_file, path_id, value):
    """Stash one object's own script_type_index on its file."""
    table = getattr(assets_file, _SIDETABLE, None)
    if table is None:
        table = {}
        try:
            setattr(assets_file, _SIDETABLE, table)
        except AttributeError:              # pragma: no cover - slotted file
            return
    table[path_id] = value


def _recall(assets_file, path_id):
    table = getattr(assets_file, _SIDETABLE, None)
    return None if not table else table.get(path_id)


# --------------------------------------------------------------------------
# GUARD 2 -- streaming: never truncate the source, always tmp + rename
# --------------------------------------------------------------------------
def close_env(env):
    """Release every file handle UnityPy opened, then collect.

    Windows refuses `os.replace()` onto a file that is still open, so this is
    what makes the tmp-and-rename guard work there at all; macOS and Linux
    tolerate it but leak descriptors on a 151-bundle run. UnityPy closes these
    in `__del__`, which is not something a save path should rely on -- a
    reference held by an exception traceback keeps the handle alive long after
    the env itself went out of scope. Callers should drop their own reference to
    the env as well; this cannot do that for them.
    """
    import gc
    seen = set()

    def walk(f, depth=0):
        if depth > 4 or id(f) in seen:
            return
        seen.add(id(f))
        stream = getattr(getattr(f, "reader", None), "stream", None)
        if stream is not None and hasattr(stream, "close"):
            try:
                stream.close()
            except Exception:
                pass
        for sub in (getattr(f, "files", None) or {}).values():
            walk(sub, depth + 1)

    for f in getattr(env, "files", {}).values():
        walk(f)
    gc.collect()


def serialized_files(env):
    """Every SerializedFile inside an environment.

    Matching on the class name rather than on `hasattr(f, "objects")` matters:
    a SerializedFile's `.files` attribute is its OBJECT dict (path_id ->
    ObjectReader), and those objects also answer `hasattr(f, "objects")`, so the
    obvious duck-typed check walks straight into 261 ObjectReaders and tries to
    call `save()` on each.
    """
    out, seen = [], set()

    def walk(f, depth=0):
        if depth > 4 or id(f) in seen:
            return
        seen.add(id(f))
        if type(f).__name__ == "SerializedFile":
            out.append(f)
            return                       # do not descend into the object dict
        for sub in (getattr(f, "files", None) or {}).values():
            walk(sub, depth + 1)

    for f in getattr(env, "files", {}).values():
        walk(f)
    return out


def serialized_bytes(env):
    """{node path: the exact bytes UnityPy would write for that file}.

    This is the honest measure of "did the save change anything": the object
    record metadata (including every script_type_index) lives in here, so two
    environments that produce identical bytes are indistinguishable to Unity,
    whatever their Python objects look like.
    """
    out = {}
    for sf in serialized_files(env):
        out[getattr(sf, "name", "?")] = sf.save()
    return out


def tmp_path(dest, tag="save"):
    """A temp path beside `dest` that is unique to this process AND this call.

    README section 2: two builds running at once on one machine used to collide
    on the temp file, and an iOS texture ended up in an Android bundle. The PID
    keeps concurrent processes apart; the pid+counter tail keeps repeated calls
    inside one process apart too.
    """
    d = os.path.dirname(os.path.abspath(dest)) or "."
    base = os.path.basename(dest)
    return os.path.join(d, ".%s.%s.%d.%d.tmp" % (base, tag, os.getpid(),
                                                  _next_seq()))


_seq = 0


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _write_atomic(dest, data, tag="save"):
    """Write `data` to `dest` the only safe way: temp file, fsync, rename."""
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = tmp_path(dest, tag)
    try:
        with open(tmp, "wb") as fh:          # the SOURCE is never opened "wb"
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)                # atomic on Windows, macOS and Linux
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return dest


def save_bundle(env, dest, packer="lz4", source=None):
    """Serialize an environment's bundle to `dest` without corrupting anything.

    env    -- a UnityPy Environment (or a BundleFile) loaded from `source`
    dest   -- where the packed bytes go. NEVER the file the env was loaded from.
    packer -- "lz4" (what the genuine 2016 bundles use), "original", or None.

    The bytes are fully built in memory while the source is still readable, so
    the streaming trap cannot fire; only then does anything touch the disk.
    """
    bundle = env if _is_bundle(env) else bundle_of(env)
    if bundle is None:
        raise ValueError("no UnityFS bundle in that environment")
    src = source if source is not None else _source_of(env)
    if src and os.path.abspath(src) == os.path.abspath(dest):
        raise ValueError(
            "refusing to save a bundle over the file it was loaded from -- that "
            "is the truncation trap: write to a temp name and rename it")
    packed = bundle.save(packer=packer)     # source still intact here
    return _write_atomic(dest, packed, tag="bundle")


def save_serialized(sf, dest):
    """A bare SerializedFile (no UnityFS wrapper) written the same safe way."""
    data = sf.save()
    return _write_atomic(dest, data, tag="serialized")


def _is_bundle(obj):
    try:
        from UnityPy.files.BundleFile import BundleFile
    except Exception:                       # pragma: no cover
        return False
    return isinstance(obj, BundleFile)


def _source_of(env):
    for f in getattr(env, "files", {}).values():
        p = getattr(f, "name", None)
        if p and os.path.isfile(p):
            return p
    return None


def bundle_of(env):
    """The single BundleFile inside an environment, or None."""
    for f in getattr(env, "files", {}).values():
        if _is_bundle(f):
            return f
    return None


def open_bundle(env):
    """Alias kept for readability at call sites that mean 'the packed bytes'."""
    return bundle_of(env)


def load(path):
    """UnityPy.load, through the guards. The only supported way in."""
    mod = _import_unitypy()
    if mod is None:
        raise ImportError(missing_reason())
    return mod.load(path)


# --------------------------------------------------------------------------
# Cloning
# --------------------------------------------------------------------------
def clone(src_obj, target_file, path_id=None, class_id=None, new_name=None):
    """Copy an object into another SerializedFile, template's script index and all.

    README section 6: "clones inherit their template's [script_type_index]".
    Without that, an injected MonoBehaviour arrives bound to whatever script
    happened to sit at the same type index in the destination, which is the same
    grey-screen bug by another route.
    """
    c = copy.copy(src_obj)
    c.assets_file = target_file
    c.reader = getattr(target_file, "reader", None)
    c.path_id = path_id if path_id is not None else src_obj.path_id
    if class_id is not None:
        c.class_id = class_id
        try:
            from UnityPy.enums import ClassIDType
            c.type = ClassIDType(class_id)
        except Exception:
            pass
    if new_name is not None:
        c.assets_file = target_file
    # carry the script index across
    src_index = _recall(src_obj.assets_file, src_obj.path_id)
    if src_index is not None:
        _remember(target_file, c.path_id, src_index)
    return c


# --------------------------------------------------------------------------
# Texture formats
# --------------------------------------------------------------------------
RGBA32 = 4            # the only format this project re-encodes to, always

# Unity TextureFormat ids seen in the 2016 Android/iOS bundles, with what they
# are. Kept as data (not an enum import) so this module loads on any host.
texture_formats = {
    1: "Alpha8", 2: "ARGB4444", 3: "RGB24", 4: "RGBA32", 5: "ARGB32",
    7: "RGB565", 10: "R10G10B10A2", 12: "RGBA4444", 13: "BGRA32",
    15: "RHalf", 17: "RGB9e5", 22: "RGBFloat", 23: "AlphaFloat", 24: "RGBAHalf",
    25: "RGBAFloat", 27: "RGBHalf", 28: "RGFloat", 41: "DXT1", 42: "DXT5",
    45: "ETC2_RGBA8", 46: "ETC2_RGB", 47: "ETC2_RGBA1", 48: "DXT1Crunched",
    49: "DXT5Crunched", 50: "PVRTC_RGB2", 51: "PVRTC_RGBA2", 52: "PVRTC_RGB4",
    53: "PVRTC_RGBA4", 54: "ETC_RGB4", 55: "ATC_RGB4", 56: "ATC_RGBA8",
    57: "EAC_R", 58: "EAC_R_SIGNED", 59: "EAC_RG", 60: "EAC_RG_SIGNED",
    61: "ETC2_RGB_PUNCHTHROUGH_ALPHA1", 62: "BGRA32_SRGB", 63: "R16",
}

# Formats UnityPy can decode but NOT re-encode. Anything we write goes to RGBA32.
UNREENCODABLE = {45, 46, 47, 54, 41, 42, 48, 49, 50, 51, 52, 53, 55, 56,
                 57, 58, 59, 60, 61, 2, 12, 10, 7, 5, 3, 1, 17, 15, 27,
                 24, 25, 28, 22, 23, 63}


def format_name(fmt):
    return texture_formats.get(int(fmt), "format %d" % int(fmt))


def mesh_handler(mesh):
    """MeshHandler(mesh), processed, or None.

    UnityPy's own vertex-stream reader, exposed here so callers never have to
    import UnityPy themselves. These bundles ship uncompressed meshes, which is
    what MeshHandler can turn into a vertex array; a compressed one returns None
    instead of pretending, because it would silently export a husk.
    """
    mod = _import_unitypy()
    if mod is None:
        return None
    from UnityPy.helpers.MeshHelper import MeshHandler   # noqa: PLC0415
    h = MeshHandler(mesh)
    h.process()
    return h


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def selftest(bundle_path_):
    """Round-trip a bundle with NO edits and prove nothing changed.

    Returns (ok, report). Run this after upgrading UnityPy, and on any new
    bundle shape, before trusting a build.

    The check is the serialized file metadata, compared byte for byte. The
    object-record table lives in that metadata, so a clobbered
    script_type_index shows up here as a two-byte difference -- which is
    precisely the corruption the guard exists to prevent, and precisely the thing
    that turns into a silent "splash screen, then grey" on device. Comparing
    the re-serialized bytes (rather than the parsed objects) also avoids
    crediting a save that merely happens to parse back to the same values.
    """
    mod = _import_unitypy()
    if mod is None:
        return False, missing_reason()

    if not os.path.isfile(bundle_path_):
        return False, "no such file: %s" % bundle_path_
    with open(bundle_path_, "rb") as fh:
        head = fh.read(8)
    if not head.startswith(b"UnityFS"):
        return False, ("not a plain UnityFS bundle (first bytes %r). Decrypt it "
                       "first -- `asset_tools decrypt <name> -o out.bundle`."
                       % head)

    env = mod.load(bundle_path_)
    try:
        before = serialized_bytes(env)
        counts = [(len(sf.objects), sum(1 for o in sf.objects.values()
                                        if o.class_id == 114))
                  for sf in serialized_files(env)]
        packed = bundle_of(env).save(packer="lz4")
    finally:
        close_env(env)
        del env

    tmp = tmp_path(bundle_path_, "selftest")
    with open(tmp, "wb") as fh:
        fh.write(packed)
    try:
        env = mod.load(tmp)
        try:
            after = serialized_bytes(env)
        finally:
            close_env(env)
            del env
    finally:
        _remove_quietly(tmp)

    problems = []
    if sorted(before) != sorted(after):
        problems.append("file set changed: %s -> %s"
                        % (sorted(before), sorted(after)))
    for name, was in before.items():
        now = after.get(name)
        if now is None:
            continue
        if now == was:
            continue
        where = [i for i, (a, b) in enumerate(zip(was, now)) if a != b]
        problems.append("%s: %d/%d bytes differ, first at 0x%X (script_type_index)"
                        % (name, len(where), len(was),
                           where[0] if where else 0))

    objects = sum(c[0] for c in counts)
    monos = sum(c[1] for c in counts)
    report = ("%d objects (%d MonoBehaviours), %d bytes in / %d out, "
              "metadata byte-identical"
              % (objects, monos, os.path.getsize(bundle_path_), len(packed)))
    return (not problems), (report if not problems
                            else report + " -- FAILED: " + "; ".join(problems[:6]))


def _remove_quietly(path, attempts=5):
    """Delete a temp file, tolerating a handle that has not been collected yet."""
    for i in range(attempts):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            import gc
            import time
            gc.collect()
            time.sleep(0.05 * (i + 1))
    return False


if __name__ == "__main__":
    # py src/tools/unitypy_fix.py <bundle> [<bundle> ...]
    if not have_unitypy():
        print(missing_reason())
        raise SystemExit(2)
    targets = sys.argv[1:] or []
    if not targets:
        print(__doc__)
        raise SystemExit(2)
    rc = 0
    for t in targets:
        ok, report = selftest(t)
        print("%-14s %s  %s" % (os.path.basename(t), "OK  " if ok else "FAIL",
                                report))
        rc |= 0 if ok else 1
    raise SystemExit(rc)
