"""Explicit, opt-in smoke test against the host's signed-in ``kiro-cli``.

The normal E2E suites use the packaged fake ACP backend. This module is the
single real-service exception and stays dark unless either its activation or
REQUIRE marker is exactly ``1``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator, NoReturn

import pytest


def _required() -> bool:
    return os.environ.get("KIROCREW_E2E_REAL_KIRO_REQUIRE", "") == "1"


def _enabled() -> bool:
    return os.environ.get("KIROCREW_E2E_REAL_KIRO", "") == "1" or _required()


pytestmark = pytest.mark.skipif(
    not _enabled(),
    reason=(
        "Real-kiro-cli smoke. Set KIROCREW_E2E_REAL_KIRO=1 to run "
        "(needs a signed-in host kiro-cli)."
    ),
)

_TURN_TIMEOUT = 120.0
_READY_TIMEOUT = 90.0
_HOST_PROBE_PREFIX = "KIROCREW_REAL_SMOKE_HOST_PROBE:"
_PROMPT_TEMPLATE = (
    "This is an automated smoke test. Use your file-reading tool to read the "
    "exact contents of {path} and reply with ONLY that file's contents, nothing else."
)


def _unresolved(message: str) -> NoReturn:
    if _required():
        pytest.fail(message)
    pytest.skip(message)


def _real_kiro_home() -> Path:
    """Host identity root, independent of pytest's KIRO_HOME/path overrides."""
    return (Path.home() / ".kiro").resolve()


def _resolve_real_kiro_cli(real_kiro_home: Path) -> str:
    """Resolve once without accepting a test backend inherited via the override."""
    from kiro_crew.acp.client import _KiroExecutableTrustError, _resolve_kiro_bin

    env = dict(os.environ)
    env.pop("KIROCREW_KIRO_BIN", None)
    try:
        resolved = _resolve_kiro_bin(environ=env, home=real_kiro_home.parent)
    except _KiroExecutableTrustError as exc:
        _unresolved(f"host kiro-cli failed executable validation: {exc}")
    if not resolved:
        _unresolved(
            "no real host kiro-cli found after excluding KIROCREW_KIRO_BIN; "
            "install it and run `kiro-cli login`"
        )
    return resolved


