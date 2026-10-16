"""The ``kirocrew-worker`` agent: the bracket around a load this process cannot lock.

What the gate returns and the post-load check that closes it, a benign concurrent
write that re-derives and respawns once while a revocation still ends the session,
and the array-backed hosts, whose array is the load and carries its snapshot.
Shared pins and spec shapes come from ``worker_agent_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from worker_agent_helpers import _accepting_kiro_cli  # noqa: F401
from worker_agent_helpers import _DEFAULT_SPEC_ON_DISK, _package_sources, _stale_worker_spec

from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME, WORKER_AGENT_FILENAME

# One xdist worker for the whole module: the enumeration gate below streams src/
# (~1,700 files) through ``test/source_corpus.py``, which memoises only the file list.
# Grouping keeps that list single-copy per run and the gate on one worker, so its
# reads do not compete with three other workers re-walking the same tree.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_worker_agent_load_bracket")


# ── the bracket around a load this process cannot lock ──────────────────────


def test_the_gate_returns_what_it_verified(tmp_path, monkeypatch):
    """The capture half. A caller that has to prove freshness STILL holds after another
    process has read the spec needs to know what was checked, and `None` (nothing mirrors
    anything) has to stay distinguishable from a verdict."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert snap is not None
    assert snap.identity == agent.default_spec_identity()
    assert snap.fingerprint == agent.default_spec_fingerprint()
    assert agent.require_fresh_derived_spec("kirocrew", None) is None


def test_a_default_changed_during_the_load_ends_the_session(tmp_path, monkeypatch):
    """The window the pair closes: the pre-check passes, then a revocation lands, and
    kiro-cli — another process, some milliseconds later — reads the pre-revocation spec.
    Holding a writer lock across a subprocess's read is not available, so the window is
    closed by DETECTION instead."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("builder-mcp")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_unchanged_derived_spec(snap)
    assert "changed during worker load" in str(excinfo.value)
    # Fingerprints are reported as short hashes, not as spec contents.
    assert snap.fingerprint[:12] in str(excinfo.value)
    assert "builder-mcp" not in str(excinfo.value)


def test_an_unchanged_default_passes_the_post_load_check(tmp_path, monkeypatch):
    """The ordinary case, which must cost nothing and say nothing."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    agent.require_unchanged_derived_spec(snap)


def test_a_post_load_check_that_cannot_read_ends_the_session(tmp_path, monkeypatch):
    """Fail closed on the re-check itself: an unreadable default here is not "probably
    fine", it is the one state in which this function cannot do its job, and the session
    it guards is already running on a spec it cannot vouch for."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    (tmp_path / AGENT_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_unchanged_derived_spec(snap)
    assert "became unreadable" in str(excinfo.value)


def test_the_post_load_check_is_not_applicable_without_a_snapshot():
    """`None` is "the bracket does not apply", not "satisfied" — the pre-check answers it
    for every agent that mirrors nothing, and the post-check must not invent a verdict."""
    agent.require_unchanged_derived_spec(None)


def test_the_runtime_closes_the_bracket_after_the_handshake():
    """Placement is the whole mechanism: the check has to sit after the point kiro-cli has
    provably READ its spec — the `initialize` response — and inside the guard that kills
    the process, since a session that may have loaded an unverified spec must not
    survive."""
    import inspect

    from kiro_crew.acp import runtime

    src = inspect.getsource(runtime.AcpRuntime)
    assert "require_unchanged_derived_spec" in src
    before, after = src.split("require_unchanged_derived_spec", 1)
    # The handshake precedes it.
    assert '"initialize"' in before
    # And the kill-and-re-raise guard encloses it.
    assert "except BaseException:" in after
    assert "failed init handshake cleanup" in after


def test_the_snapshot_is_a_coherent_pair_from_one_observation(tmp_path, monkeypatch):
    """The defect a two-observation snapshot has: an identity from a fresh stat beside a
    fingerprint read out of the sidecar is a TORN pair -- the NEW identity carrying the
    OLD content's hash -- and the post-load check then accepts the new default while the
    subprocess loaded the old spec. Here the default moves during the verification, and the
    pair that comes back describes ONE generation of the file, whichever one it settled on."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")

    real_read = agent._installed_default_spec
    moved: list[bool] = []

    def _read_then_move():
        spec = real_read()
        if not moved:
            # The write lands between the identity stat and the fingerprint's own read --
            # exactly the instant a two-observation pair tears at.
            moved.append(True)
            default.write_text(json.dumps(edited), encoding="utf-8")
        return spec

    monkeypatch.setattr(agent, "_installed_default_spec", _read_then_move)
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert moved, "the reader never fired, so this test proved nothing"
    assert snap is not None

    # COHERENT: the identity and the fingerprint describe the same generation. Asserted
    # against the file as it now stands rather than against either expected value, so the
    # test states the property instead of the outcome.
    monkeypatch.setattr(agent, "_installed_default_spec", real_read)
    assert (snap.identity, snap.fingerprint) == (
        agent.default_spec_identity(),
        agent.default_spec_fingerprint(),
    ), "the snapshot pairs an identity with a fingerprint from a different generation"

    # And the bracket still does its job: a change after the observation ends the session.
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale):
        agent.require_unchanged_derived_spec(snap)


