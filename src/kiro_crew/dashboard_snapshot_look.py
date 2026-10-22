"""The look a dashboard snapshot is drawn in: the tab's default light theme and Tailwind.

The Dashboard tab does not show a crewmate's page as the gateway composes it. It wraps
the page in its own sandbox document (``website/src/lib/widgetSrcdoc.ts``
``buildSrcdoc``), which adds the person's live theme tokens (``--bg``, ``--card`` and the
rest of ``THEME_VAR_NAMES``) and the same-origin Tailwind v4 runtime. A page that uses
``var(--card)`` or utility classes therefore looks different without them.

A snapshot has no browser to read the person's theme from, so it is drawn in the
dashboard's DEFAULT LIGHT theme: :data:`LIGHT_TOKENS`, copied from the
``[data-theme="light"]`` block of ``website/src/styles/design-tokens.css`` and pinned to
it by a test that fails on drift. A person on another theme pack or in dark mode sees
other colours, and the snapshot tool says so.

The Tailwind runtime and its ``</script`` escape are :mod:`kiro_crew.publish_sync`'s,
which inlines the same file for a published widget: one reader, one escape. The body's
padding and font are the frame's own host stylesheet's, as on the tab. The runtime is read from the built package at the moment of the
snapshot and inlined, because the offline run fetches nothing. A source checkout that
has never built the website has no such file, and :func:`look_prefix` says so instead
of drawing a page without it.
"""

from __future__ import annotations

from typing import Final

#: The dashboard's default light theme, for every name the tab hands its frames
#: (``THEME_VAR_NAMES``). Pinned to ``design-tokens.css`` by
#: ``test_dashboard_snapshot.py``.
LIGHT_TOKENS: Final[dict[str, str]] = {
    "--bg": "#fafafa",
    "--bg-elevated": "#ffffff",
    "--bg-hover": "#f0f0f0",
    "--card": "#ffffff",
    "--card-fg": "#18181b",
    "--text": "#3f3f46",
    "--text-strong": "#18181b",
    "--muted": "#71717a",
    "--muted-strong": "#52525b",
    "--border": "#e4e4e7",
    "--border-strong": "#d4d4d8",
    "--accent": "#047558",
    "--accent-hover": "#059669",
    "--accent-subtle": "rgba(4,117,88,.12)",
    "--ok": "#16a34a",
    "--ok-subtle": "rgba(22,163,74,.1)",
    "--warn": "#a16207",
    "--warn-subtle": "rgba(161,98,7,.1)",
    "--danger": "#dc2626",
    "--danger-subtle": "rgba(220,38,38,.1)",
    "--info": "#0891b2",
}

#: The tab's aliases for names some pages use (``THEME_VAR_ALIASES``).
ALIASES: Final[dict[str, str]] = {
    "--surface": "--card",
    "--surface-secondary": "--bg-elevated",
}

#: The tab's Tailwind v4 directive block (``TAILWIND_V4_DIRECTIVES``).
TAILWIND_DIRECTIVES: Final[str] = "@custom-variant dark (&:where(.dark, .dark *));"

NO_RUNTIME: Final[str] = (
    "this installation has no built Tailwind runtime (static/dist/vendor/"
    "tailwindcss-browser.js), so the page cannot be drawn the way the Dashboard tab draws it"
)


def theme_css() -> str:
    """The tab's theme stylesheet (``buildThemeCss``) over :data:`LIGHT_TOKENS`.

    Without the tab's base body style: the tab puts that ahead of the frame's own
    host stylesheet, which then sets the body's padding and font, and this prefix sits
    after it, so carrying it here would override the frame instead."""
    declarations = [f"{name}:{value}" for name, value in LIGHT_TOKENS.items()]
    declarations += [f"{alias}:var({target})" for alias, target in ALIASES.items()]
    return (
        f":root{{{';'.join(declarations)};color-scheme:light}}"
        "html{background:var(--bg)}body{background:var(--bg);color:var(--text)}"
    )


def look_prefix() -> tuple[str, str]:
    """``(markup, "")`` to put in front of the page, or ``("", reason)`` when there is
    no built runtime to draw it with."""
    from kiro_crew.publish_sync import _SCRIPT_CLOSE_RE, _tailwind_runtime_js

    runtime = _tailwind_runtime_js()
    if not runtime:
        return "", NO_RUNTIME
    escaped = _SCRIPT_CLOSE_RE.sub(lambda m: "<\\/" + m.group(1), runtime)
    return (
        f'<style type="text/tailwindcss">{TAILWIND_DIRECTIVES}</style>\n'
        f"<style>{theme_css()}</style>\n"
        f"<script>{escaped}</script>\n",
        "",
    )
