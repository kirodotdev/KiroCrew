"""Who mints the per-session stub token and how it reaches the stub.

Minting and the injected ACP entry's env, the stub's register payload and its daemon
generation gate, the Crew runtime and providers that mint the token and push its claim,
and the shipped kiro-cli delivering the element's env without losing precedence.

gatewayd's claim-push and binding are pinned in
``test_mcp_gateway_stub_session_token_claim_binding.py``; where the token may and may not
go in ``test_mcp_gateway_stub_session_token.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import mcp_gateway_stub_session_token_helpers
import pytest
from mcp_gateway_stub_session_token_helpers import (
    PARENT_KEY,
    SUB_KEY,
    TOKEN_A,
    TOKEN_B,
    _attest,
    _claim_with_token,
    _close,
    _live_conn,
    _next_caller,
    _register_with_token,
    _stub_args,
)
from test_mcp_gateway_claim import (
    _ANCESTORS,
    _PID,
    _handle,
    _patch_env,
    _QueueReader,
    _RecordingWriter,
)
from test_mcp_gateway_session_inject import _PROBE_SCRIPT, REAL_CLI

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import claim as claim_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.daemon import connection as connection_mod
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.session_servers import (
    STUB_SESSION_TOKEN_ENV,
    attach_stub_session_token,
)
from kiro_crew.testing.wait import wait_until

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_clean_gateway_state = mcp_gateway_stub_session_token_helpers._clean_gateway_state

pytestmark = pytest.mark.xdist_group("mcp_gateway")


# ---------------------------------------------------------------------------
# Minting and the injected entry
# ---------------------------------------------------------------------------


def test_token_is_unguessable_and_unique() -> None:
    """A guessable token IS another session's identity: gatewayd hands a
    connection the session its token names."""
    tokens = {claim_mod.mint_stub_session_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        # hex over >= 128 bits of randomness (the module mints 256).
        assert len(token) >= 32
        assert int(token, 16) >= 0


def test_attach_names_every_stub_entry_of_one_session() -> None:
    entries = [
        {"name": "one", "command": "python", "args": [], "env": []},
        {"name": "two", "command": "python", "args": [], "env": [{"name": "X", "value": "1"}]},
    ]
    out = attach_stub_session_token(entries, TOKEN_A)
    for entry in out:
        assert {"name": STUB_SESSION_TOKEN_ENV, "value": TOKEN_A} in entry["env"]
    # The operator's own pair survives beside it.
    assert {"name": "X", "value": "1"} in out[1]["env"]
    # The caller's list may be a cached array shared with another session.
    assert entries[0]["env"] == [] and len(entries[1]["env"]) == 1


def test_attach_is_a_no_op_without_a_token() -> None:
    """A build with the gateway off, or a caller that cannot mint, keeps the
    pre-token wire shape byte for byte."""
    entries = [{"name": "one", "command": "python", "args": [], "env": []}]
    assert attach_stub_session_token(entries, "") is entries


def test_attach_replaces_rather_than_appends_a_second_token() -> None:
    """Two pairs of the same name leave which one wins to the child's env
    parser — so the entry must never carry two."""
    once = attach_stub_session_token(
        [{"name": "one", "command": "python", "args": [], "env": []}], TOKEN_A
    )
    twice = attach_stub_session_token(once, TOKEN_B)
    names = [pair["name"] for pair in twice[0]["env"]]
    assert names.count(STUB_SESSION_TOKEN_ENV) == 1
    assert twice[0]["env"][-1]["value"] == TOKEN_B


def test_stub_forwards_the_token_from_its_env(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _stub_args()
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    assert stub_mod.build_register_payload(args)["stub_session_token"] == TOKEN_A
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV)
    # Absent, the key is omitted entirely rather than sent empty: gatewayd's
    # PID-keyed behavior is selected by the field NOT being there.
    assert "stub_session_token" not in stub_mod.build_register_payload(args)


def test_the_token_is_not_a_pool_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-connection value in the PoolKey would give every session its own
    backend and pooling would silently stop."""
    args = _stub_args()
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    first = stub_mod.build_register_payload(args)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_B)
    second = stub_mod.build_register_payload(args)
    assert first["stub_session_token"] != second["stub_session_token"]
    assert PoolKey.from_register(first).stable_hash() == PoolKey.from_register(second).stable_hash()


