"""``kirocrew open``: hand the desktop app a ``kirocrew://`` deep link.

The desktop app registers the ``kirocrew`` URL scheme (``website/electron/
deep-link.js`` owns the grammar and the allowlist; the Electron shell routes an
accepted URL to the dashboard's own ``/chat?sid=`` page). This command is the
CLI face of that scheme, so a script, a Raycast action or another local app can
say "open this session in Kiro Crew" without knowing the URL shape.

The slot-key grammar is duplicated here on purpose and pinned by a test against
the JavaScript module: the two must agree, and a Python caller cannot import the
Electron side. Nothing here makes a network call. Opening goes through
:func:`kiro_crew.platform_compat_trust.open_with_default_app` on macOS and Linux
(the same launcher the dashboard's "open file" uses) and ``os.startfile`` on
Windows, where that helper refuses; either way the OS dispatches to whatever
registered the scheme. ``--print`` skips the OS step.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

from kiro_crew.platform_compat_trust import open_with_default_app

SCHEME = "kirocrew"

#: Mirrors ``SLOT_KEY_RE`` in website/electron/deep-link.js. Checked with
#: ``fullmatch``: under ``re.match`` a trailing ``$`` also matches before a final
#: newline, which the JavaScript regex (no ``m`` flag) does not.
_SLOT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

#: How long ``open_url`` waits for ``open`` / ``xdg-open`` to report whether an
#: application took the URL.
_LAUNCHER_WAIT_SECS = 2.0


class DeepLinkError(ValueError):
    """A target the desktop app's resolver would refuse."""


def build_deep_link(*, session: str | None = None) -> str:
    """Return the ``kirocrew://`` URL for *session*, or the bare scheme for none.

    Raises :class:`DeepLinkError` for a key the desktop resolver would refuse,
    so the CLI fails here with a reason instead of launching the app onto a URL
    it then drops.
    """
    if not session:
        return f"{SCHEME}://"
    if not _SLOT_KEY_RE.fullmatch(session):
        raise DeepLinkError(
            "session key may contain letters, digits, '.', '_', ':' and '-', "
            "must start with a letter or digit, and be at most 200 characters"
        )
    return f"{SCHEME}://chat?sid={quote(session, safe='')}"


def open_url(url: str, platform: str = sys.platform) -> tuple[bool, str]:
    """Hand *url* to the OS. Returns ``(ok, detail)``; never raises.

    On macOS and Linux the shared launcher starts ``/usr/bin/open`` or
    ``/usr/bin/xdg-open`` with ``PATH`` pinned to trusted directories and waits
    up to :data:`_LAUNCHER_WAIT_SECS` for it to exit. A non-zero exit means no
    application took the scheme, which is the case this command must report. A
    launcher still running at the deadline counts as opened, so an ``xdg-open``
    that runs the app in the foreground cannot hold the command open. Windows
    uses ``os.startfile``, because the shared launcher refuses Windows by design.
    """
    if platform.startswith("win"):
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # Windows only
            return True, "os.startfile"
        except OSError as exc:  # no handler registered, or a refused launch
            return False, str(exc)
    if open_with_default_app(url, wait_for_exit=_LAUNCHER_WAIT_SECS):
        return True, "open_with_default_app"
    return False, f"no application is registered for {SCHEME}://"


def cmd_open(args) -> int:  # type: ignore[no-untyped-def]  # argparse.Namespace
    """Entry point for ``kirocrew open``."""
    try:
        url = build_deep_link(session=getattr(args, "session", None))
    except DeepLinkError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    if getattr(args, "print_url", False):
        print(url)
        return 0
    ok, detail = open_url(url)
    if ok:
        return 0
    print(
        f"❌ Could not open {url} ({detail}). The kirocrew:// scheme is registered by the "
        "Kiro Crew desktop app's installer; with no desktop app installed, open the dashboard "
        "in a browser instead (`kirocrew token`).",
        file=sys.stderr,
    )
    return 1
