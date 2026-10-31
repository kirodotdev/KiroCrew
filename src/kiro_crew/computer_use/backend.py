"""The computer-use backend seam: one ABC, one registry, one platform branch.

``ComputerUseBackend`` is what varies by OS; everything above it (policy,
rendering, the index lifecycle, the MCP dispatch) is platform-free. The seam is a
plain ``abc.ABC`` with a runtime-swappable factory — the shape
``embeddings.register_embedding_backend`` already uses — rather than a
``PlatformContext`` extension point, because CPP is the *edition* seam
(standalone vs companion) while computer use varies by *platform*, and because a
registry stays swappable inside a single pytest process.

Two structural guarantees:

* **One platform branch.** The selection functions here are the only place in
  the whole package that asks which OS this is, and they ask
  ``platform_compat.IS_MACOS`` / ``IS_WINDOWS`` / ``IS_LINUX`` rather than
  reading ``sys.platform`` itself. The one driver is agent-desktop
  (:mod:`agent_desktop_driver`); a :class:`NativeHelper` sits under it on macOS
  and Windows.
* **No exception crosses the seam.** Every method returns a
  :class:`DriverResult`; drivers convert their own failures into
  ``ok=False``. A ``ComputerUseError`` escaping into the MCP stdio loop's worker
  thread would take the tool call down, and an unhandled ctypes fault would take
  the sidecar with it.

Nothing in this module imports ctypes, and the native helpers are imported
lazily inside :func:`select_native_helper` — so importing this package on a
Linux CI runner loads no native library at all.
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.computer_use import index
from kiro_crew.computer_use.types import (
    ERR_LAUNCH_ALREADY_RUNNING,
    ERR_LAUNCH_AMBIGUOUS,
    ERR_LAUNCH_FAILED,
    ERR_LAUNCH_NOT_INSTALLED,
    ERR_LAUNCH_NOT_INSTALLED_NEAR,
    LAUNCH_POLL_INTERVAL_SECS,
    LAUNCH_RESULT,
    LAUNCH_RESULT_NO_WINDOW,
    LAUNCH_WINDOW_TIMEOUT_SECS,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_UNSUPPORTED,
    PLATFORM_WINDOWS,
    REFUSAL_UNSUPPORTED,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    AmbiguousLaunchTarget,
    AppRef,
    BackendStatus,
    ClickRequest,
    DragRequest,
    DriverResult,
    ElementRec,
    LaunchIdentity,
    NoSuchLaunchTarget,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

UNKNOWN_PLATFORM_REASON = "this operating system has no computer-use driver"
DRIVER_IMPORT_REASON = "the computer-use driver could not be loaded ({detail})"


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
    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """Start the installed application *query* names, so it HAS a window.

        *permit* is the caller's policy predicate, and an implementation MUST apply it
        to the RESOLVED :class:`LaunchIdentity` before spawning — with every identifier
        the resolution produced, not only the display name. The caller cannot do this
        itself: it has only the raw query, which a prefix resolves away from, and which
        carries none of the spellings (executable key, bundle id) an operator's deny
        rule is written against.

        The one method here that creates a process rather than observing or driving
        one, which puts three requirements on every implementation:

        * **Resolve through an OS-owned catalog, and refuse a path.** *query* is a
          NAME, never a filesystem path, argument vector or URL. Accepting any of
          those would make this "run an arbitrary program with attacker-chosen
          input" rather than "open an application" — and because computer use is
          deliberately not governance-gated, that program would also bypass the
          ``BUILTIN_DENIED_RULES`` floor every ``bash`` call passes.
        * **Verify what the catalog returned.** A catalog can be agent-writable (on
          Windows, ``HKCU``'s ``App Paths`` and the per-user Start Menu both are), so
          the implementation must bound the RESOLVED executable rather than trusting
          the lookup. See :mod:`launch_windows` for the measurements and the
          protected-root check they produced.
        * **Confirm by finding the WINDOW, not by the launcher's exit status.** A
          packaged-app launcher exits immediately and hands off; a launch that
          reported the launcher's own result would call a successful start a failure.

        Returns ``ok=True`` with the resolved :class:`AppRef` once a window exists,
        and ``ok=True`` WITHOUT one when the process started but showed nothing
        within the timeout — the process really did start, so reporting failure would
        invite the model to launch a second copy.
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


class NativeHelper(abc.ABC):
    """The platform code the agent-desktop driver calls for what agent-desktop cannot do.

    agent-desktop owns the accessibility tree and every element action. This is the
    rest, on macOS and Windows only: the window inventory and the app identity rules,
    a vetted launch, coordinate input (``app_post``/``sky_click`` leave the pointer
    alone on macOS; ``global`` moves it on both), and keystrokes sent to whatever
    holds keyboard focus. The caller moves and checks focus first, so nothing here
    takes an element.

    Same contract as :class:`ComputerUseBackend`: never raise, and never move the
    pointer unless ``req.moves_pointer`` is True.
    """

    @property
    @abc.abstractmethod
    def platform_id(self) -> str:
        """``macos`` or ``windows``."""

    @abc.abstractmethod
    def status(self) -> BackendStatus:
        """Whether the native libraries load here."""

    @abc.abstractmethod
    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY permission hints (see :meth:`ComputerUseBackend.probe_permissions`)."""

    @abc.abstractmethod
    def list_apps(self) -> DriverResult:
        """Applications with an on-screen window."""

    @abc.abstractmethod
    def resolve_app(self, query: str) -> DriverResult:
        """Resolve *query* to one :class:`AppRef` from the on-screen window list."""

    @abc.abstractmethod
    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """See :meth:`ComputerUseBackend.launch_app`."""

    @abc.abstractmethod
    def click(self, app: AppRef, req: ClickRequest) -> DriverResult:
        """Click *req*'s point with its concrete coordinate method."""

    @abc.abstractmethod
    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        """Drag between *req*'s points inside *app*."""

    @abc.abstractmethod
    def type_text(self, app: AppRef, text: str) -> DriverResult:
        """Type *text* into whatever holds *app*'s keyboard focus."""

    @abc.abstractmethod
    def press_key(self, app: AppRef, key: str) -> DriverResult:
        """Send one key spec to whatever holds *app*'s keyboard focus."""

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

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
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


def await_launched_window(
    find: "Callable[[], AppRef | None]",
    *,
    timeout: float = LAUNCH_WINDOW_TIMEOUT_SECS,
    interval: float = LAUNCH_POLL_INTERVAL_SECS,
) -> "tuple[AppRef | None, float]":
    """Poll *find* until it reports a window. Returns ``(app_or_None, secs_waited)``.

    Lives at the seam rather than in either ``apps_*`` module because it is the
    ``launch_app`` contract's third requirement — *confirm by finding the window, not
    by the launcher's exit status* — and that requirement is identical on both
    platforms while the finder is not. Written twice it would drift; a driver would
    end up with a different timeout from the one the contract documents.

    Why polling at all, rather than reading the spawned process's result: a launcher
    exits before the application it started has a window. Measured on Windows,
    ``explorer.exe`` returned ``rc=1`` after 2.9s while MS Paint's window appeared at
    9.9s; ``/usr/bin/open`` on macOS returns as soon as LaunchServices accepts the
    request. Either exit status read as the outcome would report a successful cold
    start as a failure.

    ``time.monotonic``, never wall clock, so a clock adjustment during a slow cold
    start can neither end the wait early nor extend it indefinitely.

    The elapsed time is RETURNED rather than logged because it belongs in the result
    the model reads: a 10s launch and a 0.3s one are the same outcome but very
    different information about what the machine is doing.
    """
    started = time.monotonic()
    deadline = started + timeout
    while True:
        found = find()
        if found is not None:
            return found, time.monotonic() - started
        if time.monotonic() >= deadline:
            return None, time.monotonic() - started
        time.sleep(interval)


def run_launch(
    query: str,
    *,
    resolve: "Callable[[str], tuple[str, str]]",
    find: "Callable[[str], AppRef | None]",
    spawn: "Callable[[str], None]",
    permit: "Callable[[LaunchIdentity], str | None] | None" = None,
    identity: "Callable[[str, str], LaunchIdentity] | None" = None,
    refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    window_timeout: float = LAUNCH_WINDOW_TIMEOUT_SECS,
    window_poll_interval: float = LAUNCH_POLL_INTERVAL_SECS,
) -> DriverResult:
    """The whole ``launch_app`` flow, once, for every platform to reuse.

    The flow — resolve, refuse, check already-running, spawn, await the window — is
    identical on both platforms; only the three injected functions differ (which catalog
    resolves a name, which enumeration finds a window, which launcher starts a process).
    Written per driver it was ~45 duplicated lines, and the drift that invites is not
    hypothetical: each of the four refusals below encodes a decision (near-miss
    suggestions, already-running rather than a second copy, no-window as a SUCCESS) that
    has to hold on both platforms or the model learns a different contract depending on
    the OS it is running under. That is the same reason
    :func:`await_launched_window` lives here rather than in either ``apps_*`` module.

    *resolve* raises :class:`NoSuchLaunchTarget` or :class:`AmbiguousLaunchTarget`; both
    are caught here so the refusal prose is composed in exactly one place. Every other
    exception propagates to the driver's own ``_guarded`` seam, which is what keeps the
    "no exception crosses the seam" contract a driver-level guarantee rather than
    something this function has to re-implement.

    *identity* names the resolved target for *permit*, given the resolved
    ``(target, display_name)``. A platform supplies it to add the identifiers the OS
    will report once the process exists — the executable key, the bundle id — because
    those are what an operator's deny rule is written against and the display name
    alone matches neither. Omitting it checks the display name only, which is what a
    platform with no second spelling should do.

    *refuse_launched* is the same policy applied to the ``AppRef`` the window list reports
    once a window exists, and it is a SEPARATE parameter from *permit* because it takes a
    different argument: a real ``AppRef``, whose ``window_title`` is the only place a
    packaged app's name appears. See the comment at the call site for why that cannot be
    folded into the pre-spawn check.

    *window_timeout* and *window_poll_interval* bound the post-spawn window wait; they
    default to the production values and exist so tests can shrink the no-window wait.
    """
    try:
        target, name = resolve(query)
    except AmbiguousLaunchTarget as exc:
        return DriverResult(
            ok=False,
            text=ERR_LAUNCH_AMBIGUOUS.format(query=query, count=exc.count, names=exc.names),
        )
    except NoSuchLaunchTarget as exc:
        # Near misses turn a dead end into a recoverable refusal: resolution is
        # prefix-only, so a model that typed a fragment of a real name needs to be told
        # the real one rather than left to invent a path — the one retry that can never
        # be served.
        if exc.near:
            return DriverResult(
                ok=False, text=ERR_LAUNCH_NOT_INSTALLED_NEAR.format(query=query, near=exc.near)
            )
        return DriverResult(
            ok=False, text=ERR_LAUNCH_NOT_INSTALLED.format(query=query, tool=TOOL_LIST_APPS)
        )

    # THE POLICY RE-CHECK, on EVERY resolved identity and before any process exists.
    #
    # The dispatcher already checked the caller's raw string, and that is not enough for
    # two independent reasons — both measured, both ending with a denied application
    # running:
    #
    # * a prefix resolves, so an operator who denied ``notepad`` is bypassed by a request
    #   for ``note``: the raw-name check sees a string no rule matches, and the resolver
    #   then returns the denied application;
    # * the identifiers differ. A launch is requested by DISPLAY name (``notepad``) while
    #   every other computer-use refusal names the OS identity (``notepad.exe``), so that
    #   is the spelling an operator's ``extra_denied_apps`` entry naturally carries — and
    #   a check that knows only the display name matches neither it nor a bundle-id rule.
    #
    # Re-checking here rather than tightening the resolver is deliberate: prefix matching
    # is what makes the verb usable ("paint" for "mspaint"), and the defect is not that
    # a prefix resolves — it is that the policy was evaluated against a different string
    # from the one that got launched. So the check moves to where the resolved identity is
    # known, and is applied to ALL of its spellings, which is also the last point before
    # ``spawn``. A detached spawn cannot be undone, so this must precede it.
    if permit is not None:
        who = identity(target, name) if identity is not None else LaunchIdentity(display=name)
        refusal = permit(who)
        if refusal:
            return DriverResult(ok=False, text=refusal)

    existing = find(name)
    if existing is not None:
        # Refused rather than re-launched: a second copy of an editor is a second
        # unsaved document, and the model's actual goal — a window to drive — is
        # already met, so the useful reply names the window and points at the reader.
        return DriverResult(
            ok=False,
            text=ERR_LAUNCH_ALREADY_RUNNING.format(
                app=name, title=existing.window_title, tool=TOOL_GET_STATE
            ),
        )

    try:
        spawn(target)
    except OSError as exc:
        return DriverResult(
            ok=False, text=ERR_LAUNCH_FAILED.format(app=name, detail=exc.strerror or exc)
        )

    appeared, waited = await_launched_window(
        lambda: find(name), timeout=window_timeout, interval=window_poll_interval
    )
    if appeared is None:
        # A SUCCESS with a caveat, not a failure. The process did start; reporting
        # failure is what makes a model launch again, and the second attempt is what
        # produces two copies of the application.
        return DriverResult(
            ok=True, text=LAUNCH_RESULT_NO_WINDOW.format(app=name, secs=waited, tool=TOOL_LIST_APPS)
        )
    # The THIRD check, on the identity the OS actually published, and the one that covers
    # what the pre-spawn check structurally cannot: the WINDOW TITLE.
    #
    # A packaged app is named by its title and by nothing the catalog knows. Sometimes the
    # title IS the identity — ``apps_windows`` publishes it as both name and bundle id for a
    # window fronted by ``ApplicationFrameHost``, because the broker's image name identifies
    # no application — and sometimes it is only ``window_title`` (measured: Snipping Tool
    # reports its own ``SnippingTool.exe`` and carries "Snipping Tool" as the title alone).
    # Either way the title does not exist until a window does: before the spawn the catalog
    # offers ``SnippingTool.exe`` and nothing else, so an operator's ``Snipping Tool`` rule
    # matches no pre-spawn identity.
    #
    # ``appeared`` is passed WHOLE rather than rebuilt from two of its fields, so the title
    # rule — which is a substring test on ``window_title``, and the reason
    # ``policy.denied_rule_for`` reads that field at all — sees what it needs. Rebuilding
    # dropped it and the refusal did not fire on a real host.
    #
    # This cannot prevent the process starting, and does not claim to. What it prevents is
    # the launch REPORTING success: the driver returns a refusal instead of an ``AppRef``, so
    # nothing downstream snapshots or drives the window, and every later verb re-resolves the
    # same title and refuses too. The residual — a denied packaged app can be made to start
    # once, then does nothing — is stated in the spec.
    if refuse_launched is not None:
        refusal = refuse_launched(appeared)
        if refusal:
            return DriverResult(ok=False, text=refusal)
    return DriverResult(
        ok=True,
        app=appeared,
        text=LAUNCH_RESULT.format(app=name, title=appeared.window_title, secs=waited),
    )


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


AGENT_DESKTOP_MISSING_REASON = (
    "computer use runs on the agent-desktop driver, which is not installed "
    "(install it from Settings -> Computer Use)"
)


def select_default_backend() -> ComputerUseBackend:
    """The agent-desktop backend for THIS platform, or a typed refusal.

    agent-desktop is the only driver. :func:`agent_desktop_locate.locate` must find an
    executable that verifies against the pinned digests; without one every tool
    refuses with :data:`AGENT_DESKTOP_MISSING_REASON` (or the verify reason). On macOS
    and Windows a :class:`NativeHelper` from :func:`select_native_helper` sits under
    it for what agent-desktop cannot do, and the native window capture adds the
    screenshot. Linux has neither: agent-desktop's own ``screenshot`` is used.

    Building the backend never raises: a driver that cannot load must disable one
    capability, not crash the process that asked about it.
    """
    platform_id = platform_id_for_current_os()
    if platform_id == PLATFORM_UNSUPPORTED:
        return UnsupportedBackend(platform_id, UNKNOWN_PLATFORM_REASON)
    try:
        from kiro_crew.computer_use import agent_desktop_locate

        binary = agent_desktop_locate.locate()
        reason = agent_desktop_locate.verify(binary) if binary else AGENT_DESKTOP_MISSING_REASON
    except Exception as exc:
        binary, reason = None, DRIVER_IMPORT_REASON.format(detail=exc)
    if reason or not binary:
        return UnsupportedBackend(platform_id, reason or AGENT_DESKTOP_MISSING_REASON)
    try:
        from kiro_crew.computer_use.agent_desktop_driver import AgentDesktopBackend, state_home_for

        native = select_native_helper()
        capture = _native_capture()
        return AgentDesktopBackend(
            binary,
            platform_id,
            native=native if native is not None and native.status().supported else None,
            capture=capture,
            state_home=state_home_for(str(agent_desktop_locate.INSTALL_PREFIX())),
            # No native capture (Linux): agent-desktop's own screenshot command.
            cli_capture=capture is None,
        )
    except Exception as exc:
        logger.warning("agent-desktop driver could not be built: %s", exc)
        return UnsupportedBackend(platform_id, DRIVER_IMPORT_REASON.format(detail=exc))


def _native_capture() -> "Callable[[Snapshot, int, int], Snapshot] | None":
    """The native window capture for this platform, adapted to the driver's call shape.

    Each capture function owns the secure-field and truncation floors (it returns the
    snapshot unchanged when either holds), so the agent-desktop driver inherits them
    by calling it rather than re-implementing them.
    """
    try:
        if platform_compat.IS_MACOS:
            from kiro_crew.computer_use import capture_macos as capture
        elif platform_compat.IS_WINDOWS:
            from kiro_crew.computer_use import capture_windows as capture  # type: ignore[no-redef]
        else:
            return None
    except Exception:
        logger.debug("native capture unavailable", exc_info=True)
        return None
    return lambda snap, max_px, quality: capture.capture_snapshot_image(
        snap, max_px=max_px, quality=quality
    )


def select_native_helper() -> "NativeHelper | None":
    """The :class:`NativeHelper` for THIS platform, or ``None`` (Linux, or a failed load).

    The import is deferred: it would be a circular import at module scope (the helpers
    subclass :class:`NativeHelper`), and a module-scope import would load
    ApplicationServices on every machine that merely imports the package, including
    the Linux CI fleet. A helper that will not import is logged and left out; the
    agent-desktop driver then refuses the verbs that needed it.
    """
    try:
        if platform_compat.IS_MACOS:
            from kiro_crew.computer_use.macos_driver import MacOSHelper

            return MacOSHelper()
        if platform_compat.IS_WINDOWS:
            from kiro_crew.computer_use.windows_driver import WindowsHelper

            return WindowsHelper()
    except Exception as exc:
        logger.warning("native computer-use helper unavailable: %s", exc)
    return None


def platform_could_be_supported() -> bool:
    """Whether THIS OS has a driver at all, WITHOUT loading one.

    The cheap half of the support question, for the agent spec gate, whose job is to
    avoid PAYING for a backend process on an OS that has no driver. It reads only
    ``platform_compat`` flags: macOS, Windows and Linux all run the agent-desktop
    driver. Deliberately OPTIMISTIC: it does not ask whether agent-desktop is
    installed. A host without it still gets the computer-use server, whose tools
    refuse in-band with :data:`AGENT_DESKTOP_MISSING_REASON`, and an install from
    Settings starts working at the next backend build with no agent-spec rebuild.

    Never use this to decide whether an ACTION may proceed: for that, ``status()`` is
    the only honest answer.
    """
    return platform_id_for_current_os() != PLATFORM_UNSUPPORTED


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
