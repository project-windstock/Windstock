"""
decrypt_bundle.py -- MOVED. Now a shim over ``asset_tools``.

The implementation is ``src/asset_tools/bundle_crypto.py`` and the command line
is ``src/asset_tools/cli.py``. This file stays because the README's commands and
the test scripts do `from decrypt_bundle import decrypt, digest_key`, and those
are not worth breaking to save a filename.

    py src/asset_tools/cli.py verify --all
    py src/asset_tools/cli.py decrypt pm0025 -o pm0025.bundle

Both spellings do exactly the same thing.

What the format is, and the warning about the asset-digest checksum field, lives
in the docstring of ``src/asset_tools/bundle_crypto.py``.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from asset_tools import bundle_crypto as crypto  # noqa: E402
from asset_tools import config as _config        # noqa: E402

# Re-export the primitives by name. This module used to BE the implementation.
from asset_tools.bundle_crypto import (   # noqa: E402,F401
    KEY_MASK, DIGEST_FILE, EXTRA_FILE, aes_key_for, decrypt, encrypt, hmac_ok,
    crc32c, unityfs_size, is_bundle, verify_encrypted, verify_all, digest_key,
    digest_entry, extra_path, load_extra, save_extra, register,
)

DEFAULT_ASSETS = _config.assets_dir("android")

__all__ = ["main", "KEY_MASK", "DIGEST_FILE", "EXTRA_FILE", "aes_key_for",
           "decrypt", "encrypt", "hmac_ok", "crc32c", "unityfs_size",
           "is_bundle", "verify_encrypted", "verify_all", "digest_key",
           "digest_entry", "extra_path", "load_extra", "save_extra",
           "register", "DEFAULT_ASSETS"]


def main(argv=None):
    """Delegate to the unified CLI, keeping the old flags working."""
    from asset_tools import cli
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--verify-all":
        argv = ["verify", "--all"]
    elif "--verify-all" in argv:
        argv = ["verify"] + [x for x in argv if x != "--verify-all"]
    else:
        argv = ["decrypt"] + argv
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
