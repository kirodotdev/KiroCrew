"""What the event loop was running while the controller's timer was late.

The controller measures loop lag after the fact: its timer fires late, and by
then whatever held the loop has returned. :class:`LoopBusySampler` is one
daemon thread that looks while the lag is happening. The controller arms it
before each sleep with the time by which the loop should have woken it plus the
lag that counts as pressure; if the controller has not woken by then, the thread
reads the loop thread's current frame once. The controller then disarms it and
gets back one short description of that frame.

A description names code, never data: a module name and a function's qualified
name, each bounded, with no file path, argument or local value, so it is safe in
gateway.log. One frame read per late tick and none on a punctual one keeps it
cheap.
"""

from __future__ import annotations

import sys
import threading
import time
from types import FrameType
from typing import Callable, Optional

#: The most characters one ``module.qualname`` part keeps.
_PART_MAX_CHARS = 120
#: The most frames walked outward looking for this package's own code.
_MAX_DEPTH = 64
_OWN_PACKAGE = "kiro_crew"


def _name_of(frame: FrameType) -> str:
    module = frame.f_globals.get("__name__")
    qualname = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
    name = f"{module}.{qualname}" if isinstance(module, str) and module else str(qualname)
    return name[:_PART_MAX_CHARS]


def _is_own(frame: FrameType) -> bool:
    module = frame.f_globals.get("__name__")
    return isinstance(module, str) and (
        module == _OWN_PACKAGE or module.startswith(_OWN_PACKAGE + ".")
    )


def _handle_callback(frame: FrameType) -> Optional[str]:
    """The callback *frame* runs when it is asyncio's ``Handle._run``, else ``None``.

    Frames beyond ``Handle._run`` are the loop's own machinery and whatever
    started the loop, so they never say what held it; the handle's callback does.
    A native callback has no Python frame of its own, which leaves this as the
    only name for it. ``""`` when the handle carries no callback.
    """
    code = frame.f_code
    if code.co_name != "_run" or frame.f_globals.get("__name__") != "asyncio.events":
        return None
    # The watchdog's own naming: attributes only, never ``repr``, which would run
    # arbitrary ``__repr__`` code on this thread while the loop is inside it.
    # Imported here because only a late tick inside a handle reaches this, and
    # light processes import this package without ever loading the dashboard.
    from kiro_crew.dashboard.stall_enrichment import _HANDLE_RUN_CODE, _callable_name

    if code is not _HANDLE_RUN_CODE:
        return None
    callback = getattr(frame.f_locals.get("self"), "_callback", None)
    if callback is None:
        return ""
    name, _native = _callable_name(callback)
    return name[:_PART_MAX_CHARS]


def describe_frame(frame: Optional[FrameType]) -> str:
    """The innermost frame as ``module.qualname``, and the innermost frame of
    this package below it as ``via module.qualname`` when the two differ.

    The innermost frame alone is often a library call (``json.decoder.raw_decode``,
    ``select.select``) that does not say which caller held the loop; the
    package frame does. The walk outward stops at asyncio's ``Handle._run``:
    with no package frame inside the callback it is running, that callback is
    named instead. ``""`` for no frame.
    """
    if frame is None:
        return ""
    top = _name_of(frame)
    if _is_own(frame):
        return top
    current: Optional[FrameType] = frame
    for _ in range(_MAX_DEPTH):
        if current is None:
            break
        if current is not frame and _is_own(current):
            return f"{top} via {_name_of(current)}"
        callback = _handle_callback(current)
        if callback is not None:
            return f"{top} via {callback}" if callback else top
        current = current.f_back
    return top


class LoopBusySampler:
    """One daemon thread that reads a late loop's current frame.

    :meth:`arm` and :meth:`disarm` are called from the loop thread around each
    controller sleep; the worker only reads a frame while armed and past the
    deadline, and keeps at most one description.
    """

    def __init__(
        self,
        *,
        frames: Callable[[], dict[int, FrameType]] = sys._current_frames,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frames = frames
        self._clock = clock
        self._cond = threading.Condition()
        self._loop_thread: Optional[int] = None
        self._deadline: Optional[float] = None
        self._busy = ""
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    def start(self, loop_thread_id: int) -> None:
        with self._cond:
            self._loop_thread = loop_thread_id
            self._stopped = False
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="adaptive-loop-busy", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._deadline = None
            self._cond.notify_all()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def arm(self, after_secs: float) -> None:
        """Look at the loop thread if it is still busy *after_secs* from now."""
        with self._cond:
            self._busy = ""
            self._deadline = self._clock() + max(0.0, after_secs)
            self._cond.notify_all()

    def disarm(self) -> str:
        """End this window and return what the loop was running, or ``""``."""
        with self._cond:
            self._deadline = None
            busy, self._busy = self._busy, ""
            self._cond.notify_all()
            return busy

    def sample_now(self) -> str:
        """Read the loop thread's frame once (the worker's one action)."""
        tid = self._loop_thread
        if tid is None:
            return ""
        try:
            frame = self._frames().get(tid)
            # Inside the guard too: naming a handle's callback reads the frame's
            # locals and the callable's attributes.
            return describe_frame(frame)
        except Exception:  # noqa: BLE001 -- a diagnostic must never raise
            return ""

    def _run(self) -> None:
        with self._cond:
            while not self._stopped:
                deadline = self._deadline
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - self._clock()
                if remaining > 0:
                    self._cond.wait(timeout=remaining)
                    continue
                # Past the deadline and still armed: the loop has not run the
                # controller since. One read per window.
                self._deadline = None
                self._busy = self.sample_now()
