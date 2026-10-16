"""The MCP stub's facade: the process code it keeps, and the contract its owners meet.

``kiro_crew.mcp_gateway.stub`` keeps the stub process -- its argv, its signal handlers,
the warm-pool caller repair and ``_amain``, which drives the ``stub_*`` owner modules
from the first connect to the exit -- and serves every other name from the owner that
defines it (``kiro_crew._facade_mirror``). This file pins:

* ``_amain``'s control flow on each way out it has, with the owners it drives replaced
  through the facade, the same seam the stub's other tests patch;
* ``main`` and the signal handlers;
* the logger: the stub logs under the name its facade's ``__name__`` gives -- ``__main__``
  when it runs as ``python -m kiro_crew.mcp_gateway.stub``, the dotted name when it is
  imported -- and a second import of the facade under its dotted name inside such a
  process, which gatewayd's import performs on the fallback vetting path, finds every
  name the family shares bound to one object;
* the read ceiling, which ``_adopt_read_ceiling`` rebinds at run time, is bound in
  ``stub_link`` alone, beside every reader of it;
* every name an owner imports from the rest of ``kiro_crew`` and the facade serves is
  named in the facade's ``TYPE_CHECKING`` block, where type checkers read it.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.mcp_gateway import stub, stub_link
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_OWNERS = tuple(importlib.import_module(name) for name in stub._STUB_OWNERS)
_SRC = Path(__file__).resolve().parents[1] / "src"


class _Exited(Exception):
    """What the fake ``fallback_exec`` raises: the real one never returns."""


class _Run:
    """One ``_amain`` run with every owner it drives replaced through the facade."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.registered: dict[str, Any] = {"capabilities": ["poolable_ack"]}
        self.payload: dict[str, Any] = {"stub_uuid": "uuid-1", "session_key": "chat-1"}
        self.handshake_error: BaseException | None = None
        self.admission: tuple[str, Any] = (stub._ADMIT_REJECTED, {})
        #: What each ``run_bridge`` call leaves on the session, in call order.
        self.bridges: list[dict[str, Any]] = [{"reason": "stdin_eof"}]
        self.reattach: list[Any] = []
        self.payload_error: BaseException | None = None
        self.recaller_started = asyncio.Event()
        self.reconnect_payloads: list[dict[str, Any]] = []
        #: Every payload ``build_register_payload`` returned, a fresh object each time.
        self.built: list[dict[str, Any]] = []
        self.argv = [
            "--server",
            "srv",
            "--agent",
            "agent-a",
            "--target-command",
            sys.executable,
            "--work-dir",
            str(tmp_path),
            "--socket",
            str(tmp_path / "gw.sock"),
        ]
        events = self.events

        # Process-wide side effects _amain makes before its own control flow.
        monkeypatch.setattr(stub.logging, "basicConfig", lambda **kwargs: None)
        monkeypatch.setattr(stub, "configure_default_executor", lambda: None)
        monkeypatch.setattr(
            stub, "_install_signal_handlers", lambda loop, stop: events.append(("signals",))
        )
        monkeypatch.setattr(stub, "_adopt_read_ceiling", lambda args: events.append(("ceiling",)))

        def build_register_payload(args: argparse.Namespace) -> dict[str, Any]:
            if self.payload_error is not None and any(e[0] == "bridge" for e in events):
                raise self.payload_error
            events.append(("payload", args.server))
            self.built.append(dict(self.payload))
            return self.built[-1]

        async def handshake(socket_path: str, payload: dict[str, Any]) -> tuple[Any, ...]:
            events.append(("handshake", socket_path))
            if self.handshake_error is not None:
                raise self.handshake_error
            return ("reader-1", "writer-1", "uuid-1", self.registered)

        async def alog_fallback(reason, stub_uuid, pool_label, args, *, terminal=False):
            events.append(("alog", reason, stub_uuid, terminal))

        def fallback_exec(args: argparse.Namespace) -> None:
            events.append(("exec", args.server))
            raise _Exited

        async def safe_close(writer: Any) -> None:
            events.append(("close", writer))

        async def ensure_backend_admitted(reader, writer, *, queue_aware, total_budget_secs):
            events.append(("admit", queue_aware, total_budget_secs))
            return self.admission

        async def serve_capacity_refusal(session, stop, *, reason, retry_after_secs, pool_label):
            events.append(("capacity", reason, retry_after_secs, type(session).__name__))
            return 7

        def keep_socket(writer: Any) -> None:
            events.append(("keep_socket", writer))

        async def run_bridge(reader, writer, stop, *, peer_supports_ping, session):
            plan = self.bridges.pop(0)
            if plan.get("await_recaller"):
                await asyncio.wait_for(self.recaller_started.wait(), timeout=30)
            events.append(("bridge", reader, peer_supports_ping))
            session.reason = plan["reason"]
            session.outstanding_ids = list(plan.get("outstanding", []))
            if "init" in plan:
                session.captured_init = plan["init"]
            if plan.get("subscribed"):
                session._subscribed = True
            if "held" in plan:
                session.push_front(plan["held"])

        async def emit_error_frames(ids, message, *, pool_label):
            events.append(("errors", list(ids)))

        async def reconnect_while_draining(
            socket_path, payload, session, stop, *, poolable, pool_label
        ):
            events.append(("reconnect", session.reason))
            self.reconnect_payloads.append(payload)
            return self.reattach.pop(0)

        async def recaller_loop(writer, channel_id, stop):
            events.append(("recaller", writer))
            self.recaller_started.set()
            try:
                # Parked until _amain cancels it once the bridge returns; bounded all the same.
                await asyncio.wait_for(asyncio.Event().wait(), timeout=30)
            except asyncio.CancelledError:
                events.append(("recaller_cancelled",))
                raise

        for name, fake in {
            "build_register_payload": build_register_payload,
            "handshake": handshake,
            "alog_fallback": alog_fallback,
            "fallback_exec": fallback_exec,
            "_safe_close": safe_close,
            "_ensure_backend_admitted": ensure_backend_admitted,
            "_serve_capacity_refusal": serve_capacity_refusal,
            "_keep_socket_across_exec": keep_socket,
            "run_bridge": run_bridge,
            "_emit_error_frames": emit_error_frames,
            "_reconnect_while_draining": reconnect_while_draining,
            "_recaller_loop": recaller_loop,
        }.items():
            monkeypatch.setattr(stub, name, fake)

    async def __call__(self) -> int | str:
        """``_amain``'s exit code, or ``"exec"`` when it reached ``fallback_exec``."""
        try:
            return await asyncio.wait_for(stub._amain(self.argv), timeout=30)
        except _Exited:
            return "exec"

    def kinds(self) -> list[Any]:
        return [event[0] for event in self.events]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Run:
    monkeypatch.delenv("KIROCREW_MCP_LOG", raising=False)
    monkeypatch.delenv("MC_MCP_LOG", raising=False)
    return _Run(monkeypatch, tmp_path)


