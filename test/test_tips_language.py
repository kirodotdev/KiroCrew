"""Generated tips follow the configured UI language.

A tip renders beside dashboard chrome in ``dashboard.language``, so the prompt
carries that tag. With no usable tag the prompt carries no language line.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from kiro_crew import tips
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.tips_pool import CatalogEntry

_REPLY = (
    '[{"id": "cron-tip", "feature": "Cron", "title": "Schedule jobs", '
    '"body": "Run a task on a schedule.", "why": "You use cron.", "doc": "cron.md", '
    '"cta_prompt": "Show me cron"}]'
)
_CONTEXT = "Worked on the nightly cron jobs."


def _configure(monkeypatch: pytest.MonkeyPatch, language: object) -> list[int]:
    """Serve a config with ``language``; the returned list records each load's thread."""
    load_threads: list[int] = []
    cfg = SimpleNamespace(dashboard=SimpleNamespace(language=language, tips_model="auto"))

    def _load() -> SimpleNamespace:
        load_threads.append(threading.get_ident())
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_load))
    return load_threads


async def _generated_prompt(monkeypatch: pytest.MonkeyPatch) -> str:
    prompts: list[str] = []

    async def _fake_oneliner(_sessions, prompt, **_kw):
        prompts.append(prompt)
        return _REPLY

    monkeypatch.setattr(tips, "_build_context", lambda _state: _CONTEXT)
    monkeypatch.setattr(tips, "run_bg_oneliner", _fake_oneliner)
    cache = tips.TipsCache(catalog=[CatalogEntry("Cron", "Run tasks on a schedule.", "cron.md")])
    state = SimpleNamespace(sessions=None, _tips_cache=cache)

    await tips.generate_tips(state)

    [prompt] = prompts
    return prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "ja"])
async def test_prompt_carries_the_configured_ui_language(monkeypatch, language) -> None:
    _configure(monkeypatch, language)

    prompt = await _generated_prompt(monkeypatch)

    assert f"BCP-47 tag {language}" in prompt
    assert '"title", "body" and "why"' in prompt
    assert prompt.index("BCP-47") < prompt.index("Feature catalog:")
    assert _CONTEXT in prompt and "Run tasks on a schedule." in prompt
    assert "{language}" not in prompt


@pytest.mark.asyncio
async def test_prompt_keeps_the_identifying_fields_out_of_the_ui_language(monkeypatch) -> None:
    _configure(monkeypatch, "zh-CN")

    prompt = await _generated_prompt(monkeypatch)

    assert '"id" stays a kebab-case ASCII identifier' in prompt
    assert '"feature" and "doc" stay exactly as the catalog gives them' in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["", "ar", "None"])
async def test_prompt_has_no_language_line_without_a_usable_ui_language(
    monkeypatch, language
) -> None:
    # "" is the follow-the-browser sentinel; "ar" has no shipped catalog and "None"
    # is what a hand-edited null arrives as. None may steer a model.
    _configure(monkeypatch, language)

    prompt = await _generated_prompt(monkeypatch)

    assert "BCP-47" not in prompt and "{language}" not in prompt
    assert (
        "- cta_prompt: a ready-to-send chat message demonstrating the feature\n\n"
        "Feature catalog:\n---\n- Cron (cron.md): Run tasks on a schedule.\n---"
    ) in prompt


@pytest.mark.asyncio
async def test_ui_language_is_read_off_the_event_loop(monkeypatch) -> None:
    load_threads = _configure(monkeypatch, "zh-CN")

    await _generated_prompt(monkeypatch)

    assert load_threads and threading.get_ident() not in load_threads
