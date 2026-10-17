"""Home-screen suggestions follow the configured UI language.

The cards sit under a heading rendered in ``dashboard.language``, so the prompt
must carry that tag, and a cached set made for another language is not served.
When no set is generated the server sends an empty list, which the welcome
screen answers with its own catalog-localized cards rather than fixed English.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import suggestions
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.testing.wait import async_wait_until

_CONTEXT = "## Recent Activity\n" + "Worked on the auth module refactor. " * 3


def _configure(monkeypatch: pytest.MonkeyPatch, language: object) -> None:
    cfg = SimpleNamespace(dashboard=SimpleNamespace(language=language))
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))


def _capture_prompts(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[str]:
    prompts: list[str] = []

    async def _fake_oneliner(_sessions, prompt, **_kw):
        prompts.append(prompt)
        return reply

    monkeypatch.setattr(suggestions, "_build_context", lambda _state: _CONTEXT)
    monkeypatch.setattr(suggestions, "run_bg_oneliner", _fake_oneliner)
    return prompts


def _state() -> SimpleNamespace:
    return SimpleNamespace(sessions=None, _background_tasks=set())


@pytest.mark.asyncio
async def test_prompt_carries_the_configured_ui_language(monkeypatch) -> None:
    _configure(monkeypatch, "zh-CN")
    prompts = _capture_prompts(monkeypatch, json.dumps([{"text": "继续重构", "kind": "code"}]))

    await suggestions.refresh_suggestions(_state(), suggestions.SuggestionsCache())

    [prompt] = prompts
    assert "BCP-47 tag zh-CN" in prompt
    assert "recent messages" not in prompt, "a configured tag must not fall back to guessing"
    assert _CONTEXT in prompt
    assert "{language}" not in prompt and "{context}" not in prompt


@pytest.mark.asyncio
async def test_prompt_infers_the_language_only_when_no_tag_is_set(monkeypatch) -> None:
    _configure(monkeypatch, "")  # the follow-the-browser sentinel
    prompts = _capture_prompts(monkeypatch, json.dumps([{"text": "Fix it", "kind": "code"}]))

    await suggestions.refresh_suggestions(_state(), suggestions.SuggestionsCache())

    [prompt] = prompts
    assert "BCP-47" not in prompt
    assert "language of the user's recent messages" in prompt
    assert "{language}" not in prompt


@pytest.mark.asyncio
async def test_prompt_keeps_kind_tags_in_english(monkeypatch) -> None:
    _configure(monkeypatch, "ja")
    prompts = _capture_prompts(monkeypatch, "[]")

    await suggestions.refresh_suggestions(_state(), suggestions.SuggestionsCache())

    [prompt] = prompts
    assert 'The "kind" value always stays one of the fixed English tags' in prompt
    for kind in suggestions.SUGGESTION_KINDS:
        assert kind in prompt


async def _get(state: SimpleNamespace) -> dict:
    request = make_mocked_request("GET", "/api/suggestions", app={"state": state})
    return json.loads((await suggestions.api_suggestions(request)).text)


def _cached(state: SimpleNamespace, language: str) -> suggestions.SuggestionsCache:
    cache = suggestions.get_suggestions_cache(state)
    cache.suggestions = [{"text": "Keep going", "kind": "code"}]
    cache.generated_at = suggestions.time.time()  # fresh, well inside the interval
    cache.language = language
    return cache


@pytest.mark.asyncio
async def test_cache_is_keyed_on_the_ui_language(monkeypatch) -> None:
    prompts = _capture_prompts(monkeypatch, json.dumps([{"text": "続ける", "kind": "code"}]))
    state = _state()
    cache = _cached(state, "en")

    # Same language: the fresh cached set is served, nothing is generated.
    _configure(monkeypatch, "en")
    body = await _get(state)
    assert body["suggestions"] == [{"text": "Keep going", "kind": "code"}]
    assert body["stale"] is False
    assert prompts == []

    # Language switched: the set is marked stale and regenerated in the background.
    _configure(monkeypatch, "ja")
    body = await _get(state)
    assert body["stale"] is True
    assert cache._task is not None
    await cache._task
    assert cache.language == "ja"
    assert len(prompts) == 1 and "BCP-47 tag ja" in prompts[0]
    assert (await _get(state))["suggestions"] == [{"text": "続ける", "kind": "code"}]


@pytest.mark.asyncio
async def test_a_slow_regeneration_after_a_language_switch_never_blocks_a_request(
    monkeypatch,
) -> None:
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_oneliner(_sessions, prompt, **_kw):
        calls.append(prompt)
        await release.wait()
        return json.dumps([{"text": "続ける", "kind": "code"}])

    monkeypatch.setattr(suggestions, "_build_context", lambda _state: _CONTEXT)
    monkeypatch.setattr(suggestions, "run_bg_oneliner", _slow_oneliner)
    state = _state()
    cache = _cached(state, "en")
    _configure(monkeypatch, "ja")

    # Both page views answer at once with the cached set while one generation runs.
    for _ in range(2):
        body = await asyncio.wait_for(_get(state), timeout=30)
        assert body["suggestions"] == [{"text": "Keep going", "kind": "code"}]
    await async_wait_until(lambda: len(calls) == 1)
    assert len(calls) == 1

    release.set()
    assert cache._task is not None
    await cache._task
    assert cache.language == "ja"
    assert cache.suggestions == [{"text": "続ける", "kind": "code"}]


# All a new install has to offer: no memory and no sessions, so the context is a
# time line under the 50-character floor that grounds a model call.
_THIN_CONTEXT = "## Current Time\nMonday 09:00"


@pytest.mark.asyncio
async def test_a_new_install_is_served_no_cards_in_any_language(monkeypatch) -> None:
    _configure(monkeypatch, "zh-CN")
    prompts = _capture_prompts(monkeypatch, json.dumps([{"text": "继续", "kind": "code"}]))
    monkeypatch.setattr(suggestions, "_build_context", lambda _state: _THIN_CONTEXT)

    body = await _get(_state())

    assert body["suggestions"] == []
    assert prompts == [], "a context under the floor must not reach the model"


async def _call_fails(_sessions, _prompt, **_kw):
    raise RuntimeError("background session unavailable")


async def _reply_unusable(_sessions, _prompt, **_kw):
    return "Sorry, I have no suggestions right now."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "oneliner", [_call_fails, _reply_unusable], ids=["call-fails", "reply-unusable"]
)
async def test_a_generation_that_yields_nothing_is_an_empty_list(monkeypatch, oneliner) -> None:
    monkeypatch.setattr(suggestions, "_build_context", lambda _state: _CONTEXT)
    monkeypatch.setattr(suggestions, "run_bg_oneliner", oneliner)

    assert await suggestions.generate_suggestions(_state(), ui_language="zh-CN") == []
