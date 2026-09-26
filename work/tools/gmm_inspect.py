"""
gmm_inspect.py -- open captured Google Mobile Maps tile exchanges and show what is in them.

The launcher saves the first 40 map-tile requests/answers it passes through to Google
(Documents/Windstock/gmm/<ms>-<n>.req / .resp on the phone -- WSURLProtocol.m). Pull them
to a folder, then:

    python3 gmm_inspect.py <folder or .resp files>

For every tile in an answer (MapTileResponseProto.tile = 9: x=2, y=3, zoom=4,
payload=6) it decrypts the payload with the client's own cipher (work/server/gmm_crypt.py),
gunzips it if it's gzip, and walks the protobuf generically -- groups included -- printing a
per-tile summary: how many polylines (roads, GeometryProto group 3) and polygons (areas:
water, parks, buildings -- group 7) it holds, and any short strings. That answers "are there
water polygons in Google's tiles at all?" before anything is built on it.
"""
import collections
import glob
import gzip
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import gmm_crypt  # noqa: E402


def varint(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if not c & 0x80:
            return v, i
        s += 7


def fields(b):
    """(field, wiretype, value) over a protobuf message; groups returned as raw bytes.
    Raises on garbage, so callers can tell 'is this protobuf?'."""
    i, out, n = 0, [], len(b)
    while i < n:
        key, i = varint(b, i)
        fn, wt = key >> 3, key & 7
        if fn == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = varint(b, i)
            if i + ln > n:
                raise ValueError("overrun")
            v, i = b[i:i + ln], i + ln
        elif wt == 3:                              # start group: find its end
            start, depth = i, 1
            while depth:
                k, i = varint(b, i)
                f2, w2 = k >> 3, k & 7
                if w2 == 3:
                    depth += 1
                elif w2 == 4:
                    depth -= 1
                    if depth == 0:
                        end = i - len(bytes([k])) if k < 128 else i - 2
                        break
                elif w2 == 0:
                    _, i = varint(b, i)
                elif w2 == 1:
                    i += 8
                elif w2 == 2:
                    ln, i = varint(b, i)
                    i += ln
                elif w2 == 5:
                    i += 4
                else:
                    raise ValueError("bad wiretype in group")
            v = b[start:end]
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wiretype {wt}")
        if i > n:
            raise ValueError("overrun")
        out.append((fn, wt, v))
    return out


def is_proto(b):
    try:
        return bool(b) and bool(fields(b))
    except Exception:
        return False


def census(b, path="", depth=0, stats=None, strings=None):
    """Count every field path in a message tree."""
    stats = stats if stats is not None else collections.Counter()
    strings = strings if strings is not None else collections.Counter()
    if depth > 12:
        return stats, strings
    for fn, wt, v in fields(b):
        p = f"{path}.{fn}" + ("g" if wt == 3 else "")
        stats[p] += 1
        if wt in (2, 3) and isinstance(v, (bytes, bytearray)):
            if is_proto(v):
                census(v, p, depth + 1, stats, strings)
            elif 2 <= len(v) <= 40:
                try:
                    t = v.decode("utf-8")
                    if t.isprintable():
                        strings[t] += 1
                except UnicodeDecodeError:
                    pass
    return stats, strings


def unwrap(payload):
    for f in (lambda d: gzip.decompress(d), lambda d: zlib.decompress(d),
              lambda d: zlib.decompress(d, -15)):
        try:
            return f(payload)
        except Exception:
            pass
    return payload


def find_response(raw):
    """The answer may carry the same DriveAbout framing as the request; find the
    MapTileResponseProto by trying every offset for a message whose field 9s parse."""
    for off in range(0, min(len(raw), 4096)):
        chunk = raw[off:]
        try:
            fs = fields(chunk)
        except Exception:
            continue
        if sum(1 for f in fs if f[0] == 9 and f[1] == 2) >= 1:
            return off, fs
    return None, None


def inspect(path):
    raw = open(path, "rb").read()
    off, fs = find_response(raw)
    print(f"== {os.path.basename(path)}  {len(raw)} B", end="")
    if fs is None:
        print("  (no tile response found)")
        return
    print(f"  (response at +{off})")
    for fn, wt, v in fs:
        if fn != 9 or wt != 2:
            continue
        t = {f: val for f, w, val in fields(v)}
        x, y, z, payload = t.get(2), t.get(3), t.get(4), t.get(6, b"")
        plain = gmm_crypt.crypt(payload, x or 0, y or 0, z or 0) if payload else b""
        body = unwrap(plain)
        ok = is_proto(body)
        print(f"   tile x={x} y={y} z={z}  payload {len(payload)} B -> "
              f"{len(body)} B {'protobuf' if ok else 'NOT protobuf (cipher/format wrong?)'}")
        if ok:
            stats, strings = census(body)
            top = ", ".join(f"{k}x{n}" for k, n in stats.most_common(14))
            print(f"      fields: {top}")
            if strings:
                print(f"      strings: {', '.join(s for s, _ in strings.most_common(12))}")


def main(argv):
    files = []
    for a in argv or ["."]:
        files += sorted(glob.glob(os.path.join(a, "*.resp"))) if os.path.isdir(a) else [a]
    for f in files:
        inspect(f)


if __name__ == "__main__":
    main(sys.argv[1:])
