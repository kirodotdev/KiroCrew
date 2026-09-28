"""Kill an agent process from outside and require the gateway to recover.

The leak gate next door measures the tidy path: sessions open, sessions close,
nothing is left. This module measures the untidy one. A process dies without
telling anyone -- ``SIGKILL`` from outside, which is what an OOM kill, a
``pkill``, or an operator with a stuck terminal actually looks like -- and five
things have to follow:

1. the affected session's NEXT turn completes,
2. the killed pid leaves the registry, so nothing later signals that number,
3. no descendant of the killed process is still alive,
4. some log line names the pid, so the death is recorded rather than absorbed,
5. no managed MCP stub is left unowned by the death.

Point 4 is asserted on the PID, not on a message. The wording of a death log
belongs to the recovery path and is expected to change; that a death is recorded
at all is the contract. Asserting the number keeps this gate from breaking every
time a sentence is rephrased, while still failing when a kill vanishes silently.

Point 3 is what "orphan" means here. A child whose parent is killed is
reparented -- to ``1``, or to whatever subreaper is above it -- and keeps
running. It has no owner at that point, so it is a leak whoever it now answers
to, which is why the check is "is it alive" and not "who is its parent".

The kill target is an agent RUNTIME root: the per-session backend process the
registry tracks, and the one process a session provably has once it has taken a
turn. It is selected by pid AND by the start identity the registry line records,
and that identity is re-checked in the same breath as the signal -- a victim
chosen on liveness alone could exit in between and have its number reused, which
would send SIGKILL to an unrelated process. The module reconciles against pid
recycling everywhere else; it must not commit the same error itself.

Two kill targets the leak-class table also names are NOT covered here, and both
are capability limits rather than oversights.

A managed MCP STUB cannot be killed deliberately because this fixture runs none:
MCP servers reach a session through the agent spec, and the offline seed declares
none. Its population is still asserted on either side of the runtime kill, so a
stub stranded by that death fails this test; what is missing is the dedicated
"kill the stub itself" leg, which needs a fixture that declares a server.

A SUBAGENT process appears only when an agent decides to spawn one, and the
offline fake backend answers prompt sentinels rather than reasoning; the repo's
pod scenario states the same boundary for the same reason. Covering it needs a
model-backed lane.

Neither gap is represented by a skipped test. A skipped test counts as a pass and
reads as coverage on the board, so the gaps are stated here in prose and in the
pull request instead, and only the leg that actually runs is shipped as a test.

Gating matches the leak module: ``KIROCREW_E2E`` lifts the module skip, and
``KIROCREW_E2E_REQUIRE`` turns an unmet precondition into a failure instead of a
skip that would count as a pass. See ``docs/ci/e2e-gate.md``.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
from e2e.process_inventory import (
    SESSION_PID_FILE,
    Inventory,
    RegistryEntry,
    assert_slice_contract,
    descendants_of,
    inventory,
    pid_alive,
    read_argv,
    read_registry,
    start_token,
    survivors,
)
from e2e.test_gateway_boot_matrix import _Client
from e2e.test_process_leak_invariant import (
    REPLY_TIMEOUT,
    _open_session_and_take_a_turn,
    _require_real_cgroups,
    _workspace_src,
    measured_gateway,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Real-process chaos harness. Set KIROCREW_E2E=1 to run.",
)

#: Sessions opened before the kill. Two rather than one so the run also shows
#: that killing one session's runtime did not take the other's down with it.
SESSION_COUNT = 2

#: How long the registry gets to forget a killed pid, and descendants get to
#: stop. Polled to this deadline rather than slept through, so a healthy run
#: returns as soon as it is true.
RECOVER_TIMEOUT = 120.0

#: Gap between recovery polls.
POLL_SECS = 1.0


@dataclass(frozen=True)
class _Live:
    """One booted gateway with live sessions on it, ready to be disturbed."""

    handle: Any
    client: _Client
    home: Path
    #: ``(slot, agent)`` per open session; the agent is needed to drive a later turn.
    sessions: list[tuple[str, str]]
    #: The inventory taken while every session was healthy, for choosing a victim.
    live: Inventory


def _runtime_roots(home: Path) -> list[RegistryEntry]:
    """Live agent-runtime roots, as whole registry entries.

    The ENTRY is returned, not the bare pid, because the line carries the start
    identity that makes signalling that pid safe. The session file holds one line
    per runtime root, which is the process whose death ends a session's turn;
    descendants live in the other file and are not roots.
    """
    return [
        entry
        for entry in read_registry(home)
        if entry.source == SESSION_PID_FILE and pid_alive(entry.pid)
    ]


def _kill_verified(entry: RegistryEntry, what: str) -> None:
    """Pin *entry*'s pid, prove it is the process the registry named, then signal it.

    The pidfd is opened BEFORE the identity is checked, and the signal goes
    through the fd rather than the number. That ordering is what closes the
    window: a bare ``os.kill`` after a separate identity read can still land on a
    replacement, because the process may exit and its number be reassigned
    between the two calls, and nothing in the test would notice -- the victim is
    dead either way, so a mis-delivered SIGKILL leaves the run green while having
    killed something on the host that the harness never selected. A pidfd pins
    the process object, so a number reused after the pin cannot be retargeted.

    This mirrors ``session_scope_reap._pidfd_signal_owned``, which the product
    already ships for the same hazard.

    Refuses rather than falling back to a bare kill: an entry with no recorded
    identity, a vanished or recycled victim, or a host without pidfd all abort
    the test. The module reconciles against pid recycling everywhere else and
    must not commit the error itself, and a refusal costs only a skipped
    measurement where a wrong kill costs an unrelated process.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        pytest.skip(
            "pidfd signalling is unavailable on this host, and this test refuses to "
            "signal a bare pid: the victim could exit between the identity check and "
            "the signal, and an unrelated process would be killed"
        )
    if entry.token is None:
        pytest.fail(
            f"the registry entry for the {what} (pid {entry.pid}) carries no start "
            "identity, so it cannot be proven to still name the tracked process; "
            "refusing to signal a bare pid"
        )
    try:
        fd = pidfd_open(entry.pid)
    except ProcessLookupError:
        pytest.fail(
            f"the {what} (pid {entry.pid}) vanished before it could be pinned, so "
            "the recovery this test measures never had a subject"
        )
    except OSError as exc:
        pytest.fail(f"could not pin the {what} (pid {entry.pid}) with a pidfd: {exc}")
    try:
        # Read the identity only AFTER the pin, so what is verified is what the fd
        # holds rather than whatever happens to own the number at read time.
        current = start_token(entry.pid)
        if current is None:
            pytest.fail(
                f"the {what} (pid {entry.pid}) vanished before it could be killed, so "
                "the recovery this test measures never had a subject"
            )
        if current != entry.token:
            pytest.fail(
                f"the {what} (pid {entry.pid}) is not the process the registry "
                f"recorded (start {current} against {entry.token}): its number was "
                "reused, and signalling it would kill an unrelated process"
            )
        try:
            pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pytest.fail(f"the {what} (pid {entry.pid}) exited before the signal landed")
        except OSError as exc:
            pytest.fail(f"could not signal the pinned {what} (pid {entry.pid}): {exc}")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _await_registry_forgets(home: Path, pid: int) -> list[int]:
    """Poll until *pid* is in no registry entry. Returns what is still tracked."""
    deadline = time.monotonic() + RECOVER_TIMEOUT
    tracked = [entry.pid for entry in read_registry(home)]
    while time.monotonic() < deadline:
        if pid not in tracked:
            return tracked
        time.sleep(POLL_SECS)
        tracked = [entry.pid for entry in read_registry(home)]
    return tracked


