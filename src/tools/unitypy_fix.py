"""
unitypy_fix.py -- moved.

The implementation now lives in ``src/asset_tools/unitypy_fix.py``, because it
is an asset tool and not part of the game server: the server has to keep running
on a machine with no UnityPy installed, and keeping the patch next to the
editors that need it makes that boundary obvious instead of arguable.

This shim exists so the commands in the README keep working:

    py src/tools/unitypy_fix.py <bundle.bundle>     # self-test
    from unitypy_fix import load, save_bundle      # old imports

Everything is re-exported from the one implementation -- there is no second copy
of the patch to keep in step.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from asset_tools import unitypy_fix as _impl  # noqa: E402

__all__ = list(_impl.__all__)
for _name in __all__:
    globals()[_name] = getattr(_impl, _name)
for _name in ("_SIDETABLE", "_write_atomic", "_remove_quietly", "_next_seq"):
    globals()[_name] = getattr(_impl, _name)

_IMPL_DOC = _impl.__doc__


if __name__ == "__main__":
    if not have_unitypy():
        print(missing_reason())
        raise SystemExit(2)
    targets = sys.argv[1:] or []
    if not targets:
        print(_IMPL_DOC)
        raise SystemExit(2)
    rc = 0
    for t in targets:
        ok, report = selftest(t)
        print("%-14s %s  %s" % (os.path.basename(t), "OK  " if ok else "FAIL",
                                report))
        rc |= 0 if ok else 1
    raise SystemExit(rc)
