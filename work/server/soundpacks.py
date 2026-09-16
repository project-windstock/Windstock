"""
Sound pack builder -- the backend for the World Manager's Sound Packs page, and the
place the windstock tweak downloads packs from.

A pack is a folder data/soundpacks/<pack>/ of audio files named after the game's own
sound clips (<clip>.mp3 / .m4a / .wav / .caf / .aac / .aif / .aiff) -- exactly the
layout the tweak reads from Documents/SoundPacks on the phone, so a pack built here
is copied over as-is. soundpack_catalogue.json lists every clip the client plays,
grouped (music, catching, map, stops, eggs, menus, cries, moves).

World Manager (localhost only):  admin.py routes /soundpacks and /api/sp/*
Phone (game host, over HTTPS):   GET /soundpacks/index.json
                                 GET /soundpacks/file/<pack>/<file>
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.parse

import datadir

HERE = os.path.dirname(os.path.abspath(__file__))
PACKS_DIR = datadir.path("soundpacks")
AUDIO_EXTS = ("mp3", "m4a", "wav", "caf", "aac", "aif", "aiff")
MAX_FILE_BYTES = 20 * 1024 * 1024

_lock = threading.Lock()
_catalogue = None


def catalogue():
    global _catalogue
    if _catalogue is None:
        with open(os.path.join(HERE, "soundpack_catalogue.json"), encoding="utf-8") as fh:
            _catalogue = json.load(fh)
    return _catalogue


def _known_clips():
    return {c["clip"] for g in catalogue()["groups"] for c in g["clips"]}


def _safe_pack(name):
    """A pack name that is safe as a folder name (letters, digits, space, - _ .)."""
    name = re.sub(r"[^A-Za-z0-9 _\-.]", "", str(name or "")).strip().strip(".")
    return name[:40]


def _pack_dir(pack):
    p = _safe_pack(pack)
    return os.path.join(PACKS_DIR, p) if p else None


def packs():
    """[{name, clips: {clip: {file, size, mtime}}, bytes}] for every pack."""
    os.makedirs(PACKS_DIR, exist_ok=True)
    out = []
    for name in sorted(os.listdir(PACKS_DIR), key=str.lower):
        d = os.path.join(PACKS_DIR, name)
        if not os.path.isdir(d):
            continue
        clips, total = {}, 0
        for f in sorted(os.listdir(d)):
            stem, _, ext = f.rpartition(".")
            if ext.lower() not in AUDIO_EXTS or not stem:
                continue
            st = os.stat(os.path.join(d, f))
            clips[stem] = {"file": f, "size": st.st_size, "mtime": int(st.st_mtime)}
            total += st.st_size
        out.append({"name": name, "clips": clips, "bytes": total})
    return out


def create(pack):
    d = _pack_dir(pack)
    if not d:
        return False, "Pick a name (letters, numbers, spaces)."
    if os.path.isdir(d):
        return False, "A pack with that name already exists."
    os.makedirs(d)
    return True, f"Created {os.path.basename(d)}."


def delete(pack):
    d = _pack_dir(pack)
    if not d or not os.path.isdir(d):
        return False, "No such pack."
    shutil.rmtree(d)
    return True, f"Deleted {os.path.basename(d)}."


def rename(pack, new_name):
    d, nd = _pack_dir(pack), _pack_dir(new_name)
    if not d or not os.path.isdir(d):
        return False, "No such pack."
    if not nd:
        return False, "Pick a name (letters, numbers, spaces)."
    if os.path.exists(nd):
        return False, "A pack with that name already exists."
    os.rename(d, nd)
    return True, f"Renamed to {os.path.basename(nd)}."


def put_clip(pack, clip, filename, data):
    """Store one uploaded file as <clip>.<ext>, replacing any earlier version."""
    d = _pack_dir(pack)
    if not d or not os.path.isdir(d):
        return False, "No such pack."
    if clip not in _known_clips():
        return False, f"Unknown sound {clip!r}."
    ext = (filename or "").rpartition(".")[2].lower()
    if ext not in AUDIO_EXTS:
        return False, "Use an mp3, m4a, wav, caf, aac or aiff file."
    if not data:
        return False, "That file is empty."
    if len(data) > MAX_FILE_BYTES:
        return False, "That file is over 20 MB."
    with _lock:
        for e in AUDIO_EXTS:                              # one file per clip
            old = os.path.join(d, f"{clip}.{e}")
            if os.path.exists(old):
                os.remove(old)
        tmp = os.path.join(d, f".{clip}.{ext}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(d, f"{clip}.{ext}"))
    return True, f"Saved {clip}.{ext}"


def remove_clip(pack, clip):
    d = _pack_dir(pack)
    if not d or not os.path.isdir(d):
        return False, "No such pack."
    gone = False
    for e in AUDIO_EXTS:
        f = os.path.join(d, f"{clip}.{e}")
        if os.path.exists(f):
            os.remove(f)
            gone = True
    return gone, ("Removed." if gone else "Nothing to remove.")


def file_path(pack, filename):
    """The on-disk path of one pack file, or None (never escapes the pack folder)."""
    d = _pack_dir(pack)
    if not d:
        return None
    base = os.path.basename(filename or "")
    stem, _, ext = base.rpartition(".")
    if not stem or ext.lower() not in AUDIO_EXTS:
        return None
    f = os.path.join(d, base)
    return f if os.path.isfile(f) else None


def content_type(filename):
    ext = filename.rpartition(".")[2].lower()
    return {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
            "wav": "audio/wav", "caf": "audio/x-caf",
            "aif": "audio/aiff", "aiff": "audio/aiff"}.get(ext, "application/octet-stream")


def handle(method, path, query, headers, body, log):
    """Phone-facing routes on the game host (the tweak's "Download packs")."""
    if path == "/soundpacks/index.json":
        idx = {"generated": int(time.time()),
               "packs": [{"name": p["name"], "bytes": p["bytes"],
                          "files": [dict(c, clip=k) for k, c in p["clips"].items()]}
                         for p in packs()]}
        log(f"[soundpacks] phone asked for the pack list ({len(idx['packs'])} pack(s))")
        return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
            json.dumps(idx).encode("utf-8")
    if path.startswith("/soundpacks/file/"):
        rest = urllib.parse.unquote(path[len("/soundpacks/file/"):])
        pack, _, fname = rest.partition("/")
        f = file_path(pack, fname)
        if not f:
            return 404, {"Content-Type": "text/plain"}, b"no such file"
        with open(f, "rb") as fh:
            data = fh.read()
        return 200, {"Content-Type": content_type(f)}, data
    return 404, {"Content-Type": "text/plain"}, b"?"
