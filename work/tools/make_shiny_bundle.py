"""
make_shiny_bundle.py -- build a shiny variant of a genuine 2016 Pokemon model bundle.

The 0.29/0.35 client has no shinies, so a shiny is a SEPARATE bundle (pm0025_s) with a
recoloured body texture, which the windstock tweak asks the game to load instead of
pm0025 when the Pokemon on screen is shiny. Everything is built from YOUR OWN genuine
bundles; the output stays in the server's (git-ignored) assets folders.

Pipeline:
  1. decrypt the genuine bundle (assets_ios/pm####, key from its asset_digest)
  2. recolour the body texture: fur hue/saturation/value shifted by the difference
     between the official normal and shiny icons, dark tips lifted to grey
     (the shift is passed in, or measured from two icon PNGs) -- or, with --map, every
     body texture through a per-colour-region mapping learned from the icon pair
     (shiny_colour.py), which handles multi-colour shinies like Charizard
  3. rename the AssetBundle (+ container alias pm####_s) and its CAB, so it can be
     loaded next to the normal model
  4. pack (LZ4) and ENCRYPT in the client's container format:
        [0x01][16-byte IV][AES-128-CBC(PKCS7)][HMAC-SHA1(aes_key, everything before)]
     AES key = digest_key XOR "PFAi$;]G7Rg>kz4w"
     digest checksum = CRC-32C(encrypted file)        -- both verified on 302/302
     genuine 2016 bundles (iOS + Android), 2026-09-16
  5. write <assets>/pm####_s and add its entry to <assets>/extra_digest.json, which
     the server appends to the genuine asset digest

Usage:
  python3 make_shiny_bundle.py 25 --normal-icon pokemon_icon_025_00.png \\
          --shiny-icon pokemon_icon_025_00_shiny.png
  python3 make_shiny_bundle.py 25 --shift -0.0094 1.38 0.98      # hue, sat x, val x
  python3 make_shiny_bundle.py 6 --map --normal-icon ... --shiny-icon ...  # any species
Requires: UnityPy, Pillow, pycryptodome, crcmod (+ numpy for --map)
"""
import argparse
import colorsys
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import crcmod.predefined
import UnityPy
from Crypto.Cipher import AES
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server")
sys.path.insert(0, HERE)
sys.path.insert(0, SERVER)
from decrypt_bundle import KEY_MASK, decrypt, digest_key  # noqa: E402
import unitypy_fix  # noqa: E402,F401  -- keep per-object script indices on save

_crc32c = crcmod.predefined.mkCrcFun("crc-32c")


def fur_stats(path):
    im = Image.open(path).convert("RGBA")
    hs, ss, vs = [], [], []
    for r, g, b, a in im.getdata():
        if a < 200:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if 0.08 < h < 0.20 and s > 0.35 and v > 0.55:
            hs.append(h); ss.append(s); vs.append(v)
    if not hs:
        raise SystemExit(f"{path}: no fur-coloured pixels found")
    n = len(hs)
    return sum(hs) / n, sum(ss) / n, sum(vs) / n


def recolour(img, dh, ks, kv, tips=True, hidden_only=False):
    """Shift the fur colour. tips=True also lifts near-black to shiny grey (the body
    texture's ear/tail tips). hidden_only=True recolours ONLY fully transparent pixels:
    the face material is drawn opaque, so the face texture's "transparent" pixels show
    their hidden RGB -- normal yellow -- as a pale patch around the eyes; the eyes
    themselves (opaque pixels) are left exactly as they are."""
    img = img.convert("RGBA")
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if hidden_only and a != 0:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if 0.06 < h < 0.22 and s > 0.25:                 # fur
                h = (h + dh) % 1.0
                s = min(1.0, s * ks)
                v = min(1.0, v * kv)
                r, g, b = [int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v)]
            elif tips and v < 0.18:                           # black tips -> shiny grey
                r = g = b = 78
            px[x, y] = (r, g, b, a)
    return img


def encrypt(plain, key16):
    aes_key = bytes(a ^ b for a, b in zip(key16, KEY_MASK))
    iv = os.urandom(16)
    pad = 16 - len(plain) % 16
    body = b"\x01" + iv + AES.new(aes_key, AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad)
    return body + hmac.new(aes_key, body, hashlib.sha1).digest()


def hue_shift_recolourer(num, dh, ks, kv, body_texture=None, tips=True):
    """The original Pikachu mode: one fur-hue shift on the body, hidden face pixels."""
    name = f"pm{num:04d}"
    want = {body_texture or f"{name}_00_BodyExample": (tips, False),
            f"{name}_00_Face": (False, True)}                  # (tips, hidden_only)

    def fn(tex_name, img):
        if tex_name not in want:
            return None
        t, hidden = want[tex_name]
        return recolour(img, dh, ks, kv, tips=t, hidden_only=hidden)
    return fn


