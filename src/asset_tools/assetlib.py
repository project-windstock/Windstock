"""
assetlib -- the engine behind the Asset Editor.

Everything the editor's web UI can do to a Pokemon model bundle, in one place,
so the page and the command line are the same code and cannot drift apart. A
bundle is a Unity 5.3.5f1 `UnityFS` AssetBundle, encrypted in the 2016 client's
own container, that the client downloads per Pokemon and instantiates by name.

The three things this module exists to get right, all of which have gone wrong
before in one form or another:

* **Nothing is ever edited in place.** A build writes a NEW bundle whose name
  carries a variant suffix -- `pm0025_s` (shiny), `pm0025_i` (icy), `pm0025_x`
  (frosty), or whatever suffix you pick -- and leaves the shipped bundle alone.
  Overwriting a normal model is a separate, explicitly enabled operation that
  backs the original up first (see `replace_bundle`). The reason is not
  caution for its own sake: `asset_digest` still carries the shipped entry for
  `pm0025` with its own size and timestamp, so a same-named file with different
  bytes is a bundle whose metadata lies about it.

* **UnityPy is only ever reached through `unitypy_fix`.** Plain UnityPy rewrites
  every MonoBehaviour's `script_type_index` to -1 on save, which the client reads
  as "splash screen, then grey, and the game never contacts the server". The
  module docstring there has the full argument; `selftest()` is the check, and
  `build_variant` runs a scaled-down version of it on the bundle it is about to
  write.

* **The digest is never rewritten.** `asset_digest` is genuine, shipped inside
  the APK, and is what the client trusts. A bundle built here is registered in
  `extra_digest.json` beside it, and `windstock.game.protocol` appends those
  entries to the genuine digest when it answers GET_ASSET_DIGEST, moving the
  timestamp past the newest one so a phone re-fetches. That is why finishing a
  build needs no server restart.

The 3D preview is a real export, not a screenshot: `export_gltf` turns the
rig into a binary glTF 2.0 -- skinned, with the bone hierarchy, the skin
weights and the textures as PNG -- which the browser loads with three.js. So
what you see is the mesh the client will instantiate, and a texture you change
in the page reappears on it immediately.

Requires: UnityPy==1.25.2, Pillow, pycryptodome, crcmod, lz4
    py -m pip install --user UnityPy==1.25.2 Pillow pycryptodome crcmod lz4
Nothing here imports UnityPy at module level, and the game server imports this
module without needing any of it installed -- the editor's Settings panel reads
`available()` to say so.
"""
import colorsys
import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import uuid

from asset_tools import config
from asset_tools import unitypy_fix as U

# The one protobuf codec in the project; see asset_tools/__init__.py for why
# there is a single one rather than a copy.
import pbcodec as pb

# The 151 Kanto models, in dex order, for the editor's picker. Kept here rather
# than imported from admin.py so the library does not depend on the web layer.
DEX_NAMES = (
    "Bulbasaur Ivysaur Venusaur Charmander Charmeleon Charizard Squirtle Wartortle "
    "Blastoise Caterpie Metapod Butterfree Weedle Kakuna Beedrill Pidgey Pidgeotto "
    "Pidgeot Rattata Raticate Spearow Fearow Ekans Arbok Pikachu Raichu Sandshrew "
    "Sandslash NidoranF Nidorina Nidoqueen NidoranM Nidorino Nidoking Clefairy "
    "Clefable Vulpix Ninetales Jigglypuff Wigglytuff Zubat Golbat Oddish Gloom "
    "Vileplume Paras Parasect Venonat Venomoth Diglett Dugtrio Meowth Persian "
    "Psyduck Golduck Mankey Primeape Growlithe Arcanine Poliwag Poliwhirl Poliwrath "
    "Abra Kadabra Alakazam Machop Machoke Machamp Bellsprout Weepinbell Victreebel "
    "Tentacool Tentacruel Geodude Graveler Golem Ponyta Rapidash Slowpoke Slowbro "
    "Magnemite Magneton Farfetchd Doduo Dodrio Seel Dewgong Grimer Muk Shellder "
    "Cloyster Gastly Haunter Gengar Onix Drowzee Hypno Krabby Kingler Voltorb "
    "Electrode Exeggcute Exeggutor Cubone Marowak Hitmonlee Hitmonchan Lickitung "
    "Koffing Weezing Rhyhorn Rhydon Chansey Tangela Kangaskhan Horsea Seadra "
    "Goldeen Seaking Staryu Starmie MrMime Scyther Jynx Electabuzz Magmar Pinsir "
    "Tauros Magikarp Gyarados Lapras Ditto Eevee Vaporeon Jolteon Flareon Porygon "
    "Omanyte Omastar Kabuto Kabutops Aerodactyl Snorlax Articuno Zapdos Moltres "
    "Dratini Dragonair Dragonite Mewtwo Mew"
).split()

# A bundle name is pm#### plus an optional variant suffix: letters/digits/_
VARIANT_SUFFIXES = {
    "s": "Shiny",
    "i": "Icy",
    "x": "Frosty",
    "g": "Golden",
    "m": "Midnight",
    "w": "Winter",
}

_lock = threading.Lock()          # one build at a time, per process


# --------------------------------------------------------------------------
# Availability + paths
# --------------------------------------------------------------------------
def available():
    """Why the editor cannot run, or None when it can. Shown in the UI verbatim."""
    if not U.have_unitypy():
        return U.missing_reason()
    try:
        import PIL                     # noqa: F401
    except ImportError:
        return "Pillow is not installed (py -m pip install --user Pillow)"
    from asset_tools import bundle_crypto
    return bundle_crypto.missing_reason()


def assets_dir(platform="android"):
    return config.assets_dir(platform)


def bundle_path(name, platform="android"):
    return os.path.join(assets_dir(platform), name)


def _read(name, platform="android"):
    with open(bundle_path(name, platform), "rb") as fh:
        return fh.read()


def is_bundle_name(s):
    """pm#### exactly, or pm####_suffix where suffix is lowercase alphanumerics.

    The underscore is what separates a variant from its base, so it has to be
    accepted here -- otherwise every name this module generates is rejected by
    its own validator, which is exactly what happened.
    """
    if not isinstance(s, str) or len(s) < 6:
        return False
    if s[:2] != "pm" or not s[2:6].isdigit():
        return False
    tail = s[6:]
    if tail == "":
        return True
    if not tail.startswith("_"):
        return False
    tail = tail[1:]
    return bool(tail) and tail.isalnum() and tail.islower()


def split_name(s):
    """'pm0025_s' -> ('pm0025', 's'); 'pm0025' -> ('pm0025', '')."""
    if not isinstance(s, str) or not s.startswith("pm"):
        return s, ""
    if len(s) > 6 and s[6] == "_":
        return s[:6], s[7:]
    return s[:6], ""


def dex_of(name):
    return int(split_name(name)[0][2:])


