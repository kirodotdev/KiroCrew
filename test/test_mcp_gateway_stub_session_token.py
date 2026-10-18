"""Per-session stub tokens: one runtime PID, one identity per ACP session.

Every identity channel the broker-stub path had before this token is keyed on
the PROCESS TREE — the stub's own ``KIROCREW_SESSION_KEY``/pid-file walk,
gatewayd's SO_PEERCRED ``/proc`` walk, and claim-push, which re-targets every
connection indexed under a runtime PID. One kiro-cli process hosts N ACP
sessions (``agent.session_sharing``: a ``spawn_run`` subagent runs on its
parent's process), so all three answer with the PARENT's session for a
subagent's stub, and a parent re-claim overwrites whatever the subagent had.

The token is the per-SESSION name that tree cannot supply. These tests pin:

* it is minted per session, rides the injected ACP entry's ``env``, and comes
  back on the Register frame WITHOUT entering the PoolKey digest,
* claim-push keyed by ``(pid, token)`` across all three topologies — no
  connection tokened, all tokened, mixed — and the isolation property that
  makes the whole item worth landing: a claim for session A leaves session B's
  stub on the same runtime alone,
* a claim carrying NO token still re-targets every connection under the PID,
  byte-for-byte as before, so a stub from a hand-written config or an older
  overlay is unaffected,
* the register path prefers the token binding over BOTH process-tree sources,
  and refuses a tree answer outright when a sibling session on the same runtime
  is named while this connection's token is not,
* the token never reaches a log record, ``stats()``, the stub fallback journal,
  or the prewarm file — it is a bearer name for a session's identity.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import json
import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import mcp_gateway_stub_session_token_helpers
import pytest
from test_mcp_gateway_claim import (
    _ANCESTORS,
    _CALL,
    _PID,
)
from test_mcp_gateway_claim import _FakeBackend as _FakeBackendType
from test_mcp_gateway_claim import (
    _FakePool,
    _handle,
    _patch_env,
    _QueueReader,
    _RecordingWriter,
    _register,
)

from kiro_crew import mcp_caller, platform_compat
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import prewarm as prewarm_mod
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.session_servers import (
    STUB_SESSION_TOKEN_ENV,
)
from kiro_crew.testing.wait import await_until, finished

pytestmark = pytest.mark.xdist_group("mcp_gateway")

# The constants, frame helpers and autouse reset shared with the sibling modules, bound
# here by name (pytest registers the fixture for this module's tests).
_attest = mcp_gateway_stub_session_token_helpers._attest
_claim_with_token = mcp_gateway_stub_session_token_helpers._claim_with_token
_clean_gateway_state = mcp_gateway_stub_session_token_helpers._clean_gateway_state
_close = mcp_gateway_stub_session_token_helpers._close
_live_conn = mcp_gateway_stub_session_token_helpers._live_conn
PARENT_KEY = mcp_gateway_stub_session_token_helpers.PARENT_KEY
_register_with_token = mcp_gateway_stub_session_token_helpers._register_with_token
_stub_args = mcp_gateway_stub_session_token_helpers._stub_args
TOKEN_A = mcp_gateway_stub_session_token_helpers.TOKEN_A


# ---------------------------------------------------------------------------
# The token is a bearer name: it must not be written anywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_log_record_or_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Anyone who can read a token can be the session it names, so it must not
    appear in a log line an operator pastes into a ticket, nor in the metrics
    snapshot the dashboard renders."""
    caplog.set_level(logging.DEBUG)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn(monkeypatch, PARENT_KEY, TOKEN_A, "stub-parent")
    await _close(reader, task)
    assert TOKEN_A not in caplog.text

    snapshot = json.dumps(_FakePoolStats().stats())
    assert TOKEN_A not in snapshot


class _FakePoolStats(_FakePool):
    """``BackendPool.stats()`` shape as the control plane returns it — the token
    is not one of its dimensions and must not become one."""

    def stats(self) -> dict[str, Any]:
        return {"backends": 1, "sessions": 2, "pool_labels": ["cp-agent:echo-mcp"]}


def test_the_token_never_reaches_the_stub_fallback_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The journal survives the process and is world-readable to the operator's
    own tooling; a degrading stub must not leave its session's name in it."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    monkeypatch.setattr(stub_mod, "_fallback_log_path", lambda: tmp_path / "stub_fallback.jsonl")
    stub_mod.log_fallback("handshake_timeout", "stub-parent", "cp-agent:echo-mcp", _stub_args())
    assert TOKEN_A not in (tmp_path / "stub_fallback.jsonl").read_text(encoding="utf-8")