def test_an_owned_control_plane_register_names_the_stubs_code_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Crew's own MCP target needs a daemon-generation compatibility gate.

    A third-party target keeps the wire shape it had before this field; otherwise
    upgrading Kiro Crew would unpool every unrelated server even when its own
    binary and protocol did not move.
    """
    from kiro_crew import code_fingerprint as fingerprint_mod

    monkeypatch.setattr(fingerprint_mod, "code_fingerprint", lambda: "stub-generation")
    owned = stub_mod._parse_args(
        [
            "--server",
            "kirocrew-core",
            "--agent",
            "cp-agent",
            "--target-command",
            "kirocrew",
            "--target-args",
            "mcp-core",
            "--work-dir",
            "/tmp",
            "--poolable",
        ]
    )
    assert stub_mod.build_register_payload(owned)["stub_code_fingerprint"] == "stub-generation"
    assert "stub_code_fingerprint" not in stub_mod.build_register_payload(_stub_args())


def test_register_generation_attestation_requires_managed_server_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third-party name cannot opt into the Crew daemon-generation gate by argv."""
    from kiro_crew import code_fingerprint as fingerprint_mod

    monkeypatch.setattr(fingerprint_mod, "code_fingerprint", lambda: "stub-generation")
    common_args = [
        "--agent",
        "cp-agent",
        "--target-command",
        "kirocrew",
        "--target-args",
        "mcp-core",
        "--work-dir",
        ".",
        "--poolable",
    ]
    owned = stub_mod.build_register_payload(
        stub_mod._parse_args(["--server", "kirocrew-core", *common_args])
    )
    third_party = stub_mod.build_register_payload(
        stub_mod._parse_args(["--server", "third-party", *common_args])
    )

    assert owned["stub_code_fingerprint"] == "stub-generation"
    assert "stub_code_fingerprint" not in third_party
    assert third_party["binary_version"] == owned["binary_version"]


def test_the_stubs_owned_subcommand_set_is_the_managed_server_table() -> None:
    """``_KIROCREW_MCP_SUBCOMMANDS`` is spelled out in ``stub_register.py`` so the stub's
    timed cold-start path never imports ``mcp_discovery``; this is the ratchet
    that keeps the copy honest.

    The set decides which stubs fold the code fingerprint into their pool key AND
    which run the daemon-generation check, so a managed server missing from it
    keeps attaching to a pre-fingerprint daemon after an upgrade. ``mcp-debug``
    and ``mcp-panel`` were missing when the check landed.
    """
    from kiro_crew import mcp_discovery

    assert stub_mod._KIROCREW_MCP_SUBCOMMANDS == frozenset(
        mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.values()
    )


