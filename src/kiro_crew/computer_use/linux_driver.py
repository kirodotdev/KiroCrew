"""Linux computer-use backend — a typed refusal until a driver exists.

Subclasses :class:`UnsupportedBackend`, so every tool answers with the same clear
"not supported on this platform" result instead of raising.

**Imports nothing native.** No D-Bus, no GI bindings — importing this module is
stdlib-only on every platform, which keeps the package import-safe on the CI
fleet and lets a test exercise this path by flipping
``platform_compat.IS_LINUX``.

Implementation plan for whoever writes the real driver:

* **Tree** — AT-SPI 2 over D-Bus (``org.a11y.atspi.Accessible``), reached from
  the session bus address in ``org.a11y.Bus``. Requires the toolkit-side
  accessibility bridge to be enabled (``GTK_MODULES=gail:atk-bridge`` /
  ``QT_ACCESSIBILITY=1``), which is NOT the default on every desktop — so the
  driver must detect a missing bridge and report it as the reason rather than
  returning an empty tree that looks like a working app with no controls.
  ``ATSPI_STATE_PROTECTED`` (Qt/GTK password entries) is the
  ``AXSecureTextField`` analogue and MUST drive ``ElementRec.secure``.
* **Capture is the hard part, and it is a genuine design fork.** On X11,
  ``XGetImage`` over the window works directly. On **Wayland there is no
  client-side screen capture at all**: a compositor screenshot requires
  ``xdg-desktop-portal``'s ``org.freedesktop.portal.Screenshot``, which shows an
  interactive consent dialog — unusable from a background sidecar. So a first
  Linux cut is very likely **tree-only**. That is already accommodated: the
  contract lets ``snapshot()`` return a tree with no image, ``want_image`` is a
  request rather than a requirement, and the renderer simply omits the
  screenshot line.
* **Input** — ``XTestFakeKeyEvent`` on X11 (global, not per-window). Wayland has
  no equivalent for an unprivileged client; ``libei``/``xdg-desktop-portal``
  ``RemoteDesktop`` is the forward path and again requires consent. Decide the
  focus-stealing question explicitly before shipping input, exactly as on
  Windows — do not quietly steal focus.
* **App list** — enumerate AT-SPI applications and their frames; take the pid
  from the application object, never from a process-name search.
"""

from __future__ import annotations

from kiro_crew.computer_use.backend import LINUX_REASON, UnsupportedBackend
from kiro_crew.computer_use.types import PLATFORM_LINUX


class LinuxBackend(UnsupportedBackend):
    """Linux placeholder backend: reports unsupported, refuses every action."""

    def __init__(self) -> None:
        super().__init__(PLATFORM_LINUX, LINUX_REASON)
