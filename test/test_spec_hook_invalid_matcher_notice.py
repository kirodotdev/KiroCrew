"""A spec hook dropped because Crew cannot read its matcher is named in a session notice.

Crew's spec-hook conversion drops a tool-event hook whose matcher is outside the
kinds it reads (an anchored regular expression, say), since running it with no
matcher would widen it to every tool. The drop wrote a gateway warning and an SEL
row only, so the guard was skipped unseen. A new session gets one notice row
naming the event, the matcher and the kinds Crew reads, in either spec shape.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import SPEC_HOOKS_NOTICE_KIND

_REGEX = "^(read_file|disclose_context)$"


def _scan(spec: dict) -> list[tuple[str, str]]:
    # Imported per test, so a tree without the module fails these tests rather
    # than stopping the whole file at collection.
    from kiro_crew.agent_sdk.spec_hooks_matchers import _invalid_matchers

    return _invalid_matchers(spec)


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


def _object_spec() -> dict:
    return {
        "hooks": {
            "preToolUse": [
                {"matcher": "fs_read", "command": "a.sh"},
                {"matcher": _REGEX, "command": "b.sh"},
                {"matcher": "disclose_context", "command": "c.sh"},
                # Dropped for the command first, so not blamed on the matcher.
                {"matcher": "^x$", "command": " "},
                {"matcher": "^y$", "command": "x" * (spec_hooks._MAX_COMMAND_LEN + 1)},
            ],
            "postToolUse": [{"matcher": ["fs_read"], "command": "d.sh"}],
            # A non-tool event never reads its matcher.
            "agentSpawn": [{"matcher": "^z$", "command": "e.sh"}],
        }
    }


def _document_spec() -> dict:
    action = {"type": "command", "command": "x.sh"}
    return {
        "hooks": [
            {"trigger": "preToolUse", "matcher": _REGEX, "action": action},
            {"trigger": "preToolUse", "matcher": "fs_read", "action": action},
            {"trigger": "preToolUse", "matcher": None, "action": action},
            # Dropped for another field first, so not blamed on the matcher.
            {"trigger": "preToolUse", "matcher": "^a$", "timeout": -1, "action": action},
            {"trigger": "bogus", "matcher": "^b$", "action": action},
        ]
    }


def test_the_object_form_names_only_the_hooks_dropped_for_their_matcher() -> None:
    assert _scan(_object_spec()) == [
        ("preToolUse", _REGEX),
        ("postToolUse", '["fs_read"]'),
    ]


def test_the_document_form_names_only_the_hooks_dropped_for_their_matcher() -> None:
    assert _scan(_document_spec()) == [("preToolUse", _REGEX)]


@pytest.mark.parametrize("spec", [_object_spec(), _document_spec()], ids=["object", "documents"])
def test_the_named_hooks_are_the_ones_the_conversion_drops(spec: dict) -> None:
    """Every hook the scan names is absent from what runs, and none that runs is named."""
    kept = {hook.matcher for hook in spec_hooks.spec_script_hooks("agent-x", spec)}
    for hook in _scan(spec):
        assert hook.matcher not in kept


def test_a_disabled_or_confirm_gated_document_is_not_blamed_on_its_matcher() -> None:
    action = {"type": "command", "command": "x.sh"}
    spec = {
        "hooks": [
            {"trigger": "preToolUse", "matcher": "^a$", "enabled": False, "action": action},
            {"trigger": "preToolUse", "matcher": "^b$", "confirm": True, "action": action},
        ]
    }
    assert _scan(spec) == []


def test_an_object_form_hook_past_the_cap_is_not_blamed_on_its_matcher() -> None:
    """The conversion reads no entry once the cap of hooks that run is reached."""
    cap = spec_hooks._MAX_SPEC_HOOKS
    running = [{"command": f"s{i}.sh"} for i in range(cap - 1)]
    spec = {
        "hooks": {
            # Bad matchers before the cap do not count toward it.
            "agentSpawn": running,
            "preToolUse": [
                {"matcher": "^before$", "command": "a.sh"},
                {"matcher": "fs_read", "command": "b.sh"},
                {"matcher": "^after$", "command": "c.sh"},
            ],
        }
    }
    assert _scan(spec) == [("preToolUse", "^before$")]
    kept = spec_hooks.spec_script_hooks("agent-x", spec)
    assert len(kept) == cap


def test_a_hook_document_past_the_cap_is_not_blamed_on_its_matcher() -> None:
    cap = spec_hooks._MAX_SPEC_HOOKS
    action = {"type": "command", "command": "x.sh"}
    docs = [{"trigger": "preToolUse", "matcher": "^before$", "action": action}]
    docs += [{"trigger": "preToolUse", "matcher": "fs_read", "action": action}] * cap
    docs += [{"trigger": "preToolUse", "matcher": "^after$", "action": action}]
    assert _scan({"hooks": docs}) == [("preToolUse", "^before$")]


def test_the_scan_writes_no_warning_or_audit_row(monkeypatch, caplog) -> None:
    """The load already warned and audited; the notice's read must not do it again."""
    from kiro_crew import agent

    audited: list[tuple] = []
    monkeypatch.setattr(agent, "_sel_hook_rejected", lambda *a: audited.append(a))
    with caplog.at_level(logging.DEBUG):
        _scan(_object_spec())
        _scan(_document_spec())
    assert audited == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_spec_without_a_bad_matcher_names_nothing() -> None:
    assert _scan({}) == []
    assert _scan({"hooks": {"preToolUse": [{"matcher": "@mcp", "command": "a"}]}}) == []
    assert _scan({"hooks": "not hooks"}) == []


