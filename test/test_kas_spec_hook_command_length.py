"""A spec hook whose command is over its form's length limit is named to the user.

Each spec form keeps the limit Crew enforces for it: the object form is bound at
``MAX_SCANNABLE_COMMAND_CHARS``, the shell gate's scan ceiling, and a hook
document at its validator's 4,096-character payload cap. A longer command does
not run, and a new session gets one notice naming the hook (event and matcher),
the length, that limit and the fix. A hook dropped for any other reason keeps
its own message and gets no length notice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.agent_materialization.kiro_hooks import _MAX_HOOK_PAYLOAD_LEN
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.security.paths import MAX_SCANNABLE_COMMAND_CHARS

_OBJECT_LIMIT = MAX_SCANNABLE_COMMAND_CHARS
_DOC_LIMIT = _MAX_HOOK_PAYLOAD_LEN


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **_kw: seen.append(text),
    )
    return seen


def _write_spec(agents_dir: Path, name: str, hooks) -> None:
    spec = {"name": name, "prompt": "p", "hooks": hooks}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _prepare(agent: str, *, is_new: bool = True):
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), provider, agent, is_new=is_new
        )
    )


def _cmd(length: int) -> str:
    return "echo " + "x" * (length - 5)


def _object(length: int, event: str = "preToolUse", **extra) -> dict:
    return {event: [{"command": _cmd(length), **extra}]}


def _document(length: int, trigger: str = "PreToolUse", **extra) -> list:
    return [{"trigger": trigger, "action": {"type": "command", "command": _cmd(length)}, **extra}]


def test_each_form_has_the_limit_crew_enforces():
    assert (_OBJECT_LIMIT, _DOC_LIMIT) == (20 * 1024, 4096)


@pytest.mark.parametrize(
    "hooks",
    [
        _object(6),
        _object(_DOC_LIMIT + 1),
        _object(_OBJECT_LIMIT),
        _document(6),
        _document(_DOC_LIMIT),
    ],
    ids=["object-6", "object-4097", "object-at-limit", "document-6", "document-at-limit"],
)
def test_a_command_within_its_forms_limit_runs_with_no_notice(agents_dir, notices, hooks):
    _write_spec(agents_dir, "a1", hooks)
    out, unreadable, _cwd = _prepare("a1")
    assert len(out) == 1
    assert unreadable is False
    assert notices == []


@pytest.mark.parametrize(
    ("hooks", "event", "length", "limit"),
    [
        (_object(_OBJECT_LIMIT + 1, matcher="execute_bash"), "preToolUse", 20481, "20,480"),
        (_document(_DOC_LIMIT + 1, matcher="execute_bash"), "PreToolUse", 4097, "4,096"),
    ],
    ids=["object-form", "document-form"],
)
def test_one_over_its_forms_limit_is_refused_and_named(
    agents_dir, notices, caplog, hooks, event, length, limit
):
    _write_spec(agents_dir, "a1", hooks)
    with caplog.at_level(logging.WARNING, logger=spec_hooks.logger.name):
        out, unreadable, _cwd = _prepare("a1")
    assert out == []
    assert unreadable is False
    assert len(notices) == 1
    notice = notices[0]
    assert f"{event} (matcher execute_bash): {length:,} characters, limit {limit}" in notice
    assert "script file" in notice
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert str(length) in logged.replace(",", "")
    assert limit.replace(",", "") in logged.replace(",", "")
    assert "script file" in logged


@pytest.mark.parametrize("trigger", ["preToolUse", "pretooluse", "agentSpawn", "stop"])
def test_a_trigger_alias_still_gets_the_notice(agents_dir, notices, trigger):
    _write_spec(agents_dir, "a1", _document(_DOC_LIMIT + 1, trigger))
    hooks, _unreadable, _cwd = _prepare("a1")
    assert hooks == []
    assert len(notices) == 1
    assert f"{trigger}: 4,097 characters, limit 4,096" in notices[0]


def test_an_agent_action_is_not_blamed_on_command_length(agents_dir, notices):
    action = {"type": "agent", "prompt": "p", "command": _cmd(_DOC_LIMIT + 1)}
    _write_spec(agents_dir, "a1", [{"trigger": "Stop", "action": action}])
    _prepare("a1")
    assert notices == []


def test_the_notice_shows_once_per_session(agents_dir, notices):
    _write_spec(agents_dir, "a1", _object(_OBJECT_LIMIT + 1, "stop"))
    _prepare("a1", is_new=True)
    assert len(notices) == 1
    assert "stop: 20,481 characters, limit 20,480" in notices[0]
    assert "matcher" not in notices[0]
    notices.clear()
    _prepare("a1", is_new=False)
    assert notices == []


def test_long_hooks_are_counted_and_a_short_one_still_runs(agents_dir, notices):
    long_ = [{"command": _cmd(_OBJECT_LIMIT + 10 + i)} for i in range(4)]
    _write_spec(agents_dir, "a1", {"stop": [*long_, {"command": "ok.sh"}]})
    hooks, _unreadable, _cwd = _prepare("a1")
    assert [h.command for h in hooks] == ["ok.sh"]
    assert len(notices) == 1
    assert "4 hooks do not run" in notices[0]
    assert "and 1 more" in notices[0]


@pytest.mark.parametrize("command", ["", "   ", None, 42])
def test_other_unusable_commands_keep_their_own_message(agents_dir, notices, caplog, command):
    _write_spec(agents_dir, "a1", {"preToolUse": [{"command": command}]})
    with caplog.at_level(logging.WARNING, logger=spec_hooks.logger.name):
        hooks, _unreadable, _cwd = _prepare("a1")
    assert hooks == []
    assert notices == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "no usable command" in logged
    assert "limit" not in logged
