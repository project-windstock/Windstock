"""
client_assets.py -- the OTHER half of Unity asset modding: the files inside the
APK/IPA, as opposed to the pm#### bundles the server serves.

This one is about the client's own data folder:

  globalgamemanagers        engine managers (PlayerSettings, GraphicsSettings)
  globalgamemanagers.assets the ~20 shaders (incl. Holo/Character) + MonoScripts
  sharedassets0.assets      467 Texture2D + 487 Sprite + the UI atlases
  resources.assets          751 meshes, 283 AudioClip, 645 Animator, prefabs
  level0..level3            scenes

Android ships each large one split into 1 MB ``.split*`` chunks, which is what
``join`` and ``split`` are for; iOS keeps them whole.

    py src/asset_tools/client_assets.py list    <file> [--type Texture2D]
    py src/asset_tools/client_assets.py export  <file> <name> <out.png>
    py src/asset_tools/client_assets.py replace <file> <name> <in.png> [-o OUT]
    py src/asset_tools/client_assets.py join    <dir> <base>
    py src/asset_tools/client_assets.py split   <file> [--chunk 1048576]

Repack flow: join -> edit -> split back -> zip into the APK at
``assets/bin/Data/`` -> zipalign + apksigner. iOS: edit in place, repack
``Payload/.../Data``, re-sign.

Two things this module does that the obvious implementation does not:

* it loads through :mod:`asset_tools.unitypy_fix`, because stock UnityPy mangles
  Unity 5.3 files on save -- a "replace one texture" round trip that looks
  successful and produces a file the game will not load is worse than no tool;
* ``replace`` writes atomically via a temp file, and refuses to overwrite its
  input unless you ask for it explicitly.
"""
import argparse
import collections
import glob
import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_tools import unitypy_fix as U

CHUNK = 1048576


def _need():
    reason = U.missing_reason()
    if reason:
        raise SystemExit("cannot use UnityPy: %s" % reason)


def _load(path):
    _need()
    # UnityPy resolves cross-file references (streamed .resS data) by looking at
    # siblings in the same folder, so callers are expected to work inside a
    # complete copy of Data/ rather than passing a lone file out of context.
    return U.load(path)


def _textures(env):
    """[(object, data, name)] for every Texture2D, tolerating unreadable ones."""
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        try:
            d = o.read()
        except Exception:
            continue
        yield o, d, (getattr(d, "m_Name", "") or "")


def _find_texture(env, name):
    for o, d, n in _textures(env):
        if n == name:
            return o, d
    return None, None


def cmd_list(a):
    env = _load(a.file)
    counts = collections.Counter()
    rows = []
    for o in env.objects:
        tn = o.type.name
        counts[tn] += 1
        if a.type and tn != a.type:
            continue
        try:
            d = o.read()
        except Exception as exc:
            rows.append("  [%s] <unreadable: %s>" % (tn, exc))
            continue
        name = getattr(d, "m_Name", "") or ""
        if tn == "Texture2D":
            rows.append("  %dx%-5d fmt%-3d %s" % (d.m_Width, d.m_Height,
                                                 int(d.m_TextureFormat), name))
        else:
            rows.append("  [%s] %s" % (tn, name))
    if a.type:
        print("%s: %d %s" % (a.file, counts[a.type], a.type))
    else:
        for k, v in counts.most_common():
            print("  %6d %s" % (v, k))
    if rows:
        print("\n".join(rows))
    return 0


def cmd_export(a):
    from PIL import Image
    env = _load(a.file)
    o, d = _find_texture(env, a.name)
    if not d:
        print("texture %r not found" % a.name, file=sys.stderr)
        return 1
    img = getattr(d, "image", None)          # PIL Image, decoded by UnityPy
    if img is None:
        img = _decode_manually(d)
    if img is None:
        print("could not decode %r (format %s)"
              % (a.name, int(d.m_TextureFormat)), file=sys.stderr)
        return 1
    img.save(a.out)
    print("exported %s (%dx%d fmt%d) -> %s"
          % (a.name, d.m_Width, d.m_Height, int(d.m_TextureFormat), a.out))
    return 0


def _decode_manually(d):
    """Last resort: the image, straight off the raw pixel array."""
    try:
        import numpy as np
        from PIL import Image
        w, h = int(d.m_Width), int(d.m_Height)
        raw = d.image_data.convert("RGBA") if hasattr(d, "image_data") else None
        if raw is None:
            return None
        return Image.frombytes("RGBA", (w, h), raw, "raw", "BGRA")
    except Exception:
        return None


def cmd_replace(a):
    from PIL import Image
    if os.path.abspath(a.out or a.file) == os.path.abspath(a.file) and not a.force:
        raise SystemExit("refusing to overwrite the input in place; pass -o for "
                         "a new file, or --force if you really mean it (keep a "
                         "backup: this rewrites the whole SerializedFile)")
    env = _load(a.file)
    o, d = _find_texture(env, a.name)
    if not d:
        print("texture %r not found" % a.name, file=sys.stderr)
        return 1
    new = Image.open(a.infile).convert("RGBA")
    d.image = new          # UnityPy re-encodes to the texture's own format
    d.save()               # write the object back into the environment
    data = env.file.save()  # serialise the whole file

    out = a.out or a.file
    d2 = os.path.dirname(os.path.abspath(out))
    fd, tmp = tempfile.mkstemp(dir=d2, prefix=".cassets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("replaced %s <- %s (%dx%d); wrote %s (%d B)"
          % (a.name, a.infile, new.width, new.height, out, len(data)))
    return 0


def cmd_join(a):
    parts = sorted(glob.glob(os.path.join(a.dir, a.base + ".split*")),
                   key=lambda p: int(p.rsplit("split", 1)[1]))
    if not parts:
        print("no %s.split* in %s" % (a.base, a.dir), file=sys.stderr)
        return 1
    dst = os.path.join(a.dir, a.base)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as out:
        for p in parts:
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
    os.replace(tmp, dst)
    print("joined %d chunks -> %s (%d B)"
          % (len(parts), dst, os.path.getsize(dst)))
    return 0


def cmd_split(a):
    with open(a.file, "rb") as fh:
        data = fh.read()
    n = 0
    for i in range(0, len(data), a.chunk):
        with open("%s.split%d" % (a.file, n), "wb") as fh:
            fh.write(data[i:i + a.chunk])
        n += 1
    print("split %s -> %d chunks of <= %d B" % (a.file, n, a.chunk))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="client_assets",
        description="list, export and replace assets inside the APK/IPA Data "
                    "folder (not the pm bundles)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="inventory, optionally filtered by type")
    p.add_argument("file")
    p.add_argument("--type", help="e.g. Texture2D, GameObject, Mesh")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("export", help="Texture2D -> PNG")
    p.add_argument("file")
    p.add_argument("name")
    p.add_argument("out")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("replace", help="swap a texture and rewrite the file")
    p.add_argument("file")
    p.add_argument("name")
    p.add_argument("infile")
    p.add_argument("-o", "--out")
    p.add_argument("--force", action="store_true",
                   help="allow overwriting the input file")
    p.set_defaults(fn=cmd_replace)

    p = sub.add_parser("join", help="concat Android .split* chunks")
    p.add_argument("dir")
    p.add_argument("base")
    p.set_defaults(fn=cmd_join)

    p = sub.add_parser("split", help="re-split a file for the APK")
    p.add_argument("file")
    p.add_argument("--chunk", type=int, default=CHUNK)
    p.set_defaults(fn=cmd_split)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a) or 0


if __name__ == "__main__":
    raise SystemExit(main())
