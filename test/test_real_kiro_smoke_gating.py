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

    def test_probe_uses_exact_final_env_and_cwd(self, rk, tmp_path: Path) -> None:
        binary = "C:/Kiro/kiro-cli.exe"
        env = {
            "KIRO_HOME": str(tmp_path / "crew" / "kiro"),
            "KIROCREW_KIRO_BIN": binary,
            "HOME": "unchanged-home",
            "USERPROFILE": "unchanged-profile",
            "LOCALAPPDATA": "unchanged-account-store",
        }
        before = dict(env)
        completed = type("CP", (), {"returncode": 0})()
        with patch.object(rk.subprocess, "run", return_value=completed) as run:
            rk._probe_signed_in(binary, env, tmp_path)
        assert run.call_args.args[0] == [binary, "whoami"]
        assert run.call_args.kwargs["env"] is env
        assert run.call_args.kwargs["cwd"] == str(tmp_path)
        assert env == before

    def test_auth_failure_never_falls_back(self, rk, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", "1")
        env = {"KIRO_HOME": str(tmp_path / "kiro"), "KIROCREW_KIRO_BIN": "kiro-cli"}
        completed = type("CP", (), {"returncode": 1})()
        with patch.object(rk.subprocess, "run", return_value=completed) as run:
            with pytest.raises(pytest.fail.Exception, match="whoami"):
                rk._probe_signed_in("kiro-cli", env, tmp_path)
        assert run.call_count == 1
        assert run.call_args.kwargs["env"] is env


class TestFreshPrivatePreflight:
    @pytest.mark.parametrize("reachable", ["same", "missing", "different"])
    def test_requires_launcher_already_on_path(self, rk, tmp_path, monkeypatch, reachable):
        from kiro_crew import agent

        home = tmp_path / "kiro"
        launcher = tmp_path / "bin" / "kirocrew"
        monkeypatch.setenv("KIRO_HOME", str(home))
        monkeypatch.setattr(agent, "_resolve_kirocrew_bin", lambda: str(launcher))
        monkeypatch.setattr(agent, "_launcher_works", lambda _path: True)
        found = {"same": str(launcher), "missing": None, "different": str(tmp_path / "other")}
        monkeypatch.setattr(rk.shutil, "which", lambda _name: found[reachable])
        payload = rk._private_probe_payload()
        assert bool(payload["blockers"]) is (reachable != "same")
        assert payload["actual_home"] == str(home.resolve())

    def test_probe_uses_exact_env_and_cwd(self, rk, tmp_path):
        payload = {"actual_home": "private", "target": "agents", "blockers": []}
        completed = type(
            "CP",
            (),
            {
                "returncode": 0,
                "stdout": rk._HOST_PROBE_PREFIX + json.dumps(payload) + "\n",
            },
        )()
        env = {"KIRO_HOME": str(tmp_path / "kiro")}
        with patch.object(rk.subprocess, "run", return_value=completed) as run:
            assert rk._run_private_probe(env, tmp_path) == payload
        assert run.call_args.kwargs["env"] is env
        assert run.call_args.kwargs["cwd"] == str(tmp_path)


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

    def test_rejects_a_read_of_a_different_path(self, rk, tmp_path: Path) -> None:
        path = tmp_path / "nonce.txt"
        other = tmp_path / "other.txt"
        client = _ClientStub(
            [{"running": True, "queue": [], "messages": [_tool(other), _permission(other)]}]
        )
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


# Shared-home deletion is retired; owned-tree cleanup is covered below.


class TestConfinedRealSmoke:
    def test_private_spec_and_real_hook_ceiling(self, rk, tmp_path, monkeypatch):
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy

        nonce = tmp_path / "nonce.txt"
        other = tmp_path / "other.txt"
        nonce.write_text("allowed", encoding="utf-8")
        other.write_text("forbidden", encoding="utf-8")
        agent, policy_path = rk._confined_project(tmp_path, nonce)
        spec = json.loads((tmp_path / ".kiro" / "agents" / f"{agent}.json").read_text())
        assert spec["tools"] == ["@real-smoke/read"]
        assert spec["allowedTools"] == []
        assert set(spec["mcpServers"]) == {"real-smoke"}
        assert "autoApprove" not in spec["mcpServers"]["real-smoke"]
        assert spec["includeMcpJson"] is False
        assert spec["resources"] == []
        assert "hooks" not in spec and "permissions" not in spec
        ceiling = parse_policy(json.loads(policy_path.read_text()))
        base = build_default_context(KiroCrewConfig.load())
        monkeypatch.setattr(
            "kiro_crew.hooks.current_context", lambda: dataclasses.replace(base, governance=ceiling)
        )
        hooks = HookManager()
        effects = []
        for path, expected in [(nonce, TOOL_AUTO_APPROVE), (other, TOOL_DENY)]:
            result = hooks.on_tool_call(
                "read",
                session_key="dashboard:smoke",
                tool_kind="read",
                raw_params=_read_params(path),
            )
            assert result.action == expected
            if result.action == TOOL_AUTO_APPROVE:
                effects.append(path.name)
                assert path.read_text() == "allowed"
        assert effects == ["nonce.txt"]
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: dataclasses.replace(base, governance=ceiling),
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy_path))
        original_read = Path.read_text

        def checked_read(path, *args, **kwargs):
            assert path != other, "denied file was opened"
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", checked_read):
            assert rk._confined_read("read", {"path": str(nonce)}) == "allowed"
            assert "Blocked by governance policy" in rk._confined_read("read", {"path": str(other)})
        receipts = [
            json.loads(line) for line in (tmp_path / "read-effects.jsonl").read_text().splitlines()
        ]
        assert receipts == [{"path": str(nonce)}]

    def test_confined_positive_accepts_exact_autoapproved_read(self, rk, tmp_path):
        path = tmp_path / "nonce.txt"
        client = _ClientStub(
            [
                {
                    "running": False,
                    "messages": [
                        _tool(path, done=True, output="nonce"),
                        {"role": "assistant", "content": "nonce"},
                    ],
                }
            ]
        )
        assistant, _ = rk._await_completed_turn(
            client, "slot", path, "nonce", 1, require_manual=False
        )
        assert assistant["content"] == "nonce"
        assert client.posts == []

    def test_confined_positive_rejects_autoapproved_other_read(self, rk, tmp_path):
        client = _ClientStub(
            [
                {
                    "running": False,
                    "messages": [
                        _tool(tmp_path / "other", done=True, output="nonce"),
                        {"role": "assistant", "content": "nonce"},
                    ],
                }
            ]
        )
        with pytest.raises(AssertionError, match="terminal tool event"):
            rk._await_completed_turn(
                client, "slot", tmp_path / "nonce", "nonce", 1, require_manual=False
            )

    def test_negative_requires_policy_denial_not_model_refusal(self, rk, tmp_path):
        client = _ClientStub(
            [{"running": False, "messages": [{"role": "assistant", "content": "BLOCKED"}]}]
        )
        with pytest.raises(AssertionError, match="pre-execution"):
            rk._await_denied_read(client, "slot", tmp_path / "other", "secret-marker", 0)

    @pytest.mark.parametrize("empty_policy", [False, True])
    def test_missing_read_ceiling_fails_before_open(self, rk, tmp_path, monkeypatch, empty_policy):
        from types import SimpleNamespace

        from kiro_crew.platform.governance import parse_policy

        ceiling = (
            parse_policy({"version": 1, "boot": {"fail_closed": True}}) if empty_policy else None
        )
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=ceiling),
        )
        with patch.object(Path, "read_text") as read:
            with pytest.raises(RuntimeError, match="missing read ceiling"):
                rk._confined_read("read", {"path": str(tmp_path / "nonce")})
        read.assert_not_called()


