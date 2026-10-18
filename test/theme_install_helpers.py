"""Theme-pack builders the theme install test modules share.

A valid variable map, a JSON writer, the Level-0 pack builder, the tiered (L0/L1/L2)
pack builder and a valid MP3 header. ``test_theme_install.py`` covers the store and
the tier-aware validation core, ``test_theme_install_declarations.py`` the manifest
declarations, the placement the asset descriptor reports and font roles, and
``test_theme_install_identity.py`` slugs and pack identity.
"""

from __future__ import annotations

import json
from pathlib import Path

# _validate_theme_data only *requires* --bg/--text/--accent and rejects unknown
# keys, so a 3-var map per mode is a complete, valid Level-0 theme.
_VALID_VARS = {
    "dark": {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"},
    "light": {"--bg": "#ffffff", "--text": "#000000", "--accent": "#0033cc"},
}


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_theme(
    root: Path,
    *,
    level: int = 0,
    name: str = "LCARS",
    styled: bool = False,
    variables: dict | None = None,
) -> Path:
    """Build a theme directory under ``root`` and return its path."""
    d = root / "theme"
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "theme.json",
        {"slug": "lcars", "name": name, "emoji": "🖖", "level": level, "formatVersion": 1},
    )
    varobj = _VALID_VARS if variables is None else variables
    _write(d / ("styles/variables.json" if styled else "variables.json"), varobj)
    return d


# A minimal but valid MP3 header (ID3 or MPEG frame sync) for the audio sniff.
_VALID_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 64


def _make_tiered(
    root: Path,
    *,
    level: int,
    extra: dict[str, object] | None = None,
    slug: str | None = None,
) -> Path:
    """Build a Level 1/2 theme directory; ``extra`` maps rel-path -> str|bytes."""
    d = root / f"theme-l{level}"
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "theme.json",
        {
            "slug": slug or f"pack-l{level}",
            "name": f"Pack L{level}",
            "emoji": "🎨",
            "level": level,
            "formatVersion": 1,
        },
    )
    _write(d / "variables.json", _VALID_VARS)
    for rel, content in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(str(content), encoding="utf-8")
    return d
