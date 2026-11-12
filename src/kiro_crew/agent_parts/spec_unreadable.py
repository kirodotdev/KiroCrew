"""The unreadable-main-spec episode: a rebuild that cannot read ``kirocrew.json``.

The rebuild keeps the user's spec and skips its cycle (``_skip_unreadable_main_spec``)
instead of writing defaults over a file whose contents it could not read, and a rebuild
that writes the spec again closes the episode (``_note_main_spec_readable``). Starts are
not gated on the episode: while the file stays unreadable they behave as they would with
the file missing, and only the overwrite is gone. New work on the skip or the episode's
bookkeeping goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`. The state below is this module's initial
binding; ``kiro_crew.agent`` holds the live one, which the functions read and rebind.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kiro_crew.agent import (
        _AgentSpecUnreadable,
        _conductor_spec_held,
        _install_sibling_specs,
        logger,
        managed_mcp,
    )


#: Whether the last rebuild skipped because ``kirocrew.json`` could not be read
#: (:func:`_skip_unreadable_main_spec`), cleared once a rebuild writes it. While
#: set, the main spec may still carry auto-approvals an earlier ceiling derived,
#: so the rebuild stays held (:func:`_spec_hold_open`) and the maintenance wake
#: retries it.
_main_spec_unreadable = False

#: Counts the skips of :func:`_skip_unreadable_main_spec`, under the episode lock. A
#: rebuild reads it before it reads the spec and hands it to
#: :func:`_note_main_spec_readable`, which closes the episode only if it is still the
#: one the rebuild saw: a skip that landed while that rebuild was writing is a newer
#: episode, and the older write never read what that skip failed on.
_main_spec_episode_revision = 0

#: The governance answer generation (``governance_answer_generation``) the last
#: sibling pass of an unreadable episode ran under, or ``None`` outside an
#: episode. A later skip under the same generation has nothing new to re-filter,
#: so it runs no sibling pass (:func:`_skip_unreadable_main_spec`).
_main_spec_skip_generation: int | None = None

#: When (on :data:`_held_pass_clock`) the last sibling pass of this episode ran
#: and held a sibling spec, or ``None``. A held pass is re-run under an unchanged
#: generation only once :data:`HELD_SIBLING_PASS_RETRY_SECS` have passed, so the
#: config watcher's 2-second retry of a pending default-model change does not
#: rewrite every sibling spec on each tick while one of them stays unreadable.
_main_spec_held_pass_at: float | None = None

#: How long a held sibling pass waits before the same generation runs it again on
#: an ordinary skip (the config watcher's retry). The hourly maintenance wake does
#: not wait for it: it makes the pass due first (:func:`_make_held_sibling_pass_due`).
HELD_SIBLING_PASS_RETRY_SECS = 1800.0

#: The clock of :data:`_main_spec_held_pass_at`, a module attribute so a test can
#: step it without touching the process-wide ``time.monotonic``.
_held_pass_clock = time.monotonic

#: Guards the episode state above and the warning state below. Held only for
#: bookkeeping, never across a rebuild or a log call.
_main_spec_episode_lock = threading.Lock()

#: The warnings this episode already logged at WARNING: ``read:<errno>`` for
#: the skip (a changed cause warns again) and ``retry`` for a failed
#: maintenance retry. Episode state, under :data:`_main_spec_episode_lock`: each
#: one is decided in the critical section that reads or sets
#: :data:`_main_spec_unreadable`, and the close clears it in the same section,
#: so a key never outlives its episode or collides with the next one. Mutated
#: in place, never rebound.
_main_spec_warned: set[str] = set()


def _first_main_spec_warning(kind: str) -> bool:
    """Record *kind* for this episode; whether it was new. Caller holds the episode lock."""
    first = kind not in _main_spec_warned
    _main_spec_warned.add(kind)
    return first


def _spec_hold_open() -> bool:
    """Whether a rebuild still owes a retry: a held conductor spec, or an open episode.

    The one predicate the retry and the boot baseline read, so an open
    unreadable-spec episode is always a hold, whichever rebuild last settled
    ``_conductor_spec_held``. Read under the episode lock, the lock every writer
    of either flag takes.
    """
    with _main_spec_episode_lock:
        return _conductor_spec_held or _main_spec_unreadable


def _skip_unreadable_main_spec(
    path: Path,
    exc: _AgentSpecUnreadable,
    refresh_forks: bool | Literal["defer"],
    *,
    clean: bool,
    wrote_out: list[bool] | None,
    held_out: list[bool] | None,
) -> Path:
    """End a rebuild that found the main spec present but unreadable.

    Writing defaults would erase the user's spec, so ``kirocrew.json``
    stays as it is, and the heartbeat and worker specs mirrored from it are not
    re-derived; their existing grants are re-filtered through the current
    ceiling in place (:func:`_refilter_kept_mirrors`). The steps that write
    other files still run (:func:`_install_sibling_specs`),
    so a moved ceiling still re-filters the forks and conductor specs, and a
    fork refresh that cannot complete still records its failure for the fork
    gate. The skip is reported as an unwritten, held rebuild: ``wrote`` is
    False, so the ceiling memo stays pending and the default-model applier
    reports the change as not landed, and the held flag keeps the boot baseline
    unseeded and has the hourly maintenance wake retry the rebuild
    (:func:`retry_held_conductor_specs`). The flag and the warning come first,
    because the sibling steps can raise. Nothing here refuses a start.

    The warning is once per episode, and so is the sibling pass unless the
    governance generation moves: the config watcher retries a failed
    default-model change every tick, and each retry lands here.
    """
    global _conductor_spec_held, _main_spec_unreadable, _main_spec_skip_generation
    global _main_spec_episode_revision, _main_spec_held_pass_at
    from kiro_crew.platform.governance_profiles import governance_answer_generation

    cause = exc.__cause__
    errno_ = getattr(cause, "errno", None)
    with _main_spec_episode_lock:
        first_skip = not _main_spec_unreadable
        _conductor_spec_held = True
        _main_spec_unreadable = True
        _main_spec_episode_revision += 1
        first_warning = _first_main_spec_warning(f"read:{errno_}")
    (logger.warning if first_warning else logger.debug)(
        "cannot read the existing agent spec %s (%s); leaving it in place "
        "instead of resetting it to defaults until a rebuild can read it",
        path,
        exc,
    )
    # The sibling pass runs on the first skip of an episode and again only when
    # the governance generation moved since the last one: the config watcher
    # retries a pending default-model change every tick, and each retry lands
    # here, where a repeat pass under an unchanged generation re-filters nothing.
    # A pure read, no filesystem access.
    # The generation is recorded only after a pass that raised nothing and left
    # no fork refresh deferred: a deferred refresh (the boot path) settles after
    # this returns, and a crew-bound shared template it then holds is retried
    # only by a later pass under the same generation. A pass that held a sibling
    # spec records the generation too, with its time, and runs again under it
    # once HELD_SIBLING_PASS_RETRY_SECS have passed (the hourly maintenance wake),
    # not on each 2-second config-watch retry.
    generation = governance_answer_generation()
    now = _held_pass_clock()
    held_retry_due = (
        _main_spec_held_pass_at is not None
        and now - _main_spec_held_pass_at >= HELD_SIBLING_PASS_RETRY_SECS
    )
    if first_skip or generation != _main_spec_skip_generation or held_retry_due:
        try:
            gated_off = managed_mcp._gated_off_servers()
            sibling_held = _install_sibling_specs(
                refresh_forks, gated_off, clean=clean, mirror_main=False
            )
        finally:
            # Again after, raise or not: the sibling steps settle the memo to the
            # conductor installers' own verdict, which knows nothing of this skip.
            with _main_spec_episode_lock:
                _conductor_spec_held = True
        if refresh_forks != "defer":
            with _main_spec_episode_lock:
                _main_spec_skip_generation = generation
                _main_spec_held_pass_at = now if sibling_held else None
    if wrote_out is not None:
        wrote_out.append(False)
    if held_out is not None:
        held_out.append(True)
    return path


def _make_held_sibling_pass_due() -> None:
    """Let the next skip re-run a held sibling pass now, whatever its interval.

    The hourly maintenance wake (``retry_held_conductor_specs``) calls this before its
    rebuild. That wake is the retry a held conductor spec relies on, and it runs on its
    own schedule, so it may land inside :data:`HELD_SIBLING_PASS_RETRY_SECS` of a pass
    another rebuild (the ceiling hook, the profile watch) ran; only the config watcher's
    2-second retries are held to the interval.
    """
    global _main_spec_held_pass_at
    with _main_spec_episode_lock:
        if _main_spec_held_pass_at is not None:
            _main_spec_held_pass_at = float("-inf")


def _note_main_spec_readable(path: Path, episode_revision: int) -> None:
    """Close an unreadable-spec episode for *path* once a rebuild has written it.

    *episode_revision* is :data:`_main_spec_episode_revision` as the rebuild read
    it before it read the spec. A skip that landed since is a newer episode, one
    this write never read past, so it stays open and held. Clears the episode's
    warning state too, so the next episode warns again.
    """
    global _main_spec_unreadable, _main_spec_skip_generation, _main_spec_held_pass_at
    with _main_spec_episode_lock:
        if _main_spec_episode_revision != episode_revision:
            return
        was_open = _main_spec_unreadable
        _main_spec_skip_generation = None
        _main_spec_held_pass_at = None
        _main_spec_unreadable = False
        _main_spec_warned.clear()
    if was_open:
        logger.info("the agent spec %s reads again and was rebuilt", path)