def variant_label(suffix):
    return VARIANT_SUFFIXES.get(suffix, suffix.upper() or "Normal")


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def list_bundles(platform="android", want_digest=True):
    """Every bundle on disk, newest info first. Never decrypts anything -- this
    has to be fast enough to run on every page load."""
    adir = assets_dir(platform)
    digest = {}
    if want_digest:
        # The genuine asset_digest plus anything we built. Both come from the
        # manifest layer rather than from the server, so a bundle the server
        # would not serve still shows up here as a problem worth seeing.
        digest = _manifest_index(platform)
    rows = []
    try:
        names = sorted(os.listdir(adir))
    except OSError:
        return rows
    for fn in names:
        if not fn.startswith("pm") or not is_bundle_name(fn):
            continue
        if not os.path.isfile(os.path.join(adir, fn)):
            continue
        base, suffix = split_name(fn)
        try:
            size = os.path.getsize(os.path.join(adir, fn))
        except OSError:
            size = 0
        dex = dex_of(fn)
        entry = digest.get(fn) or {}
        rows.append({
            "name": fn,
            "dex": dex,
            "base": base,
            "suffix": suffix,
            "variant": variant_label(suffix),
            "size": size,
            "known": fn in digest,
            "ours": bool(suffix),
            "version": int(entry.get("version") or 0),
            "checksum": int(entry.get("checksum") or 0),
            "label": (f"#{dex:03d} {DEX_NAMES[dex - 1]}"
                      if 1 <= dex <= len(DEX_NAMES) and not suffix
                      else f"{fn}"),
        })
    return rows


def summary(platform="android"):
    """Counts for the editor's header. Cheap."""
    rows = list_bundles(platform)
    ours = [r for r in rows if r["ours"]]
    info = config.describe(platform)
    return {
        "platform": platform,
        "dir": assets_dir(platform),
        "configured": info["configured"],
        "source": info["source"],
        "missing_configured": info["missing_configured"],
        "allow_replace": info["allow_replace"],
        "bundles": len(rows),
        "normal": len(rows) - len(ours),
        "variants": len(ours),
        "missing": sorted(set("pm%04d" % d
                              for d in range(1, len(DEX_NAMES) + 1))
                          - {r["base"] for r in rows}),
    }


def _manifest_index(platform="android"):
    """{bundle_name: entry} for everything the server would serve, or could.

    Genuine entries come from asset_digest, ours from extra_digest.json. Ours
    win on a clash, because a rebuilt variant supersedes the entry before it.
    """
    from asset_tools import bundle_crypto
    out = {}
    adir = assets_dir(platform)
    try:
        with open(os.path.join(adir, bundle_crypto.DIGEST_FILE), "rb") as fh:
            for raw in pb.get_all(pb.decode(fh.read()), 1):
                e = pb.decode(raw)
                name = pb.get(e, 2, pb.WT_LEN)
                if not name:
                    continue
                aid = pb.get(e, 1, pb.WT_LEN)
                out[name.decode("ascii", "replace")] = {
                    "asset_id": aid.decode("ascii", "replace")
                    if isinstance(aid, bytes) else name.decode("ascii", "replace"),
                    "version": pb.get(e, 3, pb.WT_VARINT) or 0,
                    "checksum": pb.get(e, 4, pb.WT_32) or 0,
                    "size": pb.get(e, 5, pb.WT_VARINT) or 0,
                    "key": pb.get(e, 6, pb.WT_LEN),
                    "ours": False,
                }
    except (OSError, ValueError):
        pass
    try:
        for name, entry in (bundle_crypto.load_extra(adir) or {}).items():
            if isinstance(entry, dict):
                out[name] = dict(entry, ours=True)
    except (OSError, ValueError):
        pass
    return out