# --------------------------------------------------------------------------- #
# _amain: the ways out before any bridge runs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_handshake_timeout_falls_back_to_a_direct_exec(run: _Run) -> None:
    run.handshake_error = asyncio.TimeoutError()
    assert await run() == "exec"
    assert ("alog", "handshake_timeout", "uuid-1", False) in run.events
    assert run.kinds()[:5] == ["ceiling", "payload", "signals", "handshake", "alog"]


@pytest.mark.asyncio
async def test_a_fallback_request_logs_its_reason_and_execs(run: _Run) -> None:
    run.handshake_error = stub.FallbackRequestedError("daemon says no")
    assert await run() == "exec"
    assert ("alog", "daemon says no", "uuid-1", False) in run.events


@pytest.mark.asyncio
async def test_a_private_server_behind_a_daemon_without_poolable_ack_degrades(
    run: _Run,
) -> None:
    run.registered = {"capabilities": []}
    assert await run() == "exec"
    assert ("alog", "gateway does not honour the poolable field", "uuid-1", False) in run.events
    assert ("close", "writer-1") in run.events


@pytest.mark.asyncio
async def test_a_nonce_scoped_server_behind_a_nonce_blind_daemon_degrades(run: _Run) -> None:
    run.argv[1] = "kirocrew-computer"
    run.argv.append("--poolable")
    run.registered = {"capabilities": ["poolable_ack"]}
    assert await run() == "exec"
    assert ("alog", "gateway mints no per-connection tenant nonce", "uuid-1", False) in run.events


