"""
Self-check for the per-species model override (assets.model_overrides).

Builds a SYNTHETIC assets directory in a temp folder -- a genuine-looking
`asset_digest` with two invented bundles, plus one custom variant registered in
`extra_digest.json` -- and then proves the whole chain switches together:

    1. the digest entry keeps the STOCK asset_id and bundle_name
    2. ...but advertises the CUSTOM checksum / size / key
    3. the download-URL entry quotes those same custom numbers
    4. GET /asset/<that id> serves the custom FILE
    5. with the override cleared, all four go back to stock
    6. a name that is not a real bundle is never advertised

Nothing here touches the shipped assets or the real settings.json -- the assets
directory and the settings lookup are both redirected into the temp folder.

    py src/server/windstock/tools/test_model_overrides.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from windstock.game import pb                                    # noqa: E402
from windstock.game import protocol as P                         # noqa: E402

STOCK_KEY = bytes(range(16))
CUSTOM_KEY = bytes(range(16, 32))
STOCK_CRC, CUSTOM_CRC = 0x11111111, 0x22222222


def _build_fake_assets(root):
    """A genuine-looking digest for two species, one custom variant, and the
    files themselves. Sizes are the real byte lengths so a size check means
    something."""
    os.makedirs(root, exist_ok=True)

    def entry(asset_id, name, crc, size, key, version=1467337882194000):
        return P.build_asset_digest_entry(asset_id, name, version=version,
                                         checksum=crc, size=size, key=key)

    p1 = os.path.join(root, "pm0001")
    p25 = os.path.join(root, "pm0025")
    p25s = os.path.join(root, "pm0025_s")
    for p, n in ((p1, 111), (p25, 222), (p25s, 333)):
        with open(p, "wb") as fh:
            fh.write(b"\x01" + b"\xAB" * (n - 1))

    digest = (pb.Writer()
              .message(1, entry("guid-bulbasaur/1467337882194000", "pm0001",
                                STOCK_CRC, os.path.getsize(p1), STOCK_KEY))
              .message(1, entry("guid-pikachu/1467337882194001", "pm0025",
                                STOCK_CRC, os.path.getsize(p25), STOCK_KEY))
              .uint(2, 1467338276561000)          # the digest's own timestamp
              .uint(3, 1)
              .to_bytes())
    with open(os.path.join(root, "asset_digest"), "wb") as fh:
        fh.write(digest)

    with open(os.path.join(root, "extra_digest.json"), "w", encoding="utf-8") as fh:
        json.dump({"pm0025_s": {
            "asset_id": "guid-custom-pikachu/1790405314000000",
            "version": 1790405314000000,
            "checksum": CUSTOM_CRC,
            "size": os.path.getsize(p25s),
            "key": CUSTOM_KEY.hex(),
        }}, fh, indent=1)
    return {"pm0001": p1, "pm0025": p25, "pm0025_s": p25s}


def _entry_for(data, bundle_name):
    """The AssetDigestEntry for one bundle, as a field dict."""
    for raw in pb.get_all(pb.decode(data), 1):
        e = pb.decode(raw)
        n = pb.get(e, 2, pb.WT_LEN)
        if isinstance(n, bytes) and n.decode() == bundle_name:
            return e
    return None


def main():
    tmp = tempfile.mkdtemp(prefix="model_override_")
    files = _build_fake_assets(tmp)
    P.assets_dir = lambda platform="android": tmp
    P._EXTRA_DIGEST.clear()
    P._REAL_DIGEST.clear()
    P._DIGEST_RESPONSE.clear()
    P._RAW_DIGEST_BYTES.clear()
    P._DIGEST_TS.clear()

    overrides = {}
    real_get = P._cfg.get          # the genuine settings.get, before patching

    def fake_get(section, key, env=None, cast=None):
        if section == "assets" and key == "model_overrides":
            return dict(overrides)
        if section == "assets" and key == "model_overrides_ts":
            return 1800000000000000        # microseconds, past the shipped ts
        return P._cfg.get(section, key, env, cast)

    P._cfg.get = fake_get
    P.clear_digest_cache()
    bad = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}"
              + ("" if ok else f"  (want {want!r})"))
        if not ok:
            bad.append(label)

    def digest_for(pid):
        return P.build_get_asset_digest_response("android")

    print("stock (no override)")
    d = digest_for(25)
    e = _entry_for(d, "pm0025")
    check("asset_id unchanged", pb.get(e, 1, pb.WT_LEN), b"guid-pikachu/1467337882194001")
    check("checksum is stock", pb.get(e, 4, pb.WT_32), STOCK_CRC)
    check("served file is stock", os.path.basename(
        P.bundle_path(b"guid-pikachu/1467337882194001")), "pm0025")
    check("self-check", P.check_digest_response(d, "android"), [])
    check("override map empty", P._model_override_map("android"), {})

    print("\nwith 25 -> pm0025_s")
    overrides[25] = "pm0025_s"
    P.clear_digest_cache()
    d = digest_for(25)
    e = _entry_for(d, "pm0025")
    check("asset_id STILL the stock one", pb.get(e, 1, pb.WT_LEN),
          b"guid-pikachu/1467337882194001")
    check("bundle_name unchanged", pb.get(e, 2, pb.WT_LEN), b"pm0025")
    check("checksum is the custom one", pb.get(e, 4, pb.WT_32), CUSTOM_CRC)
    check("size is the custom one", pb.get(e, 5, pb.WT_VARINT),
          os.path.getsize(files["pm0025_s"]))
    check("key is the custom one", pb.get(e, 6, pb.WT_LEN), CUSTOM_KEY)
    check("exactly one pm0025 entry",
          sum(1 for raw in pb.get_all(pb.decode(d), 1)
              if pb.get(pb.decode(raw), 2, pb.WT_LEN) == b"pm0025"), 1)
    check("served file is the CUSTOM one", os.path.basename(
        P.bundle_path(b"guid-pikachu/1467337882194001")), "pm0025_s")
    check("self-check tolerates the rewrite", P.check_digest_response(d, "android"), [])

    urls = P.build_get_download_urls_response(
        ["guid-pikachu/1467337882194001", "guid-bulbasaur/1467337882194000"], "u")
    entries = [pb.decode(x) for x in pb.get_all(pb.decode(urls), 1)]
    check("download entry carries the custom size", pb.get(entries[0], 3, pb.WT_VARINT),
          os.path.getsize(files["pm0025_s"]))
    check("download entry carries the custom checksum",
          pb.get(entries[0], 4, pb.WT_VARINT), CUSTOM_CRC)
    check("an untouched species keeps its stock checksum",
          pb.get(entries[1], 4, pb.WT_VARINT), STOCK_CRC)

    import time
    check("digest timestamp moved past the shipped one",
          P.digest_timestamp("android") > 1467338276561000, True)
    check("timestamp is the override's own value",
          P.digest_timestamp("android"), 1800000000000000)

    print("\ncleared again")
    overrides.clear()
    P.clear_digest_cache()
    d = digest_for(25)
    e = _entry_for(d, "pm0025")
    check("checksum back to stock", pb.get(e, 4, pb.WT_32), STOCK_CRC)
    check("served file back to stock", os.path.basename(
        P.bundle_path(b"guid-pikachu/1467337882194001")), "pm0025")
    check("self-check", P.check_digest_response(d, "android"), [])

    print("\na bundle that does not exist")
    overrides[25] = "pm0025_nope"
    P.clear_digest_cache()
    check("not advertised", P._model_override_map("android"), {})
    d = digest_for(25)
    check("stock checksum untouched", pb.get(_entry_for(d, "pm0025"), 4, pb.WT_32),
          STOCK_CRC)
    check("self-check", P.check_digest_response(d, "android"), [])

    print("\nWorld Manager round-trip (real endpoints, temp settings.json)")
    _admin_round_trip(tmp, bad, real_get)

    print()
    if bad:
        print(f"FAILED: {len(bad)} check(s): " + ", ".join(bad))
        return 1
    print("all checks passed")
    return 0


class _Stub:
    """Just enough of the admin's request handler to call do_GET/do_POST.

    Built off the real handler class so its own _json/_send are exercised (they
    are what the browser actually goes through), with the three socket-level
    methods replaced by recorders.
    """
    def __new__(cls, path, body=None):
        import io
        from windstock.web import admin as _A
        self = _A._Handler.__new__(_A._Handler)
        self.path = path
        self.headers = {"Content-Length": str(len(body or b""))}
        self.rfile = io.BytesIO(body or b"")
        self.wfile = io.BytesIO()
        self.code = None
        self.send_response = lambda code: setattr(self, "code", code)
        self.send_header = lambda *a: None
        self.end_headers = lambda: None
        # attached here rather than as a method: __new__ returns a _Handler, not
        # a _Stub, so methods on this class would never be found.
        self.sent = lambda: json.loads(self.wfile.getvalue() or b"{}")
        return self


def _admin_round_trip(tmp, bad, real_get):
    """Drive the real World Manager endpoints: GET /api/models, then POST
    /api/model, and check that the digest and the served bytes follow with NO
    fake settings in play -- everything is read back out of a temp
    settings.json, so the real one is never written.
    """
    from windstock.config import settings as _S
    from windstock.web import admin as _A

    saved_file = _S.SETTINGS_FILE
    _S.SETTINGS_FILE = os.path.join(tmp, "settings.json")
    # P._cfg IS the settings module, so undoing main()'s patch means putting the
    # real function back on it -- `P._cfg.get = _S.get` would be a no-op.
    _S.get = real_get
    try:
        g = _Stub("/api/models")
        _A._Handler.do_GET(g)
        j = g.sent()
        names = [b["name"] for b in j["bundles"]["android"]]
        ok = "pm0025_s" in names
        print(f"  {'ok  ' if ok else 'FAIL'} /api/models lists the built bundle: {names}")
        if not ok:
            bad.append("/api/models bundles")
        ok = not j["overrides"]
        print(f"  {'ok  ' if ok else 'FAIL'} starts with no overrides: {j['overrides']}")
        if not ok:
            bad.append("/api/models overrides")

        def post(payload):
            s = _Stub("/api/model", json.dumps(payload).encode())
            _A._Handler.do_POST(s)
            return s.sent()

        for label, payload, why in (
                ("a bundle that was never built is refused",
                 {"pokemon_id": 25, "bundle": "pm0025_nope"}, "refuse unknown bundle"),
                ("'the normal bundle' is refused as a no-op",
                 {"pokemon_id": 25, "bundle": "pm0025"}, "refuse normal bundle"),
                ("a non-Kanto number is refused",
                 {"pokemon_id": 999, "bundle": "pm0025_s"}, "refuse bad species")):
            r = post(payload)
            ok = not r.get("ok")
            print(f"  {'ok  ' if ok else 'FAIL'} {label}")
            if not ok:
                bad.append(why)
        # ...and none of those refusals left anything behind.
        ok = not _S.get("assets", "model_overrides")
        print(f"  {'ok  ' if ok else 'FAIL'} a refused change wrote nothing: "
              f"{_S.get('assets', 'model_overrides')}")
        if not ok:
            bad.append("refused change persisted")

        r = post({"pokemon_id": 25, "bundle": "pm0025_s"})
        ok = bool(r.get("ok"))
        print(f"  {'ok  ' if ok else 'FAIL'} applying it: {r.get('message') or r}")
        if not ok:
            bad.append("apply override")
            return

        # The whole chain, read back from the temp settings file, with no
        # manual cache clearing beyond what the endpoint itself did.
        d = P.build_get_asset_digest_response("android")
        e = _entry_for(d, "pm0025")
        for label, got, want in (
                ("settings really hold it",
                 _S.get("assets", "model_overrides"), {"25": "pm0025_s"}),
                ("digest advertises the custom checksum",
                 pb.get(e, 4, pb.WT_32), CUSTOM_CRC),
                ("asset_id is still the stock one",
                 pb.get(e, 1, pb.WT_LEN), b"guid-pikachu/1467337882194001"),
                ("download serves the custom file", os.path.basename(
                    P.bundle_path(b"guid-pikachu/1467337882194001")), "pm0025_s"),
                ("self-check still clean",
                 P.check_digest_response(d, "android"), [])):
            ok = got == want
            print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
            if not ok:
                bad.append(label)

        r = post({"pokemon_id": 25, "bundle": ""})
        ok = bool(r.get("ok"))
        print(f"  {'ok  ' if ok else 'FAIL'} clearing it: {r.get('message') or r}")
        if not ok:
            bad.append("clear override")
        d = P.build_get_asset_digest_response("android")
        ok = pb.get(_entry_for(d, "pm0025"), 4, pb.WT_32) == STOCK_CRC
        print(f"  {'ok  ' if ok else 'FAIL'} back to the stock checksum")
        if not ok:
            bad.append("revert checksum")
    finally:
        _S.SETTINGS_FILE = saved_file


if __name__ == "__main__":
    sys.exit(main())
