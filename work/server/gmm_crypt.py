"""
TileCrypt -- the cipher on Google Mobile Maps tile payloads.

Recovered from nia::map::gmm::parse::TileCryptInputStream in the 0.35 client
(arm64, stripped; found via its RTTI descriptor -> vtable -> Next()). It is
RC4 with a 256-round drop, keyed by a 40-byte block built from a baked-in
16-byte secret and the tile's own coordinates.

  TileCryptInputStream::Next   0x1015cbe40   (vtable slot 2)
  PRGA                         0x101661268   (only caller: Next)
  ctor / KSA                   0x1015cbc54
  key builder                  0x1016612c0 -> 0x1016614a8
  identity S-box constant      0x101b13636  (256 bytes, 00..ff)
  obfuscated secret            0x101b13736 (A) and 0x101b13746 (B)

The S-box starts as a memcpy of a constant rather than a fill loop, which
looks like a custom permutation until you check it -- it is plain 0..255, so
the KSA is textbook.

NOT VERIFIED END TO END. Every step here is read off the disassembly, and
there is no captured ciphertext to check it against: Google never answered us,
so we have never held a real encrypted tile. The RC4 core round-trips with
itself, which says the transform is self-consistent, not that it is right.
"""
import struct

# key[k] = (A[k] * 47) ^ B[k], unrolled in the binary over 16 bytes.
_A = bytes.fromhex("5aee039d0ed76ab2743f36502887e0ef")
_B = bytes.fromhex("d97d3d5c26a994b9440e2bdef005c159")
SECRET = bytes(((a * 47) & 0xFF) ^ b for a, b in zip(_A, _B))
assert SECRET.hex() == "5fcfb08fb4d0e217089fc16ea8cce1b8"

# The 4th key field is a literal 9 at the call site -- a protocol/format
# version, not anything we get to choose.
FORMAT_VERSION = 9


def tile_key(x, y, zoom, session=0, cookie=0):
    """The 40-byte RC4 key for one tile.

    Field order and widths are exactly what the builder emits, big-endian
    throughout:

        secret(16) y(u32) zoom(u32) x(u32) version(u16) session(u16) cookie(u64)

    `session` and `cookie` are the constructor's last two arguments. They come
    from the response/cookie exchange, which means WE choose them when we are
    the server -- leaving both 0 is a valid choice and makes the key depend on
    nothing but the tile.
    """
    return (SECRET
            + struct.pack(">I", y & 0xFFFFFFFF)
            + struct.pack(">I", zoom & 0xFFFFFFFF)
            + struct.pack(">I", x & 0xFFFFFFFF)
            + struct.pack(">H", FORMAT_VERSION & 0xFFFF)
            + struct.pack(">H", session & 0xFFFF)
            + struct.pack(">Q", cookie & 0xFFFFFFFFFFFFFFFF))


def _ksa(key):
    s = list(range(256))            # the constant at 0x101b13636
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    return s


def _drop(s, n=256):
    """The client runs the PRGA n times and throws the output away, keeping
    i and j. Skipping this is the easiest way to get a stream that looks
    plausible and decrypts to noise."""
    i = j = 0
    for _ in range(n):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
    return i, j


def crypt(data, x, y, zoom, session=0, cookie=0):
    """RC4 is symmetric, so this both decrypts a Google tile and encrypts one
    of ours."""
    s = _ksa(tile_key(x, y, zoom, session, cookie))
    i, j = _drop(s)
    out = bytearray(len(data))
    for n, c in enumerate(data):
        i = (i + 1) & 0xFF
        a = s[i]
        j = (j + a) & 0xFF
        b = s[j]
        s[i], s[j] = b, a
        out[n] = c ^ s[(a + b) & 0xFF]
    return bytes(out)
