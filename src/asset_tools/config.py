"""Filesystem layout for the asset tools.

These tools are deliberately standalone. They must run on a machine that has
never had the game server installed, and they must not drag ``windstock`` in
with them -- so they read ``settings.json`` themselves rather than importing
``windstock.config``.

That leaves two modules that both know where the settings file is. The contract
between them is the *file*, not the code: the ``assets`` section this module
reads is the same one ``windstock/config/settings.py`` defines, so a path
configured in the editor's Settings panel is the path the server then serves
from. If you change one, change both.
"""
import json
import os
import sys

#: ``src/`` -- the parent of this package, and of ``server/`` and ``tools/``.
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(SRC_DIR, "server")
ASSETS_DIR = os.path.join(SERVER_DIR, "assets")
ASSETS_IOS_DIR = os.path.join(SERVER_DIR, "assets_ios")
DATA_DIR = os.path.join(SERVER_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULTS = {
    "assets": {
        "dir": "",
        "dir_ios": "",
        "allow_replace": False,
    },
}

_cache = None
_cache_key = None


def settings_path():
    """Where settings.json is, for this checkout or frozen build."""
    override = os.environ.get("WINDSTOCK_SETTINGS")
    if override:
        return override
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "settings.json")
    return SETTINGS_FILE


def _read():
    path = settings_path()
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return dict(DEFAULTS), path
    global _cache, _cache_key
    key = (path, stamp)
    if _cache is not None and _cache_key == key:
        return _cache, path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    for section, values in DEFAULTS.items():
        data.setdefault(section, {})
        if isinstance(values, dict):
            for key_name, value in values.items():
                data[section].setdefault(key_name, value)
    _cache, _cache_key = data, key
    return data, path


def get(section, key, default=None):
    data, _ = _read()
    try:
        value = data[section][key]
    except (KeyError, TypeError):
        return default
    return default if value is None else value


def set_value(section, key, value):
    """Write one setting, keeping the rest of the file intact.

    Read-modify-write with a temp file and a rename, so a crash mid-save cannot
    leave a truncated settings.json -- which would take the server's config with
    it, not just the editor's.
    """
    path = settings_path()
    data, _ = _read()
    data.setdefault(section, {})
    if isinstance(value, str):
        value = value.strip()
    data[section][key] = value
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    global _cache, _cache_key
    _cache, _cache_key = None, None
    return value


def assets_dir(platform="android"):
    """The bundle directory in force for a platform.

    Order: the path set in settings.json, then the copy shipped beside the
    server, then -- for iOS only -- android's, so a half-populated tree degrades
    to the old behaviour instead of finding nothing. A configured path that does
    not exist falls through rather than being honoured, because serving 404s
    from a directory somebody moved is worse than serving the shipped set.
    """
    if platform == "ios":
        custom = get("assets", "dir_ios")
        if custom and os.path.isdir(custom):
            return custom
        if os.path.isdir(ASSETS_IOS_DIR):
            return ASSETS_IOS_DIR
        return ASSETS_DIR
    custom = get("assets", "dir")
    if custom and os.path.isdir(custom):
        return custom
    return ASSETS_DIR


def allow_replace():
    return bool(get("assets", "allow_replace", False))


def data_dir(*parts):
    """A writable folder under the server's data/ directory."""
    d = os.path.join(DATA_DIR, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def describe(platform="android"):
    """What the editor's Settings panel shows: the path and where it came from."""
    d = assets_dir(platform)
    configured = get("assets", "dir_ios" if platform == "ios" else "dir")
    if configured and os.path.normcase(os.path.abspath(d)) == os.path.normcase(
            os.path.abspath(configured)):
        source = "settings.json"
    elif platform == "ios" and os.path.isdir(ASSETS_IOS_DIR):
        source = "shipped iOS set"
    else:
        source = "shipped Android set"
    return {
        "platform": platform,
        "dir": d,
        "configured": bool(configured),
        "configured_dir": configured or "",
        "missing_configured": bool(configured) and not os.path.isdir(configured),
        "source": source,
        "allow_replace": allow_replace(),
        "settings_file": settings_path(),
    }