def test_the_fallback_backend_never_inherits_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the gateway is unavailable the stub EXECs the operator's real backend
    in its own place, copying its environment wholesale. That environment holds
    this session's token, and the process about to inherit it is a third-party
    server binary which could later register with it and be answered as this
    session. Its own declared env is restored on that path; the token never was
    part of it.
    """
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    monkeypatch.setenv("PATH", str(tmp_path))
    declared = tmp_path / "env.json"
    declared.write_text(json.dumps({"REAL_SERVER_KEY": "abc"}), encoding="utf-8")
    args = stub_mod._parse_args(
        [
            "--server",
            "echo-mcp",
            "--agent",
            "cp-agent",
            "--target-command",
            "true",
            "--work-dir",
            str(tmp_path),
            "--env-file",
            str(declared),
        ]
    )

    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: list[str], _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit):
        stub_mod.fallback_exec(args)

    assert STUB_SESSION_TOKEN_ENV not in seen["env"]
    assert TOKEN_A not in json.dumps(seen["env"])
    # …while the server's OWN declared env still reaches it: this scrub must not
    # cost the fallback path the parity with a directly-launched server that is
    # its entire purpose.
    assert seen["env"]["REAL_SERVER_KEY"] == "abc"


def _fallback_args(tmp_path: Path, server: str, command: str, target_args: list[str]) -> Any:
    import base64

    return stub_mod._parse_args(
        [
            "--server",
            server,
            "--agent",
            "cp-agent",
            "--target-command",
            command,
            "--target-args-b64=" + base64.b64encode(json.dumps(target_args).encode()).decode(),
            "--work-dir",
            str(tmp_path),
        ]
    )


def _capture_fallback_env(monkeypatch: pytest.MonkeyPatch, args: Any) -> dict[str, str]:
    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: str, _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit):
        stub_mod.fallback_exec(args)
    return seen["env"]


def test_the_fallback_keeps_the_token_for_a_vetted_control_plane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kiro Crew's own control plane is what the token exists for. Without it a
    fallback kirocrew-core refuses every call identity_unattested wherever the
    kernel peer check cannot name the session (a runtime hosting several
    sessions, a TCP connection). It is kept only when gatewayd's own vetting
    accepts the target, and that vetting judges the env the child really gets,
    minus the token and minus Kiro Crew's pinned UTF-8 keys."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    for key, value in platform_compat._UTF8_PROCESS_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SOME_OTHER_VAR", "x")
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    judged: list[tuple[str, str, list[str], dict[str, str]]] = []

    def _vet(name: str, command: str, args: Any, *, env: Any = None, **_kw: Any) -> bool:
        judged.append((name, command, list(args), dict(env)))
        return True

    monkeypatch.setattr(gw, "_spawns_own_control_plane", _vet)
    monkeypatch.setattr(gw, "_user_site_holds_our_package", lambda: False)
    env = _capture_fallback_env(
        monkeypatch, _fallback_args(tmp_path, "kirocrew-core", "/opt/kc/bin/kirocrew", ["mcp-core"])
    )

    assert env[STUB_SESSION_TOKEN_ENV] == TOKEN_A
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    [(name, command, target_args, verdict_env)] = judged
    assert (name, command, target_args) == ("kirocrew-core", "/opt/kc/bin/kirocrew", ["mcp-core"])
    assert STUB_SESSION_TOKEN_ENV not in verdict_env
    assert not set(platform_compat._UTF8_PROCESS_ENV) & set(verdict_env)
    assert verdict_env["SOME_OTHER_VAR"] == "x"


def test_a_non_pinned_utf8_value_still_reaches_the_vetting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the exact pinned values are excused. Any other value for those keys is
    an overlay like any other ``PYTHON*`` key and must reach the fence."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    judged: list[dict[str, str]] = []

    def _vet(_name: str, _command: str, _args: Any, *, env: Any = None, **_kw: Any) -> bool:
        judged.append(dict(env))
        return False

    monkeypatch.setattr(gw, "_spawns_own_control_plane", _vet)
    env = _capture_fallback_env(
        monkeypatch, _fallback_args(tmp_path, "kirocrew-core", "/opt/kc/bin/kirocrew", ["mcp-core"])
    )
    assert judged[0]["PYTHONIOENCODING"] == "latin-1"
    assert STUB_SESSION_TOKEN_ENV not in env


def test_a_reserved_name_on_a_foreign_binary_does_not_get_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The server NAME is not proof. The real vetting refuses a reserved name whose
    command is not the managed invocation, so the fallback drops the token -- and
    says so on the stub's own stderr, naming the server, because the only other
    symptom is every policy-reading tool answering ``identity_unattested``."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    with caplog.at_level(logging.INFO, logger=stub_mod.logger.name):
        env = _capture_fallback_env(
            monkeypatch, _fallback_args(tmp_path, "kirocrew-core", sys.executable, ["mcp-core"])
        )
    assert STUB_SESSION_TOKEN_ENV not in env
    assert TOKEN_A not in json.dumps(env)
    (record,) = [r for r in caplog.records if "without the session token" in r.getMessage()]
    assert record.levelno == logging.WARNING and "'kirocrew-core'" in record.getMessage()
    # The real vetting ran to a verdict: this is a refusal, not a broken check.
    assert not [r for r in caplog.records if "vetting of" in r.getMessage()]
    assert TOKEN_A not in caplog.text


def test_a_refused_reserved_name_logs_the_vettings_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The fallback hands the vetting its ``denial`` collector and relays what it
    wrote, so the stderr line points at the condition that failed rather than
    leaving the operator to correlate it with the daemon logger's wording."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)

    def _refuse(*_a: Any, denial: Any = None, **_kw: Any) -> bool:
        assert denial is not None, "the fallback discards the vetting's reason"
        denial.append("spawned '/opt/evil' is not the spec's '/opt/kc/bin/kirocrew'")
        return False

    monkeypatch.setattr(gw, "_spawns_own_control_plane", _refuse)
    with caplog.at_level(logging.INFO, logger=stub_mod.logger.name):
        env = _capture_fallback_env(
            monkeypatch, _fallback_args(tmp_path, "kirocrew-core", "/opt/evil", ["mcp-core"])
        )
    assert STUB_SESSION_TOKEN_ENV not in env
    (record,) = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert record.levelno == logging.WARNING
    assert "'kirocrew-core'" in record.getMessage()
    assert "is not the spec's '/opt/kc/bin/kirocrew'" in record.getMessage()
    assert TOKEN_A not in caplog.text


def test_a_third_party_fallback_stays_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropping the token is the ordinary answer for a third-party server, which
    is most of them: the stub's own name precheck answers before the vetting is
    even imported, and the fallback adds no line of its own at INFO or above."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)
    with caplog.at_level(logging.DEBUG):
        env = _capture_fallback_env(
            monkeypatch, _fallback_args(tmp_path, "echo-mcp", sys.executable, ["--stdio"])
        )
    assert STUB_SESSION_TOKEN_ENV not in env
    assert not [
        r for r in caplog.records if r.name == stub_mod.logger.name and r.levelno >= logging.INFO
    ]
    assert not [r for r in caplog.records if "denied the session token" in r.getMessage()]


def test_the_fallback_precheck_covers_every_control_plane() -> None:
    """The stub decides "is this a Crew name" from the leaf ``mcp_cleanup`` set so
    a third-party fallback never imports gatewayd. That precheck must be a
    superset of the daemon's ``CONTROL_PLANE_BACKENDS``, or a control plane added
    to the daemon's set alone would be skipped here silently: no vetting, no
    token, no WARNING -- the exact tokenless shape this PR fixed."""
    from kiro_crew import mcp_cleanup

    assert stub_mod.KIROCREW_BIN_MCP_SERVERS is mcp_cleanup.KIROCREW_BIN_MCP_SERVERS
    assert gw.CONTROL_PLANE_BACKENDS <= frozenset(stub_mod.KIROCREW_BIN_MCP_SERVERS)


_FALLBACK_IMPORT_PROBE = r"""
import json, sys
from kiro_crew.mcp_gateway import stub

def _capture(*_a, **_k):
    raise SystemExit(0)

stub.os.execvpe = _capture
stub.platform_compat.IS_WINDOWS = False
args = stub._parse_args(
    ["--server", sys.argv[1], "--agent", "cp-agent", "--target-command", sys.argv[2],
     "--work-dir", sys.argv[3]]
)
try:
    stub.fallback_exec(args)
except SystemExit:
    pass