# --------------------------------------------------------------------------
# Opening a bundle
# --------------------------------------------------------------------------
class Bundle:
    """A decrypted bundle, open for editing.

    Hold one of these across a few edits rather than re-decrypting per request:
    it keeps the plain UnityFS bytes in a temp file, which is what UnityPy reads
    lazily, and it releases the handles and the temp file on close(). Use it as a
    context manager -- `with Bundle("pm0025") as b: ...`.
    """

    def __init__(self, name, platform="android"):
        if not is_bundle_name(name):
            raise ValueError(f"not a bundle name: {name!r}")
        self.name = name
        self.platform = platform
        self.reason = available()
        if self.reason:
            raise RuntimeError(self.reason)
        self.base, self.suffix = split_name(name)
        self._dir = tempfile.mkdtemp(prefix="windstock-assets-")
        self.path = os.path.join(self._dir, name + ".bundle")
        self.env = None
        self._open()

    # -- lifecycle ------------------------------------------------------
    def _open(self):
        from asset_tools import bundle_crypto as DB
        enc = _read(self.name, self.platform)
        key = _key_for(self.name, self.platform, DB)
        plain = DB.decrypt(enc, key)
        if not DB.is_bundle(plain):
            raise ValueError(f"{self.name} does not decrypt to a UnityFS bundle")
        with open(self.path, "wb") as fh:
            fh.write(plain)
        self.env = U.load(self.path)
        self.bundle = U.bundle_of(self.env)
        if self.bundle is None:
            self.close()
            raise ValueError(f"{self.name}: no UnityFS container inside")

    def close(self):
        if self.env is not None:
            U.close_env(self.env)
            self.env = None
        if getattr(self, "_dir", None) and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- contents -------------------------------------------------------
    def objects(self):
        """[{path_id, type, name}] for everything in the bundle."""
        out = []
        for o in self.env.objects:
            row = {"path_id": o.path_id, "type": o.type.name}
            if row["type"] in ("Texture2D", "Material", "Mesh", "GameObject",
                               "Animator", "AudioClip", "AssetBundle"):
                try:
                    row["name"] = getattr(o.read(), "m_Name", "") or ""
                except Exception:
                    row["name"] = ""
            else:
                row["name"] = ""
            out.append(row)
        return out

    def counts(self):
        c = {}
        for o in self.env.objects:
            c[o.type.name] = c.get(o.type.name, 0) + 1
        return c

    def textures(self):
        """[{path_id, name, width, height, format, format_name, reencode}].

        `reencode` is the one that matters: UnityPy DECODES every format this
        client ships but can only RE-ENCODE the uncompressed ones, so anything
        false here has to be written back as RGBA32. Doing that silently is what
        makes a build either much bigger than expected or visibly wrong.
        """
        out = []
        for o in self.env.objects:
            if o.type.name != "Texture2D":
                continue
            try:
                d = o.read()
            except Exception as e:
                out.append({"path_id": o.path_id, "name": f"<unreadable: {e}>",
                            "width": 0, "height": 0, "format": -1,
                            "format_name": "unreadable", "reencode": False,
                            "broken": True})
                continue
            fmt = int(getattr(d, "m_TextureFormat", -1))
            out.append({
                "path_id": o.path_id,
                "name": getattr(d, "m_Name", "") or "",
                "width": int(getattr(d, "m_Width", 0)),
                "height": int(getattr(d, "m_Height", 0)),
                "format": fmt,
                "format_name": U.format_name(fmt),
                "reencode": fmt not in U.UNREENCODABLE,
            })
        out.sort(key=lambda r: (r["name"] == "", r["name"]))
        return out

    def texture_png(self, path_id):
        """PNG bytes for one texture, whatever format it is stored in."""
        o = self._object(path_id, "Texture2D")
        img = o.read().image
        if img is None:
            raise ValueError("UnityPy decoded no image for that texture")
        img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def texture_size(self, path_id):
        o = self._object(path_id, "Texture2D")
        d = o.read()
        return int(getattr(d, "m_Height", 0)), int(getattr(d, "m_Width", 0))

    def set_texture(self, path_id, png_or_pil):
        """Replace a texture's pixels. Always written as RGBA32 (format 4).

        The original ETC2/PVRTC bytes are dropped rather than re-encoded: UnityPy
        has no encoder for them, and leaving the old compressed payload behind
        with a new format id is how you get a bundle that loads and then shows
        garbage. RGBA32 is bigger -- a 512x512 body sheet goes from ~170 KB to
        ~1 MB -- which is the trade the shiny builds already make.
        """
        from PIL import Image
        o = self._object(path_id, "Texture2D")
        d = o.read()
        img = (png_or_pil if isinstance(png_or_pil, Image.Image)
               else Image.open(io.BytesIO(png_or_pil)))
        img = img.convert("RGBA")
        old = (int(getattr(d, "m_Width", 0)), int(getattr(d, "m_Height", 0)))
        if (img.width, img.height) != old:
            raise ValueError(
                f"texture is {old[0]}x{old[1]}, the replacement is "
                f"{img.width}x{img.height} -- a Unity texture cannot change size "
                f"without every material's UVs going with it")
        d.m_TextureFormat = U.RGBA32
        d.image = img
        d.save()
        return {"path_id": o.path_id, "width": img.width, "height": img.height,
                "format": U.RGBA32}

    def materials(self):
        """Every Material, with the texture path_ids its _MainTex points at.

        The property list lives on the READER's typetree, not on the parsed
        Material -- Material objects have no read_typetree, and asking for one
        turns every row into an error string that still looks like data.
        """
        out = []
        for o in self.env.objects:
            if o.type.name != "Material":
                continue
            try:
                d = o.read()
                t = o.read_typetree()
            except Exception as e:
                out.append({"path_id": o.path_id, "name": "<unreadable: %s>" % e,
                            "textures": [], "shader": "?"})
                continue
            tints = []
            saved = t.get("m_SavedProperties") or {}
            for entry in saved.get("m_TexEnvs") or []:
                env_ = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else entry
                if isinstance(env_, dict) and env_.get("m_Texture"):
                    pid = env_["m_Texture"].get("m_PathID") or 0
                    if pid:
                        tints.append(pid)
            out.append({"path_id": o.path_id,
                        "name": getattr(d, "m_Name", "") or "",
                        "shader": str((t.get("m_Shader") or {}).get("m_PathID", "")),
                        "textures": tints})
        return out

    def _object(self, path_id, want_type=None):
        for o in self.env.objects:
            if o.path_id == path_id and (want_type is None
                                         or o.type.name == want_type):
                return o
        raise KeyError(f"no {want_type or 'object'} with path_id {path_id}")

    def prefab_keys(self):
        """The AssetBundle container aliases -- what the client actually asks
        for. A variant has to carry its own so it can sit beside the original."""
        out = []
        for o in self.env.objects:
            if o.type.name != "AssetBundle":
                continue
            try:
                t = o.read_typetree()
            except Exception:
                continue
            for k in t.get("m_Container", []) or []:
                if isinstance(k, (list, tuple)) and k:
                    out.append(str(k[0]))
        return out

    def rename(self, new_name):
        """Point the AssetBundle and its container alias at a new bundle name.

        Both have to move together: the client instantiates by CONTAINER KEY, so a
        bundle whose AssetBundle still says `pm0025_rig` loads fine and then
        renders the model you were not looking at -- or nothing, if the original
        is already cached under that key.
        """
        if not is_bundle_name(new_name):
            raise ValueError(f"not a bundle name: {new_name!r}")
        old = self.name
        changed = 0
        for o in self.env.objects:
            if o.type.name != "AssetBundle":
                continue
            t = o.read_typetree()
            t["m_Name"] = new_name
            if "m_AssetBundleName" in t:
                t["m_AssetBundleName"] = new_name
            cont = []
            for entry in t.get("m_Container", []) or []:
                if isinstance(entry, (list, tuple)) and entry and old in str(entry[0]):
                    cont.append([str(entry[0]).replace(old, new_name, 1),
                                 entry[1] if len(entry) > 1 else None])
                    changed += 1
                else:
                    cont.append(entry)
            t["m_Container"] = cont
            o.save_typetree(t)
        if not changed:
            raise ValueError(
                f"{old}: no container key mentioned {old}, so the client would "
                f"never ask for this bundle under a new name")
        # the CAB is named after the bundle; leaving it alone is legal but makes
        # two bundles with different contents share a name in the container
        cab = "CAB-" + hashlib.md5(new_name.encode()).hexdigest()
        files = self.bundle.files
        for key in list(files):
            if str(key).startswith("CAB-"):
                f = files.pop(key)
                files[cab] = f
                try:
                    f.name = cab
                except Exception:
                    pass
                break
        return new_name


def _key_for(name, platform, DB):
    """The 16-byte key a bundle is encrypted with.

    A bundle we built carries its key in extra_digest.json, as hex; anything
    else comes from the genuine asset_digest, as raw bytes. Looking in that
    order is what lets the editor open its own output again.
    """
    e = manifest_entry(name, platform)
    raw = (e or {}).get("key")
    if isinstance(raw, str):
        try:
            raw = bytes.fromhex(raw)
        except ValueError:
            raw = None
    if isinstance(raw, bytes) and len(raw) == 16:
        return raw
    if isinstance(raw, str) and len(raw) == 32:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return DB.digest_key(assets_dir(platform), name)


def manifest_entry(name, platform="android"):
    """The digest record for a bundle: {version, checksum, size, key, ours}.

    Ours win over the genuine digest, because a rebuilt variant supersedes the
    entry before it. Public because the CLI's `verify` and the editor's
    Properties panel need the key, and neither should re-implement this.
    """
    from asset_tools import bundle_crypto
    e = _manifest_index(platform).get(name)
    if e is None:
        return None
    raw = e.get("key")
    if isinstance(raw, (bytes, bytearray)):
        e = dict(e, key=bytes(raw).hex())
    return e


def key_for(name, platform="android"):
    """Public wrapper around the internal key lookup (see _key_for)."""
    from asset_tools import bundle_crypto as DB
    return _key_for(name, platform, DB)


