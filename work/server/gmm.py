"""
Google Mobile Maps tiles -- http://mobilemaps.clients.google.com/glm/mmap

This is where the roads come from. The client's map is drawn by Niantic's own
tile layer (nia::map::MapTileManager -> MapTileFetcher -> gmm::TileParser),
which fetches vector tiles from Google's "mmap" endpoint and decodes them with
a protobuf schema compiled into the binary as gmm_tile.pb.cc. Niantic's 2016
API key is long dead, so every one of those requests fails and the ground
renders as flat green -- no roads, no water, no buildings.

Until now the request was not even redirected: mobilemaps.clients.google.com
was missing from the launcher's hostname list, so it went to the real Google
and quietly 4xx'd. It lands here instead now.

What this module does TODAY is capture. Serving OSM roads means writing tiles
in Google's format, and that format is not recoverable by inspection:
gmm_tile.pb.cc is built against protobuf-lite, so there is no embedded
FileDescriptorProto to pull the schema out of (checked -- zero hits for
"gmm_tile.proto" in the binary). Working it out means reading what the client
actually asks for and what TileParser does with the answer, and step one of
that is having the requests on disk.

So: log every request, keep the bodies under data/gmm_captures/, and answer in
a way that does not wedge the client. Set GMM_CAPTURE=0 to stop writing files.
"""
import math
import os
import time

import datadir

CAPTURE_DIR = os.path.join(datadir.DATA_DIR, "gmm_captures")
_MAX_CAPTURES = 200
_seen = [0]


def _capture(method, path, query, body):
    """Keep the raw request. Capped, because the client asks constantly and
    this is a debugging aid, not a log we want eating the container."""
    if os.environ.get("GMM_CAPTURE") == "0":
        return None
    if _seen[0] >= _MAX_CAPTURES:
        return None
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{_seen[0]:03d}.bin"
        with open(os.path.join(CAPTURE_DIR, name), "wb") as fh:
            fh.write(f"{method} {path}?{query}\n".encode("utf-8", "replace"))
            fh.write(b"----\n")
            fh.write(body or b"")
        _seen[0] += 1
        return name
    except OSError:
        return None


# ---------------------------------------------------------------- request

# The body is not protobuf from byte zero: there is a DriveAbout framing header
# first -- a version word, then 16-bit length-prefixed strings (locale, client
# name, client version, platform), then two tagged blocks. The tile request is
# the second one. Rather than parse the framing exactly, find the protobuf by
# the marker that ends the fixed part, which is stable across every capture.
_PROTO_MARK = b"SYSTEM"


def _varint(b, i):
    v = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if not c & 0x80:
            return v, i
        s += 7
    raise ValueError("truncated varint")


def parse_request(body):
    """-> [{"x":, "y":, "z":, "kind":}] for every tile asked for.

    TileRequest { kind=1, x=2, y=3, zoom=4 }, repeated in field 9 of the
    payload. Verified against live captures: zoom is always 17 and x/y are
    ordinary Web Mercator tile numbers -- the same scheme any OSM tile server
    uses, which is what makes serving these from OSM data conceivable at all.
    """
    if not body:
        return []
    mark = body.find(_PROTO_MARK)
    if mark < 0:
        return []
    pb = body[mark + len(_PROTO_MARK) + 6:]      # skip the block header too
    i = 0
    tiles = []
    while i < len(pb):
        try:
            key, i = _varint(pb, i)
        except ValueError:
            break
        fn, wt = key >> 3, key & 7
        if wt == 2:
            try:
                ln, i = _varint(pb, i)
            except ValueError:
                break
            sub, i = pb[i:i + ln], i + ln
            if fn == 9:
                j, d = 0, {}
                try:
                    while j < len(sub):
                        k, j = _varint(sub, j)
                        v, j = _varint(sub, j)
                        d[k >> 3] = v
                except ValueError:
                    continue
                if 2 in d and 3 in d:
                    tiles.append({"kind": d.get(1), "x": d[2], "y": d[3],
                                  "z": d.get(4, 17)})
        elif wt == 0:
            try:
                _, i = _varint(pb, i)
            except ValueError:
                break
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return tiles


def tile_to_latlng(x, y, z):
    n = 2.0 ** z
    lng = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lng


def handle(method, path, query, headers, body, log):
    """Answer a tile request.

    An empty 200 is deliberate. The client treats a failed fetch and an empty
    tile differently: a failure is retried forever (which is what the three
    requests per session in the device log were), an empty tile is accepted
    and cached, so the map settles instead of hammering. Neither draws roads
    yet -- that needs real tile bytes.
    """
    name = _capture(method, path, query, body)
    tiles = parse_request(body)
    if tiles:
        zs = {t["z"] for t in tiles}
        xs = [t["x"] for t in tiles]
        ys = [t["y"] for t in tiles]
        z = max(zs)
        lat, lng = tile_to_latlng(sum(xs) / len(xs) + 0.5,
                                  sum(ys) / len(ys) + 0.5, z)
        log(f"[gmm] {len(tiles)} tiles z{sorted(zs)} "
            f"x {min(xs)}-{max(xs)} y {min(ys)}-{max(ys)} "
            f"~({lat:.5f},{lng:.5f})"
            + (f"  saved {name}" if name else ""))
    else:
        log(f"[gmm] {method} {path}  body={len(body or b'')}B (unparsed)"
            + (f"  saved {name}" if name else ""))
    return 200, {"Content-Type": "application/binary"}, b""