print(json.dumps({
    "stub": stub.__file__,
    "modules": sorted(m for m in sys.modules if m.startswith("kiro_crew.mcp_gateway.")),
}))
"""

_HEAVY = ("kiro_crew.mcp_gateway.gatewayd", "kiro_crew.mcp_gateway.daemon")


def _fallback_import_footprint(server: str, command: str, tmp_path: Path) -> list[str]:
    """The ``kiro_crew.mcp_gateway`` modules a fresh stub process holds after one
    fallback exec of *server*. A subprocess, because this test process imported
    gatewayd at collection and ``sys.modules`` here can prove nothing. The child
    is pinned to THIS checkout's package by absolute path: the harness's
    ``PYTHONPATH`` may be relative, and resolved from ``tmp_path`` it would hand
    the child whatever ``kiro_crew`` the interpreter has installed instead."""
    src_root = Path(stub_mod.__file__).resolve().parents[2]
    env = {**os.environ, STUB_SESSION_TOKEN_ENV: TOKEN_A}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root), *filter(None, [os.environ.get("PYTHONPATH", "")])]
    )
    proc = subprocess.run(
        [sys.executable, "-c", _FALLBACK_IMPORT_PROBE, server, command, str(tmp_path)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(report["stub"]).resolve() == Path(stub_mod.__file__).resolve(), report["stub"]
    return list(report["modules"])


def test_a_third_party_fallback_does_not_import_gatewayd(tmp_path: Path) -> None:
    """The fallback runs for EVERY stubbed server while gatewayd is down and the
    token is always in a stub's env, so a gatewayd import inside the vetting
    helper would be paid by every third-party fallback the vetting refuses on
    its first line -- the daemon's whole module graph plus its module-level SSL
    setup. The name precheck must answer first. The Crew-name run is the
    control: it shows the probe can see the import when it does happen."""
    third_party = _fallback_import_footprint("echo-mcp", sys.executable, tmp_path)
    assert not set(third_party) & set(_HEAVY), third_party
    assert "kiro_crew.mcp_gateway.stub" in third_party

    control_plane = _fallback_import_footprint("kirocrew-core", sys.executable, tmp_path)
    assert set(_HEAVY) <= set(control_plane), control_plane


def test_a_tokenless_fallback_never_consults_the_vetting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no token in the stub's env there is nothing to keep, so even a Crew
    name takes the plain exec path: no vetting, no gatewayd import."""
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)

    def _never(*_a: Any, **_kw: Any) -> bool:
        raise AssertionError("the vetting ran for a fallback that holds no token")

    monkeypatch.setattr(gw, "_spawns_own_control_plane", _never)
    env = _capture_fallback_env(
        monkeypatch, _fallback_args(tmp_path, "kirocrew-core", "/opt/kc/bin/kirocrew", ["mcp-core"])
    )
    assert STUB_SESSION_TOKEN_ENV not in env