def _probe_signed_in(kiro_bin: str, env: dict[str, str], cwd: Path) -> None:
    """Use the final child environment; never copy credentials or fall back home."""
    assert env.get("KIROCREW_KIRO_BIN") == kiro_bin, "native binary pin changed"
    try:
        completed = subprocess.run(
            [kiro_bin, "whoami"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _unresolved(f"`kiro-cli whoami` could not run: {type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        _unresolved(
            "`kiro-cli whoami` failed for the exact binary selected for the smoke "
            f"(exit {completed.returncode}); run `kiro-cli login`"
        )


def _private_probe_payload() -> dict:
    """Fresh-process path and launcher verdict; never inspect the host's files."""
    from kiro_crew import agent
    from kiro_crew.config import paths

    launcher = Path(agent._resolve_kirocrew_bin())
    reachable = shutil.which("kirocrew")
    blockers = []
    if (
        not launcher.is_absolute()
        or not agent._launcher_works(launcher)
        or not reachable
        or Path(reachable).resolve() != launcher.resolve()
    ):
        blockers.append("existing kirocrew launcher must be reachable on child PATH before boot")
    return {
        "actual_home": str(paths.kiro_home().resolve()),
        "target": str(agent.kiro_agents_dir_path().resolve()),
        "blockers": blockers,
    }


_PRIVATE_PROBE_CODE = (
    "import json,runpy,sys; "
    "ns=runpy.run_path(sys.argv[1]); "
    "payload=ns['_private_probe_payload'](); "
    f"print('{_HOST_PROBE_PREFIX}'+json.dumps(payload,sort_keys=True),flush=True)"
)


def _run_private_probe(env: dict[str, str], cwd: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _PRIVATE_PROBE_CODE, str(Path(__file__).resolve())],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        _unresolved(f"fresh private-home preflight failed (exit {completed.returncode})")
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_HOST_PROBE_PREFIX):
            payload = json.loads(line[len(_HOST_PROBE_PREFIX) :])
            if isinstance(payload, dict):
                return payload
    _unresolved("fresh private-home preflight returned no payload")


def _native_transcript_count(home: Path) -> int:
    """Observe native files ONLY in the owned writer tree, independent of readers."""
    root = home / "sessions" / "cli"
    files = []
    if root.is_dir():
        assert root.resolve() == home.resolve() / "sessions" / "cli", "redirected native root"
        for path in root.iterdir():
            if path.suffix not in (".json", ".jsonl"):
                continue
            assert str(uuid.UUID(path.stem)) == path.stem, "unexpected native filename"
            info = path.lstat()
            assert (
                stat.S_ISREG(info.st_mode) and path.resolve().parent == root.resolve()
            ), "native transcript is not a regular owned file"
            if info.st_size:
                files.append(path)
    assert files, "no native transcripts appeared in private KIRO_HOME"
    return len(files)


@contextlib.contextmanager
def _booted_with_real_kiro(kiro_bin: str) -> Iterator[tuple[object, "_Client"]]:
    from kiro_crew.testing.harness import spawn_feature_gateway

    private_home = None
    handle = None

    def _preflight(env: dict[str, str], cwd: Path) -> None:
        nonlocal private_home
        # This is the harness-owned default, not a transcript-reader override.
        data_home = Path(env["KIROCREW_HOME"]).resolve()
        expected = data_home / "kiro"
        if not env.get("KIRO_HOME") or Path(env["KIRO_HOME"]).resolve() != expected:
            _unresolved("real smoke requires the harness-private KIRO_HOME; no host fallback")
        private_home = expected
        payload = _run_private_probe(env, cwd)
        if payload.get("actual_home") != str(expected) or payload.get("target") != str(
            expected / "agents"
        ):
            _unresolved("fresh process did not resolve the private KIRO_HOME and agents")
        if payload.get("blockers"):
            _unresolved(f"private-home preflight blocked before boot: {payload['blockers']!r}")
        _probe_signed_in(kiro_bin, env, cwd)

    try:
        with spawn_feature_gateway(
            fixture="minimal",
            approval="interactive",
            timeout=_READY_TIMEOUT,
            kiro_bin=kiro_bin,
            before_spawn=_preflight,
        ) as handle:
            client = _Client(handle.port, handle.token)
            client.diagnostics = handle.diagnostics
            yield handle, client
    finally:
        # The harness owns deletion, including initialization failures before
        # READY and failures before session/new or session_map publishes an ID.
        # It retains the entire tree on an unconfirmed stop; never delete here.
        if handle is not None:
            assert (
                handle.teardown_confirmed is True
            ), "gateway tree stop unconfirmed; private native transcripts retained"
        if private_home is not None:
            assert not os.path.lexists(private_home.parent), "owned home residue remains after stop"
            if handle is not None:
                print("native transcript cleanup: tree stopped; private home absent")


class _Client:
    def __init__(self, port: int, token: str) -> None:
        import http.cookiejar
        import urllib.request

        from kiro_crew.loopback_http import build_loopback_opener

        self._port = port
        self.diagnostics = lambda: ""
        jar = http.cookiejar.CookieJar()
        self._opener = build_loopback_opener()
        self._opener.add_handler(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(f"http://localhost:{port}/api/status?token={token}")
        with self._opener.open(request, timeout=30):
            pass

    def get(self, path: str) -> dict:
        import urllib.request

        return self._open(urllib.request.Request(f"http://localhost:{self._port}{path}"), 30)

    def post(self, path: str, body: dict) -> dict:
        import urllib.request

        request = urllib.request.Request(
            f"http://localhost:{self._port}{path}",
            data=json.dumps(body).encode(),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        return self._open(request, 60)

    def _open(self, request, timeout: float) -> dict:
        import urllib.error

        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:2000]
            raise AssertionError(
                f"{request.get_method()} {request.selector} -> HTTP {exc.code}: {body}\n"
                f"{self.diagnostics()}"
            ) from exc


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_exact_nonce_read(raw: object, nonce_path: Path) -> bool:
    """Accept only one typed line/file read of the exact synthetic nonce path."""
    if not isinstance(raw, str) or not raw:
        return False
    try:
        params = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(params, dict):
        return False
    purpose = params.pop("__tool_use_purpose", None)
    if purpose is not None and not isinstance(purpose, str):
        return False

    expected = nonce_path.resolve()

    def _same_path(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return Path(value).resolve() == expected
        except OSError:
            return False

    if "path" in params:
        allowed = {"path", "line_start", "line_end", "offset", "limit"}
        return (
            set(params) <= allowed
            and _same_path(params.get("path"))
            and all(_nonnegative_int(value) for key, value in params.items() if key != "path")
        )

    if set(params) != {"operations"}:
        return False
    operations = params.get("operations")
    if not isinstance(operations, list) or len(operations) != 1:
        return False
    operation = operations[0]
    if not isinstance(operation, dict):
        return False
    allowed = {"mode", "path", "offset", "limit"}
    return (
        set(operation) <= allowed
        and set(operation) >= {"mode", "path"}
        and operation.get("mode") == "Line"
        and _same_path(operation.get("path"))
        and all(
            _nonnegative_int(value)
            for key, value in operation.items()
            if key not in {"mode", "path"}
        )
    )


def _verified_read_tool(
    messages: list[dict], tool_call_id: str, nonce_path: Path, *, allow_other: bool = False
) -> dict | None:
    matches = []
    for message in messages:
        meta = message.get("meta") or {}
        if (
            message.get("role") == "tool"
            and isinstance(meta, dict)
            and str(meta.get("tool_call_id") or "") == tool_call_id
            and meta.get("kind") in (("read", "other") if allow_other else ("read",))
            and _is_exact_nonce_read(meta.get("input"), nonce_path)
        ):
            matches.append(message)
    return matches[-1] if matches else None


def _pending_permission(message: dict) -> tuple[str, dict] | None:
    if message.get("role") != "permission":
        return None
    meta = message.get("meta") or {}
    if not isinstance(meta, dict) or meta.get("resolved"):
        return None
    return str(meta.get("approval_id") or ""), meta


def _reject_unexpected(client: _Client, approval_id: str, reason: str) -> NoReturn:
    if approval_id:
        client.post(f"/api/approvals/{approval_id}/reject", {})
    raise AssertionError(reason)


def _await_completed_turn(
    client: _Client,
    slot: str,
    nonce_path: Path,
    nonce: str,
    timeout: float,
    *,
    require_manual: bool = True,
) -> tuple[dict, dict]:
    """Approve one correlated read, then require a genuine successful terminal state."""
    deadline = time.monotonic() + timeout
    detail: dict = {}
    approved_id = ""
    approved_tool_call_id = ""
    while time.monotonic() < deadline:
        detail = client.get(f"/api/chat/slots/{slot}")
        messages = detail.get("messages", [])
        if not isinstance(messages, list):
            raise AssertionError(f"slot returned non-list messages: {messages!r}")

        errors = [message for message in messages if message.get("role") == "error"]
        if errors:
            raise AssertionError(f"real kiro-cli turn entered an error state: {errors!r}")

        for message in messages:
            pending = _pending_permission(message)
            if pending is None:
                continue
            approval_id, meta = pending
            tool_call_id = str(meta.get("tool_call_id") or "")
            tool = _verified_read_tool(
                messages, tool_call_id, nonce_path, allow_other=not require_manual
            )
            exact_permission = (
                bool(approval_id)
                and bool(tool_call_id)
                and meta.get("is_shell") in ("", False, None)
                and _is_exact_nonce_read(meta.get("tool_input"), nonce_path)
                and tool is not None
            )
            if approved_id:
                if (
                    approval_id == approved_id
                    and tool_call_id == approved_tool_call_id
                    and exact_permission
                ):
                    continue
                _reject_unexpected(
                    client,
                    approval_id,
                    "real smoke requested more than one permission; the extra operation "
                    f"was rejected (first={approved_id!r}, extra={approval_id!r})",
                )
            if not exact_permission:
                _reject_unexpected(
                    client,
                    approval_id,
                    "real smoke requested an operation other than the one correlated "
                    f"read of the synthetic nonce file: permission={message!r}",
                )
            client.post(f"/api/approvals/{approval_id}/approve", {})
            approved_id = approval_id
            approved_tool_call_id = tool_call_id

        if detail.get("running") is False:
            if not approved_id and require_manual:
                raise AssertionError("turn completed without the required explicit read approval")
            if not approved_id:
                reads = [m for m in messages if m.get("role") == "tool"]
                assert len(reads) == 1, "confined smoke must execute exactly one read"
                approved_tool_call_id = str((reads[0].get("meta") or {}).get("tool_call_id") or "")
            resolved = [
                message
                for message in messages
                if message.get("role") == "permission"
                and isinstance(message.get("meta"), dict)
                and message["meta"].get("approval_id") == approved_id
                and message["meta"].get("resolved") == "approved"
            ]
            if approved_id and len(resolved) != 1:
                raise AssertionError(
                    "turn completed without one transcript-confirmed allow-once decision: "
                    f"{resolved!r}"
                )
            if detail.get("queue"):
                raise AssertionError(f"turn stopped with queued recovery work: {detail['queue']!r}")
            tool = _verified_read_tool(
                messages, approved_tool_call_id, nonce_path, allow_other=not require_manual
            )
            tool_meta = (tool or {}).get("meta") or {}
            if tool is None or tool_meta.get("done") is not True:
                raise AssertionError(
                    f"approved read has no successful terminal tool event: {tool!r}"
                )
            if tool_meta.get("output") != nonce:
                raise AssertionError(
                    "approved read's tool result was not exactly the synthetic nonce: "
                    f"{tool_meta.get('output')!r}"
                )
            assistants = [message for message in messages if message.get("role") == "assistant"]
            exact = [message for message in assistants if message.get("content") == nonce]
            if len(exact) != 1 or len(assistants) != 1:
                raise AssertionError(
                    "completed turn did not contain exactly one assistant response equal "
                    f"to the nonce: {assistants!r}"
                )
            return exact[0], tool
        time.sleep(1.0)

    seen = [
        (message.get("role"), str(message.get("content", ""))[:160])
        for message in detail.get("messages", [])
    ]
    raise AssertionError(
        f"no safely completed real-backend turn within {timeout:.0f}s; "
        f"slot messages={seen!r}\n{client.diagnostics()}"
    )


def _confined_read(name: str, params: dict) -> str:
    """Execution-side gate: native MCP pre-approval cannot bypass this check."""
    from kiro_crew.hooks import TOOL_DENY, HookManager
    from kiro_crew.platform.context import current_context

    ceiling = current_context().governance
    if ceiling is None or ceiling.get("filesystem.read") is None:
        raise RuntimeError("missing read ceiling")
    if name != "read" or set(params) != {"path"} or not isinstance(params["path"], str):
        raise ValueError("only one exact file path is accepted")
    verdict = HookManager().on_tool_call(
        "read",
        session_key="dashboard:real-smoke",
        tool_kind="read",
        raw_params=params,
        mcp_server_name="real-smoke",
        mcp_tool_name="read",
        mcp_identity_trusted=True,
    )
    if verdict.action == TOOL_DENY:
        return f"Blocked before read: {verdict.reason}"
    content = Path(params["path"]).read_text(encoding="utf-8")
    receipt = Path(os.environ["KIROCREW_SECURITY_POLICY"]).parent / "read-effects.jsonl"
    with receipt.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"path": params["path"]}) + "\n")
    return content


