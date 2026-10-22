"""Gateway-side checks of a crewmate's saved page, for the ``dashboard_save`` reply.

The offline page run is skipped on a host with no browser, so two mistakes seen on
crewmate pages are checked here on the package text instead: a list, record or time
bound to a ``data-dashboard-field`` cell, which the host fills as raw text, and a
theme whose fixed colours cover one colour scheme only, including a surface the page
paints itself under text that keeps the tab's scheme colours. All answer in words
and never refuse the save.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

#: How a page shows each field type a ``data-dashboard-field`` cell shows raw.
RENDERING: Final[dict[str, str]] = {
    "array": (
        "A list of rows, read whole through window.kirocrew.subscribe. Its row keys "
        "can drive charts, comparisons, distributions, timelines or detail rows. "
        "A visible data-dashboard-field cell instead shows raw JSON such as []; "
        "a script-only field needs no text cell or hidden sink."
    ),
    "object": (
        "A nested record, read through window.kirocrew.subscribe and drawn from its "
        "published keys. A visible data-dashboard-field cell instead shows raw JSON; "
        "a script-only field needs no text cell or hidden sink."
    ),
    "timestamp": (
        "An ISO-8601 time. Format it in script (new Date(value).toLocaleString(), "
        "or an elapsed time with a known start). A text binding shows the raw stamp."
    ),
}

#: One line of a save reply's ``shape_warnings``.
SHAPE_WARNING: Final[str] = (
    "{field} is {kind} and is bound to a text cell. If visible without formatting, "
    "that cell shows the raw value. {how}"
)

#: A CSS-text heuristic, not a measured contrast failure.
THEME_ONE_SCHEME: Final[str] = (
    "Colour check: fixed text or surface colours may not be paired in both schemes. "
    "This CSS-text check does not measure contrast. Inspect the page in light and dark "
    "and pair each surface with readable text, using the dashboard's theme variables "
    "or your own scheme-aware colours. Keep the intended design; removing colour is "
    "not the goal."
)

#: ``{names}`` is the unresolved variables; ``{host}`` lists host-defined choices.
UNDEFINED_VAR: Final[str] = (
    "{names} is not a variable defined by the Dashboard tab, your theme tokens or "
    "your stylesheet, so its fixed fallback is used. Check that fallback on its "
    "actual surface in light and dark, or define the custom property. Available "
    "host variables: {host}. A fixed colour alone is not a contrast failure."
)

#: How many CSS rules, and selectors per rule, the colour checks read. The checks
#: compare surfaces with text rules pairwise, so the reading is bounded rather than
#: let grow with whatever a page holds; a page past it is told the check stopped.
MAX_THEME_RULES: Final[int] = 400
MAX_SELECTOR_PARTS: Final[int] = 16

#: Said when the colour checks stopped at :data:`MAX_THEME_RULES`.
THEME_CUT_SHORT: Final[str] = (
    "Colour check: this page has more than {limit} CSS rules, so only the first "
    "{limit} were checked. Inspect the rest of the page in light and dark yourself."
)

#: The text scan cannot prove where a positioned decorative layer sits.
PAINTED_SURFACE: Final[str] = (
    "Colour check on {selectors}: this fixed background may sit under text that "
    "follows the Dashboard scheme. Inspect whether text actually appears on it and "
    "reads clearly in light and dark. Pair surface and text colours if needed. "
    "A decorative layer without text can remain; this is a CSS-text heuristic, "
    "not a measured contrast failure. In var(--text, ...), the fallback is unused "
    "while the tab defines --text. Pair a fixed surface with its own text property, "
    "including captions and bound cells."
)

#: ``{n}`` families, ``{names}`` the first few. A pairing rule, not a refusal.
FONT_FAMILIES: Final[str] = (
    "Type check: this page sets {n} font families ({names}). It may read calmer with "
    "at most two, each with a clear job: one that reads, for headings and body, and "
    "one for numbers, code or ids. Build hierarchy with size, weight and colour "
    "instead of another family, and align figures with font-variant-numeric: "
    "tabular-nums. A style the user named wins."
)

#: ``{names}`` the named typefaces. A named face may be missing on the reader's system.
FONT_NAMED: Final[str] = (
    "Type check: {names} names a typeface. A named face may be missing on the reader's "
    "system, or differ from one system to the next. Use the frame's role variables "
    "var(--font-sans), var(--font-serif) and var(--font-mono), or a generic family, "
    "unless the user named this face."
)

#: Useful host variables; page-defined custom properties work too.
HOST_COLOUR_VARS: Final[str] = (
    "--bg, --bg-elevated, --card, --card-fg, --text, --text-strong, --muted, --border, "
    "--accent, --accent-subtle, --ok, --warn, --danger"
)

#: ``verification.note`` on a save whose page run was skipped.
VERIFICATION_SKIPPED: Final[str] = (
    "The page was NOT run: {reason}. Nothing checked that it draws, that its script "
    "runs or that it reads in light and dark, so check those yourself in the page "
    "before you apply it."
)

#: Hand-written observations have provenance and age rather than automatic freshness.
MANUAL_FIELDS_NOTE: Final[str] = (
    "These fields change through dashboard_write, not an automatic source update. "
    "Use a fold where it records the same fact. Verified project snapshots and your "
    "own verdicts can be manual: show their source and observation time, and update "
    "them when that source changes. Arrays and objects can carry measured samples; "
    "an old observation is not a live reading."
)

#: The field types a page has to draw itself: a text cell shows them raw.
_DRAWN_BY_SCRIPT: Final[tuple[str, ...]] = ("array", "object", "timestamp")


def page_rendering() -> dict[str, str]:
    """How a page shows each field type a ``data-dashboard-field`` cell cannot."""
    return dict(RENDERING)


def skipped_note(reason: str) -> str:
    """The words a skipped page run carries, so it is not read as a pass."""
    return VERIFICATION_SKIPPED.format(reason=reason or "no reason given")


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
            out.append(SHAPE_WARNING.format(field=name, kind=kind, how=RENDERING[kind]))
    return out


#: A colour written as a value rather than read from a variable.
_FIXED_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(")
_COLOUR_DECL = re.compile(r"(?<![-\w])(color|background(?:-color)?)\s*:\s*([^;}]+)")
#: No ``\s*`` after the comma: it would overlap the fallback's ``[^()]*`` and
#: backtrack quadratically over a long run of spaces. Readers strip the fallback.
_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,([^()]*(?:\([^()]*\)[^()]*)*))?\)")
_FG_TOKEN = re.compile(r"(?:fg|text|ink|fore)", re.I)
_BG_TOKEN = re.compile(r"(?:bg|back|surface|panel|board|paper)", re.I)
#: Ways a theme covers both schemes, or defers to the host's scheme-aware variables.
_BOTH_SCHEMES = ("prefers-color-scheme", "light-dark(", "data-mode", "data-theme")
#: The variables the Dashboard tab sets in the page for the scheme in force
#: (``website/src/lib/widgetSrcdoc.ts`` ``THEME_VAR_NAMES``): reading one is never fixed.
HOST_THEME_VARS: Final[frozenset[str]] = frozenset(
    {
        "--bg", "--bg-elevated", "--bg-hover", "--card", "--card-fg", "--text",
        "--text-strong", "--muted", "--muted-strong", "--border", "--border-strong",
        "--accent", "--accent-hover", "--accent-subtle", "--ok", "--ok-subtle", "--warn",
        "--warn-subtle", "--danger", "--danger-subtle", "--info",
    }
)  # fmt: skip
#: Selectors whose background is the whole page's, so every other rule sits on it.
_PAGE_SELECTORS = frozenset({"html", "body", ":root", "*"})


def _opaque(value: str) -> bool:
    """False for a colour that lets the background through (``rgba(..., 0.04)``)."""
    # Read with string splits, not a backtracking pattern: *value* is page text.
    low = value.casefold()
    for m in _COLOUR_FN.finditer(low):
        end = low.find(")", m.end())
        if end < 0:
            break
        inside = low[m.end() : end]
        if "/" in inside:
            raw = inside.rsplit("/", 1)[1].strip()
        elif m.group(1) in ("rgba", "hsla") and "," in inside:
            raw = inside.rsplit(",", 1)[1].strip()
        else:
            continue
        try:
            number = float(raw.rstrip("%")) / (100 if raw.endswith("%") else 1)
        except ValueError:
            return True
        return number >= 0.5
    return True


_COLOUR_FN = re.compile(r"\b(rgba?|hsla?|oklch|oklab|lab|lch)\(")


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _style_bodies(html: str) -> str:
    """The text of every ``<style>`` element in *html*, joined, in one linear pass.

    The tags are found in an ASCII-lowered copy of the same length, so each offset in it
    is an offset into *html*: ``casefold`` turns "ß" into "ss" and would shift them.
    """
    low = html.translate(_ASCII_LOWER)
    out: list[str] = []
    at = 0
    while True:
        start = low.find("<style", at)
        if start < 0:
            break
        open_end = low.find(">", start)
        if open_end < 0:
            break
        close = low.find("</style>", open_end + 1)
        if close < 0:
            break
        out.append(html[open_end + 1 : close])
        at = close + len("</style>")
    return " ".join(out)


def _resolve(value: str, tokens: Mapping[str, Any], *, pin_host: bool = True) -> str:
    """*value* with each ``var()`` replaced by what it draws in a scheme the page fixes.

    A theme token becomes its value, checked first because a token that sets a host
    variable (``--accent-subtle: #1c3a4f``) pins it to that one value in both schemes;
    a token written with ``light-dark()`` follows the scheme. A host theme variable the
    tokens leave alone follows the scheme, so it becomes ``""`` (nothing fixed); any
    other variable, which nothing defines, draws its fallback. With *pin_host* off a
    host variable follows the scheme whatever the tokens set: a page's own accent used
    for a text mark reads alike in both schemes, so it is not a one-scheme colour.
    """

    def one(m: re.Match[str]) -> str:
        name, fallback = m.group(1), (m.group(2) or "").strip()
        token = tokens.get(name)
        if isinstance(token, str) and (pin_host or name not in HOST_THEME_VARS):
            return "" if "light-dark(" in token else token
        if name in HOST_THEME_VARS:
            return ""
        return fallback

    return _VAR.sub(one, value)


def _rules(text: str) -> list[tuple[str, str]]:
    """Each innermost ``selector { body }`` in *text*, in one linear pass.

    A rule nested in ``@media`` yields its own selector. A pattern search here
    would backtrack over a long brace-free tail (an inlined source map) in time
    quadratic in its length, and this runs on text the crewmate wrote.
    """
    out: list[tuple[str, str]] = []
    chunks = text.split("}")
    for chunk in chunks[:-1]:  # the last piece has no closing brace
        head, brace, body = chunk.rpartition("{")
        if not brace:
            continue
        selector = head.rsplit("{", 1)[-1]
        if selector:
            out.append((selector, body))
    return out


def _parts(selector: str) -> list[list[str]]:
    """Each comma-separated selector as its compound steps (``.a > .b`` -> ``.a``, ``.b``)."""
    parts = [p.split() for p in re.sub(r"[>+~]", " ", selector).split(",") if p.strip()]
    return parts[:MAX_SELECTOR_PARTS]


def _covered(fg_steps: list[str], bg_selectors: list[list[str]]) -> bool:
    """Whether a rule with these steps sits on a fixed background a rule above it sets."""
    for bg in bg_selectors:
        if len(bg) == 1 and bg[0] in _PAGE_SELECTORS:
            return True
        if bg == fg_steps[: len(bg)]:
            return True
    return False


def theme_warnings(package: Mapping[str, Any]) -> list[str]:
    """A warning when the page's fixed colours cover one scheme only.

    Text with a fixed colour drawn on a background the host paints (or the reverse)
    reads in one scheme and not the other: light text on the host's white page. Each
    CSS rule that fixes a text colour is checked against a fixed opaque background on
    the same rule, on a rule for an ancestor selector (``.board`` above ``.board h1``)
    or on the page itself, so a chalkboard with chalk text reads the same in each and a
    coloured dot elsewhere does not count as the page's background. A ``var()`` of a
    host theme variable follows the scheme; any other ``var()`` draws what its token or
    fallback says. A fixed page background with no fixed text colour is the reverse
    case. A surface the page paints itself, with the tab's scheme text colours left on
    it, gets :data:`PAINTED_SURFACE` (see :func:`_painted_surfaces`). A theme that
    names both schemes is left alone. A heuristic on the CSS text
    (theme tokens, ``theme.css`` and the page's own ``<style>``), run here because the
    page run is skipped on a host with no browser.
    """
    theme = package.get("theme") or {}
    raw_tokens = theme.get("tokens") if isinstance(theme, Mapping) else None
    tokens: Mapping[str, Any] = raw_tokens if isinstance(raw_tokens, Mapping) else {}
    css = theme.get("css") if isinstance(theme, Mapping) else ""
    html = ((package.get("view") or {}).get("html")) or ""
    styles = _style_bodies(html) if isinstance(html, str) else ""
    text = f"{css if isinstance(css, str) else ''}\n{styles}"
    out = _undefined_var_warnings(text, html if isinstance(html, str) else "", tokens)
    out += font_warnings(text, html if isinstance(html, str) else "", tokens)
    if any(marker in text for marker in _BOTH_SCHEMES):
        return out
    fg_token = bg_token = False
    for name, value in tokens.items():
        if isinstance(value, str) and _FIXED_COLOUR.search(value):
            if _FG_TOKEN.search(str(name)):
                fg_token = True
            elif _BG_TOKEN.search(str(name)) and _opaque(value):
                bg_token = True
    fg_rules: list[tuple[list[list[str]], bool]] = []
    bg_selectors: list[list[str]] = []
    surfaces: list[tuple[str, list[list[str]], bool]] = []
    scheme_text: list[list[str]] = []
    rules = _rules(text)
    cut = len(rules) > MAX_THEME_RULES
    for selector, body in rules[:MAX_THEME_RULES]:
        fixed_fg = fixed_bg = scheme_fg = False
        for prop, value in _COLOUR_DECL.findall(body):
            drawn = _resolve(value, tokens, pin_host=prop != "color")
            if not _FIXED_COLOUR.search(drawn):
                scheme_fg = scheme_fg or (prop == "color" and "var(" in value)
                continue
            if prop == "color":
                fixed_fg = True
            elif _opaque(drawn):
                fixed_bg = True
        parts = _parts(selector.strip())
        if fixed_bg:
            bg_selectors.extend(parts)
            if not any(len(p) == 1 and p[0] in _PAGE_SELECTORS for p in parts):
                surfaces.append((selector.strip(), parts, _a_layer(body)))
        if fixed_fg:
            fg_rules.append((parts, fixed_bg))
        if scheme_fg and not fixed_fg:
            scheme_text.extend(parts)
    painted = _painted_surfaces(surfaces, scheme_text, fg_rules)
    page_bg = bg_token or any(len(b) == 1 and b[0] in _PAGE_SELECTORS for b in bg_selectors)
    uncovered = any(
        not own_bg and not page_bg and not all(_covered(p, bg_selectors) for p in parts)
        for parts, own_bg in fg_rules
    )
    one_scheme = (
        (fg_token and not bg_token)
        or uncovered
        or (page_bg and not fg_token and not fg_rules)
        or (bg_token and not fg_token and not fg_rules)
    )
    cut_note = [THEME_CUT_SHORT.format(limit=MAX_THEME_RULES)] if cut else []
    return ([THEME_ONE_SCHEME] if one_scheme else []) + painted + out + cut_note


#: A rule that lifts its element out of the flow, so it lies under or over other content.
_LAYER = re.compile(r"(?<![-\w])position\s*:\s*(?:absolute|fixed)\b")
_PX_SIZE = re.compile(r"(?<![-\w])(width|height)\s*:\s*(\d{1,6}(?:\.\d{1,4})?)px")
#: A positioned element no bigger than this in both directions is a mark, not a surface.
_MARK_PX: Final[float] = 48.0


def _a_layer(body: str) -> bool:
    """Whether a rule lays its element under or over other content, larger than a mark."""
    if not _LAYER.search(body):
        return False
    sizes = dict(_PX_SIZE.findall(body))
    return not (len(sizes) == 2 and all(float(v) <= _MARK_PX for v in sizes.values()))


def _painted_surfaces(
    surfaces: list[tuple[str, list[list[str]], bool]],
    scheme_text: list[list[str]],
    fg_rules: list[tuple[list[list[str]], bool]],
) -> list[str]:
    """One line naming the surfaces the page paints under text in the tab's colours.

    A surface is a rule with a fixed opaque background that is not the page's own. Its
    text is the tab's when a rule for it or an element inside it colours text with a
    variable that follows the scheme, or, for a layer laid under other content (a
    positioned element larger than a mark), when any text on the page keeps the tab's
    colours or the page fixes no text colour at all: where the layer lies is layout,
    which the CSS text alone does not settle.
    A fixed text colour on the surface or inside it is the page owning that text, the
    chalkboard case, and leaves the surface alone.
    """
    fixed_text = [p for parts, _ in fg_rules for p in parts]
    names: list[str] = []
    for selector, parts, layer in surfaces:

        def on(steps: list[str], parts: list[list[str]] = parts) -> bool:
            return any(steps[: len(s)] == s for s in parts)

        if any(on(p) for p in fixed_text):
            continue
        if any(on(t) for t in scheme_text) or (layer and (scheme_text or not fixed_text)):
            names.append(" ".join(selector.split())[:60])
    if not names:
        return []
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return [PAINTED_SURFACE.format(selectors=shown)]


#: A custom property the page's own CSS defines (``--name: value``).
_DEFINED_VAR = re.compile(r"(?<![\w-])(--[\w-]+)\s*:")


def _undefined_var_warnings(text: str, html: str, tokens: Mapping[str, Any]) -> list[str]:
    """One line when a colour ``var()`` names a variable nothing defines, with a fixed fallback.

    The Dashboard tab sets :data:`HOST_THEME_VARS` for the scheme in force; a variable
    neither the tab, the theme tokens nor the page's own CSS defines always draws its
    fallback, so ``var(--my-fg, #e8e8e8)`` is pale text on the light tab whatever the
    rest of the page does. Read over the CSS and the page's inline ``style`` attributes.
    """
    scope = f"{text}\n{html}"
    defined = set(HOST_THEME_VARS) | {str(k) for k in tokens} | set(_DEFINED_VAR.findall(text))
    names: dict[str, None] = {}  # ordered and constant-time to test
    for m in _VAR.finditer(scope):
        name, fallback = m.group(1), (m.group(2) or "").strip()
        if name in defined or name in names:
            continue
        if _FIXED_COLOUR.search(_resolve(fallback, tokens)):
            names[name] = None
    if not names:
        return []
    listed = list(names)
    shown = ", ".join(listed[:8]) + (f" and {len(listed) - 8} more" if len(listed) > 8 else "")
    return [UNDEFINED_VAR.format(names=shown, host=HOST_COLOUR_VARS)]


#: The frame's three role variables, and the generic families each one stands for.
#: A family is counted by its ROLE, so ``var(--font-mono)`` and ``ui-monospace`` are one.
_FONT_ROLES: Final[dict[str, str]] = {
    "var(--font-sans)": "sans",
    "system-ui": "sans",
    "sans-serif": "sans",
    "ui-sans-serif": "sans",
    "ui-rounded": "sans",
    "var(--font-serif)": "serif",
    "ui-serif": "serif",
    "serif": "serif",
    "var(--font-mono)": "mono",
    "ui-monospace": "mono",
    "monospace": "mono",
    "cursive": "cursive",
    "fantasy": "fantasy",
    "math": "math",
    "fangsong": "fangsong",
}
#: Words a ``font-family`` may hold that name no family of their own.
_FONT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"inherit", "initial", "unset", "revert", "revert-layer", "emoji", "caption", "icon",
     "menu", "message-box", "small-caption", "status-bar"}
)  # fmt: skip
#: ``font-family: ...`` (also inside ``@font-face``, where it names the face it loads,
#: an inline ``data:`` font included) and the ``font:`` shorthand, read to ``;`` or ``}``.
#: The value class has no overlap with what precedes it, so a scan stays linear.
_FONT_DECL = re.compile(r"(?<![-\w])(font-family|font)\s*:([^;}<>]*)")
#: In the shorthand, the families follow the size (``600 14px/1.3 var(--font-sans)``).
_SHORTHAND_SIZE = re.compile(r"(?:^|\s)[\d.]+(?:px|em|rem|pt|%|vw|vh|ch|ex)(?:\s*/\s*[\w.%]+)?\s+")


def _font_families(value: str, shorthand: bool) -> list[str]:
    """The families one declaration lists, in order, lowercased and unquoted."""
    if shorthand:
        m = None
        for m in _SHORTHAND_SIZE.finditer(value):
            pass
        if m is None:
            return []  # ``font: inherit`` and other forms that name no family
        value = value[m.end() :]
    names = []
    for part in value.split(","):
        name = part.strip().strip("\"'").strip().casefold()
        name = name.replace("!important", "").strip()
        if name and name not in _FONT_KEYWORDS:
            names.append(name)
    return names


def font_warnings(text: str, html: str, tokens: Mapping[str, Any]) -> list[str]:
    """A soft warning when a page sets more than two families, or names a typeface.

    Read over the theme CSS, the page's ``<style>`` and its inline ``style``
    attributes, and the theme tokens, so an ``@font-face`` that loads an inline
    ``data:`` font counts as the family it names. A declaration counts by its first
    family; the frame's role variables and the generic families count by their role
    (sans, serif, mono). Any family that is neither, anywhere in a list, is a named
    typeface. Never refuses the save.
    """
    token_text = " ".join(
        f"font-family: {v};" for k, v in tokens.items() if isinstance(v, str) and "font" in str(k)
    )
    scope = f"{text}\n{html}\n{token_text}"
    roles: list[str] = []
    named: list[str] = []
    for prop, value in _FONT_DECL.findall(scope):
        families = _font_families(value, prop == "font")
        if not families:
            continue
        first = families[0]
        role = _FONT_ROLES.get(first, first)
        if role not in roles:
            roles.append(role)
        for family in families:
            if family not in _FONT_ROLES and not family.startswith("var("):
                if family not in named:
                    named.append(family)
    out: list[str] = []
    if len(roles) > 2:
        shown = ", ".join(roles[:4]) + (" and more" if len(roles) > 4 else "")
        out.append(FONT_FAMILIES.format(n=len(roles), names=shown))
    if named:
        out.append(FONT_NAMED.format(names=", ".join(named[:3])))
    return out
