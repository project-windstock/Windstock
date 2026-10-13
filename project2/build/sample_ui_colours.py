"""Samples the colours the launcher painted, from a device screenshot.

Reads a PNG without any image library (the build hosts have none): decodes the pixel data the
same way every PNG decoder does, then reports the few screen regions that say which Material 3
scheme is on screen and whether the navigation bar marked its selected section.

    py -3 build/sample_ui_colours.py shot.png

The expected values are the baseline Material 3 palette in res/values/colors.xml:
light surface #FFFBFE, light surfaceContainer #F7F2FA, light primaryContainer #EADDFF,
dark surface #1C1B1F, dark surfaceContainer #211F26, dark primaryContainer #4F378B.
"""

import struct
import sys
import zlib


def read_png(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, idat, width, height, colour = 8, b"", None, None, None
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + length]
        i += 12 + length
        if kind == b"IHDR":
            width, height, _, colour = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    raw = zlib.decompress(idat)
    bpp = 4 if colour == 6 else 3
    stride = width * bpp
    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        filter = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if filter == 1:
                line[x] = (line[x] + a) & 255
            elif filter == 2:
                line[x] = (line[x] + b) & 255
            elif filter == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif filter == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out += line
        prev = line
    return width, height, bpp, bytes(out)


def main(path):
    width, height, bpp, pixels = read_png(path)

    def at(x, y):
        offset = (y * width + x) * bpp
        return "#%02X%02X%02X" % (pixels[offset], pixels[offset + 1], pixels[offset + 2])

    # The three sections are equal thirds of the bar. Each section's pill is sampled 60px left of
    # its centre, which is inside the 64dp indicator but clear of the 24dp glyph drawn on it, so the
    # reading is the pill's own colour rather than a stroke of the icon.
    print("size %dx%d" % (width, height))
    print("  top app bar     %s  (light #FFFBFE / dark #1C1B1F)" % at(width // 2, 150))
    print("  nav bar         %s  (light #F7F2FA / dark #211F26)" % at(5, height - 280))
    for index, name in enumerate(("Apps", "Add apps", "Settings"), start=1):
        centre = width * (2 * index - 1) // 6
        print("  %-8s pill    %s%s" % (name, at(centre - 60, height - 245),
                                        "  <- selected: #EADDFF / #4F378B" if index == 3 else ""))
    print("  group card      %s  (surfaceContainer)" % at(width // 2, 470))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "shot.png")
