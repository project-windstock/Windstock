"""
Compatibility shim: the protobuf codec now lives at ``src/pbcodec.py``.

It is a generic, ``struct``-only wire codec shared by the server and the
standalone asset tools (both need to read the protobuf asset digest), so it sits
above both packages instead of inside this one. This module keeps the old
``from windstock.game import pb`` path working.

The server puts ``src/server`` on sys.path rather than ``src``, so import the
sibling module by locating it relative to this file.
"""
import os
import sys

try:
    import pbcodec
except ImportError:  # running with only src/server importable
    _SRC = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    import pbcodec

WT_VARINT = pbcodec.WT_VARINT
WT_64 = pbcodec.WT_64
WT_LEN = pbcodec.WT_LEN
WT_32 = pbcodec.WT_32
varint = pbcodec.varint
Writer = pbcodec.Writer
decode = pbcodec.decode
get = pbcodec.get
get_all = pbcodec.get_all
pretty = pbcodec.pretty

# Private helpers protocol.py already relied on when pb lived here. Re-exported
# deliberately: the digest merge rebuilds the genuine protobuf bytes field by
# field, and silently dropping these would turn a working build into an
# AttributeError the first time someone served a custom bundle.
_tag = pbcodec._tag
_read_varint = pbcodec._read_varint

__all__ = [
    "WT_VARINT", "WT_64", "WT_LEN", "WT_32",
    "varint", "Writer", "decode", "get", "get_all", "pretty",
    "_tag", "_read_varint",
]
