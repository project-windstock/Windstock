"""
Find where a 0.29 iOS function lives in the 0.35 iOS binary (arm64).

The il2cpp dump (work/il2cpp/ios/out/dump.cs) only has 0.29 addresses, but the
windstock tweak targets 0.35. The code is mostly identical between the builds; what
changes are the PC-relative bits (b/bl targets, adrp pages, page offsets, literal
loads). So: take N instructions from the 0.29 function, blank those fields, and scan
the 0.35 __TEXT for the one place the masked pattern matches.

Also reports how many direct b/bl callers the 0.35 match has -- a method-pointer-table
redirect (the no-JIT hook the tweak uses) only catches calls that go through
MethodInfo, so 0 direct callers means the redirect sees every call.

Run:  python3 port_rva.py 0x1F0C34 [0x1F1018 ...]      (0.29 RVAs, hex)
      python3 port_rva.py --callers 0x206F44            (0.35 RVA: count direct callers)
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_029 = os.path.join(HERE, "..", "il2cpp", "ios", "pokemongo")
BIN_035 = os.path.join(HERE, "..", "il2cpp", "ios035", "pokemongo")
CPU_ARM64 = 0x0100000C


def arm64_slice(path):
    """(bytes of the arm64 Mach-O, image base vmaddr, __TEXT (fileoff, vmaddr, size))."""
    data = open(path, "rb").read()
    if struct.unpack(">I", data[:4])[0] == 0xCAFEBABE:                 # fat
        n = struct.unpack(">I", data[4:8])[0]
        for i in range(n):
            ct, _cs, off, size, _al = struct.unpack(">iiIII", data[8 + 20 * i:28 + 20 * i])
            if ct == CPU_ARM64:
                data = data[off:off + size]
                break
        else:
            raise SystemExit(f"{path}: no arm64 slice")
    magic, _ct, _st, _ft, ncmds, _sz, _fl, _r = struct.unpack("<IiiIIIII", data[:32])
    if magic != 0xFEEDFACF:
        raise SystemExit(f"{path}: not a 64-bit Mach-O")
    p, base, text = 32, None, None
    for _ in range(ncmds):
        cmd, size = struct.unpack("<II", data[p:p + 8])
        if cmd == 0x19:                                                  # LC_SEGMENT_64
            name = data[p + 8:p + 24].rstrip(b"\0")
            vmaddr, vmsize, fileoff, filesize = struct.unpack("<QQQQ", data[p + 24:p + 56])
            if name == b"__TEXT":
                base, text = vmaddr, (fileoff, vmaddr, filesize)
        p += size
    return data, base, text


def mask_word(w):
    """Blank the parts of one instruction that move between builds."""
    if (w & 0x7C000000) == 0x14000000:          # b / bl imm26
        return w & 0xFC000000, 0xFC000000
    if (w & 0x1F000000) == 0x10000000:          # adr / adrp immlo+immhi
        return w & 0x9F00001F, 0x9F00001F
    if (w & 0xFF000010) == 0x54000000:          # b.cond imm19
        return w & 0xFF00001F, 0xFF00001F
    if (w & 0x7E000000) == 0x34000000:          # cbz / cbnz imm19
        return w & 0xFF00001F, 0xFF00001F
    if (w & 0x7E000000) == 0x36000000:          # tbz / tbnz imm14
        return w & 0xFFF8001F, 0xFFF8001F
    if (w & 0x3B000000) == 0x18000000:          # ldr literal imm19
        return w & 0xFF00001F, 0xFF00001F
    if (w & 0xFF800000) == 0x91000000:          # add x, x, #imm12 (adrp page offset)
        return w & 0xFFC003FF, 0xFFC003FF
    if (w & 0x3FC00000) == 0x39400000 or (w & 0x3FC00000) == 0x39000000:  # ldr/str unsigned imm12
        return w & 0xFFC003FF, 0xFFC003FF
    return w, 0xFFFFFFFF


def words(data, off, n):
    return list(struct.unpack("<%dI" % n, data[off:off + 4 * n]))


def find_in_035(rva029, n=24):
    d29, base29, t29 = arm64_slice(BIN_029)
    d35, base35, t35 = arm64_slice(BIN_035)
    src = words(d29, rva029, n)                   # __TEXT fileoff 0 -> RVA == slice offset
    pat = [mask_word(w) for w in src]
    first_val, first_mask = pat[0]
    start, end = t35[0], t35[0] + t35[2] - 4 * n
    hits = []
    text = d35
    for off in range(start, end, 4):
        w = struct.unpack_from("<I", text, off)[0]
        if (w & first_mask) != first_val:
            continue
        ok = True
        for i in range(1, n):
            v, m = pat[i]
            if (struct.unpack_from("<I", text, off + 4 * i)[0] & m) != v:
                ok = False
                break
        if ok:
            hits.append(off)
            if len(hits) > 5:
                break
    return hits, src[0]


_SLICES = {}


def _slices():
    if not _SLICES:
        _SLICES["29"] = arm64_slice(BIN_029)
        _SLICES["35"] = arm64_slice(BIN_035)
    return _SLICES["29"], _SLICES["35"]


def find_in_035_all(rva029, n):
    """Every 0.35 offset matching n masked instructions from the 0.29 function."""
    (d29, _b29, _t29), (d35, _b35, t35) = _slices()
    pat = [mask_word(w) for w in words(d29, rva029, n)]
    fv, fm = pat[0]
    start, end = t35[0], t35[0] + t35[2] - 4 * n
    hits = []
    for off in range(start, end, 4):
        if (struct.unpack_from("<I", d35, off)[0] & fm) != fv:
            continue
        for i in range(1, n):
            v, m = pat[i]
            if (struct.unpack_from("<I", d35, off + 4 * i)[0] & m) != v:
                break
        else:
            hits.append(off)
            if len(hits) > 64:
                break
    return hits, pat[0][0]


def direct_callers(rva035):
    d35, _b, t35 = arm64_slice(BIN_035)
    start, size = t35[0], t35[2]
    count = 0
    for off in range(start, start + size, 4):
        w = struct.unpack_from("<I", d35, off)[0]
        if (w & 0x7C000000) == 0x14000000:        # b / bl
            imm = w & 0x03FFFFFF
            if imm & 0x02000000:
                imm -= 0x04000000
            if off + imm * 4 == rva035:
                count += 1
    return count


def main(argv):
    if not argv:
        print(__doc__)
        return
    if argv[0] == "--callers":
        for a in argv[1:]:
            r = int(a, 16)
            print(f"0.35 {r:#x}: {direct_callers(r)} direct b/bl caller(s)")
        return
    # --anchor 0x240568=0x25cc18 : a known 0.29->0.35 pair near the targets. Functions
    # keep their order between builds, so when a short/generic function matches in
    # several places, the right one is the match closest to rva + (anchor shift).
    anchors = []
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--anchor":
            k, v = next(it).split("=")
            anchors.append((int(k, 16), int(v, 16)))
        else:
            rest.append(a)
    for a in rest:
        r = int(a, 16)
        done = False
        last = []
        # Grow the pattern until it is unique. Start SHORT: a small function's tail
        # runs into its neighbour, which may have changed between builds -- a long
        # pattern then matches nothing even though the function itself is intact.
        for n in (6, 8, 12, 16, 24, 48, 96):
            hits, first = find_in_035_all(r, n)
            if len(hits) == 1:
                h = hits[0]
                print(f"0.29 {r:#x} -> 0.35 {h:#x}  ({n} instr, unique, "
                      f"{direct_callers(h)} direct callers)")
                done = True
                break
            if not hits:
                break                              # pattern ran into code that changed
            last = hits
        if done:
            continue
        if last and anchors:
            ak, av = min(anchors, key=lambda p: abs(p[0] - r))
            expect = r + (av - ak)
            best = min(last, key=lambda h: abs(h - expect))
            dist = abs(best - expect)
            print(f"0.29 {r:#x} -> 0.35 {best:#x}  ({len(last)} candidates, nearest to "
                  f"expected {expect:#x} by {dist:#x}; {direct_callers(best)} direct callers)")
        elif last:
            print(f"0.29 {r:#x}: {len(last)} candidates {[hex(h) for h in last[:6]]} "
                  f"-- pass --anchor OLD=NEW to pick")
        else:
            print(f"0.29 {r:#x}: no match")


if __name__ == "__main__":
    main(sys.argv[1:])
