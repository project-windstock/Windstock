"""
unity_assets.py -- MOVED. Now a shim over ``asset_tools.client_assets``.

The implementation is ``src/asset_tools/client_assets.py``; run it directly:

    py src/asset_tools/client_assets.py list <file> [--type Texture2D]
    py src/asset_tools/client_assets.py export  <file> <name> <out.png>
    py src/asset_tools/client_assets.py replace <file> <name> <in.png> [-o OUT]
    py src/asset_tools/client_assets.py join    <dir> <base>
    py src/asset_tools/client_assets.py split   <file> [--chunk 1048576]

Two behaviour changes come with the move, both intentional:

* loads go through ``asset_tools.unitypy_fix``, because stock UnityPy corrupts
  Unity 5.3 files on save;
* ``replace`` writes atomically and refuses to overwrite its own input without
  ``-o`` or ``--force``, because it rewrites the whole SerializedFile.

This is the APK/IPA ``Data/`` folder tool. The pm#### bundles the server serves
are a different tool: ``py src/asset_tools/cli.py --help``.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from asset_tools import client_assets as _impl  # noqa: E402

CMD_ALIASES = {
    # the old hand-rolled dispatcher took "join" as `join <dir> <base>`; argparse
    # takes the same two positionals, so the only translation needed is the
    # old --chunk-with-no-value default, which argparse also handles.
}


def main(argv=None):
    return _impl.main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