def _await_descendants_gone(kin: set[int]) -> set[int]:
    """Poll until no member of *kin* is alive. Returns the survivors."""
    deadline = time.monotonic() + RECOVER_TIMEOUT
    left = survivors(kin)
    while time.monotonic() < deadline and left:
        time.sleep(POLL_SECS)
        left = survivors(kin)
    return left


def _log_baseline(home: Path, captured: str) -> tuple[int, frozenset[str]]:
    """What the two log sources held BEFORE the kill, as a point to read past.

    The victim's pid is already all over the startup log -- it was spawned,
    tracked and reported -- so searching the whole log for the number proves only
    that the process once existed, and the death assertion would pass on a
    gateway that never recorded the death at all.

    The two sources need different baselines because they behave differently.
    ``gateway.log`` is append-only, so a byte offset is exact. The captured
    output is NOT a growing buffer: it is rebuilt on each call from bounded tails
    of stderr and stdout, so a character index taken from an earlier call indexes
    a different string, and once those tails saturate it points at content that
    predates the kill. Its baseline is therefore the SET of lines already seen,
    which no truncation can invalidate.
    """
    try:
        size = (home / "gateway.log").stat().st_size
    except OSError:
        size = 0
    return size, frozenset(captured.splitlines())


def _logs_naming(
    home: Path, pid: int, captured: str, baseline: tuple[int, frozenset[str]]
) -> list[str]:
    """Lines new since *baseline* that mention *pid* as a whole number.

    Both sources are read because they are populated differently: the file is
    the gateway's own rotating log, and the captured output is what a harness
    child writes to its pipes. A death recorded in either one is recorded.

    The pid is matched as a complete numeric token, not as a substring: pid 123
    appears inside 1234, inside a port, and inside a timestamp, so a substring
    test would accept an unrelated line as proof that the death was recorded.
    """
    pattern = re.compile(rf"(?<!\d){pid}(?!\d)")
    file_offset, seen_lines = baseline
    hits: list[str] = []
    try:
        with open(home / "gateway.log", "r", encoding="utf-8", errors="replace") as handle:
            # A rotation can leave the file SHORTER than the offset; start over
            # rather than seeking past the end and reading nothing.
            if handle.seek(0, os.SEEK_END) >= file_offset:
                handle.seek(file_offset)
            else:
                handle.seek(0)
            appended = handle.read()
    except OSError:
        appended = ""
    for line in appended.splitlines():
        if pattern.search(line):
            hits.append(f"gateway.log: {line.strip()[:240]}")
    for line in captured.splitlines():
        if line not in seen_lines and pattern.search(line):
            hits.append(f"captured output: {line.strip()[:240]}")
    return hits


