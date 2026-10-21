"""Gateway-side checks of a crewmate's saved page, for the ``dashboard_save`` reply.

The offline page run is skipped on a host with no browser, so two
mistakes seen on crewmate pages are checked here on the package text instead: a list,
record or time bound to a ``data-dashboard-field`` cell, which the host fills as raw
text, and a theme whose fixed colours cover one colour scheme only. Both answer in
words from ``crewmate_prompts.json`` and never refuse the save.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from kiro_crew import crewmate_prompts as _crewmate_prompts

#: The field types a page has to draw itself: a text cell shows them raw.
_DRAWN_BY_SCRIPT: Final[tuple[str, ...]] = ("array", "object", "timestamp")


def page_rendering() -> dict[str, str]:
    """How a page shows each field type a ``data-dashboard-field`` cell cannot."""
    return {
        "array": _crewmate_prompts.text("dashboard.render_array"),
        "object": _crewmate_prompts.text("dashboard.render_object"),
        "timestamp": _crewmate_prompts.text("dashboard.render_timestamp"),
    }


_BOUND_CELL = re.compile(r"""data-dashboard-field\s*=\s*["']?([a-z][a-z0-9_]*)""")


def shape_warnings(package: Mapping[str, Any]) -> list[str]:
    """One line per array, object or timestamp field the page binds as a text cell.

    The host fills a ``data-dashboard-field`` element as TEXT, so a list shows as
    ``[]`` and a time as a raw ISO string. A field the page only reads in its script is
    not bound that way and is not named.
    """
    types = ((package.get("model") or {}).get("types")) or {}
    html = ((package.get("view") or {}).get("html")) or ""
    bound = set(_BOUND_CELL.findall(html)) if isinstance(html, str) else set()
    out: list[str] = []
    for name in sorted(bound):
        spec = types.get(name)
        kind = spec.get("type") if isinstance(spec, Mapping) else None
        if kind in _DRAWN_BY_SCRIPT:
            out.append(
                _crewmate_prompts.fill(
                    "dashboard.shape_warning",
                    field=name,
                    kind=kind,
                    how=page_rendering()[kind],
                )
            )
    return out


#: A colour written as a value rather than read from a variable.
_FIXED_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(")
_COLOUR_DECL = re.compile(r"(?<![-\w])(color|background(?:-color)?|fill)\s*:\s*([^;}]+)")
_FG_TOKEN = re.compile(r"(?:fg|text|ink|fore)", re.I)
_BG_TOKEN = re.compile(r"(?:bg|back|surface|panel|board|paper)", re.I)
#: Ways a theme covers both schemes, or defers to the host's scheme-aware variables.
_BOTH_SCHEMES = ("prefers-color-scheme", "light-dark(", "data-mode", "data-theme")


def _opaque(value: str) -> bool:
    """False for a colour that lets the background through (``rgba(..., 0.04)``)."""
    alpha = re.search(r"(?:rgba|hsla)\([^)]*,\s*([0-9.]+)\s*\)|/\s*([0-9.]+%?)\s*\)", value)
    if not alpha:
        return True
    raw = alpha.group(1) or alpha.group(2) or "1"
    number = float(raw.rstrip("%")) / (100 if raw.endswith("%") else 1)
    return number >= 0.5


def theme_warnings(package: Mapping[str, Any]) -> list[str]:
    """A warning when the page's fixed colours cover one scheme only.

    A theme that fixes the text colour and leaves the background to the host (or the
    reverse) reads in one scheme and not the other: Cove's light text on the host's
    white page. Fixing BOTH, like a chalkboard with chalk text, reads the same in each,
    and a theme that names both schemes is left alone. A heuristic on the CSS text
    (theme tokens, ``theme.css`` and the page's own ``<style>``), run here because the
    page run is skipped on a host with no browser.
    """
    theme = package.get("theme") or {}
    tokens = theme.get("tokens") if isinstance(theme, Mapping) else None
    css = theme.get("css") if isinstance(theme, Mapping) else ""
    html = ((package.get("view") or {}).get("html")) or ""
    styles = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    text = f"{css or ''}\n{styles}"
    if any(marker in text for marker in _BOTH_SCHEMES):
        return []
    fg = bg = False
    for name, value in (tokens or {}).items():
        if isinstance(value, str) and _FIXED_COLOUR.search(value):
            if _FG_TOKEN.search(name):
                fg = True
            elif _BG_TOKEN.search(name) and _opaque(value):
                bg = True
    for prop, value in _COLOUR_DECL.findall(text):
        if not _FIXED_COLOUR.search(value):
            continue
        if prop == "color" or prop == "fill":
            fg = True
        elif _opaque(value):
            bg = True
    return [_crewmate_prompts.text("dashboard.theme_one_scheme")] if fg != bg else []
