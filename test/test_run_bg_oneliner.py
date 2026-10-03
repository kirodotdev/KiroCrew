"""Tests for the shared `run_bg_oneliner` background one-liner helper in
llm_helpers — the consolidated acquire/drive/destroy skeleton used by title,
link-label, folder-icon, and session-summary generation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError, _rejected_model_from_error
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
    TurnUsage,
)
from kiro_crew.image_refs import strip_image_refs
from kiro_crew.llm_helpers import run_bg_oneliner


class _FakeSession:
    def __init__(self, events, *, raise_on_prompt=False, turn_credits=0.0, prior_credits=0.0):
        self._events = events
        self._raise = raise_on_prompt
        self._turn_credits = turn_credits
        self.destroyed = False
        self.model = None
        # Mirrors AcpSessionHandle: "" until a set_model lands, then the
        # backend-resolved id (the fake resolves to exactly what was asked).
        self.served_model = ""
        self.rejected: list = []
        # Mirrors AcpSessionHandle: the shared session arrives carrying whatever
        # the PREVIOUS turn left behind, and installs fresh per-turn stats only
        # once a turn actually begins (see prompt()).
        self.last_prompt_stats = SimpleNamespace(credits=prior_credits)

    async def set_model(self, model):
        self.model = model
        self.served_model = model

    async def prompt(self, _prompt, *, allow_image=True):
        if self._raise:
            raise RuntimeError("backend boom")
        # Fresh per-turn stats, installed as the real handle does once the turn
        # starts -- AFTER any pre-dispatch failure, which is what makes a failed
        # dispatch distinguishable from a turn that ran.
        self.last_prompt_stats = SimpleNamespace(credits=self._turn_credits)
        for e in self._events:
            yield e

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def destroy(self):
        self.destroyed = True


class _FakeSessions:
    def __init__(self, session):
        self._session = session

    async def get_bg_session(self, start_priority=None):
        return self._session


@pytest.mark.asyncio
async def test_output_byte_budget_refuses_multibyte_overflow_and_destroys_handle():
    session = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="界"),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="界"),
        ]
    )
    with pytest.raises(ValueError, match="output budget"):
        await run_bg_oneliner(_FakeSessions(session), "p", max_output_bytes=5)
    assert session.destroyed


class _ResettingSessions(_FakeSessions):
    """A registry whose owning slot is RESET while the background call runs.

    ``get_provider`` answers the pre-reset unit the first time and the successor
    every time after, which is what a slot reset, agent switch or compaction does:
    the successor cold-starts a new ACP session id.
    """

    def __init__(self, session, key: str, before: str, after: str) -> None:
        super().__init__(session)
        self._key = key
        self._answers = [before]
        self._after = after

    def get_provider(self, key: str):
        if key != self._key:
            return None
        served = self._answers.pop(0) if self._answers else self._after
        return SimpleNamespace(session_id=served)


@pytest.mark.asyncio
async def test_the_background_owner_is_pinned_before_the_call_not_after_it(monkeypatch):
    """The spend must stay with the session that ordered the work.

    ``run_bg_oneliner`` writes its ledger entry in the teardown, and the resolver
    answers which unit a slot's work is landing in NOW -- so resolving there would
    file this call's cost against a successor session that never incurred it,
    silently, in an append-only file. The registry double flips its answer between
    the two moments, so only a pre-call resolution can pass.
    """
    from kiro_crew.crew_log import emit

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    recorded: list[str] = []
    monkeypatch.setattr(
        emit,
        "on_background_completed",
        lambda session_id, **_kw: recorded.append(session_id),
    )
    session = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hi"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ],
        turn_credits=1.0,
    )
    sessions = _ResettingSessions(session, "dashboard:chat-7", "owner-sid", "successor-sid")
    await run_bg_oneliner(
        sessions,
        "prompt",
        crew_log_kind="title",
        crew_log_session_key="dashboard:chat-7",
    )
    assert recorded == ["owner-sid"], "the entry must name the unit that ordered the work"


@pytest.mark.asyncio
async def test_accumulates_text_and_sets_model_and_destroys():
    sess = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hello "),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="world"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-haiku-4.5")
    assert out == "hello world"
    assert sess.model == "claude-haiku-4.5"
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_image_paths_in_the_prompt_are_not_sent_as_attachments(tmp_path):
    """A one-liner quotes session history; a pasted screenshot's path in it must
    reach the builder as the history marker, never as a readable path it would
    inline as an image block (a text-only model rejects the whole request)."""
    from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER

    shot = tmp_path / "pasted-image-1.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    sent: list[str] = []

    class _Capturing(_FakeSession):
        async def prompt(self, prompt, *, allow_image=True):
            sent.append(prompt)
            async for event in super().prompt(prompt):
                yield event

    sess = _Capturing([SimpleNamespace(kind=EVENT_COMPLETE, text="")])
    await run_bg_oneliner(_FakeSessions(sess), f"Summarize.\nuser: look at {shot} and `{shot}`")

    assert sent == [f"Summarize.\nuser: look at {STRIPPED_IMAGE_MARKER} and `{shot}`"]


@pytest.mark.asyncio
async def test_auto_model_is_passed_to_set_model_for_wire_resolution():
    """``model="auto"`` IS forwarded to set_model. The real wire chokepoint
    (AcpSessionHandle.set_model -> resolve_usable_model) turns it into a usable
    id against the advertised list — "auto" where advertised, else the first
    advertised model — so a partition that doesn't serve auto never
    gets a raw ``auto`` on the wire. The fake session just records
    the requested value; resolver behavior is covered in test_acp_client."""
    sess = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hi"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="auto")
    assert out == "hi"
    assert sess.model == "auto"
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_empty_model_does_not_override_session_default():
    """An empty model string inherits the session default (no set_model call)."""
    sess = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="hi"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
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
    sess = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
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
    sess = _FakeSession(
        [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
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
        self.served_model = ""
        self.destroyed = False

    async def set_model(self, model):
        self.models.append(model)
        self.served_model = model

    async def prompt(self, _prompt, *, allow_image=True):
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
async def test_budgeted_caller_does_not_spend_a_second_attempt_on_model_rejection():
    sess = _RejectThenSucceedSession("auto", ["available-test-model"])
    with pytest.raises(AcpError):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="auto", retry_rejected_model=False)
    assert sess._calls == 1
    assert sess.models == ["auto"]
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_strict_model_disables_rejected_model_fallback():
    """``strict_model=True`` makes the requested model a hard requirement: a
    mid-prompt rejection propagates instead of retrying on the first
    advertised model. Callers whose result is only meaningful on that exact
    model (the poisoned-conversation canary) must never receive a success
    produced by a different model."""
    sess = _RejectThenSucceedSession("claude-x", ["gpt-5.6-terra"])
    with pytest.raises(AcpError):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-x", strict_model=True)
    # Only the strict model was ever set — no fallback attempt.
    assert sess.models == ["claude-x"]
    assert sess.destroyed is True


class _SetModelFailsSession(_FakeSession):
    async def set_model(self, model):
        raise RuntimeError("override refused")


class _SilentInheritSession(_FakeSession):
    """Mirrors the substitute-style set_model seam: a requested id absent from
    the advertised set is a silent no-op (resolve_usable_model → "" → inherit
    the session default) — NO exception, session keeps serving its default."""

    async def set_model(self, model):
        self.model = model  # request recorded...
        self.served_model = "some-default-model"  # ...but default still served


@pytest.mark.asyncio
async def test_strict_model_rejects_silent_default_inherit():
    """``strict_model=True`` verifies the POST-CONDITION, not just that
    set_model didn't raise: when the seam silently inherits the session
    default (requested id not advertised), the canary must refuse rather than
    produce a success on a different model — that success would be fabricated
    evidence for discarding a healthy conversation."""
    sess = _SilentInheritSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="should never stream"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    with pytest.raises(RuntimeError, match="serves"):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-x", strict_model=True)
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_strict_model_raises_when_set_model_fails():
    """``strict_model=True`` + failed set_model override → raise, never run
    the prompt on the session default model."""
    sess = _SetModelFailsSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="should never stream"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    with pytest.raises(RuntimeError, match="override refused"):
        await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-x", strict_model=True)
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_lenient_mode_still_degrades_on_set_model_failure():
    """Default (lenient) behavior is unchanged: a failed override logs and
    runs on the session default."""
    sess = _SetModelFailsSession(
        [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    out = await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-x")
    assert out == "ok"
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_non_rejection_error_is_not_retried():
    """A generic AcpError with no rejected_model tag is not a model rejection —
    it must propagate unchanged (no retry), and the session is destroyed."""

    class _BoomSession(_FakeSession):
        async def prompt(self, _p, *, allow_image=True):
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
            _rejected_model_from_error({"data": "The model 'opus-x' is not available"}) == "opus-x"
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
        mod,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw)),
    )

    class _RejectRaises(_FakeSession):
        async def reject_tool(self, request_id):
            raise RuntimeError("transport down")

    sess = _RejectRaises(
        [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", text=""),
            SimpleNamespace(kind=EVENT_COMPLETE, text=""),
        ]
    )
    with pytest.raises(RuntimeError, match="transport down"):
        await run_bg_oneliner(_FakeSessions(sess), "p", sel_source="unit")
    # Denial was audited despite reject_tool failing, and the handle was destroyed.
    assert logged and logged[0]["outcome"] == "denied"
    assert sess.destroyed is True


_USAGE_TARGET = "kiro_crew.dashboard.handlers.usage.persist_token_record_async"


class _SubstitutingSession(_FakeSession):
    """``set_model`` lands, but the backend serves a DIFFERENT id.

    That is the substitute-style seam which makes a requested model a preference
    rather than a guarantee, and it is the case where recording the request would
    bill spend to a model that never ran.
    """

    async def set_model(self, model):
        self.model = model
        self.served_model = "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_cancellation_while_accounting_still_destroys_the_session():
    """CancelledError is a BaseException, so the accounting handler never sees
    it. If destroy() sat after that handler rather than in a finally, a
    cancelled turn would leak the session's runtime."""
    sess = _FakeSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_credits=2.0)

    with patch(_USAGE_TARGET, side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await run_bg_oneliner(_FakeSessions(sess), "p")

    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_spend_is_recorded_against_the_served_model_not_the_requested_one():
    sess = _SubstitutingSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_credits=1.5)

    with patch(_USAGE_TARGET) as persist:
        await run_bg_oneliner(_FakeSessions(sess), "p", model="claude-haiku-4.5")

    assert persist.await_args.args[1] == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_a_dispatch_that_never_ran_does_not_rebill_the_previous_turn():
    """The shared session arrives carrying the last turn's credits, which were
    already recorded. A prompt that fails before the runner installs fresh stats
    must record nothing rather than bill that earlier turn a second time."""
    sess = _FakeSession([], raise_on_prompt=True, prior_credits=9.0)

    with patch(_USAGE_TARGET) as persist:
        with pytest.raises(RuntimeError):
            await run_bg_oneliner(_FakeSessions(sess), "p")

    persist.assert_not_awaited()
    assert sess.destroyed is True


