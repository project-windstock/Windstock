"""
cli.py -- the asset tools from a terminal.

One entry point for the whole pm-bundle workflow, so `py src/asset_tools/cli.py
--help` is the index instead of a directory of half-overlapping scripts. Each
subcommand is a thin wrapper over :mod:`asset_tools.assetlib`; the logic lives
there, because the desktop editor calls exactly the same functions and a build
that works in the GUI must work here too.

    py src/asset_tools/cli.py status
    py src/asset_tools/cli.py list [--variants-only]
    py src/asset_tools/cli.py inspect pm0025
    py src/asset_tools/cli.py textures pm0025 [--out DIR]
    py src/asset_tools/cli.py export pm0025 -o pm0025.glb
    py src/asset_tools/cli.py build pm0025 --variant shiny
    py src/asset_tools/cli.py build pm0025 --variant custom --replace-texture Body
    py src/asset_tools/cli.py verify --all
    py src/asset_tools/cli.py decrypt pm0025 -o pm0025.bundle
    py src/asset_tools/cli.py selftest pm0025.bundle   # needs a plain bundle
    py src/asset_tools/cli.py editor
"""
import argparse
import os
import sys

if __package__ in (None, ""):          # allow `py src/asset_tools/cli.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_tools import assetlib as A
from asset_tools import bundle_crypto as C
from asset_tools import config
from asset_tools import unitypy_fix as U


def _need_engine():
    """UnityPy, pycryptodome, crcmod, Pillow. Say which, once, and stop."""
    reason = A.available()
    if reason:
        print("cannot use the asset engine: %s" % reason, file=sys.stderr)
        return False
    return True


def _name(arg):
    """Normalise user input to a bare bundle name: pm0025, pm0025_s.

    Accepts pm0025, pm0025.bundle, pm0025_s.bundle and case differences. The
    ".bundle" suffix is added by the engine when it builds paths, so stripping it
    here is what lets every command take whichever form a user tries first.
    """
    n = str(arg).strip()
    if n.lower().endswith(".bundle"):
        n = n[:-len(".bundle")]
    n = n.lower()
    if not A.is_bundle_name(n):
        raise SystemExit("not a bundle name: %r (expected pm0025 or pm0025_s)"
                         % arg)
    return n


def _pretty_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


# --------------------------------------------------------------------------
# status / list / inspect
# --------------------------------------------------------------------------
def cmd_status(a):
    info = config.describe(a.platform)
    s = A.summary(a.platform)
    print("%d bundles in %s" % (s["bundles"], info["dir"]))
    print("  source        %s" % info["source"])
    print("  normal        %d" % s["normal"])
    print("  variants      %d" % s["variants"])
    print("  replace mode  %s" % ("ON (settings.json allow_replace)"
                                  if info["allow_replace"] else "off"))
    print("  settings      %s" % info["settings_file"])
    if info["missing_configured"]:
        print("  WARNING: settings.json points at a directory that does not "
              "exist, so the shipped set is being used instead")
    if s["missing"]:
        print("  missing dex   %s" % ", ".join(s["missing"]))
    if not _need_engine():
        return 1
    return 0


def cmd_list(a):
    rows = A.list_bundles(a.platform)
    if a.variants_only:
        rows = [r for r in rows if r["ours"]]
    if a.dex:
        rows = [r for r in rows if r["dex"] == a.dex]
    if not rows:
        print("no bundles matched")
        return 0
    print("%-18s %-11s %-4s %9s  %s" % ("name", "label", "dex", "size", "flags"))
    for r in rows:
        flags = ",".join(f for f, on in (
            ("digest", r["known"]), ("ours", r["ours"]), ("no-checksum",
             r["known"] and not r["checksum"])) if on)
        print("%-18s %-11s %4s %9s  %s"
              % (r["name"], r["label"][:11], r["dex"] or "-",
                 _pretty_size(r["size"]), flags))
    return 0


def cmd_inspect(a):
    if not _need_engine():
        return 1
    name = _name(a.name)
    path = A.bundle_path(name, a.platform)
    if not path:
        print("no such bundle: %s in %s" % (name, A.assets_dir(a.platform)),
              file=sys.stderr)
        return 1
    with A.Bundle(name, a.platform) as b:
        print("%s  (%s)" % (name, _pretty_size(os.path.getsize(path))))
        counts = b.counts()
        for k, v in sorted(counts.items()):
            if not k.startswith("_") and v:
                print("  %-20s %d" % (k, v))
        tex = b.textures()
        print("  textures (%d):" % len(tex))
        for t in sorted(tex, key=lambda x: x.get("name") or ""):
            print("    %21d  %-28s %dx%d %s%s"
                  % (t["path_id"], t.get("name") or "?",
                     t.get("width") or 0, t.get("height") or 0,
                     t.get("format_name") or t.get("format"),
                     "  (re-encode)" if t.get("reencode") else ""))
        mats = b.materials()
        if mats:
            print("  materials (%d):" % len(mats))
            for m in mats:
                tids = m.get("textures") or []
                print("    %-26s texture=%s"
                      % (m.get("name") or "?",
                         ", ".join(str(x) for x in tids) or "-"))
    return 0



