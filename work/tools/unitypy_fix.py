"""
unitypy_fix.py -- make UnityPy save Unity 5.x (SerializedFile v11-v16) files faithfully.

Import it before loading anything:  import unitypy_fix  # noqa: F401

In format v11-v16 every object's info record carries its own script_type_index (which
MonoScript a MonoBehaviour runs). UnityPy keeps that number on the SerializedType the
object points at -- and objects SHARE those (in 0.35's sharedassets0, dozens of
MonoBehaviours share type_id -3), so on load the last object read wins and on save every
sharer is written with that one index. The game then binds MonoBehaviours to the wrong
scripts: splash, then a grey screen, no network. (Found 2026-09-24: a no-op
load/save of sharedassets0 changed an index 87 -> 9.)

Fix: remember each object's own index when it is read, and put it back on the type just
before that object is written. (ObjectReader has __slots__, so the indices live in a side
table keyed by (file, path_id); an object cloned into a new path_id calls inherit().)
"""
import struct

from UnityPy.files import ObjectReader as _orm

_OR = _orm.ObjectReader if hasattr(_orm, "ObjectReader") else _orm
_from_reader = _OR.from_reader.__func__
_write = _OR.write
_STI = {}                     # (id(assets_file), path_id) -> script_type_index


def inherit(new_obj, template):
    """A clone of template under a new path_id writes the template's index."""
    key = (id(template.assets_file), template.path_id)
    if key in _STI:
        _STI[(id(new_obj.assets_file), new_obj.path_id)] = _STI[key]


def _patched_from_reader(cls, assets_file, reader):
    obj = _from_reader(cls, assets_file, reader)
    v = assets_file.header.version
    if 11 <= v < 17:
        # the record ends ... class_id? (u16, v<16), script_type_index (i16),
        # is_stripped (u8, v15-16): read the index back from where it sat
        back = 3 if v in (15, 16) else 2
        pos = reader.Position
        reader.Position = pos - back
        raw = reader.read_bytes(2)
        reader.Position = pos
        _STI[(id(assets_file), obj.path_id)] = struct.unpack(
            ("<" if reader.endian == "<" else ">") + "h", raw)[0]
    return obj


def _patched_write(self, header, writer, data_writer):
    sti = _STI.get((id(self.assets_file), self.path_id))
    if sti is not None and self.serialized_type is not None:
        self.serialized_type.script_type_index = sti
    return _write(self, header, writer, data_writer)


if not getattr(_OR, "_windstock_sti_fix", False):
    _OR.from_reader = classmethod(_patched_from_reader)
    _OR.write = _patched_write
    _OR._windstock_sti_fix = True
