# Unity-level modding — Android (0.35 APK)

How the Windstock game-data mods are applied to the Android client: **shiny models for all
151 Kanto Pokémon**, the **custom medals** (Shiny Hunter, Night Owl, Shinydex), and the
**Shiny Charm / Shiny Incense in the in-game store**. All of it is *data*: nothing in the
game's code is changed, so the same `libil2cpp.so` keeps running.

| Mod | Where it lives | Applied by |
|---|---|---|
| Shiny models `pm0001_s` … `pm0151_s` | server, `work/server/assets/` (Android set) | the server serves them; the Android agent swaps them in |
| Medal art, names, descriptions | inside the APK (`sharedassets0` + text tables) | `patch_badges.py` |
| Store tiles + names for the shiny items | inside the APK (`sharedassets0` + `items` table) | `patch_badges.py` |
| Medal progress, targets, award popup | server (`protocol.py` `CUSTOM_BADGES`, game master) | nothing to do |

---

## 1. Requirements

The asset tools live in `windstock/src/asset_tools/` and need their own
dependencies — the game server does not import any of them, which is the point
of keeping them out of `src/server/`:

```
python3 -m pip install -r windstock/requirements-assets.txt
```

- **UnityPy must be imported through `windstock/src/asset_tools/unitypy_fix.py`** (every tool
  does this). Plain UnityPy corrupts Unity 5.3 files on save — see [§6](#6-how-it-works).
  `src/tools/unitypy_fix.py`, `src/tools/decrypt_bundle.py` and
  `src/tools/unity_assets.py` are thin shims that re-export the one implementation.
- Signing uses `2017/apk_sign_v2.py` and the key in `2017/signkey/` (`sign.key`, `sign.crt`).

## 1b. The asset tools

Everything below works on the shipped set with no server running.

```
cd windstock
py src/asset_tools/cli.py status            # 151 bundles, variants, replace mode
py src/asset_tools/cli.py inspect pm0025    # object counts, textures, materials
py src/asset_tools/cli.py build pm0025 --variant shiny
py src/asset_tools/cli.py verify --all
py src/asset_tools/cli.py editor            # the desktop editor
```

| Command | What it does |
|---|---|
| `status` / `list` | what is in the assets folder, and what is a variant |
| `inspect` | object counts plus the texture and material tables |
| `textures` | every texture as a PNG (default `src/server/data/texture_exports/`) |
| `export` | a rigged, textured `.glb` |
| `build` | write `pm####_suffix` and register it in `extra_digest.json` |
| `verify` | re-check the HMAC and `asset_digest` checksum of every bundle |
| `decrypt` | strip the AES layer to a plain `UnityFS` bundle |
| `selftest` | UnityPy round-trip guard — **needs a plain bundle**, so `decrypt` first |
| `editor` | the CustomTkinter editor |

`build` never overwrites: a variant name that already exists is refused, and
replacing a genuine bundle needs `assets.allow_replace` in `settings.json` (the
original is backed up to `src/server/data/asset_backups/android/` first).

`--replace-texture Name=file.png` matches a texture by name (exact, then
case-insensitive, then unique substring) or by `--texture path_id`, and is
applied **on top of** the recolour, so painted pixels win.

### The editor

```
py src/asset_tools/cli.py editor [pm0025]
```

Pick a bundle on the left; the **Texture** tab is a pixel editor (draw/erase,
brush size, swatches, zoom and pan) and the **3D preview** tab renders the
skinned mesh with the current textures — drag to orbit, scroll to zoom.
Recolour sliders and a tint turn the build into a custom one; **Build variant**
is the only thing that writes to disk, and it carries your painted pixels
across. Work happens on a worker thread, so the window never freezes.

The preview is an orthographic software renderer (`src/asset_tools/render3d.py`):
no OpenGL, no GPU driver, no browser. Nearest-neighbour sampling is deliberate —
it shows whether alpha and UVs are right instead of hiding them.

## 1c. How to make your custom model show up instead of the default one

This is the part that surprises people, so it is spelled out from the bottom up.
Short version first, then the detail.

### The one-paragraph version

The game asks for a model by **asset id** (a long id baked into the client's
item templates), and the server answers "here is the file for that id". Building a
variant (`pm0147_shiny`) gives you a **brand-new asset id**, so on its own nothing
points the game at it — the phone keeps asking for `pm0147` and keeps getting the
original. To change what an *existing* Pokémon looks like you must keep its
**asset id** and change only the file behind it. That is what the editor's
**replace base** option does, and it is the only option that makes a stock,
unmodified client show your texture.

### Step by step

**Step 1 — turn on replace mode (once).**

Open the server's settings file. From the `windstock` folder it is
`src/server/data/settings.json`. In the `assets` section set:

```json
"assets": {
  "dir": "",
  "dir_ios": "",
  "allow_replace": true
}
```

`"dir": ""` means "use the set that shipped with the server", which is what you
want. `allow_replace` is the master switch for overwrite-in-place. Without it the
editor greys out the checkbox and the CLI refuses, so you cannot do this by
accident. (`py src/asset_tools/cli.py status` prints the current value, and shows
`replace mode  off` while it is off.)

Save the file. **No server restart needed.**

**Step 2 — open the Pokémon in the editor.**

```
cd windstock
py src/asset_tools/cli.py editor pm0147
```

Pick what you like: paint on the **Texture** tab, or use the **Recolour** sliders
for a preset, or move the sliders yourself for a custom one. The **3D preview** tab
shows the result on the actual model.

**Step 3 — tick "replace base".**

In the **Build** panel on the right, tick the **replace base** box. The button
underneath relabels itself from **Build variant** to **Replace base bundle** —
that is your confirmation that you are about to overwrite a shipped bundle, not
make a new one. Press it.

The variant-suffix box is ignored in this mode — it overwrites `pm0147` itself.

What happens, in order:

1. The original `pm0147` is copied to `src/server/data/asset_backups/android/` first. If
   you did not like the result, that backup is your undo.
2. `pm0147` is rebuilt with your textures and written over the original.
3. It is registered in `src/server/assets/extra_digest.json` **under the same
   name**, with a new checksum, size and encryption key — and **the same asset
   id**.

**Step 4 — that is it. No restart, no re-patching the APK.**

The server notices the new checksum and the phone downloads your file the next
time it starts the game.

### Why the asset id is the whole trick

Here is what the id looks like before and after, for a real replace of `pm0001`:

| | asset id | what the phone asks for |
|---|---|---|
| shipped | `0bd50fd0-…-88b6ec74a1bd/1467337882194000` | that exact id |
| after replace | `0bd50fd0-…-88b6ec74a1bd/1467337882194000` | **the same id** — gets your file |
| if the id had changed | `9f2c…-newuuid/1790405314` | the old id matches nothing → no model |

A variant like `pm0147_shiny` is deliberately a *different* asset with a
*different* id, so it needs something to point the game at it. For the shiny
models that something already exists: `work/server/shiny.py` rolls whether a
spawn is shiny, and if it is, the encounter loads the `pm####_s` model instead
(see its own header comment). That is fine for "make this species shiny". It is
not what you want for "make every Mewtwo look like this" — for that you replace
the base bundle.

**Rule of thumb:**

| What you want | What to do |
|---|---|
| Change how a Pokémon looks, for everyone, always | **replace base** (keeps the asset id) |
| A shiny/alternate model the game asks for by name | build a variant — something else selects it |
| Try a texture without committing | just build a variant and export a `.glb` to look at it |

### Turning it back off

Put `"allow_replace": false` back. That only stops *future* replaces; the file on
disk is still yours. To get the original back, copy it out of
`src/server/data/asset_backups/android/` over `pm0147` in the assets folder and delete
that bundle's entry from `extra_digest.json` — the shipped `asset_digest` is never
rewritten, so removing your entry is a clean revert to stock.

### Doing it from the command line instead

Same three steps without the editor:

```
py src/asset_tools/cli.py build pm0147 --replace --variant unused
```

Check it afterwards:

```
py src/asset_tools/cli.py verify pm0147
```

### If a model does not appear

Work down this list — it is ordered by how often each one is the cause.

| Check | How | What a problem looks like |
|---|---|---|
| Is your bundle listed? | `py src/asset_tools/cli.py status` | your build is missing, or the count is still "variants 0" |
| Does it verify? | `py src/asset_tools/cli.py verify pm0147` | `checksum, size, HMAC, UnityFS header` all OK |
| Is it the *same* asset id? | open `src/server/assets/extra_digest.json`, compare `asset_id` with the shipped one in `asset_digest` | a **new uuid** means you built a variant, not a replace |
| Is the server's view right? | see §1d | the entry is missing or stale |
| Did you replace `pm0147` but are looking at `pm0147_s`? | `py src/asset_tools/cli.py status` | two bundles for one Pokémon; the client only ever loads the id it knows |
| Android **and** iOS sets | `assets.dir` vs `assets.dir_ios` | replaced the Android set, testing on iOS (or the reverse) |

**The most common mistake by far:** building `pm0147_s` and expecting the normal
Pokémon to change. It does not. Use replace base.

**Second most common:** replacing a bundle while the server is serving a *cached*
copy, then not fully quitting the game on the phone. Force-close the app, not
just the background task.

### Why the phone re-downloads at all

The client caches models by the digest, and only re-downloads when something in
the digest entry changes. Replacing a bundle changes its checksum and size, and
the server also moves the digest's own timestamp forward past the newest bundle
it has — so a phone that already cached the old digest sees something new, asks
again, and gets your file. That timestamp bump is why "no server restart needed"
is true.

## 1d. Checking what the server will actually hand out

When a model will not show up, the fastest way to see the server's actual answer
instead of guessing:

```
cd windstock
py -c "import sys; sys.path.insert(0,'src/server'); from windstock.game import protocol as P; \
d=P.build_get_asset_digest_response('android'); \
print('problems:', P.check_digest_response(d,'android') or 'none'); \
print('bundles advertised:', len(P._load_real_digest('android')))"
```

- `check_digest_response` returns the things that have each silently broken
  before: the 151 shipped entries still intact, exactly one timestamp, a `result`
  field, no bundle listed twice, and nothing advertised that is missing from disk.
- An empty list means the digest itself is fine, so the problem is on the phone
  side (caching, wrong platform set, or the wrong asset id).

To confirm one specific id resolves to a file:

```
py -c "import sys; sys.path.insert(0,'src/server'); from windstock.game import protocol as P; \
import os; print(P.bundle_path(b'0bd50fd0-3d5f-4a1c-98af-88b6ec74a1bd/1467337882194000'))"
```

If that prints a path, the server is ready to serve it and the problem is the
client. If it prints `None`, the id is not in the digest — usually a variant id
where a base id was expected.

## 2. Build the Android shiny models (server side)

Already built — `work/server/assets/pm0001_s` … `pm0151_s`, listed in
`work/server/assets/extra_digest.json`. To rebuild them all:

```
cd work/tools
python3 make_all_shinies.py 1 24 --force --assets ../server/assets
python3 make_all_shinies.py 27 151 --force --assets ../server/assets
```

- 25 (Pikachu) and 26 (Raichu) are **hand-tuned**: leave them out of `--force` runs, as above.
- Colour comes from the official normal/shiny icon pair in `_shiny_icons/` (PokeMiners
  `pogo_assets`). `shiny_overrides.py` has hand rules for 49, 81, 82 and 133; Ponyta and
  Rapidash get blue flames from `FLAME_SPECIES` in `make_shiny_bundle.py`.
- **Never run two builds at the same time on the same machine without the unique temp
  files** (`make_shiny_bundle.py` names them per process) — an old version collided and
  could put iOS textures in an Android bundle.
- No server restart needed: the server re-reads `extra_digest.json` when it changes and
  moves the advertised digest timestamp past the newest bundle (`protocol.digest_timestamp`),
  so the phone fetches the new list — and the rebuilt bundles — the next time the game starts.

## 3. Patch the APK

Always start from an **unpatched** APK — the tool refuses one it has already patched.

```
python3 work/tools/patch_badges.py RELEASE/pokemon-go-0.35-windstock.apk \
        -o RELEASE/pokemon-go-0.35-windstock-unity.apk
```

Expected output (about 15 s):

```
sharedassets0: BadgeService #12772, texture #16424
text: 17 strings added to ['36dcea23e84ab4a3e98b42efc8bd2151', 'e5ce7899cb30147b2ab7d3690f429238']
verified: 11 sprites render, strings decrypt
sharedassets0: 86 pieces -> 89
  self-check OK: v2 block valid, digest matches, signature verifies (algo 0x103)
wrote …/pokemon-go-0.35-windstock-unity.apk (signed v2 with sign.crt)
```

What changes in the APK, and nothing else (checked entry by entry):

| Entry | Change |
|---|---|
| `assets/bin/Data/sharedassets0.assets.split0` … `split88` | rewritten (was `split0` … `split85`): 1 new texture + 11 sprites, `BadgeService.badgeSprites` +3 sets, `ItemSpriteLookup.iapSprites` +4 SKUs |
| `assets/bin/Data/36dcea23e84ab4a3e98b42efc8bd2151` | the `general` text table: `38`, `38_title`, `38_value_format` … `40_*` |
| `assets/bin/Data/e5ce7899cb30147b2ab7d3690f429238` | the `items` text table: `shinycharm.1_title/_description`, `shinycharm_*`, `shinyincense.1_*`, `shinyincense_*` |
| `META-INF/MANIFEST.MF`, `POGO.SF`, `POGO.RSA` | **removed** (the old v1 signature can't match) |
| everything else | byte-identical, same order, same compression |

## 4. Turn it on in the server

The server can't tell a patched Android client from a stock one, so say so in the server's
`settings.json` (in its data folder, `work/server/data/` for the desktop server; it is
re-read on change, no restart):

```json
"shop": { "patched_android": true }
```

Without it the in-game store leaves the two shiny items out on Android (they'd be blank
tiles on a stock APK); they're always in the web shop. iOS needs nothing — the launcher's
copy of the game is patched by `ios-launcher/tools/embed_guest.sh`.

## 5. Install and check

- **Android 7.0 or newer**: the APK is signed with APK Signature Scheme **v2 only**.
- **Uninstall the old app first.** The release APK is signed with a different key, so
  Android refuses an update (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`). Trainer data lives on
  the server, so nothing is lost.
- `adb install RELEASE/pokemon-go-0.35-windstock-unity.apk`, or copy it to the phone and open it.

Check on the phone:

| Where | Expect |
|---|---|
| Trainer profile → Medals | Shiny Hunter (sparkles), Night Owl (moon), Shinydex (Poké Ball + sparkle) with their names — a Poké Ball medal named `38_title` means the APK isn't patched |
| Shop | Shiny Charm and Shiny Incense tiles with icons (needs `patched_android`) |
| A shiny encounter | the recoloured model (the Android agent swaps `pm####` for `pm####_s`) |

## 6. How it works

### Android keeps the data differently from iOS

| | iOS (`Payload/*.app/Data/`) | Android (`assets/bin/Data/`) |
|---|---|---|
| Big files | whole files | cut into **1 MiB pieces**: `sharedassets0.assets.split0…N` — the tool joins them, patches, and re-cuts at exactly 1 048 576 bytes |
| Resources (text tables) | all in `resources.assets` | **one hash-named file each** — `general` is `36dcea23…`, `items` is `e5ce7899…` (found by name, not hard-coded) |
| Texture data | `.resS` side file | inline in the asset file |
| Signature | re-signed by Sideloadly | v2 by `apk_sign_v2.py`, v1 files removed |

### The data the medals and store read

- **Medal art**: `BadgeService` (a MonoBehaviour in `sharedassets0`) has a saved
  `badgeSprites` array of `{badgeType, sprites[4]}` — locked, bronze, silver, gold.
  `GetBadgeSprite(type, rank)` looks the type up there, else uses the generic medal. New
  medals = new entries pointing at new sprites.
- **Medal text**: a badge type the game has no name for (38+) still parses —
  `HoloBadgeType` is an int on the wire — and `Enum.ToString()` gives `"38"`, so its text
  keys are `38`, `38_title`, `38_value_format`.
- **Store art**: `ItemSpriteLookup.iapSprites` is `{sku, sprite}`; the tool adds both
  `shinycharm.1` and `shinycharm` (the game looks up both forms). Tile names are
  `"<sku>_title"` in the `items` table.
- **Text tables** are base64 of AES-256-CBC of a tab-separated table. Key
  `9edcbe74…61e8`, IV `03d9f1a7…236a` — the two static byte arrays of `TextEncryption`,
  read out of `global-metadata.dat`. The 0.35 tables quote every cell and have the columns
  `Key, English, Japanese, French, Spanish, German, Italian`; the tool writes new rows in
  the same shape, English in every language column.
- **New sprites** are unpacked (`settingsRaw = 2`), full-rect, in their own 768×1024 RGBA32
  texture `WindstockBadgeAtlas`, cloned from the stock Pikachu medal / Lucky Egg sprites.

### Two UnityPy traps (both handled; don't remove the guards)

1. **Script indices.** In Unity 5.3 files every object record carries its own
   `script_type_index` (which MonoScript a MonoBehaviour runs). UnityPy stores it on a type
   record that many objects share, so a plain save rebinds MonoBehaviours to the wrong
   scripts. Symptom: **splash screen, then grey, and the game never talks to the server.**
   `unitypy_fix.py` (now `windstock/src/asset_tools/unitypy_fix.py`) keeps each object's
   own index; clones inherit their template's.
2. **Streaming.** UnityPy reads object data from the source file until `save()` returns,
   so `open(path, "wb").write(sf.save())` truncates the file first and writes a broken,
   much smaller one. The tools write to `*.tmp` and rename.

### The bundle container and the digest

Each `pm####` bundle is `[0x01][16-byte IV][AES-128-CBC][20-byte HMAC-SHA1]`. The
key is per-bundle: the digest key XORed with the fixed mask `PFAi$;]G7Rg>kz4w`. The
`asset_digest` file lists, per bundle, a **CRC32C of the encrypted bytes** and the
**file size** — so a corrupted or truncated download is caught before UnityPy ever
touches it. `verify --all` checks the HMAC, the size and that CRC32C against all 151
shipped bundles.

Variants (`pm0025_shiny`) are not in `asset_digest`, which stays exactly as shipped;
they are listed separately in `extra_digest.json`, which the server merges in and
re-reads on change, so a rebuild is picked up on the next game start with no restart.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| Splash, then grey screen, no network | an asset file saved without `unitypy_fix` — rebuild from the unpatched APK |
| Medal shows a Poké Ball and `38_title` | APK not patched (or a different APK installed) |
| Blank store tile / no shiny items in the store | `shop.patched_android` off, or the APK isn't patched |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | uninstall the old app first (different signing key) |
| Install refused on an old phone | Android < 7.0 can't verify a v2-only signature |
| `… is already patched -- give the unpatched APK` | run it on `RELEASE/pokemon-go-0.35-windstock.apk`, not on its output |
| `already exists. Pick another suffix` | that variant is already built — use a different `--variant`, or delete it |
| `replacing a normal bundle is switched off` | set `assets.allow_replace` in `settings.json` (variants need nothing) |
| `not a plain UnityFS bundle` from `selftest` | the bundles are encrypted — `decrypt` to a temp file first |
| Shiny has a normal-coloured rim around the eyes | an old shiny build — rebuild with §2 (eye sheets are recoloured since 2026-09-25) |

## 8. Not covered on Android

The shiny **sparkle effect**, the **shiny mark next to the CP**, and marking **shiny eggs**
in storage are drawn by the iOS tweak (`work/mods/ios-tweak-035`), not by game data, so they
don't exist on Android until the Android agent (`work/mods/android-035`) gets them.
Shiny eggs themselves (the 1-in-100 drop, guaranteed shiny hatch) are server-side and work
on both platforms.
