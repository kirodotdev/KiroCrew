"""Golden test for what one chat turn sends to each socket and writes to its transcript.

Each scenario replays a recorded ACP turn from ``test/fixtures/acp_frames/``
through ``chat_runner._run_chat`` with two dashboard-user socket doubles
registered, and compares the ordered frames each socket received, plus the
transcript lines the turn wrote, with ``test/fixtures/turn_golden/``. See
``test/turn_golden_harness.py`` for what is pinned and what is normalized.

This is the guard for any change that moves where frames are produced or how
they reach a socket, such as a per-socket writer queue or a process boundary
between the turn and the hub: such a change must leave every golden file
unchanged. A change that alters frames on purpose rewrites the files with
``python3 scripts/update_turn_golden_snapshots.py`` in the same commit, so the
review shows the frame diff. The test itself never writes into the repository.

When ``KIROCREW_TURN_GOLDEN_OUT`` names a directory, the test writes the rendered
result there instead of comparing it. Only the writer script sets it, and it
points outside the repository.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest
from turn_golden_harness import (
    GOLDEN_DIR,
    SAME_AS_VIEWER,
    SCENARIOS,
    golden_path,
    render,
    run_scenario,
)

from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.platform import governance_profiles

_OUT_ENV = "KIROCREW_TURN_GOLDEN_OUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_turn_matches_golden(scenario: str, tmp_path: Path) -> None:
    golden = await run_scenario(scenario, tmp_path / "home")
    rendered = render(golden)
    out_dir = os.environ.get(_OUT_ENV)
    if out_dir:
        (Path(out_dir) / golden_path(scenario).name).write_text(rendered, encoding="utf-8")
        return
    target = golden_path(scenario)
    assert (
        target.exists()
    ), f"{target.name} is missing; run `python3 scripts/update_turn_golden_snapshots.py`"
    expected = target.read_text(encoding="utf-8")
    if rendered != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{target.name} (committed)",
                tofile=f"{target.name} (this run)",
                n=2,
            )
        )
        pytest.fail(
            f"{scenario}: the frames or transcript of this turn changed.\n"
            "If the change is intended, run `python3 scripts/update_turn_golden_snapshots.py` "
            "and commit the rewritten file with it.\n" + diff[:6000]
        )


@pytest.mark.asyncio
async def test_golden_is_not_vacuous(tmp_path: Path) -> None:
    """A golden that records nothing passes against any change, so pin its floor.

    Every socket must receive the turn's stream frames and its end frame, and the
    transcript must hold the assistant reply. If the harness stops reaching the
    real fan-out (a stub left in place, a socket left unregistered), this fails
    by name instead of the goldens silently shrinking on the next rewrite.
    """
    golden = await run_scenario(SCENARIOS[0], tmp_path / "home")
    for name, frames in golden["sockets"].items():
        if frames == SAME_AS_VIEWER:
            frames = golden["sockets"]["viewer"]
        types = [frame["type"] for frame in frames]
        for required in ("slots", "tool_call", "tool_result", "chat_chunk", "chat_done"):
            assert required in types, f"socket {name} received no {required} frame: {types}"
        assert types.index("chat_chunk") < types.index("chat_done"), types
    (lines,) = golden["transcript"].values()
    assert any('"role": "assistant"' in line for line in lines), lines


@pytest.mark.asyncio
async def test_golden_ignores_process_wide_counters(tmp_path: Path, monkeypatch) -> None:
    """A counter another test on the worker moved must not change the golden.

    ``gitlabHostsGeneration`` and ``governanceGeneration`` ride every slots frame
    and count events of the whole process, so an earlier test that publishes
    provider hosts or reloads a governance profile moves them. Both are moved
    here before the turn, and the result must still equal the committed file.
    """
    # Patched where ``_do_slots_broadcast`` imports them from, at call time.
    monkeypatch.setattr(source_providers, "gitlab_hosts_generation", lambda: 41)
    monkeypatch.setattr(governance_profiles, "governance_answer_generation", lambda: 97)
    rendered = render(await run_scenario(SCENARIOS[0], tmp_path / "home"))
    assert rendered == golden_path(SCENARIOS[0]).read_text(encoding="utf-8")


def test_every_scenario_has_one_golden_file() -> None:
    """A golden file with no scenario is dead weight; a scenario with none never ran."""
    expected = {golden_path(s).name for s in SCENARIOS}
    present = {p.name for p in GOLDEN_DIR.glob("*.golden.json")}
    assert present == expected, f"missing {expected - present}, stray {present - expected}"