@pytest.mark.parametrize(
    ("server", "subcommand"),
    [("kirocrew-debug", "mcp-debug"), ("kirocrew-panel", "mcp-panel")],
)
def test_the_caller_aware_opt_in_servers_name_the_stubs_code_generation(
    monkeypatch: pytest.MonkeyPatch, server: str, subcommand: str
) -> None:
    """Exactly the servers whose mounts refuse every call as ``identity_unattested``
    without the per-session attestation must run the generation check, or a
    pre-fingerprint daemon keeps serving them after an upgrade."""
    from kiro_crew import code_fingerprint as fingerprint_mod

    monkeypatch.setattr(fingerprint_mod, "code_fingerprint", lambda: "stub-generation")
    owned = stub_mod._parse_args(
        [
            "--server",
            server,
            "--agent",
            "cp-agent",
            "--target-command",
            "kirocrew",
            "--target-args",
            subcommand,
            "--work-dir",
            "/tmp",
            "--poolable",
        ]
    )
    payload = stub_mod.build_register_payload(owned)
    assert payload["stub_code_fingerprint"] == "stub-generation"
    assert payload["binary_version"].endswith("+stub-generation")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registered", "reason"),
    [
        ({"type": "registered"}, "did not report"),
        (
            {"type": "registered", "fingerprint": "older-daemon-generation"},
            "does not match",
        ),
    ],
    ids=["pre-fingerprint-daemon", "different-generation"],
)
async def test_an_owned_control_plane_falls_back_from_a_stale_daemon(
    monkeypatch: pytest.MonkeyPatch,
    registered: dict[str, Any],
    reason: str,
) -> None:
    """A package upgrade must not strand every session behind an old broker.

    Before this gate, the new stub accepted the old daemon's ``registered`` frame.
    That daemon forwarded a caller key but not the new per-session token, so the
    current MCP server's policy read answered ``identity_unattested`` and refused
    every tool call. Requesting fallback execs this session's direct server, whose
    own element carries the signed token and preserves the fail-closed policy gate.
    """
    reader = _QueueReader()
    writer = _RecordingWriter()
    reader.feed(registered)

    async def _connect(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(stub_mod.transport, "connect", _connect)
    payload = {
        "type": "register",
        "stub_uuid": "generation-probe",
        "stub_code_fingerprint": "current-stub-generation",
    }
    with pytest.raises(stub_mod.StaleGenerationError, match=reason) as excinfo:
        await stub_mod.handshake("ignored.sock", payload)
    # Still a FallbackRequestedError, so the cold-start caller degrades to its
    # per-session exec unchanged; the subclass exists so the reconnect path can
    # refuse it terminally instead of retrying it as an outage.
    assert isinstance(excinfo.value, stub_mod.FallbackRequestedError)


@pytest.mark.asyncio
async def test_an_owned_control_plane_accepts_the_matching_daemon_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _QueueReader()
    writer = _RecordingWriter()
    reader.feed({"type": "registered", "fingerprint": "same-generation"})

    async def _connect(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(stub_mod.transport, "connect", _connect)
    attached = await stub_mod.handshake(
        "ignored.sock",
        {
            "type": "register",
            "stub_uuid": "generation-probe",
            "stub_code_fingerprint": "same-generation",
        },
    )
    assert attached[0] is reader and attached[1] is writer


@pytest.mark.asyncio
async def test_registered_reply_names_the_daemons_code_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection_mod, "code_fingerprint", lambda: "daemon-generation")
    _patch_env(monkeypatch)
    reader = _QueueReader()
    writer = _RecordingWriter()
    reader.feed(_register_with_token(PARENT_KEY, TOKEN_A))
    reader.feed({"type": "unregister"})

    await _handle(reader, writer)

    registered = next(frame for frame in writer.frames if frame.get("type") == "registered")
    assert registered["fingerprint"] == "daemon-generation"


# ---------------------------------------------------------------------------
# The Crew side: who mints the token, and when the claim is pushed
# ---------------------------------------------------------------------------


def _bare_runtime(
    monkeypatch: pytest.MonkeyPatch,
    pid: int = 4242,
    published: list[tuple[str, str]] | None = None,
) -> Any:
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime(work_dir="/tmp")
    runtime._mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
    monkeypatch.setattr(type(runtime), "pid", property(lambda _self: pid))
    # ``_own_stub_session`` now also publishes the token's signed mapping (the
    # switch-free identity channel). Recorded rather than written: these are unit
    # tests of the naming/claim contract, and letting them touch the mapping
    # directory would make each one depend on a trust root it never set up.
    import kiro_crew.acp.runtime as rt_mod

    sink = published if published is not None else []
    monkeypatch.setattr(
        rt_mod, "publish_session_token", lambda token, key: sink.append((token, key))
    )
    return runtime


@pytest.mark.asyncio
async def test_the_runtime_names_the_session_before_its_stubs_can_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kiro-cli launches this session's stubs while it serves ``session/new``, so
    a claim sent afterwards races the register it exists to inform. The claim is
    pushed — and awaited — first, and the token it names is the one on the
    entries handed to that request."""
    import kiro_crew.acp.runtime as rt_mod

    sent: list[tuple[Any, ...]] = []

    async def _record(socket_path, pid, session_key, channel_id, token):
        sent.append((socket_path, pid, session_key, token))
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _record)
    runtime = _bare_runtime(monkeypatch)
    entries, token = await runtime._own_stub_session(
        [{"name": "one", "command": "python", "args": [], "env": []}], SUB_KEY
    )
    assert token
    assert {"name": STUB_SESSION_TOKEN_ENV, "value": token} in entries[0]["env"]
    assert sent == [("/tmp/kirocrew-gw.sock", 4242, SUB_KEY, token)]


@pytest.mark.asyncio
async def test_each_session_on_one_runtime_gets_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sessions sharing a kiro-cli process must not share the name that
    tells them apart."""
    import kiro_crew.acp.runtime as rt_mod

    async def _ok(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _ok)
    runtime = _bare_runtime(monkeypatch)
    entry = [{"name": "one", "command": "python", "args": [], "env": []}]
    _first, parent_token = await runtime._own_stub_session(list(entry), PARENT_KEY)
    _second, sub_token = await runtime._own_stub_session(list(entry), SUB_KEY)
    assert parent_token and sub_token and parent_token != sub_token


@pytest.mark.asyncio
async def test_an_unclaimed_worker_mints_a_token_but_pushes_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm-pool worker is spawned before any session claims it, so there is
    no owner to name yet — its ``rekey()`` carries the same token later."""
    import kiro_crew.acp.runtime as rt_mod

    sent: list[Any] = []

    async def _record(*a: Any, **k: Any) -> bool:
        sent.append(a)
        return True

    monkeypatch.setattr(rt_mod, "send_claim", _record)
    runtime = _bare_runtime(monkeypatch)
    entries, token = await runtime._own_stub_session(
        [{"name": "one", "command": "python", "args": [], "env": []}], ""
    )
    assert token
    assert {"name": STUB_SESSION_TOKEN_ENV, "value": token} in entries[0]["env"]
    assert sent == []


@pytest.mark.asyncio
async def test_no_stub_entries_still_names_the_session_but_pushes_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway off: nothing to CLAIM, but the session still gets a name.

    A token without a reachable gatewayd is not inert, because gatewayd is not its
    only reader: it also resolves through a MAC-signed mapping file
    (:mod:`kiro_crew.session_token_sig`) that the strict identity resolver reads
    with no daemon, no broker stub and no config switch. So a session on a
    gateway-less install — the default install — needs a name of its own, and
    withholding one is the identity gap this asserts against.

    The claim half is independent and is asserted too: with no stub entries there
    is no stub connection for a claim to inform, so no claim is pushed.
    """
    import kiro_crew.acp.runtime as rt_mod

    async def _boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("claimed a session with no stub connections to inform")

    monkeypatch.setattr(rt_mod, "send_claim", _boom)
    published: list[tuple[str, str]] = []
    runtime = _bare_runtime(monkeypatch, published=published)
    entries, token = await runtime._own_stub_session([], PARENT_KEY)
    assert entries == []
    assert token
    # ...and the name is published, or the resolver would have nothing to read.
    assert published == [(token, PARENT_KEY)]


def test_the_shared_runtime_rekey_claims_its_own_session_not_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm-pool re-claim on a runtime hosting subagents must reach only the
    claiming session's stubs — which is the token the handle carries."""
    import kiro_crew.acp.session_provider as sp_mod

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )

    class _Handle:
        stub_session_token = TOKEN_A

        def rebind_watchdog(self, *_a: Any, **_k: Any) -> None:
            pass

        def bind_session_key(self, _key: str) -> None:
            pass

        class last_prompt_stats:  # noqa: N801 - mirrors the real attribute name
            @staticmethod
            def reset_context_state() -> None:
                pass

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = 4242
        _crew_agent = ""
        _last_activity = 0.0

    provider = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    provider._handle = _Handle()
    provider._runtime = _Runtime()
    provider.rekey(PARENT_KEY, None, "", None)
    assert pushed == [(4242, PARENT_KEY, TOKEN_A)]


