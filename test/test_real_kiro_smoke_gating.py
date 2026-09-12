"""Offline unit tests for the opt-in real-kiro-cli smoke."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_MODULE_PATH = Path(__file__).resolve().parent / "e2e" / "test_real_kiro_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_real_kiro_smoke_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture()
def rk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KIROCREW_E2E_REAL_KIRO", raising=False)
    monkeypatch.delenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", raising=False)
    return _load_module()


def _materialize_path(value, path: Path):
    if isinstance(value, dict):
        return {key: _materialize_path(item, path) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize_path(item, path) for item in value]
    return str(path) if value == "{path}" else value


def _read_params(path: Path) -> dict:
    return {"operations": [{"mode": "Line", "path": str(path)}]}


def _tool(path: Path, call_id: str = "tc-read", *, done: bool = False, output: str = "") -> dict:
    meta: dict[str, object] = {
        "tool_call_id": call_id,
        "kind": "read",
        "input": json.dumps(_read_params(path)),
    }
    if done:
        meta.update(done=True, output=output)
    return {"role": "tool", "content": "ignored display title", "meta": meta}


def _permission(path: Path, approval_id: str = "approval-1", call_id: str = "tc-read") -> dict:
    return {
        "role": "permission",
        "content": "untrusted prose",
        "meta": {
            "approval_id": approval_id,
            "tool_call_id": call_id,
            "tool_input": json.dumps(_read_params(path)),
            "is_shell": "",
        },
    }


def _safe_probe(home: Path, snapshot: dict | None = None) -> dict:
    return {
        "expected_home": str(home.resolve()),
        "actual_home": str(home.resolve()),
        "target": str((home / "agents").resolve()),
        "ambient": str((home / "agents").resolve()),
        "target_equals_ambient": True,
        "guard_refuses_shared_write": True,
        "blockers": [],
        "snapshot": snapshot or {"kirocrew.json": [1, 2, 3, "digest"]},
    }


class _ClientStub:
    def __init__(self, details: list[dict]) -> None:
        self.details = iter(details)
        self.last = details[-1]
        self.posts: list[tuple[str, dict]] = []
        self.diagnostics = lambda: "stub diagnostics"

    def get(self, _path: str) -> dict:
        self.last = next(self.details, self.last)
        return self.last

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        return {"ok": True}


class TestGateAndIdentity:
    def test_default_is_skipped(self, rk) -> None:
        assert rk.pytestmark.args[0] is True

    @pytest.mark.parametrize("name", ["KIROCREW_E2E_REAL_KIRO", "KIROCREW_E2E_REAL_KIRO_REQUIRE"])
    def test_either_exact_marker_enables_module(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.delenv("KIROCREW_E2E_REAL_KIRO", raising=False)
        monkeypatch.delenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", raising=False)
        monkeypatch.setenv(name, "1")
        assert _load_module().pytestmark.args[0] is False

    def test_real_home_ignores_kiro_home_override(
        self, monkeypatch: pytest.MonkeyPatch, rk, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "redirected"))
        with patch.object(rk.Path, "home", return_value=tmp_path / "host"):
            assert rk._real_kiro_home() == (tmp_path / "host" / ".kiro").resolve()

    def test_resolution_ignores_inherited_binary_override(
        self, monkeypatch: pytest.MonkeyPatch, rk, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "fake-backend")
        seen: dict = {}

        def _resolve(*, environ, home):
            seen.update(environ)
            assert home == tmp_path
            return "C:/Program Files/Kiro-Cli/kiro-cli.exe"

        with patch("kiro_crew.acp.client._resolve_kiro_bin", side_effect=_resolve):
            result = rk._resolve_real_kiro_cli(tmp_path / ".kiro")
        assert result.endswith("kiro-cli.exe")
        assert "KIROCREW_KIRO_BIN" not in seen

    def test_probe_pins_selected_binary_and_identity_home(self, rk, tmp_path: Path) -> None:
        completed = type("CP", (), {"returncode": 0})()
        with patch("subprocess.run", return_value=completed) as run:
            rk._probe_signed_in("C:/Kiro/kiro-cli.exe", tmp_path / ".kiro")
        assert run.call_args.args[0] == ["C:/Kiro/kiro-cli.exe", "whoami"]
        assert run.call_args.kwargs["env"]["KIRO_HOME"] == str(tmp_path / ".kiro")
        assert run.call_args.kwargs["env"]["KIROCREW_KIRO_BIN"] == "C:/Kiro/kiro-cli.exe"


class TestFreshHostPreflight:
    def test_run_host_probe_uses_exact_env_and_cwd(self, rk, tmp_path: Path) -> None:
        home = tmp_path / "host" / ".kiro"
        payload = _safe_probe(home)
        completed = type(
            "CP",
            (),
            {
                "returncode": 0,
                "stdout": rk._HOST_PROBE_PREFIX + json.dumps(payload) + "\n",
                "stderr": "",
            },
        )()
        env = {"KIRO_HOME": str(home), "SENTINEL": "same"}
        cwd = tmp_path / "repo"
        with patch.object(rk.subprocess, "run", return_value=completed) as run:
            assert rk._run_host_probe(env, cwd, home) == payload
        assert run.call_args.kwargs["env"] is env
        assert run.call_args.kwargs["cwd"] == str(cwd)

    @pytest.mark.parametrize(
        ("change", "match"),
        [
            ({"actual_home": "wrong"}, "different identity home"),
            ({"target_equals_ambient": False}, "shared spec home"),
            ({"guard_refuses_shared_write": False}, "would allow"),
            ({"blockers": ["sanitizer would write"]}, "sanitizer would write"),
        ],
    )
    def test_unsafe_fresh_probe_blocks_required_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rk,
        tmp_path: Path,
        change: dict,
        match: str,
    ) -> None:
        monkeypatch.setenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", "1")
        payload = {**_safe_probe(tmp_path / ".kiro"), **change}
        with pytest.raises(pytest.fail.Exception, match=match):
            rk._require_safe_host_probe(payload, tmp_path / ".kiro")

    def test_boot_preflight_receives_gateway_env_and_cwd(self, rk, tmp_path: Path) -> None:
        home = tmp_path / "host" / ".kiro"
        env = {
            "KIRO_HOME": str(home),
            "KIROCREW_KIRO_BIN": "C:/Kiro/kiro-cli.exe",
            "KIROCREW_HOME": str(tmp_path / "crew"),
        }
        cwd = tmp_path / "repo"
        seen: dict = {}

        class _Handle:
            port = 1
            token = "token"

            @staticmethod
            def diagnostics() -> str:
                return ""

        @contextlib.contextmanager
        def _spawn(**kwargs):
            seen.update(kwargs)
            kwargs["before_spawn"](dict(env), cwd)
            yield _Handle()

        safe = _safe_probe(home)
        with (
            patch("kiro_crew.testing.harness.spawn_feature_gateway", side_effect=_spawn),
            patch.object(rk, "_run_host_probe", side_effect=[safe, safe]) as probe,
            patch.object(rk, "_Client", lambda *_args: _Handle()),
        ):
            with rk._booted_with_real_kiro("C:/Kiro/kiro-cli.exe", home):
                pass
        assert seen["kiro_home"] == home
        assert seen["kiro_bin"] == "C:/Kiro/kiro-cli.exe"
        assert seen["approval"] == "reads"
        assert probe.call_args_list[0].args == (env, cwd, home)

    def test_boot_detects_host_spec_mutation(self, rk, tmp_path: Path) -> None:
        home = tmp_path / "host" / ".kiro"
        before = _safe_probe(home, {"kirocrew.json": [1, 2, 3, "before"]})
        after = _safe_probe(home, {"kirocrew.json": [1, 2, 4, "after"]})

        class _Handle:
            port = 1
            token = "token"

            @staticmethod
            def diagnostics() -> str:
                return ""

        @contextlib.contextmanager
        def _spawn(**kwargs):
            kwargs["before_spawn"]({}, tmp_path)
            yield _Handle()

        with (
            patch("kiro_crew.testing.harness.spawn_feature_gateway", side_effect=_spawn),
            patch.object(rk, "_run_host_probe", side_effect=[before, after]),
            patch.object(rk, "_Client", lambda *_args: _Handle()),
        ):
            with pytest.raises(AssertionError, match="mutated the host agent-spec"):
                with rk._booted_with_real_kiro("kiro-cli", home):
                    pass


class TestLoopbackClient:
    def test_client_uses_hardened_loopback_opener(self, rk) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def read() -> bytes:
                return b"{}"

        class _Opener:
            def __init__(self) -> None:
                self.handlers: list[object] = []
                self.requests: list[tuple[str, int]] = []

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

            def open(self, request, timeout):
                self.requests.append((request.full_url, timeout))
                return _Response()

        opener = _Opener()
        with patch("kiro_crew.loopback_http.build_loopback_opener", return_value=opener):
            rk._Client(51234, "secret")
        assert opener.requests == [("http://localhost:51234/api/status?token=secret", 30)]
        assert len(opener.handlers) == 1


class TestTypedReadApproval:
    @pytest.mark.parametrize(
        "params",
        [
            {"path": "{path}"},
            {"path": "{path}", "line_start": 0, "line_end": 1},
            {"operations": [{"mode": "Line", "path": "{path}"}]},
            {
                "operations": [{"mode": "Line", "path": "{path}", "offset": 0, "limit": 2}],
                "__tool_use_purpose": "read synthetic nonce",
            },
        ],
    )
    def test_accepts_only_typed_read_selectors(self, rk, tmp_path: Path, params: dict) -> None:
        path = tmp_path / "nonce.txt"
        encoded = json.dumps(_materialize_path(params, path))
        assert rk._is_exact_nonce_read(encoded, path) is True

    @pytest.mark.parametrize(
        "params",
        [
            {"path": "{path}", "content": "write"},
            {"operations": [{"mode": "Directory", "path": "{path}"}]},
            {"operations": [{"mode": "Line", "path": "{path}"}, {"mode": "Line", "path": "x"}]},
            {"operations": [{"mode": "Line", "path": "{path}", "offset": -1}]},
        ],
    )
    def test_rejects_non_read_or_multi_operation_shapes(
        self, rk, tmp_path: Path, params: dict
    ) -> None:
        path = tmp_path / "nonce.txt"
        encoded = json.dumps(_materialize_path(params, path))
        assert rk._is_exact_nonce_read(encoded, path) is False

    def test_waits_past_streaming_then_approves_once_and_requires_exact_results(
        self, rk, tmp_path: Path
    ) -> None:
        nonce = "nonce-exact"
        path = tmp_path / "nonce.txt"
        first = {
            "running": True,
            "queue": [],
            "messages": [_tool(path), _permission(path), {"role": "streaming", "content": nonce}],
        }
        resolved = _permission(path)
        resolved["meta"]["resolved"] = "approved"
        final = {
            "running": False,
            "queue": [],
            "messages": [
                _tool(path, done=True, output=nonce),
                resolved,
                {"role": "assistant", "content": nonce},
            ],
        }
        client = _ClientStub([first, first, final])
        with patch.object(rk.time, "sleep", return_value=None):
            assistant, tool = rk._await_completed_turn(client, "slot", path, nonce, 1.0)
        assert assistant["content"] == nonce
        assert tool["meta"]["done"] is True
        assert client.posts == [("/api/approvals/approval-1/approve", {})]

    def test_rejects_a_write_even_when_title_claims_read(self, rk, tmp_path: Path) -> None:
        path = tmp_path / "nonce.txt"
        write_tool = _tool(path)
        write_tool["meta"].update(
            kind="edit", input=json.dumps({"path": str(path), "content": "replacement"})
        )
        permission = _permission(path)
        permission["content"] = "Read file"
        permission["meta"]["tool_input"] = write_tool["meta"]["input"]
        client = _ClientStub([{"running": True, "queue": [], "messages": [write_tool, permission]}])
        with patch.object(rk.time, "sleep", return_value=None):
            with pytest.raises(AssertionError, match="operation other than"):
                rk._await_completed_turn(client, "slot", path, "nonce", 1.0)
        assert client.posts == [("/api/approvals/approval-1/reject", {})]

    def test_rejects_and_fails_a_second_permission(self, rk, tmp_path: Path) -> None:
        path = tmp_path / "nonce.txt"
        first = {"running": True, "queue": [], "messages": [_tool(path), _permission(path)]}
        second = {
            "running": True,
            "queue": [],
            "messages": [
                _tool(path),
                _tool(path, call_id="tc-second"),
                _permission(path, "approval-2", "tc-second"),
            ],
        }
        client = _ClientStub([first, second])
        with patch.object(rk.time, "sleep", return_value=None):
            with pytest.raises(AssertionError, match="more than one permission"):
                rk._await_completed_turn(client, "slot", path, "nonce", 1.0)
        assert client.posts == [
            ("/api/approvals/approval-1/approve", {}),
            ("/api/approvals/approval-2/reject", {}),
        ]
