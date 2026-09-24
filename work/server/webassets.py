"""
Static files we ship ourselves -- currently Leaflet.

Both the World Manager (admin.py, its own port) and the game server (server.py)
serve this, because the phone's in-game map page is loaded from the game
server's origin while the World Manager runs on another. Vendored rather than
pulled from a CDN: the phone runs this whole stack with no internet, and a
missing map library used to take a page's JavaScript down with it.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "webassets")

TYPES = {".js": "application/javascript", ".css": "text/css", ".png": "image/png",
         ".svg": "image/svg+xml", ".ttf": "font/ttf"}


def handle(path):
    """(status, headers, body) for a /webassets/... path, whoever is serving it."""
    rel = os.path.normpath(path[len("/webassets/"):].split("?")[0]).lstrip("./")
    full = os.path.join(ROOT, rel)
    if not os.path.abspath(full).startswith(os.path.abspath(ROOT) + os.sep):
        return 404, {"Content-Type": "text/plain"}, b"no"      # no climbing out
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return 404, {"Content-Type": "text/plain"}, b"no such asset"
    ctype = TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
    return 200, {"Content-Type": ctype, "Cache-Control": "max-age=86400"}, data
