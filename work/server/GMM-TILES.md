# Google Mobile Maps tiles — what we know

Why the map's ground is flat green, and what serving roads from OSM would take.

## The path

The map is not drawn from anything our server sends. It is drawn by Niantic's
own tile layer, compiled into the client:

```
nia::map::MapTileManager
  -> nia::map::gmm::MapTileFetcherGMM
     -> nia::map::gmm::store::GMMTileStore   (DispatchRequest, batched)
        -> nia::map::gmm::store::GMMDataLoader
  -> nia::map::gmm::parse::GMMTileParser
     -> TileCryptInputStream -> GzipInputStream
     -> TileBuilderFactory -> RoadBuilder / AreaBuilder / BuildingBuilder
                              / LineMeshBuilder
```

It fetches Google Mobile Maps vector tiles from
`http://mobilemaps.clients.google.com/glm/mmap`. Niantic's 2016 API key stopped
working years ago, so every fetch fails and nothing is drawn. The host was also
missing from the launcher's redirect list, so the requests were not even
reaching us — both fixed; `/glm/` is routed by path in `server.py` because the
host matches none of the other checks.

## Request — DECODED, verified against live captures

Not protobuf from byte zero. A DriveAbout framing header comes first:

```
0000  00 17 00 00 00 00 00 00 00 00 00 02 65 6e 00 12  ............en..
0010  61 6e 64 72 6f 69 64 2d 44 72 69 76 65 41 62 6f  android-DriveAbo
0020  75 74 00 03 33 2e 30 00 07 61 6e 64 72 6f 69 64  ut..3.0..android
```

- `00 17` version word, then eight zero bytes
- u16-BE length-prefixed strings: locale `en`, client `android-DriveAbout`,
  version `3.0`, platform `android`
- a tagged block (`MapTileRequestHeader`: `DriveAbout`, `SYSTEM`)
- then the tile request payload, starting 6 bytes past the `SYSTEM` string

Payload (`MapTileRequestProto`):

```
field 1  { 1: 256 (tile size px), 2: [0, 11] (layers), 4: 3 }
field 9  repeated TileRequest { kind=1, x=2, y=3, zoom=4 }
```

**x / y / z are ordinary Web Mercator tile numbers** — the same scheme every
OSM tile server speaks. Always z17, ~45 tiles for a fresh view and ~7 for an
incremental pan. Confirmed by round-tripping real captures back to lat/lng:

```
45 tiles z[17]  x 65489-65495  y 43585-43591  ~(51.50105,-0.11948)   London
45 tiles z[17]  x 27306-27312  y 49740-49746  ~(39.74204,-104.99222) Denver
```

Both matched where the player actually was, so the tile layer does follow the
game's position — no extra spoofing needed.

`gmm.py` parses all of this.

## Response — SCHEMA RECOVERED (`gmm_tile.proto`)

`MapTileResponseProto` is protobuf-**lite**, so there is no embedded
`FileDescriptorProto` and the binary is stripped to 1472 symbols — but the
generated serializer gives everything up. Route: RTTI type name -> typeinfo ->
vtable -> **slot 13**, `SerializeWithCachedSizes`, which calls
`WireFormatLite::Write<Type>(field_number, value, output)` with the field
number as an immediate in `w0`. Field number and wire type read straight off.

Writer helpers, identified by the wire type they emit and whether they zigzag:

| address | wire | meaning |
|---|---|---|
| `0x1014f4228` | 0 | int32 |
| `0x1014f4b64` | 0 | varint-a |
| `0x1014f43d0` | 0 | varint-b |
| `0x1014f4560` | 0, **zigzag** | sint32 |
| `0x1014f5b68` | 2 | message |
| `0x1014f5550` | 2 | string |
| `0x1014f4ed8` | 2 | bytes |
| `0x1014f5a44` | **3** | GROUP |

Two findings worth calling out: `GeometryProto` fields 3 and 7 are **groups**
(wire type 3 — 2016-era Google protobuf still used them), and
`EfficientMapPointProto`'s two fields are **zigzag** sint32, i.e. delta-encoded
vertices, which is what the "Efficient" buys.

Full schema for all twelve messages is in `gmm_tile.proto`. Field numbers and
wire types are solid; which submessage type sits in each message-typed field,
and repeated-vs-optional, are labelled as unknown.

## TileCrypt — SOLVED (`gmm_crypt.py`)

It is **RC4 with a 256-round drop**. Found via the RTTI descriptor for
`TileCryptInputStream` -> its vtable at `0x101d47030` -> `Next()` (slot 2,
`0x1015cbe40`), which is only plumbing; the cipher is its single callee at
`0x101661268`, textbook RC4 PRGA with the S-box at state+0, `i` at +0x100,
`j` at +0x104.

The KSA is in the constructor at `0x1015cbc54`. The S-box is memcpy'd from a
256-byte constant at `0x101b13636` instead of being filled in a loop — that
constant is plain `00..ff`, so the KSA is standard. After it, the PRGA runs
256 times with the output discarded (RC4-drop[256]) and `i`/`j` are kept.

The key is 40 bytes, matching the `reserve(40)` exactly, big-endian
throughout:

```
secret(16) y(u32) zoom(u32) x(u32) version=9(u16) session(u16) cookie(u64)
```

The 16-byte secret is obfuscated as `key[k] = (A[k] * 47) ^ B[k]` over two
tables at `0x101b13736` and `0x101b13746`, and resolves to:

```
5fcfb08fb4d0e217089fc16ea8cce1b8
```

`session` and `cookie` are the constructor's last two arguments, sourced from
the response/cookie exchange — which we control as the server, so both can
simply be 0. That removes the Zwieback-cookie problem entirely: the key then
depends on nothing but the tile coordinate.

**Caveat:** derived wholly from disassembly and never checked against real
ciphertext, because Google never answered us and we have no genuine encrypted
tile. The implementation round-trips with itself; that proves consistency,
not correctness.

## Where it stands

Data side: solved — plain XYZ, and we already ship OSM extracts
(`data/osm_forts.json`, `fetch_pois_pbf.py`).
Request: decoded. Crypt: solved, modulo verification.
Request: decoded. Crypt: solved. Response schema: recovered.
Remaining: which submessage type goes in each message-typed field, the vertex
packing inside PolyLine.vertices, and the layer/class values RoadBuilder
expects. All three are answerable by reading the matching parser
(vtable slot 11) and RoadBuilder itself.

None of it is verified against the client yet -- the real test is whether it
accepts a tile we build and encrypt.

Meanwhile `/glm/mmap` answers an empty 200. That is deliberate: the client
retries a *failed* fetch forever but accepts and caches an *empty* tile, so the
map settles instead of hammering. It draws no roads either way.

Captures land in `data/gmm_captures/` (cap 200, `GMM_CAPTURE=0` to disable).
