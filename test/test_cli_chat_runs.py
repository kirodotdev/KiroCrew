"""``kirocrew chat --list`` and ``--continue``: the run record and the restore."""

import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_chat, cli_chat_runs
from kiro_crew.config import KiroCrewConfig

#: ``_chat`` builds a ``SkillsLoader``, whose index and catalog worker outlive the test.
pytestmark = pytest.mark.usefixtures("close_skills_loaders")


class _Provider:
    """A KAS provider that restores the session it is asked for, unless told not to.

    Its start swaps in a new client, as the kiro path does, so only a read after the
    start sees whether the load succeeded.
    """

    is_kas_backend = True

    def __init__(self, session_id: str, *, restores: bool = True) -> None:
        self.session_id = session_id
        self.restores = restores
        self.asked = ""
        self.client = SimpleNamespace(resumed=False, set_resume_session_id=self._ask)
        self.start = AsyncMock(side_effect=self._start)
        self.shutdown = AsyncMock()
        self.context_usage_pct = lambda: 0.0

    def _ask(self, sid: str) -> None:
        self.asked = sid

    async def _start(self) -> None:
        restored = bool(self.asked) and self.restores
        self.session_id = self.asked if restored else self.session_id
        self.client = SimpleNamespace(resumed=restored)


@pytest.fixture
def chat_home(monkeypatch, tmp_path):
    """An isolated data home, a project directory to run in, and a stubbed turn."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    cfg = KiroCrewConfig()
    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent, kiro_agent="": MagicMock())
    send = AsyncMock()
    monkeypatch.setattr(cli_chat, "_send_and_print", send)
    # Every run here shares this test's process; a real earlier run has exited.
    monkeypatch.setattr(cli_chat_runs.platform_compat, "process_incarnation_id", lambda pid: None)
    return SimpleNamespace(project=project, send=send)


def _serve(monkeypatch, provider) -> None:
    monkeypatch.setattr(cli_chat, "build_provider_factory", lambda config: lambda *a, **k: provider)


def _eof() -> str:
    raise EOFError


def _write(home, entries) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / cli_chat_runs.RUNS_FILE).write_text(json.dumps(entries))


def _entry(session_id, cwd, *, agent="", pid=0, pid_token=""):
    return {
        "session_id": session_id,
        "cwd": str(cwd),
        "last_at": 1.0,
        "first_prompt": "hi",
        "agent": agent,
        "pid": pid,
        "pid_token": pid_token,
    }


@pytest.mark.asyncio
async def test_continue_reopens_the_last_chat_run_in_this_directory(
    monkeypatch, chat_home, capsys
) -> None:
    _serve(monkeypatch, _Provider("sess-1"))
    typed = iter(["fix the parser"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed, None) or _eof())
    await cli_chat._chat(None, None, run=cli_chat_runs.ChatRun())

    second = _Provider("sess-fresh")
    _serve(monkeypatch, second)
    run = cli_chat_runs.ChatRun(resume=cli_chat_runs.choose_resume(None))
    await cli_chat._chat("and the lexer?", None, run=run)

    assert second.asked == "sess-1"
    assert chat_home.send.await_count == 2
    assert "Continuing your chat from" in capsys.readouterr().err
    cli_chat_runs.print_runs()
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 1 and listed[0].startswith("sess-1  ")
    assert listed[0].endswith(f"{chat_home.project.resolve()}  fix the parser")


def test_a_chat_the_backend_does_not_restore_is_refused_and_sends_nothing(
    monkeypatch, chat_home, tmp_path
) -> None:
    _write(tmp_path / "home", [_entry("sess-1", chat_home.project.resolve())])
    provider = _Provider("sess-fresh", restores=False)
    _serve(monkeypatch, provider)
    run = cli_chat_runs.ChatRun(resume=cli_chat_runs.choose_resume(None))

    with pytest.raises(cli_chat_runs.CannotContinue, match="could not be restored"):
        cli_chat._run_chat("hello", None, run=run)

    chat_home.send.assert_not_awaited()
    provider.shutdown.assert_awaited_once()


def test_continue_with_no_earlier_chat_exits_1_before_a_backend_is_built(
    monkeypatch, chat_home, capsys
) -> None:
    from kiro_crew import cli

    monkeypatch.setattr(cli_chat, "build_provider_factory", lambda config: pytest.fail("built"))
    monkeypatch.setattr(sys, "argv", ["kirocrew", "chat", "--continue", "-m", "x"])
    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "No earlier terminal chat in this directory started without --agent." in err


@pytest.mark.asyncio
async def test_continue_on_another_harness_is_refused_before_it_starts(
    monkeypatch, chat_home, tmp_path
) -> None:
    _write(tmp_path / "home", [_entry("sess-1", chat_home.project.resolve())])
    provider = _Provider("sess-1")
    provider.is_kas_backend = False
    _serve(monkeypatch, provider)
    run = cli_chat_runs.ChatRun(resume=cli_chat_runs.choose_resume(None))

    with pytest.raises(cli_chat_runs.CannotContinue, match="KAS and Kiro CLI backends only"):
        await cli_chat._chat("hello", None, run=run)

    provider.start.assert_not_awaited()


def test_continue_picks_only_this_directory_and_this_agent(chat_home, tmp_path) -> None:
    here = chat_home.project.resolve()
    _write(
        tmp_path / "home",
        [
            _entry("sess-elsewhere", tmp_path),
            _entry("sess-reviewer", here, agent="reviewer"),
            _entry("../../escape", here),
            _entry("sess-mine", here),
        ],
    )

    assert cli_chat_runs.choose_resume(None).session_id == "sess-mine"
    assert cli_chat_runs.choose_resume("reviewer").session_id == "sess-reviewer"
    with pytest.raises(cli_chat_runs.CannotContinue, match="for --agent builder"):
        cli_chat_runs.choose_resume("builder")


@pytest.mark.parametrize(
    "last_at", [float("inf"), float("nan"), 10**400], ids=["inf", "nan", "huge"]
)
def test_list_ignores_an_entry_whose_time_cannot_be_shown(
    chat_home, tmp_path, capsys, last_at
) -> None:
    here = chat_home.project.resolve()
    _write(
        tmp_path / "home",
        [{**_entry("sess-bad", here), "last_at": last_at}, _entry("sess-ok", here)],
    )

    cli_chat_runs.print_runs()

    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["sess-ok"]


@pytest.mark.parametrize(
    ("field", "bound"),
    [
        ("first_prompt", cli_chat_runs.FIRST_PROMPT_CHARS),
        ("cwd", cli_chat_runs.MAX_FIELD_CHARS),
        ("agent", cli_chat_runs.MAX_FIELD_CHARS),
        ("pid_token", cli_chat_runs.MAX_FIELD_CHARS),
    ],
)
def test_an_entry_holding_text_longer_than_its_bound_is_ignored(
    chat_home, tmp_path, field, bound
) -> None:
    here = chat_home.project.resolve()
    _write(
        tmp_path / "home",
        [{**_entry("sess-bad", here), field: "x" * (bound + 1)}, _entry("sess-ok", here)],
    )

    assert [entry.session_id for entry in cli_chat_runs.read_runs()] == ["sess-ok"]


def test_continue_refuses_a_chat_still_open_in_another_terminal(
    monkeypatch, chat_home, tmp_path
) -> None:
    open_run = _entry("sess-1", chat_home.project.resolve(), pid=4242, pid_token="t")
    _write(tmp_path / "home", [open_run])
    alive = {4242: "t", os.getpid(): "me"}
    compat = cli_chat_runs.platform_compat
    monkeypatch.setattr(compat, "process_incarnation_id", lambda pid: alive.get(pid))
    monkeypatch.setattr(compat, "own_process_start_time", lambda: "me")

    with pytest.raises(cli_chat_runs.CannotContinue, match="still open"):
        cli_chat_runs.choose_resume(None)

    del alive[4242]  # that terminal closed; this one now continues the chat
    assert cli_chat_runs.choose_resume(None).session_id == "sess-1"
    with pytest.raises(cli_chat_runs.CannotContinue, match="still open"):
        cli_chat_runs.choose_resume(None)  # a third terminal, before this one sent a turn


def test_a_record_that_cannot_be_claimed_is_a_refusal_not_a_traceback(
    monkeypatch, chat_home, tmp_path
) -> None:
    _write(tmp_path / "home", [_entry("sess-1", chat_home.project.resolve())])

    def _read_only(*args, **kwargs):
        raise PermissionError("read-only data home")

    monkeypatch.setattr(cli_chat_runs, "atomic_write", _read_only)
    with pytest.raises(cli_chat_runs.CannotContinue, match="read-only data home"):
        cli_chat_runs.choose_resume(None)


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_written_does_not_stop_the_chat(
    monkeypatch, chat_home, tmp_path, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger=cli_chat_runs.__name__)
    _serve(monkeypatch, _Provider("sess-1"))

    def _read_only(*args, **kwargs):
        raise PermissionError("read-only data home")

    monkeypatch.setattr(cli_chat_runs, "atomic_write", _read_only)
    await cli_chat._chat("hello", None, run=cli_chat_runs.ChatRun())

    chat_home.send.assert_awaited_once()
    assert not (tmp_path / "home" / cli_chat_runs.RUNS_FILE).exists()
    assert "Could not record this terminal chat for --continue: read-only data home" in caplog.text


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_not_replaced(
    monkeypatch, chat_home, tmp_path
) -> None:
    here = chat_home.project.resolve()
    _write(tmp_path / "home", [_entry("sess-a", here), _entry("sess-b", here)])
    before = (tmp_path / "home" / cli_chat_runs.RUNS_FILE).read_bytes()

    def _unreadable(*args, **kwargs):
        raise PermissionError("held open by another process")

    monkeypatch.setattr(cli_chat_runs, "read_bytes_with_retry", _unreadable)
    await cli_chat_runs.note_turn(cli_chat_runs.ChatRun(), _Provider("sess-new"), "hi")

    assert (tmp_path / "home" / cli_chat_runs.RUNS_FILE).read_bytes() == before


@pytest.mark.asyncio
async def test_the_entry_follows_the_run_onto_its_next_session(chat_home) -> None:
    run = cli_chat_runs.ChatRun()
    provider = _Provider("sess-1")
    await cli_chat_runs.note_turn(run, provider, "first question")
    provider.session_id = "sess-2"  # an auto-compact restart opened a new session
    await cli_chat_runs.note_turn(run, provider, "second question")

    entries = cli_chat_runs.read_runs()
    assert [(e.session_id, e.first_prompt) for e in entries] == [("sess-2", "first question")]


def test_continue_picks_the_newest_chat_even_when_an_older_turn_is_recorded_last(
    chat_home,
) -> None:
    # Two terminals' turns overlapped, and the older turn's write took the file lock last.
    cli_chat_runs._record(cli_chat_runs.ChatRun(first_prompt="newer"), "sess-new", 200.0)
    cli_chat_runs._record(cli_chat_runs.ChatRun(first_prompt="older"), "sess-old", 100.0)

    assert [e.session_id for e in cli_chat_runs.read_runs()] == ["sess-new", "sess-old"]
    assert cli_chat_runs.choose_resume(None).session_id == "sess-new"


@pytest.mark.asyncio
async def test_a_turn_that_did_not_complete_records_nothing(
    monkeypatch, chat_home, tmp_path
) -> None:
    _serve(monkeypatch, _Provider("sess-1"))
    chat_home.send.side_effect = SystemExit(1)

    with pytest.raises(SystemExit):
        await cli_chat._chat("hello", None, run=cli_chat_runs.ChatRun())

    assert not (tmp_path / "home" / cli_chat_runs.RUNS_FILE).exists()


@pytest.mark.parametrize("flag", ["--list", "--continue"])
def test_the_new_flags_are_refused_with_headless(monkeypatch, capsys, flag) -> None:
    from kiro_crew import cli

    monkeypatch.setattr(sys, "argv", ["kirocrew", "chat", "--headless", "-m", "x", flag])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert "--list and --continue are not --headless flags" in capsys.readouterr().err
