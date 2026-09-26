"""
patch_badges.py -- add Windstock's custom medals to the game's own data files.

    python3 patch_badges.py <pokemongo.app>             # patch an unpacked app in place
    python3 patch_badges.py <in.ipa> -o <out.ipa>       # or an IPA

Works on the 0.29 and 0.35 iOS clients (Unity 5.3.5f1). Nothing here touches code:
the medal screen is data-driven all the way down, so new medals are new data.

  * HoloBadgeType is just an int on the wire. A type the client has no enum name for
    (38+) still parses, and Enum.ToString() gives "38", so its text keys are "38",
    "38_title", "38_value_format" (BadgeDetailGuiController.Initialize builds them as
    type.ToString().ToLower() + suffix).
  * BadgeService (a MonoBehaviour in sharedassets0.assets) has a serialized
    badgeSprites array of {badgeType, sprites[4]}; GetBadgeSprite(type, rank) looks the
    type up there, else falls back to the generic Poke Ball medal. We append sets for
    the new types, pointing at new Sprites on a new Texture2D (drawn by badge_art.py
    from the stock Pikachu medal frames).
  * The strings live in the TextAsset "general" (resources.assets): base64 of AES-256-CBC
    of a TSV. Key and IV are TextEncryption's two static byte arrays, read out of
    global-metadata.dat (<PrivateImplementationDetails>.$$field-0 / -1).
  * The medal-earned popup also needs the type's BadgeSettings in the game master --
    that side is the server's (protocol.py CUSTOM_BADGES).

Always patches from a pristine copy kept in _pristine/<version>/ (git-ignored), so
running it twice gives the same result instead of stacking.
"""
import argparse
import base64
import copy
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

import unitypy_fix  # noqa: F401  -- must precede UnityPy loads (script indices)
import UnityPy
from Crypto.Cipher import AES
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import badge_art  # noqa: E402

# (type, emblem, title, description, value format). Must agree with the server's
# CUSTOM_BADGES in work/server/protocol.py.
BADGES = [
    (38, "shiny", "Shiny Hunter", "Caught {0} shiny Pokémon.", "N0"),
    (39, "night", "Night Owl", "Caught {0} Pokémon between 7 PM and 6 AM.", "N0"),
    (40, "shinydex", "Shinydex", "Caught {0} different kinds of shiny Pokémon from Kanto.", "N0"),
]

# In-game store items for the Shiny Charm / Shiny Incense (server shop.py, sold in the
# native store on patched clients): (sku stem, icon in work/server/shopicons, title,
# description). ItemSpriteLookup.iapSprites gets "<stem>.1" and "<stem>"; the store label
# is "<stripped sku>_title" in the "items" table.
SHOP_ITEMS = [
    ("shinycharm", "shinycharm.png", "Shiny Charm",
     "A shining charm. Shiny Pokémon appear more often, for good."),
    ("shinyincense", "shinyincense.png", "Shiny Incense",
     "Shiny Pokémon appear far more often while it burns."),
]
# The Item each store listing says it yields (server shop._DISPLAY_ITEM): items the
# 2016 game never uses, renamed here and given the item's icon, so the "you got..."
# popup names it instead of showing item_unknown_name. {stem: (Item, i18n key stem)}
DISPLAY_ITEMS = {"shinycharm": (604, "item_x_miracle"),
                 "shinyincense": (402, "item_incense_spicy")}
SHOP_ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "shopicons")

