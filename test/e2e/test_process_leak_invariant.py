"""After N sessions open and close, what is still on this machine? Only the gateway.

This module is the leak gate. It boots ONE real gateway on a throwaway data
home, drives ``SESSION_COUNT`` dashboard chat sessions through a full turn each,
closes every one of them, and then asks the kernel and the product's own
registry what survived. Four populations must be empty:

* live pids inside the instance's own agent slice that any SESSION created --
  the gateway's own background runtimes (``EXPECTED_BACKGROUND_RUNTIMES``) are
  the one documented residue, bounded by count and identified by the agent they
  run, never by argv shape or age,
* managed MCP stub processes,
* registry entries naming a process that is gone,
* crew-log write handles held by the gateway process.

Why a real gateway subprocess and not the in-process boot in
``test/integration/``: two of the four assertions are about the gateway PROCESS
itself. ``/proc/<gateway pid>/fd`` is only the gateway's fd table when the
gateway is its own process, and an agent slice is only this instance's when the
instance has its own data home. Booted in-process, the "gateway pid" is pytest,
whose fd table and cgroup belong to the test runner.

The measurement is a RECONCILIATION, never a single count -- see
``e2e.process_inventory``. A leak shows up as a live process nothing owns; a
stale registry entry shows up as an owner whose process is gone. The two need
opposite fixes, so they are reported separately.

The live peak is asserted BEFORE the teardown, and that is the load-bearing
part of this file. "Nothing survived" is also what a run produces when nothing
ever started -- a gateway whose turns silently failed, a slice this harness
looked for in the wrong place, a fixture that spawns no agent at all. Without
the peak assertion the whole module is green on an empty machine, which is the
exact false green the gate exists to prevent.

Gating, and why there are two switches
--------------------------------------
* ``KIROCREW_E2E`` lifts the module skip. A bare ``pytest`` run does not pay a
  gateway boot plus five model turns.
* ``KIROCREW_E2E_REQUIRE`` turns an unmet PRECONDITION from a graceful
  ``pytest.skip`` into a ``pytest.fail``. pytest counts a skip as a pass, so a
  required job could otherwise report green having measured nothing. Shared
  with the other E2E gates rather than spelled per-module; see
  ``docs/ci/e2e-gate.md``.

The hard precondition is a usable ``systemd --user`` scope backend. Without it
the product spawns agents unwrapped, no per-instance slice directory is ever
created, and every population reads empty for want of somewhere to look. That
is reported as unresolved -- never as a pass. No substitute is accepted: the
whole value of this layer is real processes in a real cgroup.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest
from e2e.process_inventory import (
    Inventory,
    SliceCleanupError,
    assert_slice_contract,
    crew_log_write_handles,
    inventory,
    scope_backend_usable,
    stop_instance_slice,
)
from e2e.test_gateway_boot_matrix import (
    _await_assistant_reply,
    _booted,
    _Client,
    _unresolved,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Real-process leak invariant. Set KIROCREW_E2E=1 to run.",
)

#: How many dashboard chat sessions the invariant is measured over. Five rather
#: than one because a leak is frequently a per-session remainder, and a single
#: session cannot tell "one runtime survived" from "one runtime never died".
SESSION_COUNT = 5

#: Per-turn ceiling for the reply poll. Generous on purpose: it only matters
#: when the turn is already broken, and it must stay well inside the job's
#: ``--timeout`` so a stuck turn fails by name here instead of killing the run.
REPLY_TIMEOUT = 90.0

#: How long every population gets to reach zero after the last slot is deleted.
#: Teardown is asynchronous -- the close path retires the runtime, the registry
#: write-back follows, and a reaper pass may be what finally clears a scope --
#: so this is polled to a deadline rather than slept through. A generous bound
#: costs nothing on a healthy run because the poll returns as soon as it is
#: quiet, and it is what keeps a slow runner from reading as a leak.
QUIET_TIMEOUT = 120.0

#: Gap between quiet polls.
QUIET_POLL_SECS = 1.0

#: The warm pool must be OFF for the bar to be zero: a gateway configured to
#: keep spare runtimes is SUPPOSED to hold processes after a slot closes. The
#: shipped default is 0 and the seed fixture does not set it; this is asserted
#: rather than assumed so turning it on fails by name instead of quietly making
#: the invariant wrong.
EXPECTED_POOL_SIZE = 0

#: How many runtimes the GATEWAY itself may still hold after every slot is
#: closed. These are not session residue: the persistent ``_bg`` background
#: session is created at boot by ``start_pool()`` and is never idle-expired, and
#: the multiplexed ``_bg_runtime`` behind ``get_bg_session()`` (auto-titles,
#: suggestions) is spawned lazily and retired only by staleness, a backend
#: switch, or ``close_all()`` -- see ``docs/system-specs/modules/session.md``,
#: "Background Session" and "Multiplexed _bg runtime". Both run the background
#: agent (``session.BACKGROUND_AGENT``) and neither belongs to a slot, so a slot
#: close is not supposed to end them. EXACTLY one of each, pinned as an equality
#: so the ratchet cannot loosen in either direction: a third background-agent
#: runtime is a parked drain that never drained, or a leak, and one missing means
#: the gateway lost a holder it depends on (every slot's first turn generates a
#: title, so the lazy one is always spawned by the time the slots close).
#: Anything running another agent is session residue and fails regardless of
#: count.
EXPECTED_BACKGROUND_RUNTIMES = 2


def _authored_agent(home: Path, argv: str) -> str:
    """The agent a runtime was spawned FOR, from the ``--agent`` it was spawned with.

    The product hands the backend a projected skill-view alias
    (``kirocrew-skill-view-<hash>``), not the authored name, and the same
    authored agent yields the same alias, so the alias alone cannot say whether
    a process is the background agent. The projection writes a sidecar per
    alias into the agents directory the harness pins (``KIRO_HOME`` is
    ``<home>/kiro`` -- ``harness_environment``), and that sidecar names the
    authored agent. An un-aliased ``--agent`` is returned as written; a runtime
    with no ``--agent`` at all, or an alias with no sidecar, answers ``""`` so
    the caller classifies it as foreign rather than guessing.
    """
    from kiro_crew.acp.skill_projection import _MANAGED_AGENT, _PROJECTION_METADATA_DIR_NAME
    from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX

    words = argv.split()
    try:
        agent = words[words.index("--agent") + 1]
    except (ValueError, IndexError):
        return ""
    if not agent.startswith(NATIVE_SKILL_ALIAS_PREFIX):
        return agent
    sidecar = home / "kiro" / "agents" / _PROJECTION_METADATA_DIR_NAME / f"{agent}.json"
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    authored = record.get(_MANAGED_AGENT) if isinstance(record, dict) else None
    return authored if isinstance(authored, str) else ""


def _split_background_runtimes(home: Path, alive: tuple) -> tuple[list, list]:
    """Partition live tracked processes into ``(background, foreign)``.

    ``background`` runs the product's own background agent and is the residue
    the gateway is documented to keep; ``foreign`` is everything else, which
    after every slot is closed can only be a session's runtime that outlived it.
    """
    from kiro_crew.session import BACKGROUND_AGENT

    background = [f for f in alive if _authored_agent(home, f.argv) == BACKGROUND_AGENT]
    foreign = [f for f in alive if f not in background]
    return background, foreign


def _workspace_src() -> Path:
    """The in-repo ``src/`` the harness runs the product from."""
    from kiro_crew.testing.harness import _resolve_workspace_src

    return _resolve_workspace_src()


def _require_real_cgroups() -> None:
    """Refuse to measure without a scope backend that actually works.

    Call this INSIDE a test that has already requested ``real_user_session``:
    the suite runs with the session-bus locators stripped from the environment,
    so the probe would answer "no user session" for every test that had not
    opted back in, and the whole layer would be permanently unresolved.
    """
    ok, why = scope_backend_usable()
    if not ok:
        _unresolved(
            "this host cannot place a spawned agent in a systemd --user scope, so "
            "no per-instance agent slice is created and the leak populations "
            f"would all read empty for want of somewhere to look: {why or 'no reason given'}"
        )


def _delete(client: _Client, path: str) -> dict[str, Any]:
    """DELETE one path through the client's primed cookie jar.

    The shared client covers GET and POST; closing a slot is the one verb this
    module needs beyond them, and it goes through the same opener so it carries
    the same session cookie.
    """
    req = urllib.request.Request(f"http://localhost:{client._port}{path}", method="DELETE")
    return client._open(req, timeout=60)


def _seeded_pool_size(home: Path) -> int:
    path = home / "config.json"
    if not path.is_file():
        return EXPECTED_POOL_SIZE
    data = json.loads(path.read_text(encoding="utf-8"))
    session = data.get("session") or {}
    return int(session.get("pool_size", EXPECTED_POOL_SIZE))


def _open_session_and_take_a_turn(client: _Client) -> tuple[str, str]:
    """Create one chat slot, drive a full turn on it, return ``(slot, agent)``.

    A slot that never took a turn spawns no runtime, so the turn is what makes
    the session count as load. The reply is awaited rather than assumed for the
    same reason: an accepted POST proves the gateway queued the message, not
    that a backend process came up.

    The agent is returned alongside the key because a later turn on the same
    slot has to name it, and the value is the gateway's choice rather than
    something a caller can reconstruct.
    """
    created = client.post("/api/chat/slots", {})
    slot = created["key"]
    agent = created["agent"]
    assert slot, f"slot creation returned no key: {created!r}"
    assert isinstance(agent, str) and agent.strip(), f"no agent in {created!r}"
    client.post("/api/chat?ws=1", {"message": "ping", "slot": slot, "agent": agent})
    _await_assistant_reply(client, slot, timeout=REPLY_TIMEOUT)
    return str(slot), str(agent)


def _await_quiet(home: Path, ignore: frozenset[int]) -> Inventory:
    """Poll the inventory until nothing but the gateway's background runtimes
    remain, or the deadline.

    "Quiet" is: no dead registry entry, no unowned live process, and every live
    tracked process is a background-agent runtime (see
    ``EXPECTED_BACKGROUND_RUNTIMES``). Those runtimes are the gateway's for its
    whole life, so waiting for them to leave would spend the full deadline on
    every healthy run and then report a leak that is not one.

    Returns the LAST inventory taken either way, so a caller that timed out
    reports what was still there rather than re-reading a machine that may have
    settled in the meantime.
    """
    deadline = time.monotonic() + QUIET_TIMEOUT
    latest = inventory(home, ignore_pids=ignore)
    while time.monotonic() < deadline:
        _background, foreign = _split_background_runtimes(home, latest.owned_alive)
        if not (foreign or latest.owned_dead or latest.unowned_alive):
            return latest
        time.sleep(QUIET_POLL_SECS)
        latest = inventory(home, ignore_pids=ignore)
    return latest


@contextlib.contextmanager
def measured_gateway(fixture: str = "minimal") -> Iterator[tuple[Any, _Client]]:
    """A booted gateway whose per-instance agent slice is stopped afterwards.

    Every boot here runs on a fresh data home, so it derives a slice name no
    other run uses -- and systemd keeps a slice loaded once created until someone
    stops it. Unstopped, this harness would add one unit per run to the
    operator's user manager forever, which is the very shape of residue it
    exists to report.

    A failed stop FAILS the test, because the unit it left behind is permanent
    and nothing retries it. It is raised only when the body itself succeeded: a
    test that already failed has a more informative error, and replacing it with
    a cleanup complaint would hide the finding. When both go wrong the cleanup
    failure is attached to the original rather than replacing it.

    The suite strips the session-bus locators process-wide, so a caller must
    also request the ``real_user_session`` fixture; without it the spawn finds
    no bus, nothing is placed in a scope, and there is no slice to stop.
    """
    with _booted(fixture) as (handle, client):
        home = Path(handle.home)
        body_failed = False
        try:
            yield handle, client
        except BaseException as exc:
            body_failed = True
            try:
                stop_instance_slice(home)
            except SliceCleanupError as cleanup_exc:
                exc.__notes__ = [  # type: ignore[attr-defined]
                    *getattr(exc, "__notes__", []),
                    f"harness cleanup also failed: {cleanup_exc}",
                ]
            raise
        finally:
            if not body_failed:
                stop_instance_slice(home)


def test_closing_every_session_leaves_nothing(real_user_session: Any) -> None:
    """Open ``SESSION_COUNT`` sessions, close them, and assert the machine is clean.

    The four populations and the peak control are all asserted here rather than
    split across tests because they share one expensive setup: a gateway boot
    plus five real turns. Splitting them would boot a gateway per assertion and
    measure a different machine each time.

    ``real_user_session`` hands this test the operator's systemd user manager,
    which the suite otherwise withholds from every test; it skips where the host
    has none.
    """
    _require_real_cgroups()
    with measured_gateway() as (handle, client):
        home = Path(handle.home)
        src = _workspace_src()
        # Prove the harness and the product agree on which slice to read before
        # any number from it is believed.
        assert_slice_contract(home, src)
        pool_size = _seeded_pool_size(home)
        assert pool_size == EXPECTED_POOL_SIZE, (
            f"the booted config sets session.pool_size={pool_size}; a warm pool is "
            "supposed to retain runtimes after a slot closes, so the zero-survivor "
            "bar below does not apply. Re-point this test at a fixture with the "
            "pool off, or teach it the pool's expected residue."
        )

        # The gateway itself is not agent work, and neither is the test runner.
        ignore = frozenset({handle.proc.pid, os.getpid()})

        opened = [_open_session_and_take_a_turn(client) for _ in range(SESSION_COUNT)]
        slots = [slot for slot, _agent in opened]
        assert len(set(slots)) == SESSION_COUNT, f"slots were not distinct: {slots!r}"

        peak = inventory(home, ignore_pids=ignore)
        # THE CONTROL. Everything below is an assertion that a population is
        # empty, and an empty population is also what a machine that never ran
        # anything reports. This is the line that proves the run had load.
        assert peak.owned_alive, (
            f"{SESSION_COUNT} sessions each completed a turn, yet the instance's "
            "agent slice and registry show no live tracked process. The harness is "
            "measuring the wrong place, or the turns were served without spawning "
            f"a backend -- either way nothing below would be evidence.\n{peak.render()}\n"
            f"{handle.diagnostics()}"
        )
        assert peak.slice_dir is not None, (
            "the instance owns no agent slice directory even though its sessions "
            f"ran, so the kernel half of the reconciliation is blind.\n{peak.render()}"
        )

        for slot in slots:
            _delete(client, f"/api/chat/slots/{slot}")

        final = _await_quiet(home, ignore)

        assert not final.unowned_alive, (
            f"{len(final.unowned_alive)} process(es) are alive in this instance's "
            "agent slice with no registry entry, so nothing owns them and nothing "
            f"will ever reclaim them.\n{final.render()}"
        )
        background, foreign = _split_background_runtimes(home, final.owned_alive)
        assert not foreign, (
            f"{len(foreign)} tracked process(es) that are not the gateway's own "
            "background runtimes survived closing every session: "
            + ", ".join(
                f"pid={f.pid} agent={_authored_agent(home, f.argv) or '?'}" for f in foreign
            )
            + f"\n{final.render()}"
        )
        assert len(background) == EXPECTED_BACKGROUND_RUNTIMES, (
            f"{len(background)} background-agent runtime(s) alive after every session "
            f"closed, expected exactly {EXPECTED_BACKGROUND_RUNTIMES}: the persistent _bg "
            "session and one multiplexed _bg runtime. More means displaced runtimes "
            "nothing reaped; fewer means the gateway lost a holder it still depends on."
            f"\n{final.render()}"
        )
        assert not final.owned_dead, (
            f"{len(final.owned_dead)} registry entr(ies) still name a process that "
            "is gone; the registry has not forgotten them, and anything that later "
            f"signals by pid can reach a recycled number.\n{final.render()}"
        )
        assert not final.stub_pids, (
            f"{len(final.stub_pids)} managed MCP stub process(es) outlived every "
            f"session that could have wanted one.\n{final.render()}"
        )

        handles = crew_log_write_handles(handle.proc.pid)
        assert not handles, (
            "the gateway process still holds the crew log open after every session "
            f"closed: {handles}\n{final.render()}"
        )


def test_crew_log_handle_probe_detects_a_real_handle(tmp_path: Path) -> None:
    """The fd probe finds a crew-log handle when one exists.

    Without this, ``test_closing_every_session_leaves_nothing``'s crew-log
    assertion is unfalsifiable: a probe that looks at the wrong directory, or
    matches a path token the product does not use, reports "no handles" forever
    and the assertion passes by measuring nothing. So hold one open deliberately
    and require the probe to name it.

    Runs without the E2E gateway because it tests the probe, not the product.
    """
    if sys.platform != "linux":
        pytest.skip("the probe reads /proc/<pid>/fd, which is Linux-only")
    log_dir = tmp_path / "crew-log"
    log_dir.mkdir()
    target = log_dir / "segment.jsonl"
    target.write_text("", encoding="utf-8")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "handle = open(sys.argv[1], 'ab')\n"
            "sys.stdout.write('open\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n",
            str(target),
        ],
        # Never the caller's CWD: a child that writes by a relative path must
        # not leave the file in the repository checkout.
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == b"open", "holder never opened the file"
        found = crew_log_write_handles(holder.pid)
        assert found, (
            "the probe found no crew-log handle on a process that is holding one "
            f"open at {target}; the path tokens it matches on are out of date"
        )
        assert any(
            "segment.jsonl" in entry for entry in found
        ), f"probe returned {found}, none of which names the held file"
    finally:
        holder.kill()
        holder.wait(timeout=30)

    # And the other direction: once the holder is gone the probe reports nothing,
    # so a positive result cannot be an artifact of the probe itself.
    assert not crew_log_write_handles(holder.pid)
