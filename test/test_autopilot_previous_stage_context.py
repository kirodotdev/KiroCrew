"""What a later stage inherits from the stages before it.

Inlining EVERY earlier stage at up to 2000 bytes makes the injected context grow
with the stage index — ~18 KB by stage 10 — and re-reads all of those files at
each boundary, on the worker the ``asyncio.to_thread`` hop exists to protect. Only
the three most recent are inlined in full; an older stage contributes its headline
and its path, which the model opens with its file tools. The path is emitted for
every stage either way, so nothing a later stage could reach is out of reach.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat_orchestrator import (
    _PREV_FULL_STAGES,
    _read_previous_results,
)


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _write_results(tmp_path, count, *, body="detail line about what happened"):
    """Write *count* stage result files and return the recorded (num, path) list."""
    recorded = []
    for n in range(1, count + 1):
        p = tmp_path / f"stage_{n}_result.md"
        p.write_text(f"Stage {n} headline\n{body} {n}\n", encoding="utf-8")
        recorded.append((n, str(p)))
    return recorded


class TestOnlyTheRecentStagesAreInlined:
    def test_older_stages_contribute_a_headline_and_a_path(self, tmp_path):
        """RED BEFORE: every stage's body was inlined, so stage 1's was present."""
        recorded = _write_results(tmp_path, _PREV_FULL_STAGES + 2)

        out = _read_previous_results(recorded)

        assert "Stage 1 headline" in out, "the summary of an older stage is gone too"
        assert (
            "detail line about what happened 1" not in out
        ), "stage 1's body was inlined; the context still grows with the plan"
        assert "detail line about what happened 2" not in out

    def test_the_last_three_keep_their_bodies(self, tmp_path):
        total = _PREV_FULL_STAGES + 2
        recorded = _write_results(tmp_path, total)

        out = _read_previous_results(recorded)

        for n in range(total - _PREV_FULL_STAGES + 1, total + 1):
            assert f"detail line about what happened {n}" in out, f"stage {n} lost its body"

    def test_every_stage_still_carries_its_path(self, tmp_path):
        """The summarised stages must stay REACHABLE, not merely mentioned."""
        recorded = _write_results(tmp_path, _PREV_FULL_STAGES + 2)

        out = _read_previous_results(recorded)

        for _n, path in recorded:
            assert f"`{path}`" in out

    def test_a_short_plan_is_unchanged(self, tmp_path):
        """Preservation: under the threshold nothing is summarised."""
        recorded = _write_results(tmp_path, _PREV_FULL_STAGES)

        out = _read_previous_results(recorded)

        for n in range(1, _PREV_FULL_STAGES + 1):
            assert f"detail line about what happened {n}" in out

    def test_an_older_stage_with_no_readable_headline_still_lists_its_path(self, tmp_path):
        recorded = _write_results(tmp_path, _PREV_FULL_STAGES + 1)
        # Stage 1 is the summarised one; leave it with nothing but a separator.
        (tmp_path / "stage_1_result.md").write_text("───── Stage 1 ─────\n", encoding="utf-8")

        out = _read_previous_results(recorded)

        assert "### Stage 1" in out
        assert f"`{recorded[0][1]}`" in out

    def test_a_missing_older_file_is_not_an_error(self, tmp_path):
        recorded = _write_results(tmp_path, _PREV_FULL_STAGES + 1)
        (tmp_path / "stage_1_result.md").unlink()

        out = _read_previous_results(recorded)

        assert "### Stage 1" in out

    def test_a_sensitive_older_path_contributes_no_content(self, tmp_path, monkeypatch):
        """The existing rule for full inlining has to hold for the headline too."""
        from kiro_crew.dashboard import chat_orchestrator

        recorded = _write_results(tmp_path, _PREV_FULL_STAGES + 1)
        secret = recorded[0][1]
        monkeypatch.setattr(chat_orchestrator, "is_sensitive_path", lambda p: str(p) == secret)

        out = _read_previous_results(recorded)

        assert "Stage 1 headline" not in out
        assert f"`{secret}`" in out
