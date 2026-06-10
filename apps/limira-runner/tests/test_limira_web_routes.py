"""Limira web route contract tests.

The historical test module is mechanically split into ``limira_web_route_parts``
to keep each source file below the project line limit while preserving pytest
collection from this module.
"""

from pathlib import Path as _Path

_PARTS_DIR = _Path(__file__).with_name("limira_web_route_parts")
_PART_SOURCES = []
for _part_path in sorted(_PARTS_DIR.glob("test_limira_web_routes_part_*.pyfrag")):
    _PART_SOURCES.append(_part_path.read_text(encoding="utf-8"))

exec(compile("\n".join(_PART_SOURCES), __file__, "exec"), globals())

del _Path, _PARTS_DIR, _PART_SOURCES, _part_path