# Settings -> Help Center / Report an Issue open these (the game reads the addresses from
# the "general" table). The Help Center is the server's own /hc page: on iOS the launcher's
# server listens on 127.0.0.1:8080 and the tweak opens web links in the game, so the server
# keeps running. Android has no tweak and its server isn't on the phone, so there only the
# Report link changes -- its Help Center keeps Niantic's address, which the Android setup
# already redirects to the server's /hc.
WINDSTOCK_SITE = "https://projectwindstock.site.je"
# "Report High-Priority Issue" becomes the Project Windstock site. On iOS the About button
# already opens the tweak's sound pack menu (with "Open About page" in it), so its label
# says so; Android has no tweak, so its About stays "About".
LINKS_IOS = {
    "help_center_link": "http://127.0.0.1:8080/hc",
    "settings_are_you_sure_support_prompt": "Open the Windstock Help Center?",
    "settings_report_issues": "Project Windstock",
    "report_issues_link": WINDSTOCK_SITE,
    "settings_are_you_sure_report_prompt": "Open the Project Windstock website?",
    "about_screen_btn": "Sound Packs & About",
}
LINKS_ANDROID = {
    "settings_report_issues": "Project Windstock",
    "report_issues_link": WINDSTOCK_SITE,
    "settings_are_you_sure_report_prompt":
        "Do you want to leave the game and open the Project Windstock website?",
}

TEXT_KEY = bytes.fromhex("9edcbe74a8b3d222334f5023aa36182fd9ce9f39482b985875c32ffc3fc061e8")
TEXT_IV = bytes.fromhex("03d9f1a7ec5a305a211f078d0900236a")

LEVELS = ("BRONZE", "SILVER", "GOLD")
TEX_NAME = "WindstockBadgeAtlas"
SIZE = badge_art.SIZE
PPU = 50.0


# ------------------------------------------------------------------ helpers
def _file(env, name):
    for k, f in env.files.items():
        if os.path.basename(k) == name:
            return f
    raise SystemExit(f"{name} not loaded")


def _new_object(sf, template, data_dict):
    """A new object of the template's class in serialized file sf, holding data_dict."""
    obj = copy.copy(template)
    obj.path_id = max(sf.objects) + 1
    obj.data = None
    sf.objects[obj.path_id] = obj
    unitypy_fix.inherit(obj, template)
    obj.save_typetree(data_dict)
    return obj


