"""End-to-end routing verification for descriptor backends.

The threat the gate answers: a descriptor DECLARES a routing, and a host can
honour the declared mechanism (load the agent, accept the config option) while
never sending ``session/request_permission`` -- every tool call it made would
bypass PreToolUse, the deny rules and the audit. So a declared routing makes a
descriptor known and runnable-for-verification, never selectable; only an
attestation this gateway recorded after observing the host ask (and honour a
refusal) does -- bound to the descriptor's spawn shape AND to the bytes of the
executable that was verified, and re-checked against the bytes about to run on
every spawn. These tests pin the store, the gate, the probe's verdicts, and the
two ways the probe must not be fooled (a provider for another backend; a binary
swapped under an unchanged path).
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.acp.harness.descriptor import HarnessDescriptor, PermissionConfig
from kiro_crew.acp.harness.routing_verification import (
    PROBE_FILE_NAME,
    UNVERIFIED_REASON,
    VERDICT_INCONCLUSIVE,
    VERDICT_VERIFIED,
    VERDICT_VIOLATION,
    descriptor_fingerprint,
    executable_digest,
    is_attested,
    load_attestations,
    record_attestation,
    revoke_attestation,
    spawn_attestation_problem,
    verify_routing,
)
from kiro_crew.agent_sdk import backends as b
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)


@pytest.fixture(autouse=True)
def _host_provenance_accepted(monkeypatch):
    """Stub executables live under the test's temp tree, whose ancestors are the
    host's business (a CI runner's home, a developer's /tmp), not this suite's.
    The provenance rule itself -- outside the agent-writable trees, owned by the
    gateway user or root, unwritable by others -- is ``github_runner``'s and is
    tested there; here it is accepted so the routing logic under test is what
    decides. The provenance tests in ``test_routing_verification`` re-patch it.
    """
    from kiro_crew import github_runner

    def _accept_relaxed(candidate, *, require_protected=False):
        # Provenance requires the strict form (a root-owned, gateway-unwritable
        # install) of every harness executable and of a launcher's interpreter; a
        # temp-tree stub cannot be one on this host, so the rule is answered
        # "accepted" for the stubs and the routing logic under test decides. The
        # provenance tests re-patch it to refuse.
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _accept_relaxed)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def clean_registry():
    baseline = set(b._baseline)
    selectable = set(b._selectable)
    yield
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    reg._reset_operator_diagnostics()
    b._baseline.clear()
    b._baseline.update(baseline)
    b._selectable.clear()
    b._selectable.update(selectable)


def _stub(tmp_path, name="acme", body="#!/bin/sh\nexec cat\n") -> str:
    """A real, readable, executable file: the attestation binds to its bytes.

    Resolves on every platform without being run: POSIX wants the execute bit;
    Windows has no execute bit and accepts a known runnable suffix (``.cmd``).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / (f"{name}.cmd" if sys.platform == "win32" else name)
    exe.write_text(body, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def _descriptor(exe: str, **overrides) -> HarnessDescriptor:
    base = dict(
        id="acme",
        display_name="Acme",
        executable=exe,
        argv=("{executable}", "acp"),
        agent_args=("--agent", "{agent}"),
        routing="agent_spec",
    )
    base.update(overrides)
    return HarnessDescriptor(**base)


# ── Fingerprint ──


def test_fingerprint_covers_every_spawn_shaping_field_and_nothing_else(tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    fp = descriptor_fingerprint(d)
    assert fp == descriptor_fingerprint(_descriptor(exe))
    # Cosmetic fields do not move it: a rename or a static-catalog edit must not
    # revoke an attestation, because neither changes what runs or how it routes.
    assert descriptor_fingerprint(_descriptor(exe, display_name="Renamed")) == fp
    assert descriptor_fingerprint(_descriptor(exe, models=("m1",))) == fp
    # Every spawn-shaping field does.
    assert descriptor_fingerprint(_descriptor("/opt/other")) != fp
    assert descriptor_fingerprint(_descriptor(exe, argv=("{executable}", "serve"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, agent_args=("--profile", "{agent}"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, model_args=("--model", "{model}"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, mcp_delivery="session_array")) != fp
    assert (
        descriptor_fingerprint(
            _descriptor(
                exe,
                agent_args=(),
                routing="session_config",
                permission_config=PermissionConfig("mode", "ask"),
            )
        )
        != fp
    )


# ── Store ──


def test_store_binds_fingerprint_and_executable_bytes(home, tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    assert load_attestations() == {}
    assert is_attested(d) is False
    rec = record_attestation(d, mechanism="agent_spec", evidence={"permission_requests": 1})
    assert rec["fingerprint"] == descriptor_fingerprint(d)
    assert rec["executable_path"] == exe
    assert rec["executable_digest"] == executable_digest(exe)
    assert is_attested(d) is True
    # A different spawn shape under the same id is NOT attested: the edit revoked it.
    assert is_attested(_descriptor(exe, argv=("{executable}", "serve"))) is False
    # The binary's BYTES replaced under the same path: not attested either.
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    assert is_attested(d) is False
    # The file is gateway-owned JSON beside config.json.
    on_disk = json.loads((home / "backend-routing-attestations.json").read_text("utf-8"))
    assert on_disk["acme"]["mechanism"] == "agent_spec"
    assert revoke_attestation("acme") is True
    assert revoke_attestation("acme") is False


def test_store_binds_the_agent_the_probe_ran_under(home, tmp_path):
    """One probe vouches for ONE agent. A further probe of the same descriptor
    and the same bytes adds its agent; a probe of changed bytes or a changed
    spawn shape starts the record over. The spawn check admits only agents the
    record names -- and a record from before agents were bound names none."""
    from kiro_crew.acp.harness.routing_verification import (
        agent_attestation_problem,
        attested_agents,
    )

    exe = _stub(tmp_path)
    d = _descriptor(exe)
    assert "no routing attestation" in agent_attestation_problem(d, "a")
    rec = record_attestation(d, mechanism="agent_spec", evidence={}, agent="a")
    assert rec["agents"] == ["a"]
    assert agent_attestation_problem(d, "a") is None
    assert "not one this backend's routing was verified under" in agent_attestation_problem(d, "b")
    assert "not one this backend's routing was verified under" in agent_attestation_problem(d, None)
    # Same descriptor, same bytes, another agent: ADDED.
    rec = record_attestation(d, mechanism="agent_spec", evidence={}, agent="")
    assert rec["agents"] == ["", "a"]
    assert agent_attestation_problem(d, None) is None
    assert agent_attestation_problem(d, "a") is None
    # A different spawn shape: the record starts over with only the new agent.
    d2 = _descriptor(exe, argv=("{executable}", "serve"))
    rec = record_attestation(d2, mechanism="agent_spec", evidence={}, agent="b")
    assert rec["agents"] == ["b"]
    assert "not one this backend's routing was verified under" in agent_attestation_problem(d2, "a")
    # A record with no agents list (written before agents were bound) vouches for
    # no agent at all: fail closed until verified again.
    assert attested_agents({"fingerprint": "x"}) == frozenset()
    assert attested_agents({"agents": "a"}) == frozenset()
    # The reason names neither the requested agent nor the recorded ones.
    problem = agent_attestation_problem(d2, "a")
    assert "'a'" not in problem and "'b'" not in problem


def test_the_attested_agent_list_is_bounded(home, tmp_path):
    """A bound bounds every field it retains: the agent list is re-read on every
    spawn and listing, so a further distinct agent past the cap is refused at
    verification and the store is left exactly as it was."""
    from kiro_crew.acp.harness.routing_verification import MAX_ATTESTED_AGENTS

    exe = _stub(tmp_path)
    d = _descriptor(exe)
    for i in range(MAX_ATTESTED_AGENTS):
        rec = record_attestation(d, mechanism="agent_spec", evidence={}, agent=f"agent-{i:02d}")
    assert len(rec["agents"]) == MAX_ATTESTED_AGENTS
    before = load_attestations()["acme"]
    with pytest.raises(ValueError, match="most one attestation retains"):
        record_attestation(d, mechanism="agent_spec", evidence={}, agent="one-too-many")
    assert load_attestations()["acme"] == before
    # An agent already on the list is not a new name: re-verifying it is fine.
    rec = record_attestation(d, mechanism="agent_spec", evidence={}, agent="agent-00")
    assert len(rec["agents"]) == MAX_ATTESTED_AGENTS


def test_the_pin_never_holds_the_binary_whole(home, tmp_path, monkeypatch):
    """A harness binary can be hundreds of megabytes; the pin streams it (digest
    and copy in bounded chunks) rather than allocating it. Pinned by refusing
    any single read larger than the chunk size on the operator's file."""
    import builtins
    import io

    from kiro_crew.acp.harness.routing_verification import pin_verified_executable

    exe = _stub(tmp_path, body="#!/bin/sh\n" + ("# padding\n" * 200_000))  # ~2 MiB
    d = _descriptor(exe)
    record_attestation(d, mechanism="agent_spec", evidence={}, agent="a")
    real_open = builtins.open
    biggest = {"n": 0}

    class _Guard(io.FileIO):
        def read(self, size=-1):
            if size is None or size < 0 or size > (1 << 20):
                raise AssertionError("the executable was read whole")
            biggest["n"] = max(biggest["n"], size)
            return super().read(size)

    def guarded_open(path, mode="r", *a, **kw):
        if os.fspath(path) == exe and "b" in mode and "r" in mode:
            return _Guard(exe, "r")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded_open)
    pinned, problem = pin_verified_executable(d, exe)
    assert problem is None and pinned is not None
    assert 0 < biggest["n"] <= (1 << 20)
    monkeypatch.setattr(builtins, "open", real_open)
    from kiro_crew.acp.harness.routing_verification import executable_digest

    assert executable_digest(pinned) == executable_digest(exe)


def test_recording_refuses_an_executable_it_cannot_bind_to(home):
    with pytest.raises(ValueError):
        record_attestation(_descriptor("/opt/does-not-exist"), mechanism="agent_spec", evidence={})
    assert load_attestations() == {}


def test_unreadable_store_fails_closed(home, tmp_path):
    (home / "backend-routing-attestations.json").write_text("not json", encoding="utf-8")
    assert load_attestations() == {}
    assert is_attested(_descriptor(_stub(tmp_path))) is False


# ── Spawn-path re-validation ──


def test_spawn_check_refuses_a_replaced_binary_and_allows_the_probe(home, tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    assert (
        spawn_attestation_problem(d, exe) == "no routing attestation is recorded for this backend"
    )
    record_attestation(d, mechanism="agent_spec", evidence={})
    assert spawn_attestation_problem(d, exe) is None
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    problem = spawn_attestation_problem(d, exe)
    assert problem is not None and "changed since its routing was verified" in problem
    # A descriptor edit is refused too, and named as such.
    edited = _descriptor(exe, argv=("{executable}", "serve"))
    assert "descriptor changed" in (spawn_attestation_problem(edited, exe) or "")
    # The probe's own spawn -- the one that produces the first attestation -- needs
    # no record while, and only while, the probe holds the marker AND the spawn is
    # the probe's: its working directory is the probe's private scratch. Any other
    # spawn of the same id during the probe (a chat starting on a backend being
    # re-verified) is ordinary and is held to the record. The probe's spawn is
    # still held to the bytes the probe resolved, so a swap before exec is refused.
    from kiro_crew.acp.harness import routing_verification as rv

    scratch = tmp_path / "probe-scratch"
    scratch.mkdir()
    rv._PROBING["acme"] = (rv.executable_digest(exe), os.path.realpath(str(scratch)), "x")
    try:
        assert spawn_attestation_problem(_descriptor(exe), exe, work_dir=str(scratch)) is None
        # Not the probe's spawn: another cwd, or none at all -> the record decides
        # (here: the bytes changed since verification).
        for other in (str(tmp_path / "some-chat"), None):
            problem = spawn_attestation_problem(_descriptor(exe), exe, work_dir=other)
            assert problem is not None and "changed since its routing was verified" in problem
        # The agent check admits the probe's spawn for the probe's AGENT only: the
        # same scratch under another agent is an ordinary spawn, held to the record.
        assert rv.agent_attestation_problem(_descriptor(exe), "x", work_dir=str(scratch)) is None
        assert (
            rv.agent_attestation_problem(_descriptor(exe), "y", work_dir=str(scratch)) is not None
        )
        assert rv.agent_attestation_problem(_descriptor(exe), "x", work_dir=None) is not None
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexec swapped-mid-probe\n")
        problem = spawn_attestation_problem(_descriptor(exe), exe, work_dir=str(scratch))
        assert problem is not None and "between the routing probe's resolution" in problem
    finally:
        rv._PROBING.pop("acme", None)


def test_the_spawn_execs_the_judged_file_in_place_and_refuses_changed_bytes(home, tmp_path):
    """The spawn judges the operator's file immediately before the exec and execs
    THAT path: provenance already requires a protected install, so nothing running
    as the gateway user can rewrite it between the judgement and the exec. A file
    whose bytes do not match the attestation is refused -- there is no copy and no
    fallback -- and the backend stays refused until the operator verifies the
    replacement."""
    from kiro_crew.acp.harness import routing_verification as rv

    exe = _stub(tmp_path)
    d = _descriptor(exe)
    pinned, problem = rv.pin_verified_executable(d, exe)
    assert pinned is None and problem == "no routing attestation is recorded for this backend"
    record_attestation(d, mechanism="agent_spec", evidence={})
    pinned, problem = rv.pin_verified_executable(d, exe)
    assert problem is None and pinned == exe
    assert sorted(os.listdir(str(home))) == ["backend-routing-attestations.json"]
    # The operator's file is replaced: refused, nothing else is ever execed.
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    refused, problem = rv.pin_verified_executable(d, exe)
    assert refused is None and "changed since its routing was verified" in (problem or "")
    # Re-verified with the new bytes: execed in place again.
    record_attestation(d, mechanism="agent_spec", evidence={})
    fresh, problem = rv.pin_verified_executable(d, exe)
    assert problem is None and fresh == exe


def test_an_executable_the_gateway_user_could_write_is_refused_at_verification_and_spawn(
    home, tmp_path, monkeypatch
):
    """Provenance is the STRICT provider-CLI rule for the harness executable itself:
    root-owned, unwritable by the gateway user through every parent. The agent runs
    as the gateway user, so an executable that user can write is one the agent can
    replace with a harness that answers the probe on purpose; it is refused before
    the probe spawns (nothing recorded) and at every spawn of a verified one."""
    from kiro_crew import github_runner
    from kiro_crew.acp.harness import routing_verification as rv

    exe = _stub(tmp_path)
    d = _descriptor(exe)
    record_attestation(d, mechanism="agent_spec", evidence={})
    asked: list[tuple[str, bool]] = []

    def _user_owned(candidate, *, require_protected=False):
        asked.append((os.path.realpath(str(candidate)), require_protected))
        if require_protected:
            raise ValueError("writable by the gateway user")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _user_owned)
    problem = rv.executable_provenance_problem(exe)
    assert problem is not None
    assert "protected install" in problem and "writable by the gateway user" in problem
    # The executable is asked the STRICT question, never the relaxed one.
    assert (os.path.realpath(exe), True) in asked
    assert all(strict for _path, strict in asked)
    pinned, problem = rv.pin_verified_executable(d, exe)
    assert pinned is None and "protected install" in (problem or "")
    assert rv.is_attested(d) is False


def test_an_unreadable_store_aborts_a_record_or_revoke_instead_of_emptying_it(home, tmp_path):
    """A read-modify-write that started from ``{}`` because the READ failed would
    write the document back without every other backend's attestation. Readers
    still fail closed (``{}``); WRITERS distinguish absent from unreadable and
    abort, leaving the document exactly as it was."""
    from kiro_crew.acp.harness import routing_verification as rv

    exe = _stub(tmp_path)
    other = _stub(tmp_path, name="other")
    record_attestation(_descriptor(exe), mechanism="agent_spec", evidence={})
    record_attestation(_descriptor(other, id="other"), mechanism="agent_spec", evidence={})
    store = home / rv.ROUTING_ATTESTATIONS_LEAF
    good = store.read_bytes()
    # Malformed on disk (a truncated write, a stray edit): readers see no grants ...
    store.write_text("{not json", encoding="utf-8")
    assert load_attestations() == {}
    # ... and a writer refuses to rewrite from that empty view.
    with pytest.raises(rv.AttestationStoreUnreadable):
        record_attestation(_descriptor(exe), mechanism="agent_spec", evidence={})
    with pytest.raises(rv.AttestationStoreUnreadable):
        rv.revoke_attestation("acme")
    assert store.read_text(encoding="utf-8") == "{not json"
    # Not an object: the same refusal.
    store.write_text("[]", encoding="utf-8")
    with pytest.raises(rv.AttestationStoreUnreadable):
        rv.revoke_attestation("acme")
    # Restored: both records are still there, and a revoke of one keeps the other.
    store.write_bytes(good)
    assert set(load_attestations()) == {"acme", "other"}
    assert rv.revoke_attestation("acme") is True
    assert set(load_attestations()) == {"other"}
    # ABSENT is the legitimate empty start: a first record on a fresh store works.
    store.unlink()
    record_attestation(_descriptor(exe), mechanism="agent_spec", evidence={})
    assert set(load_attestations()) == {"acme"}


def test_concurrent_record_and_revoke_do_not_lose_each_other(home, tmp_path):
    """A verify recording one backend and a spawn revoking another both do
    load -> mutate -> write on the same file; every such pair is serialised, so
    the store ends with exactly the union of what each thread did."""
    import threading

    from kiro_crew.acp.harness import routing_verification as rv

    exes = {name: _stub(tmp_path, name=name) for name in ("a", "b", "c", "d")}
    descs = {name: _descriptor(exe, id=name) for name, exe in exes.items()}
    # Seed two so the revokers have something to remove.
    record_attestation(descs["a"], mechanism="agent_spec", evidence={})
    record_attestation(descs["b"], mechanism="agent_spec", evidence={})

    barrier = threading.Barrier(4)

    def recorder(name):
        barrier.wait()
        for _ in range(25):
            record_attestation(descs[name], mechanism="agent_spec", evidence={})

    def revoker(name):
        barrier.wait()
        for _ in range(25):
            rv.revoke_attestation(name)

    threads = [
        threading.Thread(target=recorder, args=("c",)),
        threading.Thread(target=recorder, args=("d",)),
        threading.Thread(target=revoker, args=("a",)),
        threading.Thread(target=revoker, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(rv.load_attestations()) == ["c", "d"]


# ── Boot gate ──


def _write(home, mapping):
    p = home / "harnesses.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return str(p)


def _routed(exe):
    return {
        "acme": {
            "executable": exe,
            "argv": ["{executable}", "acp"],
            "agent_args": ["--agent", "{agent}"],
            "routing": "agent_spec",
        }
    }


def _attest_mapping(mapping):
    from kiro_crew.acp.harness.descriptor import descriptor_from_mapping

    for hid, raw in mapping.items():
        d, _ = descriptor_from_mapping(raw, harness_id=hid)
        assert d is not None
        record_attestation(d, mechanism=d.routing, evidence={})


def test_a_routed_descriptor_is_known_but_unselectable_until_attested(
    home, clean_registry, tmp_path
):
    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    assert "acme" in b.ACP_BACKENDS_KNOWN
    assert "acme" in b.acp_runtime_backends()
    assert "acme" not in b.selectable_backends()
    assert reg.unselectable_operator_harnesses()["acme"] == UNVERIFIED_REASON
    assert "acme" in reg.unverified_operator_harnesses()
    # An unroutable descriptor is unselectable too, but NOT verifiable: there is
    # nothing to verify.
    reg._reset_operator_diagnostics()
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    no_route = {"acme": {"executable": exe, "argv": ["{executable}"]}}
    reg.load_and_register_operator_descriptors(path=_write(home, no_route))
    assert "acme" in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_an_attested_descriptor_is_selectable_at_boot(home, clean_registry, tmp_path):
    mapping = _routed(_stub(tmp_path))
    _attest_mapping(mapping)
    reg.load_and_register_operator_descriptors(path=_write(home, mapping))
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_editing_the_spawn_shape_or_the_binary_revokes_selectability_at_boot(
    home, clean_registry, tmp_path
):
    exe = _stub(tmp_path)
    mapping = _routed(exe)
    _attest_mapping(mapping)
    edited = {"acme": dict(mapping["acme"], argv=["{executable}", "acp", "--yolo"])}
    reg.load_and_register_operator_descriptors(path=_write(home, edited))
    assert "acme" not in b.selectable_backends()
    assert reg.unselectable_operator_harnesses()["acme"] == UNVERIFIED_REASON
    # Same descriptor, replaced binary.
    reg._reset_operator_diagnostics()
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    reg.load_and_register_operator_descriptors(path=_write(home, mapping))
    assert "acme" not in b.selectable_backends()
    assert "acme" in reg.unverified_operator_harnesses()


@pytest.mark.asyncio
async def test_the_spawn_path_revokes_with_the_store_rewrite_off_the_loop(
    home, clean_registry, tmp_path, monkeypatch
):
    """The spawn path runs on the event loop, and dropping the attestation is a
    JSON read-modify-write with an atomic replace -- file I/O that must not run
    there. The async revoke offloads that half and applies the registry half on
    the loop afterwards; and when the disk half fails, the registry is withdrawn
    regardless, because a backend whose bytes do not match its attestation must not stay
    selectable over a store that could not be rewritten."""
    import asyncio
    import threading

    from kiro_crew.acp.harness import operator_registry as reg

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    record_attestation(_descriptor(exe), mechanism="agent_spec", evidence={}, agent="a")
    reg.mark_routing_verified("acme", load_attestations()["acme"])
    assert "acme" in b.selectable_backends()

    loop_thread = threading.get_ident()
    disk_on: list[int] = []
    real_revoke = reg._revoke_attestation_on_disk

    def _recording(backend_id):
        disk_on.append(threading.get_ident())
        return real_revoke(backend_id)

    monkeypatch.setattr(reg, "_revoke_attestation_on_disk", _recording)
    await reg.revoke_routing_verification_async("acme", "the executable changed")
    assert disk_on and all(t != loop_thread for t in disk_on)
    assert "acme" not in b.selectable_backends()
    assert "acme" not in load_attestations()
    assert "acme" in reg.unselectable_operator_harnesses()

    # Disk half failing: the registry is still withdrawn.
    reg.mark_routing_verified(
        "acme", record_attestation(_descriptor(exe), mechanism="agent_spec", evidence={}, agent="a")
    )
    assert "acme" in b.selectable_backends()

    def _failing(backend_id):
        raise OSError("read-only store")

    monkeypatch.setattr(reg, "_revoke_attestation_on_disk", _failing)
    await reg.revoke_routing_verification_async("acme", "the executable changed")
    assert "acme" not in b.selectable_backends()
    with pytest.raises(ValueError):
        await reg.revoke_routing_verification_async("claude", "x")
    del asyncio


def test_mark_and_revoke_routing_verification_live(home, clean_registry, tmp_path):
    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    d = reg.registered_operator_descriptor("acme")
    assert d is not None
    with pytest.raises(ValueError):
        reg.mark_routing_verified("acme", {"fingerprint": "not-this-descriptor"})
    with pytest.raises(ValueError):
        reg.mark_routing_verified("not-registered", {})
    rec = record_attestation(d, mechanism="agent_spec", evidence={})
    assert reg.mark_routing_verified("acme", rec) is True
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unverified_operator_harnesses()
    # Revocation (what the spawn path does on a digest mismatch) withdraws the
    # grant everywhere: registry, attestation file, diagnostics.
    reg.revoke_routing_verification("acme", "the executable changed")
    assert "acme" not in b.selectable_backends()
    assert "acme" not in b._baseline
    assert load_attestations() == {}
    assert "acme" in reg.unverified_operator_harnesses()
    assert reg.unselectable_operator_harnesses()["acme"].startswith("the executable changed; ")
    with pytest.raises(ValueError):
        reg.revoke_routing_verification("claude", "x")  # a builtin has no grant to revoke
    with pytest.raises(ValueError):
        b.unregister_selectable_backend("claude")


def test_live_verification_reapplies_the_agent_backend_policy(
    home, clean_registry, tmp_path, monkeypatch
):
    """An administrator's agent_backend denial holds for a backend the owner
    verifies AFTER boot: the narrowing that ran at boot never saw the id, so the
    live path re-runs it, and a denied id is verified-but-not-selectable, with
    the policy named on its row and no Verify offered (verification is not what
    it lacks)."""
    import kiro_crew.agent_backend_governance as gov

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    d = reg.registered_operator_descriptor("acme")
    assert d is not None
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: backend != "acme")
    # The verdict is applied IN the registration, not by a recompute after it:
    # every write to the selectable set is observed, and none of them ever puts
    # the denied id in. A recompute-after-register form would show one that does.
    real_narrow = gov.narrow_selectable_backends
    narrow_calls: list[str] = []
    monkeypatch.setattr(
        gov, "narrow_selectable_backends", lambda: narrow_calls.append("narrow") or real_narrow()
    )
    rec = record_attestation(d, mechanism="agent_spec", evidence={})
    assert reg.mark_routing_verified("acme", rec) is False
    assert "acme" not in b.selectable_backends()
    assert "acme" not in reg.unverified_operator_harnesses()
    assert reg.unselectable_operator_harnesses()["acme"] == reg.POLICY_DENIED_REASON
    # No recompute was needed to arrive there ...
    assert narrow_calls == []
    # ... and the id is in the BASELINE, so a loosened policy restores it.
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: True)
    real_narrow()
    assert "acme" in b.selectable_backends()


def test_background_load_registers_a_policy_denied_descriptor_never_selectable(
    home, clean_registry, tmp_path, monkeypatch
):
    """The loader runs AFTER the boot-time governance pass (a background task), and
    the attestation check between two descriptors digests a binary, so "register,
    then recompute at the end" would leave the first descriptor selectable for the
    whole digest of the second. The verdict is applied in the registration
    instead: a denied descriptor is known, visible-but-unselectable with the
    policy reason, and there is no write that ever makes it selectable."""
    import kiro_crew.agent_backend_governance as gov

    exe = _stub(tmp_path)
    mapping = _routed(exe)
    _attest_mapping(mapping)
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: backend != "acme")
    writes: list[frozenset[str]] = []
    real_governed = b.register_governed_backend

    def _spy_governed(backend, *, permitted):
        real_governed(backend, permitted=permitted)
        writes.append(frozenset(b._selectable))

    monkeypatch.setattr(reg, "register_governed_backend", _spy_governed)
    reg.load_and_register_operator_descriptors(path=_write(home, mapping))
    assert "acme" in b.ACP_BACKENDS_KNOWN
    assert "acme" not in b.selectable_backends()
    assert all("acme" not in snapshot for snapshot in writes), writes
    assert reg.unselectable_operator_harnesses()["acme"] == reg.POLICY_DENIED_REASON
    assert "acme" not in reg.unverified_operator_harnesses()
    # Permitted: the same step makes it selectable at once.
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: True)
    gov.narrow_selectable_backends()
    assert "acme" in b.selectable_backends()


# ── The probe ──


class _FakeProvider:
    """A provider whose one turn replays a scripted event list.

    ``writes_probe`` makes the fake behave like a host that executes the write
    regardless of the answer (the violation the probe exists to catch).
    ``backend`` is the identity the provider reports; the probe must refuse to
    attest when it is not the descriptor's own.
    """

    def __init__(self, events, *, cwd, backend="acme", writes_probe=False, raise_on_stream=None):
        self._events = events
        self._cwd = cwd
        self.acp_backend = backend
        self._writes_probe = writes_probe
        self._raise = raise_on_stream
        self.rejected: list = []
        self.started = False
        self.shut_down = False

    async def start(self):
        self.started = True

    async def stream(self, message):
        if self._raise is not None:
            raise self._raise
        for ev in self._events:
            if self._writes_probe and ev.kind == EVENT_COMPLETE:
                with open(os.path.join(self._cwd, PROBE_FILE_NAME), "w", encoding="utf-8") as fh:
                    fh.write("probe")
            yield ev

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def shutdown(self):
        self.shut_down = True


def _builder(events, **kw):
    made = {}

    def build(session_key, cwd):
        p = _FakeProvider(events, cwd=cwd, **kw)
        made["provider"] = p
        made["cwd"] = cwd
        made["session_key"] = session_key
        return p

    return build, made


def _perm(rid, *, path=None, unrelated=False, shell=None):
    """A permission request. Default: an edit FOR the probe file (relative to
    the probe cwd, as a host would spell it). ``unrelated``: an edit of some
    other file. ``shell``: a command-line request with that text."""
    from kiro_crew.acp.harness.routing_verification import PROBE_FILE_NAME

    if shell is not None:
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=rid,
            tool_kind="execute",
            is_shell=True,
            tool_input=shell,
            raw_tool_params={"command": shell},
        )
    target = "notes/other.txt" if unrelated else (path or PROBE_FILE_NAME)
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=rid,
        tool_kind="edit",
        raw_tool_params={"path": target, "content": "probe"},
    )


def _both_classes_ask():
    """A well-behaved host's event stream: asks before the file edit AND before
    the shell write, then completes -- the shape a verified verdict needs."""
    from kiro_crew.acp.harness.routing_verification import PROBE_SHELL_COMMAND

    return [_perm("r1"), _perm("r2", shell=PROBE_SHELL_COMMAND), LLMEvent(kind=EVENT_COMPLETE)]


@pytest.mark.asyncio
async def test_probe_verifies_a_host_that_asks_and_honours_the_refusal(tmp_path):
    """The probe asks for a write through EACH tool class -- a file edit and a
    shell command -- and attests only when the host asked before both and
    nothing landed. A host may gate one class and not the other, and a verdict
    from the gated class alone would attest the ungated one."""
    from kiro_crew.acp.harness.routing_verification import PROBE_SHELL_COMMAND

    build, made = _builder(
        [
            _perm("r1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="denied"),
            _perm("r2", shell=PROBE_SHELL_COMMAND),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
    )
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_VERIFIED
    assert result.permission_requests == 2
    assert result.details["permission_request_classes"] == ["edit", "shell"]
    assert made["session_key"] == "backend-routing-probe:acme"
    assert made["provider"].rejected == ["r1", "r2"]  # never granted
    assert made["provider"].shut_down is True
    assert not os.path.isdir(made["cwd"])  # scratch removed


@pytest.mark.asyncio
async def test_one_gated_tool_class_attests_nothing_about_the_other(tmp_path):
    """Only the edit asked (the shell write was never attempted), or only the
    shell asked: inconclusive, naming the class that never asked, and nothing
    recorded. A host whose shell tool bypasses the gate would instead have
    WRITTEN the file, which the violation branch catches."""
    from kiro_crew.acp.harness.routing_verification import PROBE_SHELL_COMMAND

    build, _ = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE and result.permission_requests == 1
    assert "asked before its file edit but never attempted its shell command" in result.reason
    assert result.details["permission_request_classes"] == ["edit"]
    assert load_attestations() == {}

    build, _ = _builder([_perm("r1", shell=PROBE_SHELL_COMMAND), LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE and result.permission_requests == 1
    assert "asked before its shell command but never attempted its file edit" in result.reason
    assert result.details["permission_request_classes"] == ["shell"]


# ── Provenance: bytes the agent could have written attest nothing ──


def _refuse_provenance(monkeypatch, *, only: str | None = None, why: str = "inside the tree"):
    """Make the shared provider-CLI rule refuse (every path, or only ``only``)."""
    from kiro_crew import github_runner

    def _validate(candidate, **kw):
        if only is None or os.path.realpath(candidate) == os.path.realpath(only):
            raise ValueError(why)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)


@pytest.mark.asyncio
async def test_probe_refuses_an_executable_the_agent_could_have_written(tmp_path, monkeypatch):
    """A harness inside the agent-writable trees (or world-writable, or owned by
    another account) could be built to recognise the fixed probe and answer it
    correctly on purpose. The probe refuses BEFORE spawning -- the provider is
    never built -- and records nothing."""
    _refuse_provenance(monkeypatch, why="executable is inside the agent-writable tree")
    build, made = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "provenance is refused" in result.reason
    assert "inside the agent-writable tree" in result.reason
    assert "provider" not in made, "nothing was spawned"
    assert load_attestations() == {}


def test_spawn_and_boot_refuse_an_executable_whose_provenance_fails(home, tmp_path, monkeypatch):
    """The same rule holds after verification: a verified file that later fails
    provenance (moved into a writable tree, mode opened up) is refused at spawn,
    withdrawn from selectability, and not selectable at boot."""
    from kiro_crew.acp.harness.routing_verification import (
        pin_verified_executable,
        spawn_attestation_problem,
    )

    exe = _stub(tmp_path)
    d = _descriptor(exe)
    record_attestation(d, mechanism="agent_spec", evidence={}, agent="a")
    assert is_attested(d) is True
    assert spawn_attestation_problem(d, exe) is None
    _refuse_provenance(monkeypatch, why="executable is world-writable")
    assert is_attested(d) is False
    problem = spawn_attestation_problem(d, exe)
    assert problem is not None and "provenance is refused" in problem
    pinned, pin_problem = pin_verified_executable(d, exe)
    assert pinned is None and pin_problem is not None and "world-writable" in pin_problem


def test_a_launcher_is_held_to_provenance_through_its_interpreter(tmp_path, monkeypatch):
    """A ``#!`` launcher's bytes are what the attestation covers, and the
    interpreter it names is what runs them, so the interpreter is judged too. An
    ``env`` indirection is REFUSED: what it runs is whatever the child's PATH
    supplies, which no file's bytes can bind; so is a relative interpreter. A
    binary has nothing further to judge."""
    from kiro_crew.acp.harness.routing_verification import (
        _shebang_interpreter,
        executable_provenance_problem,
    )

    exe = _stub(tmp_path, body="#!/opt/tool/bin/node --harmony\nconsole.log(1)\n")
    assert _shebang_interpreter(exe) == "/opt/tool/bin/node"
    env_launcher = _stub(tmp_path, name="b", body="#!/usr/bin/env node\n")
    assert _shebang_interpreter(env_launcher) == "/usr/bin/env"
    problem = executable_provenance_problem(env_launcher)
    assert problem is not None and "env indirection" in problem
    relative = _stub(tmp_path, name="c", body="#!node\n")
    assert _shebang_interpreter(relative) == "node"
    problem = executable_provenance_problem(relative)
    assert problem is not None and "relative path" in problem
    assert _shebang_interpreter(_stub(tmp_path, name="d", body="\x7fELF\x02\x01\x01")) is None
    assert executable_provenance_problem(exe) is None
    _refuse_provenance(monkeypatch, only="/opt/tool/bin/node", why="owned by another user")
    problem = executable_provenance_problem(exe)
    assert problem is not None and "interpreter provenance is refused" in problem
    assert "owned by another user" in problem


def test_a_launcher_interpreter_the_gateway_user_could_write_is_refused(tmp_path, monkeypatch):
    """The interpreter a ``#!`` names is what runs the attested bytes and is not
    itself attested, so it is held to the same PROTECTED bar as the launcher: an
    interpreter the gateway user owns (a ``~/.nvm`` node, a user-site python) is
    one the agent, running as that user, could rewrite after verification."""
    from kiro_crew import github_runner
    from kiro_crew.acp.harness.routing_verification import executable_provenance_problem

    user_node = tmp_path / "nvm" / "bin" / "node"
    user_node.parent.mkdir(parents=True)
    user_node.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    launcher = _stub(tmp_path, name="user-interp", body=f"#!{user_node}\nconsole.log(1)\n")
    seen: list[tuple[str, bool]] = []

    def _refuse_the_interpreter(candidate, *, require_protected=False):
        seen.append((os.path.realpath(str(candidate)), require_protected))
        if os.path.realpath(str(candidate)) == os.path.realpath(str(user_node)):
            raise ValueError("not a protected install")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse_the_interpreter)
    problem = executable_provenance_problem(launcher)
    assert problem is not None and "interpreter provenance is refused" in problem
    assert "protected install" in problem
    # Both the launcher and its interpreter are asked the STRICT question.
    assert (os.path.realpath(str(user_node)), True) in seen
    assert (os.path.realpath(launcher), True) in seen
    # A protected interpreter keeps the launcher acceptable.
    assert executable_provenance_problem(_stub(tmp_path, name="sys-interp")) is None


@pytest.mark.asyncio
async def test_the_probe_marker_names_the_agent_the_probe_runs_under(tmp_path):
    from kiro_crew.acp.harness import routing_verification as rv

    seen = {}

    def build(session_key, cwd):
        seen["entry"] = rv._PROBING["acme"]
        seen["cwd"] = os.path.realpath(cwd)
        return _FakeProvider([LLMEvent(kind=EVENT_COMPLETE)], cwd=cwd)

    await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5, agent="reviewer")
    digest, scratch, agent = seen["entry"]
    assert scratch == seen["cwd"] and agent == "reviewer"
    assert "acme" not in rv._PROBING


@pytest.mark.asyncio
async def test_unrelated_permission_requests_are_denied_but_are_not_evidence(tmp_path):
    """A host that asks about OTHER things and never asks about the probe write
    has proven nothing about that write: inconclusive, nothing recorded -- and
    every request is still refused."""
    build, made = _builder(
        [_perm("r1", unrelated=True), _perm("r2", unrelated=True), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.permission_requests == 0
    assert result.details["unrelated_permission_requests"] == 2
    assert "none of them for the probe write" in result.reason
    assert made["provider"].rejected == ["r1", "r2"]
    # Unrelated asks plus the write landing is still the violation.
    build, _ = _builder(
        [_perm("r1", unrelated=True), LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True
    )
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_VIOLATION
    assert result.permission_requests == 0


@pytest.mark.asyncio
async def test_a_request_counts_when_it_names_the_probe_file_as_an_edit_or_a_shell_command(
    tmp_path,
):
    from kiro_crew.acp.harness.routing_verification import PROBE_FILE_NAME

    # Absolute path inside the probe cwd: the builder learns the cwd at build
    # time, so the event is made once the scratch dir is known.
    class _AbsBuilder:
        def __init__(self):
            self.made = {}

        def __call__(self, key, cwd):
            events = [
                _perm("r1", path=os.path.join(cwd, PROBE_FILE_NAME)),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
            self.made["provider"] = _FakeProvider(events, cwd=cwd)
            return self.made["provider"]

    result = await verify_routing(_descriptor(_stub(tmp_path)), _AbsBuilder(), timeout=5)
    assert result.permission_requests == 1
    assert result.details["permission_request_classes"] == ["edit"]
    # A shell host: the command line, parsed, writes the file.
    build, _ = _builder(
        [_perm("r1", shell=f"printf probe > {PROBE_FILE_NAME}"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.permission_requests == 1
    assert result.details["permission_request_classes"] == ["shell"]
    # A shell command about something else is not.
    build, _ = _builder([_perm("r1", shell="ls -la"), LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE and result.permission_requests == 0
    # Nor is one that merely MENTIONS the file without writing it: a denied
    # read is ordinary agent behaviour, not proof that writes reach the gate.
    for mention in (
        f"cat {PROBE_FILE_NAME}",
        f"ls -la {PROBE_FILE_NAME}",
        f"grep probe {PROBE_FILE_NAME}",
        f"echo 'will write {PROBE_FILE_NAME} later'",
        f"cat {PROBE_FILE_NAME} > /dev/null",
    ):
        build, _ = _builder([_perm("r1", shell=mention), LLMEvent(kind=EVENT_COMPLETE)])
        result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
        assert result.permission_requests == 0, mention


@pytest.mark.asyncio
async def test_a_read_of_the_probe_path_is_not_evidence_but_a_diff_block_edit_is(tmp_path):
    """The file-target check applies to the WRITE plane only (``is_edit_call``):
    a read tool call naming the probe file proves nothing about a write. An
    edit that names its target only in the tool_call's diff block still counts."""
    from kiro_crew.acp.harness.routing_verification import PROBE_FILE_NAME

    read_req = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r1",
        tool_kind="read",
        raw_tool_params={"path": PROBE_FILE_NAME},
    )
    build, made = _builder([read_req, LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE and result.permission_requests == 0
    assert result.details["unrelated_permission_requests"] == 1
    assert made["provider"].rejected == ["r1"]  # still denied
    # No declared kind, no path key -- the diff content block names the target.
    diff_req = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r2",
        raw_tool_params={"content": "probe"},
        diff_path=PROBE_FILE_NAME,
    )
    build, _ = _builder([diff_req, LLMEvent(kind=EVENT_COMPLETE)])
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.permission_requests == 1
    assert result.details["permission_request_classes"] == ["edit"]


def test_shell_write_parser_recognises_writes_and_only_writes(tmp_path):
    from kiro_crew.acp.harness.routing_verification import PROBE_FILE_NAME, _shell_writes_path

    cwd = str(tmp_path)
    target = os.path.realpath(os.path.join(cwd, PROBE_FILE_NAME))
    abs_probe = os.path.join(cwd, PROBE_FILE_NAME)
    writes = [
        f"echo probe > {PROBE_FILE_NAME}",
        f"echo probe >{PROBE_FILE_NAME}",
        f"echo probe >> {PROBE_FILE_NAME}",
        f"echo probe 1> {PROBE_FILE_NAME}",
        f"printf probe &> '{abs_probe}'",
        f"echo probe | tee {PROBE_FILE_NAME}",
        f"echo probe | tee -a ./{PROBE_FILE_NAME}",
        f"touch {PROBE_FILE_NAME}",
        f"cp /etc/hostname {PROBE_FILE_NAME}",
        f"mv tmp.txt {abs_probe}",
        f"dd if=/dev/zero of={PROBE_FILE_NAME} bs=1 count=5",
        f"cd /tmp && echo probe > {abs_probe}",
        f"FOO=1 echo probe > {PROBE_FILE_NAME}",
    ]
    for cmd in writes:
        assert _shell_writes_path(cmd, cwd, target), cmd
    not_writes = [
        f"cat {PROBE_FILE_NAME}",
        f"ls {PROBE_FILE_NAME}",
        f"grep -n probe {PROBE_FILE_NAME}",
        f"echo {PROBE_FILE_NAME}",
        "echo probe > other.txt",
        f"cp {PROBE_FILE_NAME} elsewhere.txt",  # the probe is the SOURCE
        "echo probe > /dev/null",
        f"python -c \"open('{PROBE_FILE_NAME}','w').write('probe')\"",  # not parseable here
        "echo 'unbalanced > " + PROBE_FILE_NAME,  # shlex cannot split
    ]
    for cmd in not_writes:
        assert not _shell_writes_path(cmd, cwd, target), cmd


@pytest.mark.asyncio
async def test_probe_refuses_to_attest_from_a_provider_for_another_backend(tmp_path):
    # The failure GPT named: an unselectable descriptor put through the per-chat
    # selection gate degrades to the configured default, which then asks for
    # permission -- and the wrong backend would be attested. The probe asserts
    # the provider's identity before any verdict.
    build, made = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], backend="")
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "provider for backend ''" in result.reason
    assert result.details == {"provider_backend": ""}
    assert made["provider"].shut_down is True


@pytest.mark.asyncio
async def test_probe_reports_a_violation_when_the_write_lands_despite_denial(tmp_path):
    exe = _stub(tmp_path)
    build, _ = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_VIOLATION
    assert result.probe_file_written is True
    assert "without going through the permission gate" in result.reason
    # A host that never asked AND wrote is the same verdict.
    build, _ = _builder([LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_VIOLATION


@pytest.mark.asyncio
async def test_probe_is_inconclusive_when_nothing_was_attempted_or_the_turn_failed(tmp_path):
    exe = _stub(tmp_path)
    build, _ = _builder(
        [LLMEvent(kind=EVENT_TEXT_CHUNK, text="I cannot"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.permission_requests == 0
    build, made = _builder([], raise_on_stream=RuntimeError("spawn failed"))
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "spawn failed" in result.reason
    assert made["provider"].shut_down is True

    def broken(session_key, cwd):
        raise RuntimeError("no such backend")

    result = await verify_routing(_descriptor(exe), broken, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "no such backend" in result.reason


@pytest.mark.asyncio
async def test_probe_holds_the_spawn_allowance_only_while_running(tmp_path):
    from kiro_crew.acp.harness import routing_verification as rv

    seen = {}

    def build(session_key, cwd):
        seen["probing"] = "acme" in rv._PROBING
        # The marker names the probe's scratch -- the cwd the provider is built
        # with -- so only the spawn from that directory is the probe's.
        seen["scratch_is_cwd"] = rv._PROBING["acme"][1] == os.path.realpath(cwd)
        return _FakeProvider([LLMEvent(kind=EVENT_COMPLETE)], cwd=cwd)

    await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert seen["probing"] is True
    assert seen["scratch_is_cwd"] is True
    assert "acme" not in rv._PROBING


@pytest.mark.asyncio
async def test_facade_builds_the_descriptors_own_provider_and_promotes_only_on_verified(
    home, clean_registry, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.acp.harness import routing_verification as rv
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    assert "acme" not in b.selectable_backends()

    constructed = {}

    class _StubAcpProvider(_FakeProvider):
        def __init__(self, **kwargs):
            constructed.update(kwargs)
            super().__init__(
                constructed.pop("_events"), cwd=kwargs["work_dir"], backend=kwargs["acp_backend"]
            )

    def make(events):
        def _ctor(**kwargs):
            kwargs["_events"] = events
            return _StubAcpProvider(**kwargs)

        return _ctor

    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))

    monkeypatch.setattr(acp_mod, "AcpProvider", make([LLMEvent(kind=EVENT_COMPLETE)]))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_INCONCLUSIVE and out["selectable"] is False
    assert load_attestations() == {}
    # The facade built the DESCRIPTOR's provider directly, with the configured
    # default agent and sandbox -- no per-chat selection gate in the path.
    assert constructed["acp_backend"] == "acme"
    assert constructed["agent"] == "kirocrew"
    assert constructed["sandbox_mode"] == "auto"
    assert constructed["session_key"] == "backend-routing-probe:acme"

    monkeypatch.setattr(acp_mod, "AcpProvider", make(_both_classes_ask()))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_VERIFIED and out["selectable"] is True
    assert "acme" in b.selectable_backends()
    rec = load_attestations()["acme"]
    assert rec["fingerprint"] == descriptor_fingerprint(reg.registered_operator_descriptor("acme"))
    assert rec["executable_digest"] == rv.executable_digest(exe)
    assert rec["executable_path"] == exe

    with pytest.raises(ValueError):
        await facade.verify_operator_backend_routing("claude", cfg)  # a builtin has no claim


@pytest.mark.asyncio
async def test_a_binary_swapped_during_the_probe_is_inconclusive_and_records_nothing(
    home, clean_registry, tmp_path, monkeypatch
):
    """The verdict is bound to the bytes resolved BEFORE the run: a file replaced
    while the probe runs -- even by a well-behaved turn -- attests nothing, and the
    backend stays unverified."""
    from types import SimpleNamespace

    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))

    class _Swapping(_FakeProvider):
        def __init__(self, **kwargs):
            super().__init__(
                _both_classes_ask(),
                cwd=kwargs["work_dir"],
                backend=kwargs["acp_backend"],
            )

        async def start(self):
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexec replaced-while-probing\n")
            await super().start()

    monkeypatch.setattr(acp_mod, "AcpProvider", lambda **kw: _Swapping(**kw))
    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_INCONCLUSIVE
    assert "changed while the probe ran" in out["reason"]
    assert out["selectable"] is False
    assert load_attestations() == {}
    assert "acme" in reg.unverified_operator_harnesses()


@pytest.mark.asyncio
async def test_verified_but_policy_denied_reports_the_policy_and_stays_unselectable(
    home, clean_registry, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import kiro_crew.agent_backend_governance as gov
    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: backend != "acme")
    monkeypatch.setattr(
        acp_mod,
        "AcpProvider",
        lambda **kw: _FakeProvider(_both_classes_ask(), cwd=kw["work_dir"], backend="acme"),
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_VERIFIED
    assert out["selectable"] is False and out["policy_denied"] is True
    assert "agent_backend policy" in out["reason"]
    assert "acme" not in b.selectable_backends()
    assert load_attestations()["acme"]["executable_digest"]  # the evidence is kept