def test_the_snapshot_never_takes_its_fingerprint_from_the_sidecar():
    """The sidecar is bookkeeping for the fast path that avoids a RE-DERIVE, not evidence
    about the file as it stands now. Read into the snapshot it is a second observation, so
    the gate hashes the bytes it read instead -- one hash of a small file, on a path that
    is already spawning a process."""
    import inspect

    src = inspect.getsource(agent.require_fresh_derived_spec)
    assert (
        "get_mirrored_from" not in src
    ), "the snapshot's fingerprint must be of the bytes the gate read, not a recorded value"
    assert "_surface_fingerprint(" in src


def test_a_default_spec_that_never_settles_refuses_the_spawn(tmp_path, monkeypatch):
    """Bounded, and fail-closed at the bound. The loop's exit condition is another process
    leaving the file alone, so unbounded it would spin on a host rewriting the spec in a
    loop -- and a spawn that never returns is worse than one that refuses."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    stats: list[int] = []

    def _never_the_same():
        stats.append(len(stats))
        return f"moving-{len(stats)}"

    monkeypatch.setattr(agent, "default_spec_identity", _never_the_same)
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "kept changing" in str(excinfo.value)
    assert str(agent._DEFAULT_SPEC_OBSERVATION_ATTEMPTS) in str(excinfo.value)
    # It gave up rather than looping forever.
    assert len(stats) <= agent._DEFAULT_SPEC_OBSERVATION_ATTEMPTS * 4


def test_the_derive_time_pair_is_also_one_observation(tmp_path, monkeypatch):
    """The same property at the WRITE end, because a torn pair recorded here is what a
    later identity match would accept: the fingerprint is of the bytes this derivation
    mirrored, and the identity is recorded only when a re-stat proves the file held still."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")
    mirrored_bytes = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))

    real_read = agent._installed_default_spec

    def _read_then_move():
        spec = real_read()
        default.write_text(json.dumps(edited), encoding="utf-8")
        return spec

    monkeypatch.setattr(agent, "_installed_default_spec", _read_then_move)
    agent._install_worker_agent()

    # The fingerprint names what was actually mirrored, not what the file now holds.
    assert agent_state.get_mirrored_from("kirocrew-worker") == agent._spec_fingerprint(
        mirrored_bytes
    )
    # And no identity is claimed at all, because none of them describes those bytes.
    assert agent_state.get_mirrored_stat("kirocrew-worker") is None

    # Which leaves the next check in the pessimistic direction: no fast path, and the
    # truthful fingerprint does not match the file, so the mirror is re-derived.
    monkeypatch.setattr(agent, "_installed_default_spec", real_read)
    assert agent._derived_spec_matches_default("kirocrew-worker") is False