# --------------------------------------------------------------------------
# Recolouring
# --------------------------------------------------------------------------
def fur_stats(img):
    """Mean hue/saturation/value of the fur-coloured pixels, for measuring a
    shift from a normal/shiny icon pair instead of guessing one."""
    hs, ss, vs = [], [], []
    for r, g, b, a in img.convert("RGBA").getdata():
        if a < 200:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if 0.08 < h < 0.20 and s > 0.35 and v > 0.55:
            hs.append(h); ss.append(s); vs.append(v)
    if not hs:
        raise ValueError("no fur-coloured pixels found")
    n = len(hs)
    return sum(hs) / n, sum(ss) / n, sum(vs) / n


def recolour(img, dh=0.0, ks=1.0, kv=1.0, tips=True, tint=None, tint_amount=0.0):
    """Recolour a texture in place-safe fashion and return a new image.

    Two modes, because "shiny" and "icy" are not the same kind of change:

    * fur mode (`dh`/`ks`/`kv`, the shiny recipe): only pixels in the fur hue
      band move. Everything else -- eyes, markings, the outline -- is left
      exactly as it was, which is why a shiny still looks like the Pokemon it is.
      `tips` additionally lifts near-black pixels to grey, because a body sheet's
      ear and tail tips are near-black and read as soot on a bright shiny.

    * tint mode (`tint` = (r, g, b), `tint_amount`): the hue of every
      sufficiently saturated pixel is pulled toward the tint while saturation and
      value are scaled. Use it for icy/frosty/golden, where recolouring only
      the fur would leave a yellow tail on a blue Pokemon.
    """
    img = img.convert("RGBA")
    src = img.load()
    out = img.copy()
    dst = out.load()
    tr = tg = tb = None
    if tint is not None:
        tr, tg, tb = colorsys.rgb_to_hsv(*(c / 255 for c in tint))
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = src[x, y]
            if a == 0:
                continue                       # keep fully hidden pixels as they are
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if tint is not None and s > 0.08:
                # shortest way round the wheel, so pink->yellow does not go
                # through green
                d = (tr - h + 0.5) % 1.0 - 0.5
                h = (h + d * tint_amount) % 1.0
                s = min(1.0, s * ks)
                v = min(1.0, v * kv)
                r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
            elif 0.06 < h < 0.22 and s > 0.25:                 # fur
                h = (h + dh) % 1.0
                s = min(1.0, s * ks)
                v = min(1.0, v * kv)
                r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
            elif tips and v < 0.18 and tint is None:          # black tips -> grey
                r = g = b = 78
            dst[x, y] = (r, g, b, a)
    return out


def recolour_hidden_only(img, dh=0.0, ks=1.0, kv=1.0):
    """Recolour ONLY the fully transparent pixels of a face sheet.

    This one is not a style choice, it is a bug fix. The face material is drawn
    opaque, so the face texture's "transparent" pixels show their hidden RGB --
    normal yellow -- as a pale patch around the eyes. Recolouring the opaque
    pixels as well would repaint the eyes themselves. An early shiny build had
    exactly that rim and it is the first thing anyone notices.
    """
    img = img.convert("RGBA")
    src = img.load()
    out = img.copy()
    dst = out.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = src[x, y]
            if a != 0:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if 0.06 < h < 0.22 and s > 0.25:
                r, g, b = (int(c * 255) for c in
                           colorsys.hsv_to_rgb((h + dh) % 1.0,
                                               min(1.0, s * ks),
                                               min(1.0, v * kv)))
                dst[x, y] = (r, g, b, a)
    return out


#: What each preset does. `tint` is None for the fur-mode presets.
PRESETS = {
    "shiny":  {"label": "Shiny",  "dh": -0.0094, "ks": 1.38, "kv": 0.98,
               "tint": None, "tint_amount": 0.0, "tips": True},
    "icy":    {"label": "Icy",    "dh": 0.0, "ks": 0.95, "kv": 1.06,
               "tint": (110, 190, 255), "tint_amount": 0.62, "tips": False},
    "frosty": {"label": "Frosty", "dh": 0.0, "ks": 0.35, "kv": 1.22,
               "tint": (205, 235, 255), "tint_amount": 0.55, "tips": False},
    "golden": {"label": "Golden", "dh": 0.0, "ks": 1.15, "kv": 1.08,
               "tint": (255, 205, 60), "tint_amount": 0.60, "tips": False},
    "midnight": {"label": "Midnight", "dh": 0.0, "ks": 0.80, "kv": 0.55,
                 "tint": (90, 110, 220), "tint_amount": 0.70, "tips": False},
    "normal": {"label": "Normal", "dh": 0.0, "ks": 1.0, "kv": 1.0,
               "tint": None, "tint_amount": 0.0, "tips": False},
}

#: The only keys a recolour spec may carry.
SPEC_KEYS = ("label", "dh", "ks", "kv", "tint", "tint_amount", "tips")


def recolour_spec(preset="shiny", custom=None):
    """Resolve `preset` into a spec dict, layering `custom` over it.

    Lets a caller with its own controls (the editor's sliders) pass values
    through without inventing a preset name per combination. Bad preset or
    key names raise ValueError rather than silently building the wrong
    colours.
    """
    spec = PRESETS.get(preset)
    if spec is None:
        raise ValueError(f"unknown preset {preset!r}")
    out = dict(spec)
    for k, v in (custom or {}).items():
        if k not in SPEC_KEYS:
            raise ValueError(f"unknown recolour spec key {k!r}")
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Building a variant
# --------------------------------------------------------------------------
def build_variant(base, suffix, preset="shiny", platform="android",
                  textures=None, override=None, replace=False,
                  face_texture=None, register=True, progress=None, custom=None):
    """Build `base` + `_suffix` and register it. Returns a result dict.

    `textures` restricts the recolour to those path_ids (default: every texture
    the body/face naming convention finds). `override` is a list of
    {path_id, png} replacements applied instead of a preset, for when you have
    drawn the pixels you want.

    `custom` is a {dh, ks, kv, tint, tint_amount, tips} dict layered over
    `preset`, so a caller with its own sliders (the editor) does not have to
    invent a preset name to get its values through.

    `replace=True` overwrites the base bundle itself instead of writing a
    variant. It is refused unless assets.allow_replace is on in settings.json,
    and it backs the original up to data/asset_backups/ first.
    """
    reason = available()
    if reason:
        return {"ok": False, "error": reason}
    if not config.allow_replace() and replace:
        return {"ok": False, "error":
                 "replacing a normal bundle is switched off. Turn on "
                 "assets.allow_replace in settings.json (or the editor's "
                 "Settings panel) if you really want to overwrite it -- the "
                 "shipped bundle is backed up to asset_backups/ first, and the "
                 "replacement keeps the bundle's asset id so clients still ask "
                 "for it."}

    base_name = split_name(base)[0] if is_bundle_name(base) else base
    out_name = base_name if replace else f"{base_name}_{suffix}"
    if not is_bundle_name(out_name):
        return {"ok": False, "error": f"bad output bundle name {out_name!r}"}
    if not replace and os.path.isfile(bundle_path(out_name, platform)):
        return {"ok": False, "error":
                f"{out_name} already exists. Pick another suffix, or delete it "
                f"first -- nothing here overwrites a build on its own."}

    say = progress or (lambda *a, **k: None)
    with _lock:
        return _build(base_name, out_name, preset, platform, textures, override,
                      register, say, face_texture, replace, custom)


