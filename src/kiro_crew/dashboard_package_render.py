"""Render a ``kind="dashboard"`` package into the document its iframe shows.

The package says WHAT the page holds -- a model of scalar fields, a view of
blocks over them, a theme of tokens. This module is the only thing that decides
what those look like, and it is HOST code: no byte of markup or script in the
document it builds comes from the agent. An agent composes a layout out of a
closed vocabulary; it does not write a page.

That split is what lets the frame stay shut. :data:`RENDER_CSP` starts from
``default-src 'none'`` and grants inline script and inline style and nothing
else -- no origin, no ``connect-src``, no ``data:`` for script. So three.js and
anime.js cannot be fetched and are INLINED from
:mod:`kiro_crew.dashboard_package_vendor`; see that directory's README for the
versions and why the classic single-file builds.

Two halves, and they are tested against each other
--------------------------------------------------
:data:`BLOCK_RENDERERS` is keyed by the block types in
:func:`~kiro_crew.artifact_store.dashboard_package.view_block_catalog`, and each
block type's ``accepts`` names the data types it can draw.
``test_dashboard_package_render.py`` enumerates the catalog -- not a list copied
out of it -- and fails in both directions: a block type with no renderer, and a
renderer for a block type the catalog does not admit. It also renders every
(block type, accepted data type) pair and looks for that type's own marker in
the output, because a block that quietly drops a timestamp is a blank cell with
every other gate green.

Where a value comes from, and who formats it
--------------------------------------------
Values are NOT in the package. They arrive in ``read`` -- the same payload
shape :func:`kiro_crew.dashboard_frame.read_payload` builds -- and the document
carries them twice: once as text inside the element that shows them, and once in
an inert JSON island the page's own script reads for the bars, the ring and the
3D scene. :func:`display_values` is the single formatter, and a host pushing a
refill is expected to call it rather than re-implement the rules in JS; the
page's fallback formatter is deliberately plain so a drift shows up as a plain
number rather than a wrong one.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from kiro_crew.artifact_store.dashboard_package import (
    MAX_THEME_CSS_BYTES,
    data_type_catalog,
    view_block_catalog,
)
from kiro_crew.dashboard_frame import (
    BINDING_ATTRIBUTE,
    DATA_MESSAGE_TYPE,
    PAGE_EVENT,
    WINDOW_GLOBAL,
    escape_json_for_html,
)

#: The two renderings every block type has to survive. A theme supplies one
#: palette; the page is read in both, so a token set that only works in one is a
#: token set that is wrong half the time.
THEMES: Final[tuple[str, ...]] = ("light", "dark")

#: The document's own Content-Security-Policy.
#:
#: Starts from deny-everything and adds exactly two things: inline script and
#: inline style, because the script and the style ARE the document and there is
#: no URL for them to live at. Nothing else is granted -- in particular no
#: origin on ``script-src``, so the vendored libraries can only arrive inlined,
#: and ``connect-src 'none'``, so a page holding an operator's own numbers has
#: nowhere to send them. ``'unsafe-eval'`` is withheld: neither three.js r159
#: nor anime.js 3.2.2 needs a dynamic-exec primitive.
RENDER_CSP: Final[str] = "; ".join(
    (
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "font-src data:",
        "connect-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    )
)

#: The wire type of a BLOCK PATCH: new values for the blocks that subscribe to a
#: fold that just advanced, and nothing else.
#:
#: Distinct from :data:`kiro_crew.dashboard_frame.DATA_MESSAGE_TYPE`, which
#: replaces the whole read. The two are different events on the controller's
#: side -- a full refetch after a sequence gap, against the normal case of one
#: fold moving -- and collapsing them would make a patch repaint every block and
#: re-run the count-up animation on numbers that did not change.
#:
#: The payload is VALUES, never markup: ``{type, blocks: {<block id>:
#: {fields, display}}, seq?, stale?, missing?}``. A model or view change
#: versions the package and that is a reload, so a patch has nothing to rebuild
#: -- which keeps this boundary one that cannot introduce an element.
#: :func:`block_patch` builds the payload.
BLOCK_PATCH_MESSAGE_TYPE: Final[str] = "kirocrew-dashboard:block-patch"

_DATA_ELEMENT_ID: Final[str] = "kirocrew-dashboard-data"
_VENDOR_DIR: Final[Path] = Path(__file__).with_name("dashboard_package_vendor")
#: Vendored builds, by the name the renderer asks for them under.
VENDOR_FILES: Final[Mapping[str, str]] = {
    "three": "three-0.159.0.min.js",
    "anime": "anime-3.2.2.min.js",
}
#: The versions this renderer is written against, for an evidence line and for a
#: test that the files on disk are the ones named here.
VENDOR_VERSIONS: Final[Mapping[str, str]] = {"three": "0.159.0", "anime": "3.2.2"}

#: Block types that need the 3D build. Inlining 652 KB into every document would
#: make a page of numbers pay for a scene it does not draw.
_NEEDS_THREE: Final[frozenset[str]] = frozenset({"orbit"})


# --------------------------------------------------------------------------- #
# Escaping. Two functions, because the two contexts differ and using one for
# both is the whole bug class.
# --------------------------------------------------------------------------- #


def esc(text: object) -> str:
    """Escape a value for element CONTENT."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
    )


def esc_attr(text: object) -> str:
    """Escape a value for a double-quoted ATTRIBUTE."""
    return esc(text).replace('"', "&quot;").replace("'", "&#39;")


# --------------------------------------------------------------------------- #
# Theme. Tokens are re-checked HERE, over what is about to be written, because a
# renderer that trusts its input is a renderer that stops being safe the moment
# a second write path appears.
# --------------------------------------------------------------------------- #

#: A token name the renderer will emit: a CSS custom property and nothing else.
_SAFE_TOKEN_NAME = re.compile(r"\A--[a-z][a-z0-9-]{0,63}\Z")
#: A token VALUE the renderer will emit. No ``;``, ``{``, ``}``, ``<``, ``>`` and
#: no newline, so a value cannot close the declaration it is injected into and
#: open another, and cannot end the ``<style>`` element it sits in.
_SAFE_TOKEN_VALUE = re.compile(r"\A[^\n\r;{}<>]{1,120}\Z")
#: Constructs stripped from ``theme.css``: each one either fetches (impossible
#: under :data:`RENDER_CSP`, so it would fail silently) or leaves the stylesheet.
_CSS_REFUSED: Final[tuple[str, ...]] = (
    "@import",
    "url(",
    "</style",
    "<style",
    "javascript:",
    "<script",
    "expression(",
)

