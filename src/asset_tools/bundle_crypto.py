"""bundle_crypto -- the Pokemon GO asset container format, as one implementation.

This is the canonical home for reading the asset digest and for the encrypted
bundle container, so that the asset editor (`asset_tools.assetlib`), the build
tools (`src/tools/decrypt_bundle.py`) and anything else all agree by
construction instead of by copy-paste. Nothing here imports UnityPy and nothing
here touches the network.

Encrypted file layout (reverse-engineered, verified on all 151 shipped Android
bundles):
    [0]        version byte (0x01)
    [1:17]     AES-128-CBC IV (16 bytes)
    [17:-20]   ciphertext (AES-128-CBC, PKCS7 padding)
    [-20:]     HMAC-SHA1 trailer over bytes [0:-20]
  AES key = digest_key(16 bytes)  XOR  KEY_MASK(16 bytes)
      KEY_MASK = b"PFAi$;]G7Rg>kz4w"
  -> AES-128-CBC decrypt, strip PKCS7 -> the plaintext is a `UnityFS` bundle
     whose header declares its own exact size (so `unityfs_size()` is a real
     check, not a guess).

The digest entry is AssetDigestEntry { asset_id=1, bundle_name=2, version=3,
checksum=4 fixed32, size=5 int32, key=6 bytes }. Field 4 matters: it is
CRC-32C of the ENCRYPTED file bytes, and it is present on all 151 shipped
Android entries, where it matches crc32c(file) 151 for 151. (An earlier note in
this codebase claimed the field was left unset and that no standard CRC variant
reproduced it. That was wrong, and it came from misreading pb.decode's output --
it returns a list of {field, wire, value} dicts, so a plain `4 in entry` presence
test silently says False. The value was there the whole time.) So bundles we
build advertise the same thing the genuine ones do.

Note the obvious trap this closes: CRC32(plaintext) is NOT it. That was tried in
the old work/server/protocol.py CRC_FIX experiment, did not make models render,
and is still disabled there.

A freshly built bundle is not in `asset_digest` at all -- that file is genuine
and shipped with the APK, so we never rewrite it. New bundles are registered in
`extra_digest.json` next to it and the server appends those entries to the real
digest at runtime (see windstock/game/protocol.py, which merges the manifest).
"""
import hashlib
import hmac
import json
import os
import struct
import time
import uuid

import pbcodec as pb

try:
    import crcmod.predefined
    from Crypto.Cipher import AES
except Exception as _e:                        # pragma: no cover
    crcmod = None
    AES = None
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

__all__ = ["missing_reason", "KEY_MASK", "DIGEST_FILE", "digest_key",
           "digest_entry", "aes_key_for", "decrypt", "encrypt", "hmac_ok",
           "crc32c", "unityfs_size", "is_bundle", "verify_encrypted",
           "verify_all", "extra_path", "load_extra", "save_extra", "register"]

KEY_MASK = b"\x50\x46\x41\x69\x24\x3B\x5D\x47\x37\x52\x67\x3E\x6B\x7A\x34\x77"
DIGEST_FILE = "asset_digest"
EXTRA_FILE = "extra_digest.json"

_crc32c = crcmod.predefined.mkCrcFun("crc-32c") if crcmod else None


def missing_reason():
    """Why the container codec is unusable here, or None if it is fine."""
    if _IMPORT_ERROR is not None:
        return ("this needs pycryptodome and crcmod, which are not installed "
                "(%s)" % _IMPORT_ERROR)
    return None


# --------------------------------------------------------------------------
# keys and the genuine digest
# --------------------------------------------------------------------------
def _digest_path(assets_dir):
    return os.path.join(assets_dir, DIGEST_FILE)


def _iter_entries(assets_dir):
    with open(_digest_path(assets_dir), "rb") as fh:
        data = fh.read()
    for raw in pb.get_all(pb.decode(data), 1):
        e = pb.decode(raw)
        yield e, pb.get(e, 2, pb.WT_LEN)