def _build(base, out_name, preset, platform, textures, override, register, say,
           face_texture, replace, custom=None):
    from asset_tools import bundle_crypto as DB
    adir = assets_dir(platform)
    say("opening", base)
    with Bundle(base, platform) as b:
        rows = b.textures()
        if not rows:
            return {"ok": False, "error": f"{base} has no textures"}

        edited = []
        if preset is not None or custom:
            try:
                spec = recolour_spec(preset or "shiny", custom)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            want = _target_textures(base, rows, textures, face_texture)
            if not want:
                return {"ok": False, "error":
                        f"{base}: no body or face texture found, so there is "
                        f"nothing to recolour. Pass explicit texture ids."}
            say("recolouring", ", ".join(t["name"] for t in want))
            for t in want:
                o = b._object(t["path_id"], "Texture2D")
                img = o.read().image.convert("RGBA")
                if t["face"]:
                    img = recolour_hidden_only(img, spec["dh"], spec["ks"],
                                               spec["kv"])
                else:
                    img = recolour(img, spec["dh"], spec["ks"], spec["kv"],
                                   tips=spec["tips"], tint=spec["tint"],
                                   tint_amount=spec["tint_amount"])
                b.set_texture(t["path_id"], img)
                edited.append(t["name"] or f"#{t['path_id']}")

        # Overrides land on top of any recolour, so painted pixels win.
        for item in override or []:
            row = _match_texture(rows, item)
            if row is None:
                return {"ok": False, "error":
                        f"no texture matching {item.get('name') or item!r}; "
                        f"the bundle has " + ", ".join(
                            f"{t['path_id']}:{t['name']}" for t in rows)}
            pid = row["path_id"]
            b.set_texture(pid, item["png"])
            name = row["name"] or f"#{pid}"
            if name not in edited:
                edited.append(name)

        if out_name != base:
            say("renaming", base, "->", out_name)
            b.rename(out_name)
        packed = b.bundle.save(packer="lz4")     # source still intact
        # the per-object script indices we are about to write, for the check
        # below: this must be read BEFORE the context closes and releases the env
        source_sf = None
        for sf in U.serialized_files(b.env):
            if getattr(sf, U._SIDETABLE, None):
                source_sf = sf
                break

    # the guard's own check, on the object table we are about to ship
    bad = _script_index_sane(packed, source_sf)
    if bad:
        return {"ok": False, "error":
                "refusing to write: " + bad + " -- this is the UnityPy "
                "script_type_index corruption, and a bundle saved this way "
                "shows a grey screen on the phone"}

    say("packing + encrypting")
    key = os.urandom(16)
    enc = DB.encrypt(packed, key)
    problems = DB.verify_encrypted(enc, key, None, len(enc))
    if problems:
        return {"ok": False, "error": "the bundle we just built does not "
                                      "verify: " + "; ".join(problems)}
    round_trip = DB.decrypt(enc, key)
    if round_trip != packed:
        return {"ok": False, "error": "encrypt/decrypt did not round-trip"}

    dest = os.path.join(adir, out_name)
    if replace:
        _backup(out_name, platform, say)
    say("writing", out_name, f"({len(enc)} bytes)")
    U._write_atomic(dest, enc, tag="build")

    # A bundle with no digest entry is not servable, so registration is not
    # optional in practice. `register=False` is kept for a caller that wants the
    # file on disk to review before it is listed; it still gets an entry, and the
    # manifest can be edited or removed before the next server read.
    manifest = _load_extra(adir)
    now = int(time.time() * 1_000_000)
    # A REPLACED base bundle must keep the asset_id the client already asks for.
    # The client resolves a Pokemon's model from its item template's asset_id and
    # then asks the digest for that id; minting a new one here would leave the
    # request pointing at an id nothing advertises, and the model would 404 no
    # matter how good the texture is. So in replace mode reuse the existing id
    # (and the client re-downloads because the checksum/size changed). A new
    # VARIANT has no such constraint -- it is a different asset on purpose, and
    # something else has to point the game at it.
    asset_id = None
    if replace:
        try:
            prev = _manifest_index(platform).get(out_name) or {}
            prev_id = prev.get("asset_id")
            if isinstance(prev_id, bytes):
                prev_id = prev_id.decode("ascii", "replace")
            asset_id = prev_id or None
        except Exception:
            asset_id = None
    entry = {
        "asset_id": asset_id or f"{uuid.uuid4()}/{now}",
        "bundle_name": out_name,
        "version": now,
        "checksum": DB.crc32c(enc),
        "size": len(enc),
        "key": key.hex(),
    }
    if replace and asset_id:
        say("kept asset_id", asset_id, "so the client still asks for this bundle")
    manifest[out_name] = entry
    _save_extra(adir, manifest)
    say("registered in extra_digest.json")

    # checksum/key are mirrored at the top level as well as inside `entry`:
    # both the CLI and the editor print them straight off the result, and having
    # them only nested made the editor log a checksum of 0 and a key of None for
    # a build that had actually succeeded.
    return {"ok": True, "name": out_name, "size": len(enc),
            "checksum": entry["checksum"], "key": entry["key"],
            "textures": edited, "entry": entry, "replaced": bool(replace),
            "listed": bool(register)}


def _script_index_sane(packed, source_sf):
    """Reload what we are about to ship and prove no script index moved.

    The shared SerializedType records all read -1 in Python even on a perfect
    file, so there is nothing in the shared table worth comparing. What IS
    trustworthy is the per-object index the guard records while parsing: that is
    read from the object's own two bytes in the object-record table, which is
    exactly the field UnityPy used to flatten on save and leave a MonoBehaviour
    pointing at the wrong script.

    So: capture the source's per-object indices, parse the freshly packed bytes
    back through the same reader, and diff them. Sizes and pixels are not
    compared -- a recolour is supposed to change those -- only the indices.

    Returns "" when clean, else a reason to refuse the write.
    """
    want = dict(getattr(source_sf, U._SIDETABLE, {}) or {})
    if not want:
        return "the source bundle gave us no per-object script indices to check"
    env = None
    tmp = None
    d = tempfile.mkdtemp(prefix="windstock_check_")
    try:
        tmp = os.path.join(d, "packed.bundle")
        with open(tmp, "wb") as fh:
            fh.write(packed)
        env = U.load(tmp)
        got = {}
        for sf in U.serialized_files(env):
            got.update(getattr(sf, U._SIDETABLE, {}) or {})
    except Exception as e:
        return f"the bundle we packed does not load back: {e}"
    finally:
        if env is not None:
            U.close_env(env)
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    if not got:
        return "the bundle we packed has no per-object script indices"
    if got == want:
        return ""
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    moved = [(p, want[p], got[p]) for p in want
             if p in got and want[p] != got[p]]
    bits = []
    if missing:
        bits.append(f"{len(missing)} object(s) vanished (first {missing[0]})")
    if extra:
        bits.append(f"{len(extra)} object(s) appeared (first {extra[0]})")
    if moved:
        bits.append(f"{len(moved)} object(s) changed script index, e.g. "
                    f"path_id {moved[0][0]}: {moved[0][1]} -> {moved[0][2]}")
    return "; ".join(bits)


