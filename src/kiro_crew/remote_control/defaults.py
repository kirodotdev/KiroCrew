"""The one value that decides whether personal Remote Control starts on by default.

A leaf module with no imports, so the config schema can read it without pulling in
the rest of the package.

Whether Remote Control is on by default at Kiro sign-in is an open product decision.
Until it is made the default stays off, and flipping it is this one line: it is the
``tunnel.relay_enabled`` field default, and the section builder
(``config/section_builders.py::_build_tunnel_config``) reads that field default
for a ``config.json`` that omits the key, through ``SectionReader.get``. A
person who turned Remote Control off has an explicit ``false`` in ``config.json``,
so a flip here never turns it back on for them.
"""

from __future__ import annotations

#: Whether ``tunnel.relay_enabled`` is on when ``config.json`` does not set it.
REMOTE_CONTROL_ON_BY_DEFAULT: bool = False