def test_the_identity_fast_path_re_stats_after_reading_the_sidecar(tmp_path, monkeypatch):
    """Reading the sidecar is itself a window. The recorded value is evidence about the file
    the FIRST stat described, so without a second stat the fast path can answer "provably
    current" about a default that has already been replaced."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    real_identity = agent.default_spec_identity
    calls: list[str | None] = []

    def _counting():
        answer = real_identity()
        calls.append(answer)
        return answer

    monkeypatch.setattr(agent, "default_spec_identity", _counting)
    assert agent._derived_spec_matches_default("kirocrew-worker") is True
    assert len(calls) == 2, f"the fast path must stat before AND after the sidecar read: {calls}"

    # And a replacement landing during that read is caught rather than accepted.
    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")
    recorded = agent_state.get_mirrored_stat("kirocrew-worker")

    def _read_sidecar_then_move(name):
        default.write_text(json.dumps(edited), encoding="utf-8")
        return recorded

    monkeypatch.setattr(agent, "default_spec_identity", real_identity)
    monkeypatch.setattr(agent_state, "get_mirrored_stat", _read_sidecar_then_move)
    assert agent._derived_spec_matches_default("kirocrew-worker") is False


def test_the_client_closes_the_bracket_after_the_handshake(tmp_path):
    """The second subprocess spawner, and the same placement rule: the AcpClient captures
    the snapshot in ``_spawn`` and must re-verify it in ``_initialize_session`` after the
    ``initialize`` response and BEFORE any session is created, so a session is never
    built on a spec nobody verified."""
    import inspect
    from unittest.mock import patch

    import acp_launch_capture as capture_mod

    from kiro_crew.acp import client
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO

    # The capture half, observed on a real kiro-cli launch: what the gate returned
    # is what the spawn left on the client for the handshake to verify.
    snapshot = object()
    left: list[object] = []
    capture_mod.capture(
        ACP_BACKEND_KIRO,
        tmp_path,
        extra_patches=(patch.object(client, "require_fresh_derived_spec", return_value=snapshot),),
        observe=lambda spawned: left.append(spawned._derived_spec_snapshot),
    )
    assert left == [
        snapshot
    ], "the capture half must stay in _spawn -- it is the window's opening edge"

    src = inspect.getsource(client.AcpClient._initialize_session)
    assert (
        "require_unchanged_derived_spec" in src
    ), "the client spawner drives the handshake, so it owns the post-load half"
    before, after = src.split("require_unchanged_derived_spec", 1)
    # The handshake precedes it: the check is worthless before the child has read.
    assert "METHOD_INITIALIZE" in before
    assert "init_resp" in before
    # And no session exists yet when it runs: the only requests above the check are the
    # initialize round trip, and the session id is not recorded until after it.
    assert "METHOD_SESSION_LOAD" not in before
    assert "METHOD_SESSION_LOAD" in after
    assert "self._session_id" not in before
    assert "self._session_id" in after


def test_every_handshake_driving_spawner_closes_the_bracket():
    """Enumeration rather than a list of two files: a module that runs the freshness gate
    AND drives an ``initialize`` of its own is a subprocess spawner, and every one of
    those has the verified-then-changed window. A third one added tomorrow fails here
    instead of shipping the capture half alone."""
    import re as _re

    spawners = []
    closed = []
    for path, body in _package_sources():
        if "require_fresh_derived_spec" not in body:
            continue
        # Drives its own handshake: sends the `initialize` method, however the method
        # name is spelled at that site (a constant in the client, a literal in the
        # runtime).
        if not _re.search(r'METHOD_INITIALIZE|"initialize"', body):
            continue
        spawners.append(path.name)
        if "require_unchanged_derived_spec" in body:
            closed.append(path.name)

    assert sorted(spawners) == [
        "client.py",
        "runtime.py",
    ], f"the set of handshake-driving spawn paths changed: {spawners}"
    assert sorted(closed) == sorted(spawners), (
        f"spawn path(s) capture the snapshot but never close the bracket: "
        f"{sorted(set(spawners) - set(closed))}"
    )


def _client_at_the_handshake(snapshot):
    """An AcpClient positioned exactly at the post-load check, and the methods it sent.

    Built without ``__init__`` on purpose: the property-backed ``backend`` answers ""
    for an unbuilt instance, and everything ``_initialize_session`` touches before the
    check is either set by the handshake itself or supplied here. Anything the check
    were moved BELOW would need attributes this stub does not have, so a test that
    passes here is a test that ran the check early.
    """
    from kiro_crew.acp import client as client_mod

    c = client_mod.AcpClient.__new__(client_mod.AcpClient)
    c._derived_spec_snapshot = snapshot
    sent: list[str] = []

    async def _send_request(method, params):
        sent.append(method)
        return 1

    async def _wait_for_response(req_id, **kwargs):
        return {
            "protocolVersion": 1,
            "agentCapabilities": {},
            "agentInfo": {"name": "kiro", "version": "0.0.0"},
        }

    c._send_request = _send_request  # type: ignore[method-assign]
    c._wait_for_response = _wait_for_response  # type: ignore[method-assign]
    return c, sent


def test_the_client_bracket_aborts_before_a_session_is_created(tmp_path, monkeypatch):
    """The window, driven through the real handshake method: the pre-check passed, the
    default spec then changed, and the child has already read whatever kiro-cli read.
    The refusal must reach the caller as ``AcpError`` -- the type ``ensure_ready``
    handles, and therefore the one that kills the child and drops the half-registered
    session state -- with no session/load or session/new sent."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp import client as client_mod

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("builder-mcp")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    c, sent = _client_at_the_handshake(snap)
    with pytest.raises(client_mod.AcpError) as excinfo:
        asyncio.run(c._initialize_session())
    assert "changed during worker load" in str(excinfo.value)
    assert not isinstance(excinfo.value, agent.DerivedSpecStale), (
        "a raw DerivedSpecStale matches none of ensure_ready's handlers, so the child "
        "would stay alive on an unverified spec"
    )
    assert sent == ["initialize"], f"a session was created on an unverified spec: {sent}"


def test_the_client_bracket_judges_the_snapshot_its_own_spawn_captured(monkeypatch):
    """The two halves have to be about the same child. The object the check receives is
    the one ``_spawn`` stored on the instance -- not a fresh capture taken here, which
    would compare the spec against itself and always pass."""
    import asyncio

    from kiro_crew.acp import client as client_mod

    class _Reached(Exception):
        pass

    seen: list[object] = []

    def _record(snapshot, **kwargs):
        seen.append(snapshot)
        raise _Reached

    monkeypatch.setattr(client_mod, "require_unchanged_derived_spec", _record)

    sentinel = object()
    c, sent = _client_at_the_handshake(sentinel)
    with pytest.raises(_Reached):
        asyncio.run(c._initialize_session())
    assert seen and seen[0] is sentinel
    assert sent == ["initialize"]


