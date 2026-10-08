"""Pass 4 (``?token=`` values) is deferred per streamed delta to the whole-segment run.

``_run_chat`` redacts each ``agent_message_chunk`` on its own and appends the
result to the segment it later stores. A delta cut inside a companion approval
link shows pass 4 a ``token=`` with no prefix in front of it, so the value was
redacted before ``_redact_segment`` -- the only pass that sees the whole link --
could grant the exemption. These tests stream the same link through the REAL
per-delta path at several chunk sizes and read what the slot stored.

NEUTRAL PLACEHOLDER PREFIX ONLY: a companion's real approval host never appears
in the public repo.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.security import redact_credentials

_PREFIX = "https://approve.example.com/auth"
_TOKEN = "eyJ3b3JrZmxvd0lkIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAwIn0%3D"
_URL = f"{_PREFIX}?token={_TOKEN}"
_TEXT = f"Approve the request here: {_URL} and then reply."
# A 40-char cut lands inside the prefix: the delta holding ``?token=`` starts at
# ``ple.com/auth``, so no installed prefix precedes the separator in that delta.
_CHUNK_SIZES = (len(_TEXT), 6, 40)

_SECRET = "SECRETVALUE0123456789abcdefghijklmnopqrstuv"
_OTHER_URL = f"https://other.example.net/cb?token={_SECRET}"

# A value pass 1 claims only PARTLY: an AWS-key-shaped head followed by an opaque
# tail. Only a value no earlier pass touched may be deferred to the whole-segment
# run; this one must have its uncovered tail redacted per delta, because the
# whole-segment run skips a value that begins with a fixed tag. The carrier is
# NOT a URL: `redact_exfiltration_urls` runs first per delta and flags any URL
# whose query holds an AWS key, which would hide what pass 4 did with the tail.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_OPAQUE = "opaquesecretvalue123"
_MIXED_TEXT = f"Callback: approve-cb?token={_AWS_KEY}{_OPAQUE} done."
# Cuts that keep `?token=` and the whole value inside one delta: one delta; the
# delta starting at `?`; a mid-carrier cut plus a cut right after the value.
_MIXED_CUTS = ((), (_MIXED_TEXT.index("?"),), (14, _MIXED_TEXT.index(" done.")))


class _StubCredentialPolicy:
    def __init__(self, prefixes: frozenset[str]):
        self._prefixes = prefixes

    def redact(self, text: str) -> str:
        from kiro_crew.security import redact

        return redact(text)

    def exempt_exact_hosts(self) -> frozenset[str]:
        return frozenset({"approve.example.com", "other.example.net"})

    def token_param_exempt_url_prefixes(self) -> frozenset[str]:
        return self._prefixes


def _install(prefixes: frozenset[str]) -> None:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import set_context

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_StubCredentialPolicy(prefixes)))


@pytest.fixture(autouse=True)
def _reset_platform_context():
    from kiro_crew.platform.context import reset_context

    yield
    reset_context()


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _chunks_at(text: str, cuts: tuple[int, ...]) -> list[str]:
    bounds = (0, *cuts, len(text))
    return [text[a:b] for a, b in zip(bounds, bounds[1:])]


def _client_streaming(events: list) -> AsyncMock:
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.available_models = MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    return client


async def _stream_turn(
    tmp_path, monkeypatch, text: str, size: int | tuple[int, ...]
) -> tuple[str, str]:
    """Run one turn streaming *text* in *size*-char deltas (or cut at *size* offsets).

    Returns ``(stored, wire)``: the assistant row the slot persisted and the
    concatenated ``chat_chunk`` broadcasts.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s1")
    deltas = _chunks(text, size) if isinstance(size, int) else _chunks_at(text, size)
    events = [LLMEvent(kind=EVENT_TEXT_CHUNK, text=d) for d in deltas]
    events.append(LLMEvent(kind=EVENT_COMPLETE))
    state.sessions.get_or_create = AsyncMock(return_value=(_client_streaming(events), True, False))

    from kiro_crew.dashboard.chat import _run_chat

    await _run_chat(state, slot, "show me the link")

    stored = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
    assert len(stored) == 1, stored
    wire = "".join(
        call.args[1]["content"]
        for call in state.broadcast_ws.call_args_list
        if call.args[0] == "chat_chunk"
    )
    return stored[0], wire


