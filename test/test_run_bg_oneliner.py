"""Tests for the shared `run_bg_oneliner` background one-liner helper in
llm_helpers — the consolidated acquire/drive/destroy skeleton used by title,
link-label, folder-icon, and session-summary generation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.llm_helpers import run_bg_oneliner


class _FakeSession:
    def __init__(self, events, *, raise_on_prompt=False):
        self._events = events
        self._raise = raise_on_prompt
        self.destroyed = False
        self.model = None
        self.rejected: list = []

    async def set_model(self, model):
        self.model = model

    async def prompt(self, _prompt):
        if self._raise:
            raise RuntimeError("backend boom")
        for e in self._events:
            yield e

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def destroy(self):
        self.destroyed = True


class _FakeSessions:
    def __init__(self, session):
        self._session = session

    async def get_bg_session(self):
        return self._session


@pytest.mark.asyncio
async def test_accumulates_text_and_sets_model_and_destroys():
    sess = _FakeSession([
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hello "),
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="world"),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-haiku-4.5")
    assert out == "hello world"
    assert sess.model == "claude-haiku-4.5"
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_permission_request_is_rejected_and_sel_logged(monkeypatch):
    logged: list = []
    import kiro_crew.llm_helpers as mod

    def _fake_sel():
        return SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw))

    monkeypatch.setattr(mod, "_sel", _fake_sel)
    sess = _FakeSession([
        SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    out = await run_bg_oneliner(_FakeSessions(sess), "p", sel_source="unit")
    assert out == "ok"
    assert sess.rejected == ["r1"]
    assert logged and logged[0]["outcome"] == "denied" and logged[0]["source"] == "unit"


@pytest.mark.asyncio
async def test_permission_denial_is_sel_logged_even_without_sel_source(monkeypatch):
    """Every permission decision must be audited — a caller that omits
    ``sel_source`` still produces a ``denied`` SEL event under the generic
    ``bg_oneliner`` source (backend-security-controls; Codex HIGH regression)."""
    logged: list = []
    import kiro_crew.llm_helpers as mod

    def _fake_sel():
        return SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw))

    monkeypatch.setattr(mod, "_sel", _fake_sel)
    sess = _FakeSession([
        SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    # No sel_source passed — mirrors chat_title / _summarize_one call sites.
    out = await run_bg_oneliner(_FakeSessions(sess), "p")
    assert out == ""
    assert sess.rejected == ["r1"]
    assert logged, "denial must be SEL-logged even without an explicit sel_source"
    assert logged[0]["outcome"] == "denied"
    assert logged[0]["source"] == "bg_oneliner"


@pytest.mark.asyncio
async def test_propagates_error_and_destroys():
    sess = _FakeSession([], raise_on_prompt=True)
    with pytest.raises(RuntimeError, match="boom"):
        await run_bg_oneliner(_FakeSessions(sess), "p")
    assert sess.destroyed is True
