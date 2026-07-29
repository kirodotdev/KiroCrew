"""Snapshot, index and screenshot orchestration — the BLOCKING half of computer use.

Every method here is **synchronous and may block**: it calls into a
:class:`~kiro_crew.computer_use.backend.ComputerUseBackend`, which on macOS means
accessibility round-trips to another process and an in-process CoreGraphics
capture. Measured walks run 40-70ms on a healthy app and are bounded by the
driver's messaging timeout, but a genuinely hung target app can park the calling
thread for the whole timeout.

That is why this module is deliberately **sync with no asyncio anywhere**: it runs
in the gateway, and the gateway's rule is that no blocking syscall touches the
event loop. :mod:`kiro_crew.computer_use.tools` is the async half — it offloads
every call here onto ``run_in_executor(subprocess_executor(), ...)``. Keeping the
split at the module boundary means a future caller cannot accidentally await a
blocking body, and this module stays trivially unit-testable against the fake
backend with no event loop at all.

Responsibilities, and only these:

* resolve an app query to an :class:`AppRef` (always from the on-screen window
  list — never a process-name search);
* walk a window into a :class:`Snapshot`, persist any captured pixels, and cache
  the result in the shared :class:`~kiro_crew.computer_use.index.SnapshotIndex`;
* re-walk and compare fingerprints before a mutating action;
* forward the mutating verbs to the driver.

Deliberately NOT here: the primary-enable check, governance, the target policy and
the response shaping. Those all live at the one dispatch chokepoint in
``tools.py`` so there is exactly one place to read to know what is enforced.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import replace

from kiro_crew import platform_compat
from kiro_crew.computer_use import index as snapshot_index
from kiro_crew.computer_use import render
from kiro_crew.computer_use.backend import ComputerUseBackend, get_shared_backend
from kiro_crew.computer_use.types import (
    DEFAULT_ATTACH_SCREENSHOT,
    DEFAULT_MAX_TREE_DEPTH,
    DEFAULT_MAX_TREE_NODES,
    DEFAULT_SCREENSHOT_JPEG_QUALITY,
    DEFAULT_SCREENSHOT_MAX_PX,
    DEFAULT_TEXT_LIMIT,
    MAX_SCREENSHOT_MAX_PX,
    MAX_TEXT_LIMIT,
    MAX_TREE_DEPTH_LIMIT,
    MAX_TREE_NODES_LIMIT,
    MIN_SCREENSHOT_MAX_PX,
    SCREENSHOT_DIR_NAME,
    SCREENSHOT_FILE_PREFIX,
    SCREENSHOT_FILE_SUFFIX,
    SCREENSHOT_KEEP,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
    StaleIndex,
)

logger = logging.getLogger(__name__)

# Screenshot spool. ``tempfile.gettempdir()`` (never a hardcoded ``/tmp`` or a
# raw ``TMPDIR`` read) is the exact idiom ``mcp_playwright_proxy`` uses and the
# one the repo pins by source text: it honors the platform's real temp location,
# including the per-user private dir macOS hands a sandboxed process.
_SHOT_DIR_MODE = 0o700
# Millisecond resolution in the spooled filename: two captures inside the same
# second are routine (a get_state followed by a mutating action's re-snapshot), and
# a second-resolution name would make the ring trim's name ordering ambiguous. The
# name's UNIQUENESS comes from ``mkstemp``, not from this — see ``_persist_image``.
_TIMESTAMP_SCALE = 1000


class ComputerUseService:
    """Blocking orchestration over one backend and one snapshot cache.

    Thread-safe by composition: the backend contract requires thread safety and
    :class:`SnapshotIndex` locks internally. The one piece of state this class
    adds — the screenshot ring — is guarded by :attr:`_shot_lock`, because two
    concurrent snapshots would otherwise race the trim and could unlink a file
    the other just reported to the model.
    """

    def __init__(
        self,
        *,
        backend: "ComputerUseBackend | None" = None,
        idx: "snapshot_index.SnapshotIndex | None" = None,
    ) -> None:
        self._backend = backend
        self._index = idx
        self._shot_lock = threading.Lock()

    # ── wiring ──

    @property
    def backend(self) -> ComputerUseBackend:
        """The active driver.

        Resolved lazily through :func:`get_shared_backend` rather than captured
        in ``__init__`` so a test (or the runtime swap seam) that re-registers a
        backend after this service was built is honored on the next call instead
        of being pinned to a stale instance.
        """
        return self._backend if self._backend is not None else get_shared_backend()

    @property
    def index(self) -> snapshot_index.SnapshotIndex:
        """The active snapshot cache (shared unless one was injected)."""
        return self._index if self._index is not None else snapshot_index.get_shared_index()

    # ── status / diagnostics ──

    def status(self) -> BackendStatus:
        """Whether computer use can run here, and why not if it can't."""
        return self.backend.status()

    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY permission hints. Never a gate — see :class:`PermissionProbe`."""
        return self.backend.probe_permissions()

    # ── observation ──

    def list_apps(self) -> tuple[AppRef, ...]:
        """Applications with an on-screen window.

        Raises :class:`ComputerUseError` when the driver refuses, so the caller
        has exactly one failure shape to render regardless of whether the driver
        returned ``ok=False`` or the platform has no driver at all.
        """
        result = self.backend.list_apps()
        self._require_ok(result)
        return result.apps

    def resolve_app(self, query: str) -> AppRef:
        """Resolve *query* (display name or bundle id) to one application."""
        result = self.backend.resolve_app(query)
        self._require_ok(result)
        if result.app is None:
            # A driver that reports success without an app is a contract
            # violation; refuse rather than fabricate an AppRef whose pid would
            # later address the wrong process.
            raise ComputerUseError(f"the driver resolved '{query}' to no application")
        return result.app

    def snapshot(self, app: AppRef, req: SnapshotRequest, *, session_key: str) -> Snapshot:
        """Walk *app*'s focused window, persist any pixels, and cache the result.

        The cache write is what makes element indices usable by a subsequent
        action call, so it happens here rather than at the call site — a walk the
        model was shown but that was never cached would refuse every follow-up.

        *session_key* namespaces that cache write. It is REQUIRED rather than
        defaulted: an omitted key would silently pool one surface's indices with
        every other's, which is the cross-session mix-up this parameter exists to
        prevent, and a default would make that omission invisible at the call site.
        """
        result = self.backend.snapshot(app, req)
        self._require_ok(result)
        snap = result.snapshot
        if snap is None:
            raise ComputerUseError(f"the driver returned no state for '{app.label}'")
        snap = self._persist_image(snap)
        self.index.put(snap, session_key=session_key)
        return snap

    def cached(self, app: AppRef, *, session_key: str) -> Snapshot:
        """This session's last cached snapshot for *app*, or raise :class:`StaleIndex`.

        Never lazily re-snapshots. A lazy re-walk here would silently let the
        model act on a tree it was never shown, which is precisely the failure
        element indices exist to make impossible.
        """
        return self.index.require(
            app.window_key, app.bundle_id or app.name, session_key=session_key
        )

    def element(self, snap: Snapshot, element_index: int) -> ElementRec:
        """The record at *element_index* within *snap* (raises :class:`StaleIndex`)."""
        return self.index.resolve(snap, element_index)

    # ── drift verification ──

    def verify_fingerprint(
        self, app: AppRef, rec: ElementRec, req: SnapshotRequest, *, session_key: str
    ) -> None:
        """Re-walk *app* and confirm *rec* still sits at its index. Else raise.

        The real guard behind element addressing, and cheap enough to run
        unconditionally before every mutating action (40-70ms measured; 145 nodes
        for a native window, 1431 for a Chromium one).

        On drift the cache is DROPPED and :class:`StaleIndex` is raised naming
        both identities. Dropping matters: leaving the old snapshot installed
        would let a second action attempt resolve the same stale index again, and
        caching the *fresh* walk instead would hand the model indices from a tree
        it has never seen — the exact failure this check exists to prevent. The
        model must call ``computer_get_state`` again.

        Honest limit, stated in the spec too: this narrows the race, it does not
        eliminate it. The tree can still change between this walk and the action
        microseconds later. It converts a silent wrong-click into a loud refusal
        in the overwhelming majority of cases; it is not a transactional
        guarantee.
        """
        # want_image=False: the verification walk exists to compare structure, and
        # capturing (then discarding) pixels would double the cost of every
        # mutating action and spool a screenshot nobody asked for.
        fresh_req = replace(req, want_image=False)
        result = self.backend.snapshot(app, fresh_req)
        self._require_ok(result)
        fresh = result.snapshot
        if fresh is None:
            raise ComputerUseError(f"the driver returned no state for '{app.label}'")
        before = render.fingerprint(rec)
        try:
            now_rec = self.index.resolve(fresh, rec.index)
        except StaleIndex:
            # The index vanished from the fresh walk: the element is gone. Report
            # it as drift (the actionable framing) rather than as "unknown index",
            # which would read as a model error.
            self.index.invalidate(app.window_key, session_key=session_key)
            raise StaleIndex(
                snapshot_index.drift_message(
                    app.bundle_id or app.name,
                    rec.index,
                    render.describe_record(rec),
                    "no element at that index",
                )
            )
        after = render.fingerprint(now_rec)
        if after != before:
            self.index.invalidate(app.window_key, session_key=session_key)
            raise StaleIndex(
                snapshot_index.drift_message(
                    app.bundle_id or app.name,
                    rec.index,
                    render.describe_record(rec),
                    render.describe_record(now_rec),
                )
            )

    # ── mutation ──

    def click(self, app: AppRef, rec: "ElementRec | None", req: ClickRequest) -> str:
        """Click *rec* or *req*'s point, by the method *req* already resolved to.

        *req* carries a CONCRETE method: ``auto`` is resolved upstream at the
        dispatch chokepoint, and the pointer-moving method has already cleared both
        of its permits there. This layer only forwards.
        """
        return self._act(self.backend.click(app, rec, req))

    def drag(self, app: AppRef, req: DragRequest) -> str:
        """Drag between *req*'s two screen points inside *app*."""
        return self._act(self.backend.drag(app, req))

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> str:
        """Type *text* into *rec*, or into whatever *app* has focused."""
        return self._act(self.backend.type_text(app, rec, text))

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> str:
        """Send one key spec (``"cmd+shift+a"``) to *rec* in *app*."""
        return self._act(self.backend.press_key(app, rec, key))

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> str:
        """Set *rec*'s value directly, with no keystrokes."""
        return self._act(self.backend.set_value(app, rec, value))

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> str:
        """Scroll *rec* by *pages* in *direction*."""
        return self._act(self.backend.scroll(app, rec, direction, pages))

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> str:
        """Perform a named accessibility action on *rec*."""
        return self._act(self.backend.perform_action(app, rec, action))

    def end_turn(self, *, session_key: str) -> None:
        """Drop THIS session's cached snapshots (the explicit early release)."""
        self.index.end_turn(session_key=session_key)

    # ── internals ──

    @staticmethod
    def _require_ok(result: DriverResult) -> None:
        """Convert a driver refusal into :class:`ComputerUseError`.

        Drivers never raise (the seam contract), so failures arrive as
        ``ok=False``. Raising here gives the dispatcher one ``except`` to write
        instead of a success check after every call — a check that is easy to
        forget and whose omission would silently act on an empty result.
        """
        if not result.ok:
            raise ComputerUseError(result.text or "the computer-use driver refused the call")

    @staticmethod
    def _act(result: DriverResult) -> str:
        """Return a mutating call's confirmation text, or raise its refusal."""
        ComputerUseService._require_ok(result)
        return result.text

    def _persist_image(self, snap: Snapshot) -> Snapshot:
        """Spool *snap*'s JPEG bytes to an owner-only file and record the path.

        Only the PATH is ever relayed to the model; the bytes never enter a tool
        result (the MCP transport cannot express an image block, so this is a
        property of the transport rather than a policy that can regress).

        Fail-soft: a spool failure degrades to a tree-only result. Refusing the
        whole call because a temp write failed would turn a cosmetic problem into
        an outage, and the accessibility tree is the primary channel anyway.
        """
        if not snap.image_jpeg or snap.image_path:
            return snap
        try:
            with self._shot_lock:
                shot_dir = _shot_dir()
                # ATOMIC unique allocation, not a millisecond timestamp (reviewer
                # finding). ``self._shot_lock`` serializes writers within ONE service
                # instance, but it cannot serialize a second process — the gateway,
                # the CLI and the permission-probe child all spool into the same
                # ``tempfile.gettempdir()`` directory — so two captures inside the
                # same millisecond resolved to one path and the second truncated the
                # first. The caller of the first is then handed a path holding a
                # screenshot of an application it never asked about, which is a
                # cross-capture pixel leak rather than merely a lost file.
                #
                # ``mkstemp`` also creates the file 0o600 from the outset, so there is
                # no window in which it exists world-readable before
                # ``restrict_to_owner`` runs. The timestamp stays in the PREFIX
                # because the ring trim orders by name and a human reading the spool
                # wants it. Mirrors ``capture_macos.persist_jpeg``, which the same
                # finding was applied to earlier.
                prefix = f"{SCREENSHOT_FILE_PREFIX}{int(time.time() * _TIMESTAMP_SCALE)}-"
                handle_fd, path = tempfile.mkstemp(
                    prefix=prefix, suffix=SCREENSHOT_FILE_SUFFIX, dir=shot_dir
                )
                with os.fdopen(handle_fd, "wb") as handle:
                    handle.write(snap.image_jpeg)
                # Owner-only, fail-loud on POSIX and a real DACL on Windows. The
                # frame can contain anything that was on screen, so a
                # world-readable temp file would be a disclosure even though the
                # directory is 0o700.
                platform_compat.restrict_to_owner(path)
                _trim_ring(shot_dir)
            return replace(snap, image_path=path)
        except Exception:
            logger.debug("computer-use screenshot spool failed", exc_info=True)
            # Drop the bytes as well as the path: keeping them would make
            # ``render`` report a size for an image that exists nowhere.
            return replace(snap, image_jpeg=b"", image_width=0, image_height=0)