def _serve_confined_read() -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_shared import run_mcp_stdio_loop
    from kiro_crew.platform.bootstrap import bootstrap_context

    bootstrap_context(KiroCrewConfig.load())
    run_mcp_stdio_loop(
        "real-smoke",
        "1",
        lambda: [
            {
                "name": "read",
                "description": "Read the exact synthetic file path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
            }
        ],
        _confined_read,
    )


def _confined_project(project: Path, nonce_path: Path) -> tuple[str, Path]:
    """One private MCP read tool; no native filesystem, shell, or network tools."""
    from kiro_crew.dashboard.side_readonly_spec import derive_readonly_spec
    from kiro_crew.platform.governance import parse_policy

    policy_path = project / "security_policy.json"
    spec = derive_readonly_spec(
        {
            "tools": ["@real-smoke/read"],
            "mcpServers": {
                "real-smoke": {
                    "command": sys.executable,
                    "args": [
                        "-c",
                        "import runpy,sys; runpy.run_path(sys.argv[1])['_serve_confined_read']()",
                        str(Path(__file__).resolve()),
                    ],
                    "env": {"KIROCREW_SECURITY_POLICY": str(policy_path)},
                }
            },
            "resources": [],
            "prompt": "Use only the real-smoke read tool for the requested synthetic file. Never retry a denied read.",
        },
        base_name="real-smoke",
    )
    directory = project / ".kiro" / "agents"
    directory.mkdir(parents=True)
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")
    policy = {
        "version": 1,
        "boot": {"fail_closed": True},
        "filesystem": {
            "read": {"mode": "allow", "allow": [str(nonce_path.resolve())]},
        },
        "mcp": {"mode": "allow", "allow": ["@real-smoke/read"]},
        "network": {"egress": {"mode": "allow", "allow": []}},
    }
    parse_policy(policy)  # Use the production schema before publishing the test-only ceiling.
    policy_path = project / "security_policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    return spec["name"], policy_path