#: The base palette, per theme. Written out per theme rather than derived with
#: ``color-mix`` from one set, because the direction flips: a mix that reads as a
#: recessed panel against a near-white page reads as a raised one against a
#: near-black page.
#:
#: Declared as literal values, never as ``--x: var(--x, ...)``: a custom property
#: that reads a property of its own name is a self-reference, invalid at
#: computed-value time, and every shorthand containing it collapses to nothing
#: with no error anywhere.
#:
#: ``--accent-soft`` is deliberately NOT here. A base value for it would not
#: follow a package that overrides ``--accent``, and the wash would stay the
#: default hue while the rules and bars turned the author's -- which is what the
#: first render of this page did. It is mixed from ``--accent`` at the use site
#: instead, with ``var(--accent-soft, ...)`` so an author can still name one.
_BASE_TOKENS: Final[Mapping[str, Mapping[str, str]]] = {
    "light": {
        "--bg": "#faf9f7",
        "--surface": "#ffffff",
        "--text": "#2b2a28",
        "--text-strong": "#121110",
        "--muted": "#8a857d",
        "--muted-strong": "#5d5953",
        "--border": "#e2ded7",
        "--border-strong": "#c7c1b7",
        "--accent": "#0f7a5a",
        "--warn": "#b4531a",
        "--bad": "#a52414",
        "--good": "#0f7a5a",
        "--track": "#eae6df",
    },
    "dark": {
        "--bg": "#12141a",
        "--surface": "#191c23",
        "--text": "#d8d6d1",
        "--text-strong": "#f4f2ee",
        "--muted": "#7e8591",
        "--muted-strong": "#a8aeb8",
        "--border": "#262a33",
        "--border-strong": "#3a3f4a",
        "--accent": "#00d492",
        "--warn": "#ffd230",
        "--bad": "#ff5c5c",
        "--good": "#00d492",
        "--track": "#23262e",
    },
}

#: Fonts, shared by both themes. Each stack names faces that exist on a macOS
#: desktop, on a Windows desktop and on the Linux box the screenshots are taken
#: on (``P052`` is Palatino, ``C059`` Century Schoolbook, ``Cantarell`` the body
#: sans), so the page has the character it was designed with rather than falling
#: all the way back to a generic family on one of the three.
_FONT_TOKENS: Final[Mapping[str, str]] = {
    "--display-font": (
        '"Iowan Old Style", "Palatino Linotype", P052, C059, Palatino, Georgia, serif'
    ),
    "--text-font": (
        '"Avenir Next", Avenir, Cantarell, "Helvetica Neue", "Segoe UI", system-ui, sans-serif'
    ),
    "--mono-font": (
        'ui-monospace, "SF Mono", "Source Code Pro", "DejaVu Sans Mono", Menlo, monospace'
    ),
}


def theme_tokens(package: Mapping[str, Any], theme: str) -> dict[str, str]:
    """The tokens this document will declare: the base palette, then the package's.

    The package's tokens come second so they OVERRIDE by cascade order, with
    both sides declaring literal values. A token whose name or value would not
    survive :data:`_SAFE_TOKEN_NAME` / :data:`_SAFE_TOKEN_VALUE` is DROPPED
    rather than raised on: the write gate already refuses one, and a render that
    500s over stored content takes somebody's whole dashboard away over a
    styling detail.
    """
    if theme not in THEMES:
        theme = THEMES[0]
    tokens: dict[str, str] = {**_BASE_TOKENS[theme], **_FONT_TOKENS}
    supplied = package.get("theme", {})
    raw = supplied.get("tokens", {}) if isinstance(supplied, Mapping) else {}
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            if not isinstance(name, str) or _SAFE_TOKEN_NAME.fullmatch(name) is None:
                continue
            if not isinstance(value, str) or _SAFE_TOKEN_VALUE.fullmatch(value) is None:
                continue
            tokens[name] = value
    return tokens


def theme_css(package: Mapping[str, Any]) -> str:
    """The package's extra CSS, with every fetching or escaping construct removed.

    Checked again here, over the bytes about to be written, for the reason the
    tokens are: this function is what makes the RENDERED document safe, and the
    test that proves it builds a package by hand instead of going through the
    gate -- otherwise it is testing the gate twice and the renderer never.
    """
    supplied = package.get("theme", {})
    css = supplied.get("css", "") if isinstance(supplied, Mapping) else ""
    if not isinstance(css, str) or not css.strip():
        return ""
    if len(css.encode("utf-8")) > MAX_THEME_CSS_BYTES:
        return ""
    lowered = css.lower()
    if any(bad in lowered for bad in _CSS_REFUSED):
        return ""
    return css


# --------------------------------------------------------------------------- #
# Values. One formatter, in Python, so a cell and the number inside a chart
# cannot disagree -- see display_values' own note.
# --------------------------------------------------------------------------- #

_EM_DASH: Final[str] = "\u2014"


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        # An int has no size limit and a float does, so the conversion is what
        # fails: `float(10**400)` raises OverflowError, which is NOT a
        # ValueError and so is not caught by the string arm below. A fold is
        # free to carry an integer that large, and letting it through turns one
        # oversized value into a render error for the whole block.
        try:
            as_float = float(value)
        except OverflowError:
            return None
        return None if math.isnan(as_float) or math.isinf(as_float) else as_float
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return None if math.isnan(parsed) or math.isinf(parsed) else parsed
    return None


def _format_number(value: object, spec: Mapping[str, Any]) -> str:
    number = _as_number(value)
    if number is None:
        return _EM_DASH
    places = spec.get("precision")
    if not isinstance(places, int) or isinstance(places, bool) or places < 0:
        places = 0 if float(number).is_integer() else 1
    places = min(places, 6)
    text = f"{number:,.{places}f}"
    unit = spec.get("unit")
    return f"{text} {unit}" if isinstance(unit, str) and unit else text