def _target_textures(base, rows, only, face_texture):
    """Which textures a preset applies to.

    The convention in these bundles is `<name>_00_BodyExample` (the body sheet)
    and `<name>_00_Face` (the eye sheet). Both are recoloured, for different
    reasons: the body is the model, and the face's HIDDEN pixels are what show
    around the eyes. Matching on name rather than on "the biggest texture"
    because the biggest one is not reliably the body -- pm0025's is a shadow.
    """
    face = (face_texture or f"{base}_00_Face")
    out = []
    for r in rows:
        n = r["name"]
        if only is not None and r["path_id"] not in only:
            continue
        is_face = n == face or n.endswith("_Face")
        is_body = (n.endswith("_BodyExample") or "_Body" in n
                   or n.endswith("_body") or n.endswith("Body"))
        if is_face or is_body:
            out.append(dict(r, face=is_face))
    return out


def _match_texture(rows, item):
    """Find the texture an override item means, or None.

    Overrides are {path_id, png} or {name, png}. A name is matched exactly
    first, then case-insensitively, then as a substring, so the CLI's
    `--replace-texture Body=body.png` does the obvious thing without the
    caller needing to know the full texture path.
    """
    if "path_id" in item:
        pid = int(item["path_id"])
        for r in rows:
            if r["path_id"] == pid:
                return r
        return None
    needle = str(item.get("name") or "").strip()
    if not needle:
        return None
    for r in rows:
        if r["name"] == needle:
            return r
    low = needle.lower()
    for r in rows:
        if (r["name"] or "").lower() == low:
            return r
    hits = [r for r in rows if low in (r["name"] or "").lower()]
    return hits[0] if len(hits) == 1 else None


def _backup(name, platform, say):
    """Copy a bundle we are about to overwrite into data/asset_backups/."""
    src = bundle_path(name, platform)
    d = config.data_dir("asset_backups", platform)
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"{name}.{int(time.time())}.bak")
    with open(src, "rb") as a, open(dest, "wb") as b:
        b.write(a.read())
    say("backed up", os.path.basename(dest))
    return dest


def _load_extra(adir):
    from asset_tools import bundle_crypto
    try:
        raw = bundle_crypto.load_extra(adir)
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_extra(adir, manifest):
    from asset_tools import bundle_crypto
    bundle_crypto.save_extra(adir, manifest)


# --------------------------------------------------------------------------
# 3D preview: the real rig, as a binary glTF 2.0
# --------------------------------------------------------------------------
def _hierarchy(b):
    """GameObject name and Transform typetree, keyed by path_id.

    A pm bundle has ~75 of each and they are the only way to get from a
    SkinnedMeshRenderer's m_Bones to a name and a local transform, which is
    what a preview needs to show an actual rigged model rather than a T-pose
    husk.
    """
    names, transforms = {}, {}
    for o in b.env.objects:
        kind = o.type.name
        if kind == "GameObject":
            t = o.read_typetree()
            names[o.path_id] = t.get("m_Name") or ""
        elif kind == "Transform":
            transforms[o.path_id] = o.read_typetree()
    return names, transforms


def _vec(t, key, default=(0.0, 0.0, 0.0)):
    v = t.get(key)
    if not isinstance(v, dict):
        return default
    return (float(v.get("x", 0.0)), float(v.get("y", 0.0)), float(v.get("z", 0.0)))


def _quat(t, key, default=(0.0, 0.0, 0.0, 1.0)):
    v = t.get(key)
    if not isinstance(v, dict):
        return default
    return (float(v.get("x", 0.0)), float(v.get("y", 0.0)),
            float(v.get("z", 0.0)), float(v.get("w", 1.0)))


def _renderers(b):
    """SkinnedMeshRenderer typetrees, biggest mesh first."""
    rows = []
    for o in b.env.objects:
        if o.type.name != "SkinnedMeshRenderer":
            continue
        try:
            rows.append(o.read_typetree())
        except Exception:
            continue
    return rows


def _material_texture(materials, mat_pid):

    """The Texture2D a material's _MainTex points at, or None.

    Unity stores this as Material.m_SavedProperties.m_TexEnvs, a list of
    (name, value) pairs, where the value's m_Texture is a PPtr. Several of
    these materials use a shader with no _MainTex at all (the ring and streak
    effect sheets), so this legitimately returns None for them.
    """
    mat = materials.get(mat_pid)
    if not mat:
        return None
    saved = mat.get("m_SavedProperties")
    if not isinstance(saved, dict):
        return None
    for pair in saved.get("m_TexEnvs") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        name, value = pair
        name = (name or {}).get("name")
        if name not in ("_MainTex", "_BaseMap", "_BaseColorMap"):
            continue
        ptr = (value or {}).get("m_Texture") or {}
        pid = int(ptr.get("m_PathID") or 0)
        if pid:
            return pid
    return None


def _bind_matrix(m):
    """A Unity float[16] bind matrix as a flat column-major list.

    Unity serializes these row-major while glTF reads them column-major, so the
    transpose is deliberate. Skipping it is the classic "model explodes into a
    starburst" bug.
    """
    out = []
    for r in range(4):
        for c in range(4):
            key = f"e{r}{c}"
            out.append(float(m.get(key, 0.0)) if isinstance(m, dict) else 0.0)
    return [out[r * 4 + c] for c in range(4) for r in range(4)]