def test_every_set_mode_send_goes_through_the_one_bracketed_helper():
    """A ``set_mode`` naming an agent is a load of that agent's spec, so it needs the
    same bracket the spawn has -- and there is exactly ONE body that provides it. Both
    session-start paths (create and resume) reach it; no body on either path sends the
    mode method itself. A protocol written twice is two protocols the moment one copy is
    edited, so the pin is on the single helper plus the rule that nothing else sends the
    method. Activation may sit in a body the entry point hands off to -- ``create_session``
    finishes in ``_finish_create_session``, which the late-adoption collector calls too --
    so the walk FOLLOWS the ``self`` calls instead of naming one method."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import runtime as runtime_mod

    def _tree(fn):
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))

    def _handoffs(fn):
        return {
            n.func.attr
            for n in ast.walk(_tree(fn))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        }

    def _sends_the_mode(fn):
        return any(
            isinstance(n, ast.Call)
            and any(isinstance(a, ast.Name) and a.id == "METHOD_SET_MODE" for a in n.args)
            for n in ast.walk(_tree(fn))
        )

    helper = runtime_mod.AcpRuntime._activate_mode_bracketed
    for entry in (runtime_mod.AcpRuntime.create_session, runtime_mod.AcpRuntime.load_session):
        on_path: set[str] = set()
        pending = [entry.__name__]
        while pending:
            name = pending.pop()
            fn = getattr(runtime_mod.AcpRuntime, name, None)
            if name in on_path or not inspect.isfunction(fn):
                continue
            on_path.add(name)
            pending.extend(_handoffs(fn))
        assert helper.__name__ in on_path, f"{entry.__qualname__} does not activate via the helper"
        senders_on_path = sorted(
            n for n in on_path if _sends_the_mode(getattr(runtime_mod.AcpRuntime, n))
        )
        assert senders_on_path == [
            helper.__name__
        ], f"{entry.__qualname__} sends set_mode outside the helper: {senders_on_path}"

    # The whole module SENDS the method from exactly one place: the helper. The
    # transport's own method-name table (a dict literal mapping the constant to a
    # log label) names it too, so the walk counts CALLS that carry the constant as an
    # argument, not every mention.
    module_tree = ast.parse(inspect.getsource(runtime_mod))
    senders = []
    for node in ast.walk(module_tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for stmt in node.body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call) and any(
                    isinstance(arg, ast.Name) and arg.id == "METHOD_SET_MODE" for arg in n.args
                ):
                    senders.append(node.name)
    assert senders == [helper.__name__], senders


def test_the_bracketed_helper_orders_gate_send_and_check_by_consumption_point():
    """Inside the one helper: exactly ONE snapshot per consumed load, and WHERE the spec
    is consumed decides which snapshot the post-check may use.

    Wire-registered (KAS): consumed at ``session/new`` in the payload the projection
    built under its own gate, so the helper takes the PAYLOAD's snapshot and calls no
    gate -- a fresh read would judge the file while the host holds the payload, and a
    revocation between build and activation would pass it. Disk-read (kiro): consumed at
    ``set_mode`` itself, so the helper gates BEFORE the send. The post-check follows the
    send on both paths, by statement position rather than by mention."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import runtime as runtime_mod

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(runtime_mod.AcpRuntime._activate_mode_bracketed))
    )
    fn = tree.body[0]
    body = [s for s in fn.body if not isinstance(s, (ast.Import, ast.ImportFrom, ast.Expr))]

    def _names(stmts):
        return {n.id for s in stmts for n in ast.walk(s) if isinstance(n, ast.Name)}

    split = [s for s in body if isinstance(s, ast.If) and "wire_registered" in _names([s.test])]
    assert len(split) == 1, "the helper must branch on where the spec was consumed"
    wire, own = split[0].body, split[0].orelse
    assert "payload_snapshot" in _names(wire)
    assert "require_fresh_derived_spec" not in _names(
        wire
    ), "a second gate call for one consumed load is the defect, not a safeguard"
    assert "require_fresh_derived_spec" in _names(own), "the disk path consumes at set_mode"

    def _at(target):
        for i, s in enumerate(body):
            if target in _names([s]):
                return i
        raise AssertionError(f"{target} not found")

    assert _at("wire_registered") < _at("METHOD_SET_MODE") < _at("require_unchanged_derived_spec")
    assert isinstance(body[-1], ast.Return)
    assert _names([body[-1]]) == {"sent_alias"}


