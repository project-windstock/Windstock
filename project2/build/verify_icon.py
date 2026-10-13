"""Check the rasterised GitHub mark before it is shipped: right size, real transparency, and a
solid silhouette rather than an outline (which is what the VectorDrawable produced)."""
from PIL import Image
import sys

path = sys.argv[1]
img = Image.open(path).convert("RGBA")
w, h = img.size
px = img.load()
opaque = dark = outline = 0
for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a > 128:
            opaque += 1
            lum = (r * 299 + g * 587 + b * 114) // 1000
            if lum < 100:
                dark += 1
print(f"{path}: {w}x{h} mode=RGBA opaque={opaque}/{w*h} ({100*opaque/(w*h):.1f}%) dark={dark}")

# Print the middle of the mark as ASCII to eyeball the silhouette.
step = max(1, h // 32)
for y in range(0, h, step):
    line = ""
    for x in range(0, w, step // 2 or 1):
        r, g, b, a = px[min(x, w - 1), y]
        line += "." if a <= 128 else ("#" if (r * 299 + g * 587 + b * 114) // 1000 < 100 else "+")
    print(line)
