"""A backfilled agent row must restate its recommendation, not silently drop it.

Slack backfill parses the replayed row through its own seam. That seam took the
two-value form of the OPTIONS parse, so the marked label was stripped -- correctly, it
must not be dispatched -- while the value naming WHICH option was marked was discarded
before it could reach the block builder. The live send restates it; the replay did not,
so a marked turn arrived on Slack with the steer removed and nothing standing in for it,
on exactly the flow this grammar exists to serve.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.dashboard.chat_slack import _split_backfill_options
from kiro_crew.slack.format import build_options_blocks

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

MARKED = "Body.\n\n[OPTIONS: (recommended) Merge it now | Hold it]"


def _texts(blocks: list[dict]) -> str:
    # Reads `elements`, not `text`: the restatement rides in a context block, so a
    # top-level `text` read returns '' and the assertion would pass vacuously.
    out = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict) and element.get("type") == "mrkdwn":
                out.append(element.get("text", ""))
            for option in (element.get("options") or []) if isinstance(element, dict) else []:
                out.append(option.get("text", {}).get("text", ""))
    return "\n".join(out)


class TestBackfillKeepsTheRecommendation:
    def test_the_seam_reports_the_marked_label(self):
        body, choices, recommended = _split_backfill_options(
            {"role": "assistant", "content": MARKED}
        )
        assert body == "Body."
        assert choices == ["Merge it now", "Hold it"]
        assert recommended == "Merge it now"

    def test_an_unmarked_row_reports_none(self):
        _body, choices, recommended = _split_backfill_options(
            {"role": "assistant", "content": "Body.\n\n[OPTIONS: Merge it now | Hold it]"}
        )
        assert choices == ["Merge it now", "Hold it"]
        assert recommended is None

    def test_a_user_row_is_still_returned_verbatim(self):
        # A person quoting the syntax must not have choices lifted out of their words.
        body, choices, recommended = _split_backfill_options({"role": "user", "content": MARKED})
        assert body == MARKED
        assert choices == []
        assert recommended is None

    def test_the_restatement_renders_for_a_backfilled_row(self):
        _body, choices, recommended = _split_backfill_options(
            {"role": "assistant", "content": MARKED}
        )
        blocks = build_options_blocks(choices, recommended=recommended)
        rendered = _texts(blocks)
        assert "Recommended:" in rendered, rendered
        assert "Merge it now" in rendered

    def test_the_marker_never_reaches_a_backfilled_button(self):
        _body, choices, _rec = _split_backfill_options({"role": "assistant", "content": MARKED})
        assert all("(recommended)" not in c for c in choices)


class TestTheBackfillSinkIsActuallyWiredToIt:
    """Parsing the value is not the same as posting it.

    The reported defect was the WIRING: the seam can report the marked label while the
    control post drops it, and every block-level assertion above still passes because
    they call the builder directly.
    """

    def _sink_region(self) -> str:
        source = (SRC / "dashboard" / "chat_slack.py").read_text(encoding="utf-8")
        start = source.index("async def _post_options")
        return source[start : start + 2500]

    def test_the_region_is_found_at_all(self):
        # Guards the guard: a moved region would make the assertions below vacuous.
        region = self._sink_region()
        assert "build_options_blocks(" in region
        assert "recommended" in region

    def test_the_sink_accepts_and_forwards_the_recommendation(self):
        region = self._sink_region()
        assert "recommended: str | None = None" in region, "the sink no longer accepts it"
        call = region[region.index("build_options_blocks(") :]
        assert "recommended=recommended" in call[:200], call[:200]

    def test_the_live_call_site_forwards_it(self):
        source = (SRC / "dashboard" / "chat_slack.py").read_text(encoding="utf-8")
        start = source.index("posted_ts = await _post_options")
        assert "recommended=recommended" in source[start : start + 300]