def _write(path, data):
    """UnityPy streams object data from the source file until save() has run, so
    the bytes must exist before the file is opened for writing (which truncates it)."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _full_frame(sprite):
    """A packed sprite's image is only its trimmed textureRect; put it back on the
    full rect so every level has the same 256x256 frame."""
    im = sprite.image
    off = sprite.m_RD.textureRectOffset
    w, h = int(sprite.m_Rect.width), int(sprite.m_Rect.height)
    canvas = Image.new("RGBA", (w, h))
    canvas.paste(im, (int(round(off.x)), int(round(h - off.y - im.height))))
    return canvas.resize((SIZE, SIZE)) if (w, h) != (SIZE, SIZE) else canvas


# ------------------------------------------------------------------ sharedassets0
def patch_incense_material(sa):
    """The Shiny Incense burns blue. Incense smoke is a legacy particle system (emitters
    named emitter_pink): the pink is in its ParticleAnimator colours, and the game's code
    was stripped of the API to change those -- and of Material.set_shader. So the shared
    incense_fx material moves from Mobile/Particles/Alpha Blended (tex * vertex colour) to
    Particles/Alpha Blended (2 * tex * vertex colour * _TintColor) with a neutral 0.5 tint:
    ordinary Incense looks exactly the same, and the tweak can tint just the Shiny Incense's
    renderers blue (Renderer.material -> SetColor("_TintColor"))."""
    shader = None
    for o in sa.objects.values():
        if o.type.name == "Shader" and b'Shader "Particles/Alpha Blended"' in o.get_raw_data():
            shader = o.path_id
    for o in sa.objects.values():
        if o.type.name == "Material" and o.read().m_Name == "incense_fx":
            t = o.read_typetree()
            if shader is None:
                raise SystemExit("Particles/Alpha Blended not in sharedassets0.assets")
            t["m_Shader"] = {"m_FileID": 0, "m_PathID": shader}
            props = t["m_SavedProperties"]
            props["m_Colors"] = [c for c in props["m_Colors"] if c[0]["name"] != "_TintColor"] + [
                ({"name": "_TintColor"}, {"r": 0.5, "g": 0.5, "b": 0.5, "a": 0.5})]
            props["m_Floats"] = [f for f in props["m_Floats"] if f[0]["name"] != "_InvFade"] + [
                ({"name": "_InvFade"}, 1.0)]
            o.save_typetree(t)
            return o.path_id
    raise SystemExit("incense_fx material not found")


MENU_GROW = 1000.0    # canvas units; Blackout 1295 -> 2295 tall covers screens up to ~3.2:1
MENU_STOCK = {"Blackout": 1112.0, "ButtonCloseFullScreen": 2217.0}   # stock m_SizeDelta.y


def patch_menu_blackout(sa):
    """The Poke Ball menu's backdrop (MainMenuGui/MapRadialMenu/Blackout) and its tap-to-close
    area are bottom-pivoted with a fixed height made for 16:9 phones: on a 19.5:9 iPhone the
    top ~17% of the screen stayed uncovered (map and compass showing through). Both grow
    upward only, so the bottom edge and everything on a 16:9 screen look the same."""
    names = {}
    for o in sa.objects.values():
        if o.type.name == "GameObject":
            names[o.path_id] = o.read().m_Name
    rects = {o.path_id: o for o in sa.objects.values() if o.type.name == "RectTransform"}
    done = []
    for pid, o in rects.items():
        t = o.read_typetree()
        name = names.get(t["m_GameObject"]["m_PathID"])
        if name not in MENU_STOCK:
            continue
        parent = rects.get(t["m_Father"]["m_PathID"])
        if parent is None or names.get(parent.read_typetree()["m_GameObject"]["m_PathID"]) != "MapRadialMenu":
            continue
        if t["m_SizeDelta"]["y"] == MENU_STOCK[name]:      # idempotent
            t["m_SizeDelta"]["y"] += MENU_GROW
            o.save_typetree(t)
        done.append(name)
    if sorted(done) != ["Blackout", "ButtonCloseFullScreen"]:
        raise SystemExit(f"MapRadialMenu backdrop not found (got {done})")


def patch_sprites(data_dir):
    env = UnityPy.load(os.path.join(data_dir, "sharedassets0.assets"),
                       os.path.join(data_dir, "globalgamemanagers.assets"))
    sa = _file(env, "sharedassets0.assets")
    gg = _file(env, "globalgamemanagers.assets")

    # which external file index is globalgamemanagers.assets (PPtr m_FileID)
    gg_fid = 1 + [os.path.basename(e.path) for e in sa.externals].index("globalgamemanagers.assets")
    script_ids = {o.path_id for o in gg.objects.values()
                  if o.type.name == "MonoScript" and o.read().m_ClassName == "BadgeService"}
    service = None
    for o in sa.objects.values():
        if o.type.name == "MonoBehaviour":
            raw = o.get_raw_data()
            fid, pid = struct.unpack_from("<iq", raw, 16)
            if fid == gg_fid and pid in script_ids:
                service = o
    if service is None:
        raise SystemExit("BadgeService not found in sharedassets0.assets")

    sprites = {}
    for o in sa.objects.values():
        if o.type.name == "Sprite":
            sprites.setdefault(o.read().m_Name, o)
    locked = sprites["badge_lv0"]
    templates = [sprites[f"Badge_Pikachu_{lv}_01"] for lv in LEVELS]
    frames = [_full_frame(t.read()) for t in templates]

    # one texture: row = badge, column = level; the last row holds the store icons
    # (Unity textures are bottom-up)
    shop_row = len(BADGES)
    tw, th = SIZE * max(len(LEVELS), len(SHOP_ITEMS)), SIZE * (len(BADGES) + 1)
    atlas = Image.new("RGBA", (tw, th))
    cells = {}
    for row, (bt, kind, *_rest) in enumerate(BADGES):
        for col, frame in enumerate(frames):
            medal = badge_art.render_medal(frame, kind)
            atlas.paste(medal, (col * SIZE, th - (row + 1) * SIZE))
            cells[(bt, col)] = (col * SIZE, row * SIZE)
    for col, (stem, icon, *_rest) in enumerate(SHOP_ITEMS):
        im = Image.open(os.path.join(SHOP_ICONS, icon)).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
        atlas.paste(im, (col * SIZE, th - (shop_row + 1) * SIZE))
        cells[(stem, 0)] = (col * SIZE, shop_row * SIZE)

    atlas_tex_obj = sa.objects[templates[0].read().m_RD.texture.path_id]
    tex = atlas_tex_obj.read_typetree()
    tex.update({
        "m_Name": TEX_NAME, "m_Width": tw, "m_Height": th,
        "m_CompleteImageSize": tw * th * 4, "m_TextureFormat": 4, "m_MipCount": 1,
        "m_IsReadable": False, "m_ImageCount": 1,
        "image data": atlas.transpose(Image.FLIP_TOP_BOTTOM).tobytes(),
        "m_StreamData": {"offset": 0, "size": 0, "path": ""},
    })
    tex_obj = _new_object(sa, atlas_tex_obj, tex)

    def make_sprite(template, name, x, y):
        sp = template.read_typetree()
        ppu = float(sp.get("m_PixelsToUnits") or PPU)
        half = SIZE / ppu / 2
        sp["m_Name"] = name
        # not packed: for an unpacked sprite the rect IS where it sits in the texture
        sp["m_Rect"] = {"x": float(x), "y": float(y), "width": float(SIZE), "height": float(SIZE)}
        sp["m_Offset"] = {"x": 0.0, "y": 0.0}
        rd = sp["m_RD"]
        rd["texture"] = {"m_FileID": 0, "m_PathID": tex_obj.path_id}
        rd["textureRect"] = dict(sp["m_Rect"])
        rd["textureRectOffset"] = {"x": 0.0, "y": 0.0}
        rd["settingsRaw"] = 2              # not packed, rectangle mode, full-rect mesh
        rd["uvTransform"] = {"x": ppu, "y": x + SIZE / 2, "z": ppu, "w": y + SIZE / 2}
        rd["vertices"] = [{"pos": {"x": vx, "y": vy, "z": 0.0}}
                          for vx, vy in ((-half, half), (half, half), (-half, -half), (half, -half))]
        rd["indices"] = [0, 1, 2, 2, 1, 3]
        return _new_object(sa, template, sp).path_id

    new_sets = []
    for bt, kind, *_rest in BADGES:
        ids = [locked.path_id]
        for col, lv in enumerate(LEVELS):
            x, y = cells[(bt, col)]
            ids.append(make_sprite(templates[col], f"Badge_Windstock_{kind}_{lv}_01", x, y))
        new_sets.append((bt, ids))

    store_template = sprites["luckyegg"]
    shop_sprites = {}
    for stem, *_rest in SHOP_ITEMS:
        x, y = cells[(stem, 0)]
        shop_sprites[stem] = make_sprite(store_template, f"Store_Windstock_{stem}", x, y)

    # BadgeService: m_GameObject, m_Enabled, m_Script, m_Name, badgeAwardGuiPrefab,
    # defaultBadgeSprites[], badgeSprites[] -- rewrite the last array with ours added
    raw = service.get_raw_data()
    off = 28
    n = struct.unpack_from("<i", raw, off)[0]
    off = (off + 4 + n + 3) & ~3
    off += 12                                          # badgeAwardGuiPrefab
    n = struct.unpack_from("<i", raw, off)[0]
    off += 4 + 12 * n                                  # defaultBadgeSprites
    sets_at = off
    count = struct.unpack_from("<i", raw, off)[0]
    off += 4
    existing = []
    for _ in range(count):
        bt, k = struct.unpack_from("<ii", raw, off)
        existing.append((bt, raw[off:off + 8 + 12 * k]))
        off += 8 + 12 * k
    tail = raw[off:]
    ours = {bt for bt, _ in new_sets}
    kept = [blob for bt, blob in existing if bt not in ours]
    blobs = kept + [struct.pack("<ii", bt, len(ids)) + b"".join(struct.pack("<iq", 0, i) for i in ids)
                    for bt, ids in new_sets]
    service.set_raw_data(raw[:sets_at] + struct.pack("<i", len(blobs)) + b"".join(blobs) + tail)

    patch_store_sprites(sa, gg, gg_fid, shop_sprites)
    patch_incense_material(sa)
    patch_menu_blackout(sa)
    _write(os.path.join(data_dir, "sharedassets0.assets"), sa.save())
    return service.path_id, tex_obj.path_id, new_sets


def patch_store_sprites(sa, gg, gg_fid, shop_sprites):
    """ItemSpriteLookup (the in-game store's art table): m_GameObject, m_Enabled, m_Script,
    m_Name, sprites[] {Item, Sprite}, altSprites[] {Item, Sprite}, iapSprites[] {string sku,
    Sprite}, currencySprites[] ... -- append our SKUs to iapSprites."""
    script_ids = {o.path_id for o in gg.objects.values()
                  if o.type.name == "MonoScript" and o.read().m_ClassName == "ItemSpriteLookup"}
    lookup = None
    for o in sa.objects.values():
        if o.type.name == "MonoBehaviour":
            fid, pid = struct.unpack_from("<iq", o.get_raw_data(), 16)
            if fid == gg_fid and pid in script_ids:
                lookup = o
    if lookup is None:
        raise SystemExit("ItemSpriteLookup not found in sharedassets0.assets")
    raw = lookup.get_raw_data()
    off = 28
    n = struct.unpack_from("<i", raw, off)[0]
    off = (off + 4 + n + 3) & ~3
    # sprites[] {Item, Sprite}: the display-only Items get the shiny items' icons
    ours_items = {DISPLAY_ITEMS[stem][0]: pid for stem, pid in shop_sprites.items()}
    sprites_at = off
    n = struct.unpack_from("<i", raw, off)[0]
    entries = [struct.unpack_from("<iiq", raw, off + 4 + 16 * k) for k in range(n)]
    off += 4 + 16 * n
    kept = [e for e in entries if e[0] not in ours_items]
    new_sprites = struct.pack("<i", len(kept) + len(ours_items)) + b"".join(
        struct.pack("<iiq", *e) for e in kept) + b"".join(
        struct.pack("<iiq", it, 0, pid) for it, pid in sorted(ours_items.items()))
    raw = raw[:sprites_at] + new_sprites + raw[off:]
    off = sprites_at + len(new_sprites)
    n = struct.unpack_from("<i", raw, off)[0]             # altSprites {Item, Sprite}
    off += 4 + 16 * n
    iap_at = off
    count = struct.unpack_from("<i", raw, off)[0]
    off += 4
    entries = []
    for _ in range(count):
        L = struct.unpack_from("<i", raw, off)[0]
        sku = raw[off + 4:off + 4 + L].decode()
        end = ((off + 4 + L + 3) & ~3) + 12
        entries.append((sku, raw[off:end]))
        off = end
    tail = raw[off:]
    ours = {}
    for stem, pid in shop_sprites.items():
        for sku in (f"{stem}.1", stem):
            b = sku.encode()
            blob = struct.pack("<i", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4) + struct.pack("<iq", 0, pid)
            ours[sku] = blob
    blobs = [blob for sku, blob in entries if sku not in ours] + list(ours.values())
    lookup.set_raw_data(raw[:iap_at] + struct.pack("<i", len(blobs)) + b"".join(blobs) + tail)


# ------------------------------------------------------------------ resources: strings
def _decrypt(b64):
    pt = AES.new(TEXT_KEY, AES.MODE_CBC, TEXT_IV).decrypt(base64.b64decode(b64))
    return pt[:-pt[-1]].decode("utf-8")


def _encrypt(text):
    pt = text.encode("utf-8")
    pad = 16 - len(pt) % 16
    return base64.b64encode(AES.new(TEXT_KEY, AES.MODE_CBC, TEXT_IV).encrypt(pt + bytes([pad]) * pad)).decode()


def _patch_table(sf, table_name, entries):
    """Add/replace rows {key: english} in one encrypted TSV TextAsset. The layout comes
    from the table's own header: 0.35 quotes every cell ("Key", "English", "Japanese", ...);
    0.29 doesn't and has a Context and TPCi comment columns. English goes in every language
    column (better than a raw key on screen), "" in the others."""
    target = None
    for o in sf.objects.values():
        if o.type.name == "TextAsset" and o.read().m_Name == table_name:
            target = o
    if target is None:
        raise SystemExit(f"TextAsset '{table_name}' not found")
    tt = target.read_typetree()
    script = tt["m_Script"]
    if isinstance(script, bytes):
        script = script.decode("ascii")
    table = _decrypt(script.strip())
    lines = table.split("\n")
    header = lines[0].split("\t")
    quoted = header[0].startswith('"')
    names = [h.strip('"') for h in header]

    def cell(v):
        return f'"{v}"' if quoted else v

    rows = []
    for key, en in entries.items():
        row = [key]
        for h in names[1:]:
            row.append("" if (h == "Context" or h.startswith("TPCi")) else en)
        rows.append("\t".join(cell(v) for v in row))
    keys = {cell(k) for k in entries}
    kept = [l for l in lines if l.split("\t", 1)[0] not in keys]
    while kept and kept[-1] == "":
        kept.pop()
    trailing = "\n" if table.endswith("\n") else ""
    tt["m_Script"] = _encrypt("\n".join(kept + rows) + trailing)
    target.save_typetree(tt)
    return len(rows)


def _table_files(data_dir, names=("general", "items")):
    """{table name: file holding it}. iOS keeps every Resources asset in
    resources.assets; Android has no resources.assets -- each one is its own hash-named
    serialized file in Data/ (0.35: general = 36dcea23..., items = e5ce7899...)."""
    res = os.path.join(data_dir, "resources.assets")
    if os.path.exists(res):
        return {n: res for n in names}
    found = {}
    for f in sorted(os.listdir(data_dir)):
        p = os.path.join(data_dir, f)
        if not re.fullmatch(r"[0-9a-f]{32}", f) or not os.path.isfile(p):
            continue
        try:
            env = UnityPy.load(p)
        except Exception:
            continue
        for o in env.objects:
            if o.type.name == "TextAsset":
                try:
                    n = o.peek_name()
                except Exception:
                    continue
                if n in names:
                    found[n] = p
        if len(found) == len(names):
            break
    missing = set(names) - set(found)
    if missing:
        raise SystemExit(f"text tables not found: {sorted(missing)}")
    return found


def patch_strings(data_dir, links=None):
    """Returns (rows added, files written). links: extra "general" rows (LINKS_IOS /
    LINKS_ANDROID) -- Settings' Help Center / Report an Issue addresses and prompts."""
    badges = dict(links or {})
    for bt, _kind, title, desc, fmt in BADGES:
        badges.update({f"{bt}": desc, f"{bt}_title": title, f"{bt}_value_format": fmt})
    shop = {}
    for stem, _icon, title, desc in SHOP_ITEMS:
        for sku in (f"{stem}.1", stem):
            shop.update({f"{sku}_title": title, f"{sku}_description": desc})
        key = DISPLAY_ITEMS[stem][1]                        # the popup's item name
        shop.update({f"{key}_name": title, f"{key}_desc": desc})
    tables = _table_files(data_dir)
    by_file = {}
    for name, rows in (("general", badges), ("items", shop)):
        by_file.setdefault(tables[name], []).append((name, rows))
    n = 0
    for path, work in by_file.items():
        sf = list(UnityPy.load(path).files.values())[0]
        for name, rows in work:
            n += _patch_table(sf, name, rows)
        _write(path, sf.save())
    return n, sorted(by_file)


# ------------------------------------------------------------------ driver
def app_version(app):
    with open(os.path.join(app, "Info.plist"), "rb") as fh:
        return plistlib.load(fh).get("CFBundleShortVersionString", "unknown")


def patch_app(app):
    data = os.path.join(app, "Data")
    pristine = os.path.join(HERE, "_pristine", app_version(app))
    os.makedirs(pristine, exist_ok=True)
    for name in ("sharedassets0.assets", "resources.assets"):
        keep = os.path.join(pristine, name)
        if not os.path.exists(keep):
            shutil.copy2(os.path.join(data, name), keep)
            print(f"kept pristine {name} in {pristine}")
        shutil.copy2(keep, os.path.join(data, name))
    svc, tex, sets = patch_sprites(data)
    print(f"sharedassets0: BadgeService #{svc}, texture #{tex}, "
          + ", ".join(f"type {bt} -> sprites {ids[1:]}" for bt, ids in sets))
    n, _files = patch_strings(data, LINKS_IOS)
    print(f"text: {n} strings added ('general' + 'items')")
    verify(data)


def verify(data_dir):
    """Reload what we wrote: the new sprites must render, the strings must decrypt."""
    env = UnityPy.load(os.path.join(data_dir, "sharedassets0.assets"))
    sf = list(env.files.values())[0]
    ok = 0
    for o in sf.objects.values():
        if o.type.name == "Sprite":
            sp = o.read()
            if sp.m_Name.startswith("Badge_Windstock_"):
                im = sp.image
                assert im.size == (SIZE, SIZE) and im.getextrema()[3][1] > 0, sp.m_Name
                ok += 1
    assert ok == 3 * len(BADGES), f"only {ok} new sprites render"
    shop_ok = sum(1 for o in sf.objects.values() if o.type.name == "Sprite"
                  and o.read().m_Name.startswith("Store_Windstock_") and o.read().image.getextrema()[3][1] > 0)
    assert shop_ok == len(SHOP_ITEMS), f"only {shop_ok} store sprites render"
    ok += shop_ok
    want = {"general": [f"{bt}_title" for bt, *_ in BADGES],
            "items": [f"{stem}.1_title" for stem, *_ in SHOP_ITEMS]
                     + [f"{DISPLAY_ITEMS[stem][1]}_name" for stem, *_ in SHOP_ITEMS]}
    objs = []
    for path in set(_table_files(data_dir).values()):
        objs += list(list(UnityPy.load(path).files.values())[0].objects.values())
    for o in objs:
        if o.type.name == "TextAsset" and o.read().m_Name in want:
            s = o.read().m_Script
            table = _decrypt(s if isinstance(s, str) else s.decode())
            keys = {l.split("\t", 1)[0].strip('"') for l in table.split("\n")}
            for k in want[o.read().m_Name]:
                assert k in keys, f"{k} missing from {o.read().m_Name}"
    print(f"verified: {ok} sprites render, strings decrypt")


# ------------------------------------------------------------------ Android
SPLIT = 1 << 20          # Unity's Android .splitN piece size (1 MiB)
SIGNER = os.path.join(HERE, "..", "..", "2017", "apk_sign_v2.py")
SIGN_KEY = os.path.join(HERE, "..", "..", "2017", "signkey", "sign.key")
SIGN_CRT = os.path.join(HERE, "..", "..", "2017", "signkey", "sign.crt")
DATA = "assets/bin/Data/"


def patch_apk(src, out, key=SIGN_KEY, crt=SIGN_CRT):
    """Patch an Android APK: join the .split pieces, patch, re-split, rebuild, sign.

    Everything not patched is copied entry for entry (same order, same compression).
    The old v1 signature (META-INF/*.SF/.RSA/MANIFEST.MF) is dropped -- it can't match
    the new bytes -- and the result is signed with APK Signature Scheme v2 by
    2017/apk_sign_v2.py (no Java on this Mac)."""
    work = tempfile.mkdtemp()
    try:
        data = os.path.join(work, "Data")
        os.makedirs(data)
        zin = zipfile.ZipFile(src)
        infos = zin.infolist()
        pieces = {}
        for i in infos:
            if not i.filename.startswith(DATA):
                continue
            rel = i.filename[len(DATA):]
            if "/" in rel or not rel:
                continue
            m = re.fullmatch(r"(.+)\.split(\d+)", rel)
            if m:
                pieces.setdefault(m.group(1), []).append((int(m.group(2)), i))
            elif re.fullmatch(r"[0-9a-f]{32}", rel) or rel in ("sharedassets0.assets", "globalgamemanagers.assets"):
                with open(os.path.join(data, rel), "wb") as fh:
                    fh.write(zin.read(i))
        for base in ("sharedassets0.assets", "globalgamemanagers.assets"):
            if base in pieces:
                with open(os.path.join(data, base), "wb") as fh:
                    for _, i in sorted(pieces[base], key=lambda t: t[0]):
                        fh.write(zin.read(i))
        # refuse an already-patched APK: patching twice would stack the new objects
        sa = list(UnityPy.load(os.path.join(data, "sharedassets0.assets")).files.values())[0]
        if any(o.type.name == "Texture2D" and o.peek_name() == TEX_NAME for o in sa.objects.values()):
            raise SystemExit(f"{src} is already patched -- give the unpatched APK")
        del sa
        svc, tex, sets = patch_sprites(data)
        print(f"sharedassets0: BadgeService #{svc}, texture #{tex}")
        n, table_files = patch_strings(data, LINKS_ANDROID)
        print(f"text: {n} strings added to {[os.path.basename(f) for f in table_files]}")
        verify(data)

        new_sa = open(os.path.join(data, "sharedassets0.assets"), "rb").read()
        replaced = {os.path.basename(f): open(f, "rb").read() for f in table_files}
        old_sa = {i.filename for _, i in pieces.get("sharedassets0.assets", [])}
        sa_template = pieces["sharedassets0.assets"][0][1] if "sharedassets0.assets" in pieces else None
        unsigned = os.path.join(work, "unsigned.apk")
        with zipfile.ZipFile(unsigned, "w") as zout:
            wrote_sa = False
            for i in infos:
                name = i.filename
                if name.startswith("META-INF/") and re.search(r"\.(SF|RSA|DSA|EC)$|MANIFEST\.MF$", name):
                    continue                                   # stale v1 signature
                if name in old_sa:
                    if not wrote_sa:                           # all new pieces, where the old began
                        for k in range(0, len(new_sa), SPLIT):
                            zi = zipfile.ZipInfo(f"{DATA}sharedassets0.assets.split{k // SPLIT}", i.date_time)
                            zi.compress_type = sa_template.compress_type
                            zi.external_attr = sa_template.external_attr
                            zout.writestr(zi, new_sa[k:k + SPLIT])
                        wrote_sa = True
                    continue
                rel = name[len(DATA):] if name.startswith(DATA) else None
                body = replaced.get(rel) if rel else None
                zout.writestr(i, body if body is not None else zin.read(i))
        print(f"sharedassets0: {len(old_sa)} pieces -> {-(-len(new_sa) // SPLIT)}")
        sys.path.insert(0, os.path.dirname(os.path.abspath(SIGNER)))
        import apk_sign_v2
        apk_sign_v2.sign(unsigned, out, key, crt)
        print(f"wrote {out} (signed v2 with {os.path.basename(crt)})")
    finally:
        shutil.rmtree(work)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="pokemongo.app directory, an .ipa, or an Android .apk")
    ap.add_argument("-o", "--out", help="output .ipa / .apk (IPA and APK input)")
    a = ap.parse_args(argv)
    if a.target.endswith(".apk"):
        if not a.out:
            ap.error("an .apk needs -o <out.apk>")
        patch_apk(a.target, os.path.abspath(a.out))
    elif a.target.endswith(".ipa"):
        if not a.out:
            ap.error("an .ipa needs -o <out.ipa>")
        work = tempfile.mkdtemp()
        try:
            subprocess.run(["unzip", "-q", a.target, "-d", work], check=True)
            payload = os.path.join(work, "Payload")
            app = os.path.join(payload, next(n for n in os.listdir(payload) if n.endswith(".app")))
            patch_app(app)
            out = os.path.abspath(a.out)
            if os.path.exists(out):
                os.remove(out)
            subprocess.run(["zip", "-qry", out, "Payload"], cwd=work, check=True)
            print(f"wrote {out} (re-sign before installing)")
        finally:
            shutil.rmtree(work)
    else:
        patch_app(a.target.rstrip("/"))


if __name__ == "__main__":
    main(sys.argv[1:])