def test_a_vetting_error_drops_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: a vetting that raises means "not ours" -- and is logged as a
    broken check, not a refusal, because a signature drift in the privately
    imported vetting would otherwise return every control-plane fallback to the
    tokenless behaviour with nothing on stderr."""
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN_A)

    def _boom(*_a: Any, **_kw: Any) -> bool:
        raise RuntimeError("config plane down")

    monkeypatch.setattr(gw, "_spawns_own_control_plane", _boom)
    with caplog.at_level(logging.INFO, logger=stub_mod.logger.name):
        env = _capture_fallback_env(
            monkeypatch,
            _fallback_args(tmp_path, "kirocrew-core", "/opt/kc/bin/kirocrew", ["mcp-core"]),
        )
    assert STUB_SESSION_TOKEN_ENV not in env
    (record,) = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert record.levelno == logging.WARNING
    assert "vetting of 'kirocrew-core' raised" in record.getMessage()
    assert record.exc_info is not None and "config plane down" in caplog.text
    assert not [r for r in caplog.records if "without the session token" in r.getMessage()]
    assert TOKEN_A not in caplog.text


def test_the_fallback_calls_the_real_vetting_in_the_shape_it_accepts(tmp_path: Path) -> None:
    """Import contract. The accept path cannot run in CI (it needs the managed
    binary), and every double above swallows keywords, so a signature drift in
    the privately imported vetting would leave this file green while the stub's
    real call raised ``TypeError`` into its fail-closed branch. The shape the stub
    uses -- positional name, command, args; keyword ``env``, ``work_dir``,
    ``denial`` -- must bind against the REAL function, reached the way the stub
    reaches it, and a real call in that shape must reach a verdict."""
    import inspect

    from kiro_crew.mcp_gateway.daemon import control_plane as control_plane_mod

    vetting = gw._spawns_own_control_plane
    assert vetting is control_plane_mod._spawns_own_control_plane
    denial: list[str] = []
    bound = inspect.signature(vetting).bind(
        "kirocrew-core",
        str(tmp_path / "not-the-launcher"),
        ["mcp-core"],
        env={},
        work_dir=str(tmp_path),
        denial=denial,
    )
    assert vetting(*bound.args, **bound.kwargs) is False
    assert denial, "the real vetting refused without writing the reason it was handed"

    user_site = gw._user_site_holds_our_package
    assert user_site is control_plane_mod._user_site_holds_our_package
    inspect.signature(user_site).bind()
    assert isinstance(user_site(), bool)


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_prewarm_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prewarming PERSISTS whole register payloads so the hottest PoolKeys can be
    respawned at startup. It only ever needs the pool dimensions, and a token on
    disk outlives the session it names."""
    hot = prewarm_mod.HotKeyStore(tmp_path / "hot_keys.json")
    backend, sel = _patch_env(monkeypatch)

    async def _get(_key: Any) -> None:
        return None

    pool = _FakePool()
    setattr(pool, "get", _get)
    reader = _QueueReader()
    reader.feed(_register_with_token(PARENT_KEY, TOKEN_A, stub_uuid="stub-parent"))
    reader.feed(_CALL)
    task = asyncio.create_task(
        finished(
            gw._handle_connection(
                reader,
                _RecordingWriter(),
                pool=pool,
                resolver=object(),
                socket_path=Path("/tmp/cp.sock"),
                hot_keys=hot,
            ),
            "hot_keys connection",
        )
    )
    await await_until(backend.forwarded)
    await _close(reader, task)

    payloads = hot.top_register_payloads(10)
    assert payloads, "the register was never recorded — this test proves nothing"
    assert TOKEN_A not in json.dumps(payloads)
    hot.flush()
    assert TOKEN_A not in (tmp_path / "hot_keys.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The token reaches Kiro Crew's OWN pooled control planes, and nothing else
# ---------------------------------------------------------------------------


async def _live_conn_for_server(
    monkeypatch: pytest.MonkeyPatch, server_name: str, *, control_plane: bool
) -> tuple[Any, Any, Any]:
    """A token-carrying, claimed connection whose stub fronts *server_name*.

    ``control_plane`` is what the spawn path decided from the resolved command;
    the handler reads that flag, never the name the stub registered.
    """
    backend, sel = _patch_env(monkeypatch)
    backend.control_plane = control_plane
    reader = _QueueReader()
    frame = _register_with_token(PARENT_KEY, TOKEN_A, stub_uuid=f"stub-{server_name}")
    frame["server_name"] = server_name
    reader.feed(frame)
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    return backend, reader, task


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", sorted(gw.CONTROL_PLANE_BACKENDS))
async def test_a_pooled_control_plane_is_handed_the_sessions_token(
    monkeypatch: pytest.MonkeyPatch, server_name: str
) -> None:
    """gatewayd spawns a pooled backend from its OWN environment, so the
    per-session token is not in that backend's env. ``kirocrew-core`` and
    ``kirocrew-cron`` post back to the gateway for the session they act for and
    must prove it with ``X-Session-Token``; the caller block is the only channel
    that can carry it to them."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, server_name, control_plane=True
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == TOKEN_A
    parsed = CallerContext.from_meta(mcp_caller.build_caller_meta(caller))
    assert parsed is not None and parsed.session_token == TOKEN_A
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_third_party_backend_never_sees_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anyone who can read the token can be the session it names. A pooled
    third-party server gets the caller's identity, never its bearer name."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, "echo-mcp", control_plane=False
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == ""
    assert "sessionToken" not in mcp_caller.build_caller_meta(caller)[mcp_caller.CALLER_META_KEY]
    await _close(reader, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", sorted(gw.CONTROL_PLANE_BACKENDS))
async def test_a_reserved_name_on_a_foreign_command_never_sees_the_token(
    monkeypatch: pytest.MonkeyPatch, server_name: str
) -> None:
    """The name in the register frame is the stub's claim; the spawn target
    resolves separately from the spec. A backend registered under a reserved
    name whose command was NOT ours is a third party wearing our name, and the
    token stays out of its caller block."""
    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    backend, reader, task = await _live_conn_for_server(
        monkeypatch, server_name, control_plane=False
    )
    caller = backend.callers[0]
    assert caller is not None and caller.session_key == PARENT_KEY
    assert caller.session_token == ""
    await _close(reader, task)


@pytest.mark.asyncio
async def test_a_respawned_replacement_is_judged_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control plane that dies mid-session is replaced by a fresh spawn, and
    the replacement is re-checked: one that fails the control-plane check gets
    a tokenless caller, both on the respawn's own probe and on every frame the
    session forwards afterwards. The base caller the handler holds for the
    connection never carries the token, so the token the dead backend was
    entitled to cannot ride along to whatever answers next."""
    from kiro_crew.mcp_gateway.backend import BackendGone

    class _DiesOnce(_FakeBackendType):
        control_plane = True

        async def forward_from_stub(
            self, _uuid: str, _msg: dict, caller: Any = None, tenant_nonce: str = ""
        ) -> None:
            self.callers.append(caller)
            self.forwarded.set()
            raise BackendGone("died")

    replacement = _FakeBackendType()
    replacement.control_plane = False
    handed_to_respawn: list[Any] = []

    async def _fake_respawn(*_args: Any, caller: Any = None, **_kw: Any) -> Any:
        handed_to_respawn.append(caller)
        return replacement, asyncio.Queue(), asyncio.create_task(asyncio.sleep(0))

    _attest(monkeypatch, _ANCESTORS)
    await gw._apply_claim(_claim_with_token(_PID, PARENT_KEY, TOKEN_A))
    _, sel = _patch_env(monkeypatch)
    dying = _DiesOnce()

    async def _acquire_dying(_pool: Any, _key: Any, _resolver: Any, **_kw: Any) -> Any:
        return dying, True

    monkeypatch.setattr(gw, "_acquire_backend", _acquire_dying)
    monkeypatch.setattr(gw, "_respawn_backend_for_stub", _fake_respawn)
    reader = _QueueReader()
    frame = _register_with_token(PARENT_KEY, TOKEN_A, stub_uuid="stub-respawn")
    frame["server_name"] = "kirocrew-core"
    reader.feed(frame)
    reader.feed(_CALL)
    reader.feed(dict(_CALL, id=2))
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(replacement.forwarded)

    # The dead control plane was entitled to the token ...
    assert dying.callers[0] is not None and dying.callers[0].session_token == TOKEN_A
    # ... the respawn was handed the tokenless base, so its probe of the
    # replacement cannot leak it ...
    assert handed_to_respawn and handed_to_respawn[0].session_token == ""
    # ... and the replacement that failed the check never sees it either.
    assert replacement.callers[0] is not None
    assert replacement.callers[0].session_key == PARENT_KEY
    assert replacement.callers[0].session_token == ""
    await _close(reader, task)


def test_the_token_is_attached_per_backend_never_to_the_base_caller() -> None:
    """``_caller_for_backend`` is the single place the token joins a caller: a
    control plane gets a token-bearing COPY, anything else gets the base back
    unchanged, and the base itself is never mutated."""
    from types import SimpleNamespace

    base = CallerContext(session_key=PARENT_KEY)
    conn = SimpleNamespace(stub_session_token=TOKEN_A)
    ours = SimpleNamespace(control_plane=True, control_plane_denial="")
    theirs = SimpleNamespace(control_plane=False, control_plane_denial="")

    handed = gw._caller_for_backend(ours, base, conn)  # type: ignore[arg-type]
    assert handed is not None and handed.session_token == TOKEN_A
    assert handed is not base and base.session_token == ""
    assert gw._caller_for_backend(theirs, base, conn) is base  # type: ignore[arg-type]
    assert gw._caller_for_backend(ours, base, None) is base  # type: ignore[arg-type]
    assert gw._caller_for_backend(ours, None, conn) is None  # type: ignore[arg-type]
    assert (
        gw._caller_for_backend(ours, base, SimpleNamespace(stub_session_token=""))  # type: ignore[arg-type]
        is base
    )


def test_control_plane_backends_contain_session_mcp_and_justify_the_difference() -> None:
    """gatewayd names the set itself so the daemon does not import
    ``kiro_crew.agent`` at boot, and this pin is what stops the two copies
    drifting -- the two sets are not EQUAL, so it pins the RELATIONSHIP.

    ``CONTROL_PLANE_SERVERS`` decides which servers every session mounts and which
    survive a ``disabledTools`` entry; ``CONTROL_PLANE_BACKENDS`` decides who is
    handed a bearer token. Containment holds in one direction only: a server
    mounted in every session posts back for that session, so it needs the token.
    The reverse does not, and the opt-in servers are why -- each posts back for
    the CALLING session, so it needs the token, but naming one in the first set
    would mount it everywhere and make an operator's decision to switch its
    tools off unenforceable.

    The extras are pinned BY NAME to exactly the opt-in managed servers plus the
    spec-gated ``kirocrew-computer`` and the platform guide set, and each is also
    checked BY PROPERTY. The guide set is emitted into every default spec but is
    still a spec's to withhold (an operator or a policy can take it off), so it
    is not mounted regardless of the spec the way the control plane is. The name
    pin makes a new recipient an explicit, reviewable change; the property check
    stops a typo'd or third-party name from being handed a token even if someone
    edits the pin. The whole set is also pinned equal to
    ``acp.session_mcp.IDENTITY_BOUND_SERVERS`` -- the kiro-backend element list that
    carries the same token -- so the two identity paths grant the same servers.
    """
    from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, IDENTITY_BOUND_SERVERS
    from kiro_crew.agent import _MANAGED_MCP_SERVERS
    from kiro_crew.mcp_cleanup import OPT_IN_BIN_MCP_SERVERS

    assert gw.CONTROL_PLANE_BACKENDS == frozenset(IDENTITY_BOUND_SERVERS)
    assert frozenset(CONTROL_PLANE_SERVERS) < gw.CONTROL_PLANE_BACKENDS

    token_only = gw.CONTROL_PLANE_BACKENDS - frozenset(CONTROL_PLANE_SERVERS)
    assert token_only == frozenset(OPT_IN_BIN_MCP_SERVERS) | {
        "kirocrew-computer",
        "kirocrew-guide",
        "kirocrew-mate",
    }, (
        "a new token recipient must be added to this pin in the same commit that adds it "
        "to mcp_cleanup's managed-server tuples"
    )
    from kiro_crew.acp.session_mcp import session_mcp_servers

    for name in sorted(token_only):
        spec = _MANAGED_MCP_SERVERS.get(name)
        assert isinstance(spec, dict), f"{name!r} is handed a token but is not a managed server"
        # Token-only is justified only for a server that is NOT unconditionally
        # mounted. Checked by behaviour for every recipient: a spec whose `tools`
        # does not name it withholds it, while a control-plane server would be
        # mounted regardless of the spec.
        withheld = {"mcpServers": {name: {"command": "kirocrew", "args": ["x"]}}, "tools": []}
        named = {**withheld, "tools": [f"@{name}"]}
        assert name not in [e.get("name") for e in session_mcp_servers("probe", spec=withheld)], (
            f"{name!r} is token-only but is mounted even when the spec does not name it; "
            "one mounted in every session belongs in CONTROL_PLANE_SERVERS as well"
        )
        assert name in [
            e.get("name") for e in session_mcp_servers("probe", spec=named)
        ], f"{name!r}: the withholding probe proves nothing if naming it does not mount it"


def test_the_control_plane_check_asks_for_an_opt_in_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check resolves the spec with ``include_opt_in=True``, or a token-only
    control plane can never be verified at all.

    Pinned as the CALL and not only its effect, because every double in this file
    now swallows keywords: dropping that argument at the call site would leave
    ``kirocrew-dashboard`` permanently unverifiable -- every tool of its answering
    409 -- with this whole file still green. That is the exact shape of the bug
    this pins against.
    """
    from kiro_crew import agent as agent_mod

    seen: list[dict[str, Any]] = []

    def _record(name: str, **kw: Any) -> None:
        seen.append({"name": name, **kw})
        return None

    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", _record)
    gw._spawns_own_control_plane("kirocrew-dashboard", "/bin/true", [], env={})

    assert seen, "the check resolved no spec for a name that IS in CONTROL_PLANE_BACKENDS"
    assert seen[0].get("include_opt_in") is True


class TestTokenOnlyControlPlane:
    """``kirocrew-dashboard`` is the one control plane that is ``opt_in``: it
    posts back to the gateway for the CALLING session, so it needs the token,
    but no spec writer auto-emits it. Every branch that lets the check see it is
    pinned in BOTH directions here -- the grant, and what the grant does not do."""

    NAME = "kirocrew-dashboard"

    def test_the_real_dashboard_entry_matches_itself(self) -> None:
        """No patching: whatever this install emits for the dashboard set must be
        recognised as ours, or the token is never handed over and every
        ``session_create`` / ``session_send`` answers 409."""
        from kiro_crew.agent import managed_mcp_spec_entry

        entry = managed_mcp_spec_entry(self.NAME, include_opt_in=True)
        assert entry is not None
        assert entry["args"][-1] == "mcp-dashboard"
        assert gw._spawns_own_control_plane(self.NAME, entry["command"], entry["args"], env={})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("LD_PRELOAD", "/tmp/x.so"),
            ("DYLD_INSERT_LIBRARIES", "/tmp/x.dylib"),
        ],
    )
    def test_the_real_dashboard_entry_rejects_native_loader_injection(
        self,
        key: str,
        value: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A managed argv cannot earn the token while its loader can replace code."""
        from kiro_crew.agent import managed_mcp_spec_entry

        entry = managed_mcp_spec_entry(self.NAME, include_opt_in=True)
        assert entry is not None
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not gw._spawns_own_control_plane(
                self.NAME,
                entry["command"],
                entry["args"],
                env={key: value},
            )
        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        assert f"child environment carries non-empty {key}" in record.message
        assert gw._spawns_own_control_plane(
            self.NAME,
            entry["command"],
            entry["args"],
            env={key: ""},
        )

    def test_control_plane_target_resolver_strips_inherited_native_loader_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The classifier receives no inherited loader channel to reject."""
        frame = _register(PARENT_KEY)
        frame["server_name"] = self.NAME
        key = PoolKey.from_register(frame)
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_DASHBOARD", "kirocrew mcp-dashboard")
        monkeypatch.setenv("LD_PRELOAD", "/host/preload.so")

        resolved = gw.env_target_resolver(key)

        assert resolved is not None
        _command, _args, env, _work_dir = resolved
        assert "LD_PRELOAD" not in env

        third_party = _register(PARENT_KEY)
        monkeypatch.setenv("MC_MCP_TARGET_ECHO_MCP", "echo-mcp --stdio")
        resolved = gw.env_target_resolver(PoolKey.from_register(third_party))
        assert resolved is not None
        _command, _args, env, _work_dir = resolved
        assert env["LD_PRELOAD"] == "/host/preload.so"

    def test_the_real_dashboard_entry_is_checked_not_trusted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Membership is necessary, never sufficient: the same binary/argv/env
        fences that guard the always-on planes deny a dashboard spawn that is not
        byte-for-byte the managed invocation, and each denial names its reason."""
        from kiro_crew.agent import managed_mcp_spec_entry

        entry = managed_mcp_spec_entry(self.NAME, include_opt_in=True)
        assert entry is not None
        other = tmp_path / "evil"
        other.write_text("#!/bin/sh\n")
        wrong_args = list(entry["args"][:-1]) + ["mcp-core"]
        cases = [
            (dict(command=str(other), args=entry["args"], env={}), "is not the spec's"),
            (dict(command=entry["command"], args=wrong_args, env={}), "differ from spec"),
            (
                dict(
                    command=entry["command"], args=entry["args"], env={"PYTHONPATH": str(tmp_path)}
                ),
                "child environment carries non-empty PYTHONPATH",
            ),
        ]
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            for kwargs, fragment in cases:
                caplog.clear()
                assert not gw._spawns_own_control_plane(self.NAME, **kwargs)
                (record,) = [r for r in caplog.records if "denied the session token" in r.message]
                assert f"'{self.NAME}'" in record.message and fragment in record.message

    def test_the_emission_question_still_says_no(self) -> None:
        """The flag is the control-plane check's, not the spec writers': without
        it the dashboard entry is still ``None``, so an opt-in server the user
        never granted is not resurrected by the same helper that now verifies it."""
        from kiro_crew.agent import managed_mcp_spec_entry

        assert managed_mcp_spec_entry(self.NAME) is None
        assert managed_mcp_spec_entry("not-a-managed-server", include_opt_in=True) is None

    def test_include_opt_in_keeps_a_closed_spec_gate_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag skips exactly ONE disqualifier. A closed ``spec_gate`` keeps a
        backend unspawned, so a spawn under that name is anomalous: the entry stays
        ``None`` under the flag, and a gate that raises reads as closed."""
        import kiro_crew.agent as agent_mod

        gate = {"open": False}
        invocation = (sys.executable, ["-m", "kiro_crew", "mcp-probe"])

        def _raise() -> bool:
            raise RuntimeError("keystone unreadable")

        spec: dict[str, Any] = {
            "invocation_fn": lambda: invocation,
            "opt_in": True,
            "spec_gate": lambda: gate["open"],
        }
        monkeypatch.setitem(agent_mod._MANAGED_MCP_SERVERS, "kirocrew-probe", spec)
        assert agent_mod.managed_mcp_spec_entry("kirocrew-probe", include_opt_in=True) is None
        gate["open"] = True
        resolved = agent_mod.managed_mcp_spec_entry("kirocrew-probe", include_opt_in=True)
        assert resolved is not None and resolved["args"] == invocation[1]
        assert agent_mod.managed_mcp_spec_entry("kirocrew-probe") is None
        monkeypatch.setitem(
            agent_mod._MANAGED_MCP_SERVERS,
            "kirocrew-probe",
            {**spec, "spec_gate": _raise},
        )
        assert agent_mod.managed_mcp_spec_entry("kirocrew-probe", include_opt_in=True) is None

    def test_a_gate_closed_dashboard_is_denied_the_token(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Through the production path: the real dashboard invocation, under a
        spec whose gate has shut, gets no token and the denial says why."""
        import kiro_crew.agent as agent_mod

        entry = agent_mod.managed_mcp_spec_entry(self.NAME, include_opt_in=True)
        assert entry is not None
        gated = {**agent_mod._MANAGED_MCP_SERVERS[self.NAME], "spec_gate": lambda: False}
        monkeypatch.setitem(agent_mod._MANAGED_MCP_SERVERS, self.NAME, gated)
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not gw._spawns_own_control_plane(
                self.NAME, entry["command"], entry["args"], env={}
            )
        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        assert "no managed spec entry resolves" in record.message

    def test_a_name_outside_the_set_is_logged_at_debug_not_as_a_denial(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two ``False`` exits are distinguishable in the log: a third-party
        name leaves one DEBUG line naming the set and no WARNING, while a reserved
        name that fails leaves the WARNING and never the DEBUG line. A control
        plane missing from the set is exactly the silent shape this pins against."""
        import kiro_crew.agent as agent_mod

        with caplog.at_level(logging.DEBUG, logger=gw.__name__):
            assert not gw._spawns_own_control_plane("echo-mcp", "/bin/true", [], env={})
            (record,) = [r for r in caplog.records if "not in CONTROL_PLANE_BACKENDS" in r.message]
            assert record.levelno == logging.DEBUG and "'echo-mcp'" in record.message
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

            caplog.clear()
            monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: None)
            assert not gw._spawns_own_control_plane("kirocrew-core", "/bin/true", [], env={})
            assert not [r for r in caplog.records if "not in CONTROL_PLANE_BACKENDS" in r.message]
            assert [r for r in caplog.records if r.levelno == logging.WARNING]


class TestSpawnsOwnControlPlane:
    """``_spawns_own_control_plane`` is decided from the exec'd command against
    the managed spec's invocation -- the same source the spec writer reads."""

    @pytest.fixture
    def managed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
        launcher = tmp_path / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        entry = {"command": str(launcher), "args": ["mcp-core"]}
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name, **_kw: dict(entry) if name == "kirocrew-core" else None,
        )
        return entry

    def test_the_managed_invocation_is_ours(self, managed: dict[str, Any]) -> None:
        assert gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env={}
        )

    def test_a_symlink_to_the_launcher_is_the_same_program(
        self, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(managed["command"])
        assert gw._spawns_own_control_plane("kirocrew-core", str(alias), ["mcp-core"], env={})

    def test_a_foreign_binary_under_the_reserved_name_is_not(
        self, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        other = tmp_path / "evil"
        other.write_text("#!/bin/sh\n")
        assert not gw._spawns_own_control_plane("kirocrew-core", str(other), ["mcp-core"])

    def test_our_binary_with_different_args_is_not(self, managed: dict[str, Any]) -> None:
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-cron"])
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core", "--extra"]
        )

    def test_a_name_outside_the_set_is_never_ours(self, managed: dict[str, Any]) -> None:
        assert not gw._spawns_own_control_plane("echo-mcp", managed["command"], ["mcp-core"])

    def test_a_denied_reserved_name_is_logged_with_the_failed_condition(
        self,
        managed: dict[str, Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legitimate install that trips the check otherwise presents only as
        every cron tool answering 403: the daemon log must name the backend and
        which condition failed (binary, args, shadowed root, or no spec)."""
        other = tmp_path / "evil"
        other.write_text("#!/bin/sh\n")
        shadow_root = tmp_path / "proj"
        (shadow_root / "kiro_crew").mkdir(parents=True)
        (shadow_root / "kiro_crew" / "__init__.py").write_text("")
        cases = [
            (dict(command=str(other), args=["mcp-core"]), "is not the spec's"),
            (dict(command=managed["command"], args=["mcp-cron"]), "differ from spec"),
            (
                dict(
                    command=managed["command"],
                    args=["mcp-core"],
                    env={"PYTHONPATH": str(shadow_root)},
                ),
                "child environment carries non-empty PYTHONPATH",
            ),
        ]
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            for kwargs, fragment in cases:
                caplog.clear()
                assert not gw._spawns_own_control_plane("kirocrew-core", **kwargs)
                (record,) = [r for r in caplog.records if "denied the session token" in r.message]
                assert "'kirocrew-core'" in record.message and fragment in record.message
            caplog.clear()
            import kiro_crew.agent as agent_mod

            monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: None)
            assert not gw._spawns_own_control_plane(
                "kirocrew-core", managed["command"], ["mcp-core"]
            )
            assert any("no managed spec entry" in r.message for r in caplog.records)
            # A name outside the set is not a denial of anything reserved: silent.
            caplog.clear()
            assert not gw._spawns_own_control_plane("echo-mcp", managed["command"], ["mcp-core"])
            assert not [r for r in caplog.records if "denied the session token" in r.message]

    def test_an_unresolvable_managed_entry_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, managed: dict[str, Any]
    ) -> None:
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: None)
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-core"])

    def test_the_real_managed_entry_matches_itself(self) -> None:
        """No patching: whatever this install emits for ``kirocrew-cron`` must be
        recognised as ours, or the fix that hands the token over is inert."""
        from kiro_crew.agent import managed_mcp_spec_entry

        entry = managed_mcp_spec_entry("kirocrew-cron")
        assert entry is not None
        assert gw._spawns_own_control_plane(
            "kirocrew-cron", entry["command"], entry["args"], env={}
        )

    def test_a_kiro_crew_package_beside_the_launcher_is_not_ours(
        self, managed: dict[str, Any]
    ) -> None:
        """Script form: the launcher's directory is ``sys.path[0]``."""
        beside = Path(managed["command"]).parent / "kiro_crew"
        beside.mkdir()
        (beside / "__init__.py").write_text("")
        assert not gw._spawns_own_control_plane("kirocrew-core", managed["command"], ["mcp-core"])

    def test_the_spawn_site_decides_off_the_event_loop(self) -> None:
        """The classifier imports ``kiro_crew.agent``, reads config and stats a
        fixed local root; the one production call must run in a thread."""
        import inspect
        import re

        source = inspect.getsource(gw)
        bare_calls = [
            m.start()
            for m in re.finditer(r"(?<!def )_spawns_own_control_plane\(", source)
            if not source[max(0, m.start() - 200) : m.start()]
            .rstrip()
            .endswith("await asyncio.to_thread(")
        ]
        assert not bare_calls, "the control-plane verdict is computed on the event loop"
        assert "await asyncio.to_thread(\n            _spawns_own_control_plane," in source

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [True, False], ids=["ours", "denied"])
    async def test_the_verdict_is_decided_before_the_child_exists(
        self, monkeypatch: pytest.MonkeyPatch, verdict: bool
    ) -> None:
        """The check reads the child's final env and fixed local root. Run
        after the spawn, foreign code could erase its own shadow before the
        parent looked and be handed the token; so the verdict is taken BEFORE
        ``spawn_backend`` forks. ``PYTHONSAFEPATH`` remains defense in depth,
        never part of the grant decision. A denied backend gets neither the
        flag nor the token."""
        from test_mcp_gateway_spawn_gate import _fake_backend, _register_frame

        from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

        order: list[str] = []
        spawned_env: dict[str, str] = {}
        backend = _fake_backend()

        def classify(
            server_name: str, command: str, args: Any, *, env: Any, work_dir: Any, denial: Any
        ) -> bool:
            assert server_name == "kirocrew-cron" and command == "kirocrew" and args == ["mcp-cron"]
            assert "PYTHONSAFEPATH" not in env, "the check sees the env the child would get"
            order.append("verdict")
            return verdict

        async def fake_spawn(**kwargs: Any) -> Any:
            order.append("spawn")
            spawned_env.update(kwargs["env"])
            return backend

        monkeypatch.setattr(gw, "_spawns_own_control_plane", classify)
        monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
        monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (dict(env), []))
        key = PoolKey.from_register(_register_frame(server_name="kirocrew-cron"))
        got, was_spawned = await gw._acquire_backend(
            BackendPool(max_backends=4),
            key,
            lambda k: ("kirocrew", ["mcp-cron"], {}, k.work_dir),
        )

        assert got is backend and was_spawned
        assert order == ["verdict", "spawn"], "the verdict must precede the fork"
        assert backend.control_plane is verdict
        assert (spawned_env.get("PYTHONSAFEPATH") == "1") is verdict

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("server_name", "command", "args"),
        [("kirocrew-cron", "kirocrew", ["mcp-cron"]), ("echo-mcp", "echo-server", ["--stdio"])],
        ids=["control-plane", "third-party"],
    )
    async def test_utf8_pinning_is_applied_after_the_verdict_for_every_backend(
        self, monkeypatch: pytest.MonkeyPatch, server_name: str, command: str, args: list[str]
    ) -> None:
        """The classifier is handed a ``PYTHON*``-free environment and the child
        still receives Kiro Crew's UTF-8 pair. Re-asserting the pair before the
        verdict would deny every control plane its own token; dropping it would
        let a pooled interpreter build stdio from a Windows ANSI codepage."""
        from test_mcp_gateway_spawn_gate import _fake_backend, _register_frame

        from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
        from kiro_crew.platform_compat import _UTF8_PROCESS_ENV

        classifier_env: dict[str, str] = {}
        spawned_env: dict[str, str] = {}
        backend = _fake_backend()

        def classify(
            name: str, cmd: str, argv: Any, *, env: Any, work_dir: Any, denial: Any
        ) -> bool:
            classifier_env.update(env)
            return name == "kirocrew-cron"

        async def fake_spawn(**kwargs: Any) -> Any:
            spawned_env.update(kwargs["env"])
            return backend

        monkeypatch.setattr(gw, "_spawns_own_control_plane", classify)
        monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
        monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (dict(env), []))
        key = PoolKey.from_register(_register_frame(server_name=server_name))
        resolved_env = {"PATH": "/usr/bin"}
        got, _ = await gw._acquire_backend(
            BackendPool(max_backends=4),
            key,
            lambda k: (command, list(args), dict(resolved_env), k.work_dir),
        )

        assert got is backend
        assert not [k for k in classifier_env if k.upper().startswith("PYTHON")]
        for env_key, value in _UTF8_PROCESS_ENV.items():
            assert spawned_env[env_key] == value
        assert backend.control_plane is (server_name == "kirocrew-cron")

    def test_utf8_pinning_keeps_the_real_fence_on_our_side(self, managed: dict[str, Any]) -> None:
        """The pair the spawn site adds is exactly what the real classifier
        would deny, which is why it is added only after the verdict."""
        from kiro_crew.platform_compat import _UTF8_PROCESS_ENV

        assert gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env={}
        )
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", managed["command"], ["mcp-core"], env=dict(_UTF8_PROCESS_ENV)
        )


