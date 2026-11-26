"""The session that saved a rule hears what the background contradiction sweep removed.

The sweep deletes after its save has answered, so the save's own ``superseded`` list
cannot name those deletes. The gateway keeps them for that session's next lesson save
or list, and the ``learn_add`` / ``learn_list`` tools print them once.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.dashboard.handlers import cron
from kiro_crew.dashboard.handlers import cron_lesson_supersede as supersede
from kiro_crew.history import ConversationLog
from kiro_crew.lesson_validation import LESSON_APPLIES_ON_TOPIC
from kiro_crew.mcp_tools import learn
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

#: Stored user text leaving the process: the notice must carry it redacted.
_TOKEN = github_classic(seed=42392)
_REMOVED = "Always run the full test suite before a commit"
_CAUSE = "Run only the targeted tests before a commit"
_KEPT = "Run the linter before a commit"


@pytest.mark.asyncio
@pytest.mark.parametrize("next_call", ["learn_add", "learn_add_refused", "learn_list"])
async def test_the_saving_session_is_told_once_what_its_sweep_removed(next_call: str) -> None:
    state = MagicMock()
    state._background_tasks = set()
    state.conversation_log = ConversationLog()
    vs = MagicMock()
    vs.algorithm_version = "v1"
    vs.embed_lesson.return_value = [0.1] * 384
    vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED)
    vs.get_lessons.return_value = []
    vs.count_lessons.return_value = 0
    vs.find_contradiction_candidates.return_value = [
        {"key": key, "rule": rule, "similarity": 0.6, "applies": LESSON_APPLIES_ON_TOPIC}
        for key, rule in (("lesson.old", f"{_REMOVED} {_TOKEN}"), ("lesson.kept", _KEPT))
    ]
    # Both are called contradictory, but the row under ``lesson.kept`` changed while
    # its verdict ran, so the compare-and-delete keeps it.
    vs.delete_semantic.side_effect = lambda key, *_args, **_kwargs: key == "lesson.old"

    async def route(session: str, save: bool, internal: bool = True) -> dict:
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": session}
        request.query = {}
        # The lesson tools post with the internal secret; an app or the Memory tab does not.
        request.get = {"internal_auth": internal}.get
        attach_body(request, {"rule": _CAUSE, "category": "knowledge"})
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vs)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_blocks_reads_session", return_value=False),
            patch.object(
                cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))
            ),
            patch.object(cron, "_get_active_workspace", return_value="default"),
            patch.object(
                cron, "_classify_contradiction", new=AsyncMock(return_value="CONTRADICTORY")
            ),
            patch.object(cron, "_sel"),
        ):
            resp = await (cron.api_lessons_create if save else cron.api_lessons)(request)
            for task in list(state._background_tasks):
                await task
        assert resp.status == 200
        return json.loads(resp.text)

    def render(body: dict) -> str:
        if next_call == "learn_list":
            with patch.object(learn.mcp_core, "_get", return_value=body):
                return learn.learn_list("learn_list", {})
        with (
            patch.object(learn.mcp_core, "_post", return_value=body),
            patch.object(learn.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
        ):
            return learn.learn_add("learn_add", {"rule": _CAUSE, "category": "knowledge"})

    first = await route("dashboard:ui", save=True)
    assert "swept" not in first
    vs.find_contradiction_candidates.return_value = []
    if next_call == "learn_list":
        # The saved rule is listed, so the notice follows a page of rows, as it does live.
        vs.get_lessons.return_value = [
            {
                "key": "lesson.new",
                "value_json": json.dumps({"rule": _CAUSE, "category": "knowledge"}),
            }
        ]
        vs.count_lessons.return_value = 1
    assert "swept" not in await route("dashboard:other", save=False)
    # A reader that does not print the notice, with the same key, leaves it pending.
    assert "swept" not in await route("dashboard:ui", save=False, internal=False)
    if next_call == "learn_add_refused":
        # Every outcome of the next save carries the notice, a refusal included.
        vs.write_lesson.return_value = LessonWriteResult(
            LessonWriteOutcome.REFUSED, "volatile_session_fact"
        )

    save = next_call != "learn_list"
    told = render(await route("dashboard:ui", save=save))
    assert next_call != "learn_list" or f"[knowledge] {_CAUSE}" in told
    assert "REMOVED 1 stored lesson judged to contradict" in told
    assert f"\n  - {_REMOVED} " in told
    assert f"\n    (contradicted by: {_CAUSE})" in told
    assert _TOKEN not in told
    assert _KEPT not in told
    assert "REMOVED" not in render(await route("dashboard:ui", save=save))


def test_a_notice_the_cap_evicts_still_reaches_its_session_as_a_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(supersede, "_CONTRADICTION_NOTICE_CAP", 1)
    state = SimpleNamespace()

    def take(session: str, internal: bool = True) -> dict:
        request = MagicMock()
        request.app = {"state": state}
        request.get = {"internal_auth": internal}.get
        return supersede._take_contradiction_notices(request, session)

    named = {"removed": _REMOVED, "cause": _CAUSE}
    unnamed = {"removed": _KEPT, "cause": _CAUSE}
    supersede._record_contradiction_notice(state, "dashboard:a", unnamed)
    supersede._record_contradiction_notice(state, "dashboard:b", unnamed)
    with caplog.at_level(logging.WARNING, logger=supersede.logger.name):
        # Evicts b's first notice, and the per-session counts pass the same cap, so the
        # oldest count, a's, goes to the operator log instead.
        supersede._record_contradiction_notice(state, "dashboard:b", named)
    assert "Dropped 1 contradiction-sweep notice(s) owed to session dashboard:a" in caplog.text

    assert take("dashboard:b", internal=False) == {}
    taken = take("dashboard:b")
    assert taken == {"swept": [named], "swept_dropped": 1}
    assert take("dashboard:b") == {}
    assert take("dashboard:a") == {}
    told = learn._swept_warning(taken)
    assert "REMOVED 2 stored lessons judged to contradict" in told
    assert f"\n  - {_REMOVED}\n    (contradicted by: {_CAUSE})" in told
    assert "\n  - 1 more, whose details were not kept" in told
    assert _KEPT not in told