def cmd_textures(a):
    if not _need_engine():
        return 1
    # Default into data/texture_exports, not the cwd: `textures pm0025` with no
    # -o would otherwise spray 13 PNGs across wherever the user happens to be.
    out = a.out or config.data_dir("texture_exports")
    os.makedirs(out, exist_ok=True)
    out = os.path.abspath(out)
    base, _ = A.split_name(_name(a.name))
    written = []
    with A.Bundle(_name(a.name), a.platform) as b:
        for t in sorted(b.textures(), key=lambda x: x.get("name") or ""):
            nm = t.get("name") or ""
            if a.only and a.only.lower() not in nm.lower():
                continue
            dest = os.path.join(out, "%s_%d_%s.png" % (base, t["path_id"], nm))
            with open(dest, "wb") as fh:
                fh.write(b.texture_png(t["path_id"]))   # returns PNG bytes
            written.append(dest)
            print("wrote %s  (%dx%d %s)"
                  % (dest, t.get("width") or 0, t.get("height") or 0,
                     t.get("format_name") or ""))
    if not written:
        print("nothing matched")
    return 0


def cmd_export(a):
    if not _need_engine():
        return 1
    name = _name(a.name)
    if not A.bundle_path(name, a.platform):
        print("no such bundle: %s" % name, file=sys.stderr)
        return 1
    try:
        data = A.export_gltf(name, a.platform, with_textures=not a.no_textures)
    except Exception as exc:
        print("export failed: %s" % exc, file=sys.stderr)
        return 1
    base, suffix = A.split_name(name)
    # Keep the suffix: pm0025_shiny must not overwrite pm0025's export.
    out = a.out or (base + ("_" + suffix if suffix else "") + ".glb")
    with open(out, "wb") as fh:
        fh.write(data)
    st = A.glb_stats(data)
    print("wrote %s (%.1f KB, %d meshes, %d skins, %d images, %d nodes, "
          "%d vertices)" % (out, len(data) / 1024.0, st["meshes"], st["skins"],
                            st["images"], st["nodes"], st["vertices"]))
    return 0


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def cmd_build(a):
    if not _need_engine():
        return 1
    base = A.split_name(_name(a.name))[0]
    preset = None if a.preset == "none" else a.preset
    override = None
    if a.replace_texture:
        from PIL import Image
        override = []
        for spec in a.replace_texture:
            for needle, src in (p.split("=", 1) for p in spec.split(",")):
                img = Image.open(src).convert("RGBA")
                override.append({"name": needle, "png": img})
    try:
        r = A.build_variant(
            base, a.variant, preset=preset, platform=a.platform,
            textures=a.texture, override=override, replace=a.replace,
            face_texture=a.face_texture, register=not a.no_register,
            progress=lambda *parts: print("  " + " ".join(str(p) for p in parts)),
        )
    except Exception as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 1
    if not r.get("ok"):
        # assetlib reports refusals (no such texture, name already taken,
        # replace disabled) as a result dict rather than an exception, so
        # checking ok is what keeps a failed build from looking successful.
        print("build failed: %s" % r.get("error", "unknown error"),
              file=sys.stderr)
        return 1
    for k in ("name", "out", "plain", "encrypted", "size", "checksum",
              "key", "listed", "textures"):
        if k in r and r[k] is not None:
            print("%-11s %s" % (k, r[k]))
    return 0


# --------------------------------------------------------------------------
# crypto
# --------------------------------------------------------------------------
def cmd_verify(a):
    adir = A.assets_dir(a.platform)
    if a.all or not a.name:
        ok, bad = C.verify_all(adir, quiet=a.quiet, out=print)
        return 0 if ok else 1
    rc = 0
    for name in [_name(a.name)]:
        path = os.path.join(adir, name)
        if not os.path.isfile(path):
            print("%s: not found in %s" % (name, adir))
            rc = 1
            continue
        with open(path, "rb") as fh:
            enc = fh.read()
        # Through the merged manifest, not the genuine digest: a variant we built
        # has its key and checksum in extra_digest.json, and "verify" has to work
        # on our own output or it is not a check of anything.
        e = A.manifest_entry(name, a.platform)
        if not e:
            print("%s: not in the asset digest and not in extra_digest.json"
                  % name)
            rc = 1
            continue
        key = A.key_for(name, a.platform)
        problems = C.verify_encrypted(enc, key, e.get("checksum"),
                                      e.get("size"))
        ok = not problems
        print("%-18s %s  %s%s" % (name, "OK" if ok else "FAIL",
                                  "checksum, size, HMAC, UnityFS header"
                                  if ok else "; ".join(problems),
                                  "  (ours)" if e.get("ours") else ""))
        rc |= 0 if ok else 1
    return rc


