"""The computer-use backend seam: one ABC, one registry, one platform branch.

``ComputerUseBackend`` is what varies by OS; everything above it (policy,
rendering, the index lifecycle, the MCP dispatch) is platform-free. The seam is a
plain ``abc.ABC`` with a runtime-swappable factory — the shape
``embeddings.register_embedding_backend`` already uses — rather than a
``PlatformContext`` extension point, because CPP is the *edition* seam
(standalone vs companion) while computer use varies by *platform*, and because a
registry stays swappable inside a single pytest process.

Two structural guarantees:

* **One platform branch.** :func:`select_default_backend` is the only place in
  the whole package that asks which OS this is, and it asks
  ``platform_compat.IS_MACOS`` / ``IS_WINDOWS`` / ``IS_LINUX`` rather than
  reading ``sys.platform`` itself.
* **No exception crosses the seam.** Every method returns a
  :class:`DriverResult`; drivers convert their own failures into
  ``ok=False``. A ``ComputerUseError`` escaping into the MCP stdio loop's worker
  thread would take the tool call down, and an unhandled ctypes fault would take
  the sidecar with it.

Nothing in this module imports ctypes, and the native drivers are imported
lazily inside :func:`select_default_backend` — so importing this package on a
Linux CI runner loads no native library at all.
"""

from __future__ import annotations

