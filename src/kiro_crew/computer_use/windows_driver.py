"""Windows computer-use backend — a typed refusal until a driver exists.

Subclasses :class:`UnsupportedBackend`, so every tool answers with the same
clear "not supported on this platform" result instead of raising. That mirrors
how ``dashboard/handlers/terminal.py`` degrades the PTY on Windows: a missing
capability should be legible, not a traceback.

**Imports nothing native.** No ``ctypes.windll``, no ``comtypes`` — importing
this module on Linux, macOS or Windows loads only stdlib, so the package remains
import-safe everywhere and CI can exercise this path on any runner by flipping
``platform_compat.IS_WINDOWS``.

Implementation plan for whoever writes the real driver (all reachable from
``ctypes.windll`` with no new dependency, but each needs explicit ``argtypes`` —
a missing one truncates a 64-bit pointer to 32 bits and segfaults):

* **Tree** — ``UIAutomationCore.dll`` via ``CoUninitialize``/``CoCreateInstance``
  of ``CUIAutomation``, then ``IUIAutomation::ElementFromHandle`` on the target
  window and a ``TreeWalker`` for the walk. ``UIA_IsPasswordPropertyId``
  (30097) is the ``AXSecureTextField`` analogue and MUST drive
  ``ElementRec.secure`` — the same whole-window screenshot suppression and input
  refusal then apply unchanged.
* **Capture** — ``user32.PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)`` into a
  ``gdi32`` DIB section, encoded with WIC (``windowscodecs.dll``) so there is
  still no image dependency. Note ``PrintWindow`` fails on some
  hardware-composited windows; ``BitBlt`` is the documented fallback.
* **Input** — ``user32.SendInput`` with ``INPUT_KEYBOARD``. This is a
  *global* injection: unlike macOS's ``CGEventPostToPid`` there is no
  per-process posting on Windows, so the input verbs must either focus the
  target first (which steals the user's focus — a product decision, not an
  implementation one) or stay unimplemented. Resolve that explicitly before
  shipping input on Windows; do not quietly steal focus.
* **App list** — ``EnumWindows`` filtered by ``IsWindowVisible`` +
  ``GetWindowTextLength``, with ``GetWindowThreadProcessId`` for the pid. As on
  macOS, resolve the pid from the WINDOW, never from a process-name search.
"""

from __future__ import annotations

from kiro_crew.computer_use.backend import WINDOWS_REASON, UnsupportedBackend
from kiro_crew.computer_use.types import PLATFORM_WINDOWS


class WindowsBackend(UnsupportedBackend):
    """Windows placeholder backend: reports unsupported, refuses every action."""

    def __init__(self) -> None:
        super().__init__(PLATFORM_WINDOWS, WINDOWS_REASON)
