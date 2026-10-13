"""Turn GitHub's mark-github octicon SVG into a VectorDrawable.

Android's PathParser accepts SVG path syntax but not every *compact* number form: in
"a2.9 2.9 0 0 1-1.078.224" it has to split "1-1.078.224" into 1, -1.078, .224 on its own, and older
implementations get that wrong. So the path is tokenized here and re-emitted with explicit
separators, which the parser definitely reads.
"""
import re
import sys

SVG = "project2/build/icons/mark-github-24.svg"
OUT = "project2/launcher/app/src/main/res/drawable/ic_github.xml"

# One token per command letter or per number. A '-' always begins a new number (unless it is an
# exponent sign), which is what makes the compact SVG form unambiguous.
TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

svg = open(SVG, encoding="utf-8").read()
d = re.search(r'<path\s+d="([^"]+)"', svg).group(1)
tokens = TOKEN.findall(d)
if not tokens:
    sys.exit("no path tokens")
# Commands stay glued to their first argument only; everything else is space separated.
out = []
for token in tokens:
    out.append(token)
path_data = " ".join(out)
assert "1-1.078" not in path_data, "separator normalisation failed"

xml = f'''<?xml version="1.0" encoding="utf-8"?>
<!-- GitHub's own "mark-github" octicon (primer/octicons, MIT), used for the button that links to
     this build's author. Regenerate with project2/build/make_github_drawable.py. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="48dp"
    android:height="48dp"
    android:viewportWidth="24"
    android:viewportHeight="24">
    <path
        android:fillColor="#FF24292F"
        android:pathData="{path_data}" />
</vector>
'''
open(OUT, "w", encoding="utf-8", newline="\n").write(xml)
print(f"{len(tokens)} tokens -> {OUT}")
