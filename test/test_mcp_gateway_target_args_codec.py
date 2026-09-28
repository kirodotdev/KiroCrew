"""Generated stub argv survives cmd.exe and preserves backend argument boundaries."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import hashing, rewriter, session_servers, stub

CASES = [
    [],
    [""],
    ["-P", "-s"],
    ["-P", "-s", "space value"],
    ["value|with|pipes", ""],
    ["~", "trailing\\", '"quoted"', "unicode-éß中"],
    ["a&b", "c^d", "e%PATH%f", "g>h", "i<j", "k;l", "!bang!"],
]

# cmd.exe expands a percent-delimited NAME that is set in its environment; the
# probe value carries no quote or delimiter so a single token stays one token.
PROBE = "KC_ARGV_PROBE"
PROBE_ENV = {PROBE: "expanded"}
PERCENT = f"%{PROBE}%"

# base64url payload plus the flag spelling and its ``=`` joiner.
SHELL_INERT = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
_STUB_PYTHON_PREFIX = ["-s", "-m", rewriter._STUB_MODULE]


def _stub_flags_argv(entry: dict) -> list[str]:
    args = entry["args"]
    if args[:3] == _STUB_PYTHON_PREFIX:
        return args[3:]
    assert args[:2] == ["-m", rewriter._STUB_MODULE]
    return args[2:]


def _entry(
    tmp_path: Path,
    args: list[str],
    identity: bool = False,
    auto_approve: list[str] | None = None,
    target_command: str = sys.executable,
    server_name: str = "probe",
    agent_name: str = "probe-agent",
    stubs_dir: Path | None = None,
    socket_path: Path | None = None,
    work_dir: Path | None = None,
) -> dict:
    return rewriter._build_stub_entry(
        stubs_dir=stubs_dir or tmp_path / "stubs",
        server_name=server_name,
        agent_name=agent_name,
        original={"command": target_command, "args": args, "autoApprove": auto_approve or []},
        env_pairs={"ALPHA": "a", "BETA": "b"} if identity else {},
        target_command=target_command,
        socket_path=socket_path or tmp_path / "absent.sock",
        work_dir=work_dir or tmp_path,
        sandbox_mode="standard",
        approval_mode="interactive",
        identity_keys=("ALPHA", "BETA") if identity else (),
    )


def _parsed(entry: dict):
    return stub._parse_args(_stub_flags_argv(entry))


def _percent_entry(tmp_path: Path, identity: bool = True) -> dict:
    """Every raw metadata value the rewriter emits carries the probe name."""
    base = tmp_path / f"dir{PERCENT}"
    work_dir = base / f"work{PERCENT}"
    work_dir.mkdir(parents=True)
    return _entry(
        tmp_path,
        [f"literal{PERCENT}", "-P"],
        identity=identity,
        auto_approve=[f"read{PERCENT}", "write_file"],
        target_command=str(base / f"python{PERCENT}.exe"),
        server_name=f"probe{PERCENT}",
        agent_name=f"agent{PERCENT}",
        stubs_dir=base / "stubs",
        socket_path=base / f"gw{PERCENT}.sock",
        work_dir=work_dir,
    )


def _modelled_cmd_expansion(tokens: list[str], env: dict[str, str]) -> list[str]:
    """cmd.exe ``/c`` substitutes ``%NAME%`` for a set NAME and leaves an unset
    one literal. Applied per token because the probe value has no quote."""
    return [re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1), m.group(0)), tok) for tok in tokens]


@pytest.mark.parametrize("args", CASES)
def test_codec_roundtrip(args):
    encoded = hashing.encode_target_args(args)
    assert set(encoded) <= SHELL_INERT
    assert hashing.decode_target_args(encoded) == args


@pytest.mark.parametrize("payload", ["", "W10=!", "!!!!", "é", "e30=", "WzFd", "//8="])
def test_bad_payload_rejected_without_echo(payload):
    with pytest.raises(ValueError) as error:
        hashing.decode_target_args(payload)
    assert str(error.value) in {
        "malformed target-args payload",
        "target-args payload is not a JSON array of strings",
    }


def test_generated_metadata_tokens_are_shell_inert(tmp_path, nonbundled_python_without_user_site):
    entry = _percent_entry(tmp_path)
    assert entry["args"][:3] == _STUB_PYTHON_PREFIX
    for token in _stub_flags_argv(entry):
        assert set(token) <= SHELL_INERT, token
    # The envelope is the only carrier; nothing raw rides beside it.
    flags = hashing.expand_stub_flags(_stub_flags_argv(entry))
    assert any(PERCENT in tok for tok in flags)
    assert "--auto-approve" in flags and "--env-file" in flags


def test_percent_metadata_survives_modelled_expansion(tmp_path):
    entry = _percent_entry(tmp_path)
    direct = _parsed(entry)
    parsed = stub._parse_args(_modelled_cmd_expansion(_stub_flags_argv(entry), PROBE_ENV))
    assert vars(parsed) == vars(direct)
    for field in ("server", "agent", "target_command", "work_dir", "socket", "env_file"):
        assert PERCENT in getattr(parsed, field), field
    assert stub._parse_auto_approve(parsed.auto_approve) == [f"read{PERCENT}", "write_file"]
    assert stub._resolve_target_args(parsed) == [f"literal{PERCENT}", "-P"]
    payload = stub.build_register_payload(parsed)
    expected = stub.build_register_payload(direct)
    for field in ("autoapprove_set_hash", "command_args_hash", "effective_env_hash"):
        assert payload[field] == expected[field]
    # The daemon side reads the same envelope and hashes the same argv.
    env: dict[str, str] = {}
    rewriter._collect_target_env({"probe": entry}, env)
    assert env["KIROCREW_MCP_TARGET_PROBE__" + payload["command_args_hash"]] == " ".join(
        shlex.quote(p) for p in [parsed.target_command, f"literal{PERCENT}", "-P"]
    )


def test_plain_flag_overlay_parses_to_identical_payload(
    tmp_path, nonbundled_python_without_user_site
):
    """An overlay written before the envelope keeps its meaning and its hashes."""
    entry = _percent_entry(tmp_path)
    plain = dict(entry, args=entry["args"][:3] + hashing.expand_stub_flags(_stub_flags_argv(entry)))
    assert not any(t.startswith(hashing.STUB_FLAGS_FLAG) for t in plain["args"])
    assert vars(_parsed(plain)) == vars(_parsed(entry))
    from_plain = stub.build_register_payload(_parsed(plain))
    from_envelope = stub.build_register_payload(_parsed(entry))
    for field in ("autoapprove_set_hash", "command_args_hash", "effective_env_hash", "work_dir"):
        assert from_plain[field] == from_envelope[field]
    enveloped_env: dict[str, str] = {}
    plain_env: dict[str, str] = {}
    rewriter._collect_target_env({"probe": entry}, enveloped_env)
    rewriter._collect_target_env({"probe": plain}, plain_env)
    assert enveloped_env == plain_env


@pytest.mark.parametrize("equals", [True, False])
def test_expand_stub_flags_splices_in_place(equals):
    inner = ["--server", "s%X%", "--work-dir", "C:\\w%X%\\d", "--poolable"]
    encoded = hashing.encode_target_args(inner)
    envelope = (
        [f"{hashing.STUB_FLAGS_FLAG}={encoded}"] if equals else [hashing.STUB_FLAGS_FLAG, encoded]
    )
    argv = ["--agent", "a", *envelope, "--channel-id", "C1"]
    assert hashing.expand_stub_flags(argv) == ["--agent", "a", *inner, "--channel-id", "C1"]


def test_expand_stub_flags_passes_plain_and_non_string_tokens():
    argv = ["--server", "s", 7, None, "--poolable"]
    assert hashing.expand_stub_flags(argv) == argv
    assert hashing.expand_stub_flags([]) == []


@pytest.mark.parametrize(
    "argv",
    [
        [f"{hashing.STUB_FLAGS_FLAG}="],
        [f"{hashing.STUB_FLAGS_FLAG}=!!!!"],
        [f"{hashing.STUB_FLAGS_FLAG}={hashing.encode_target_args(['ok'])[:-3]}"],
        [hashing.STUB_FLAGS_FLAG],
    ],
)
def test_expand_stub_flags_rejects_malformed_envelope(argv):
    with pytest.raises(ValueError):
        hashing.expand_stub_flags(argv)
    with pytest.raises(ValueError):
        stub._parse_args(["--server", "s", *argv])


def test_session_channel_id_rides_the_envelope(tmp_path):
    entry = _percent_entry(tmp_path)
    shaped = session_servers._acp_server_entry("probe", entry, f"C{PERCENT}")
    assert shaped is not None
    for token in _stub_flags_argv(shaped):
        assert set(token) <= SHELL_INERT, token
    parsed = stub._parse_args(_modelled_cmd_expansion(_stub_flags_argv(shaped), PROBE_ENV))
    assert parsed.channel_id == f"C{PERCENT}"
    assert vars(parsed) == dict(vars(_parsed(entry)), channel_id=f"C{PERCENT}")
    # A second shaping of an entry that already names its channel adds nothing.
    again = session_servers._acp_server_entry("probe", shaped, "other")
    assert again is not None and again["args"] == shaped["args"]
    assert session_servers._acp_server_entry("probe", entry, None)["args"] == entry["args"]


@pytest.mark.parametrize("channel_id", [None, "C1"])
def test_session_skips_a_stub_with_an_unreadable_envelope(tmp_path, caplog, channel_id):
    """An overlay entry whose envelope cannot be decoded is left out of the
    session, like a command-less one; the session itself still starts."""
    entry = _percent_entry(tmp_path)
    broken = dict(entry, args=entry["args"][:3] + [f"{hashing.STUB_FLAGS_FLAG}=!!!!"])
    assert session_servers._acp_server_entry("probe", broken, channel_id) is None
    assert any("unreadable flag envelope" in r.message for r in caplog.records)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "agent.json").write_text(
        json.dumps({"mcpServers": {"broken": broken, "probe": entry}}), encoding="utf-8"
    )
    injected = session_servers.pooled_session_servers(overlay, "agent", channel_id)
    assert [s["name"] for s in injected] == ["probe"]


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


def _plain_flags(entry: dict) -> list[str]:
    """Rewrite an emitted entry into the pre-envelope plain-flag overlay shape."""
    return entry["args"][:3] + hashing.expand_stub_flags(_stub_flags_argv(entry))


@pytest.mark.parametrize("equals", [True, False])
def test_encoded_precedence_matches_daemon(tmp_path, equals):
    entry = _entry(tmp_path, [])
    entry["args"] = [a for a in _plain_flags(entry) if not a.startswith("--target-args-b64=")]
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
    entry["args"] = [a for a in _plain_flags(entry) if not a.startswith("--target-args-b64=")]
    entry["args"].extend(["--target-args-b64=", "--target-args=wrong"])
    with pytest.raises(ValueError):
        stub._resolve_target_args(_parsed(entry))
    with pytest.raises(ValueError):
        rewriter._collect_target_env({"probe": entry}, {})


def test_pool_identity_names_and_hash(tmp_path):
    entry = _entry(tmp_path, [], identity=True)
    parsed = _parsed(entry)
    assert stub._resolve_pool_identity_env(parsed) == frozenset({"ALPHA", "BETA"})
    flags = hashing.expand_stub_flags(_stub_flags_argv(entry))
    assert "|" not in flags[flags.index("--pool-identity-env-b64") + 1]
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


def _through_cmd(tmp_path: Path, entry: dict, extra_env: dict[str, str] | None = None) -> dict:
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
    inner = subprocess.list2cmdline([sys.executable, str(receiver), *_stub_flags_argv(entry)])
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        PYTHONIOENCODING="utf-8",
        **(extra_env or {}),
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


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native cmd.exe")
def test_generated_percent_metadata_through_cmd(tmp_path):
    """With the probe SET in the child's environment, every raw metadata value
    arrives literal and the parsed hashes match a direct parse."""
    entry = _percent_entry(tmp_path)
    direct = _parsed(entry)
    result = _through_cmd(tmp_path, entry, PROBE_ENV)
    assert result["args"] == vars(direct)
    for field in ("server", "agent", "target_command", "work_dir", "socket", "env_file"):
        assert PERCENT in result["args"][field], field
        assert "expanded" not in result["args"][field], field
    assert json.loads(result["args"]["auto_approve"]) == [f"read{PERCENT}", "write_file"]
    expected = stub.build_register_payload(direct)
    for field in ("autoapprove_set_hash", "command_args_hash", "effective_env_hash", "work_dir"):
        assert result["payload"][field] == expected[field]
    assert result["payload"]["command_args_hash"] == hashing.hash_command(
        direct.target_command, [f"literal{PERCENT}", "-P"]
    )


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native cmd.exe")
def test_plain_flag_metadata_expands_through_cmd(tmp_path):
    """The pre-envelope overlay shape is what the issue measured: the same
    percent-bearing values expand when the flags cross cmd.exe raw. This pins
    the premise so the envelope test above is known to be load-bearing."""
    entry = _percent_entry(tmp_path)
    plain = dict(entry, args=_plain_flags(entry))
    result = _through_cmd(tmp_path, plain, PROBE_ENV)
    assert result["args"]["target_command"].endswith("pythonexpanded.exe")
    assert "readexpanded" in json.loads(result["args"]["auto_approve"])


@pytest.mark.parametrize("schema", [3, 4])
def test_rewrite_invalidates_legacy_fingerprint(tmp_path, schema):
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
    # A cache built with the delimiter-era (3) or plain-flag-era (4) schema
    # must not pin that output.
    with patch.object(rewriter, "_FINGERPRINT_SCHEMA", schema):
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