@pytest.mark.asyncio
async def test_an_admission_preflight_that_times_out_falls_back(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "ensure_backend"]}
    run.admission = (stub._ADMIT_TIMEOUT, None)
    assert await run() == "exec"
    assert ("admit", False, stub._SPAWN_QUEUE_WAIT_BUDGET_SECS) in run.events
    assert ("alog", f"ensure_backend_io:{stub._ADMIT_TIMEOUT}", "uuid-1", False) in run.events


@pytest.mark.asyncio
async def test_a_capacity_rejection_serves_typed_errors_and_never_execs(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "ensure_backend"]}
    run.admission = (
        stub._ADMIT_REJECTED,
        {"class": stub._REJECT_CLASS_CAPACITY, "reason": "full", "retry_after_secs": 5},
    )
    assert await run() == 7
    assert ("alog", "capacity:full", "uuid-1", True) in run.events
    assert ("capacity", "full", 5, "StubSession") in run.events
    assert "exec" not in run.kinds()


@pytest.mark.asyncio
async def test_a_rejection_that_is_not_fallback_eligible_ends_the_stub(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "ensure_backend"]}
    run.admission = (stub._ADMIT_REJECTED, {"reason": "nope"})
    assert await run() == 1
    assert ("alog", "terminal:nope", "uuid-1", True) in run.events
    assert "exec" not in run.kinds()


@pytest.mark.asyncio
async def test_an_unknown_target_rejection_from_an_older_daemon_falls_back(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "ensure_backend"]}
    run.admission = (stub._ADMIT_REJECTED, {"reason": stub.TARGET_UNKNOWN_REASON})
    assert await run() == "exec"
    assert ("close", "writer-1") in run.events


@pytest.mark.asyncio
async def test_a_queue_aware_fallback_keeps_the_socket_across_the_exec(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "ensure_backend", "spawn_queue"]}
    run.admission = (stub._ADMIT_REJECTED, {"class": "compat", "reason": "too old"})
    assert await run() == "exec"
    assert ("keep_socket", "writer-1") in run.events
    assert ("close", "writer-1") not in run.events
    assert ("alog", "too old", "uuid-1", False) in run.events


