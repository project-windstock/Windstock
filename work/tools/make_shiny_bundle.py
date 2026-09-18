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
     (the shift is passed in, or measured from two icon PNGs)
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
Requires: UnityPy, Pillow, pycryptodome, crcmod
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


def build(num, assets, dh, ks, kv, body_texture=None, tips=True):
    name = f"pm{num:04d}"
    shiny = name + "_s"
    enc = open(os.path.join(assets, name), "rb").read()
    plain = decrypt(enc, digest_key(assets, name))
    tmp = os.path.join(HERE, f".{name}.plain.bundle")
    open(tmp, "wb").write(plain)
    try:
        env = UnityPy.load(tmp)
        bundle = list(env.files.values())[0]
        # body + face: v53 only recoloured the body, so the face texture (around the
        # eyes) stayed normal yellow on a shiny body -- spotted on device 2026-09-16
        want = {body_texture or f"{name}_00_BodyExample": (tips, False),
                f"{name}_00_Face": (False, True)}          # (tips, hidden_only)
        swapped = 0
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            t = o.read()
            if t.m_Name not in want:
                continue
            tips, hidden_only = want[t.m_Name]
            img = recolour(t.image, dh, ks, kv, tips=tips, hidden_only=hidden_only)
            t.m_TextureFormat = 4                             # RGBA32
            t.image = img
            t.save()
            swapped += 1
        if not swapped:
            raise SystemExit(f"{name}: textures {list(want)} not found")
        print(f"recoloured {swapped} texture(s)")
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
    a = ap.parse_args(argv)
    if a.shift:
        dh, ks, kv = a.shift
    elif a.normal_icon and a.shiny_icon:
        nh, ns, nv = fur_stats(a.normal_icon)
        sh, ss, sv = fur_stats(a.shiny_icon)
        dh, ks, kv = sh - nh, ss / ns, sv / nv
    else:
        ap.error("give --shift or both --normal-icon and --shiny-icon")
    print(f"colour shift: hue {dh * 360:+.1f} deg, saturation x{ks:.2f}, value x{kv:.2f}")
    shiny, size, entry = build(a.number, a.assets, dh, ks, kv, a.texture, tips=not a.no_tips)
    print(f"wrote {shiny} ({size} bytes), checksum {entry['checksum']:#010x}, "
          f"asset_id {entry['asset_id']}")


if __name__ == "__main__":
    main(sys.argv[1:])
