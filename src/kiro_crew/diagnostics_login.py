"""Replace the OS login in a diagnostics bundle member with ``<user>``.

The home collapse (``diagnostics._collapse_home_prefixes``) hides the login where it
is a segment of a home path. :func:`replace_logins` takes it where it stands as a
word of its own anywhere else: the user of a ``<login>@<host>`` target, a
``user=<login>`` field, a folder of another path, a line of serialized output. A
character that continues the segment on either side makes it part of another name,
so a sibling login such as ``jo.doe``, ``ann.jo`` or ``jo2``, or a host name such as
``dev-jo-box``, is left as written. :func:`count_logins` counts what that leaves
which still ends like the login, so the bundle can say so before it is posted.
"""

from __future__ import annotations

import re

from kiro_crew.diagnostics_host_name import (
    _ESCAPE_LETTERS,
    _ends_control_sequence,
    _follows_escaped_escape,
    _in_windows_path,
)


def replace_logins(text: str, homes: list[str]) -> tuple[str, int]:
    """Replace the login of each of *homes* in *text*; return (text, count).

    The login is the last segment of each home spelling. All of them go in one
    case-insensitive pass, longest first, so a ``<user>`` is never matched again.
    The segment ends where ``diagnostics._PREFIX_BOUNDARY`` says, as for the home
    collapse, and starts where :func:`_starts_word` says.
    """
    pattern = _login_pattern(homes)
    if pattern is None:
        return text, 0
    count = 0

    def replace(m: re.Match[str]) -> str:
        nonlocal count
        if not _starts_word(text, *m.span()):
            return m.group()
        count += 1
        return "<user>"

    return pattern.sub(replace, text), count


def count_logins(text: str, homes: list[str]) -> int:
    """How many logins of *homes* are still in *text* after :func:`replace_logins`.

    One that ends where the replacement's boundary says but does not start a word
    of its own, as in ``dev-<login>`` or ``ann.<login>``, was left by design and is
    counted here, so the bundle can warn about it instead of rewriting another name.
    The ``<user>`` the replacement writes is never counted, even for a login ``user``.
    """
    pattern = _login_pattern(homes)
    if pattern is None:
        return 0
    return len(pattern.findall(text.replace("<user>", "<>")))


def residual_warning(residuals: dict[str, int]) -> str:
    """The line that tells the user which bundle members still hold the login."""
    if not residuals:
        return ""
    return (
        f"{sum(residuals.values())} login occurrence(s) left in {len(residuals)} file(s)"
        f" ({', '.join(residuals)}): search the zip for your login before posting it."
    )


def _login_pattern(homes: list[str]) -> re.Pattern[str] | None:
    """The logins of *homes*, longest first, ending at the home collapse's boundary."""
    # circular import: diagnostics imports this module at its top.
    from kiro_crew.diagnostics import _PREFIX_BOUNDARY

    segments = (re.split(r"[\\/]+", h.rstrip("\\/"))[-1] for h in homes)
    logins = {login.lower(): login for login in segments}.values()
    if not logins:
        return None
    alternation = "|".join(re.escape(login) for login in sorted(logins, key=len, reverse=True))
    return re.compile("(?:" + alternation + ")" + _PREFIX_BOUNDARY, re.IGNORECASE)


def _starts_word(text: str, start: int, end: int) -> bool:
    """Whether the login at ``text[start:end]`` starts a word of its own.

    Not after a character that continues a segment (a word character, ``-``, or a
    word character and a ``.``), except where that character ends an ANSI colour
    code or a serialized escape such as ``\\n``, and always after a doubly serialized
    one (``\\\\n``, as in the host-name pass). Right after a ``\\`` it counts only
    as a Windows path folder, so the letter of an escape is not taken for it; the
    same test keeps a folder that an escape letter starts (the host-name pass's rule),
    judged by the run before that letter alone, as an escape can follow the login too.
    """
    if start == 0 or _follows_escaped_escape(text, start):
        return True
    before = text[start - 1]
    if before == "\\":
        return _in_windows_path(text, start, end)
    if re.match(r"[\w-]", before):
        if start >= 2 and text[start - 2] == "\\" and before.lower() in _ESCAPE_LETTERS:
            return not _in_windows_path(text, start - 1, start)
        return _ends_control_sequence(text, start)
    return not (before == "." and start >= 2 and re.match(r"\w", text[start - 2]) is not None)
