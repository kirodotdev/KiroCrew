"""The pinned kiro-cli's probes run inside the sandbox.

The pinned install can sit in ``~/.local/bin``, which the sandboxed agent writes as
the same account. So ``--version``, ``acp --help``, ``chat --help`` and the KAS v3
start must reach the sandbox chokepoint, never a plain spawn of that path.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from kiro_crew import kiro_cli, kiro_prerequisite
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.kiro_prerequisite_process import ProcessResult


@pytest.fixture
def pinned(tmp_path, monkeypatch) -> str:
    """An executable stand-in that is the pinned install, at the ``standard`` tier."""
    binary = tmp_path / "kiro-cli"
    binary.write_bytes(b"")
    binary.chmod(0o755)
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
    for cache in ("_version_cache", "_chat_engines_cache", "_kas_start_cache"):
        monkeypatch.setattr(kiro_cli, cache, {})

    monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "standard")
    return str(binary)


@pytest.fixture
def runner(monkeypatch):
    """Record each call into the sandboxed probe runner and answer it."""
    calls: list[dict] = []
    answer = {"result": ProcessResult(ok=True, output="", returncode=0)}

    async def _run(command, args, **kwargs):
        calls.append({"argv": [command, *args], **kwargs})
        return answer["result"]

    monkeypatch.setattr(kiro_prerequisite, "_run_process", _run)
    return SimpleNamespace(calls=calls, answer=answer)


def _answer(runner, output: str, returncode: int = 0) -> None:
    runner.answer["result"] = ProcessResult(
        ok=returncode == 0, output=output, returncode=returncode
    )


@pytest.mark.parametrize(
    ("probe", "args", "output"),
    [
        (kiro_cli.kiro_cli_version_at, ["--version"], "kiro-cli 2.40.0\n"),
        (kiro_cli.kas_relay_help, ["acp", "--help"], "Usage: kiro-cli acp\n"),
        (kiro_cli.chat_agent_engines_at, ["chat", "--help"], "Usage: kiro-cli chat\n"),
    ],
    ids=["version", "acp-help", "chat-help"],
)
def test_each_probe_runs_through_the_sandboxed_runner(pinned, runner, probe, args, output):
    _answer(runner, output)
    probe(pinned)
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == [pinned, *args]
    assert call["sandbox_mode"] == "standard"
    # Keeps a host with no sandbox backend probing, as before.
    assert call["first_party_fixed_argv"] is True


def test_the_answers_are_read_from_the_sandboxed_output(pinned, runner):
    _answer(runner, "kiro-cli 2.40.0\n")
    assert kiro_cli.kiro_cli_version_at(pinned) == (2, 40, 0)
    _answer(runner, "      --agent-engine <ENGINE>\n          [possible values: v2, v3]\n")
    assert kiro_cli.chat_agent_engines_at(pinned) == ("v2", "v3")
    _answer(runner, "Usage: kiro-cli acp [OPTIONS]\n", returncode=2)
    # Help that ran without the selector is an answer, not an unknown.
    assert kiro_cli.kas_relay_help(pinned) == "Usage: kiro-cli acp [OPTIONS]\n"


def test_a_line_after_the_version_does_not_hide_it(pinned, runner):
    """stdout and stderr arrive as one text from the runner."""
    _answer(runner, "kiro-cli 2.40.0\nan update is available\n")
    assert kiro_cli.kiro_cli_version_at(pinned) == (2, 40, 0)


def test_a_sandbox_refusal_is_unknown_and_not_cached(pinned, runner):
    runner.answer["result"] = ProcessResult(
        ok=False, returncode=1, sandbox_failure=("transient", "detail", "remedy")
    )
    assert kiro_cli.kiro_cli_version_at(pinned) is None
    assert kiro_cli.chat_agent_engines_at(pinned) is None
    assert kiro_cli.kas_relay_help(pinned) is None
    _answer(runner, "kiro-cli 2.40.0\n")
    assert kiro_cli.kiro_cli_version_at(pinned) == (2, 40, 0)


def test_a_timeout_or_a_full_capture_is_unknown(pinned, runner):
    from kiro_crew.kiro_prerequisite_process import _MAX_CAPTURED_OUTPUT

    runner.answer["result"] = ProcessResult(ok=False, timed_out=True, error="process timed out")
    assert kiro_cli.kas_relay_help(pinned) is None
    _answer(runner, "x" * _MAX_CAPTURED_OUTPUT)
    assert kiro_cli.kas_relay_help(pinned) is None


def test_an_absent_binary_is_not_probed(tmp_path, runner):
    assert kiro_cli.kas_relay_help(str(tmp_path / "absent")) is None
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_probe_called_on_a_running_loop_still_answers(pinned, runner):
    """Main's plain spawn answered here too; the runner moves to a worker thread."""
    _answer(runner, "kiro-cli 2.40.0\n")
    assert kiro_cli.kiro_cli_version_at(pinned) == (2, 40, 0)


def test_the_kas_start_probe_runs_through_the_sandboxed_runner(pinned, runner):
    tail = ("acp", "--agent-engine", "v3")
    _answer(runner, "V3 is not supported here\n", returncode=1)
    assert kiro_cli.kas_relay_start_output(pinned, tail) == "V3 is not supported here\n"
    (call,) = runner.calls
    assert call["argv"] == [pinned, *tail]
    assert call["sandbox_mode"] == "standard"
    assert call["first_party_fixed_argv"] is True
    # The supervisor ends what the start left in its process group.
    assert call["reap_survivors"] is True
    # Cached per binary identity: the second answer runs nothing.
    assert kiro_cli.kas_relay_start_output(pinned, tail) == "V3 is not supported here\n"
    assert len(runner.calls) == 1


def test_a_kas_start_the_sandbox_refused_is_unknown_and_not_cached(pinned, runner):
    from kiro_crew.kiro_prerequisite_process import ProcessResult

    runner.answer["result"] = ProcessResult(
        ok=False, returncode=1, sandbox_failure=("transient", "detail", "remedy")
    )
    assert kiro_cli.kas_relay_start_output(pinned, ("acp",)) is None
    assert kiro_cli._kas_start_cache == {}


def test_the_unattended_whoami_runs_through_the_sandboxed_runner(pinned, runner, monkeypatch):
    from kiro_crew.cloud import login_target

    monkeypatch.setattr(login_target, "pin_kiro_cli", lambda: (pinned, False))
    _answer(runner, '{"accountType": "BuilderId", "email": "a@example.com"}\nProfile: x\n')
    assert login_target.whoami_names_personal_account() is True
    assert runner.calls[0]["argv"] == [pinned, "whoami", "--format", "json"]
    assert runner.calls[0]["first_party_fixed_argv"] is True
    runner.answer["result"] = ProcessResult(ok=False, timed_out=True, error="process timed out")
    assert login_target.whoami_names_personal_account() is False


@pytest.mark.skipif(
    sys.platform != "linux" or not sandbox_mod.userns_available(),
    reason="needs the Linux namespace sandbox",
)
def test_the_version_probe_child_runs_under_the_sandbox_launcher(tmp_path, monkeypatch):
    """A real run: the stand-in reports whether the launcher's marker reached it."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(kiro_cli, "_version_cache", {})
    monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "standard")
    binary = tmp_path / "kiro-cli"
    binary.write_text(
        '#!/bin/sh\nif [ -n "$KIROCREW_SANDBOX_ACTIVE" ]; then echo "kiro-cli 2.40.1"; '
        'else echo "kiro-cli 2.40.0"; fi\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    assert kiro_cli.kiro_cli_version_at(str(binary)) == (2, 40, 1)
