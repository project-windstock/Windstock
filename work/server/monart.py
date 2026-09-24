"""
Pokemon art for the radars -- the little sprites that stand on the map.

The art is the set Research already ships, research_art/pokemon/<dex>.png, read
in place rather than copied: one folder, one answer, and the iOS staging script
already carries it onto the phone.

A file in datadir monart/ overrides it, so a better sprite can be dropped in
beside the save data without touching the install. Leave a species out of both
and the radar falls back to a lettered dot -- a half-filled folder is a
perfectly good folder.

Served by BOTH radars from their own origin (the World Manager on localhost, the
Help Center on the game host), because the phone cannot reach the manager's port.
"""
import os
import re
import struct

import datadir

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "research_art", "pokemon")
# A player can add art without touching the install, the same way photos work.
USER_DIR = datadir.path("monart")


def _dirs():
    return [d for d in (USER_DIR, DIR) if os.path.isdir(d)]


def have():
    """Dex numbers with art, as a set -- what the radar may draw as a sprite."""
    return set(sizes())


def _png_size(path):
    """(width, height) from the PNG header alone -- 24 bytes, no decoding."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", head[16:24])
    except (OSError, struct.error):
        return None


_SIZES = {}


def sizes():
    """{dex: (width, height)} for every sprite we have.

    The radar needs the shape of each one: the art is cropped tight to the
    Pokemon, and they are wildly different shapes (Fearow is 244x200, Magnemite
    143x60, Bulbasaur 77x92). Fitting them all into one square box makes a flat
    wide one render as a sliver and a tall one tower over it, so the page scales
    each by these dimensions instead. Read once -- the files do not change under
    a running server.
    """
    if _SIZES:
        return _SIZES
    for d in reversed(_dirs()):          # user art last, so it wins
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = re.fullmatch(r"0*(\d{1,4})\.png", n, re.IGNORECASE)
            if not m:
                continue
            wh = _png_size(os.path.join(d, n))
            if wh and wh[0] and wh[1]:
                _SIZES[int(m.group(1))] = [int(wh[0]), int(wh[1])]
    return _SIZES


def file_for(pokemon_id):
    """Where this species' PNG lives, or None. A player-supplied file wins, so a
    better sprite can be dropped in beside the save data without replacing ours."""
    try:
        pid = int(pokemon_id)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    # Research names them 25.png, an override may be 025.png -- accept either.
    for d in _dirs():
        for name in ("%03d.png" % pid, "%d.png" % pid):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                return fp
    return None


def serve(pokemon_id):
    """(status, headers, body) for one sprite -- the shape both servers return."""
    fp = file_for(pokemon_id)
    if not fp:
        return 404, {"Content-Type": "text/plain"}, b"no art"
    with open(fp, "rb") as fh:
        # Art never changes under a given number, so let the page keep it.
        return 200, {"Content-Type": "image/png",
                     "Cache-Control": "public, max-age=86400"}, fh.read()