def _shot_dir() -> str:
    """The screenshot spool directory, created ``0o700`` on first use."""
    shot_dir = os.path.join(tempfile.gettempdir(), SCREENSHOT_DIR_NAME)
    os.makedirs(shot_dir, mode=_SHOT_DIR_MODE, exist_ok=True)
    # ``makedirs`` honors the umask, and ``exist_ok=True`` skips the mode
    # entirely for a directory that already exists (including one a previous
    # laxer build created), so re-assert the mode every time.
    platform_compat.chmod_safe(shot_dir, _SHOT_DIR_MODE)
    return shot_dir


def _trim_ring(shot_dir: str) -> None:
    """Keep at most :data:`SCREENSHOT_KEEP` spooled frames, oldest first.

    The spool is a cache, not an archive: a long session must not be able to
    fill the temp volume. Best-effort — a file another process removed under us
    is not an error worth failing a snapshot over.
    """
    try:
        names = [
            name
            for name in os.listdir(shot_dir)
            if name.startswith(SCREENSHOT_FILE_PREFIX) and name.endswith(SCREENSHOT_FILE_SUFFIX)
        ]
    except OSError:
        return
    if len(names) <= SCREENSHOT_KEEP:
        return
    # Filename-sorted rather than mtime-sorted: the name carries a millisecond
    # timestamp, so lexical order IS chronological order and no stat() per file
    # is needed (the mtime of a file being written is also racier than its name).
    for name in sorted(names)[: len(names) - SCREENSHOT_KEEP]:
        try:
            os.unlink(os.path.join(shot_dir, name))
        except OSError:
            logger.debug("computer-use screenshot trim skipped %s", name, exc_info=True)


