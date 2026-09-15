"""Provider liveness reads the declared ``LLMProvider`` ABC, not hasattr probes.

``LLMProvider`` declares both liveness capabilities with safe defaults —
``is_process_alive()`` falls back to ``is_alive()`` and ``exit_code`` to
``None`` — so every provider answers a direct call and no call site needs a
capability probe. Harness parity (H14) reads capabilities off what an object
DECLARES; ``hasattr(provider, ...)`` is the absence-probe style the ABC
replaced, and it silently grades a non-provider object as dead instead of
failing loudly. The source scan below is a ratchet: it fails while any probe
remains in ``session.py`` and keeps future call sites from re-adding one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent, LLMProvider
from kiro_crew.session import _provider_effectively_alive

_SESSION_PY = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "session.py"


class _BareProvider(LLMProvider):
    """Only the abstract surface — exercises the ABC's liveness defaults."""

    async def start(self) -> None:
        raise AssertionError("not expected in this test")

    async def shutdown(self) -> None:
        raise AssertionError("not expected in this test")

    async def stream(self, message: str):
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        raise AssertionError("not expected in this test")

    async def reject_tool(self, request_id: str | int) -> None:
        raise AssertionError("not expected in this test")

    def context_usage_pct(self) -> float:
        return 0.0


class TestAbcLivenessContract:
    def test_is_process_alive_defaults_to_is_alive(self) -> None:
        provider = _BareProvider()
        assert provider.is_process_alive() is provider.is_alive()

    def test_exit_code_defaults_to_none(self) -> None:
        assert _BareProvider().exit_code is None

    def test_an_override_beats_the_default(self) -> None:
        provider = _BareProvider()
        provider.is_process_alive = lambda: True  # type: ignore[method-assign]
        provider.is_alive = lambda: False  # type: ignore[method-assign]
        assert provider.is_process_alive() is True


class TestEffectivelyAliveSemantics:
    def test_the_process_check_wins_over_the_stale_activity_view(self) -> None:
        # Pool processes sit idle, so is_alive()'s 600s stale-activity
        # threshold would falsely kill them; the helper must ask the
        # OS-truth check instead of the stale view.
        provider = _BareProvider()
        provider.is_alive = lambda: False  # type: ignore[method-assign]
        provider.is_process_alive = lambda: True  # type: ignore[method-assign]
        assert _provider_effectively_alive(provider) is True

    def test_a_dead_process_is_not_effectively_alive(self) -> None:
        provider = _BareProvider()
        provider.is_process_alive = lambda: False  # type: ignore[method-assign]
        assert _provider_effectively_alive(provider) is False


class TestNoCapabilityProbesRatchet:
    def test_session_source_has_no_provider_liveness_probes(self) -> None:
        text = _SESSION_PY.read_text(encoding="utf-8")
        tree = ast.parse(text)
        probed: list[str] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "hasattr"):
                continue
            if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant):
                continue
            attr = node.args[1].value
            if attr in ("is_process_alive", "exit_code"):
                probed.append(f"line {node.lineno}: hasattr(provider, {attr!r})")
        assert not probed, "provider liveness must come from the declared ABC: " + "; ".join(probed)
