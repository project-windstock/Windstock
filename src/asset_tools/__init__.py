"""Asset tools for Windstock: read, edit, preview and rebuild the Pokemon GO
asset bundles, without the game server anywhere in sight.

The pieces, roughly in the order you meet them:

* :mod:`asset_tools.bundle_crypto` -- the encrypted bundle container and the
  asset digest. Pure bytes, no UnityPy.
* :mod:`asset_tools.unitypy_fix` -- the ONLY sanctioned UnityPy import. Plain
  UnityPy corrupts Unity 5.3 files on save; this patches the read and write
  paths so a round trip is byte-identical.
* :mod:`asset_tools.assetlib` -- the engine: inventory, open a bundle, recolour
  textures, build a variant, export a rigged glTF.
* :mod:`asset_tools.meshdata` -- the skinned mesh as plain numpy arrays.
* :mod:`asset_tools.render3d` -- a small software renderer, so the 3D preview
  needs no OpenGL and no browser.
* :mod:`asset_tools.editor` -- the CustomTkinter desktop editor.
* :mod:`asset_tools.cli` -- the same things from a terminal.

Nothing here imports :mod:`windstock`. The tools read ``settings.json``
themselves (see :mod:`asset_tools.config`), so they work in a checkout with no
server installed, and so the game server keeps working on a machine with no
UnityPy, no Pillow and no customtkinter -- which is the whole reason the asset
editor is not a server module.
"""
import os
import sys

# Allow `py src/asset_tools/cli.py` as well as `import asset_tools.cli`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The asset digest is protobuf, and this tree has exactly one codec
# (``pbcodec``, a generic struct-only module that sits above both packages
# because the server needs it too). Two codecs would mean debugging a
# wire-format difference that only shows up on one platform.
import pbcodec  # noqa: F401,E402

__version__ = "1.0"