import abc
import logging
import threading
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.computer_use import index
from kiro_crew.computer_use.types import (
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_UNSUPPORTED,
    PLATFORM_WINDOWS,
    REFUSAL_UNSUPPORTED,
    AppRef,
    BackendStatus,
    ClickRequest,
    DragRequest,
    DriverResult,
    ElementRec,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

# Reasons the non-macOS backends report. Concrete rather than "not supported":
# a user on Windows should learn what is missing, and a maintainer should find
# the next implementation step named here.
WINDOWS_REASON = (
    "the Windows UI Automation driver is not implemented yet; computer use is "
    "macOS-only in this release"
)
LINUX_REASON = (
    "the Linux AT-SPI driver is not implemented yet; computer use is macOS-only "
    "in this release (Wayland has no unprivileged window capture)"
)
UNKNOWN_PLATFORM_REASON = "this operating system has no computer-use driver"
DRIVER_IMPORT_REASON = "the native computer-use driver could not be loaded ({detail})"


class ComputerUseBackend(abc.ABC):
    """Abstract desktop-automation driver for one platform.

    Contract every implementation must honor:

    * **Never raise.** Convert every failure — a missing app, an accessibility
      error, a permission denial — into ``DriverResult(ok=False, text=<reason>)``
      with the reason WITHOUT an ``Error: `` prefix (the dispatch layer adds it
      exactly once).
    * **Never move the pointer unless the request says to.** Every method is
      app-scoped by default: an element click is an accessibility action and a
      coordinate click/drag posts to the target process, so the operator's cursor
      does not move. The ONE exception is a :class:`ClickRequest` /
      :class:`DragRequest` whose ``moves_pointer`` is True (the ``global``
      method), which the model must have NAMED explicitly — ``auto`` never resolves
      to it. A driver MUST NOT warp the cursor for any other method, and MUST NOT
      silently upgrade a refused app-scoped click into a pointer-moving one.
    * **Set ``ElementRec.secure`` from BOTH ``AXRole`` and ``AXSubrole``** (or
      the platform equivalent). A password box reports an innocuous role with a
      secure *subrole* and a readable value; a role-only check misses every one.
      Every downstream protection — value redaction, input refusal, screenshot
      suppression — keys off this flag, so a driver that gets it wrong defeats
      all three at once.
    * **Set ``Snapshot.captured_at`` from ``time.monotonic()``**, never wall
      clock, so the TTL cannot be defeated by a clock adjustment.
    * **Be thread-safe.** The MCP loop dispatches on a worker thread while the
      main thread reads stdin.
    """

    @property
    @abc.abstractmethod
    def platform_id(self) -> str:
        """Stable platform identifier (``macos``/``windows``/``linux``/``fake``)."""

    @abc.abstractmethod
    def status(self) -> BackendStatus:
        """Whether this backend can drive computer use here, and why not if it can't."""

    @abc.abstractmethod
    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY permission hints for the Settings UI — never a gate.

        macOS attributes a TCC grant to the responsible parent of the process
        tree, so a probe can report ``missing`` while a full-fidelity capture
        succeeds. Callers must not refuse an action based on this.
        """

    @abc.abstractmethod
    def list_apps(self) -> DriverResult:
        """Applications with an on-screen window, resolved from the window list."""

    @abc.abstractmethod
    def resolve_app(self, query: str) -> DriverResult:
        """Resolve *query* (name or bundle id) to one :class:`AppRef`.

        MUST resolve from the on-screen window list, never from a process-name
        search: a ``pgrep``-style match returns short-lived helper processes
        whose accessibility trees are empty.
        """

    @abc.abstractmethod
    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        """Walk *app*'s focused window into a :class:`Snapshot`.

        Honors every budget in *req*. When ``req.want_image`` is set the
        snapshot MAY carry encoded JPEG bytes; a driver that cannot capture
        returns the tree alone rather than failing the call.
        """

    @abc.abstractmethod
    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        """Click *rec* (accessibility press) or *req*'s point (mouse event).

        *req* arrives with a CONCRETE method — ``auto`` is resolved at the dispatch
        chokepoint, so a driver never re-decides it and cannot accidentally pick the
        pointer-warping path. ``rec`` is ``None`` for a coordinate click, and
        ``req.point`` is ``None`` for the element form; exactly one is set (validated
        upstream by ``policy.check_click_target``).

        Honour ``req.button`` and ``req.count`` for a mouse-event click, and warp the
        pointer ONLY when ``req.moves_pointer`` is True.
        """

    @abc.abstractmethod
    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        """Drag from *req*'s start point to its end point inside *app*.

        Coordinate-only by construction: a drag's meaning IS the path between two
        points, and no accessibility action expresses it. App-scoped unless
        ``req.moves_pointer`` is True, in which case the two upstream permits have
        already been checked.
        """

    @abc.abstractmethod
    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        """Type *text* into *rec*, or into the focused element when *rec* is None."""

    @abc.abstractmethod
    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        """Send one key spec (``"cmd+shift+a"``) to *app*."""

    @abc.abstractmethod
    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        """Set *rec*'s value directly (no keystrokes)."""

    @abc.abstractmethod
    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        """Scroll *rec* by *pages* in *direction*."""

    @abc.abstractmethod
    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        """Perform a named accessibility action on *rec*."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources. Safe to call repeatedly."""


class UnsupportedBackend(ComputerUseBackend):
    """Shared base for platforms with no driver: every method refuses identically.

    Concrete on purpose — a subclass supplies only ``platform_id`` and a reason,
    so a new unsupported platform is ~10 lines and CANNOT accidentally implement
    half a driver. Nothing here raises: the model gets a coherent refusal naming
    the platform instead of a broken capability, which is the same posture
    ``dashboard/handlers/terminal.py`` takes for the Windows PTY.
    """

    def __init__(self, platform_id: str, reason: str) -> None:
        self._platform_id = platform_id
        self._reason = reason

    @property
    def platform_id(self) -> str:
        return self._platform_id

    @property
    def reason(self) -> str:
        return self._reason

    def status(self) -> BackendStatus:
        return BackendStatus(supported=False, platform_id=self._platform_id, reason=self._reason)

    def probe_permissions(self) -> PermissionProbe:
        return PermissionProbe(
            accessibility=PERMISSION_UNSUPPORTED,
            screen_recording=PERMISSION_UNSUPPORTED,
            responsible_hint="",
        )

    def _refuse(self) -> DriverResult:
        """The one refusal every method returns."""
        return DriverResult(
            ok=False,
            text=REFUSAL_UNSUPPORTED.format(platform=self._platform_id, reason=self._reason),
        )

    def list_apps(self) -> DriverResult:
        return self._refuse()

    def resolve_app(self, query: str) -> DriverResult:
        return self._refuse()

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        return self._refuse()

    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        return self._refuse()

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        return self._refuse()

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        return self._refuse()

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        return self._refuse()

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        return self._refuse()

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        return self._refuse()

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        return self._refuse()

    def close(self) -> None:
        """Nothing to release."""


def unsupported_snapshot(app: AppRef) -> Snapshot:
    """An empty snapshot for *app* — the shape an unsupported platform reports.

    Kept beside the refusal so a caller that needs a ``Snapshot`` object (rather
    than a ``DriverResult``) never has to hand-build one and accidentally leave
    ``has_secure`` unset.
    """
    return Snapshot(app=app, elements=(), captured_at=0.0)


# ── Registry: one process-wide backend, swappable at runtime ──

_shared_backend: ComputerUseBackend | None = None
_shared_backend_lock = threading.Lock()
_backend_factory: "Callable[[], ComputerUseBackend] | None" = None


def register_computer_use_backend(factory: "Callable[[], ComputerUseBackend] | None") -> None:
    """Override the backend (the swap seam for tests and future platforms).

    Pass a factory returning a :class:`ComputerUseBackend`; the next
    :func:`get_shared_backend` constructs through it. Pass ``None`` to restore
    the platform default. Call :func:`reset_shared_backend` afterwards so an
    already-built singleton — and the snapshot cache it filled — is replaced.

    The suite registers ``FakeComputerUseBackend`` process-wide so CI never
    loads a native framework, never captures a real window, and never touches a
    real application.
    """
    global _backend_factory
    with _shared_backend_lock:
        _backend_factory = factory


def get_shared_backend() -> ComputerUseBackend:
    """Process-wide backend singleton.

    One instance per process: a driver holds cached framework handles and an
    event source, and building a second would double both for no benefit.
    """
    global _shared_backend
    with _shared_backend_lock:
        if _shared_backend is None:
            _shared_backend = (_backend_factory or select_default_backend)()
        return _shared_backend


def reset_shared_backend() -> None:
    """Drop the singleton AND the snapshot cache.

    Dropping the cache is not housekeeping — it is required for correctness.
    Element indices are only meaningful against the walk that produced them, so
    a new backend inheriting the previous one's snapshots could resolve an index
    to a completely different element.
    """
    global _shared_backend
    with _shared_backend_lock:
        if _shared_backend is not None:
            try:
                _shared_backend.close()
            except Exception:
                # A driver that fails to release its handles must not prevent the
                # swap: leaving the old instance installed would be worse than
                # leaking whatever it held.
                logger.debug("computer-use backend close() failed", exc_info=True)
        _shared_backend = None
    index.reset_shared_index()


def select_default_backend() -> ComputerUseBackend:
    """Build the backend for THIS platform. The only platform branch in the package.

    Branches on ``platform_compat.IS_MACOS`` / ``IS_WINDOWS`` / ``IS_LINUX``
    (never a raw ``sys.platform`` read), which is also what lets a test flip a
    single flag and exercise the Windows/Linux degradation path on a Linux
    runner.

    The native driver import is deferred into this function for two reasons: it
    would be a circular import at module scope (the drivers subclass the classes
    defined above), and a module-scope import would load ApplicationServices on
    every machine that merely imports the package — including the Linux CI fleet,
    where it would break collection of every test that transitively touches
    ``kiro_crew``.

    A driver module that fails to import degrades to a typed refusal rather than
    propagating: a partial install or a framework that will not load should
    disable one capability, not crash the process that asked about it.
    """
    if platform_compat.IS_MACOS:
        try:
            # Deferred + circular: macos_driver subclasses ComputerUseBackend.
            from kiro_crew.computer_use.macos_driver import MacOSBackend

            return MacOSBackend()
        except Exception as exc:
            logger.warning("macOS computer-use driver unavailable: %s", exc)
            return UnsupportedBackend(PLATFORM_MACOS, DRIVER_IMPORT_REASON.format(detail=exc))
    if platform_compat.IS_WINDOWS:
        # Deferred + circular: windows_driver subclasses UnsupportedBackend.
        from kiro_crew.computer_use.windows_driver import WindowsBackend

        return WindowsBackend()
    if platform_compat.IS_LINUX:
        # Deferred + circular: linux_driver subclasses UnsupportedBackend.
        from kiro_crew.computer_use.linux_driver import LinuxBackend

        return LinuxBackend()
    return UnsupportedBackend(PLATFORM_UNSUPPORTED, UNKNOWN_PLATFORM_REASON)


def platform_id_for_current_os() -> str:
    """The platform id this OS would select, WITHOUT building a backend.

    For the dashboard payload and diagnostics: it answers "which platform is
    this" without loading a native framework, which is why the Settings row can
    render on any OS without a driver import.
    """
    if platform_compat.IS_MACOS:
        return PLATFORM_MACOS
    if platform_compat.IS_WINDOWS:
        return PLATFORM_WINDOWS
    if platform_compat.IS_LINUX:
        return PLATFORM_LINUX
    return PLATFORM_UNSUPPORTED
