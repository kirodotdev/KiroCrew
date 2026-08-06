"""Tests for the shared `run_bg_oneliner` background one-liner helper in
llm_helpers — the consolidated acquire/drive/destroy skeleton used by title,
link-label, folder-icon, and session-summary generation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.client import AcpError, _rejected_model_from_error
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
async def test_auto_model_is_passed_to_set_model_for_wire_resolution():
    """``model="auto"`` IS forwarded to set_model. The real wire chokepoint
    (AcpSessionHandle.set_model -> resolve_usable_model) turns it into a usable
    id against the advertised list — "auto" where advertised, else the first
    advertised model — so a partition that doesn't serve auto never
    gets a raw ``auto`` on the wire. The fake session just records
    the requested value; resolver behavior is covered in test_acp_client."""
    sess = _FakeSession([
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hi"),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="auto")
    assert out == "hi"
    assert sess.model == "auto"
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_empty_model_does_not_override_session_default():
    """An empty model string inherits the session default (no set_model call)."""
    sess = _FakeSession([
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hi"),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="")
    assert out == "hi"
    assert sess.model is None
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


# ── Reactive retry-on-model-rejection (option A) ──


class _RejectThenSucceedSession:
    """First ``prompt()`` raises a model-rejection ``AcpError`` (as
    ``_raise_acp_error`` tags it); the second yields text. Records every
    ``set_model`` call so the test can assert the fallback model was applied."""

    def __init__(self, rejected: str, advertised: list, success_text: str = "ok"):
        self._calls = 0
        self._rejected = rejected
        self._advertised = advertised
        self._text = success_text
        self.models: list = []
        self.destroyed = False

    async def set_model(self, model):
        self.models.append(model)

    async def prompt(self, _prompt):
        self._calls += 1
        if self._calls == 1:
            err = AcpError("model rejected", transient=False)
            err.rejected_model = self._rejected
            err.advertised = list(self._advertised)
            raise err
        for e in [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=self._text),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]:
            yield e

    async def reject_tool(self, request_id):
        pass

    async def destroy(self):
        self.destroyed = True


@pytest.mark.asyncio
async def test_reactive_retry_on_rejection_uses_first_advertised():
    """When the preferred model is refused mid-prompt (e.g. "auto" on a
    partition that doesn't serve it), retry ONCE with the first
    advertised model that is neither the rejected id nor "auto"."""
    sess = _RejectThenSucceedSession("auto", ["gpt-5.6-terra", "gpt-5.6-luna"])
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="auto")
    assert out == "ok"
    assert sess.models == ["auto", "gpt-5.6-terra"]
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_reactive_retry_reraises_when_no_usable_fallback():
    """No advertised model other than the rejected id / "auto" → nothing safe to
    retry with, so the error propagates (caller decides fail-open)."""
    sess = _RejectThenSucceedSession("auto", ["auto"])
    with pytest.raises(AcpError):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="auto")
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_non_rejection_error_is_not_retried():
    """A generic AcpError with no rejected_model tag is not a model rejection —
    it must propagate unchanged (no retry), and the session is destroyed."""
    class _BoomSession(_FakeSession):
        async def prompt(self, _p):
            raise AcpError("backend boom")
            yield  # pragma: no cover

    sess = _BoomSession([])
    with pytest.raises(AcpError, match="backend boom"):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="auto")
    assert sess.destroyed is True


class TestRejectedModelClassifier:
    def test_matches_invalid_model_id(self):
        assert (
            _rejected_model_from_error({"data": "Invalid model ID: claude-haiku-4.5"})
            == "claude-haiku-4.5"
        )

    def test_matches_invalid_model_id_auto_in_message(self):
        assert _rejected_model_from_error({"message": "Invalid model ID: auto"}) == "auto"

    def test_matches_model_not_available(self):
        assert (
            _rejected_model_from_error({"data": "The model 'opus-x' is not available"})
            == "opus-x"
        )

    def test_returns_none_for_unrelated_error(self):
        assert _rejected_model_from_error({"data": "ThrottlingException: slow down"}) is None

    def test_returns_none_for_non_dict(self):
        assert _rejected_model_from_error("nonsense") is None


@pytest.mark.asyncio
async def test_permission_denial_is_audited_even_if_reject_fails(monkeypatch):
    """Audit-before-reject: the SEL denial is emitted BEFORE ``reject_tool``, so a
    ``reject_tool`` transport failure cannot skip the audit (every permission
    decision must be logged; backend-security-controls)."""
    logged: list = []
    import kiro_crew.llm_helpers as mod

    monkeypatch.setattr(
        mod, "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw)),
    )

    class _RejectRaises(_FakeSession):
        async def reject_tool(self, request_id):
            raise RuntimeError("transport down")

    sess = _RejectRaises([
        SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
        SimpleNamespace(kind=EVENT_COMPLETE, text=""),
    ])
    with pytest.raises(RuntimeError, match="transport down"):
        await run_bg_oneliner(_FakeSessions(sess), "p", sel_source="unit")
    # Denial was audited despite reject_tool failing, and the handle was destroyed.
    assert logged and logged[0]["outcome"] == "denied"
    assert sess.destroyed is True
