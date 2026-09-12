"""Explicit, opt-in smoke test against the host's signed-in ``kiro-cli``.

The normal E2E suites use the packaged fake ACP backend. This module is the
single real-service exception and stays dark unless either its activation or
REQUIRE marker is exactly ``1``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
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


def _probe_signed_in(kiro_bin: str, real_kiro_home: Path) -> None:
    """Run the exact binary later pinned into the gateway child environment."""
    env = dict(os.environ)
    env["KIRO_HOME"] = str(real_kiro_home)
    env["KIROCREW_KIRO_BIN"] = kiro_bin
    try:
        completed = subprocess.run(
            [kiro_bin, "whoami"],
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


def _agent_spec_snapshot(kiro_home: Path) -> dict[str, list[object]]:
    root = kiro_home / "agents"
    if not root.is_dir():
        return {}
    snapshot: dict[str, list[object]] = {}
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name.endswith(".lock"):
            continue
        info = entry.lstat()
        digest = ""
        if stat.S_ISREG(info.st_mode):
            hasher = hashlib.sha256()
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        snapshot[entry.name] = [info.st_mode, info.st_size, info.st_mtime_ns, digest]
    return snapshot


def _host_probe_payload(expected_home: str) -> dict:
    """Read-only host verdict. Real runs invoke this only in a fresh process."""
    from kiro_crew import agent, mcp_cleanup
    from kiro_crew.config import paths

    expected = Path(expected_home).resolve()
    actual = paths.kiro_home().resolve()
    target = agent.kiro_agents_dir_path().resolve()
    ambient = paths.ambient_agents_dir().resolve()
    blockers: list[str] = []

    for filename in agent.OWNED_KIRO_AGENT_FILES:
        path = actual / "agents" / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if isinstance(hooks, dict):
            legacy = sorted(set(hooks).intersection(agent._LEGACY_KIROCREW_HOOK_KEYS))
            if legacy:
                blockers.append(f"{filename} has legacy hook keys: {legacy!r}")

    mcp_path = actual / "settings" / "mcp.json"
    try:
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        mcp_data = {}
    servers = mcp_data.get("mcpServers") if isinstance(mcp_data, dict) else None
    if isinstance(servers, dict):
        stale = sorted(
            name
            for name, spec in servers.items()
            if name in mcp_cleanup.STALE_MANAGED_MCP_SERVERS
            or name in mcp_cleanup._predecessor_mcp_names()
            or mcp_cleanup._invokes_superseded_agent(spec)
            or mcp_cleanup._invokes_deleted_playwright_proxy(spec)
        )
        if stale:
            blockers.append(f"first-run cleanup would remove host MCP entries: {stale!r}")

    return {
        "expected_home": str(expected),
        "actual_home": str(actual),
        "target": str(target),
        "ambient": str(ambient),
        "target_equals_ambient": target == ambient,
        "guard_refuses_shared_write": agent._decline_shared_agent_home(audit=False) is not None,
        "blockers": blockers,
        "snapshot": _agent_spec_snapshot(actual),
    }


_HOST_PROBE_CODE = (
    "import json,runpy,sys; "
    "ns=runpy.run_path(sys.argv[1]); "
    "payload=ns['_host_probe_payload'](sys.argv[2]); "
    f"print('{_HOST_PROBE_PREFIX}'+json.dumps(payload,sort_keys=True),flush=True)"
)


def _run_host_probe(env: dict[str, str], cwd: Path, real_kiro_home: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _HOST_PROBE_CODE,
            str(Path(__file__).resolve()),
            str(real_kiro_home),
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "fresh host preflight failed "
            f"(exit {completed.returncode}): {completed.stderr[-2000:]!r}"
        )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_HOST_PROBE_PREFIX):
            payload = json.loads(line[len(_HOST_PROBE_PREFIX) :])
            if isinstance(payload, dict):
                return payload
    raise AssertionError(f"fresh host preflight returned no payload: {completed.stdout[-2000:]!r}")


def _require_safe_host_probe(payload: dict, real_kiro_home: Path) -> None:
    expected = str(real_kiro_home.resolve())
    if payload.get("actual_home") != expected or payload.get("expected_home") != expected:
        _unresolved(
            "real-CLI smoke host preflight resolved a different identity home: "
            f"expected={expected!r} actual={payload.get('actual_home')!r}"
        )
    if payload.get("target_equals_ambient") is not True:
        _unresolved("real-CLI smoke host preflight did not inspect the gateway's shared spec home")
    if payload.get("guard_refuses_shared_write") is not True:
        _unresolved(
            "real-CLI smoke blocked before gateway boot: the production shared-agent "
            "guard would allow this checkout to rewrite host specs"
        )
    blockers = payload.get("blockers") or []
    if blockers:
        _unresolved(f"real-CLI smoke blocked before gateway boot: {blockers!r}")


@contextlib.contextmanager
def _booted_with_real_kiro(
    kiro_bin: str, real_kiro_home: Path
) -> Iterator[tuple[object, "_Client"]]:
    from kiro_crew.testing.harness import spawn_feature_gateway

    probe_env: dict[str, str] | None = None
    probe_cwd: Path | None = None
    before: dict | None = None

    def _preflight(env: dict[str, str], cwd: Path) -> None:
        nonlocal probe_env, probe_cwd, before
        probe_env = env
        probe_cwd = cwd
        payload = _run_host_probe(env, cwd, real_kiro_home)
        _require_safe_host_probe(payload, real_kiro_home)
        before = payload.get("snapshot")

    try:
        with spawn_feature_gateway(
            fixture="minimal",
            approval="reads",
            timeout=_READY_TIMEOUT,
            kiro_home=real_kiro_home,
            kiro_bin=kiro_bin,
            before_spawn=_preflight,
        ) as handle:
            client = _Client(handle.port, handle.token)
            client.diagnostics = handle.diagnostics
            yield handle, client
    finally:
        if before is not None and probe_env is not None and probe_cwd is not None:
            after_payload = _run_host_probe(probe_env, probe_cwd, real_kiro_home)
            _require_safe_host_probe(after_payload, real_kiro_home)
            after = after_payload.get("snapshot")
            assert after == before, (
                "real-CLI smoke mutated the host agent-spec directory despite the "
                f"fresh-process preflight; before={before!r} after={after!r}"
            )


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


def _verified_read_tool(messages: list[dict], tool_call_id: str, nonce_path: Path) -> dict | None:
    matches = []
    for message in messages:
        meta = message.get("meta") or {}
        if (
            message.get("role") == "tool"
            and isinstance(meta, dict)
            and str(meta.get("tool_call_id") or "") == tool_call_id
            and meta.get("kind") == "read"
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
            tool = _verified_read_tool(messages, tool_call_id, nonce_path)
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
            if not approved_id:
                raise AssertionError("turn completed without the required explicit read approval")
            resolved = [
                message
                for message in messages
                if message.get("role") == "permission"
                and isinstance(message.get("meta"), dict)
                and message["meta"].get("approval_id") == approved_id
                and message["meta"].get("resolved") == "approved"
            ]
            if len(resolved) != 1:
                raise AssertionError(
                    "turn completed without one transcript-confirmed allow-once decision: "
                    f"{resolved!r}"
                )
            if detail.get("queue"):
                raise AssertionError(f"turn stopped with queued recovery work: {detail['queue']!r}")
            tool = _verified_read_tool(messages, approved_tool_call_id, nonce_path)
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


def test_real_kiro_completes_one_approved_nonce_read(tmp_path: Path) -> None:
    real_kiro_home = _real_kiro_home()
    kiro_bin = _resolve_real_kiro_cli(real_kiro_home)
    _probe_signed_in(kiro_bin, real_kiro_home)

    nonce = f"kirocrew-real-smoke-{uuid.uuid4().hex}"
    nonce_path = tmp_path / "real-kiro-smoke-nonce.txt"
    nonce_path.write_text(nonce, encoding="utf-8")

    with _booted_with_real_kiro(kiro_bin, real_kiro_home) as (handle, client):
        slot = client.post("/api/chat/slots", {})["key"]
        assert slot
        client.post(
            "/api/chat?ws=1",
            {"message": _PROMPT_TEMPLATE.format(path=nonce_path), "slot": slot},
        )
        assistant, tool = _await_completed_turn(client, slot, nonce_path, nonce, _TURN_TIMEOUT)
        assert assistant["content"] == nonce
        assert tool["meta"]["output"] == nonce