@pytest.mark.asyncio
async def test_an_unbilled_turn_records_nothing():
    """A turn that ran but cost nothing has no row worth writing."""
    sess = _FakeSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")])

    with patch(_USAGE_TARGET) as persist:
        await run_bg_oneliner(_FakeSessions(sess), "p")

    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_turn_duration_reaches_the_row():
    """The acp provider never fills TurnUsage.duration_ms, so this local
    measurement is the only duration a background row can carry. The clock is
    substituted rather than timed, so the assertion pins the arithmetic rather
    than the host's speed."""
    sess = _FakeSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_credits=1.0)
    # Exactly representable in binary floating point, so the truncation to ms is
    # unambiguous and the assertion cannot drift on a fraction like 0.4.
    clock = iter([50.0, 50.25])

    with patch("kiro_crew.llm_helpers.time", SimpleNamespace(monotonic=lambda: next(clock))):
        with patch(_USAGE_TARGET) as persist:
            await run_bg_oneliner(_FakeSessions(sess), "p")

    assert persist.await_args.kwargs["elapsed_ms"] == 250


class _ClaudeSeamStats:
    """Mirrors AcpPromptStats on the claude seam: ``credits`` stays 0 and the
    billing dimensions travel through ``to_turn_usage()`` (the
    stats -> event contract that ``_attempt_usage`` duck-types on)."""

    def __init__(self, usage: TurnUsage) -> None:
        self.credits = 0.0
        self._usage = usage

    def to_turn_usage(self) -> TurnUsage:
        return self._usage