def test_a_revocation_between_the_payload_build_and_activation_is_caught(tmp_path, monkeypatch):
    """The window the payload snapshot closes. The definition is registered from spec A, a
    revocation lands, and ``set_mode`` then activates the registered A. A fresh read at
    activation sees B and passes -- so the session runs the grants the revocation removed.
    Comparing against the payload's own snapshot catches it."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.harness import SessionExtras
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    revoked = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    revoked["mcpServers"].pop("builder-mcp")

    async def _payload(
        agent_name,
        *,
        member_dispatch=False,
        crew_panel=False,
        session_key="",
        session_work_dir=None,
    ):
        # The projection's gate, then the payload built from what it verified -- spec A.
        snapshot = agent.require_fresh_derived_spec(agent_name, str(tmp_path / "wd"))
        # The revocation lands AFTER the definition is registered and BEFORE activation.
        default.write_text(json.dumps(revoked), encoding="utf-8")
        return SessionExtras(custom_agents=[{"id": agent_name}], derived_spec_snapshot=snapshot)

    rt._kas_custom_agents = _payload  # type: ignore[method-assign]

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "the activation must have been attempted"
    assert terminated == ["sid-1"], "a session holding a revoked definition must end"
    assert "changed during worker load" in str(excinfo.value)
    # The refusal names the generation the PAYLOAD was built from, which is the whole
    # point: a fresh read at activation would have matched the file and said nothing.
    assert agent.default_spec_fingerprint() not in str(excinfo.value)


# ── a benign concurrent write re-derives and respawns once; a revocation still ends it ──


def _runtime_for_spawn(monkeypatch, attempts_outcomes):
    """A runtime whose ``_spawn_admitted`` plays back the given outcomes in order.

    A ``DerivedSpecStale`` outcome leaves the state the real failed-start guard
    leaves for the post-handshake refusal -- its reap is a ``kill()``, so the child
    is gone and the runtime marked dead -- and an attempt on a runtime still marked
    dead fails the way the real handshake does, so a retry that skipped the revive
    cannot pass here. Any other raised outcome stands for a refusal before a child
    exists, which leaves nothing to reap.
    """
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.session_handle import AcpRuntimeDead
    from kiro_crew.acp.types import ACP_BACKEND_KIRO

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._process = None
    rt._dead = False
    rt._process_tree_confirmed_dead = False
    rt._initialized = False
    rt._session_queues = {}
    rt._answer_tasks = set()
    rt._scratch_dir = None
    rt._stderr_lines = []
    calls: list[int] = []
    outcomes = list(attempts_outcomes)

    async def _spawn_admitted():
        if rt._dead:
            raise AcpRuntimeDead("runtime is dead")
        calls.append(len(calls))
        outcome = outcomes.pop(0)
        if isinstance(outcome, agent.DerivedSpecStale):
            rt._dead = True
            rt._process_tree_confirmed_dead = True
        if isinstance(outcome, BaseException):
            raise outcome

    rt._spawn_admitted = _spawn_admitted  # type: ignore[method-assign]

    class _Admission:
        active = 0
        queued = 0

        async def acquire(self, priority=None):
            return 0.0

        def release(self, priority=None):
            return None

        class semaphore:  # noqa: N801 - the attribute the spawn log reads
            @staticmethod
            def describe() -> str:
                return ""

    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: _Admission())
    return rt, calls


def test_a_default_written_during_the_spawn_window_is_re_derived_and_respawned_once(monkeypatch):
    """Fail-closed on a revocation is right; fail-closed on the dashboard's MCP sync
    landing inside a spawn's window is a dispatch that failed for no reason the operator
    did anything wrong. The bracket kills the child either way -- it cannot tell them apart
    -- so the spawn path retries exactly once: the second attempt re-runs the gate, which
    re-derives the mirror from the default as it now stands."""
    import asyncio

    rt, calls = _runtime_for_spawn(
        monkeypatch, [agent.DerivedSpecStale("changed during load"), None]
    )
    asyncio.run(rt.spawn())
    assert calls == [0, 1], "one retry, on the bracket's own refusal"


def test_a_default_that_keeps_moving_across_two_spawns_is_refused(monkeypatch):
    """Never a loop: the exit condition is another process leaving the file alone. A
    second refusal propagates as the runtime's abort type rather than a bare
    ``RuntimeError`` no caller of ``spawn()`` catches."""
    import asyncio

    from kiro_crew.acp.session_handle import AcpRuntimeError

    rt, calls = _runtime_for_spawn(
        monkeypatch,
        [agent.DerivedSpecStale("changed during load"), agent.DerivedSpecStale("still changing")],
    )
    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.spawn())
    assert calls == [0, 1]
    assert "still changing" in str(excinfo.value)


def test_a_mirror_that_cannot_be_re_derived_is_not_retried(monkeypatch):
    """The pre-spawn gate's own refusal arrives as ``AcpRuntimeError`` already, and a
    second attempt would fail the same way for the same reason: the retry is for the
    post-handshake bracket only."""
    import asyncio

    from kiro_crew.acp.session_handle import AcpRuntimeError

    rt, calls = _runtime_for_spawn(monkeypatch, [AcpRuntimeError("could not be re-derived")])
    with pytest.raises(AcpRuntimeError):
        asyncio.run(rt.spawn())
    assert calls == [0], "the gate's refusal is not the bracket's, and is not retried"


class _FakeHandle:
    """The session handle, reduced to what the activation branch touches.

    Patched in rather than constructed, so these tests exercise the RUNTIME's bracket
    around ``set_mode`` and not the handle's own config parsing, served-default lookup or
    drain timing -- each of which has its own tests.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.drained = False
        self.native_context_documents = {}

    def store_session_config(self, resp):
        return None

    async def ensure_served_default(self):
        return None

    async def apply_session_permission_routing(self):
        return None

    def mcp_session_report(self):
        return self

    def begin_session(self, servers):
        return None

    def record_mcp_launches(self, spec, wire_servers):
        return None

    def queued_frame_count(self):
        return 0

    async def drain_init(self, **kwargs):
        self.drained = True