# Ponyta / Rapidash: the flames are meshes whose textures are region MASKS; the colour
# comes from the FireSten materials' _Color1 (inner, yellow) and _Color2 (outer, orange).
# The shiny flames are blue, which the icon mapping can't reach (it extrapolates pure
# yellow to green), so those two colours are set from the shiny icon's flame pixels.
# Every other fire Pokemon keeps its normal flames when shiny.
FLAME_SPECIES = {77, 78}


def shiny_flame_colours(shiny_icon):
    """(inner, outer) RGB 0..1 from the blue flame pixels of a shiny icon."""
    import numpy as np
    a = np.asarray(Image.open(shiny_icon).convert("RGBA"), dtype=float) / 255
    px = a[a[..., 3] > 0.8][:, :3]
    hsv = np.array([colorsys.rgb_to_hsv(*p) for p in px])
    flame = px[(hsv[:, 0] > 0.5) & (hsv[:, 0] < 0.75) & (hsv[:, 1] > 0.2) & (hsv[:, 2] > 0.6)]
    if len(flame) < 200:
        raise SystemExit(f"{shiny_icon}: no blue flame pixels")
    by_light = flame[np.argsort(flame.sum(1))]
    q = len(flame) // 4
    return np.median(by_light[-q:], 0), np.median(by_light[:q], 0)


def flame_material_fn(shiny_icon):
    inner, outer = shiny_flame_colours(shiny_icon)
    print(f"flames: inner {inner.round(2).tolist()}, outer {outer.round(2).tolist()}")

    def fn(mat):
        if not mat["m_Name"].startswith("FireSten"):
            return False
        for prop, value in mat["m_SavedProperties"]["m_Colors"]:
            rgb = {"_Color1": inner, "_Color2": outer}.get(prop["name"])
            if rgb is not None:
                value["r"], value["g"], value["b"] = (float(c) for c in rgb)
        return True
    return fn


def icon_map_recolourer(num, normal_icon, shiny_icon):
    """Every body texture through a colour mapping learned from the icon pair
    (shiny_colour.py); the face and eye textures' hidden background too (eyes themselves --
    the opaque pixels -- are left alone)."""
    import shiny_colour
    name = f"pm{num:04d}"
    mapping = shiny_colour.learn(normal_icon, shiny_icon)
    err, base = shiny_colour.self_check(mapping, normal_icon, shiny_icon)
    print(f"{name}: {mapping.describe()}, icon error {err:.1f} (untouched {base:.1f})")

    import shiny_overrides

    def fn(tex_name, img):
        if tex_name.startswith(f"{name}_00_Body"):
            mapped = shiny_colour.recolour_image(img, mapping)
            mapped = shiny_overrides.apply(num, tex_name, img, mapped)   # hand-tuned species
            return shiny_colour.clean_edges(img, mapped)                   # no fringes at edges
        # v3: eye textures too -- every Eye1 sheet is painted on the Pokemon's normal body
        # colour (hidden under alpha 0), which showed as a normal-coloured rim around the
        # eyes of most shinies. Same treatment as the face.
        if tex_name.startswith(name + "_") and ("Face" in tex_name or "Eye" in tex_name):
            # the face is drawn opaque: its "transparent" pixels show their hidden colour, and
            # the anti-aliased rim around each eye shows a MIX -- so every pixel changes in
            # proportion to how transparent it is (v1 changed alpha==0 only: a rim of the
            # normal colour stayed around the eyes)
            # v4: skin-vs-line unmixing (recolour_ink_sheet) -- per-pixel mapping + edge
            # clean-up left rough halos round every eye (orange on Charmander, olive specks
            # on Eevee). Eyes/mouths stay as drawn; only the skin under them changes.
            return shiny_colour.recolour_ink_sheet(img, mapping)
        return None
    return fn


