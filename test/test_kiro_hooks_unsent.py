"""A hook Crew accepts but leaves out of the kiro-cli spec, and why.

``hook_documents_to_object_form`` drops a validated hook document for three
documented reasons: a trigger kiro-cli has no event for, an ``agent`` action, and
``enabled: false`` / ``confirm: true``. The Hooks page shows those documents beside
the kiro-cli hooks, so the reason it shows must be the one the emission acted on.
These tests pin that: one decision, read by both, and a read that adds nothing to
the audit trail on every page load.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent
from kiro_crew.agent_materialization import kiro_hooks
from kiro_crew.agent_materialization.kiro_hooks import (
    UNSENT_AGENT_ACTION,
    UNSENT_CONFIRM,
    UNSENT_DISABLED,
    UNSENT_TRIGGER,
    hook_document_unsent_reason,
    hook_documents_to_object_form,
    normalize_spec_hooks,
    unsent_hook_documents,
)
from kiro_crew.dashboard.handlers.hooks import api_kiro_hooks

_CMD = "/usr/local/bin/guard.sh"

#: The seven kiro-agent triggers kiro-cli names no event for, as the issue lists them.
_NO_EVENT_TRIGGERS = (
    "SessionEnd",
    "PreTaskExec",
    "PostTaskExec",
    "PostFileCreate",
    "PostFileSave",
    "PostFileDelete",
    "Manual",
)
_EVENT_TRIGGERS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop")


def _doc(trigger: str = "PreToolUse", **extra: object) -> dict:
    action = extra.pop("action", {"type": "command", "command": _CMD})
    return {"name": f"h-{trigger}", "trigger": trigger, "action": action, **extra}


class TestUnsentReason:
    @pytest.mark.parametrize("trigger", _NO_EVENT_TRIGGERS)
    def test_a_trigger_with_no_kiro_cli_event(self, trigger: str) -> None:
        assert hook_document_unsent_reason(_doc(trigger)) == UNSENT_TRIGGER

    @pytest.mark.parametrize("trigger", _EVENT_TRIGGERS)
    def test_an_agent_action(self, trigger: str) -> None:
        doc = _doc(trigger, action={"type": "agent", "prompt": "summarize"})
        assert hook_document_unsent_reason(doc) == UNSENT_AGENT_ACTION

    def test_disabled_and_confirm(self) -> None:
        assert hook_document_unsent_reason(_doc(enabled=False)) == UNSENT_DISABLED
        assert hook_document_unsent_reason(_doc(confirm=True)) == UNSENT_CONFIRM

    @pytest.mark.parametrize("trigger", _EVENT_TRIGGERS)
    def test_an_expressible_hook_has_no_reason(self, trigger: str) -> None:
        assert hook_document_unsent_reason(_doc(trigger)) is None
        assert hook_document_unsent_reason(_doc(trigger, enabled=True, confirm=False)) is None

    def test_precedence_matches_the_emission(self) -> None:
        # Off outranks confirm, both outrank expressibility, and a trigger with no
        # event outranks an agent action on the same document.
        agent_action = {"type": "agent", "prompt": "p"}
        both = _doc("Manual", action=agent_action, enabled=False, confirm=True)
        assert hook_document_unsent_reason(both) == UNSENT_DISABLED
        assert hook_document_unsent_reason(_doc("Manual", confirm=True)) == UNSENT_CONFIRM
        assert hook_document_unsent_reason(_doc("Manual", action=agent_action)) == UNSENT_TRIGGER


def _every_shape() -> list[dict]:
    docs: list[dict] = []
    for trigger in _EVENT_TRIGGERS + _NO_EVENT_TRIGGERS:
        for action in (
            {"type": "command", "command": _CMD},
            {"type": "agent", "prompt": "check"},
        ):
            for flags in ({}, {"enabled": False}, {"confirm": True}, {"enabled": True}):
                docs.append(_doc(trigger, action=dict(action), **flags))
    return docs


def test_the_reason_is_none_exactly_for_what_reaches_kiro_cli() -> None:
    """The emission and the reason are one decision, across every shape."""
    for doc in normalize_spec_hooks(_every_shape()):
        emitted = hook_documents_to_object_form([doc])
        reason = hook_document_unsent_reason(doc)
        assert (reason is None) == bool(emitted), (doc, reason, emitted)


class TestUnsentHookDocuments:
    def test_lists_each_reason_with_the_document(self) -> None:
        hooks = [
            _doc("PreToolUse", matcher="execute_bash"),
            _doc("PostFileSave"),
            _doc("Stop", action={"type": "agent", "prompt": "wrap up"}),
            _doc("PreToolUse", enabled=False),
            _doc("UserPromptSubmit", confirm=True),
        ]
        unsent = unsent_hook_documents(hooks)
        assert [(d["trigger"], d["reason"]) for d in unsent] == [
            ("PostFileSave", UNSENT_TRIGGER),
            ("Stop", UNSENT_AGENT_ACTION),
            ("PreToolUse", UNSENT_DISABLED),
            ("UserPromptSubmit", UNSENT_CONFIRM),
        ]
        assert unsent[1]["action"] == {"type": "agent", "prompt": "wrap up"}

    def test_object_form_and_non_lists_have_none(self) -> None:
        assert unsent_hook_documents({"preToolUse": [{"command": _CMD}]}) == []
        assert unsent_hook_documents(None) == []
        assert unsent_hook_documents("hooks") == []

    def test_an_invalid_document_is_not_listed(self) -> None:
        # Not a hook Crew accepted: a bad trigger never becomes a document.
        assert unsent_hook_documents([_doc("NoSuchTrigger"), "not-a-doc"]) == []

    def test_the_read_writes_no_warning_and_no_audit(self, caplog) -> None:
        hooks = [_doc("NoSuchTrigger"), _doc("Manual"), {"trigger": 3}]
        with patch.object(agent, "_sel_hook_rejected") as sel:
            with caplog.at_level("WARNING"):
                unsent_hook_documents(hooks)
        assert sel.call_count == 0
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_the_mute_ends_with_the_read(self) -> None:
        unsent_hook_documents([_doc()])
        with patch.object(agent, "_sel_hook_rejected") as sel:
            normalize_spec_hooks([_doc("NoSuchTrigger")])
        assert sel.call_count == 1

    def test_the_mute_ends_even_when_validation_raises(self) -> None:
        with patch.object(kiro_hooks, "normalize_spec_hooks", side_effect=RuntimeError):
            with pytest.raises(RuntimeError):
                unsent_hook_documents([_doc()])
        assert kiro_hooks._document_audit_muted.get() is False


# --------------------------------------------------------------------------- #
# GET /api/kiro-hooks
# --------------------------------------------------------------------------- #
_P_AGENTS_DIR = "kiro_crew.agent.KIRO_AGENTS_DIR"
_P_DEFAULTS = "kiro_crew.dashboard.handlers.hooks._shipped_defaults"
_P_CONFIG = "kiro_crew.agent._mc_config_path"
_P_REDACT = "kiro_crew.security.redact"


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/kiro-hooks", api_kiro_hooks)
    return app


async def _get(tmp_path: Path, crew_config: object, **patches) -> dict:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "kirocrew.json").write_text(json.dumps({"hooks": {}}))
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"hooks": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(crew_config))
    with (
        patch(_P_AGENTS_DIR, agents),
        patch(_P_DEFAULTS, return_value=defaults),
        patch(_P_CONFIG, return_value=config),
    ):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/kiro-hooks")
            assert resp.status == 200
            return await resp.json()


@pytest.mark.asyncio
async def test_the_endpoint_lists_unsent_documents_with_their_reason(tmp_path: Path) -> None:
    body = await _get(
        tmp_path,
        {
            "agent": {
                "kiro_hooks": [
                    _doc("PreToolUse"),
                    _doc("PostFileSave", name="on-save"),
                    _doc("Stop", action={"type": "agent", "prompt": "summarize"}),
                    _doc("PreToolUse", enabled=False, matcher="fs_write"),
                ]
            }
        },
    )
    assert body["unsent"] == [
        {
            "name": "on-save",
            "trigger": "PostFileSave",
            "matcher": "",
            "action_type": "command",
            "command": _CMD,
            "reason": UNSENT_TRIGGER,
        },
        {
            "name": "h-Stop",
            "trigger": "Stop",
            "matcher": "",
            "action_type": "agent",
            "command": "summarize",
            "reason": UNSENT_AGENT_ACTION,
        },
        {
            "name": "h-PreToolUse",
            "trigger": "PreToolUse",
            "matcher": "fs_write",
            "action_type": "command",
            "command": _CMD,
            "reason": UNSENT_DISABLED,
        },
    ]


def test_the_trigger_reason_names_exactly_the_triggers_kiro_cli_runs() -> None:
    """The page tells the reader where a hook CAN run; that list is the emission's own."""
    catalog = Path(__file__).resolve().parents[1] / "website/src/i18n/locales/en.manual.json"
    copy = json.loads(catalog.read_text(encoding="utf-8"))["pages"]["hooksPage"]
    listed = copy["unsent_reason_trigger"].rsplit("run on ", 1)[1].rstrip(".")
    assert listed.split(", ") == list(kiro_hooks._KAS_TRIGGER_TO_EVENT)


@pytest.mark.asyncio
async def test_the_endpoint_redacts_what_the_author_wrote(tmp_path: Path) -> None:
    with patch(_P_REDACT, side_effect=lambda t: f"[R:{t}]"):
        body = await _get(
            tmp_path,
            {"agent": {"kiro_hooks": [_doc("Manual", name="n", matcher="m")]}},
        )
    (entry,) = body["unsent"]
    assert entry["command"] == f"[R:{_CMD}]"
    assert entry["name"] == "[R:n]"
    assert entry["matcher"] == "[R:m]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crew_config",
    [
        {},
        {"agent": "not-an-object"},
        {"agent": {"kiro_hooks": {"preToolUse": [{"command": _CMD}]}}},
        {"agent": {"kiro_hooks": "nope"}},
        [],
    ],
)
async def test_the_endpoint_has_none_for_other_shapes(tmp_path: Path, crew_config) -> None:
    assert (await _get(tmp_path, crew_config))["unsent"] == []
