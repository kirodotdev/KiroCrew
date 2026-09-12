"""Generated stub argv survives cmd.exe and preserves backend argument boundaries."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import hashing, rewriter, stub

CASES = [
    [],
    [""],
    ["-P", "-s"],
    ["-P", "-s", "space value"],
    ["value|with|pipes", ""],
    ["~", "trailing\\", '"quoted"', "unicode-éß中"],
    ["a&b", "c^d", "e%PATH%f", "g>h", "i<j", "k;l", "!bang!"],
]


def _entry(
    tmp_path: Path,
    args: list[str],
    identity: bool = False,
    auto_approve: list[str] | None = None,
    target_command: str = sys.executable,
) -> dict:
    return rewriter._build_stub_entry(
        stubs_dir=tmp_path / "stubs",
        server_name="probe",
        agent_name="probe-agent",
        original={"command": target_command, "args": args, "autoApprove": auto_approve or []},
        env_pairs={"ALPHA": "a", "BETA": "b"} if identity else {},
        target_command=target_command,
        socket_path=tmp_path / "absent.sock",
        work_dir=tmp_path,
        sandbox_mode="standard",
        approval_mode="interactive",
        identity_keys=("ALPHA", "BETA") if identity else (),
    )


def _parsed(entry: dict):
    return stub._parse_args(entry["args"][2:])


@pytest.mark.parametrize("args", CASES)
def test_codec_roundtrip(args):
    encoded = hashing.encode_target_args(args)
    assert set(encoded) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    assert hashing.decode_target_args(encoded) == args


@pytest.mark.parametrize("payload", ["", "W10=!", "!!!!", "é", "e30=", "WzFd", "//8="])
def test_bad_payload_rejected_without_echo(payload):
    with pytest.raises(ValueError) as error:
        hashing.decode_target_args(payload)
    assert str(error.value) in {
        "malformed target-args payload",
        "target-args payload is not a JSON array of strings",
    }


@pytest.mark.parametrize("args", CASES)
def test_emitted_stub_and_daemon_hash_same_argv(tmp_path, args):
    entry = _entry(tmp_path, args)
    parsed = _parsed(entry)
    payload = stub.build_register_payload(parsed)
    env: dict[str, str] = {}
    rewriter._collect_target_env({"probe": entry}, env)
    expected_hash = hashing.hash_command(sys.executable, args)
    assert payload["command_args_hash"] == expected_hash
    assert shlex.split(env["KIROCREW_MCP_TARGET_PROBE__" + expected_hash]) == [
        sys.executable,
        *args,
    ]
    assert stub._resolve_target_args(parsed) == args


@pytest.mark.parametrize("sep", ["|", "~"])
@pytest.mark.parametrize("equals", [True, False])
def test_legacy_separator_and_flag_spelling(tmp_path, sep, equals):
    entry = _entry(tmp_path, [])
    entry["args"] = [
        "-m",
        rewriter._STUB_MODULE,
        "--server",
        "probe",
        "--agent",
        "probe-agent",
        "--target-command",
        sys.executable,
        "--work-dir",
        str(tmp_path),
        "--target-args-sep",
        sep,
        "--pool-identity-env",
        sep.join(["ALPHA", "BETA"]),
    ]
    argv = ["value", "with spaces", ""]
    joined = sep.join(argv)
    entry["args"].extend([f"--target-args={joined}"] if equals else ["--target-args", joined])
    parsed = _parsed(entry)
    assert stub._resolve_target_args(parsed) == argv
    assert stub._resolve_pool_identity_env(parsed) == frozenset({"ALPHA", "BETA"})
    env: dict[str, str] = {}
    rewriter._collect_target_env({"probe": entry}, env)
    assert shlex.split(env["KIROCREW_MCP_TARGET_PROBE"]) == [sys.executable, *argv]


@pytest.mark.parametrize("equals", [True, False])
def test_encoded_precedence_matches_daemon(tmp_path, equals):
    entry = _entry(tmp_path, [])
    entry["args"] = [a for a in entry["args"] if not a.startswith("--target-args-b64=")]
    encoded = hashing.encode_target_args([""])
    entry["args"].extend(
        [f"--target-args-b64={encoded}"] if equals else ["--target-args-b64", encoded]
    )
    entry["args"].append("--target-args=must-not-be-used")
    assert stub._resolve_target_args(_parsed(entry)) == [""]
    env: dict[str, str] = {}
    rewriter._collect_target_env({"probe": entry}, env)
    assert shlex.split(env["KIROCREW_MCP_TARGET_PROBE"]) == [sys.executable, ""]


def test_empty_encoded_flag_never_falls_back(tmp_path):
    entry = _entry(tmp_path, [])
    entry["args"] = [a for a in entry["args"] if not a.startswith("--target-args-b64=")]
    entry["args"].extend(["--target-args-b64=", "--target-args=wrong"])
    with pytest.raises(ValueError):
        stub._resolve_target_args(_parsed(entry))
    with pytest.raises(ValueError):
        rewriter._collect_target_env({"probe": entry}, {})


def test_pool_identity_names_and_hash(tmp_path):
    entry = _entry(tmp_path, [], identity=True)
    parsed = _parsed(entry)
    assert stub._resolve_pool_identity_env(parsed) == frozenset({"ALPHA", "BETA"})
    assert "|" not in entry["args"][entry["args"].index("--pool-identity-env-b64") + 1]
    encoded = stub.build_register_payload(parsed)
    parsed.pool_identity_env_b64 = None
    parsed.pool_identity_env = "ALPHA|BETA"
    assert (
        stub.build_register_payload(parsed)["effective_env_hash"] == encoded["effective_env_hash"]
    )


def test_fallback_uses_decoded_argv(tmp_path, monkeypatch):
    parsed = _parsed(_entry(tmp_path, ["value|pipe", ""]))
    seen = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    def child(argv, env):
        seen.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(stub, "_fallback_spawn_child", child)
    with pytest.raises(SystemExit):
        stub.fallback_exec(parsed)
    assert seen == [[sys.executable, "value|pipe", ""]]


def _through_cmd(tmp_path: Path, entry: dict) -> dict:
    # Resolve from the OS system-directory API, not a mutable SystemRoot env var.
    cmd = platform_compat.trusted_system_bin("cmd")
    assert cmd is not None, "native Windows regression requires the system cmd.exe"
    receiver = tmp_path / "receiver.py"
    receiver.write_text(
        "import json\nfrom kiro_crew.mcp_gateway.stub import _parse_args, build_register_payload\n"
        "ns = _parse_args()\n"
        "print(json.dumps({'payload': build_register_payload(ns), 'args': vars(ns)}))\n",
        encoding="utf-8",
    )
    inner = subprocess.list2cmdline([sys.executable, str(receiver), *entry["args"][2:]])
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        PYTHONIOENCODING="utf-8",
    )
    completed = subprocess.run(
        f'"{cmd}" /d /s /c "{inner}"',
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native cmd.exe")
@pytest.mark.parametrize("argv", [["-P", "-s"], ["value|with|pipes", ""], ["a&b", "e%PATH%f"]])
def test_generated_argv_through_cmd(tmp_path, argv):
    # Exercise the producer, not just the codec in isolation.
    entry = _entry(tmp_path, argv, identity=True)
    result = _through_cmd(tmp_path, entry)
    assert result["payload"]["command_args_hash"] == hashing.hash_command(sys.executable, argv)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native cmd.exe")
@pytest.mark.parametrize("tools", [["read_file"], ["read_file", "write_file", "list,with,commas"]])
def test_generated_auto_approve_through_cmd(tmp_path, tools):
    entry = _entry(tmp_path, ["-P"], auto_approve=tools)
    direct = stub.build_register_payload(_parsed(entry))
    result = _through_cmd(tmp_path, entry)
    assert result["args"] == vars(_parsed(entry))
    for field in ("autoapprove_set_hash", "command_args_hash"):
        assert result["payload"][field] == direct[field]
    assert json.loads(result["args"]["auto_approve"]) == sorted(tools)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native cmd.exe")
def test_generated_paths_with_spaces_through_cmd(tmp_path):
    work_dir = tmp_path / "directory with spaces"
    work_dir.mkdir()
    target = str(work_dir / "python with spaces.exe")
    entry = _entry(work_dir, ["probe arg"], identity=True, target_command=target)
    direct = _parsed(entry)
    result = _through_cmd(work_dir, entry)
    assert result["args"] == vars(direct)
    expected = stub.build_register_payload(direct)
    for field in ("work_dir", "effective_env_hash", "command_args_hash"):
        assert result["payload"][field] == expected[field]
    for field in ("target_command", "work_dir", "env_file", "socket"):
        assert " " in getattr(direct, field)
        assert result["args"][field] == getattr(direct, field)


def test_rewrite_invalidates_legacy_fingerprint(tmp_path):
    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "probe.json").write_text(
        json.dumps(
            {
                "name": "probe",
                "mcpServers": {"probe": {"command": sys.executable, "args": ["-P", "-s"]}},
            }
        ),
        encoding="utf-8",
    )
    kwargs = dict(
        source_dir=agents,
        overlay_dir=tmp_path / "overlay" / "agents",
        socket_path=tmp_path / "socket",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"probe"}),
    )
    # A cache built with the delimiter-era schema must not pin that output.
    with patch.object(rewriter, "_FINGERPRINT_SCHEMA", 3):
        rewriter.rewrite_agents(**kwargs)
    with patch.object(
        rewriter, "_rewrite_single_spec", wraps=rewriter._rewrite_single_spec
    ) as rewrite:
        rewriter.rewrite_agents(**kwargs)
    assert rewrite.called


@pytest.mark.parametrize("windows", [False, True])
def test_large_generated_command_warns_on_windows(tmp_path, monkeypatch, caplog, windows):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    # Private target content must never appear in the warning.
    argument = "private-argument-" + "x" * 7000
    entry = _entry(tmp_path, [argument])
    assert stub._resolve_target_args(_parsed(entry)) == [argument]
    warnings = [r.message for r in caplog.records if "cmd.exe" in r.message]
    assert bool(warnings) == windows
    if windows:
        assert "probe" in warnings[0]
        assert "8191" in warnings[0]
        assert "private-argument" not in warnings[0]


def test_short_generated_command_does_not_warn(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _entry(tmp_path, ["-P", "-s"])
    assert not [r for r in caplog.records if "cmd.exe" in r.message]