def _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated):
    """A real ``AcpRuntime`` positioned so ``create_session`` reaches the activation branch.

    ``object.__new__`` plus stubbed collaborators, the same shape the KAS projection tests
    in this tree use. Everything the branch itself does is REAL -- the gate calls, the
    ordering, the teardown -- which is what makes these tests fail when the bracket is
    removed from the product.
    """
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.types import ACP_BACKEND_KIRO

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._initialized = True
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._agent = "kirocrew"
    rt._crew_agent = "kirocrew"
    rt._work_dir = tmp_path / "wd"
    rt._member_context = False
    rt._native_launch_sources = {}
    rt._mcp_gateway_overlay = None
    rt._agent_capabilities = {}
    rt._session_queues = {}
    rt._session_inits_in_flight = 0
    rt._expect_mcp_reports = False
    rt._pid = 4242  # only the post-activation log line reads it

    resp = {
        "sessionId": "sid-1",
        "modes": {
            "currentModeId": "kirocrew",
            "availableModes": [{"id": "kirocrew"}, {"id": "kirocrew-worker"}],
        },
    }

    async def _send_and_await(method, params, timeout=None):
        sent.append(method)
        return resp

    async def _kas_custom_agents(
        agent, *, member_dispatch=False, crew_panel=False, session_key="", session_work_dir=None
    ):
        # The kiro backend builds no wire surface, so it carries no payload and no payload
        # snapshot -- which is what makes the set_mode line itself the consumed load.
        from kiro_crew.acp.harness import SessionExtras

        return SessionExtras()

    async def _session_work_dir(cwd=None):
        return tmp_path / "ws"

    async def _budget():
        return 1.0

    async def _verify(session_id, resp_, *, override=None):
        return None

    async def _terminate(session_id):
        terminated.append(session_id)

    rt._send_and_await = _send_and_await  # type: ignore[method-assign]
    rt._kas_custom_agents = _kas_custom_agents  # type: ignore[method-assign]
    rt._session_work_dir = _session_work_dir  # type: ignore[method-assign]
    rt._session_start_budget = _budget  # type: ignore[method-assign]
    rt._verify_spawn_agent_active = _verify  # type: ignore[method-assign]
    rt.terminate_session = _terminate  # type: ignore[method-assign]
    rt._finish_session_init = lambda session_id: []  # type: ignore[method-assign]

    monkeypatch.setattr(runtime_mod, "AcpSessionHandle", _FakeHandle)
    monkeypatch.setattr(runtime_mod, "_load_watchdog_settings", lambda crew: None)
    return rt


def test_activating_the_worker_on_an_unrepairable_mirror_never_sends_set_mode(
    tmp_path, monkeypatch
):
    """Fail closed BEFORE the send. Once ``set_mode`` goes out, kiro-cli has loaded the spec
    and booted its servers -- a refusal after that point is a session already running on
    grants the default agent does not have."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SESSION_NEW

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    # A mirror that cannot be re-derived, which is the one state the gate refuses.
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )
    agent_state.set_mirrored_from("kirocrew-worker", "stale-fingerprint")
    agent_state.set_mirrored_stat("kirocrew-worker", None)
    monkeypatch.setattr(agent, "rederive_worker_agent", lambda reason: False)

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert sent == [METHOD_SESSION_NEW], f"set_mode was sent on an unverified spec: {sent}"
    assert terminated == ["sid-1"], "the session must not be left half-registered"
    assert "could not be re-derived" in str(excinfo.value)


def test_a_default_changed_during_set_mode_tears_the_session_down(tmp_path, monkeypatch):
    """The window the pair closes on this path: the pre-check passed, kiro-cli read the spec
    at ``set_mode``, and a revocation landed in between. The session may have activated a
    spec nobody verified, so it is ended rather than left running."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)
    real_send = rt._send_and_await

    async def _send_then_revoke(method, params, timeout=None):
        answer = await real_send(method, params, timeout)
        if method == METHOD_SET_MODE:
            # The revocation lands while kiro-cli is loading the spec this activates.
            default.write_text(json.dumps(edited), encoding="utf-8")
        return answer

    rt._send_and_await = _send_then_revoke  # type: ignore[method-assign]

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "the send must have happened -- this is the POST-load half"
    assert terminated == ["sid-1"], "a session that may hold an unverified spec must end"
    assert "changed during worker load" in str(excinfo.value)