def cmd_decrypt(a):
    adir = A.assets_dir(a.platform)
    name = _name(a.name)
    with open(os.path.join(adir, name), "rb") as fh:
        enc = fh.read()
    e = A.manifest_entry(name, a.platform)
    key = A.key_for(name, a.platform)
    pt = C.decrypt(enc, key)
    declared = C.unityfs_size(pt) if C.is_bundle(pt) else -1
    ok = C.is_bundle(pt) and declared == len(pt)
    print("%s: enc=%dB key=%s -> dec=%dB magic=%r declared=%d crc32c=%#010x %s"
          % (name, len(enc), key.hex(), len(pt), pt[:8], declared,
             C.crc32c(enc), "OK" if ok else "CHECK"))
    if not ok:
        return 1
    out = a.out
    if not out:
        base, suffix = A.split_name(name)
        out = os.path.join(adir, base + (("_" + suffix) if suffix else "")
                           + ".plain.bundle")
        print("  (plain bundles are written beside the assets; move them out "
              "before serving)")
    with open(out, "wb") as fh:
        fh.write(pt)
    print("wrote %s" % out)
    return 0


def cmd_selftest(a):
    if not U.have_unitypy():
        print(U.missing_reason())
        return 2
    rc = 0
    for raw in a.paths:
        # Accept a bundle name like every other subcommand, or a real path
        # (a decrypted .bundle, or a bundle from a non-default assets dir).
        path = raw if os.path.isfile(raw) else A.bundle_path(_name(raw), a.platform)
        ok, report = U.selftest(path)
        print("%-20s %s  %s" % (os.path.basename(path), "OK  " if ok else "FAIL",
                                report))
        rc |= 0 if ok else 1
    return rc


def cmd_editor(a):
    try:
        from asset_tools.editor import launch
    except ImportError as exc:
        print("the editor needs customtkinter and Pillow: %s" % exc,
              file=sys.stderr)
        print("pip install customtkinter pillow numpy", file=sys.stderr)
        return 2
    return launch(platform=a.platform, name=a.name)


# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="asset_tools",
        description="read, edit, preview and rebuild Pokemon GO asset bundles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--platform", default="android", choices=("android", "ios"))
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="where the bundles are and what is enabled"
                   ).set_defaults(fn=cmd_status)

    p = sub.add_parser("list", help="inventory of bundles on disk")
    p.add_argument("--variants-only", action="store_true")
    p.add_argument("--dex", type=int, help="only this dex number")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("inspect", help="objects, textures and materials")
    p.add_argument("name")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("textures", help="export every texture as PNG")
    p.add_argument("name")
    p.add_argument("-o", "--out", help="output directory (default: cwd)")
    p.add_argument("--only", help="substring of the texture name to keep")
    p.set_defaults(fn=cmd_textures)

    p = sub.add_parser("export", help="export a rigged, textured binary glTF")
    p.add_argument("name")
    p.add_argument("-o", "--out", help="output .glb (default: <name>.glb)")
    p.add_argument("--no-textures", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("build", help="recolour and build a variant bundle")
    p.add_argument("name", help="base bundle, e.g. pm0025")
    p.add_argument("--variant", default="s",
                   help="suffix letter(s); %s" % ", ".join(A.VARIANT_SUFFIXES))
    p.add_argument("--preset", default="shiny",
                   choices=sorted(A.PRESETS) + ["none", "custom"],
                   help="'none' keeps colours, 'custom' uses --replace-texture")
    p.add_argument("--texture", action="append",
                   help="limit the recolour to textures matching this substring "
                        "(repeatable)")
    p.add_argument("--replace-texture", action="append", metavar="NAME=PNG",
                   help="replace a texture with a PNG, comma separated for "
                        "several")
    p.add_argument("--face-texture", help="path_id to use as the face texture")
    p.add_argument("--replace", action="store_true",
                   help="overwrite the base bundle (needs allow_replace in "
                        "settings.json; backs it up first)")
    p.add_argument("--no-register", action="store_true",
                   help="write the file but leave extra_digest.json alone")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("verify", help="check bundles against the asset digest")
    p.add_argument("name", nargs="?", help="one bundle; default is --all")
    p.add_argument("--all", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("decrypt", help="decrypt one bundle to a plain file")
    p.add_argument("name")
    p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_decrypt)

    p = sub.add_parser("selftest", help="prove a plain bundle round trips "
                                        "byte-identically")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("editor", help="launch the desktop editor")
    p.add_argument("name", nargs="?", help="bundle to open on start")
    p.set_defaults(fn=cmd_editor)
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