class _ClaudeSeamSession(_FakeSession):
    """Installs claude-seam per-turn stats once the turn begins."""

    def __init__(self, events, *, turn_stats: _ClaudeSeamStats) -> None:
        super().__init__(events)
        self._turn_stats = turn_stats

    async def prompt(self, _prompt, *, allow_image=True):
        self.last_prompt_stats = self._turn_stats
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_a_cost_only_claude_seam_turn_writes_a_row_with_cost_and_cache_intact():
    """On the claude seam a background turn can bill ``cost_usd`` with credits
    AND both token counts at zero -- the cost-with-zero-tokens shape. The row must be written
    (mutation guard on the gate's ``cost_usd`` conjunct) and must carry the
    cost and cache fields through ``_attempt_usage``'s ``to_turn_usage`` path
    (mutation guard on the duck-typed converter: the credits-only fallback
    constructor would zero every field and skip the row)."""
    stats = _ClaudeSeamStats(
        TurnUsage(cost_usd=0.42, cache_creation_tokens=20, cache_read_tokens=30)
    )
    sess = _ClaudeSeamSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_stats=stats)

    with patch(_USAGE_TARGET) as persist:
        await run_bg_oneliner(_FakeSessions(sess), "p")

    assert persist.await_count == 1
    recorded = persist.await_args.args[2]
    assert recorded.cost_usd == pytest.approx(0.42)
    assert recorded.cache_creation_tokens == 20
    assert recorded.cache_read_tokens == 30
    assert recorded.credits == 0.0