#: Every ``create_session`` call that opens a session on a runtime SHARED with
#: other sessions, and the local name holding that session's key. A session here
#: that names no owner carries a token no claim ever names, so its stubs resolve
#: to nothing — and before the token they resolved to whichever session the
#: shared runtime's process tree pointed at. Deleting one keyword argument
#: re-opens exactly that, which no behavioural test on another file would catch.
_SHARED_RUNTIME_SESSION_SITES = [
    ("src/kiro_crew/subagent_manager/run_start.py", "session_key=session_key"),
    ("src/kiro_crew/session_allocation.py", "session_key=key"),
]


@pytest.mark.parametrize("path,expected", _SHARED_RUNTIME_SESSION_SITES)
def test_sessions_on_a_shared_runtime_name_their_own_owner(path: str, expected: str) -> None:
    body = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    start = body.index("await runtime.create_session(")
    line = body[body.rindex("\n", 0, start) + 1 : start]
    indent = line[: len(line) - len(line.lstrip())]
    call = body[start : body.index("\n" + indent + ")", start)]
    assert expected in call, f"{path} opens a shared-runtime session without naming its owner"


def test_every_turn_re_pushes_the_sessions_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding lives in the daemon's memory, so it can be lost while the
    session is alive. The turn boundary that already rewrites the pid file is
    where it is re-established — reached through ``getattr`` so a provider
    without stubs is left alone."""
    import asyncio as _asyncio

    from kiro_crew.messaging import identity as identity_mod

    calls: list[str] = []

    class _Inner:
        def reclaim(self) -> None:
            calls.append("reclaimed")

    class _Provider:
        client = _Inner()

    class _Sessions:
        def get_pid(self, _key: str) -> None:
            return None

        def get_provider(self, _key: str) -> Any:
            return _Provider()

    _asyncio.run(identity_mod.publish_turn_identity(_Sessions(), PARENT_KEY))
    assert calls == ["reclaimed"]


def test_a_provider_without_stubs_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No stub token means no claim to push and nothing that would read one."""
    import asyncio as _asyncio

    from kiro_crew.messaging import identity as identity_mod

    class _Sessions:
        def get_pid(self, _key: str) -> None:
            return None

        def get_provider(self, _key: str) -> Any:
            return object()

    # Every step swallows its own error into a DEBUG line, so "left alone" means
    # no step reached one: an empty log, not merely no exception.
    with caplog.at_level(logging.DEBUG, logger=identity_mod.logger.name):
        _asyncio.run(identity_mod.publish_turn_identity(_Sessions(), PARENT_KEY))
    assert "failed" not in caplog.text


