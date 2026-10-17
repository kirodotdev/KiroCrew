"""A local slash command's reply carries the redaction records a model reply carries.

The dashboard answers some commands itself (``/workflow``, ``/goal``, ``/agent``,
``/prompts``, a harness command the backend cannot run) and writes the answer to
the same assistant row a model reply goes to. That text can hold a credential or
a suspicious link: an error string, a prompt description, or the user's own
argument echoed back. Each such row must be redacted AND carry the records
(``meta.redactions`` / ``meta.blocked_links``) the dashboard renders the lock tag,
the blocked-link chip and the one-time coach from -- a placeholder with no record
is a value removed without telling the reader. Each test fails when its site
stops going through ``_append_command_reply``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from prompts_helpers import _isolate_home  # noqa: F401
from prompts_helpers import _ss

import kiro_crew.dashboard.chat_runner as chat_runner
from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

# Built at runtime so no literal token shape sits in the source.
_GH = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"
_LINK = "https://collect.example.com/x?token=" + "A" * 250
_TAG = "[REDACTED: credential]"


class _Slot:
    """The parts of ``_ChatSlot`` a local command's reply touches."""

    key = "slot-1"
    agent = "kirocrew"
    project = ""
    _app = ""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.credential_evidence = None

    def append(self, role, content, cls="", ts="", *, meta=None, **_kw):
        row = {"role": role, "content": content, "cls": cls, "meta": meta}
        self.rows.append(row)
        return row


class _State:
    def __init__(self) -> None:
        self.workflow_service = None

    def push_slots_update(self) -> None:
        return None

    def broadcast_ws(self, *_a, **_kw) -> None:
        return None


def _reply(slot: _Slot) -> dict:
    rows = [r for r in slot.rows if r["role"] == "assistant"]
    assert len(rows) == 1, rows
    return rows[0]


def _assert_credential_explained(row: dict) -> None:
    assert _GH not in row["content"]
    assert _TAG in row["content"]
    meta = row["meta"] or {}
    assert meta.get("redactions"), "credential placeholder persisted with no record"
    assert meta["redactions"][0]["rule"] == "github_token"


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    audit = MagicMock()
    _floor_monkeypatch.setattr(chat_runner, "sel", lambda: audit)
    return audit


def test_workflow_error_records_its_credential_and_link() -> None:
    class _Service:
        async def start_definition(self, ref, **_kw):
            return {"error": f"bad token {_GH} see {_LINK}"}

    state, slot = _State(), _Slot()
    state.workflow_service = _Service()

    asyncio.run(
        chat_runner._handle_workflow_command(state, slot, "/workflow wf go", "dashboard:main")
    )

    row = _reply(slot)
    _assert_credential_explained(row)
    assert EXFILTRATION_REDACTION_TAG_PREFIX in row["content"]
    assert [r["domain"] for r in row["meta"]["blocked_links"]] == ["collect.example.com"]


def test_goal_objective_echo_records_its_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = MagicMock()

    async def _add(*_a, **_kw):
        return SimpleNamespace(id="loop-1")

    svc.add = _add
    monkeypatch.setattr(chat_runner, "get_instance", lambda: svc)
    monkeypatch.setattr(chat_runner, "data_home", lambda: chat_runner.Path("/nonexistent-home"))
    monkeypatch.setattr(chat_runner.Path, "unlink", lambda self, missing_ok=False: None)
    slot = _Slot()

    asyncio.run(chat_runner._handle_goal_command(_State(), slot, f"/goal ship it with {_GH}"))

    _assert_credential_explained(_reply(slot))


def test_agent_refusal_records_its_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard import chat_handlers

    async def _switch(*_a, **_kw):
        return SimpleNamespace(status=409)

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", _switch)
    monkeypatch.setattr(
        chat_handlers, "agent_switch_outcome", lambda _r: (False, {"error": f"denied {_GH}"})
    )
    slot = _Slot()

    asyncio.run(chat_runner._handle_agent_command(_State(), slot, "fable", "dashboard:main"))

    _assert_credential_explained(_reply(slot))


def test_answer_without_channel_records_an_echoed_credential() -> None:
    slot = _Slot()

    asyncio.run(
        chat_runner._answer_slash_without_channel(_State(), slot, "dashboard:main", f"/x{_GH}")
    )

    _assert_credential_explained(_reply(slot))


def _run_prompts(monkeypatch: pytest.MonkeyPatch, message: str) -> dict:
    """Drive ``/prompts`` through ``_run_chat``, which answers it before any session work."""
    # The turn's tail needs far more of a slot than the prompt doubles model,
    # and is not what is under test here.

    async def _skip(*_a, **_kw):
        return None

    monkeypatch.setattr(chat_runner, "_end_turn_tail", _skip)
    state, slot = _ss()
    asyncio.run(chat_runner._run_chat(state, slot, message))
    rows = [
        {"role": r, "content": c, "meta": m}
        for (r, c, _cls), m in zip(slot.messages, slot.metas, strict=True)
    ]
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert len(assistant) == 1, rows
    return assistant[0]


def test_prompts_list_records_a_credential_in_a_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = [{"source": "local", "fullName": "deploy", "description": f"uses {_GH}"}]
    monkeypatch.setattr(chat_runner, "_list_aim_prompts", lambda _p: listing)
    row = _run_prompts(monkeypatch, "/prompts")

    _assert_credential_explained(row)


@pytest.mark.parametrize("status", ["blocked", "too_large", "not_found"])
def test_prompts_get_failure_redacts_and_records_the_echoed_name(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    async def _expand(*_a, **_kw):
        return "", status

    monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _expand)
    row = _run_prompts(monkeypatch, f"/prompts get deploy {_GH}")

    _assert_credential_explained(row)


def test_a_clean_command_reply_carries_no_meta() -> None:
    slot = _Slot()

    chat_runner._append_command_reply(slot, "No saved workflows yet.")

    assert _reply(slot)["meta"] is None
