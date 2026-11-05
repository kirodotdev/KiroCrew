"""How a contributed label is measured, and the longest one a manifest may declare.

Every surface an app contributes a row to is drawn by the dashboard, which caps the
row's labels with constants it compares against JavaScript's ``.length``. The manifest
enforces the same caps at install time, so both sides must count the same units:
``_mirrored_len`` is that count and ``_MAX_TITLE`` the shared label cap. A leaf so
that every owner of a ``contributes`` row (``manifest_contributes``,
``manifest_session_kinds``) measures with the one function.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

#: Longest accepted row label, mirroring `MAX_TITLE` in `contributedCommands.ts`.
_MAX_TITLE = 120


def _mirrored_len(text: str) -> int:
    """Length in UTF-16 code units -- what JavaScript's ``.length`` counts.

    Every label cap a ``contributes`` row carries is mirrored by a constant in the
    dashboard reader compared against ``.length``, and the two languages do not agree
    on what a character is: Python counts CODE POINTS, JavaScript counts UTF-16 units,
    so anything outside the BMP counts once here and twice there. A 100-emoji title is
    100 to ``len()`` and 200 to the launcher, so a manifest that passed here would be
    dropped by the renderer -- the app installs clean and the row never appears. A cap
    that only one side enforces is not a cap; that holds for the UNIT as well as for
    the number, so the caps are measured the host's way.
    UNPAIRED surrogates are why this passes ``surrogatepass``: JSON can carry a lone
    ``\\ud800`` escape, ``json.loads`` accepts it, and a plain ``utf-16-le`` encode then
    raises ``UnicodeEncodeError`` -- so a manifest would CRASH validation instead of
    being told what is wrong with it. With the flag the count still matches the host
    exactly: a lone surrogate is one unit in both languages, an astral character two.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2