@pytest.mark.asyncio
async def test_warm_reuse_claims_the_fresh_sessions_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``new_conversation`` reuses the runtime and launches FRESH stubs, so it
    mints a fresh token that nothing has named. Left unclaimed, those stubs are
    refused — a warm worker would run its whole next task with no identity."""
    import kiro_crew.acp.session_provider as sp_mod

    class _Handle:
        def __init__(self, token: str = "") -> None:
            self.stub_session_token = token
            self.memory_mode = "persistent"
            self.model = ""
            self.session_id = "fresh"
            self.destroyed = False

        async def destroy(self) -> None:
            self.destroyed = True

    fresh = _Handle(TOKEN_B)

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        _work_dir = "/tmp/ws"
        _agent = "kirocrew"
        pid = 4242
        # ``new_conversation`` refuses a backend whose teardown does not evict the
        # old session, so it reads the backend before creating anything. ``""`` is
        # not "unset" here -- it is kiro's own id (``ACP_BACKEND_KIRO``), the real
        # runtime's default and a member of the eviction set. Warm reuse succeeding
        # IS this test's subject, so the stub has to carry it.
        acp_backend = ""

        def is_alive(self) -> bool:
            return True

        async def create_session(self, **_kwargs: Any) -> Any:
            return fresh

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((key, token)),
    )
    provider = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    provider._handle = _Handle(TOKEN_A)
    provider._runtime = _Runtime()
    provider._session_key = PARENT_KEY
    provider._channel_id = None
    await provider.new_conversation()

    assert provider._handle is fresh
    assert pushed == [(PARENT_KEY, TOKEN_B)]


@pytest.mark.asyncio
async def test_a_cold_started_session_can_re_bind_after_a_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold-start topology: a top-level kiro session that never rekeys.

    ``rekey()`` is a warm-pool event, so a pool miss, pooling switched off, or a
    subagent's own session reaches it never — and a provider that learned its key
    only there would re-claim with an EMPTY session key. gatewayd rejects that
    frame as malformed BEFORE recording the binding, so the token could never be
    re-bound: one daemon respawn and the session is identity-less for life. The
    key therefore arrives at construction, and this asserts the whole chain —
    empty key binds nothing, seeded key re-binds and re-targets the live stub.
    """
    import kiro_crew.acp.session_provider as sp_mod

    _attest(monkeypatch, _ANCESTORS)
    backend, reader, task = await _live_conn(monkeypatch, "", TOKEN_A, "stub-cold")
    assert backend.callers[0] is None

    class _Handle:
        stub_session_token = TOKEN_A

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = _PID

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )

    # What a provider that never learned its key would have pushed — and what
    # gatewayd does with it: nothing, not even the binding.
    keyless = sp_mod.AcpSessionProvider.__new__(sp_mod.AcpSessionProvider)
    keyless._handle = _Handle()
    keyless._runtime = _Runtime()
    keyless._session_key = ""
    keyless._channel_id = None
    keyless.reclaim()
    assert pushed == [(_PID, "", TOKEN_A)]
    ack = await gw._apply_claim(_claim_with_token(_PID, "", TOKEN_A))
    assert ack["type"] == "claim-rejected"
    assert gw._token_is_unbound(TOKEN_A)

    # The cold-start provider is handed its owner at construction instead.
    pushed.clear()
    seeded = sp_mod.AcpSessionProvider(
        _Handle(), _Runtime(), session_key=PARENT_KEY, channel_id="C_CP"
    )
    seeded.reclaim()
    assert pushed == [(_PID, PARENT_KEY, TOKEN_A)]
    ack = await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    assert ack["type"] == "claimed" and ack["updated"] == 1
    assert (await _next_caller(backend, reader)).session_key == PARENT_KEY
    await _close(reader, task)