class TestPrivateWriterIsolation:
    @pytest.mark.parametrize("outcome", ["success", "initialization", "timeout", "unconfirmed"])
    def test_owned_transcripts_without_map(self, rk, tmp_path, outcome):
        """Exercise the REAL harness cleanup, not a replacement cleanup context."""
        import io
        from types import SimpleNamespace

        from kiro_crew.testing import harness

        home = tmp_path / "rig"
        home.mkdir()
        outside = tmp_path / "host-transcript.jsonl"
        outside.write_bytes(b"untouched")
        transcript = (
            home / "kiro" / "sessions" / "cli" / ("d09f6a4a-e8c8-4a19-9598-1cb92b1d26d8.jsonl")
        )
        proc = SimpleNamespace(pid=9876, stdout=None, stderr=io.BytesIO(b""), poll=lambda: 0)
        seen = {}

        def spawn(_cmd, **kwargs):
            seen.update(kwargs)
            transcript.parent.mkdir(parents=True)
            transcript.write_bytes(b"synthetic native transcript")
            return proc

        def stop(*_args):
            assert transcript.exists(), "cleanup preceded writer termination"
            return outcome != "unconfirmed"

        def ready(*_args, **_kwargs):
            if outcome == "initialization":
                raise harness.GatewaySpawnError("initialization failed")
            return {"port": 51234, "token": "synthetic"}

        def preflight(env, cwd):
            assert env["KIRO_HOME"] == str(home / "kiro")
            assert env["KIROCREW_KIRO_BIN"] == "native-cli"
            return {
                "actual_home": str(home / "kiro"),
                "target": str(home / "kiro" / "agents"),
                "blockers": [],
            }

        expected = {
            "success": contextlib.nullcontext(),
            "initialization": pytest.raises(harness.GatewaySpawnError, match="initialization"),
            "timeout": pytest.raises(TimeoutError, match="turn timeout"),
            "unconfirmed": pytest.raises(AssertionError, match="stop unconfirmed"),
        }[outcome]
        with (
            patch.object(harness.tempfile, "mkdtemp", return_value=str(home)),
            patch.object(
                harness.subprocess, "run", return_value=type("CP", (), {"returncode": 0})()
            ),
            patch.object(harness.subprocess, "Popen", side_effect=spawn),
            patch.object(harness, "_wait_for_ready_line", side_effect=ready),
            patch.object(harness, "_terminate_process_group", side_effect=stop),
            patch("kiro_crew.agent._resolve_kirocrew_bin", return_value=sys.executable),
            patch.object(harness.shutil, "which", return_value=sys.executable),
            patch.object(rk, "_run_private_probe", side_effect=preflight),
            patch.object(rk, "_probe_signed_in") as auth,
            patch.object(rk, "_Client", lambda *_args: _ClientStub([{}])),
        ):
            with expected:
                with rk._booted_with_real_kiro("native-cli") as (handle, _client):
                    assert rk._native_transcript_count(handle.home / "kiro") == 1
                    assert not (home / "session_map.json").exists()
                    if outcome == "timeout":
                        raise TimeoutError("turn timeout")
        assert auth.call_args.args == ("native-cli", seen["env"], Path(seen["cwd"]))
        assert transcript.exists() is (outcome == "unconfirmed")
        assert home.exists() is (outcome == "unconfirmed")
        assert outside.read_bytes() == b"untouched"

    @pytest.mark.parametrize("location", ["missing", "host", "sibling"])
    def test_private_preflight_rejects_fallback_before_auth(
        self, rk, tmp_path, monkeypatch, location
    ):
        monkeypatch.setenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", "1")
        home = tmp_path / "rig"
        env = {"KIROCREW_HOME": str(home), "KIROCREW_KIRO_BIN": "native-cli"}
        if location != "missing":
            env["KIRO_HOME"] = str(tmp_path / location)

        @contextlib.contextmanager
        def spawn(**kwargs):
            assert "kiro_home" not in kwargs
            kwargs["before_spawn"](env, tmp_path)
            pytest.fail("unsafe preflight reached spawn")
            yield

        with (
            patch("kiro_crew.testing.harness.spawn_feature_gateway", side_effect=spawn),
            patch.object(rk, "_probe_signed_in") as auth,
            patch.object(rk, "_run_private_probe") as probe,
            pytest.raises(pytest.fail.Exception, match="private KIRO_HOME"),
        ):
            with rk._booted_with_real_kiro("native-cli"):
                pass
        auth.assert_not_called()
        probe.assert_not_called()

    def test_native_proof_requires_real_files_not_reader_override(self, rk, tmp_path, monkeypatch):
        from kiro_crew.config import paths

        other = tmp_path / "unrelated"
        other.mkdir()
        (other / "session.jsonl").write_bytes(b"not evidence")
        monkeypatch.setattr(paths, "_sessions_dir_override", lambda: other)
        with pytest.raises(AssertionError, match="native transcripts"):
            rk._native_transcript_count(tmp_path / "private")

    def test_native_proof_rejects_nonregular_entry(self, rk, tmp_path):
        root = tmp_path / "sessions" / "cli"
        root.mkdir(parents=True)
        (root / "d09f6a4a-e8c8-4a19-9598-1cb92b1d26d8.jsonl").mkdir()
        with pytest.raises(AssertionError, match="regular"):
            rk._native_transcript_count(tmp_path)

    @pytest.mark.parametrize("bad", ["actual_home", "target", "launcher"])
    def test_fresh_probe_disagreement_blocks_before_auth(self, rk, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("KIROCREW_E2E_REAL_KIRO_REQUIRE", "1")
        home = tmp_path / "not-created"
        env = {"KIROCREW_HOME": str(home), "KIRO_HOME": str(home / "kiro")}
        payload = {
            "actual_home": str(home / "kiro"),
            "target": str(home / "kiro" / "agents"),
            "blockers": [],
        }
        if bad == "launcher":
            payload["blockers"] = ["no launcher before boot"]
        else:
            payload[bad] = "wrong"

        @contextlib.contextmanager
        def spawn(**kwargs):
            kwargs["before_spawn"](env, tmp_path)
            pytest.fail("unsafe preflight reached gateway")
            yield

        with (
            patch("kiro_crew.testing.harness.spawn_feature_gateway", side_effect=spawn),
            patch.object(rk, "_run_private_probe", return_value=payload),
            patch.object(rk, "_probe_signed_in") as auth,
            pytest.raises(pytest.fail.Exception, match="before boot|private KIRO_HOME"),
        ):
            with rk._booted_with_real_kiro("native-cli"):
                pass
        auth.assert_not_called()

    def test_confirmed_stop_with_residue_is_not_success(self, rk, tmp_path):
        from types import SimpleNamespace

        home = tmp_path / "rig"
        home.mkdir()
        env = {
            "KIROCREW_HOME": str(home),
            "KIRO_HOME": str(home / "kiro"),
            "KIROCREW_KIRO_BIN": "native-cli",
        }
        payload = {
            "actual_home": str(home / "kiro"),
            "target": str(home / "kiro" / "agents"),
            "blockers": [],
        }
        handle = SimpleNamespace(
            home=home, port=1, token="synthetic", diagnostics=lambda: "", teardown_confirmed=True
        )

        @contextlib.contextmanager
        def spawn(**kwargs):
            kwargs["before_spawn"](env, tmp_path)
            yield handle  # Model a swallowed rmtree error after a confirmed stop.

        with (
            patch("kiro_crew.testing.harness.spawn_feature_gateway", side_effect=spawn),
            patch.object(rk, "_run_private_probe", return_value=payload),
            patch.object(rk, "_probe_signed_in"),
            patch.object(rk, "_Client", lambda *_args: _ClientStub([{}])),
            pytest.raises(AssertionError, match="residue remains"),
        ):
            with rk._booted_with_real_kiro("native-cli"):
                pass
        assert home.exists(), "smoke must not perform a second independent cleanup"