def snapshot_request(
    *,
    max_nodes: "int | None" = None,
    max_depth: "int | None" = None,
    text_limit: "int | None" = None,
    want_image: "bool | None" = None,
) -> SnapshotRequest:
    """Build a :class:`SnapshotRequest` from config, overridden per call.

    Precedence is caller argument, then the operator's ``config.json``
    ``computer_use`` section, then the shipped defaults in ``types``. Every value
    is clamped to the hard ceiling in ``types`` so a hand-edited config cannot
    ask for an unbounded walk or a 40-megapixel capture; the MCP schemas clamp
    the agent-supplied side independently.
    """
    cfg = _config_section()
    return SnapshotRequest(
        max_nodes=_clamped(
            max_nodes,
            getattr(cfg, "max_tree_nodes", None),
            DEFAULT_MAX_TREE_NODES,
            1,
            MAX_TREE_NODES_LIMIT,
        ),
        max_depth=_clamped(
            max_depth,
            getattr(cfg, "max_tree_depth", None),
            DEFAULT_MAX_TREE_DEPTH,
            1,
            MAX_TREE_DEPTH_LIMIT,
        ),
        text_limit=_clamped(
            text_limit,
            getattr(cfg, "text_limit", None),
            DEFAULT_TEXT_LIMIT,
            1,
            MAX_TEXT_LIMIT,
        ),
        want_image=_first_bool(
            want_image,
            getattr(cfg, "attach_screenshot", None),
            DEFAULT_ATTACH_SCREENSHOT,
        ),
        image_max_px=_clamped(
            None,
            getattr(cfg, "screenshot_max_px", None),
            DEFAULT_SCREENSHOT_MAX_PX,
            MIN_SCREENSHOT_MAX_PX,
            MAX_SCREENSHOT_MAX_PX,
        ),
        image_quality=_clamped(
            None,
            getattr(cfg, "screenshot_jpeg_quality", None),
            DEFAULT_SCREENSHOT_JPEG_QUALITY,
            1,
            100,
        ),
    )