#: Every construction of a provider over a session that will never rekey, and
#: the local name holding that session's key. Same failure mode as the
#: shared-runtime session sites: one deleted keyword argument and the provider's
#: re-claim carries no key, which gatewayd discards.
_PROVIDER_OWNER_SITES = [
    ("src/kiro_crew/providers/acp_runtime_start.py", "session_key=self._owning_session_key()"),
    ("src/kiro_crew/subagent_manager/run_start.py", "session_key=session_key"),
]


@pytest.mark.parametrize("path,expected", _PROVIDER_OWNER_SITES)
def test_every_provider_is_told_which_session_it_serves(path: str, expected: str) -> None:
    body = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    # The CONSTRUCTION, not a mention: the module also names the class in its
    # import and in prose about which client shape a slot carries.
    start = body.index("= AcpSessionProvider(")
    call = body[start : body.index("\n        )", start)]
    assert expected in call, f"{path} builds a provider that cannot name its own session"


@pytest.mark.asyncio
async def test_a_subagents_own_turn_re_establishes_its_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent's turn is driven by its manager, not by a dispatch surface, so
    the shared identity publisher never runs for it — and it is the session type
    this token exists to protect. Its provider re-claims at its own turn
    boundary, so a daemon respawn mid-run costs it one turn rather than the whole
    remaining run."""
    import kiro_crew.acp.session_provider as sp_mod

    class _Handle:
        stub_session_token = TOKEN_B
        session_id = "subagent-test-session"

        async def prompt(self, _message: str) -> Any:
            assert pushed == [(_PID, SUB_KEY, TOKEN_B)]
            for event in ():
                yield event

    class _Runtime:
        _mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
        pid = _PID
        process_instance = "runtime-test-instance"

        def saw_not_logged_in(self) -> bool:
            return False

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sp_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((pid, key, token)),
    )
    provider = sp_mod.AcpSessionProvider(_Handle(), _Runtime(), session_key=SUB_KEY)
    async for _event in provider.stream("go"):
        pass
    assert pushed == [(_PID, SUB_KEY, TOKEN_B)]


def test_the_client_reclaim_names_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.acp.client as client_mod

    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        client_mod,
        "schedule_claim",
        lambda socket_path, pid, key, channel, token: pushed.append((key, token)),
    )
    client = client_mod.AcpClient.__new__(client_mod.AcpClient)
    client._stub_session_token = TOKEN_A
    client._mcp_gateway_socket = "/tmp/kirocrew-gw.sock"
    client._session_key = PARENT_KEY
    client._channel_id = None
    client._process = None
    client.reclaim()
    assert pushed == [(PARENT_KEY, TOKEN_A)]
    # A client with no injected stubs pushes nothing: there is no token to name.
    pushed.clear()
    client._stub_session_token = ""
    client.reclaim()
    assert pushed == []


# ---------------------------------------------------------------------------
# The shipped binary: env on the injected element does not cost precedence
# ---------------------------------------------------------------------------

# flake-ok: the driver's poll and 1 s settle run in the kiro-cli child, bounded by
# communicate(timeout=180); moved verbatim; tracked for a de-flake pass
_TOKEN_DRIVER = r"""
import json, os, subprocess, sys, threading, time
from kiro_crew import platform_compat

