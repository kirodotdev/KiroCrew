"""``expand_cron_prompt_mention``: what a cron fire does with a leading ``@name``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.cron_service.prompt_mention import expand_cron_prompt_mention
from kiro_crew.dashboard.chat_turn import prompt_assembly


def _resolver(monkeypatch: pytest.MonkeyPatch, status: str, seen: list[Any]) -> None:
    def resolve(message: str, project_dir: Path | None) -> tuple[str, str, str]:
        seen.append((message, project_dir))
        return (f"EXPANDED<{message}>" if status == "ok" else message), status, ""

    monkeypatch.setattr(prompt_assembly, "_resolve_prompt_mention", resolve)


@pytest.mark.asyncio
async def test_a_message_without_a_leading_at_never_reaches_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    _resolver(monkeypatch, "ok", seen)

    out = await expand_cron_prompt_mention("check @agent-sop:x", "/p", job_id="j", job_name="n")

    assert out == "check @agent-sop:x"
    assert seen == []


@pytest.mark.asyncio
async def test_the_resolver_gets_the_message_and_the_runs_project_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    _resolver(monkeypatch, "ok", seen)

    out = await expand_cron_prompt_mention(
        "@agent-sop:x a b", "/work/proj", job_id="j", job_name="n"
    )

    assert out == "EXPANDED<@agent-sop:x a b>"
    assert seen == [("@agent-sop:x a b", Path("/work/proj"))]


@pytest.mark.asyncio
async def test_a_run_with_no_project_directory_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    _resolver(monkeypatch, "ok", seen)

    await expand_cron_prompt_mention("@x", None, job_id="j", job_name="n")

    assert seen == [("@x", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "too_large"])
async def test_a_refused_prompt_runs_as_written_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: str
) -> None:
    _resolver(monkeypatch, status, [])

    with caplog.at_level(logging.WARNING):
        out = await expand_cron_prompt_mention("@x rest", None, job_id="j", job_name="nightly")

    assert out == "@x rest"
    assert "nightly" in caplog.text
    assert status in caplog.text


@pytest.mark.asyncio
async def test_an_unknown_name_is_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _resolver(monkeypatch, "not_found", [])

    with caplog.at_level(logging.WARNING):
        out = await expand_cron_prompt_mention("@here hi", None, job_id="j", job_name="n")

    assert out == "@here hi"
    assert caplog.text == ""