def _format_timestamp(value: object, _spec: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or not value.strip():
        return _EM_DASH
    moment = _parse_instant(value)
    if moment is None:
        return value.strip()
    # THE DAY IS BUILT, NOT FORMATTED, and that is deliberate: every strftime
    # directive for a day without a leading zero is platform-dependent. `%-d` is a
    # glibc extension that Windows rejects outright, so a branch on it renders
    # "Oct 9" on Linux and "Oct 09" on Windows -- one page, two spellings of the
    # same instant, and a test asserting either is red on the other platform.
    # `moment.day` is an int on every platform, so there is one answer.
    return f"{moment.strftime('%b')} {moment.day}, {moment.strftime('%H:%M')}"


def _format_text(value: object, spec: Mapping[str, Any]) -> str:
    if value is None:
        return _EM_DASH
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    limit = spec.get("max_len")
    if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < len(text):
        return text[: max(limit - 1, 1)] + "\u2026"
    return text or _EM_DASH


def _format_enum(value: object, spec: Mapping[str, Any]) -> str:
    choices = spec.get("choices")
    if isinstance(value, str) and isinstance(choices, Sequence) and value in choices:
        return value
    if isinstance(value, str) and value:
        # A value outside the declared choices is SHOWN, not hidden: the fold
        # recorded it, and a reader deciding what to do about an unexpected
        # state cannot do that from an em dash.
        return value
    return _EM_DASH


def _format_bool(value: object, _spec: Mapping[str, Any]) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return "Yes" if value.lower() == "true" else "No"
    return _EM_DASH


#: Data type name -> the formatter for a value of that type. Keyed by the data
#: catalog, and pinned against it: a type with no formatter is caught by test,
#: not by a reader seeing a raw dict in a cell.
_FORMATTERS: Final[Mapping[str, Callable[[object, Mapping[str, Any]], str]]] = {
    "number": _format_number,
    "text": _format_text,
    "timestamp": _format_timestamp,
    "enum": _format_enum,
    "bool": _format_bool,
}


def _parse_instant(value: str) -> datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_value(value: object, spec: Mapping[str, Any]) -> str:
    """One field's value as the string a reader sees, by the field's declared type."""
    formatter = _FORMATTERS.get(str(spec.get("type")))
    if formatter is None:
        return _EM_DASH if value is None else esc_text_fallback(value)
    return formatter(value, spec)


def esc_text_fallback(value: object) -> str:
    """A value whose declared type this build has no formatter for."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def display_values(package: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, str]:
    """Every field's formatted string, by field name. THE seam for a refill.

    A host pushing new values should send these alongside the raw ones. The
    page's own fallback formatter is a plain ``String(value)`` on purpose: if a
    caller forgets this function the cell shows an unformatted number, which a
    reader can see is unformatted, rather than a number formatted by a second
    set of rules that drifted from this one.
    """
    types = package.get("model", {}).get("types", {})
    out: dict[str, str] = {}
    for name, spec in types.items():
        if name in fields:
            out[name] = format_value(fields[name], spec)
    return out


# --------------------------------------------------------------------------- #
# Cells: one field, rendered by its DATA type. Every block that accepts a type
# renders it through this one function, which is why "number renders in a table
# and a timestamp does not" cannot happen in one block and not another.
# --------------------------------------------------------------------------- #


def _binding(name: str) -> str:
    return f'{BINDING_ATTRIBUTE}="{esc_attr(name)}"'


def _label_of(name: str, spec: Mapping[str, Any]) -> str:
    label = spec.get("label")
    if isinstance(label, str) and label.strip():
        return label
    return name.replace("_", " ")


def _value_cell(name: str, spec: Mapping[str, Any], value: object) -> str:
    """The element that shows one value, marked with the data type that shaped it.

    The ``pkg-v--<type>`` class is what the enumerating test looks for: it is
    emitted by the branch that actually rendered the value, so its presence says
    this block drew this data type rather than printing it as plain text.
    """
    kind = str(spec.get("type"))
    shown = format_value(value, spec)
    if kind == "number":
        number = _as_number(value)
        raw = "" if number is None else f' data-pkg-raw="{esc_attr(number)}"'
        return f'<span class="pkg-v pkg-v--number"{raw} {_binding(name)}>{esc(shown)}</span>'
    if kind == "timestamp":
        moment = _parse_instant(value) if isinstance(value, str) else None
        stamp = f' datetime="{esc_attr(value)}"' if moment is not None else ""
        return (
            f'<time class="pkg-v pkg-v--timestamp"{stamp} {_binding(name)}>' f"{esc(shown)}</time>"
        )
    if kind == "enum":
        state = _enum_state(value, spec)
        return (
            f'<span class="pkg-v pkg-v--enum" data-pkg-state="{esc_attr(state)}">'
            f'<i class="pkg-glyph" aria-hidden="true"></i>'
            f"<span {_binding(name)}>{esc(shown)}</span></span>"
        )
    if kind == "bool":
        state = "on" if shown == "Yes" else ("off" if shown == "No" else "unknown")
        return (
            f'<span class="pkg-v pkg-v--bool" data-pkg-state="{esc_attr(state)}">'
            f'<i class="pkg-glyph" aria-hidden="true"></i>'
            f"<span {_binding(name)}>{esc(shown)}</span></span>"
        )
    if kind == "text":
        return f'<span class="pkg-v pkg-v--text" {_binding(name)}>{esc(shown)}</span>'
    return f'<span class="pkg-v pkg-v--unknown" {_binding(name)}>{esc(shown)}</span>'


#: Words a fold or a crewmate uses for a state that wants attention, and the
#: three buckets the page draws. Matched case-insensitively on the whole word, so
#: an unknown label lands in ``other`` and is drawn neutrally rather than
#: guessed at.
_ENUM_STATES: Final[Mapping[str, frozenset[str]]] = {
    "good": frozenset({"green", "good", "done", "ok", "passed", "pass", "merged", "healthy"}),
    "warn": frozenset({"amber", "yellow", "waiting", "review", "pending", "stale", "slow"}),
    "bad": frozenset({"red", "bad", "failed", "fail", "blocked", "stuck", "error", "down"}),
}


def _enum_state(value: object, _spec: Mapping[str, Any]) -> str:
    if not isinstance(value, str):
        return "other"
    word = value.strip().lower()
    for state, words in _ENUM_STATES.items():
        if word in words:
            return state
    return "other"


# --------------------------------------------------------------------------- #
# Block renderers. One per block type in the catalog.
# --------------------------------------------------------------------------- #


def _fields_of(block: Mapping[str, Any], package: Mapping[str, Any]) -> list[str]:
    declared = package.get("model", {}).get("types", {})
    return [name for name in block.get("fields", ()) if name in declared]


def _spec(package: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return package["model"]["types"][name]


def _title(block: Mapping[str, Any]) -> str:
    title = block.get("title")
    if not isinstance(title, str) or not title.strip():
        return ""
    return f'<h2 class="pkg-title">{esc(title)}</h2>'


def _caption(block: Mapping[str, Any]) -> str:
    caption = block.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        return ""
    return f'<p class="pkg-caption">{esc(caption)}</p>'


def _description(spec: Mapping[str, Any]) -> str:
    """A field's own one-line description, or nothing.

    Nothing, rather than an empty element: an empty ``<div>`` still takes its
    line-height, so a band of five figures where none declared a description
    would carry five blank lines of padding nobody asked for.
    """
    detail = spec.get("description")
    if not isinstance(detail, str) or not detail.strip():
        return ""
    return f'<div class="pkg-note">{esc(detail)}</div>'


def _render_stat(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    name = _fields_of(block, package)[0]
    spec = _spec(package, name)
    return (
        f'<div class="pkg-stat">'
        f'<div class="pkg-lab">{esc(_label_of(name, spec))}</div>'
        f'<div class="pkg-figure">{_value_cell(name, spec, values.get(name))}</div>'
        f"</div>{_caption(block)}"
    )


def _render_stat_band(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    cells = []
    for name in _fields_of(block, package):
        spec = _spec(package, name)
        cells.append(
            f'<div class="pkg-band-cell">'
            f'<div class="pkg-lab">{esc(_label_of(name, spec))}</div>'
            f'<div class="pkg-figure">{_value_cell(name, spec, values.get(name))}</div>'
            f"{_description(spec)}"
            f"</div>"
        )
    return f'{_title(block)}<div class="pkg-band">{"".join(cells)}</div>{_caption(block)}'


def _render_table(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    rows = []
    for name in _fields_of(block, package):
        spec = _spec(package, name)
        rows.append(
            f'<tr><th scope="row">{esc(_label_of(name, spec))}</th>'
            f"<td>{_value_cell(name, spec, values.get(name))}</td></tr>"
        )
    return (
        f'{_title(block)}<table class="pkg-table"><tbody>{"".join(rows)}</tbody></table>'
        f"{_caption(block)}"
    )


def _render_list(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    items = []
    for name in _fields_of(block, package):
        spec = _spec(package, name)
        items.append(
            f'<li><div class="pkg-lab">{esc(_label_of(name, spec))}</div>'
            f'<div class="pkg-row-value">{_value_cell(name, spec, values.get(name))}</div>'
            f"{_description(spec)}</li>"
        )
    return f'{_title(block)}<ul class="pkg-list">{"".join(items)}</ul>{_caption(block)}'


def _render_note(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    inline = []
    for name in _fields_of(block, package):
        spec = _spec(package, name)
        inline.append(
            f'<span class="pkg-inline">{_value_cell(name, spec, values.get(name))}'
            f'<span class="pkg-inline-lab">{esc(_label_of(name, spec))}</span></span>'
        )
    title = block.get("title")
    headline = (
        f'<p class="pkg-note-head">{esc(title)}</p>'
        if isinstance(title, str) and title.strip()
        else ""
    )
    return (
        f'<div class="pkg-note-box">{headline}'
        f'<div class="pkg-note-values">{"".join(inline)}</div>{_caption(block)}</div>'
    )


def _render_bars(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    names = _fields_of(block, package)
    numbers = {name: _as_number(values.get(name)) for name in names}
    largest = max((abs(v) for v in numbers.values() if v is not None), default=0.0)
    rows = []
    for name in names:
        spec = _spec(package, name)
        number = numbers[name]
        share = 0.0 if (number is None or largest <= 0) else abs(number) / largest
        rows.append(
            f'<div class="pkg-bar-row" data-pkg-bar="{esc_attr(name)}">'
            f'<div class="pkg-lab">{esc(_label_of(name, spec))}</div>'
            f'<div class="pkg-track"><div class="pkg-fill" '
            f'style="width:{share * 100:.2f}%"></div></div>'
            f'<div class="pkg-bar-value">{_value_cell(name, spec, values.get(name))}</div>'
            f"</div>"
        )
    return f'{_title(block)}<div class="pkg-bars">{"".join(rows)}</div>{_caption(block)}'


def _render_gauge(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    names = _fields_of(block, package)
    value_name = names[0]
    total_name = names[1] if len(names) > 1 else None
    value = _as_number(values.get(value_name)) or 0.0
    total = _as_number(values.get(total_name)) if total_name else None
    share = 0.0 if not total or total <= 0 else max(0.0, min(1.0, value / total))
    # 2 * pi * r for r = 52, the circle drawn below. Written as the arithmetic so
    # a change to the radius cannot leave a stale constant behind.
    circumference = 2 * math.pi * 52
    offset = circumference * (1 - share)
    total_cell = ""
    if total_name is not None:
        total_spec = _spec(package, total_name)
        total_cell = (
            f'<div class="pkg-gauge-total">of '
            f"{_value_cell(total_name, total_spec, values.get(total_name))} "
            f"{esc(_label_of(total_name, total_spec))}</div>"
        )
    return (
        f'{_title(block)}<div class="pkg-gauge" data-pkg-gauge="{esc_attr(value_name)}"'
        f' data-pkg-total="{esc_attr(total_name or "")}"'
        f' data-pkg-circumference="{circumference:.3f}">'
        f'<svg class="pkg-ring" viewBox="0 0 120 120" role="presentation">'
        f'<circle class="pkg-ring-track" cx="60" cy="60" r="52"></circle>'
        f'<circle class="pkg-ring-fill" cx="60" cy="60" r="52"'
        f' stroke-dasharray="{circumference:.3f}"'
        f' stroke-dashoffset="{offset:.3f}"></circle></svg>'
        f'<div class="pkg-gauge-read">'
        f'<div class="pkg-figure">{_value_cell(value_name, _spec(package, value_name), values.get(value_name))}</div>'
        f'<div class="pkg-lab">{esc(_label_of(value_name, _spec(package, value_name)))}</div>'
        f"{total_cell}</div></div>{_caption(block)}"
    )


def _render_pills(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    chips = []
    for name in _fields_of(block, package):
        spec = _spec(package, name)
        chips.append(
            f'<div class="pkg-chip">'
            f'<span class="pkg-lab">{esc(_label_of(name, spec))}</span>'
            f"{_value_cell(name, spec, values.get(name))}</div>"
        )
    return f'{_title(block)}<div class="pkg-pills">{"".join(chips)}</div>{_caption(block)}'


def _render_timeline(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    values = read.get("fields", {})
    names = _fields_of(block, package)

    # Ordered by the instants the block names, oldest first; a field with no
    # instant of its own keeps the author's order after the dated ones.
    def key(name: str) -> tuple[int, float, int]:
        spec = _spec(package, name)
        if spec.get("type") != "timestamp":
            return (1, 0.0, names.index(name))
        raw = values.get(name)
        moment = _parse_instant(raw) if isinstance(raw, str) else None
        if moment is None:
            return (1, 0.0, names.index(name))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (0, moment.timestamp(), names.index(name))

    items = []
    for name in sorted(names, key=key):
        spec = _spec(package, name)
        items.append(
            f'<li class="pkg-event" data-pkg-event="{esc_attr(spec.get("type"))}">'
            f'<span class="pkg-dot" aria-hidden="true"></span>'
            f'<span class="pkg-lab">{esc(_label_of(name, spec))}</span>'
            f"{_value_cell(name, spec, values.get(name))}</li>"
        )
    return f'{_title(block)}<ol class="pkg-timeline">{"".join(items)}</ol>{_caption(block)}'


def _render_orbit(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    """Numbers as a turnable ring, with the same numbers as text beside it.

    The flat list is in the DOM ALWAYS, not behind a failed WebGL check: it
    carries the bindings, so a value a reader needs is reachable as text whether
    the scene drew or not, and the toggle only changes which one is on top.
    """
    values = read.get("fields", {})
    names = _fields_of(block, package)
    rows = []
    scene: list[dict[str, Any]] = []
    numbers = [_as_number(values.get(n)) or 0.0 for n in names]
    largest = max((abs(v) for v in numbers), default=0.0) or 1.0
    for index, name in enumerate(names):
        spec = _spec(package, name)
        rows.append(
            f'<li><span class="pkg-lab">{esc(_label_of(name, spec))}</span>'
            f"{_value_cell(name, spec, values.get(name))}</li>"
        )
        scene.append(
            {
                "name": name,
                "label": _label_of(name, spec),
                "share": round(abs(numbers[index]) / largest, 4),
            }
        )
    blob = escape_json_for_html(json.dumps(scene, ensure_ascii=False))
    return (
        f'{_title(block)}<div class="pkg-orbit" data-pkg-view="3d">'
        f'<script type="application/json" class="pkg-orbit-scene">{blob}</script>'
        f'<div class="pkg-orbit-stage"></div>'
        f'<ol class="pkg-orbit-flat">{"".join(rows)}</ol>'
        f'<button class="pkg-orbit-toggle" type="button">Show as text</button>'
        f"</div>{_caption(block)}"
    )


#: Block type name -> its renderer. Keyed by
#: :func:`~kiro_crew.artifact_store.dashboard_package.view_block_catalog`, and a
#: key here that is not in the catalog -- or a catalog entry missing here -- is a
#: test failure in ``test_dashboard_package_render.py``.
BLOCK_RENDERERS: Final[
    Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], str]]
] = {
    "stat": _render_stat,
    "stat_band": _render_stat_band,
    "table": _render_table,
    "list": _render_list,
    "note": _render_note,
    "bars": _render_bars,
    "gauge": _render_gauge,
    "pills": _render_pills,
    "timeline": _render_timeline,
    "orbit": _render_orbit,
}


def render_block(
    block: Mapping[str, Any], package: Mapping[str, Any], read: Mapping[str, Any]
) -> str:
    """One block as its ``<section>``, or an honest placeholder if it has no renderer.

    A block type with no renderer cannot be stored -- the gate reads the same
    catalog this dispatch table is pinned against -- so the placeholder is for a
    package written by a NEWER build and read by this one. It says which block
    and which type, because the alternative is a hole in the page with no name
    on it.
    """
    block_id = str(block.get("id", ""))
    kind = str(block.get("type", ""))
    span = block.get("span")
    span_attr = f' style="--pkg-span:{int(span)}"' if isinstance(span, int) else ""
    renderer = BLOCK_RENDERERS.get(kind)
    if renderer is None or not _fields_of(block, package):
        body = (
            f'<p class="pkg-unrenderable">This build has no renderer for a '
            f"{esc(kind)} block.</p>"
        )
    else:
        body = renderer(block, package, read)
    return (
        f'<section class="pkg-block pkg-block--{esc_attr(kind)}"'
        f' id="block-{esc_attr(block_id)}" data-pkg-block="{esc_attr(block_id)}"'
        f"{span_attr}>{body}</section>"
    )


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def vendor_source(name: str) -> str:
    """One vendored library's bytes, read from :mod:`dashboard_package_vendor`."""
    filename = VENDOR_FILES[name]
    return (_VENDOR_DIR / filename).read_text(encoding="utf-8")


def _needs_three(package: Mapping[str, Any]) -> bool:
    blocks = package.get("view", {}).get("blocks", ())
    return any(str(b.get("type")) in _NEEDS_THREE for b in blocks)


def _inline_script(source: str) -> str:
    """A classic script element holding *source*.

    ``</script`` is split rather than escaped, because the bytes are a vendored
    library and this must not alter what executes: the HTML parser stops at the
    literal sequence, so breaking it with a string concatenation that evaluates
    to the same characters keeps the program identical.
    """
    safe = source.replace("</script", '</scr" + "ipt')
    return f"<script>{safe}</script>"


def _style(package: Mapping[str, Any], theme: str) -> str:
    tokens = theme_tokens(package, theme)
    declared = "".join(f"{name}:{value};" for name, value in tokens.items())
    extra = theme_css(package)
    return f"<style>:root{{{declared}}}\n{_PAGE_CSS}\n{extra}</style>"


def render_dashboard(
    package: Mapping[str, Any],
    read: Mapping[str, Any],
    *,
    theme: str = "light",
    title: str = "",
) -> str:
    """The whole document for one dashboard package and one read of its values.

    *package* is a canonical package (what
    :func:`~kiro_crew.artifact_store.dashboard_package.parse_package` returns).
    *read* is the payload shape
    :func:`kiro_crew.dashboard_frame.read_payload` builds. *theme* picks the
    base palette the package's tokens override.
    """
    if theme not in THEMES:
        theme = THEMES[0]
    blocks = package.get("view", {}).get("blocks", ())
    body = "".join(render_block(block, package, read) for block in blocks)
    island = dict(read)
    island["display"] = display_values(package, read.get("fields", {}))
    island["formats"] = {
        name: {
            "type": spec.get("type"),
            "unit": spec.get("unit"),
            "precision": spec.get("precision"),
        }
        for name, spec in package.get("model", {}).get("types", {}).items()
    }
    blob = escape_json_for_html(json.dumps(island, ensure_ascii=False, allow_nan=False))
    libraries = _inline_script(vendor_source("anime"))
    if _needs_three(package):
        libraries += "\n" + _inline_script(vendor_source("three"))
    return (
        "<!doctype html>\n"
        f'<html lang="en" data-pkg-theme="{esc_attr(theme)}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{RENDER_CSP}">\n'
        f"<title>{esc(title or 'Dashboard')}</title>\n"
        f"{_style(package, theme)}\n"
        "</head>\n"
        "<body>\n"
        '<div id="kirocrew-stale-band" role="status" hidden>'
        "<strong>Some numbers are older.</strong> <span data-stale-detail></span></div>\n"
        f'<script type="application/json" id="{_DATA_ELEMENT_ID}">{blob}</script>\n'
        f'<main class="pkg-grid">{body}</main>\n'
        f"{libraries}\n"
        f"<script>{_PAGE_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )


_PAGE_CSS: Final[str] = """
*{box-sizing:border-box}
html{color-scheme:light}
html[data-pkg-theme='dark']{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--text);
  font:13px/1.55 var(--text-font);-webkit-font-smoothing:antialiased}
.pkg-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:26px 30px;
  max-width:1120px;margin:0 auto;padding:26px 30px 34px;align-items:start}
.pkg-block{grid-column:span var(--pkg-span,12);min-width:0}
.pkg-title{font:600 10.5px/1.2 var(--text-font);letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted-strong);margin:0 0 8px;padding-bottom:5px;
  border-bottom:1px solid var(--text-strong)}
.pkg-caption{margin:9px 0 0;font-size:11px;color:var(--muted);
  border-top:1px solid var(--border);padding-top:6px}
.pkg-lab{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.pkg-note{font-size:11px;color:var(--muted-strong)}
.pkg-figure{font-family:var(--display-font);font-size:38px;line-height:1.05;
  color:var(--text-strong);font-variant-numeric:lining-nums tabular-nums;
  letter-spacing:-.01em}
/* A figure slot is sized for a NUMBER. A line of prose set at 38px turns one
   cell of a five-figure band into a column six lines deep and the band stops
   being readable at a glance, so text and instants keep their own scale inside
   it and prose is clamped to three lines. */
.pkg-figure .pkg-v--text{font-family:var(--text-font);font-size:14px;line-height:1.45;
  color:var(--text);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  overflow:hidden}
.pkg-figure .pkg-v--timestamp{font-size:21px}
.pkg-stat .pkg-figure .pkg-v--text{font-size:16px}
.pkg-stat .pkg-figure .pkg-v--timestamp{font-size:26px}
/* The figure slot's own font-size still sets the line box, so a 12px chip in a
   52px slot leaves 40px of empty air above it. Shrink the SLOT to match what it
   holds. */
.pkg-figure:has(.pkg-v--enum),.pkg-figure:has(.pkg-v--bool),
.pkg-figure:has(.pkg-v--text){font-size:16px;line-height:1.4}
.pkg-figure:has(.pkg-v--timestamp){font-size:22px;line-height:1.25}
.pkg-v--number{font-variant-numeric:lining-nums tabular-nums}
.pkg-v--timestamp{font-family:var(--mono-font);font-size:.92em;letter-spacing:-.01em}
.pkg-v--text{color:var(--text)}
.pkg-v--enum,.pkg-v--bool{display:inline-flex;align-items:center;gap:6px;
  font-size:11.5px;font-weight:600;color:var(--text-strong);padding:2px 9px 2px 7px;
  border:1px solid var(--border-strong);border-radius:999px;background:var(--surface)}
.pkg-glyph{width:8px;height:8px;background:var(--muted);display:inline-block;flex:none}
[data-pkg-state='good'] .pkg-glyph{background:var(--good);border-radius:50%}
[data-pkg-state='warn'] .pkg-glyph{background:var(--warn);
  clip-path:polygon(50% 0,100% 100%,0 100%)}
[data-pkg-state='bad'] .pkg-glyph{background:var(--bad);transform:rotate(45deg)}
[data-pkg-state='on'] .pkg-glyph{background:var(--good);border-radius:50%}
[data-pkg-state='off'] .pkg-glyph{background:transparent;border:1.5px solid var(--muted)}
[data-pkg-state='good'],[data-pkg-state='on']{border-color:var(--good)}
[data-pkg-state='warn']{border-color:var(--warn);color:var(--warn)}
[data-pkg-state='bad']{border-color:var(--bad);color:var(--bad)}
.pkg-stat .pkg-figure{font-size:52px}
.pkg-band{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;
  border-top:1px solid var(--text-strong);border-bottom:1px solid var(--border-strong)}
.pkg-band-cell{padding:11px 15px 13px}
.pkg-band-cell+.pkg-band-cell{border-left:1px solid var(--border)}
.pkg-band .pkg-figure{font-size:34px}
.pkg-table{width:100%;border-collapse:collapse}
.pkg-table th,.pkg-table td{text-align:left;padding:7px 0;
  border-bottom:1px solid var(--border);vertical-align:baseline}
.pkg-table th{font:400 10px/1.5 var(--text-font);letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted);width:45%}
.pkg-table td{text-align:right;color:var(--text-strong);
  font-variant-numeric:lining-nums tabular-nums}
.pkg-table tr:last-child th,.pkg-table tr:last-child td{border-bottom:0}
.pkg-list{list-style:none;margin:0;padding:0}
.pkg-list li{padding:9px 0;border-bottom:1px solid var(--border)}
.pkg-list li:last-child{border-bottom:0}
.pkg-row-value{font-size:15px;color:var(--text-strong);margin-top:2px}
.pkg-note-box{border-left:3px solid var(--accent);padding:13px 16px 14px;
  background:var(--accent-soft, color-mix(in srgb, var(--accent) 13%, var(--bg)))}
.pkg-note-head{font-family:var(--display-font);font-size:20px;line-height:1.25;
  color:var(--text-strong);margin:0 0 9px}
.pkg-note-values{display:flex;flex-wrap:wrap;gap:22px}
.pkg-inline{display:flex;flex-direction:column}
.pkg-inline .pkg-v{font-family:var(--display-font);font-size:24px;color:var(--text-strong)}
.pkg-inline-lab{font-size:10px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted-strong)}
.pkg-bar-row{display:grid;grid-template-columns:minmax(90px,.9fr) minmax(0,2.2fr) auto;
  gap:12px;align-items:center;padding:6px 0}
.pkg-track{height:9px;background:var(--track);overflow:hidden}
.pkg-fill{height:100%;background:var(--accent);transition:none}
.pkg-bar-value{font-variant-numeric:lining-nums tabular-nums;color:var(--text-strong);
  font-family:var(--display-font);font-size:15px;text-align:right}
.pkg-gauge{display:flex;align-items:center;gap:20px}
.pkg-ring{width:118px;height:118px;flex:none;transform:rotate(-90deg)}
.pkg-ring-track{fill:none;stroke:var(--track);stroke-width:11}
.pkg-ring-fill{fill:none;stroke:var(--accent);stroke-width:11;stroke-linecap:butt}
.pkg-gauge-total{font-size:11px;color:var(--muted-strong);margin-top:5px}
.pkg-gauge-total .pkg-v{font-size:11px;color:var(--text-strong)}
.pkg-pills{display:flex;flex-wrap:wrap;gap:10px 18px}
.pkg-chip{display:flex;flex-direction:column;gap:4px;align-items:flex-start}
.pkg-timeline{list-style:none;margin:0;padding:0 0 0 14px;
  border-left:1px solid var(--border-strong)}
.pkg-event{display:grid;grid-template-columns:auto minmax(90px,auto) 1fr;gap:10px;
  align-items:baseline;padding:7px 0;position:relative}
.pkg-dot{position:absolute;left:-19px;top:12px;width:7px;height:7px;border-radius:50%;
  background:var(--text-strong)}
.pkg-event[data-pkg-event='timestamp'] .pkg-dot{background:var(--accent)}
.pkg-orbit{position:relative}
.pkg-orbit-stage{height:260px;background:var(--surface);
  border:1px solid var(--border);touch-action:none;overflow:hidden}
.pkg-orbit-stage canvas{display:block;width:100%;height:100%}
.pkg-orbit-flat{list-style:none;margin:10px 0 0;padding:0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:4px 18px}
.pkg-orbit-flat li{display:flex;justify-content:space-between;gap:10px;
  border-bottom:1px solid var(--border);padding:4px 0}
.pkg-orbit-flat .pkg-v{font-family:var(--display-font);font-size:15px;
  color:var(--text-strong)}
.pkg-orbit[data-pkg-view='flat'] .pkg-orbit-stage{display:none}
.pkg-orbit-toggle{margin-top:9px;font:inherit;font-size:11px;cursor:pointer;
  color:var(--text-strong);background:none;border:0;border-bottom:1px solid var(--text-strong);
  padding:0 0 1px}
.pkg-unrenderable{font-size:11.5px;color:var(--warn);margin:0}
#kirocrew-stale-band{margin:0;padding:7px 30px;font-size:11px;color:var(--warn);
  background:color-mix(in srgb, var(--warn) 12%, var(--bg));
  border-bottom:1px solid var(--warn)}
[data-dashboard-missing='true']{opacity:.55}
[data-dashboard-agentic='true']{border-bottom:1px dotted var(--muted)}
@media(max-width:860px){.pkg-grid{grid-template-columns:repeat(6,1fr);padding:18px}
  .pkg-block{grid-column:span 6}.pkg-band{grid-auto-flow:row;grid-auto-columns:auto}
  .pkg-band-cell+.pkg-band-cell{border-left:0;border-top:1px solid var(--border)}}
@media(prefers-reduced-motion:reduce){*{animation-duration:0s!important;
  transition-duration:0s!important}}
"""


_PAGE_JS: Final[str] = (
    """
(function () {
  var ISLAND = """
    + json.dumps(_DATA_ELEMENT_ID)
    + """;
  var GLOBAL = """
    + json.dumps(WINDOW_GLOBAL)
    + """;
  var BIND = """
    + json.dumps(BINDING_ATTRIBUTE)
    + """;
  var MESSAGE = """
    + json.dumps(DATA_MESSAGE_TYPE)
    + """;
  var PATCH = """
    + json.dumps(BLOCK_PATCH_MESSAGE_TYPE)
    + """;
  var EVENT = """
    + json.dumps(PAGE_EVENT)
    + """;
  var still = false;
  try {
    still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (err) { still = false; }
  var read = { fields: {}, display: {}, formats: {}, agentic: [], missing: [], stale: false };
  try { read = JSON.parse(document.getElementById(ISLAND).textContent); }
  catch (err) { read.stale = true; }

  function freeze(value) {
    if (value === null || typeof value !== 'object') return value;
    for (var key of Object.keys(value)) freeze(value[key]);
    return Object.freeze(value);
  }
  function publish() {
    Object.defineProperty(window, GLOBAL, {
      value: freeze(read), writable: false, configurable: true, enumerable: true,
    });
  }
  publish();

  // The PLAIN fallback, used only when a host pushed raw values without calling
  // dashboard_package_render.display_values. Deliberately unformatted: an
  // unformatted number is visibly unformatted, where a second set of formatting
  // rules in here would silently drift from the Python ones.
  function shown(name) {
    if (read.display && Object.prototype.hasOwnProperty.call(read.display, name)) {
      return read.display[name];
    }
    var raw = read.fields ? read.fields[name] : undefined;
    if (raw === null || raw === undefined) return '\\u2014';
    if (typeof raw === 'boolean') return raw ? 'Yes' : 'No';
    if (typeof raw === 'object') return JSON.stringify(raw);
    return String(raw);
  }
  function number(name) {
    var raw = read.fields ? read.fields[name] : undefined;
    var parsed = typeof raw === 'number' ? raw : parseFloat(raw);
    return isFinite(parsed) ? parsed : null;
  }

  // Every painter takes a ROOT. The whole page on first load; one block's own
  // <section> on a patch, because a patch names the blocks that moved and
  // repainting the rest would re-run their count-up animation for a number that
  // did not change -- which is exactly the decoration the brief rules out.
  function fill(root) {
    var scope = root || document;
    var agentic = {};
    for (var i = 0; i < (read.agentic || []).length; i++) agentic[read.agentic[i]] = true;
    var nodes = scope.querySelectorAll('[' + BIND + ']');
    for (var n = 0; n < nodes.length; n++) {
      var node = nodes[n];
      var name = node.getAttribute(BIND);
      var holder = node.closest('.pkg-v') || node;
      if (!read.fields || !Object.prototype.hasOwnProperty.call(read.fields, name)) {
        node.setAttribute('data-dashboard-missing', 'true');
        continue;
      }
      node.removeAttribute('data-dashboard-missing');
      // Text, never markup: a fold value is whatever the log recorded.
      node.textContent = shown(name);
      if (agentic[name]) holder.setAttribute('data-dashboard-agentic', 'true');
      else holder.removeAttribute('data-dashboard-agentic');
    }
    bars(scope);
    gauges(scope);
    countUp(scope);
    orbits(scope);
    band();
  }

  function bars(scope) {
    var groups = scope.querySelectorAll('.pkg-bars');
    for (var g = 0; g < groups.length; g++) {
      var rows = groups[g].querySelectorAll('.pkg-bar-row');
      var largest = 0;
      for (var r = 0; r < rows.length; r++) {
        var v = number(rows[r].getAttribute('data-pkg-bar'));
        if (v !== null && Math.abs(v) > largest) largest = Math.abs(v);
      }
      for (var k = 0; k < rows.length; k++) {
        var value = number(rows[k].getAttribute('data-pkg-bar'));
        var fillEl = rows[k].querySelector('.pkg-fill');
        if (!fillEl) continue;
        var share = (value === null || largest <= 0) ? 0 : Math.abs(value) / largest;
        var target = (share * 100).toFixed(2) + '%';
        if (still || typeof anime !== 'function') { fillEl.style.width = target; continue; }
        anime({ targets: fillEl, width: target, duration: 620, easing: 'easeOutQuart' });
      }
    }
  }

  function gauges(scope) {
    var rings = scope.querySelectorAll('.pkg-gauge');
    for (var i = 0; i < rings.length; i++) {
      var box = rings[i];
      var circumference = parseFloat(box.getAttribute('data-pkg-circumference')) || 0;
      var value = number(box.getAttribute('data-pkg-gauge'));
      var totalName = box.getAttribute('data-pkg-total');
      var total = totalName ? number(totalName) : null;
      var share = (!total || total <= 0 || value === null)
        ? 0 : Math.max(0, Math.min(1, value / total));
      var arc = box.querySelector('.pkg-ring-fill');
      if (!arc) continue;
      var offset = circumference * (1 - share);
      if (still || typeof anime !== 'function') { arc.style.strokeDashoffset = offset; continue; }
      anime({
        targets: arc, strokeDashoffset: [circumference, offset],
        duration: 760, easing: 'easeOutCubic',
      });
    }
  }

  // Numbers count up when they change, and a change is the only thing that
  // animates: a loop would be decoration, and the brief says every animation
  // says something happened.
  function countUp(scope) {
    if (still || typeof anime !== 'function') return;
    var cells = scope.querySelectorAll('.pkg-v--number[data-pkg-raw]');
    for (var i = 0; i < cells.length; i++) {
      var cell = cells[i];
      var name = cell.getAttribute(BIND);
      var end = number(name);
      if (end === null) continue;
      var target = shown(name);
      var from = parseFloat(cell.getAttribute('data-pkg-shown-raw'));
      if (!isFinite(from)) from = 0;
      if (from === end) continue;
      cell.setAttribute('data-pkg-shown-raw', String(end));
      var state = { n: from };
      var suffix = target.replace(/^[-\\d,.\\u2014\\s]+/, '');
      anime({
        targets: state, n: end, duration: 760, easing: 'easeOutExpo', round: 1,
        update: function (el, store, tail) {
          return function () {
            el.textContent = store.n.toLocaleString() + (tail ? ' ' + tail : '');
          };
        }(cell, state, suffix.trim()),
        complete: function (el, text) {
          return function () { el.textContent = text; };
        }(cell, target),
      });
    }
  }

  function band() {
    var bandEl = document.getElementById('kirocrew-stale-band');
    if (!bandEl) return;
    var detail = bandEl.querySelector('[data-stale-detail]');
    var missing = read.missing || [];
    bandEl.hidden = !read.stale;
    if (!detail) return;
    var listed = missing.slice(0, 3).join(', ');
    var rest = missing.length - 3;
    if (rest > 0) listed += ' and ' + rest + ' more';
    detail.textContent = missing.length
      ? listed + (missing.length === 1 ? ' is' : ' are') +
        ' from an earlier read. Everything else on this page is current.'
      : 'Some values are from an earlier read. Everything else is current.';
  }

  // ---- the 3D block -------------------------------------------------------
  function orbits(scope) {
    var boxes = (scope || document).querySelectorAll('.pkg-orbit');
    for (var i = 0; i < boxes.length; i++) orbit(boxes[i]);
  }
  function orbit(box) {
    // IDEMPOTENT. orbits() runs on first paint and again for any block a patch
    // touched, and a second pass over a stage that already has a canvas would
    // append another one and leave two scenes animating over each other.
    var existing = box.querySelector('.pkg-orbit-stage canvas');
    if (existing) return;
    var toggle = box.querySelector('.pkg-orbit-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var flat = box.getAttribute('data-pkg-view') === 'flat';
        box.setAttribute('data-pkg-view', flat ? '3d' : 'flat');
        toggle.textContent = flat ? 'Show as text' : 'Show the ring';
      });
    }
    var stage = box.querySelector('.pkg-orbit-stage');
    var source = box.querySelector('.pkg-orbit-scene');
    if (!stage || !source || typeof THREE === 'undefined') {
      box.setAttribute('data-pkg-view', 'flat');
      if (toggle) { toggle.textContent = 'Show the ring'; toggle.disabled = true; }
      return;
    }
    var bodies = [];
    try { bodies = JSON.parse(source.textContent); } catch (err) { bodies = []; }
    if (!bodies.length) { box.setAttribute('data-pkg-view', 'flat'); return; }
    var style = getComputedStyle(document.documentElement);
    var accent = (style.getPropertyValue('--accent') || '#00d492').trim();
    var quiet = (style.getPropertyValue('--border-strong') || '#3a3f4a').trim();
    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (err) {
      box.setAttribute('data-pkg-view', 'flat');
      if (toggle) { toggle.textContent = 'Show the ring'; toggle.disabled = true; }
      return;
    }
    var width = stage.clientWidth || 600;
    var height = stage.clientHeight || 260;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    // updateStyle left at its default TRUE. Passing false keeps the canvas at its
    // backing-store size with no CSS size, so on a 2x display the element lays out
    // at twice the stage and the scene spills over everything under it.
    renderer.setSize(width, height);
    stage.appendChild(renderer.domElement);
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100);
    camera.position.set(0, 2.5, 7.4);
    camera.lookAt(0, 0, 0);
    var group = new THREE.Group();
    scene.add(group);
    var ring = new THREE.Mesh(
      new THREE.TorusGeometry(3.6, 0.012, 3, 160),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(quiet) })
    );
    ring.rotation.x = Math.PI / 2;
    group.add(ring);
    for (var b = 0; b < bodies.length; b++) {
      var angle = (b / bodies.length) * Math.PI * 2;
      var size = 0.16 + 0.42 * (bodies[b].share || 0);
      var body = new THREE.Mesh(
        new THREE.SphereGeometry(size, 24, 16),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(accent) })
      );
      body.position.set(Math.cos(angle) * 3.6, 0, Math.sin(angle) * 3.6);
      group.add(body);
      var halo = new THREE.Mesh(
        new THREE.TorusGeometry(size * 1.9, 0.01, 3, 48),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(quiet) })
      );
      halo.position.copy(body.position);
      halo.rotation.x = Math.PI / 2;
      group.add(halo);
    }
    var spin = 0.38;
    var dragging = false;
    var lastX = 0;
    stage.addEventListener('pointerdown', function (event) {
      dragging = true; lastX = event.clientX;
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener('pointerup', function (event) {
      dragging = false;
      try { stage.releasePointerCapture(event.pointerId); } catch (err) { /* gone */ }
    });
    stage.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      spin += (event.clientX - lastX) * 0.008;
      lastX = event.clientX;
      draw();
    });
    function draw() {
      group.rotation.y = spin;
      renderer.render(scene, camera);
    }
    if (still) { draw(); return; }
    var started = null;
    function frame(now) {
      if (started === null) started = now;
      if (!dragging) spin = 0.38 + ((now - started) / 1000) * 0.12;
      draw();
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  addEventListener('message', function (event) {
    // The sender is checked BEFORE the type: this frame and every sibling the
    // dashboard embeds run with allow-scripts, so a sibling can reach
    // parent.frames[i] and post here. Without this test it could replace every
    // recorded number on the page with one it chose.
    if (event.source !== parent) return;
    var data = event.data;
    if (!data) return;
    if (data.type === MESSAGE && data.read && typeof data.read === 'object') {
      read = data.read;
      publish();
      fill();
      window.dispatchEvent(new Event(EVENT));
      return;
    }
    if (data.type !== PATCH || !data.blocks || typeof data.blocks !== 'object') return;
    // A BLOCK PATCH: only the blocks that subscribe to the fold that advanced.
    // Values only -- no markup crosses this boundary. The model and the view
    // change only when the package is versioned, and that is a reload, so a
    // patch has nothing to rebuild and this stays a path that cannot introduce
    // an element.
    var merged = { fields: {}, display: {} };
    for (var key of Object.keys(read)) merged[key] = read[key];
    merged.fields = Object.assign({}, read.fields);
    merged.display = Object.assign({}, read.display);
    var touched = [];
    for (var id of Object.keys(data.blocks)) {
      var patch = data.blocks[id];
      if (!patch || typeof patch !== 'object') continue;
      var section = document.querySelector('[data-pkg-block="' + CSS.escape(id) + '"]');
      if (!section) continue;
      Object.assign(merged.fields, patch.fields || {});
      Object.assign(merged.display, patch.display || {});
      touched.push(section);
    }
    if (!touched.length) return;
    if (typeof data.seq === 'number') merged.seq = data.seq;
    if (typeof data.stale === 'boolean') merged.stale = data.stale;
    if (Array.isArray(data.missing)) merged.missing = data.missing;
    read = merged;
    publish();
    for (var t = 0; t < touched.length; t++) fill(touched[t]);
    band();
    window.dispatchEvent(new Event(EVENT));
  });

  function start() { fill(); }
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', start);
  else start();
})();
"""
)


def blocks_reading(package: Mapping[str, Any], field_names: Sequence[str]) -> list[str]:
    """The ids of the blocks that render any of *field_names*, in view order.

    What a controller needs in order to push to the blocks a fold actually
    moved rather than to the whole page. The same answer
    ``DashboardModel.subscribers`` gives per field, over a set of fields and in
    one call.
    """
    wanted = set(field_names)
    return [
        str(block.get("id"))
        for block in package.get("view", {}).get("blocks", ())
        if wanted.intersection(block.get("fields", ()))
    ]


def block_patch(
    package: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    block_ids: Sequence[str] | None = None,
    seq: int = 0,
    stale: bool = False,
    missing: Sequence[str] = (),
) -> dict[str, Any]:
    """The payload for a :data:`BLOCK_PATCH_MESSAGE_TYPE` message.

    *fields* is the values that moved. With no *block_ids* the blocks are worked
    out from the package by :func:`blocks_reading`, so a caller holding a fold's
    new values does not also have to hold the subscription map.

    Each block carries only the fields IT renders, so a patch cannot leak a
    value to a block the view never put it on, and the formatted strings come
    from :func:`display_values` -- the same formatter the first render used,
    which is what keeps a refilled cell and a first-painted cell the same.
    """
    declared = package.get("model", {}).get("types", {})
    known = {name: value for name, value in fields.items() if name in declared}
    shown = display_values(package, known)
    ids = list(block_ids) if block_ids is not None else blocks_reading(package, list(known))
    by_block: dict[str, Any] = {}
    for block in package.get("view", {}).get("blocks", ()):
        block_id = str(block.get("id"))
        if block_id not in ids:
            continue
        mine = [name for name in block.get("fields", ()) if name in known]
        if not mine:
            continue
        by_block[block_id] = {
            "fields": {name: known[name] for name in mine},
            "display": {name: shown[name] for name in mine if name in shown},
        }
    return {
        "type": BLOCK_PATCH_MESSAGE_TYPE,
        "blocks": by_block,
        "seq": int(seq),
        "stale": bool(stale),
        "missing": sorted(missing),
    }


def unrenderable_types() -> dict[str, list[str]]:
    """Catalog entries this build cannot draw, in both directions. Empty is the contract.

    ``blocks`` holds block types with no renderer; ``fields`` holds
    ``<block>.<data type>`` pairs a block admits but has no formatter for. The
    test asserts both lists are empty by ENUMERATING the catalogs, so a type
    added upstream names itself here rather than rendering as a blank cell.
    """
    blocks = sorted(set(view_block_catalog()) - set(BLOCK_RENDERERS))
    known = set(data_type_catalog())
    pairs: list[str] = []
    for name, entry in sorted(view_block_catalog().items()):
        for kind in sorted(entry.accepts):
            if kind not in known or kind not in _FORMATTERS:
                pairs.append(f"{name}.{kind}")
    return {"blocks": blocks, "fields": pairs}
