"""``kirocrew eval --model NAME`` never scores the backend default as NAME.

A session drops a start-time pin its backend does not serve and records it in
``model_pin_refused``, then runs on the default. Scoring that session would
compare the default with itself and save a results file naming a model that
never ran, so the runner stops the session before its first turn, and the CLI
prints the refusal and exits 1 with nothing scored or saved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.eval import runner as runner_mod
from kiro_crew.eval.runner import EvalRunner, TurnResult
from kiro_crew.eval.scenario import Session, Turn


class _Provider:
    """The slice of a provider ``_run_session`` touches."""

    def __init__(self, refused: str) -> None:
        self.model_pin_refused = refused
        self.started = False
        self.shut_down = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shut_down = True


def _runner(provider: _Provider, sent: list[str]) -> EvalRunner:
    runner = EvalRunner(
        provider_factory=lambda _key, **_kw: provider, provider_kwargs={"model_override": "nope-1"}
    )

    async def _turn(_provider: Any, turn: Turn, _key: str) -> TurnResult:
        sent.append(turn.user)
        return TurnResult(user_message=turn.user, agent_response="ok")

    runner._run_turn = _turn  # type: ignore[method-assign]
    return runner


_SESSION = Session(name="s", turns=[Turn(user="hello"), Turn(user="again")])


@pytest.mark.asyncio
async def test_a_refused_pin_stops_the_session_before_its_first_turn(tmp_path: Path) -> None:
    provider = _Provider("nope-1")
    sent: list[str] = []

    with pytest.raises(Exception) as caught:
        await _runner(provider, sent)._run_session(_SESSION, tmp_path)

    assert type(caught.value).__name__ == "ModelNotOffered"
    assert str(caught.value) == "nope-1 is not a model this backend offers"
    assert sent == []
    assert provider.shut_down, "the started session must still be torn down"


@pytest.mark.asyncio
async def test_an_honoured_pin_runs_every_turn(tmp_path: Path) -> None:
    provider = _Provider("")
    sent: list[str] = []

    result = await _runner(provider, sent)._run_session(_SESSION, tmp_path)

    assert sent == ["hello", "again"]
    assert len(result.turns) == 2
    assert provider.shut_down


@pytest.mark.asyncio
async def test_eval_exits_1_and_saves_nothing_when_the_model_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    refusal = getattr(runner_mod, "ModelNotOffered", None)
    assert refusal is not None, "the runner names a refused model with ModelNotOffered"
    runner = MagicMock()
    runner.run_scenarios = AsyncMock(side_effect=refusal("nope-1"))
    written = MagicMock()
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.build_provider_factory", return_value=MagicMock()),
        patch("kiro_crew.cli_commands.EvalRunner", return_value=runner),
        patch("kiro_crew.cli_commands.write_eval_artifacts", written),
        pytest.raises(SystemExit) as exited,
    ):
        await cc._run_eval(
            _ns(all_scenarios=False, scenarios=["memory_recall_basic"], judge=False, model="nope-1")
        )

    assert exited.value.code == 1
    assert "nope-1 is not a model this backend offers" in capsys.readouterr().err
    written.assert_not_called()
    assert not (tmp_path / "eval_results").exists()