@pytest.mark.asyncio
async def test_an_unlabelable_payload_still_runs_under_an_agent_server_label(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unlabelable(payload: dict[str, Any]) -> str:
        raise ValueError("bad payload")

    monkeypatch.setattr(stub, "format_pool_label", unlabelable)
    labels: list[str] = []

    async def capture(reason, stub_uuid, pool_label, args, *, terminal=False):
        labels.append(pool_label)

    monkeypatch.setattr(stub, "alog_fallback", capture)
    run.handshake_error = stub.FallbackRequestedError("x")
    assert await run() == "exec"
    assert labels == ["agent-a:srv"]


@pytest.mark.asyncio
async def test_an_unrecognised_log_level_falls_back_to_warning(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    levels: list[Any] = []
    monkeypatch.setattr(
        stub.logging, "basicConfig", lambda **kwargs: levels.append(kwargs["level"])
    )
    monkeypatch.setenv("KIROCREW_MCP_LOG", "verbose")
    run.handshake_error = stub.FallbackRequestedError("x")
    assert await run() == "exec"
    assert levels == ["WARNING"]


# --------------------------------------------------------------------------- #
# _amain: the bridge loop and its exits
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_bridge_kiro_cli_ended_exits_cleanly(run: _Run) -> None:
    run.registered = {"capabilities": ["poolable_ack", "bridge_ping"]}
    assert await run() == 0
    assert ("bridge", "reader-1", True) in run.events
    assert "reconnect" not in run.kinds()


@pytest.mark.asyncio
async def test_a_lost_bridge_reattaches_and_serves_again(run: _Run) -> None:
    run.bridges = [
        {"reason": "socket_eof", "outstanding": [4, 5], "init": b'{"id": 1}\n'},
        {"reason": "stdin_eof"},
    ]
    run.reattach = [("reader-2", "writer-2", {"capabilities": ["bridge_ping"]}, "uuid-2")]
    assert await run() == 0
    assert ("errors", [4, 5]) in run.events
    assert ("alog", "bridge_lost_socket_eof", "uuid-1", False) in run.events
    assert ("reconnect", "socket_eof") in run.events
    assert ("bridge", "reader-2", True) in run.events
    assert run.kinds().count("payload") == 2


@pytest.mark.asyncio
async def test_a_reconnect_that_cannot_reattach_ends_the_stub_with_an_error(run: _Run) -> None:
    run.bridges = [{"reason": "peer_dead", "init": b'{"id": 1}\n'}]
    run.reattach = [None]
    assert await run() == 1
    assert run.events[-1] == ("alog", "bridge_dead_peer_dead", "uuid-1", False)


@pytest.mark.asyncio
async def test_a_payload_rebuild_failure_keeps_the_previous_payload(run: _Run) -> None:
    run.bridges = [{"reason": "socket_eof", "init": b'{"id": 1}\n'}, {"reason": "stdin_eof"}]
    run.reattach = [("reader-2", "writer-2", {"capabilities": []}, "uuid-2")]
    run.payload_error = RuntimeError("wedged read")
    assert await run() == 0
    assert len(run.built) == 1
    assert run.reconnect_payloads == [run.built[0]]
    assert run.reconnect_payloads[0] is run.built[0]
    assert ("bridge", "reader-2", False) in run.events


@pytest.mark.asyncio
async def test_live_subscriptions_refuse_the_reconnect(run: _Run) -> None:
    run.bridges = [{"reason": "socket_eof", "init": b'{"id": 1}\n', "subscribed": True}]
    assert await run() == 1
    assert "reconnect" not in run.kinds()


@pytest.mark.asyncio
async def test_a_bridge_lost_before_initialize_cannot_reconnect(run: _Run) -> None:
    run.bridges = [{"reason": "socket_write_failed"}]
    assert await run() == 1
    assert "reconnect" not in run.kinds()


@pytest.mark.asyncio
async def test_calls_still_held_at_the_end_are_answered_with_errors(run: _Run) -> None:
    run.bridges = [
        {"reason": "socket_eof", "held": [b'{"jsonrpc": "2.0", "id": 9, "method": "tools/call"}\n']}
    ]
    assert await run() == 1
    assert ("errors", [9]) in run.events
    assert ("alog", "bridge_dead_socket_eof", "uuid-1", False) in run.events


@pytest.mark.asyncio
async def test_a_warm_pool_stub_runs_its_recaller_beside_the_bridge(run: _Run) -> None:
    run.payload = {"stub_uuid": "uuid-1", "session_key": ""}
    run.bridges = [{"reason": "stdin_eof", "await_recaller": True}]
    assert await run() == 0
    kinds = run.kinds()
    assert ("recaller", "writer-1") in run.events
    assert kinds.index("recaller") < kinds.index("bridge") < kinds.index("recaller_cancelled")


# --------------------------------------------------------------------------- #
# main and the signal handlers
# --------------------------------------------------------------------------- #


def test_main_exits_through_hard_exit_with_the_amain_code(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []

    async def amain() -> int:
        return 3

    monkeypatch.setattr(stub, "_amain", amain)
    monkeypatch.setattr(stub, "_hard_exit", exits.append)
    stub.main()
    assert exits == [3]


def test_an_interrupted_main_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []

    async def amain() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(stub, "_amain", amain)
    monkeypatch.setattr(stub, "_hard_exit", exits.append)
    stub.main()
    assert exits == [0]


class _Loop:
    def __init__(self, refuse: type[BaseException] | None = None) -> None:
        self.handlers: dict[int, Any] = {}
        self.refuse = refuse

    def add_signal_handler(self, sig: int, callback: Any) -> None:
        if self.refuse is not None:
            raise self.refuse("no signals here")
        self.handlers[sig] = callback


def test_sigterm_and_sigint_set_the_stop_event() -> None:
    loop, stop = _Loop(), asyncio.Event()
    stub._install_signal_handlers(loop, stop)  # type: ignore[arg-type]
    assert set(loop.handlers) == {signal.SIGTERM, signal.SIGINT}
    loop.handlers[signal.SIGTERM]()
    assert stop.is_set()


@pytest.mark.parametrize("refusal", [NotImplementedError, RuntimeError])
def test_a_loop_without_signal_support_is_left_alone(refusal: type[BaseException]) -> None:
    loop, stop = _Loop(refuse=refusal), asyncio.Event()
    stub._install_signal_handlers(loop, stop)  # type: ignore[arg-type]
    assert loop.handlers == {}
    assert not stop.is_set()


# --------------------------------------------------------------------------- #
# The facade's contract
# --------------------------------------------------------------------------- #


def _probe(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a fresh interpreter against this checkout's ``src``."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["KIROCREW_HOME"] = str(tmp_path / "crew")
    return subprocess.run(
        [sys.executable, "-B", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
        **UTF8_TEXT,
    )


def test_the_stub_starts_under_python_dash_m(tmp_path: Path) -> None:
    """The launch line every overlay carries: the facade installs its mirror as ``__main__``."""
    proc = _probe(tmp_path, "-m", "kiro_crew.mcp_gateway.stub", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "kirocrew-mcp-stub" in proc.stdout


_MAIN_THEN_IMPORT = """
import contextlib, io, json, runpy, sys
sys.argv = ["stub", "--help"]
with contextlib.redirect_stdout(io.StringIO()):
    try:
        runpy.run_module("kiro_crew.mcp_gateway.stub", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        assert exc.code == 0, exc.code
import kiro_crew.mcp_gateway.stub as canonical
from kiro_crew.mcp_gateway import stub_bridge, stub_link
print(json.dumps({
    "name": stub_bridge.logger.name,
    "same": canonical.logger is stub_bridge.logger is stub_link.logger,
}))
"""


def test_a_stub_run_as_main_logs_as_main_and_survives_a_second_import(tmp_path: Path) -> None:
    """``python -m`` runs the facade as ``__main__``; gatewayd later imports it by name.

    The second import installs the mirror again for the dotted name, which refuses a name
    bound to different objects across the family, so the facade must bind nothing of its
    own that an owner also binds -- the logger included.
    """
    proc = _probe(tmp_path, "-c", _MAIN_THEN_IMPORT)
    assert proc.returncode == 0, proc.stderr
    report = proc.stdout.strip().splitlines()[-1]
    assert report == '{"name": "__main__", "same": true}'


def test_an_imported_stub_logs_under_its_dotted_name() -> None:
    assert stub.logger.name == "kiro_crew.mcp_gateway.stub"
    assert all(owner.logger is stub.logger for owner in _OWNERS)


def test_the_facade_binds_no_logger_of_its_own() -> None:
    """The facade's logger is the owners' object, imported, never built from ``__name__``."""
    tree = ast.parse(Path(stub.__file__).read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "logger" not in assigned


@pytest.mark.parametrize(
    "name", ["READ_BUFFER_LIMIT_BYTES", "_HELD_FRAME_BYTES", "_HELD_TOTAL_BYTES"]
)
def test_the_read_ceiling_is_bound_beside_its_readers_alone(name: str) -> None:
    """``_adopt_read_ceiling`` rebinds these through ``globals()``, which reaches one module.

    So the module that holds the function holds the names, and no other member of the
    family binds a copy that the adoption would leave stale.
    """
    holders = [module.__name__ for module in (stub, *_OWNERS) if name in vars(module)]
    assert holders == [stub_link.__name__]


def _home_imports(tree: ast.Module, family: set[str]) -> dict[str, str]:
    """Name -> module, for each name *tree* imports at its top from kiro_crew outside *family*."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.split(".")[0] == "kiro_crew" and module not in family:
                found.update({alias.asname or alias.name: module for alias in node.names})
    return found


def _type_checking_imports(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom):
                    found.update(
                        {alias.asname or alias.name: sub.module or "" for alias in sub.names}
                    )
    return found


def test_every_project_name_the_facade_serves_is_in_its_type_checking_block() -> None:
    """A name served at run time is invisible to mypy unless the block names it."""
    family = {stub.__name__, *stub._STUB_OWNERS}
    imported: dict[str, str] = {}
    for owner in _OWNERS:
        imported.update(_home_imports(ast.parse(Path(owner.__file__).read_text("utf-8")), family))
    served = {name: imported[name] for name in stub._EXPORTS if name in imported}
    block = _type_checking_imports(ast.parse(Path(stub.__file__).read_text("utf-8")))
    missing = {name: home for name, home in served.items() if block.get(name) != home}
    assert served, "the facade serves no project name: this check would pass vacuously"
    assert missing == {}


def test_the_mirror_holds_the_stub_family() -> None:
    index = vars(stub)[_facade_mirror.INDEX_ATTRIBUTE]
    assert index.owners == stub._STUB_OWNERS
    assert index.excluded == ()
