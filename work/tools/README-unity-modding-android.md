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

On this Mac (no Java, no ffmpeg, no adb needed to *build*):

```
python3 -m pip install --user UnityPy==1.25.2 Pillow pycryptodome numpy crcmod
```

- **UnityPy must be imported through `work/tools/unitypy_fix.py`** (every tool here does
  this). Plain UnityPy corrupts Unity 5.3 files on save — see [§6](#6-how-it-works).
- Signing uses `2017/apk_sign_v2.py` and the key in `2017/signkey/` (`sign.key`, `sign.crt`).

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
| `assets/bin/Data/sharedassets0.assets.split0` … `split88` | rewritten (was `split0` … `split85`): 1 new texture + 11 sprites, `BadgeService.badgeSprites` +3 sets, `ItemSpriteLookup.iapSprites` +4 SKUs, `ItemSpriteLookup.sprites` +2 Items (402, 604) |
| `assets/bin/Data/36dcea23e84ab4a3e98b42efc8bd2151` | the `general` text table: `38`, `38_title`, `38_value_format` … `40_*`, and Settings' Report button → "Project Windstock" (`settings_report_issues`, `report_issues_link` = projectwindstock.site.je, its prompt) |
| `assets/bin/Data/e5ce7899cb30147b2ab7d3690f429238` | the `items` text table: `shinycharm.1_title/_description`, `shinycharm_*`, `shinyincense.1_*`, `shinyincense_*`, and `item_x_miracle_name/_desc`, `item_incense_spicy_name/_desc` |
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
  `shinycharm.1` and `shinycharm` (the game looks up `sku`, then the part before the first
  `.`). Tile names are `"<sku>_title"` in the `items` table.
- **The "you got …" popup** names the Item a listing *yields*. The shiny pair aren't bag
  items, so the server (`shop._DISPLAY_ITEM`) says they yield two Items the 2016 game never
  uses — `ITEM_X_MIRACLE` (604) for the Shiny Charm and `ITEM_INCENSE_SPICY` (402) for the
  Shiny Incense — and the tool renames those (`item_x_miracle_name`, `item_incense_spicy_name`)
  and gives them the items' icons in `ItemSpriteLookup.sprites`. The server never puts
  either in a bag and always reports both as count 0, so nothing lingers after a purchase.
  (Before this, the popup said `item_unknown_name`.)
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
   `unitypy_fix.py` keeps each object's own index; clones inherit their template's.
2. **Streaming.** UnityPy reads object data from the source file until `save()` returns,
   so `open(path, "wb").write(sf.save())` truncates the file first and writes a broken,
   much smaller one. The tools write to `*.tmp` and rename.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| Splash, then grey screen, no network | an asset file saved without `unitypy_fix` — rebuild from the unpatched APK |
| Medal shows a Poké Ball and `38_title` | APK not patched (or a different APK installed) |
| Blank store tile / no shiny items in the store | `shop.patched_android` off, or the APK isn't patched |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | uninstall the old app first (different signing key) |
| Install refused on an old phone | Android < 7.0 can't verify a v2-only signature |
| `… is already patched -- give the unpatched APK` | run it on `RELEASE/pokemon-go-0.35-windstock.apk`, not on its output |
| Shiny has a normal-coloured rim around the eyes | an old shiny build — rebuild with §2 (eye sheets are recoloured since 2026-09-25) |

## 8. Not covered on Android

The shiny **sparkle effect** and the **shiny mark next to the CP** are drawn by the iOS
tweak (`work/mods/ios-tweak-035`), not by game data, so they don't exist on Android until
the Android agent (`work/mods/android-035`) gets them.