@pytest.mark.asyncio
async def test_a_cache_only_claude_seam_turn_still_writes_its_row():
    """A claude-seam turn can report cache tokens with no cost (the adapter
    fills PromptResponse token counts but sends no usage_update.cost) and zero
    fresh input/output. The shared gate predicate must not drop it -- the
    narrower sibling of the cost-only shape."""
    stats = _ClaudeSeamStats(TurnUsage(cache_read_tokens=30))
    sess = _ClaudeSeamSession([SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_stats=stats)

    with patch(_USAGE_TARGET) as persist:
        await run_bg_oneliner(_FakeSessions(sess), "p")

    assert persist.await_count == 1
    assert persist.await_args.args[2].cache_read_tokens == 30


@pytest.mark.asyncio
async def test_a_claude_seam_turn_that_billed_nothing_still_records_nothing():
    """The all-zero guard survives the wider gate: a claude-seam turn whose
    stats carried no billing at all must not land as zero-value noise."""
    sess = _ClaudeSeamSession(
        [SimpleNamespace(kind=EVENT_COMPLETE, text="")], turn_stats=_ClaudeSeamStats(TurnUsage())
    )

    with patch(_USAGE_TARGET) as persist:
        await run_bg_oneliner(_FakeSessions(sess), "p")

    persist.assert_not_awaited()


# ── Text-only turns ──────────────────────────────────────────────────────────
#
# A background one-liner's prompt goes out text-only, on every harness path.
#
# ``run_bg_oneliner`` prompts are text ABOUT a session -- a title, a summary, a
# suggestion, a status card -- so an image path inside one is quoted history, not
# an attachment. ``strip_image_refs`` keeps a spaced path whose shape cannot be
# told from prose, so the scrub alone cannot keep a still-readable file out of
# the request; the text-only seam does, whatever shape a caller composes.
#
# The three composed prompts below each put a channel attachment line somewhere
# the scrub leaves it (mid-line, after a prefix, inside a JSON string) and each
# asserts that, sent the default way, the prompt WOULD carry an image block --
# so the test fails if the seam is dropped rather than passing vacuously.

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


@pytest.fixture
def attachment():
    """A readable channel attachment under a temp directory holding a space.

    Directly under the system temp directory rather than ``tmp_path``: the
    suggestions builder keeps 150 characters of a row, and a Windows runner's
    ``tmp_path`` alone runs past that, cutting the path before its suffix.
    """
    with tempfile.TemporaryDirectory(prefix="kc") as root:
        spaced = pathlib.Path(root) / "John Smith"
        spaced.mkdir()
        path = spaced / "tmpab12cd_4.png"
        path.write_bytes(_PNG)
        yield str(path)


class _BuildingSession(_FakeSession):
    """A background handle that builds the real prompt blocks it would send."""

    def __init__(self) -> None:
        super().__init__([])
        self.sent: list[list[dict]] = []

    async def prompt(self, message, *, allow_image=True):
        self.sent.append(build_prompt_blocks(message, allow_image=allow_image))
        yield SimpleNamespace(kind=EVENT_COMPLETE, text="")


async def _send(prompt: str) -> list[list[dict]]:
    # The premise: sent with inlining on, the scrubbed prompt carries the file.
    assert "image" in _types(build_prompt_blocks(strip_image_refs(prompt)))
    session = _BuildingSession()
    await run_bg_oneliner(_FakeSessions(session), prompt)
    return session.sent


@pytest.mark.asyncio
async def test_the_title_prompt_carries_no_image(attachment):
    from kiro_crew.dashboard.chat_title import _build_title_prompt

    prompt = _build_title_prompt([{"role": "user", "content": f"look at this\n{attachment}"}])

    assert prompt and attachment in prompt
    assert [_types(b) for b in await _send(prompt)] == [["text"]]


@pytest.mark.asyncio
async def test_the_suggestions_prompt_carries_no_image(attachment, monkeypatch):
    from kiro_crew import suggestions

    monkeypatch.setattr(
        suggestions.ContextBuilder, "get_memory_for", MagicMock(side_effect=RuntimeError)
    )
    log = SimpleNamespace(
        list_sessions=lambda: [{"key": "k", "title": "Screenshots"}],
        derive_recent=lambda key, max_messages: [{"role": "user", "content": attachment}],
    )
    state = SimpleNamespace(conversation_log=log, crons=SimpleNamespace(list_jobs=lambda: []))
    context = suggestions._build_context(state)

    assert f"  - User: {attachment}" in context
    assert [_types(b) for b in await _send(context)] == [["text"]]


@pytest.mark.skipif(os.name == "nt", reason="JSON doubles a Windows path's separators")
@pytest.mark.asyncio
async def test_the_status_card_prompt_carries_no_image(attachment):
    from kiro_crew.dashboard import card_lifecycle

    rows = card_lifecycle._evidence_rows([{"role": "user", "content": attachment}])
    prompt = card_lifecycle._ROOT_PROMPT + json.dumps({"recent_messages": rows})

    assert f'"text": "{attachment}"' in prompt
    assert [_types(b) for b in await _send(prompt)] == [["text"]]


# ── The seam on each path a background handle can take ──────────────────────


class _PromptRuntime:
    """Runtime double that answers one session/prompt with an end of turn."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = True
        self.acp_backend = ""
        self.requests: list[tuple[str, dict]] = []
        self._queue = queue

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        self.requests.append((method, params))
        self._queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
        return 7


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow_image", "expected"), [(True, ["image"]), (False, [])])
async def test_the_runtime_handle_honours_the_seam(attachment, allow_image, expected):
    from kiro_crew.acp.session_handle import AcpSessionHandle

    queue: asyncio.Queue = asyncio.Queue()
    rt = _PromptRuntime(queue)
    handle = AcpSessionHandle("sA", queue, rt)

    async for _ in handle.prompt(f"look {attachment}", timeout=5.0, allow_image=allow_image):
        pass

    _method, params = rt.requests[-1]
    assert [t for t in _types(params["prompt"]) if t == "image"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow_image", "expected"), [(True, ["image"]), (False, [])])
async def test_the_direct_client_honours_the_seam(tmp_path, attachment, allow_image, expected):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    client._session_id = "sid"
    client._send_request = AsyncMock(return_value=3)

    await client._send_prompt(f"look {attachment}", allow_image=allow_image)

    _method, params = client._send_request.await_args[0]
    assert [t for t in _types(params["prompt"]) if t == "image"] == expected


@pytest.mark.asyncio
async def test_the_provider_paths_forward_the_seam():
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session_background import _ProviderBgSession

    seen: list[object] = []

    async def _record(message, *, allow_image=True):
        seen.append(allow_image)
        return
        yield  # pragma: no cover - makes this an async generator

    # The provider-backed background adapter, over a non-runtime backend.
    provider = SimpleNamespace(stream=_record)
    bg = _ProviderBgSession(SimpleNamespace(semaphore=asyncio.Semaphore(1), provider=provider))
    async for _ in bg.prompt("p", allow_image=False):
        pass

    # The direct-client provider that entry holds.
    with patch("kiro_crew.providers.acp.AcpClient"):
        direct = AcpProvider(acp_backend="")
    direct._client = MagicMock()
    direct._client.stream_events = _record
    async for _ in direct.stream("p", allow_image=False):
        pass

    # The runtime-backed provider.
    handle = MagicMock()
    handle.prompt = _record
    on_runtime = AcpSessionProvider(handle, MagicMock())
    async for _ in on_runtime.stream("p", allow_image=False):
        pass

    assert seen == [False, False, False]
