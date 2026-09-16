"""
Filesystem layout for the Windstock server -- the ONE place that knows where
anything lives on disk.

Two distinct roots, because the server both *writes* user data and *reads*
bundled resources, and the two live in different places once the server is
frozen into a one-file executable:

* :data:`SERVER_ROOT` -- the folder the user sees (the ``.exe``'s folder, or the
  source ``src/server`` folder). Everything the server writes goes under
  ``SERVER_ROOT/data``: settings, saves, gyms, the log, photos.
* :data:`RESOURCE_ROOT` -- read-only assets shipped with the build (``certs/``,
  ``assets/``, ``game_master.bin``, ``cpdata.json``). Under PyInstaller these are
  unpacked into ``sys._MEIPASS``; from source they sit beside this package.

Before this module existed, five modules each carried their own copy of the
"where am I running from" logic. They all import it from here now.

Anything an older build left beside the executable is moved into ``data/`` on
first run, so an existing collection carries over untouched.
"""
import os
import shutil
import sys
from pathlib import Path

# Files and folders older builds wrote beside the .exe. Order doesn't matter;
# each is moved only if `data/` doesn't already have its own copy.
LEGACY = ("settings.json", "places.json", "events.json", "gyms.json",
          "lures.json", "raid.json", "save.json", "nominations.json",
          "server-log.txt", "saves", "photos")


def is_frozen():
    """True when running from a PyInstaller-style bundle."""
    return bool(getattr(sys, "frozen", False))


def base_dir():
    """The folder the user sees: the .exe's folder, or the source server folder.

    From source this file lives at ``<server>/windstock/config/paths.py``, so the
    server root is three directories up. Resolve it from the module rather than
    the process cwd, which can be anything.
    """
    if is_frozen():
        return os.path.dirname(sys.executable)
    return str(Path(__file__).resolve().parents[2])


def resource_root():
    """Where the bundled read-only resources are unpacked."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", base_dir())
    return base_dir()


SERVER_ROOT = base_dir()
RESOURCE_ROOT = resource_root()

# ---- writable runtime data --------------------------------------------------
DATA_DIR = os.path.join(SERVER_ROOT, "data")

# ---- read-only bundled resources --------------------------------------------
CERT_DIR = os.path.join(RESOURCE_ROOT, "certs")
ASSETS_DIR = os.path.join(RESOURCE_ROOT, "assets")
ASSETS_IOS_DIR = os.path.join(RESOURCE_ROOT, "assets_ios")
ASSETS_PLAIN_DIR = os.path.join(RESOURCE_ROOT, "assets_plain")
GAME_MASTER_FILE = os.path.join(RESOURCE_ROOT, "game_master.bin")
CPDATA_FILE = os.path.join(RESOURCE_ROOT, "cpdata.json")
# Biome definitions (favoured types + the OpenStreetMap tags that mark each
# terrain). Shipped beside cpdata.json; see src/server/biomes.json.
BIOMES_FILE = os.path.join(RESOURCE_ROOT, "biomes.json")
SITE_DIR = os.path.join(RESOURCE_ROOT, "site")
ICON_DIR = os.path.join(RESOURCE_ROOT, "shopicons")


def path(*parts):
    """A path inside the writable ``data/`` folder, created if needed."""
    ensure()
    return os.path.join(DATA_DIR, *parts)


_ready = False


def ensure():
    global _ready
    if _ready:
        return DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _migrate()
    except OSError:
        pass
    _ready = True
    return DATA_DIR


def _migrate():
    """Move an older layout's files into data/. Never overwrites: if both exist,
    the copy already in data/ wins and the stray one is left alone rather than
    silently thrown away -- one of them is somebody's save file."""
    moved = []
    for name in LEGACY:
        old = os.path.join(SERVER_ROOT, name)
        new = os.path.join(DATA_DIR, name)
        if not os.path.exists(old) or os.path.exists(new):
            continue
        try:
            shutil.move(old, new)
            moved.append(name)
        except (OSError, shutil.Error):
            pass
    if moved:
        # print, not log: this runs before logging is set up
        print(f"[data] moved into data/: {', '.join(moved)}", flush=True)
    return moved