def _assistant_count(client: _Client, slot: str) -> int:
    """How many non-empty assistant messages the slot's history holds."""
    detail = client.get(f"/api/chat/slots/{slot}")
    return sum(
        1
        for msg in detail.get("messages", [])
        if msg.get("role") == "assistant" and str(msg.get("content", "")).strip()
    )


def _await_additional_reply(client: _Client, slot: str, baseline: int) -> None:
    """Wait until the slot holds MORE assistant replies than *baseline*.

    Waiting for "an assistant reply" is not enough after a kill: every slot
    already carries the reply from the turn that created its runtime, so a poll
    for the first non-empty assistant message returns immediately and the
    recovery assertion passes even when the gateway never served the new turn.
    The count is what makes the second turn's completion observable.
    """
    deadline = time.monotonic() + REPLY_TIMEOUT
    latest = baseline
    while time.monotonic() < deadline:
        latest = _assistant_count(client, slot)
        if latest > baseline:
            return
        time.sleep(POLL_SECS)
    raise AssertionError(
        f"slot {slot} still holds {latest} assistant repl(ies) after "
        f"{REPLY_TIMEOUT:.0f}s, the same as before the recovery turn was posted: "
        "the session did not serve a turn after the kill"
    )


@contextlib.contextmanager
def _gateway_with_live_sessions(count: int = SESSION_COUNT) -> Iterator[_Live]:
    """A booted gateway with *count* sessions that have each completed a turn.

    Yields the pieces the chaos test needs together, and refuses to yield at all
    when nothing is running: with no live tracked process there is no kill target,
    and the later assertions would pass against an idle machine.
    """
    with measured_gateway() as (handle, client):
        home = Path(handle.home)
        assert_slice_contract(home, _workspace_src())
        sessions = [_open_session_and_take_a_turn(client) for _ in range(count)]
        live = inventory(home, ignore_pids=frozenset({handle.proc.pid, os.getpid()}))
        assert live.owned_alive, (
            "no live tracked process after every session took a turn, so there is "
            f"nothing to kill and nothing below would be evidence.\n{live.render()}\n"
            f"{handle.diagnostics()}"
        )
        yield _Live(handle=handle, client=client, home=home, sessions=sessions, live=live)