w = sys.argv[1]
p = subprocess.Popen(["kiro-cli", "acp", "--agent", "pooltest"], cwd=w + "/proj",
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True,
                     env={**os.environ, "KIRO_HOME": w + "/khome"})


def teardown():
    try:
        platform_compat.kill_process_tree(p.pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        p.kill()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    for stream in (p.stdin, p.stdout, p.stderr):
        try:
            stream.close()
        except OSError:
            pass


send = lambda o: (p.stdin.write(json.dumps(o) + "\n"), p.stdin.flush())
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1, "clientCapabilities":
                 {"fs": {"readTextFile": False, "writeTextFile": False}}}})
_line = []
_t = threading.Thread(target=lambda: _line.append(p.stdout.readline()), daemon=True)
_t.start()
_t.join(30)
if not _line:
    teardown()
    sys.exit("kiro-cli never answered initialize within 30s")
send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": w + "/proj", "mcpServers": [
          {"name": "shared", "command": sys.executable,
           "args": [sys.argv[2], w + "/marks/INJECTED"],
           "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": sys.argv[3]}]}]}})
deadline = time.time() + 20
injected = os.path.join(w, "marks", "INJECTED")
while time.time() < deadline and not os.path.exists(injected):
    time.sleep(0.05)
time.sleep(1)
teardown()
"""

#: Writes the marker AND the token it observed in its own env, so the test can
#: tell "the element was honored" from "the element's env was honored".
_TOKEN_PROBE = _PROBE_SCRIPT.replace(
    'pathlib.Path(sys.argv[1]).write_text("x", encoding="utf-8")',
    "pathlib.Path(sys.argv[1]).write_text(\n"
    '    os.environ.get("KIROCREW_STUB_SESSION_TOKEN", ""), encoding="utf-8"\n'
    ")",
).replace("import json\nimport pathlib", "import json\nimport os\nimport pathlib")


@pytest.mark.skipif(not REAL_CLI, reason="kiro-cli not on PATH")
@pytest.mark.skipif(os.name != "posix", reason="POSIX pathing in fixture")
def test_real_kiro_cli_delivers_the_token_and_still_overrides_the_spec() -> None:
    """ANTI-DRIFT GUARD, the token half.

    ``test_real_kiro_cli_prefers_session_injected_server`` pins that a
    session-injected element outranks the agent spec's same-named entry — with
    an EMPTY ``env``. The token rides that element's ``env``, so this pins the
    two things that shape now depends on: the child receives the pair, and
    carrying it does not cost the precedence pooling relies on.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "khome" / "agents").mkdir(parents=True)
        (root / "proj").mkdir()
        (root / "marks").mkdir()
        probe = root / "probe.py"
        probe.write_text(_TOKEN_PROBE, encoding="utf-8")
        (root / "khome" / "agents" / "pooltest.json").write_text(
            json.dumps(
                {
                    "name": "pooltest",
                    "description": "token delivery probe",
                    "model": "claude-haiku-4.5",
                    "tools": [],
                    "prompt": "probe",
                    "mcpServers": {
                        "shared": {
                            "command": sys.executable,
                            "args": [str(probe), str(root / "marks" / "FROM_SPEC")],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        driver = root / "drive.py"
        driver.write_text(_TOKEN_DRIVER, encoding="utf-8")
        # Popen + a finally that reaps the whole TREE, not subprocess.run: the
        # driver spawns kiro-cli, which spawns the probe servers, and a
        # parent-side timeout would return from run() leaving all of them alive
        # holding the temp dir. The driver tears its own tree down on every exit
        # it controls; this covers the exit it does not.
        # kiro-cli writes its own log directory and telemetry spool under TMPDIR;
        # aimed at this tree, that residue is deleted with the test's directory
        # instead of outliving it in the shared temp root.
        child_tmp = root / "tmp"
        child_tmp.mkdir()
        child_env = {
            **os.environ,
            "TMPDIR": str(child_tmp),
            "TMP": str(child_tmp),
            "TEMP": str(child_tmp),
        }
        proc = subprocess.Popen(
            [sys.executable, str(driver), str(root), str(probe), TOKEN_A],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "driver exceeded its 180s budget"
        finally:
            if proc.poll() is None:
                try:
                    platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    proc.kill()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
                wait_until(
                    lambda: (root / "marks" / "INJECTED").exists(),
                    describe=lambda: (
                        f"driver exit: {proc.returncode}\n"
                        + f"driver stdout: {(stdout or '')[-2000:]}\n"
                        + f"driver stderr: {(stderr or '')[-2000:]}"
                    ),
                )
        assert (root / "marks" / "INJECTED").exists(), (
            "the injected server never launched with env on its element\n"
            f"driver exit: {proc.returncode}\n"
            f"driver stdout: {(stdout or '')[-2000:]}\n"
            f"driver stderr: {(stderr or '')[-2000:]}"
        )
        assert (root / "marks" / "INJECTED").read_text(encoding="utf-8") == TOKEN_A, (
            "the element's env never reached the child: the stub cannot report a "
            "token it was not given, and every session on a shared runtime would "
            "fall back to the parent's identity"
        )
        # flake-ok: absence read after the driver's settle; runs only with a real kiro-cli
        # on PATH; moved verbatim; tracked for a de-flake pass
        assert not (root / "marks" / "FROM_SPEC").exists(), (
            "the spec's same-named server ALSO launched: an element carrying env "
            "no longer overrides, so every pooled server would run twice"
        )
