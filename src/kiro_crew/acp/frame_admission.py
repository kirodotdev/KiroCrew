"""Hold a session-open admission to the moment its frame is written.

A ``session/new`` or ``session/load``, and a ``session/set_mode`` kiro-cli
resolves by name, is admitted by an off-loop check against the
unreadable-main-spec episode (:func:`kiro_crew.agent.require_main_spec_launch_bound`).
The frame then waits for the transport's stdin lock, which a co-tenant can hold
for as long as kiro-cli is not reading. An episode that opens during that wait
would let the frame go out on an admission that has lapsed.

So the episode reading the check ran against
(:func:`kiro_crew.agent.main_spec_episode`, taken before the check) travels with
the frame, and the writer compares it with the current reading under the lock,
right before the write. That comparison is an in-process read of two fields
under a lock held only for bookkeeping, never across I/O, so the stdin lock is
never held across the check's own filesystem reads or a thread hop. When the
reading moved, nothing is written and the lock is released (the writer's
``before_write`` contract), and the check runs again off the lock: it refuses
with its own refusal (one SEL denial and the early retry) or admits on the new
reading, and the same frame is tried again.

The direct client checks and writes in one function and passes the admission
itself. On the shared runtime the check runs in the caller and the write deep
in a send path many callers share, so the caller holds the admission for its
send (:func:`holding`) and the writer reads it back (:func:`held_admission`),
only for the methods it admits: no other frame sent in that scope is held.

The reading pins the episode and nothing else. The check's other inputs, the
name the main spec last declared and the project spec files under the cwd, can
change during the wait without moving it; kiro-cli resolves the project spec
before the user level, after the write, so no check under the lock could close
that, and the governance spec documents it as a residual.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW, METHOD_SET_MODE

_T = TypeVar("_T")

EpisodeReading = tuple[int, bool]

#: Writes of one frame, the first included, before an open whose reading keeps
#: moving under the lock fails. Each retry needs an episode transition during
#: the lock wait, so a healthy host never retries.
ADMISSION_ATTEMPTS = 3


#: The frames an admission holds: the session opens and the ``set_mode`` that
#: activates an agent on one.
ADMITTED_METHODS = frozenset({METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SET_MODE})

_held: contextvars.ContextVar[FrameAdmission | None] = contextvars.ContextVar(
    "kirocrew_frame_admission", default=None
)


class AdmissionMoved(Exception):
    """The episode reading moved while the frame waited for the stdin lock."""


@dataclass
class FrameAdmission:
    """The episode reading a frame was admitted on, and the check that admitted it.

    ``recheck`` runs the check again and returns the reading it ran against; it
    raises the check's own refusal. *agent* is the agent the frame starts, named
    in the SEL denial an open that never settles writes.
    """

    reading: EpisodeReading
    recheck: Callable[[], Awaitable[EpisodeReading]]
    agent: str


def _read_episode() -> EpisodeReading:
    from kiro_crew.agent import main_spec_episode

    return main_spec_episode()


async def admit(check: Callable[[], Awaitable[None]], *, agent: str) -> FrameAdmission:
    """Run *check* and return the admission it gave, bound to the reading before it.

    The reading is taken before the check, so an episode that opens while the
    check runs (its own reads happen off the loop) moves it.
    """

    async def run() -> EpisodeReading:
        reading = _read_episode()
        await check()
        return reading

    return FrameAdmission(await run(), run, agent)


@contextlib.contextmanager
def holding(admission: FrameAdmission) -> Iterator[None]:
    """Hold *admission* for the admitted frame sent inside this scope.

    Anything but a :class:`FrameAdmission` raises ``TypeError`` before the scope
    opens: a hold of ``None`` would let the frame inside it go out unchecked.
    """
    if not isinstance(admission, FrameAdmission):
        raise TypeError(f"holding() needs a FrameAdmission, got {type(admission).__name__}")
    token = _held.set(admission)
    try:
        yield
    finally:
        _held.reset(token)


def held_admission(method: str) -> FrameAdmission | None:
    """The admission held for a *method* frame, or ``None`` for any other frame."""
    if method not in ADMITTED_METHODS:
        return None
    return _held.get()


def under_lock_check(
    admission: FrameAdmission, base: Callable[[], None] | None = None
) -> Callable[[], None]:
    """The ``before_write`` hook that refuses a frame whose reading moved.

    *base* is the transport's own under-lock check (a dead runtime), run first.
    """

    def check() -> None:
        if base is not None:
            base()
        if _read_episode() != admission.reading:
            raise AdmissionMoved()

    return check


async def write_admitted(
    write: Callable[[Callable[[], None]], Awaitable[_T]],
    admission: FrameAdmission,
    *,
    unsettled: Callable[[str], Exception],
    base: Callable[[], None] | None = None,
) -> _T:
    """Write one frame through *write*, held to *admission* under the lock.

    *write* takes the ``before_write`` hook and performs one locked write. A
    reading that moved runs the check again and retries the same bytes; after
    :data:`ADMISSION_ATTEMPTS` writes the open fails with ``unsettled(message)``,
    after the same SEL denial and early retry as any other launch-bound refusal
    (:func:`kiro_crew.agent.refuse_unprojected_main_spec_start`).
    """
    for attempt in range(ADMISSION_ATTEMPTS):
        try:
            return await write(under_lock_check(admission, base))
        except AdmissionMoved:
            if attempt + 1 == ADMISSION_ATTEMPTS:
                break
            admission.reading = await admission.recheck()
    from kiro_crew.agent import refuse_unprojected_main_spec_start

    # Off the loop: the denial is a file write.
    await asyncio.to_thread(refuse_unprojected_main_spec_start, admission.agent)
    raise unsettled(
        "The agent spec's readability kept changing while this session was "
        "starting, so it was not started. Start it again; if it keeps failing, "
        "restart the gateway."
    )
