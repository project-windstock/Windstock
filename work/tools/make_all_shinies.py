"""
make_all_shinies.py -- build shiny bundles for a range of species with make_shiny_bundle's
--map mode, from the icon pairs in _shiny_icons/ (PokeMiners pogo_assets,
"Images/Pokemon - 256x256": pokemon_icon_###_00.png + pokemon_icon_###_00_shiny.png).

Species that already have a shiny bundle are skipped unless --force: pm0025_s and
pm0026_s were hand-tuned with the hue-shift mode and checked on device.

  python3 make_all_shinies.py                       # 1-151, iOS assets
  python3 make_all_shinies.py 1 9 --preview /tmp/p  # a range, with before|after PNGs
  python3 make_all_shinies.py --assets ../server/assets   # Android
"""
import argparse
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_shiny_bundle as msb  # noqa: E402

ICONS = os.path.join(HERE, "_shiny_icons")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("first", type=int, nargs="?", default=1)
    ap.add_argument("last", type=int, nargs="?", default=151)
    ap.add_argument("--assets", default=os.path.join(msb.SERVER, "assets_ios"))
    ap.add_argument("--icons", default=ICONS)
    ap.add_argument("--preview")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    built, skipped, failed = [], [], []
    for num in range(a.first, a.last + 1):
        name = f"pm{num:04d}"
        if not os.path.exists(os.path.join(a.assets, name)):
            skipped.append((num, "no model bundle")); continue
        if os.path.exists(os.path.join(a.assets, name + "_s")) and not a.force:
            skipped.append((num, "already built")); continue
        normal = os.path.join(a.icons, f"pokemon_icon_{num:03d}_00.png")
        shiny = os.path.join(a.icons, f"pokemon_icon_{num:03d}_00_shiny.png")
        if not (os.path.exists(normal) and os.path.exists(shiny)):
            skipped.append((num, "no icon pair")); continue
        prev = None
        if a.preview:
            prev = os.path.join(a.preview, name)
            os.makedirs(prev, exist_ok=True)
        try:
            fn = msb.icon_map_recolourer(num, normal, shiny)
            mat_fn = msb.flame_material_fn(shiny) if num in msb.FLAME_SPECIES else None
            _, size, _ = msb.build(num, a.assets, fn, prev, mat_fn)
            built.append(num)
        except BaseException as e:           # SystemExit from build() included
            if isinstance(e, KeyboardInterrupt):
                raise
            traceback.print_exc()
            failed.append((num, str(e)))
    print(f"\nbuilt {len(built)}, skipped {len(skipped)}, failed {len(failed)}")
    for num, why in skipped:
        print(f"  skipped #{num}: {why}")
    for num, why in failed:
        print(f"  FAILED #{num}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
