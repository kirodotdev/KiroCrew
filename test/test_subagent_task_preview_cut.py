"""A sub-agent task preview never cuts a link, so it never names another target.

The completion notice quotes the task cut to 100 characters. A plain
slice landed inside ``https://github.com/kirodotdev/KiroCrew/pull/43162`` and
left ``.../pull/4316``: a different, valid-looking link. The sidebar chip then
read that number (an issue, not a pull request) from GitHub every minute.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.source_providers.links import iter_source_url_candidates, parse_source_url
from kiro_crew.subagent import SubagentInfo

_PR = "https://github.com/kirodotdev/KiroCrew/pull/43162"
# The task the incident's preview came from: the 100th character falls inside
# the pull request number.
_TASK = f"You are a REPAIR subagent for one review finding on {_PR}. Fix it and push."
# Short enough to quote whole in the completion, but a wave digest line quotes 80.
_DIGEST_TASK = "go " * 10 + "x " + _PR
_PARENT = "dashboard:chat-7"
#: Names a hang; never decides whether a test passes.
_HANG_CEILING_SECS = 30.0


@pytest.fixture
def _home(tmp_path, monkeypatch, close_subagent_managers):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))


def _manager(**kwargs):  # type: ignore[no-untyped-def]
    from kiro_crew.subagent import SubagentManager

    return SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), on_done=AsyncMock(), **kwargs
    )


def _pull_numbers(text: str) -> set[int]:
    """The pull request numbers the sidebar chip scanner reads in *text*."""
    return {parse_source_url(url).number for url in iter_source_url_candidates(text, ("/pull/",))}


def test_the_incident_task_is_cut_inside_its_link() -> None:
    assert _TASK[:100].endswith("/pull/4316")


def test_the_completion_notice_names_no_other_pull_request() -> None:
    from kiro_crew.slack.gateway import _compose_subagent_completion

    report = _compose_subagent_completion(
        SubagentInfo(id="c0ffee", task=_TASK, result="done"), _flush_only=False
    )
    assert _pull_numbers(report.announce) <= {43162}
    assert _pull_numbers(report.sub_meta["task"]) <= {43162}
    assert report.task_text.endswith(" …")


def test_a_link_the_cut_keeps_whole_still_names_its_pull_request() -> None:
    from kiro_crew.preview_text import clip_preview

    task = f"Fix the one finding the GPT lane raised on {_PR} immediately after the rebase."
    assert task[:100].endswith("/pull/43162 immedia")
    assert _pull_numbers(clip_preview(task, 100)) == {43162}
    assert _pull_numbers(clip_preview(task, task.index(" immediately"))) == {43162}


def test_a_wave_digest_line_names_no_other_pull_request() -> None:
    from kiro_crew.slack.gateway import GatewayOrchestrator, _compose_subagent_completion

    assert len(_DIGEST_TASK) <= 100 and _DIGEST_TASK[:80].endswith("/pull/4316")
    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw._batch_progress = {}
    gw.subagent_mgr = None
    info = SubagentInfo(id="c0ffee", task=_DIGEST_TASK, result="done", batch_id="w1", batch_total=2)
    report = _compose_subagent_completion(info, _flush_only=False)
    gw._subagent_wave_digest(
        info,
        report=report,
        _flush_only=False,
        _batch_id="w1",
        _batch_total=2,
        _no_batch_pending=False,
        _injection_slot_name="",
    )
    (line,) = gw._batch_progress["w1"]["ok_lines"]
    assert _pull_numbers(line) <= {43162}


@pytest.mark.asyncio
async def test_the_injection_failure_notice_names_no_other_pull_request(_home) -> None:
    queued: list[str] = []

    async def _on_event(etype, _info, data=None):  # type: ignore[no-untyped-def]
        if etype == "subagent_injection_failed":
            queued.append((data or {})["failure_msg"])

    manager = _manager(on_event=_on_event)
    info = SubagentInfo(id="c0ffee", task=_TASK, parent_session_key=_PARENT)
    manager.notify_injection_failed(info, reason="injection timed out")
    await asyncio.wait_for(asyncio.gather(*list(manager._report_tasks)), timeout=_HANG_CEILING_SECS)
    (notice,) = queued
    assert _pull_numbers(notice) <= {43162}


@pytest.mark.asyncio
async def test_the_restart_orphan_notice_names_no_other_pull_request(_home, monkeypatch) -> None:
    from kiro_crew import subagent_persistence as sp

    sp.create_agent_folder("orpP001", task=_TASK, parent_session=_PARENT)
    manager = _manager()
    sent: list[tuple[str, dict]] = []

    async def _inject(_parent, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
        sent.append((msg, meta or {}))
        return True

    monkeypatch.setattr(manager, "_try_inject_orphan_notification", _inject)
    state = {"id": "orpP001", "task": _TASK, "parent_session": _PARENT, "result_complete": True}
    await manager._notify_orphan("orpP001", state, True)
    ((msg, meta),) = sent
    assert _pull_numbers(msg) <= {43162}
    assert _pull_numbers(str(meta)) <= {43162}


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short enough", 100, "short enough"),
        ("exactly ten", 11, "exactly ten"),
        ("cut between words", 9, "cut …"),
        ("cut at a space here", 7, "cut at …"),
        (f"see {_PR} now", 30, "see …"),
        (f"see\n{_PR}", 30, "see …"),
        (_PR + " tail", 20, "…"),
        ("www.example.com/pull/43162 tail", 20, "…"),
        ("/a/long/path/without/any/space/in/it", 10, "/a/long/pa …"),
        (f"see {_PR} now", 4 + len(_PR), f"see {_PR} …"),
    ],
)
def test_clip_preview_cuts_only_between_words(text: str, limit: int, expected: str) -> None:
    from kiro_crew.preview_text import clip_preview

    assert clip_preview(text, limit) == expected