@pytest.mark.asyncio
@pytest.mark.parametrize("size", _CHUNK_SIZES)
async def test_companion_link_survives_every_chunking(tmp_path, monkeypatch, size: int) -> None:
    _install(frozenset({_PREFIX}))
    stored, wire = await _stream_turn(tmp_path, monkeypatch, _TEXT, size)
    assert stored == _TEXT
    # The live wire is a window that cannot see the whole destination: it never
    # exempts, so the value is withheld there whatever the chunking.
    assert _TOKEN not in wire
    assert "token=[REDACTED: credential]" in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("size", _CHUNK_SIZES)
async def test_no_prefix_installed_keeps_redacting(tmp_path, monkeypatch, size: int) -> None:
    _install(frozenset())
    stored, wire = await _stream_turn(tmp_path, monkeypatch, _TEXT, size)
    assert _TOKEN not in stored
    assert "?token=[REDACTED: credential]" in stored
    assert _TOKEN not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("size", (6, 40))
async def test_non_exempt_token_split_across_deltas_is_still_redacted(
    tmp_path, monkeypatch, size: int
) -> None:
    """A prefix set defers pass 4 per delta; the whole-segment run still applies it."""
    _install(frozenset({_PREFIX}))
    text = f"Callback: {_OTHER_URL} done."
    stored, wire = await _stream_turn(tmp_path, monkeypatch, text, size)
    assert _SECRET not in stored
    assert stored == "Callback: https://other.example.net/cb?token=[REDACTED: credential] done."
    assert _SECRET not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("cuts", _MIXED_CUTS)
async def test_partially_claimed_token_split_across_deltas_loses_its_tail(
    tmp_path, monkeypatch, cuts: tuple[int, ...]
) -> None:
    """A value pass 1 already partly claimed is not deferred; its tail is redacted too.

    Deferring it would persist the tail: the per-delta run leaves
    ``token=[REDACTED: credential]<tail>`` and the whole-segment run skips a value
    that begins with the fixed tag.
    """
    _install(frozenset({_PREFIX}))
    stored, wire = await _stream_turn(tmp_path, monkeypatch, _MIXED_TEXT, cuts)
    assert _AWS_KEY not in stored
    assert _OPAQUE not in stored
    assert stored == "Callback: approve-cb?token=[REDACTED: credential][REDACTED: credential] done."
    assert _AWS_KEY not in wire
    assert _OPAQUE not in wire


class TestDeferTokenParamKeyword:
    """The keyword itself, on the delta a 40-char cut produces."""

    _DELTA = _chunks(_TEXT, 40)[1]

    def test_default_is_byte_identical_without_a_prefix_set(self) -> None:
        _install(frozenset())
        assert redact_credentials(self._DELTA, defer_token_param=True) == redact_credentials(
            self._DELTA
        )
        assert "token=[REDACTED: credential]" in redact_credentials(self._DELTA)[0]

    def test_defers_only_pass_4_when_a_prefix_is_installed(self) -> None:
        _install(frozenset({_PREFIX}))
        delta = f"{self._DELTA} key AKIAIOSFODNN7EXAMPLE"
        out, warnings = redact_credentials(delta, defer_token_param=True)
        assert out == f"{self._DELTA} key [REDACTED: credential]"
        assert warnings and all("token parameter" not in w for w in warnings)
        # The plain call on the same delta still redacts the value.
        assert "token=[REDACTED: credential]" in redact_credentials(delta)[0]

    def test_other_callers_are_unchanged(self) -> None:
        _install(frozenset({_PREFIX}))
        out, _ = redact_credentials(self._DELTA)
        assert "token=[REDACTED: credential]" in out

    def test_partially_claimed_value_is_redacted_not_deferred(self) -> None:
        """Only a value no earlier pass touched is eligible for the exemption."""
        _install(frozenset({_PREFIX}))
        text = f"x?token={_AWS_KEY}{_OPAQUE}"
        deferred = redact_credentials(text, defer_token_param=True)
        assert deferred == redact_credentials(text)
        assert _OPAQUE not in deferred[0]
        assert deferred[0] == "x?token=[REDACTED: credential][REDACTED: credential]"

    def test_whole_value_after_a_partially_claimed_one_is_still_deferred(self) -> None:
        """The first match is redacted, and the loop goes on to defer the second."""
        _install(frozenset({_PREFIX}))
        text = f"a?token={_AWS_KEY}{_OPAQUE} b?token={_SECRET}"
        out, warnings = redact_credentials(text, defer_token_param=True)
        assert out == f"a?token=[REDACTED: credential][REDACTED: credential] b?token={_SECRET}"
        assert [w for w in warnings if "token parameter" in w] == [
            f"Redacted token parameter value ({len(_AWS_KEY) + len(_OPAQUE)} chars)"
        ]