def export_gltf(name, platform="android", with_textures=True):
    """A .glb of the real rigged model, textured -- what the preview pane loads.

    Not a stand-in. The geometry comes from UnityPy's own vertex-stream reader
    on the same uncompressed meshes the client uses; the skin comes from the
    SkinnedMeshRenderer's m_Bones (which is the exact index space the mesh's
    JOINTS_0 values refer to -- no name hashing needed, and none of the guessing
    that goes with it); the bone hierarchy comes from the Transforms'
    m_Father links; and the inverse bind matrices are the mesh's own m_BindPose.

    Everything is emitted in Unity's coordinate frame, with the original winding
    and doubleSided materials. Converting handedness would be more "correct" and
    less useful: three.js renders either one, and a mirrored model makes the
    text on the face sheet read backwards, which is worse than a flipped normal
    nobody looks at.

    Only works on uncompressed meshes, which is what these bundles ship. A
    compressed one raises ValueError with the reason instead of exporting a husk.
    """
    with Bundle(name, platform) as b:
        names, transforms = _hierarchy(b)
        materials = {o.path_id: o.read_typetree()
                     for o in b.env.objects if o.type.name == "Material"}

        meshes = {}
        for o in b.env.objects:
            if o.type.name != "Mesh":
                continue
            try:
                d = o.read()
            except Exception:
                continue
            # the typetree lives on the READER, not on the parsed Mesh; m_BindPose
            # and m_BoneNameHashes are only there, so reading d directly silently
            # costs you the entire skin
            try:
                t = o.read_typetree()
            except Exception:
                t = {}
            if int(t.get("m_MeshCompression", 0)):
                raise ValueError(
                    f"{d.m_Name} is a COMPRESSED mesh (m_MeshCompression "
                    f"{t.get('m_MeshCompression')}). These bundles ship "
                    f"uncompressed ones; if you hit this, the bundle is from a "
                    f"different game version.")
            h = U.mesh_handler(d)
            if h is None or not h.m_VertexCount:
                continue
            meshes[o.path_id] = (d.m_Name, h, t)

        renderers = [r for r in _renderers(b)
                     if int((r.get("m_Mesh") or {}).get("m_PathID") or 0) in meshes]
        if not renderers:
            raise ValueError(f"{name} has no skinned mesh to preview")

        # the body is the one with the most vertices; the rest of a pm bundle is
        # shadows, effect rings and the attack animation's mesh
        def vcount(r):
            mname, h, _ = meshes[int(r["m_Mesh"]["m_PathID"])]
            return h.m_VertexCount

        renderers.sort(key=vcount, reverse=True)

        gltf = _Gltf()
        gltf.asset(generator="windstock assetlib", version="2.0")
        root = gltf.node(None, f"{name} (bind pose)")

        # textures next, so the materials below can point at them
        images = {}
        bone_cache = {}
        if with_textures:
            for row in b.textures():
                if not row["name"] or row.get("broken"):
                    continue
                try:
                    png = b.texture_png(row["path_id"])
                except Exception:
                    continue
                images[row["path_id"]] = gltf.image(png, row["name"])

        for r in renderers:
            mesh_pid = int(r["m_Mesh"]["m_PathID"])
            mname, h, t = meshes[mesh_pid]
            mat_pid = int((r.get("m_Materials") or [{}])[0].get("m_PathID") or 0)
            tex_pid = _material_texture(materials, mat_pid)
            mat = gltf.material(mname, images.get(tex_pid))
            prim = _primitive(gltf, h, mat)
            if prim is None:
                continue
            mesh = gltf.mesh(mname, [prim])
            bones = [int(p.get("m_PathID") or 0)
                     for p in (r.get("m_Bones") or [])]
            if bones and t.get("m_BindPose") and len(bones) == len(t["m_BindPose"]):
                joints = _joints(gltf, root, bones, transforms, names, bone_cache)
                skin = gltf.skin(joints, _bind_acc(gltf, t["m_BindPose"]))
                gltf.node(root, mname, mesh=mesh, skin=skin)
            else:
                gltf.node(root, mname, mesh=mesh)


        return gltf.finish()


def glb_stats(data):
    """{meshes, skins, images, nodes, vertices} counted from a finished .glb.

    Read back out of the JSON chunk rather than threaded through the exporter:
    the point is to report what was actually serialised, and a count taken before
    the buffer is packed is a count of an intention. Also what the editor's
    status line shows, so both agree by construction.
    """
    import json
    import struct
    out = {"meshes": 0, "skins": 0, "images": 0, "nodes": 0, "vertices": 0}
    try:
        if data[:4] != b"glTF":
            return out
        _, total = struct.unpack_from("<II", data, 8)
        off = 12
        js = None
        while off < min(len(data), total):
            clen, ctype = struct.unpack_from("<II", data, off)
            body = data[off + 8:off + 8 + clen]
            if ctype == 0x4E4F534A:
                js = json.loads(body.decode("utf-8"))
                break
            off += 8 + clen + (-clen % 4)
        if not js:
            return out
        for k in ("meshes", "skins", "images", "nodes"):
            out[k] = len(js.get(k) or [])
        for m in js.get("meshes") or []:
            for prim in m.get("primitives") or []:
                ai = (prim.get("attributes") or {}).get("POSITION")
                if ai is None:
                    continue
                acc = (js.get("accessors") or [])[ai]
                n = acc.get("count") or 0
                out["vertices"] += n
    except Exception:
        pass
    return out


def _joints(gltf, root, bones, transforms, names, cache):
    """One glTF node per bone, parented by Unity's Transform hierarchy.

    Node order must equal the mesh's JOINTS_0 index space, which is the order of
    m_Bones -- so the list is built in bone order and only the parent links are
    filled in afterwards. Getting that order wrong produces a model that loads
    cleanly and animates inside itself.

    `cache` is shared across renderers on purpose: the two skinned meshes of a
    pm bundle (body and sippo skin) are driven by the same 43 transforms, and
    duplicating those nodes would give the preview two independent skeletons
    instead of one body with two pieces of geometry on it.
    """
    node_of = {}
    for pid in bones:
        if pid in cache:
            node_of[pid] = cache[pid]
            continue
        t = transforms.get(pid)
        label = names.get((t or {}).get("m_GameObject", {}).get("m_PathID"), "")
        idx = gltf.node(None, label or f"bone{pid}")
        if t:
            n = gltf.g["nodes"][idx]          # node() hands back the index
            n["translation"] = list(_vec(t, "m_LocalPosition"))
            n["rotation"] = list(_quat(t, "m_LocalRotation"))
            n["scale"] = list(_vec(t, "m_LocalScale", (1.0, 1.0, 1.0)))
        node_of[pid] = idx
        cache[pid] = idx

    for pid in bones:
        t = transforms.get(pid)
        if not t:
            continue
        father = int((t.get("m_Father") or {}).get("m_PathID") or 0)
        if father in node_of:
            gltf.attach(node_of[pid], node_of[father])
        else:
            # the topmost bones hang off the scene root
            gltf.attach(node_of[pid], root)
    return [node_of[pid] for pid in bones]



def _bind_acc(gltf, bind_pose):
    flat = []
    for m in bind_pose:
        flat += _bind_matrix(m)
    return gltf.accessor(flat, 5126, "MAT4")


