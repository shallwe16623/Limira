"""Limira browser/API router.

This module is mechanically split into ``limira_parts`` to keep individual source
files reviewable while preserving the historical ``limira`` module import path.
"""

from pathlib import Path as _Path

_PARTS_DIR = _Path(__file__).with_name("limira_parts")
_PART_SOURCES = []
for _part_path in sorted(_PARTS_DIR.glob("limira_part_*.pyfrag")):
    _PART_SOURCES.append(_part_path.read_text(encoding="utf-8"))

exec(compile("\n".join(_PART_SOURCES), __file__, "exec"), globals())

del _Path, _PARTS_DIR, _PART_SOURCES, _part_path