def _set_smoke_project(handle, project: Path) -> None:
    """Configure only the harness-owned home, before creating any test slot."""
    from kiro_crew.config.loader import update_config_locked

    def configure(data: dict) -> dict:
        data.setdefault("session", {})["eager_spawn"] = False
        data.setdefault("dashboard", {})["default_project"] = str(project)
        return data

    update_config_locked(handle.home / "config.json", mutate=configure)


def _await_denied_read(client, slot: str, path: Path, marker: str, start: int) -> None:
    deadline = time.monotonic() + _TURN_TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/chat/slots/{slot}")
        messages = detail.get("messages", [])[start:]
        assert marker not in json.dumps(messages), "denied synthetic file contents escaped"
        for message in messages:
            pending = _pending_permission(message)
            if pending:
                _reject_unexpected(
                    client, pending[0], "read confinement did not deny before approval"
                )
        if detail.get("running") is False:
            assert not detail.get("queue"), "denied turn queued recovery"
            denied = [
                m
                for m in messages
                if m.get("role") == "tool"
                and _is_exact_nonce_read((m.get("meta") or {}).get("input"), path)
                and "Blocked by governance policy"
                in (m.get("content", "") + str((m.get("meta") or {}).get("output", "")))
            ]
            assert denied, f"no pre-execution filesystem.read denial: {messages!r}"
            assert not any((m.get("meta") or {}).get("output") == marker for m in messages)
            print("negative read: production filesystem.read denial; synthetic marker absent")
            return
        time.sleep(0.1)
    raise AssertionError("confined negative read did not finish")