def _primitive(gltf, h, material):
    """One glTF primitive for one mesh, or None if it has no geometry."""
    if not h.m_Vertices or not h.m_VertexCount:
        return None
    pos = []
    for v in h.m_Vertices:
        pos += [v[0], v[1], v[2]]
    nrm = []
    for n in (h.m_Normals or []):
        nrm += [n[0], n[1], n[2]]
    uv = []
    for t in (h.m_UV0 or []):
        uv += [t[0], t[1]]                     # same frame as the positions
    joints, weights = [], []
    for idx, wgt in zip(h.m_BoneIndices or [], h.m_BoneWeights or []):
        joints += [int(idx[0]), int(idx[1]), int(idx[2]), int(idx[3])]
        weights += [float(wgt[0]), float(wgt[1]), float(wgt[2]), float(wgt[3])]

    a_pos = gltf.accessor(pos, 5126, "VEC3", target=34962, bounds=True)
    a_nrm = gltf.accessor(nrm, 5126, "VEC3", target=34962) if nrm else None
    a_uv = gltf.accessor(uv, 5126, "VEC2", target=34962) if uv else None
    a_j = gltf.accessor(joints, 5121, "VEC4", target=34962) if joints else None
    a_w = gltf.accessor(weights, 5126, "VEC4", target=34962) if weights else None

    # get_triangles yields one list per submesh; concatenate rather than taking
    # the first, or a multi-submesh body exports with holes in it
    tris = []
    for group in h.get_triangles():
        tris += list(group)
    if not tris:
        return None
    wide = len(set(tris)) > 65535
    a_idx = gltf.accessor(tris, 5125 if wide else 5123, "SCALAR", target=34963)

    attrs = {"POSITION": a_pos}
    if a_nrm is not None:
        attrs["NORMAL"] = a_nrm
    if a_uv is not None:
        attrs["TEXCOORD_0"] = a_uv
    if a_j is not None:
        attrs["JOINTS_0"] = a_j
    if a_w is not None:
        attrs["WEIGHTS_0"] = a_w
    return {"attributes": attrs, "indices": a_idx, "material": material,
            "mode": 4}


class _Gltf:
    """Just enough glTF 2.0 to emit one binary .glb."""

    def __init__(self):
        self.g = {
            "asset": {}, "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [], "meshes": [], "materials": [], "accessors": [],
            "bufferViews": [], "buffers": [], "textures": [], "images": [],
            "samplers": [{"magFilter": 9729, "minFilter": 9987,
                          "wrapS": 10497, "wrapT": 10497}],
            "skins": [],
        }
        self.bin = bytearray()
        self._root = None

    # -- json helpers ---------------------------------------------------
    def asset(self, generator, version):
        self.g["asset"] = {"version": version, "generator": generator}

    def node(self, parent, name, **kw):
        n = {"name": name}
        n.update(kw)
        self.g["nodes"].append(n)
        idx = len(self.g["nodes"]) - 1
        if parent is None:
            # only the FIRST orphan is the scene root; bone nodes are also
            # created parentless and then attached, and they must not steal it
            if self._root is None:
                self._root = idx
        else:
            self.g["nodes"][parent].setdefault("children", []).append(idx)
        return idx

    def attach(self, child, parent):
        """Add an existing node to a parent's children."""
        kids = self.g["nodes"][parent].setdefault("children", [])
        if child not in kids:
            kids.append(child)

    def image(self, png, name):
        self.g["images"].append({"name": name, "mimeType": "image/png",
                                 "bufferView": self._view(png)})
        i = len(self.g["images"]) - 1
        self.g["textures"].append({"sampler": 0, "source": i})
        return len(self.g["textures"]) - 1

    def material(self, name, texture=None, base=(1, 1, 1, 1)):
        pbr = {"baseColorFactor": list(base), "metallicFactor": 0.0,
               "roughnessFactor": 1.0}
        if texture is not None:
            pbr["baseColorTexture"] = {"index": texture}
        self.g["materials"].append({"name": name, "pbrMetallicRoughness": pbr,
                                    "doubleSided": True})
        return len(self.g["materials"]) - 1

    def skin(self, joints, inverse_bind):
        self.g["skins"].append({"joints": joints,
                                "inverseBindMatrices": inverse_bind})
        return len(self.g["skins"]) - 1

    def mesh(self, name, primitives):
        self.g["meshes"].append({"name": name, "primitives": primitives})
        return len(self.g["meshes"]) - 1

    def _view(self, data):
        # 4-byte alignment: every accessor offset into a bufferView must be
        # aligned to its component size, and a float32 one wants 4
        pad = (-len(data)) % 4
        off = len(self.bin)
        self.bin += data + b"\x00" * pad
        self.g["bufferViews"].append({"buffer": 0, "byteOffset": off,
                                      "byteLength": len(data)})
        return len(self.g["bufferViews"]) - 1

    def accessor(self, values, ctype, atype, target=None, normalized=False,
                 bounds=False):
        """values: a flat list already in the right component order.

        `bounds` adds min/max, which the spec makes optional but three.js reads
        to build its bounding sphere -- without it the preview camera frames the
        model as if it were a point at the origin.
        """
        data = _pack(ctype, values)
        per = _NTYPE[atype]
        n, rem = divmod(len(values), per)
        if rem:
            raise ValueError("%s accessor got %d values, not a multiple of %d"
                             % (atype, len(values), per))
        acc = {"bufferView": self._view(data), "componentType": _CT[ctype],
               "count": n, "type": atype}
        if bounds and n:
            rows = [values[i * per:(i + 1) * per] for i in range(n)]
            acc["min"] = [min(r[k] for r in rows) for k in range(per)]
            acc["max"] = [max(r[k] for r in rows) for k in range(per)]
        if target is not None:
            acc.setdefault("target", target)
        if normalized:
            acc["normalized"] = True
        self.g["accessors"].append(acc)
        return len(self.g["accessors"]) - 1

    def finish(self):
        if self._root is not None:
            self.g["scenes"][0]["nodes"] = [self._root]
        pad = (-len(self.bin)) % 4
        self.bin += b"\x00" * pad
        self.g["buffers"] = [{"byteLength": len(self.bin)}]
        js = json.dumps(self.g, separators=(",", ":")).encode("utf-8")
        js += b" " * ((-len(js)) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        out = bytearray()
        out += struct.pack("<III", 0x46546C67, 2, total)      # glTF, version, len
        out += struct.pack("<II", len(js), 0x4E4F534A)        # 'JSON'
        out += js
        out += struct.pack("<II", len(self.bin), 0x004E4942)   # 'BIN\0'
        out += self.bin
        return bytes(out)


_CT = {5121: 5121, 5123: 5123, 5125: 5125, 5126: 5126}
#: components per accessor TYPE. This is what "count" means in glTF, and it is
#: a different axis from componentType -- keying this by componentType is how
#: every VEC3 accessor ends up claiming three times as many elements as it has,
#: and the buffer then reads off its own end.
_NTYPE = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_FMT = {5121: "<B", 5123: "<H", 5125: "<I", 5126: "<f"}


def _pack(ctype, values):
    out = bytearray()
    fmt = _FMT[ctype]
    for v in values:
        try:
            out += struct.pack(fmt, v)
        except (struct.error, TypeError, OverflowError):
            out += struct.pack(fmt, 0)
    return bytes(out)
