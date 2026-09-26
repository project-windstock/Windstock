"""
fix_shiny_faces.py -- redo ONLY the face / eye textures of existing shiny bundles with
shiny_colour.recolour_ink_sheet (skin-vs-line unmixing), leaving every body texture
byte-for-byte as it was (pm0025_s / pm0026_s bodies are hand-tuned; the rest were checked
on device).

The faces used to be recoloured pixel by pixel, which left a rough rim round every eye
(an orange ring on shiny Charmander, olive specks on Eevee). The normal bundle's face and
eye sheets are the source; the colour change comes from the same icon-pair mapping
make_all_shinies.py uses.

  python3 fix_shiny_faces.py                       # 1-151, iOS assets
  python3 fix_shiny_faces.py 4 4 --preview /tmp/p  # one species, before|after PNGs, no write
  python3 fix_shiny_faces.py --assets ../server/assets   # Android
"""
import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_shiny_bundle as msb  # noqa: E402  (also loads unitypy_fix)
import shiny_colour  # noqa: E402
import UnityPy  # noqa: E402
from PIL import Image  # noqa: E402

ICONS = os.path.join(HERE, "_shiny_icons")


def is_face(name, tex):
    """Face / eye sheets, whatever the variant: pm0133_00_Face, pm0004_00_Eye1, and the
    odd ones the old matcher missed entirely (their faces never went shiny):
    pm0102_00_AFace, pm0103_00_FaceCEyeAndMouth, pm0115_51_EyeA1, pm0115_00_FaceB."""
    return tex.startswith(name + "_") and ("Face" in tex or "Eye" in tex)


def fix(num, assets, manifest, preview=None):
    name, shiny = f"pm{num:04d}", f"pm{num:04d}_s"
    normal_icon = os.path.join(ICONS, f"pokemon_icon_{num:03d}_00.png")
    shiny_icon = os.path.join(ICONS, f"pokemon_icon_{num:03d}_00_shiny.png")
    mapping = shiny_colour.learn(normal_icon, shiny_icon)
    src = UnityPy.load(msb.decrypt(open(os.path.join(assets, name), "rb").read(),
                                   msb.digest_key(assets, name)))
    faces = {}
    for o in src.objects:
        if o.type.name == "Texture2D":
            t = o.read()
            if is_face(name, t.m_Name):
                faces[t.m_Name] = t.image.convert("RGBA")
    if not faces:
        return 0
    key = bytes.fromhex(manifest[shiny]["key"])
    tmp = os.path.join(HERE, f".{shiny}.{os.getpid()}.plain.bundle")
    open(tmp, "wb").write(msb.decrypt(open(os.path.join(assets, shiny), "rb").read(), key))
    try:
        env = UnityPy.load(tmp)
        bundle = list(env.files.values())[0]
        n = 0
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            t = o.read()
            if t.m_Name not in faces:
                continue
            old = t.image.convert("RGBA")
            # the current shiny face's flat skin already matches the body (same recolour,
            # hand-tuned for 25/26); use it -- unless the face was never recoloured
            same = old.tobytes() == faces[t.m_Name].tobytes()
            new = shiny_colour.recolour_ink_sheet(faces[t.m_Name], mapping,
                                                  target=None if same else old)
            if preview:
                w, h = new.size
                pair = Image.new("RGB", (w * 3, h))
                for i, im in enumerate((faces[t.m_Name], old, new)):
                    pair.paste(im.convert("RGB"), (i * w, 0))
                pair.save(os.path.join(preview, f"{t.m_Name}.png"))
            t.m_TextureFormat = 4                             # RGBA32
            t.image = new
            t.save()
            n += 1
        if preview or not n:
            return n
        packed = bundle.save(packer="lz4")
    finally:
        os.remove(tmp)
    k = os.urandom(16)
    out = msb.encrypt(packed, k)
    assert msb.decrypt(out, k) == packed
    open(os.path.join(assets, shiny), "wb").write(out)
    version = int(time.time() * 1_000_000)
    manifest[shiny] = {"asset_id": f"{uuid.uuid4()}/{version}", "bundle_name": shiny,
                       "version": version, "checksum": msb._crc32c(out) & 0xFFFFFFFF,
                       "size": len(out), "key": k.hex()}
    return n


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("first", type=int, nargs="?", default=1)
    ap.add_argument("last", type=int, nargs="?", default=151)
    ap.add_argument("--assets", default=os.path.join(msb.SERVER, "assets_ios"))
    ap.add_argument("--preview")
    a = ap.parse_args(argv)
    mpath = os.path.join(a.assets, "extra_digest.json")
    manifest = json.load(open(mpath))
    done, none, failed = [], [], []
    for num in range(a.first, a.last + 1):
        if f"pm{num:04d}_s" not in manifest:
            continue
        try:
            n = fix(num, a.assets, manifest, a.preview)
            (done if n else none).append(num)
            print(f"pm{num:04d}_s: {n} face/eye texture(s)", flush=True)
        except Exception as e:
            failed.append((num, str(e)))
            print(f"pm{num:04d}_s: FAILED {e}", flush=True)
        if not a.preview and done and done[-1] == num:
            json.dump(manifest, open(mpath, "w"), indent=1)      # save as we go
    print(f"\nfixed {len(done)}, no face sheets {len(none)}, failed {len(failed)}")
    for num, why in failed:
        print(f"  FAILED #{num}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