def digest_key(assets_dir, bundle_name):
    """Look up a bundle's 16-byte key from assets/asset_digest."""
    for e, name in _iter_entries(assets_dir):
        if name == bundle_name.encode():
            return pb.get(e, 6, pb.WT_LEN)
    raise KeyError("%s not in %s" % (bundle_name, _digest_path(assets_dir)))


def digest_entry(assets_dir, bundle_name):
    """The full digest entry for one bundle.

    Field 4 (`checksum`) is CRC-32C of the encrypted file, present and non-zero
    on every genuine entry. It still comes back as 0 for a hand-written or
    truncated entry that omits the field, because pb.get returns None for a
    missing field; `verify_encrypted` treats 0 as "nothing to compare" rather
    than as a checksum. See the module docstring.
    """
    for e, name in _iter_entries(assets_dir):
        if name == bundle_name.encode():
            return {
                "asset_id": pb.get(e, 1, pb.WT_LEN).decode("ascii", "replace"),
                "version": pb.get(e, 3, pb.WT_VARINT) or 0,
                "checksum": pb.get(e, 4, pb.WT_32) or 0,
                "size": pb.get(e, 5, pb.WT_VARINT) or 0,
                "key": pb.get(e, 6, pb.WT_LEN),
            }
    raise KeyError("%s not in %s" % (bundle_name, _digest_path(assets_dir)))


# --------------------------------------------------------------------------
# the encrypted container
# --------------------------------------------------------------------------
def aes_key_for(key16):
    return bytes(a ^ b for a, b in zip(key16, KEY_MASK))


def decrypt(enc_bytes, key16):
    """Encrypted bundle bytes -> plaintext UnityFS bundle bytes."""
    if not enc_bytes or enc_bytes[0] != 0x01:
        raise ValueError("not an encrypted bundle (version byte != 0x01)")
    aes_key = aes_key_for(key16)
    iv, ct = enc_bytes[1:17], enc_bytes[17:-20]
    if len(iv) != 16 or len(ct) < 16 or len(ct) % 16:
        raise ValueError("truncated bundle: %d bytes" % len(enc_bytes))
    pt = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ct)
    pad = pt[-1]
    if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
        pt = pt[:-pad]
    return pt


def encrypt(plain, key16):
    """Plaintext UnityFS bundle bytes -> the client's encrypted container."""
    aes_key = aes_key_for(key16)
    iv = os.urandom(16)
    pad = 16 - len(plain) % 16
    body = (b"\x01" + iv
            + AES.new(aes_key, AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad))
    return body + hmac.new(aes_key, body, hashlib.sha1).digest()


def hmac_ok(enc_bytes, key16):
    """Does the SHA1 trailer match the ciphertext?"""
    aes_key = aes_key_for(key16)
    return hmac.compare_digest(hmac.new(aes_key, enc_bytes[:-20],
                                        hashlib.sha1).digest(), enc_bytes[-20:])


def crc32c(data):
    return _crc32c(data) & 0xFFFFFFFF


def unityfs_size(pt):
    """Total bundle size declared in the UnityFS header (sanity check)."""
    o = 12                                   # after "UnityFS\0" + int32 format
    for _ in range(2):                       # skip unityVersion + unityRevision
        while pt[o]:
            o += 1
        o += 1
    return struct.unpack(">q", pt[o:o + 8])[0]


def is_bundle(data):
    return data[:8].startswith(b"UnityFS")


