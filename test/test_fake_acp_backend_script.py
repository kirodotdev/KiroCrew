"""The fake ACP backend's reply script (``KIROCREW_FAKE_ACP_SCRIPT``).

In-process through ``fake._handle`` like ``test_fake_acp_backend.py``, plus one
run of the real module as a child process speaking ACP over stdio, so the
whole path a gateway drives is covered without an installed kiro-cli.
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import hooks
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing import fake_acp_backend as fake
from kiro_crew.testing import scripted_replies
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_FRAME_CAP = 1024 * 1024
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _fresh_script_state(_floor_monkeypatch):
    """Loaded scripts and session state are module globals; never share them."""
    _floor_monkeypatch.setattr(scripted_replies, "_LOADED", {})
    _floor_monkeypatch.setattr(fake, "_SESSIONS", {"s1": ([], ""), "s2": ([], "")})
    _floor_monkeypatch.delenv(scripted_replies.SCRIPT_ENV, raising=False)


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(fake.sys, "stdout", buf)
    return buf


def _messages(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _prompt(text: str, req_id: int = 1, session_id: str = "s1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    }


def _texts(msgs: list[dict[str, Any]]) -> list[str]:
    return [
        m["params"]["update"]["content"]["text"]
        for m in msgs
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    ]


def _script(tmp_path: Path, monkeypatch, body: Any) -> Path:
    path = tmp_path / "replies.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    monkeypatch.setenv(scripted_replies.SCRIPT_ENV, str(path))
    return path


def _turn(monkeypatch, text: str, session_id: str = "s1") -> list[dict[str, Any]]:
    buf = _capture(monkeypatch)
    fake._handle(_prompt(text, session_id=session_id))
    return _messages(buf)


def test_without_a_script_a_plain_prompt_keeps_the_canned_reply(monkeypatch):
    msgs = _turn(monkeypatch, "hello")
    assert _texts(msgs) == [fake.REPLY_TEXT]
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}


def test_ordered_replies_answer_one_turn_each_then_the_canned_reply_returns(tmp_path, monkeypatch):
    _script(tmp_path, monkeypatch, {"replies": [{"text": "first"}, {"text": "second"}]})
    assert _texts(_turn(monkeypatch, "a")) == ["first"]
    assert _texts(_turn(monkeypatch, "b")) == ["second"]
    last = _turn(monkeypatch, "c")
    assert _texts(last) == [fake.REPLY_TEXT]
    assert last[-1]["result"] == {"stopReason": "end_turn"}


def test_a_when_entry_answers_every_matching_prompt_without_using_up_the_sequence(
    tmp_path, monkeypatch
):
    _script(
        tmp_path,
        monkeypatch,
        {"replies": [{"when": "deploy", "text": "not without approval"}, {"text": "hi"}]},
    )
    assert _texts(_turn(monkeypatch, "please deploy it")) == ["not without approval"]
    assert _texts(_turn(monkeypatch, "deploy again")) == ["not without approval"]
    assert _texts(_turn(monkeypatch, "hello")) == ["hi"]


def test_each_session_walks_the_sequence_from_its_own_start(tmp_path, monkeypatch):
    _script(tmp_path, monkeypatch, {"replies": [{"text": "one"}, {"text": "two"}]})
    assert _texts(_turn(monkeypatch, "x", "s1")) == ["one"]
    assert _texts(_turn(monkeypatch, "x", "s2")) == ["one"]
    assert _texts(_turn(monkeypatch, "x", "s1")) == ["two"]


def test_scripted_tool_calls_stream_before_the_reply(tmp_path, monkeypatch):
    tool = {"title": "Run tests", "kind": "execute", "input": {"command": "pytest"}}
    failed = {"title": "Push", "status": "failed"}
    _script(tmp_path, monkeypatch, {"replies": [{"text": "done", "tools": [tool, failed]}]})
    msgs = _turn(monkeypatch, "go")
    updates = [m["params"]["update"] for m in msgs if m.get("method") == "session/update"]
    kinds = [u["sessionUpdate"] for u in updates]
    assert kinds == [
        "tool_call",
        "tool_call_update",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    first, first_done, second, second_done = updates[:4]
    assert (first["title"], first["kind"], first["rawInput"]) == (
        "Run tests",
        "execute",
        {"command": "pytest"},
    )
    assert first_done["toolCallId"] == first["toolCallId"] != second["toolCallId"]
    assert (first_done["status"], second["kind"], second_done["status"]) == (
        "completed",
        "other",
        "failed",
    )


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_a_scripted_stop_reason_ends_the_turn(tmp_path, monkeypatch, stop):
    _script(tmp_path, monkeypatch, {"replies": [{"text": "cut", "stop": stop}]})
    assert _turn(monkeypatch, "x")[-1]["result"] == {"stopReason": stop}


def test_a_scripted_error_fails_the_turn_with_its_message(tmp_path, monkeypatch):
    _script(tmp_path, monkeypatch, {"replies": [{"error": "model unavailable"}]})
    (msg,) = _turn(monkeypatch, "x")
    assert msg["error"] == {"code": fake.ERROR_CODE, "message": "model unavailable"}


def test_a_naming_prompt_never_uses_up_a_scripted_turn(tmp_path, monkeypatch):
    _script(tmp_path, monkeypatch, {"replies": [{"text": "first"}]})
    naming = _turn(monkeypatch, fake.NAMING_PROMPT_OPENING + " Title this.")
    assert _texts(naming) == [fake.NAMING_REPLY_TEXT]
    assert _texts(_turn(monkeypatch, "hello")) == ["first"]


def test_a_missing_script_keeps_the_canned_reply_and_warns_by_file_name(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(scripted_replies.SCRIPT_ENV, str(tmp_path / "gone.json"))
    msgs = _turn(monkeypatch, "hello")
    assert _texts(msgs) == [fake.REPLY_TEXT]
    warning = "gone.json: ignored, could not be read (FileNotFoundError)"
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}
    assert capsys.readouterr().err == f"fake ACP backend script: {warning}\n"


def test_a_refused_read_names_only_the_file_and_the_refusal_class(tmp_path, monkeypatch):
    import kiro_crew.hooks as hooks

    def refuse(path: str, **_: Any) -> str:
        raise PermissionError(f"Blocked: access to sensitive path: {'/secret/target'!r}")

    monkeypatch.setattr(hooks, "safe_read_file", refuse)
    script = scripted_replies.load_script(str(tmp_path / "replies.json"))
    assert script.warnings == ["replies.json: ignored, could not be read (PermissionError)"]
    assert (script.matched, script.ordered) == ([], [])


@pytest.mark.parametrize(
    "body, warning",
    [
        ("not json", "replies.json: ignored, not valid JSON"),
        ('{"turns": []}', "replies.json: ignored, expected an object with a 'replies' list"),
    ],
)
def test_a_script_of_the_wrong_shape_is_ignored(tmp_path, monkeypatch, capsys, body, warning):
    _script(tmp_path, monkeypatch, body)
    msgs = _turn(monkeypatch, "hello")
    assert _texts(msgs) == [fake.REPLY_TEXT]
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}
    assert capsys.readouterr().err == f"fake ACP backend script: {warning}\n"


def test_a_file_nested_past_the_recursion_limit_keeps_the_canned_reply(
    tmp_path, monkeypatch, capsys
):
    _script(tmp_path, monkeypatch, "[" * 100_000)
    msgs = _turn(monkeypatch, "hello")
    assert _texts(msgs) == [fake.REPLY_TEXT]
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}
    assert capsys.readouterr().err == (
        "fake ACP backend script: replies.json: ignored, nested too deeply to read\n"
    )


def test_a_file_name_with_controls_reaches_stderr_as_one_plain_line(tmp_path, monkeypatch, capsys):
    hostile = tmp_path / "evil\x1b[2J\nforged line.json"
    monkeypatch.setenv(scripted_replies.SCRIPT_ENV, str(hostile))
    assert _texts(_turn(monkeypatch, "hello")) == [fake.REPLY_TEXT]
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert err.count("\n") == 1
    assert err.startswith("fake ACP backend script: ")
    assert "could not be read (" in err


def test_a_file_name_with_controls_is_one_plain_line_whichever_error_the_platform_raises(
    tmp_path, monkeypatch, capsys
):
    def refuse(path: str) -> str:
        raise OSError(errno.EINVAL, "The filename is incorrect", path)

    monkeypatch.setattr(hooks, "safe_read_file", refuse)
    monkeypatch.setenv(scripted_replies.SCRIPT_ENV, str(tmp_path / "evil\x1b[2J\nforged line.json"))
    assert _texts(_turn(monkeypatch, "hello")) == [fake.REPLY_TEXT]
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert err.count("\n") == 1
    assert err.endswith("could not be read (OSError)\n")
    assert str(tmp_path) not in err


def test_entries_that_break_a_rule_are_dropped_and_the_rest_kept(tmp_path, monkeypatch, capsys):
    replies = [
        "just a string",
        {"error": "boom", "text": "and a reply"},
        {"when": "", "text": "matches everything"},
        {"text": 7},
        {"text": "kept", "stop": "sideways", "colour": "blue", "tools": [{"kind": "x"}]},
    ]
    _script(tmp_path, monkeypatch, {"replies": replies})
    msgs = _turn(monkeypatch, "hello")
    assert _texts(msgs) == ["kept"]
    assert [m["method"] for m in msgs[:-1]] == ["session/update"]
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}
    script = scripted_replies.active_script()
    assert script is not None
    assert script.warnings == [
        "replies.json replies[0]: dropped, an entry must be an object",
        "replies.json replies[1]: dropped, 'error' must be a string and stand alone",
        "replies.json replies[2]: dropped, 'when' must be a non-empty string",
        "replies.json replies[3]: dropped, 'text' must be a string",
        "replies.json replies[4]: ignored unknown key 'colour'",
        "replies.json replies[4].tools[0]: dropped, a tool needs a string 'title'",
        "replies.json replies[4]: 'stop' is not one of "
        "('end_turn', 'max_tokens', 'refusal'), used 'end_turn'",
    ]
    printed = capsys.readouterr().err.splitlines()
    assert printed == [f"fake ACP backend script: {w}" for w in script.warnings]


def test_the_backend_spells_the_script_variable_the_script_module_reads():
    assert fake.SCRIPT_ENV == scripted_replies.SCRIPT_ENV


def _stdio_session(tmp_path: Path, body: Any) -> subprocess.CompletedProcess[str]:
    """Run the real module over stdio: initialize, new session, one prompt."""
    script = tmp_path / "replies.json"
    script.write_text(json.dumps(body), "utf-8")
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}},
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        scripted_replies.SCRIPT_ENV: str(script),
    }
    env.pop(fake.NATIVE_STORE_ENV, None)
    stdin = "".join(json.dumps(f) + "\n" for f in frames)
    stdin += json.dumps(_prompt("hello", req_id=3, session_id="fake-1")) + "\n"
    return subprocess.run(
        [sys.executable, "-m", "kiro_crew.testing.fake_acp_backend", "acp"],
        input=stdin,
        capture_output=True,
        **UTF8_TEXT,
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )


def test_the_backend_process_answers_from_the_script_over_stdio(tmp_path):
    done = _stdio_session(tmp_path, {"replies": [{"text": "scripted over stdio"}]})
    assert done.returncode == 0, done.stderr
    msgs = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert _texts(msgs) == ["scripted over stdio"]
    assert msgs[-1] == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}


@pytest.mark.parametrize(
    "char, count",
    [("x", 11 * 1024 * 1024), ("\N{GRINNING FACE}", 1024 * 1024)],
    ids=["ascii", "escaped-astral"],
)
def test_a_reply_larger_than_the_frame_limit_arrives_whole_in_several_frames(tmp_path, char, count):
    reply = char * count
    done = _stdio_session(tmp_path, {"replies": [{"text": reply}]})
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert max(len(line.encode("utf-8")) for line in lines) < _STDOUT_BUFFER_LIMIT
    msgs = [json.loads(line) for line in lines]
    chunks = _texts(msgs)
    assert len(chunks) > 1
    assert "".join(chunks) == reply
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}


@pytest.mark.parametrize("field", ["tool-input", "tool-title", "error"])
def test_a_tool_call_or_error_larger_than_one_frame_is_dropped_with_a_warning(tmp_path, field):
    big = "x" * (_FRAME_CAP + 1)
    entries = {
        "tool-input": {"text": "after", "tools": [{"title": "t", "input": {"content": big}}]},
        "tool-title": {"text": "after", "tools": [{"title": big}]},
        "error": {"error": big},
    }
    done = _stdio_session(tmp_path, {"replies": [entries[field]]})
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert max(len(line.encode("utf-8")) for line in lines) < _STDOUT_BUFFER_LIMIT
    msgs = [json.loads(line) for line in lines]
    assert msgs[-1]["id"] == 3
    assert "result" in msgs[-1]
    assert msgs[-1]["result"] == {"stopReason": "end_turn"}
    warnings = [line for line in done.stderr.splitlines() if "dropped" in line]
    assert len(warnings) == 1
    assert "its frame would pass" in warnings[0]
    assert big not in done.stderr
    if field == "error":
        assert _texts(msgs) == [fake.REPLY_TEXT]
    else:
        assert _texts(msgs) == ["after"]
        assert not [
            m
            for m in msgs
            if m.get("method") == "session/update"
            and m["params"]["update"]["sessionUpdate"] == "tool_call"
        ]


def test_a_tool_call_just_under_the_frame_cap_is_still_sent(tmp_path):
    tool = {"title": "t", "input": {"content": "x" * (_FRAME_CAP - 100)}}
    done = _stdio_session(tmp_path, {"replies": [{"text": "ok", "tools": [tool]}]})
    assert done.returncode == 0, done.stderr
    msgs = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    calls = [
        m
        for m in msgs
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call"
    ]
    assert len(calls) == 1
    assert "dropped" not in done.stderr


def test_a_tool_call_past_the_transport_limit_never_reaches_the_wire(tmp_path):
    tool = {"title": "t", "input": {"content": "x" * (11 * 1024 * 1024)}}
    done = _stdio_session(tmp_path, {"replies": [{"text": "after", "tools": [tool]}]})
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert max(len(line.encode("utf-8")) for line in lines) < _STDOUT_BUFFER_LIMIT
    assert _texts([json.loads(line) for line in lines]) == ["after"]
