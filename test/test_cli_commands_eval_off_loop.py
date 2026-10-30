"""``kirocrew eval`` keeps its filesystem work off the event loop.

``_run_eval`` is a coroutine that runs every scenario's sessions concurrently on
one loop. Finding the scenario files (``resolve``, ``exists``, ``iterdir``),
loading them and writing the two result files are blocking disk calls, so each
test records the thread they ran on and requires it to differ from the loop's.
The provider, runner and judge are stubbed: no model is called, and the results
land in the chdir'd ``tmp_path``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig

_SCENARIOS_DIR = Path(cc.__file__).resolve().parent / "eval" / "scenarios"


def _recording(calls: dict[str, list[int]], name: str, real: Callable[..., Any]) -> Any:
    """``real``, wrapped to append the calling thread's id under ``name``."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.setdefault(name, []).append(threading.get_ident())
        return real(*args, **kwargs)

    return wrapper


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[int]]]:
    """Stub the eval's collaborators and record the thread of every disk call
    on the scenarios directory, every scenario load and the results write."""

    monkeypatch.chdir(tmp_path)
    recorded: dict[str, list[int]] = {}
    for method in ("exists", "iterdir"):
        real = getattr(Path, method)

        def scoped(self: Path, *a: Any, _real: Any = real, _name: str = method, **kw: Any) -> Any:
            if self == _SCENARIOS_DIR or self.parent == _SCENARIOS_DIR:
                recorded.setdefault(_name, []).append(threading.get_ident())
            return _real(self, *a, **kw)

        monkeypatch.setattr(Path, method, scoped)
    runner = MagicMock()
    runner.run_scenarios = AsyncMock(
        return_value=[SimpleNamespace(passed=True, sessions=[], summary=lambda: {})]
    )
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.build_provider_factory", return_value=MagicMock()),
        patch("kiro_crew.cli_commands.EvalRunner", return_value=runner),
        patch("kiro_crew.cli_commands.format_results", return_value="## Report"),
        patch("kiro_crew.cli_commands.score_by_dimension", return_value={}),
        patch(
            "kiro_crew.cli_commands.load_scenario",
            _recording(recorded, "load_scenario", cc.load_scenario),
        ),
        patch(
            "kiro_crew.cli_commands.load_scenarios",
            _recording(recorded, "load_scenarios", cc.load_scenarios),
        ),
        patch(
            "kiro_crew.cli_commands.write_eval_artifacts",
            _recording(recorded, "write_eval_artifacts", cc.write_eval_artifacts),
        ),
    ):
        yield recorded


def _on_loop(calls: dict[str, list[int]]) -> dict[str, list[int]]:
    """The recorded calls that ran on this (the event loop's) thread."""

    loop = threading.get_ident()
    return {name: ids for name, ids in calls.items() if loop in ids}


@pytest.mark.asyncio
async def test_named_scenario_is_found_loaded_and_saved_off_the_loop(
    calls: dict[str, list[int]], tmp_path: Path
) -> None:
    await cc._run_eval(_ns(all_scenarios=False, scenarios=["memory_recall_basic"], judge=False))
    assert {"exists", "load_scenario", "write_eval_artifacts"} <= calls.keys()
    assert _on_loop(calls) == {}
    assert len(list((tmp_path / "eval_results").iterdir())) == 2


@pytest.mark.asyncio
async def test_unknown_scenario_lists_the_directory_off_the_loop(
    calls: dict[str, list[int]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        await cc._run_eval(_ns(all_scenarios=False, scenarios=["does-not-exist"], judge=False))
    assert "Available scenarios:" in capsys.readouterr().err
    assert "iterdir" in calls
    assert _on_loop(calls) == {}
    assert not (tmp_path / "eval_results").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("all_scenarios", "loader"), [(True, "load_scenarios"), (False, "load_scenario")]
)
async def test_default_and_all_scenarios_load_off_the_loop(
    calls: dict[str, list[int]], all_scenarios: bool, loader: str
) -> None:
    await cc._run_eval(_ns(all_scenarios=all_scenarios, scenarios=None, judge=False))
    assert {loader, "write_eval_artifacts"} <= calls.keys()
    assert _on_loop(calls) == {}