def verify_encrypted(enc_bytes, key16, expect_checksum=None, expect_size=None):
    """Full check of one encrypted bundle. Returns a list of problems.

    `expect_checksum` is compared when non-zero. Every genuine entry has one
    (crc32c of the encrypted bytes), so this is normally a real comparison; the
    zero guard only exists so a hand-written entry that omits field 4 does not
    fail the whole directory. See the module docstring.
    """
    problems = []
    if not enc_bytes or enc_bytes[0] != 0x01:
        return ["version byte is 0x%02x, expected 0x01"
                % (enc_bytes[0] if enc_bytes else 0)]
    if not hmac_ok(enc_bytes, key16):
        problems.append("HMAC-SHA1 mismatch")
    try:
        pt = decrypt(enc_bytes, key16)
    except Exception as e:
        return problems + ["decrypt failed: %s" % e]
    if not is_bundle(pt):
        problems.append("plaintext is not UnityFS (%r)" % pt[:8])
    else:
        declared = unityfs_size(pt)
        if declared != len(pt):
            problems.append("UnityFS declares %d bytes, file is %d"
                            % (declared, len(pt)))
    if expect_size and expect_size != len(enc_bytes):
        problems.append("digest size %d != file %d" % (expect_size,
                                                       len(enc_bytes)))
    if expect_checksum:
        got = crc32c(enc_bytes)
        if got != expect_checksum:
            problems.append("CRC-32C %#010x != advertised %#010x"
                            % (got, expect_checksum))
    return problems


def verify_all(assets_dir, quiet=False, out=None):
    """Check every bundle in the directory. Returns (ok, failures).

    Genuine bundles are checked against asset_digest; bundles we built are
    checked against their extra_digest.json entry, with their own key. Both are
    real checks -- a variant whose key was written wrong, or whose bytes changed
    after registration, is exactly the failure worth catching, and leaving
    variants out of this loop (as an earlier version did) reported "verified"
    while silently skipping every file the editor had produced.
    """
    say = out or (lambda *a: None)
    extra = load_extra(assets_dir)
    names = sorted(n for n in os.listdir(assets_dir)
                   if n.startswith("pm") and not n.endswith(".json"))
    bad = []
    for n in names:
        with open(os.path.join(assets_dir, n), "rb") as fh:
            enc = fh.read()
        x = extra.get(n)
        if x:
            key = bytes.fromhex(x["key"])
            checksum, size, kind = x.get("checksum", 0), x.get("size", 0), "extra"
        else:
            try:
                e = digest_entry(assets_dir, n)
            except KeyError as ex:
                bad.append((n, [str(ex) + " (in no digest at all)"]))
                continue
            key, checksum, size, kind = e["key"], e["checksum"], e["size"], "asset_digest"
        problems = verify_encrypted(enc, key, checksum, size)
        if problems:
            bad.append((n, problems))
        elif not quiet:
            say("  %s: OK  %d B  %s checksum %s"
                % (n, len(enc), kind, hex(checksum)))
    if not quiet:
        say("%d/%d bundles verified (%d genuine, %d built here)"
            % (len(names) - len(bad), len(names),
               len(names) - len(extra), len(extra)))
    return (not bad), bad


# --------------------------------------------------------------------------
# extra_digest.json -- the registry for bundles we build
# --------------------------------------------------------------------------
def extra_path(assets_dir):
    return os.path.join(assets_dir, EXTRA_FILE)


def load_extra(assets_dir):
    p = extra_path(assets_dir)
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_extra(assets_dir, manifest):
    """Write extra_digest.json atomically (temp + rename), sorted for diffs.

    The temp name carries the pid *and* a counter: two saves inside one process
    would otherwise race on a single ".tmp" name, and the loser would leave the
    manifest half-written.
    """
    from asset_tools import unitypy_fix

    p = extra_path(assets_dir)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = unitypy_fix.tmp_path(p, "extra")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def register(assets_dir, name, enc_bytes, key16, manifest=None):
    """Add a freshly built bundle to extra_digest.json and return its entry.

    `version` is microseconds since the epoch, matching the genuine digest, and
    the asset_id is "<uuid>/<version>" for the same reason. A new key is used per
    bundle: the client's only source of a bundle's key is the digest, so a key
    chosen here is authoritative, and reusing the source bundle's key would make
    the two indistinguishable to any cache in between.
    """
    manifest = load_extra(assets_dir) if manifest is None else manifest
    version = int(time.time() * 1_000_000)
    entry = {
        "asset_id": "%s/%d" % (uuid.uuid4(), version),
        "bundle_name": name,
        "version": version,
        "checksum": crc32c(enc_bytes),
        "size": len(enc_bytes),
        "key": key16.hex(),
    }
    manifest[name] = entry
    save_extra(assets_dir, manifest)
    return entry