def _config_section() -> object:
    """The ``computer_use`` config section, or a stand-in with no attributes.

    Resolved defensively through ``getattr`` rather than imported as a type: the
    display/limit section is optional-by-construction (the primary enable lives on
    the keystone, not in ``config.json``), so a config build without the section
    must fall back to the shipped defaults instead of raising inside a tool call.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return getattr(KiroCrewConfig.load(), "computer_use", object())
    except Exception:
        logger.debug("computer-use config section unavailable; using defaults", exc_info=True)
        return object()


def _clamped(override: "int | None", configured: object, default: int, low: int, high: int) -> int:
    """First usable int of (*override*, *configured*, *default*), clamped to range."""
    for candidate in (override, configured):
        # ``bool`` is an ``int`` subclass; a stray ``true`` in a hand-edited
        # config must not become the number 1.
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return max(low, min(high, candidate))
    return max(low, min(high, default))


def _first_bool(override: "bool | None", configured: object, default: bool) -> bool:
    """First real bool of (*override*, *configured*, *default*)."""
    for candidate in (override, configured):
        if isinstance(candidate, bool):
            return candidate
    return default


# ── Process-wide shared service ──
# One per process, matching the single shared backend and the single shared
# snapshot cache it composes. Threading a service through every call site would
# be state for no benefit, and a second instance would give a second screenshot
# ring racing the first one's trim.

_shared_service: "ComputerUseService | None" = None
_shared_service_lock = threading.Lock()


def get_shared_service() -> ComputerUseService:
    """Process-wide :class:`ComputerUseService` singleton."""
    global _shared_service
    with _shared_service_lock:
        if _shared_service is None:
            _shared_service = ComputerUseService()
        return _shared_service


def reset_shared_service() -> None:
    """Drop the shared service (tests, backend swap, data-home change).

    Does NOT close the backend — that is ``reset_shared_backend``'s job, and
    doing it here too would close a driver another holder is still using.
    """
    global _shared_service
    with _shared_service_lock:
        _shared_service = None


__all__ = [
    "ComputerUseService",
    "get_shared_service",
    "reset_shared_service",
    "snapshot_request",
]