def build(num, assets, recolour_fn, preview_dir=None, material_fn=None):
    name = f"pm{num:04d}"
    shiny = name + "_s"
    enc = open(os.path.join(assets, name), "rb").read()
    plain = decrypt(enc, digest_key(assets, name))
    tmp = os.path.join(HERE, f".{name}.{os.getpid()}.plain.bundle")    # unique: builds may run side by side
    open(tmp, "wb").write(plain)
    try:
        env = UnityPy.load(tmp)
        bundle = list(env.files.values())[0]
        # body + face: v53 only recoloured the body, so the face texture (around the
        # eyes) stayed normal yellow on a shiny body -- spotted on device 2026-09-16
        swapped = 0
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            t = o.read()
            src = t.image
            img = recolour_fn(t.m_Name, src)
            if img is None:
                continue
            if preview_dir:
                pair = Image.new("RGBA", (src.width * 2, src.height))
                pair.paste(src.convert("RGBA"), (0, 0)); pair.paste(img, (src.width, 0))
                pair.save(os.path.join(preview_dir, f"{t.m_Name}.png"))
            t.m_TextureFormat = 4                             # RGBA32
            t.image = img
            t.save()
            swapped += 1
        if not swapped:
            raise SystemExit(f"{name}: no texture to recolour")
        print(f"recoloured {swapped} texture(s)")
        if material_fn:
            for o in env.objects:
                if o.type.name == "Material":
                    mat = o.read_typetree()
                    if material_fn(mat):
                        o.save_typetree(mat)
                        print(f"recoloured material {mat['m_Name']}")
        for o in env.objects:
            if o.type.name == "AssetBundle":
                ab = o.read_typetree()
                ab["m_Name"] = shiny
                if "m_AssetBundleName" in ab:
                    ab["m_AssetBundleName"] = shiny
                extra = [(k.replace(name, shiny, 1), dict(info))
                         for k, info in ab["m_Container"] if name in k and shiny not in k]
                ab["m_Container"] = list(ab["m_Container"]) + extra
                o.save_typetree(ab)
        old = next(iter(bundle.files))
        cab = "CAB-" + hashlib.md5(shiny.encode()).hexdigest()
        f = bundle.files.pop(old)
        bundle.files[cab] = f
        try:
            f.name = cab
        except Exception:
            pass
        packed = bundle.save(packer="lz4")
    finally:
        os.remove(tmp)

    key = os.urandom(16)
    out = encrypt(packed, key)
    # sanity: decrypts back to the exact packed bundle and passes both client checks
    assert decrypt(out, key) == packed
    aes_key = bytes(a ^ b for a, b in zip(key, KEY_MASK))
    assert hmac.new(aes_key, out[:-20], hashlib.sha1).digest() == out[-20:]
    open(os.path.join(assets, shiny), "wb").write(out)

    manifest_path = os.path.join(assets, "extra_digest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    version = int(time.time() * 1_000_000)
    manifest[shiny] = {"asset_id": f"{uuid.uuid4()}/{version}", "bundle_name": shiny,
                       "version": version, "checksum": _crc32c(out) & 0xFFFFFFFF,
                       "size": len(out), "key": key.hex()}
    json.dump(manifest, open(manifest_path, "w"), indent=1)
    return shiny, len(out), manifest[shiny]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("number", type=int)
    ap.add_argument("--assets", default=os.path.join(SERVER, "assets_ios"))
    ap.add_argument("--normal-icon")
    ap.add_argument("--shiny-icon")
    ap.add_argument("--shift", nargs=3, type=float, metavar=("HUE", "SATx", "VALx"))
    ap.add_argument("--texture", help="body texture name (default pm####_00_BodyExample)")
    ap.add_argument("--no-tips", action="store_true",
                    help="leave near-black areas alone (Pikachu's ear/tail tips go grey when "
                         "shiny; most other Pokemon keep their dark markings)")
    ap.add_argument("--map", action="store_true",
                    help="learn a per-region colour mapping from the icon pair and apply it "
                         "to every body texture (any species) instead of one fur-hue shift")
    ap.add_argument("--preview", help="write before|after PNGs of each recoloured texture here")
    a = ap.parse_args(argv)
    if a.map:
        if not (a.normal_icon and a.shiny_icon):
            ap.error("--map needs --normal-icon and --shiny-icon")
        fn = icon_map_recolourer(a.number, a.normal_icon, a.shiny_icon)
        mat_fn = flame_material_fn(a.shiny_icon) if a.number in FLAME_SPECIES else None
    else:
        if a.shift:
            dh, ks, kv = a.shift
        elif a.normal_icon and a.shiny_icon:
            nh, ns, nv = fur_stats(a.normal_icon)
            sh, ss, sv = fur_stats(a.shiny_icon)
            dh, ks, kv = sh - nh, ss / ns, sv / nv
        else:
            ap.error("give --shift or both --normal-icon and --shiny-icon")
        print(f"colour shift: hue {dh * 360:+.1f} deg, saturation x{ks:.2f}, value x{kv:.2f}")
        fn = hue_shift_recolourer(a.number, dh, ks, kv, a.texture, tips=not a.no_tips)
        mat_fn = None
    shiny, size, entry = build(a.number, a.assets, fn, a.preview, mat_fn)
    print(f"wrote {shiny} ({size} bytes), checksum {entry['checksum']:#010x}, "
          f"asset_id {entry['asset_id']}")


if __name__ == "__main__":
    main(sys.argv[1:])