def test_activating_a_non_derived_agent_costs_no_check(tmp_path, monkeypatch):
    """The scope guard, so ordinary sessions pay nothing. An agent that mirrors nothing has
    no generation to be stale against, and the gate answers that from the name alone --
    without reading the default spec, let alone hashing it."""
    import asyncio

    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    read: list[str] = []
    monkeypatch.setattr(
        agent, "_installed_default_spec", lambda: read.append("read") or _DEFAULT_SPEC_ON_DISK
    )

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "a non-derived agent must still be activated"
    assert terminated == []
    assert read == [], "the scope guard must answer before the default spec is read"


# ── the array-backed hosts: the array IS the load, so it carries the snapshot ──


def test_the_array_projection_carries_the_snapshot_it_was_built_from(tmp_path, monkeypatch):
    """Claude/codex/opencode read no spec of their own -- the ``session/new`` array IS
    where they consume the derived spec. So the projection has to hand back the snapshot
    those elements were built from, or the client has a gate and a payload but nothing to
    re-verify once the host has taken it."""
    from kiro_crew import agent_state
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda: {})

    projection = session_mcp.session_mcp_projection(
        "kirocrew-worker", work_dir=str(tmp_path / "wd")
    )
    snap = projection.derived_spec_snapshot
    assert snap is not None
    assert snap.identity == agent.default_spec_identity()
    assert snap.fingerprint == agent.default_spec_fingerprint()
    # The elements were built from THAT snapshot's bytes, not from a second read.
    assert {e["name"] for e in projection.servers} >= {"kirocrew-work"}

    # An agent that mirrors nothing has no generation to be stale against.
    assert (
        session_mcp.session_mcp_projection("kirocrew", work_dir=None).derived_spec_snapshot is None
    )


def test_every_array_mirror_passes_the_snapshot_through(monkeypatch):
    """Three mirrors build the wire array from one projection, and each must hand the
    snapshot on rather than dropping it at its own return -- a mirror that forgot would
    leave its host family with no post-consume check while the others had one."""
    import inspect

    from kiro_crew.providers.mirrors import claude_code, codex, opencode

    for module in (claude_code, codex, opencode):
        source = inspect.getsource(module)
        # The return that carries the array carries the snapshot beside it.
        body = source[source.index("session_mcp_projection(") :]
        assert "derived_spec_snapshot=projection.derived_spec_snapshot" in body, module.__name__


def _claude_client_at_session_new(tmp_path, snapshot):
    """A claude-backed AcpClient positioned so ``_initialize_session`` reaches session/new
    with a pre-warmed array and the snapshot it was built from."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    c = client_mod.AcpClient(
        work_dir=tmp_path, agent="kirocrew-worker", acp_backend=ACP_BACKEND_CLAUDE
    )
    c._resume_session_id = None
    c._session_mcp_cache = []  # warmed on the spawn path; the array itself is not under test
    c._session_mcp_snapshot = snapshot
    sent: list[str] = []

    async def _send_request(method, params):
        sent.append(method)
        return len(sent)

    async def _wait_for_response(rid, timeout=None, *, method="", expected_mcp=None):
        return {"protocolVersion": "1.0", "sessionId": "sess-123"}

    c._send_request = _send_request  # type: ignore[method-assign]
    c._wait_for_response = _wait_for_response  # type: ignore[method-assign]
    c._drain_notifications = AsyncMock()  # type: ignore[method-assign]
    return c, sent


def test_a_revocation_between_the_array_build_and_session_new_ends_the_session(
    tmp_path, monkeypatch
):
    """The gap the array-backed family had: the projection built the array from spec A
    and dropped the snapshot; the client's only post-check judged the kiro-cli
    ``--agent`` read, which a claude child never performs and which is None on this
    backend. A revocation landing after the build and before ``session/new`` therefore
    registered the revoked server with nothing to catch it."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.client import AcpError
    from kiro_crew.acp.types import METHOD_INITIALIZE, METHOD_SESSION_NEW

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    c, sent = _claude_client_at_session_new(tmp_path, snap)
    real_wait = c._wait_for_response

    async def _wait_then_revoke(rid, timeout=None, *, method="", expected_mcp=None):
        resp = await real_wait(rid, timeout=timeout, method=method, expected_mcp=expected_mcp)
        if method == METHOD_SESSION_NEW:
            revoked = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
            revoked["mcpServers"].pop("builder-mcp")
            default.write_text(json.dumps(revoked), encoding="utf-8")
        return resp

    c._wait_for_response = _wait_then_revoke  # type: ignore[method-assign]

    with pytest.raises(AcpError) as excinfo:
        asyncio.run(c._initialize_session())

    assert sent[:2] == [METHOD_INITIALIZE, METHOD_SESSION_NEW], sent
    assert "changed during worker load" in str(excinfo.value)