def test_real_kiro_completes_one_approved_nonce_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_kiro_home = _real_kiro_home()
    kiro_bin = _resolve_real_kiro_cli(real_kiro_home)

    nonce = f"kirocrew-real-smoke-{uuid.uuid4().hex}"
    nonce_path = tmp_path / "real-kiro-smoke-nonce.txt"
    nonce_path.write_text(nonce, encoding="utf-8")
    denied_marker = f"must-not-read-{uuid.uuid4().hex}"
    denied_path = tmp_path / "denied.txt"
    denied_path.write_text(denied_marker, encoding="utf-8")
    agent_name, policy_path = _confined_project(tmp_path, nonce_path)
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy_path))

    with _booted_with_real_kiro(kiro_bin) as (handle, client):
        _set_smoke_project(handle, tmp_path)
        policy_status = client.get("/api/governance/policy")
        assert (
            policy_status.get("has_policy") is True
        ), f"test ceiling not installed: {policy_status!r}"
        read_scope = next(s for s in policy_status["scopes"] if s["scope"] == "filesystem.read")
        assert read_scope["governed"] is True and read_scope["source"] == "policy"
        print(f"read ceiling active: {read_scope!r}")
        slot = f"real-smoke-{uuid.uuid4().hex}"
        created = client.post(
            "/api/chat/slots", {"name": slot, "agent": agent_name, "title": "Confined real smoke"}
        )
        assert created["key"] == slot
        assert created["agent"] == agent_name
        assert Path(created["project"]).resolve() == tmp_path.resolve()
        client.post(
            "/api/chat?ws=1",
            {"message": _PROMPT_TEMPLATE.format(path=nonce_path), "slot": slot},
        )
        assistant, tool = _await_completed_turn(
            client, slot, nonce_path, nonce, _TURN_TIMEOUT, require_manual=False
        )
        assert assistant["content"] == nonce
        assert tool["meta"]["output"] == nonce
        assert (tmp_path / "read-effects.jsonl").is_file(), "private read tool was not used"
        _native_transcript_count(handle.home / "kiro")
        start = len(client.get(f"/api/chat/slots/{slot}")["messages"])
        client.post(
            "/api/chat?ws=1",
            {
                "slot": slot,
                "message": (
                    f"Attempt exactly one read of {denied_path}. If denied, reply BLOCKED. "
                    "Do not retry and do not read any other file."
                ),
            },
        )
        _await_denied_read(client, slot, denied_path, denied_marker, start)
        count = _native_transcript_count(handle.home / "kiro")
        print(f"native writer proof: {count} nonempty private transcript files before teardown")
        effects = [
            json.loads(line)
            for line in (tmp_path / "read-effects.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert effects == [{"path": str(nonce_path)}], f"unexpected read effects: {effects!r}"
        print("private read effects: exactly the nonce file; no denied-file read")