class TestModuleFormShadowing:
    """A managed ``<python> -m kiro_crew <sub>`` still needs a token fence.

    The fixed CWD or launcher root must not shadow this process's package.
    The complete Python environment namespace denies the token without
    modelling interpreter flags, path entries, archive loaders, or future
    import semantics.
    """

    SUB = "mcp-cron"

    @pytest.fixture
    def module_entry(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        entry = {
            "command": sys.executable,
            "args": ["-s", "-P", "-m", "kiro_crew", self.SUB],
        }
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name, **_kw: dict(entry) if name == "kirocrew-cron" else None,
        )
        return entry

    def _ours(self, entry: dict[str, Any], **kw: Any) -> bool:
        return gw._spawns_own_control_plane("kirocrew-cron", entry["command"], entry["args"], **kw)

    def test_a_clean_project_cwd_is_ours(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        (tmp_path / "README.md").write_text("not a package")
        assert self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_a_project_carrying_its_own_kiro_crew_package_is_not(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    # The extension suffix is the platform's own (``.so`` / ``.pyd``): the
    # classifier asks the import system for its suffixes, so the test must too.
    @pytest.mark.parametrize("suffix", [".py", ".pyc", importlib.machinery.EXTENSION_SUFFIXES[-1]])
    def test_a_project_carrying_a_kiro_crew_module_file_is_not(
        self, module_entry: dict[str, Any], tmp_path: Path, suffix: str
    ) -> None:
        (tmp_path / f"kiro_crew{suffix}").write_bytes(b"")
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_a_namespace_directory_does_not_shadow(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        """No ``__init__.py``: the import system scans past it to the real package."""
        (tmp_path / "kiro_crew").mkdir()
        assert self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_our_own_checkout_as_cwd_is_ours(self, module_entry: dict[str, Any]) -> None:
        """A dev gateway running from ``src/`` with ``src/`` as the project CWD
        loads the very package this process runs -- that is not a shadow."""
        import kiro_crew

        src = Path(kiro_crew.__file__).resolve().parent.parent
        assert self._ours(module_entry, env={}, work_dir=src)

    def test_pythonpath_can_shadow_too(self, module_entry: dict[str, Any], tmp_path: Path) -> None:
        clean = tmp_path / "project"
        clean.mkdir()
        foreign = tmp_path / "lib"
        (foreign / "kiro_crew").mkdir(parents=True)
        (foreign / "kiro_crew" / "__init__.py").write_text("")
        assert self._ours(module_entry, env={}, work_dir=clean)
        assert not self._ours(module_entry, env={"PYTHONPATH": str(foreign)}, work_dir=clean)
        # Relative values are non-empty too; no child-root model is consulted.
        assert not self._ours(module_entry, env={"PYTHONPATH": "lib"}, work_dir=tmp_path)
        # An empty variable adds no root and remains allowed.
        assert self._ours(module_entry, env={"PYTHONPATH": ""}, work_dir=clean)

    def test_any_non_empty_pythonpath_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        clean = tmp_path / "clean"
        clean.mkdir()
        assert not self._ours(module_entry, env={"PYTHONPATH": str(clean)}, work_dir=clean)

    def test_a_zip_pythonpath_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        """A zipimport package bypassed the old directory-and-suffix scan."""
        archive = tmp_path / "foreign.egg"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("kiro_crew/__init__.py", "")
        assert not self._ours(module_entry, env={"PYTHONPATH": str(archive)}, work_dir=tmp_path)

    def test_any_non_empty_pythonhome_denies_the_token(
        self, module_entry: dict[str, Any], tmp_path: Path
    ) -> None:
        home = tmp_path / "python-home"
        home.mkdir()
        assert not self._ours(module_entry, env={"PYTHONHOME": str(home)}, work_dir=tmp_path)
        assert self._ours(module_entry, env={"PYTHONHOME": ""}, work_dir=tmp_path)

    def test_pythonuserbase_denies_and_names_the_variable(
        self,
        module_entry: dict[str, Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not self._ours(
                module_entry,
                env={"PYTHONUSERBASE": str(tmp_path / "foreign-user-site")},
                work_dir=tmp_path,
            )

        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        assert "child environment carries non-empty PYTHONUSERBASE" in record.message

    @pytest.mark.parametrize(
        "key",
        ["PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHON_FUTURE_IMPORT_ROOT"],
    )
    def test_python_namespace_additions_fail_closed(
        self, module_entry: dict[str, Any], tmp_path: Path, key: str
    ) -> None:
        assert not self._ours(module_entry, env={key: "foreign"}, work_dir=tmp_path)
        assert self._ours(module_entry, env={key: ""}, work_dir=tmp_path)

    def test_a_foreign_package_in_per_user_site_denies_the_token(
        self,
        module_entry: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Per-user site-packages needs no variable to be on the child's path.

        The interpreter adds it from a default location ahead of the install's
        own site-packages, so removing ``PYTHONUSERBASE`` relocates it rather
        than disabling it and ``PYTHONSAFEPATH`` does not cover it either.
        """
        user_site = tmp_path / "user-site"
        (user_site / "kiro_crew").mkdir(parents=True)
        (user_site / "kiro_crew" / "__init__.py").write_text("")
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))

        project = tmp_path / "project"
        project.mkdir()
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not self._ours(module_entry, env={}, work_dir=project)

        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        # The reason quotes the root with ``!r``, which doubles the backslashes
        # of a Windows path, so compare against the repr rather than the str.
        assert repr(str(user_site)) in record.message

    def test_a_user_install_keeps_its_token(
        self, module_entry: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``--user`` install holds the real package in user-site, not a foreign one.

        Disabling user-site would break that shape outright, so the check must
        compare the package it finds rather than refuse the root's existence.
        """
        ours = Path(gw.kiro_crew.__file__).resolve().parent
        user_site = tmp_path / "user-site"
        user_site.mkdir()
        (user_site / "kiro_crew").symlink_to(ours, target_is_directory=True)
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))

        project = tmp_path / "project"
        project.mkdir()
        assert self._ours(module_entry, env={}, work_dir=project)

    def test_an_unresolvable_user_site_fails_closed(
        self, module_entry: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("no user base")

        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", _boom)
        assert not self._ours(module_entry, env={}, work_dir=tmp_path)

    def test_user_site_is_switched_off_when_nothing_there_is_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``.pth`` entry runs code at startup under any filename.

        Inspecting user-site for a foreign package cannot see a startup hook, so
        the surface is removed instead when nothing legitimate occupies it.
        """
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(tmp_path / "elsewhere"))
        assert gw._user_site_holds_our_package() is False

    def test_user_site_stays_on_for_a_user_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its own package lives there; switching user-site off would break it."""
        user_site = tmp_path / "user-site"
        user_site.mkdir()
        (user_site / "kiro_crew").symlink_to(
            Path(gw.kiro_crew.__file__).resolve().parent, target_is_directory=True
        )
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", lambda: str(user_site))
        assert gw._user_site_holds_our_package() is True

    def test_an_unresolvable_user_site_leaves_the_child_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unresolvable must not silently switch user-site off for the child."""

        def _boom() -> str:
            raise RuntimeError("no user base")

        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", True)
        monkeypatch.setattr(gw.site, "getusersitepackages", _boom)
        assert gw._user_site_holds_our_package() is True

    def test_disabled_user_site_needs_no_further_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw.site, "ENABLE_USER_SITE", False)
        assert gw._user_site_holds_our_package() is False

    def test_interpreter_flags_do_not_relax_the_token_fence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``-P``/``-I`` semantics may change; the token fence stays stricter."""
        entry = {"command": sys.executable, "args": ["-P", "-s", "-m", "kiro_crew", self.SUB]}
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(entry))
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")
        assert not self._ours(entry, env={}, work_dir=tmp_path)
        assert not self._ours(entry, env={"PYTHONPATH": str(tmp_path)}, work_dir=tmp_path)
        isolated = {"command": sys.executable, "args": ["-I", "-m", "kiro_crew", self.SUB]}
        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(isolated))
        assert not self._ours(isolated, env={"PYTHONPATH": str(tmp_path)}, work_dir=tmp_path)