def test_post_check_uses_snapshot_sent_not_the_reseeded_one(tmp_path, monkeypatch):
    """The post-check judges the snapshot sent to session/new even after re-seeding
    invalidates the instance projection for the next composition."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.client import AcpError
    from kiro_crew.acp.types import METHOD_SESSION_NEW

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    sent_snapshot = agent.require_fresh_derived_spec("kirocrew-worker", None)

    c, _sent = _claude_client_at_session_new(tmp_path, sent_snapshot)
    real_wait = c._wait_for_response
    real_reseed = c._reseed_after_capture
    snapshots_after_reseed = []

    async def _wait_then_revoke(rid, timeout=None, *, method="", expected_mcp=None):
        resp = await real_wait(rid, timeout=timeout, method=method, expected_mcp=expected_mcp)
        if method == METHOD_SESSION_NEW:
            revoked = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
            revoked["mcpServers"].pop("builder-mcp")
            default.write_text(json.dumps(revoked), encoding="utf-8")
        return resp

    async def _reseed_and_observe_reset():
        await real_reseed()
        snapshots_after_reseed.append(c._session_mcp_snapshot)

    c._wait_for_response = _wait_then_revoke  # type: ignore[method-assign]
    c._reseed_after_capture = _reseed_and_observe_reset  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="changed during worker load"):
        asyncio.run(c._initialize_session())

    assert snapshots_after_reseed == [None]


def test_an_unchanged_default_lets_the_array_backed_session_proceed(tmp_path, monkeypatch):
    """The ordinary case pays one stat and says nothing."""
    import asyncio

    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    c, sent = _claude_client_at_session_new(tmp_path, snap)
    asyncio.run(c._initialize_session())
    assert c._session_id == "sess-123"


def test_the_array_snapshot_is_dropped_with_the_array_on_reset(tmp_path):
    """Per-spawn freshness, same as the array: a replacement process must judge its own
    array against its own snapshot, never inherit this one's."""
    from kiro_crew.acp import client as client_mod

    c = client_mod.AcpClient(work_dir=tmp_path, agent="kirocrew-worker")
    c._session_mcp_cache = [{"name": "x"}]
    c._session_mcp_snapshot = object()
    c._reset_state()
    assert c._session_mcp_cache is None
    assert c._session_mcp_snapshot is None


def test_the_session_mcp_projection_reads_nothing_after_the_gate(tmp_path, monkeypatch):
    """ZERO reads, not one read placed carefully. A read taken after the gate returns is a
    SECOND observation of a file the gate already finished with, however tight the sequence
    looks -- and a revocation landing between the two becomes the session's MCP surface as
    though it had been checked. The gate reads and verifies those bytes, so the projection
    is handed the object rather than the path."""
    from kiro_crew import agent_state
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    reads: list[str] = []
    real_reader = session_mcp._read_agent_spec

    def _counting(path, **kwargs):
        reads.append(str(path))
        return real_reader(path, **kwargs)

    monkeypatch.setattr(session_mcp, "_read_agent_spec", _counting)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)

    spec = session_mcp._agent_spec_for("kirocrew-worker", str(tmp_path / "wd"))

    assert spec is not None
    assert reads == [], f"the projection re-read the spec after the gate: {reads}"
    # And it is the gate's own object, so the bytes verified and the bytes projected
    # cannot drift apart.
    snap = agent.require_fresh_derived_spec("kirocrew-worker", str(tmp_path / "wd"))
    assert snap is not None and snap.spec == spec

    # Every other agent mirrors nothing, has no snapshot, and is read here as before --
    # otherwise this branch would silently stop serving them.
    reads.clear()
    (tmp_path / "kirocrew.json").exists()
    session_mcp._agent_spec_for("kirocrew", str(tmp_path / "wd"))
    assert reads, "a non-derived agent must still be read from disk"


def test_the_snapshot_reader_takes_no_read_of_its_own(monkeypatch):
    """The exported snapshot is the projection's own answer, not a parallel read that could
    resolve a different spec than the projection ran on."""
    import inspect

    from kiro_crew.acp import session_mcp

    snapshot_src = inspect.getsource(session_mcp.agent_spec_snapshot)
    assert "_agent_spec_for(" in snapshot_src
    assert "_read_agent_spec(" not in snapshot_src