def test_killing_a_runtime_root_recovers(real_user_session: Any) -> None:
    """External SIGKILL of a session's backend root: sessions recover, nothing leaks.

    One test rather than one per property: they share a gateway boot plus two
    real turns, and each property is about the SAME kill. Split across tests,
    each would kill a different process on a different machine and none would be
    measuring the recovery the others reported on.
    """
    _require_real_cgroups()
    with _gateway_with_live_sessions() as env:
        ignore = frozenset({env.handle.proc.pid, os.getpid()})
        roots = _runtime_roots(env.home)
        assert roots, (
            "the registry's session file lists no live runtime root, so the kill "
            f"target cannot be chosen.\n{env.live.render()}"
        )
        victim = roots[0]
        before = inventory(env.home, ignore_pids=ignore)
        stubs_before = set(before.stub_pids)
        kin = descendants_of(victim.pid, set(before.live_pids)) - {victim.pid}
        victim_argv = read_argv(victim.pid)
        # Read forward from HERE: the victim's pid is already in the startup log.
        baseline = _log_baseline(env.home, env.handle.diagnostics())

        _kill_verified(victim, "agent runtime root")

        # The victim must actually go. Everything below is about what the gateway
        # did in response, so a surviving victim would make the rest meaningless.
        deadline = time.monotonic() + RECOVER_TIMEOUT
        while pid_alive(victim.pid) and time.monotonic() < deadline:
            time.sleep(POLL_SECS)
        assert not pid_alive(
            victim.pid
        ), f"runtime root {victim.pid} survived SIGKILL: {victim_argv[:160]}"

        left = _await_descendants_gone(kin)
        assert not left, (
            f"killing the runtime root (pid {victim.pid}) left {len(left)} of its "
            f"{len(kin)} descendant(s) running with no owner: "
            + ", ".join(f"{pid} :: {read_argv(pid)[:100]}" for pid in sorted(left))
            + "\n"
            + inventory(env.home, ignore_pids=ignore).render()
        )

        tracked = _await_registry_forgets(env.home, victim.pid)
        assert victim.pid not in tracked, (
            f"the registry still tracks the killed runtime root {victim.pid} after "
            f"{RECOVER_TIMEOUT:.0f}s; anything that later signals by pid can reach a "
            f"recycled number.\n{inventory(env.home, ignore_pids=ignore).render()}"
        )

        recorded = _logs_naming(env.home, victim.pid, env.handle.diagnostics(), baseline)
        assert recorded, (
            f"no log line appended after the kill names the runtime root {victim.pid}, so "
            "the death was absorbed silently. Checked the gateway log file and the "
            f"captured output.\n{inventory(env.home, ignore_pids=ignore).render()}"
        )

        # The point of the whole exercise: the sessions still work. The reply
        # COUNT is snapshotted first, because each slot already holds the reply
        # from the turn that created its runtime.
        baselines = {slot: _assistant_count(env.client, slot) for slot, _ in env.sessions}
        for slot, agent in env.sessions:
            env.client.post(
                "/api/chat?ws=1", {"message": "ping again", "slot": slot, "agent": agent}
            )
        for slot, _agent in env.sessions:
            _await_additional_reply(env.client, slot, baselines[slot])

        # A stub that was alive before the kill must not be left unowned by it.
        # This fixture declares no MCP server, so the population is normally
        # empty; the assertion is what makes a fixture that DOES declare one
        # cover the stranded-stub case without another test.
        after = inventory(env.home, ignore_pids=ignore)
        stranded = {fact.pid for fact in after.unowned_alive if fact.pid in stubs_before}
        assert not stranded, (
            f"{len(stranded)} managed MCP stub(s) that the killed runtime owned are "
            f"alive and now in no registry entry.\n{after.render()}"
        )