def _rows(monkeypatch, spec: dict | Exception, slot=None) -> list[dict]:
    def _spec(*_a, **_kw):
        if isinstance(spec, Exception):
            raise spec
        return spec

    monkeypatch.setattr(spec_hooks, "_agent_spec", _spec)
    rows: list[dict] = []

    def _capture(_state, _slot, role, content, cls="", *, meta=None, broadcast_user=False):
        rows.append({"role": role, "content": content, "meta": meta})
        return {}

    monkeypatch.setattr(chat_runner, "append_and_surface", _capture)
    slot = slot if slot is not None else SimpleNamespace(_session_notice_marks=[])
    asyncio.run(
        chat_runner._surface_invalid_matcher_hooks(SimpleNamespace(), slot, "agent-x", None, "")
    )
    return rows


def test_a_new_session_gets_one_notice_naming_event_matcher_and_kinds(monkeypatch) -> None:
    (row,) = _rows(monkeypatch, _object_spec())
    assert row["role"] == "notice"
    assert row["meta"] == {"kind": SPEC_HOOKS_NOTICE_KIND}
    text = row["content"]
    assert text.startswith("ℹ Agent agent-x: 2 hooks do not run because Crew cannot read")
    assert f"preToolUse matcher {_REGEX}" in text
    assert 'postToolUse matcher ["fs_read"]' in text
    assert "@builtin, @<server> or @<server>/<tool glob>" in text
    assert "a regular expression is not read" in text


def test_the_notice_shows_once_per_chat(monkeypatch) -> None:
    slot = SimpleNamespace(_session_notice_marks=[])
    assert len(_rows(monkeypatch, _document_spec(), slot)) == 1
    assert _rows(monkeypatch, _document_spec(), slot) == []


def test_the_notice_redacts_a_credential_in_the_matcher(monkeypatch) -> None:
    secret = github_classic(seed=7)
    spec = {"hooks": {"preToolUse": [{"matcher": f"^({secret})$", "command": "a.sh"}]}}
    (row,) = _rows(monkeypatch, spec)
    assert "1 hook does not run" in row["content"]
    assert "no tool call reaches it." in row["content"]
    assert secret not in row["content"]
    assert "[REDACTED: credential]" in row["content"]


def test_the_notice_counts_hooks_past_the_shown_few(monkeypatch) -> None:
    entries = [{"matcher": f"^t{i}$", "command": "a.sh"} for i in range(5)]
    (row,) = _rows(monkeypatch, {"hooks": {"preToolUse": entries}})
    assert "5 hooks do not run" in row["content"]
    assert "; and 2 more)" in row["content"]


def test_no_bad_matcher_or_an_unreadable_spec_renders_no_row(monkeypatch) -> None:
    assert (
        _rows(monkeypatch, {"hooks": {"preToolUse": [{"matcher": "fs_read", "command": "a"}]}})
        == []
    )
    assert _rows(monkeypatch, OSError("unreadable")) == []


def test_the_reader_reads_the_session_spec(monkeypatch) -> None:
    seen: list[tuple] = []

    def _spec(agent_id, project_dir, session_key, *, kas_checkout=None):
        seen.append((agent_id, project_dir, session_key, kas_checkout))
        return _document_spec()

    monkeypatch.setattr(spec_hooks, "_agent_spec", _spec)
    from kiro_crew.agent_sdk.spec_hooks_matchers import invalid_matcher_spec_hooks

    found = invalid_matcher_spec_hooks("a", "/p", session_key="k", kas_checkout="/c")
    assert found == [("preToolUse", _REGEX)]
    assert seen == [("a", "/p", "k", "/c")]


@pytest.mark.parametrize("backend", ["kas", "codex"])
def test_a_new_session_start_surfaces_the_notice(monkeypatch, backend: str) -> None:
    """Both session-start paths, Crew-fired hooks (KAS) and observer hooks (codex)."""
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    monkeypatch.setattr(
        spec_hooks, "_agent_spec_with_origin", lambda *_a, **_k: (_object_spec(), "user")
    )
    rows: list[dict] = []

    def _capture(_state, _slot, role, content, cls="", *, meta=None, broadcast_user=False):
        rows.append({"role": role, "content": content, "meta": meta})
        return {}

    async def _nothing(*_a, **_kw):
        return None

    monkeypatch.setattr(chat_runner, "append_and_surface", _capture)
    # Only this row is under test; the other session-start notices read the
    # filesystem and are covered by their own tests.
    monkeypatch.setattr(chat_runner, "_notice_unread_steering", _nothing)
    monkeypatch.setattr(chat_runner, "_notice_spec_hooks_run_nowhere", _nothing)
    monkeypatch.setattr(chat_runner, "_notice_tools_settings_applied_nowhere", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_overlong_hook_commands", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hooks", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hook_files", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_kas_project_hooks", _nothing)
    client = SimpleNamespace(
        capabilities=capabilities_for(backend),
        cwd="",
        client=SimpleNamespace(initialize_result={"agentCapabilities": {}}),
    )
    slot = SimpleNamespace(_session_notice_marks=[])
    asyncio.run(
        chat_runner._prepare_spec_hooks(SimpleNamespace(), slot, client, "agent-x", is_new=True)
    )
    matched = [r for r in rows if "cannot read the matcher" in r["content"]]
    assert len(matched) == 1, rows
    assert _REGEX in matched[0]["content"]
