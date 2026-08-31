"""A spent OPTIONS record renders NO recommendation, and the value is still threaded.

Two separate contracts, and the distinction is the point.

RENDER: the struck-through record of a superseded turn shows no marker. Nothing on it can
be acted on, so a marker would buy a reader nothing while re-introducing the raw
``(recommended)`` text that the rest of this path keeps out of what a reader sees.

THREAD: the backfill still carries the value to the options sink rather than discarding it
at the parse. The live control renders it; the spent record chooses not to. Those are
different decisions, and dropping the value at the parse would remove the choice.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from kiro_crew.slack.format import build_options_selected_blocks

CHOICES = ["Merge it now", "Hold it open"]


def _text(blocks: list[dict]) -> str:
    out = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict) and element.get("type") == "mrkdwn":
                out.append(element.get("text", ""))
    return "\n".join(out)


class TestTheSpentRecordRendersNoRecommendation:
    def test_no_marker_appears_on_a_spent_record(self):
        rendered = _text(build_options_selected_blocks(CHOICES, []))
        assert "(recommended)" not in rendered, rendered
        assert "Recommended" not in rendered, rendered

    def test_both_choices_still_render_struck_through(self):
        # Guards over-removal: dropping the marker must not drop the record.
        rendered = _text(build_options_selected_blocks(CHOICES, []))
        assert "~Merge it now~" in rendered, rendered
        assert "~Hold it open~" in rendered, rendered

    def test_the_selected_highlight_is_unaffected(self):
        rendered = _text(build_options_selected_blocks(CHOICES, [0]))
        assert "*Merge it now*" in rendered, rendered
        assert "~Hold it open~" in rendered, rendered

    def test_the_builder_takes_no_recommendation_at_all(self):
        # The parameter is gone, not merely unused -- a caller cannot reintroduce the
        # marker by passing it.
        params = inspect.signature(build_options_selected_blocks).parameters
        assert "recommended" not in params, sorted(params)


class TestTheBackfillStillThreadsTheValue:
    """Removing the RENDER must not restore the discard at the parse."""

    def _tree(self) -> ast.Module:
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        return ast.parse((src / "dashboard" / "chat_slack.py").read_text(encoding="utf-8"))

    def _calls(self, name: str) -> list[ast.Call]:
        out = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            got = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if got == name:
                out.append(node)
        return out

    def test_the_scan_finds_the_calls_at_all(self):
        # Guards the guard: a rename would make the assertions below vacuous.
        assert self._calls("_post_options"), "no _post_options call found"
        assert self._calls("_split_backfill_options"), "no parse call found"

    def _is_spent(self, call: ast.Call) -> bool:
        return any(
            kw.arg == "interactive"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in call.keywords
        )

    def test_the_interactive_post_forwards_the_value(self):
        live = [c.lineno for c in self._calls("_post_options") if not self._is_spent(c)]
        forwarding = [
            c.lineno
            for c in self._calls("_post_options")
            if any(kw.arg == "recommended" for kw in c.keywords)
        ]
        assert live, "no interactive post found"
        assert sorted(forwarding) == sorted(live), (forwarding, live)

    def test_the_spent_post_forwards_nothing_its_sink_would_drop(self):
        offenders = [
            c.lineno
            for c in self._calls("_post_options")
            if self._is_spent(c) and any(kw.arg == "recommended" for kw in c.keywords)
        ]
        assert offenders == [], f"the spent post is handed a value its sink drops: {offenders}"

    def test_the_newest_turn_parse_still_threads_the_value(self):
        threaded = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "_split_backfill_options":
                continue
            target = node.targets[0]
            assert isinstance(target, ast.Tuple), ast.dump(target)
            third = target.elts[2]
            assert isinstance(third, ast.Name), ast.dump(third)
            if not third.id.startswith("_"):
                threaded.append(node.lineno)
        assert threaded, "no parse threads the recommendation under a real name"

    def test_the_spent_render_is_not_handed_a_recommendation(self):
        misses = [
            call.lineno
            for call in self._calls("build_options_selected_blocks")
            if any(kw.arg == "recommended" for kw in call.keywords)
        ]
        assert misses == [], f"the spent render was handed a recommendation at: {misses}"
