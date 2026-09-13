"""Two seams the recommendation grammar must not break.

1. The producer rule tells an agent to mark its recommended label. A plan action is
   matched VERBATIM by the consumer, and the strip deliberately declines to remove a
   marker from one, so a marked ``Go All`` can never round-trip: it passes validation,
   keeps its marker, and stops being recognised as a plan action. The rule text itself
   therefore has to carry the exemption -- nothing downstream can repair it.

2. Every Slack seam that parses an OPTIONS trailer and then builds blocks must carry the
   recommendation identity to the builder. The two-value wrapper drops it by contract, so
   a caller that uses it renders indistinguishable choices with the recommendation lost.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kiro_crew.constants import _RESERVED_PLAN_ACTIONS, strip_recommended_marker
from kiro_crew.context import _OPTIONS_RECOMMENDED_RULE


def _is_plan_action(label: str) -> bool:
    return label.strip().casefold() in _RESERVED_PLAN_ACTIONS


SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
PLAN_ACTIONS = ["Go", "Go All", "Cancel"]


class TestThePlanActionExemptionIsInTheRule:
    def test_the_rule_names_every_canonical_plan_action(self):
        for action in PLAN_ACTIONS:
            assert f"`{action}`" in _OPTIONS_RECOMMENDED_RULE, action

    def test_the_rule_tells_the_producer_not_to_mark_them(self):
        assert "NEVER mark a plan action" in _OPTIONS_RECOMMENDED_RULE

    def test_an_unmarked_plan_action_still_satisfies_the_consumer(self):
        for action in PLAN_ACTIONS:
            assert _is_plan_action(action), action

    def test_a_marked_plan_action_is_the_hazard_the_rule_now_forbids(self):
        # The strip declines by design, so the marker survives and recognition fails.
        for action in PLAN_ACTIONS:
            marked = f"(recommended) {action}"
            assert strip_recommended_marker(marked) == marked, action
            assert not _is_plan_action(marked), action


class TestTheRecommendationTravelsOnlyFromTheDashboard:
    """A source-level ratchet, now two-sided.

    The marker is instructed on the dashboard alone, so a channel-origin turn carrying one is
    model drift. Restating it there would give drift protocol standing -- the spec's own words --
    so those seams must pass nothing, while the dashboard seams must still carry the identity.
    """

    SEAM_FILES = [
        "dashboard/handlers/messaging.py",
        "dashboard/chat_slack.py",
        "slack/gateway.py",
        "slack/handler.py",
        "slack/renderer.py",
    ]
    DASHBOARD_SEAMS = ["dashboard/handlers/messaging.py", "dashboard/chat_slack.py"]
    CHANNEL_SEAMS = ["slack/gateway.py", "slack/renderer.py"]
    # The Slack handler is in neither list: its one build sits inside the shared footer helper,
    # whose recommendation each caller chooses, so the arm below pins the callers instead.

    def _bare_calls(self, rel: str) -> list[str]:
        out = []
        for i, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])extract_options\(", line):
                out.append(f"{rel}:{i}")
        return out

    def _builds(self, rel: str) -> list[ast.Call]:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "build_options_blocks":
                out.append(node)
        return out

    def test_the_scan_finds_a_known_instance(self):
        # Guards the guard: the pattern must still be able to match a real call, so a
        # rename cannot make the seam scan below pass by matching nothing.
        text = (SRC / "slack" / "format.py").read_text(encoding="utf-8")
        assert re.search(r"(?<![\w.])extract_options_with_recommendation\(", text)

    def test_the_narrow_two_value_parse_no_longer_exists(self):
        # Stronger than policing call sites: with no two-value spelling in the module, a
        # seam cannot select it, so the deny scan below cannot be evaded by a new caller.
        text = (SRC / "slack" / "format.py").read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])def extract_options\(", text)

    def test_no_seam_uses_the_two_value_parse(self):
        offenders = [c for rel in self.SEAM_FILES for c in self._bare_calls(rel)]
        assert offenders == [], f"these seams drop the recommendation: {offenders}"

    def test_every_dashboard_block_build_passes_recommended(self):
        misses = [
            f"{rel}:{n.lineno}"
            for rel in self.DASHBOARD_SEAMS
            for n in self._builds(rel)
            if not any(kw.arg == "recommended" for kw in n.keywords)
        ]
        assert misses == [], f"these dashboard block builds drop the recommendation: {misses}"

    def test_no_channel_block_build_restates_the_recommendation(self):
        offenders = [
            f"{rel}:{n.lineno}"
            for rel in self.CHANNEL_SEAMS
            for n in self._builds(rel)
            if any(kw.arg == "recommended" for kw in n.keywords)
        ]
        assert offenders == [], f"these channel seams restate model drift: {offenders}"

    def test_the_channel_footer_helper_takes_no_recommendation(self):
        """The shared footer helper has no recommendation parameter to pass.

        A channel seam cannot restate a marker it has no way to supply, so pin the
        absence at the signature rather than at each caller's argument list.
        """
        tree = ast.parse((SRC / "slack/handler.py").read_text(encoding="utf-8"))
        sigs = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_append_footer_actions"
        ]
        assert len(sigs) == 1, f"expected one helper definition, saw {len(sigs)}"
        names = {a.arg for a in sigs[0].args.args} | {a.arg for a in sigs[0].args.kwonlyargs}
        assert "staleness_token" in names, f"scan reads the wrong signature: {sorted(names)}"
        assert "recommended" not in names, "the channel footer helper takes a recommendation"

    def test_the_channel_footer_callers_supply_no_recommendation(self):
        """Both channel callers exist and pass nothing beyond the token."""
        checked = 0
        offenders = []
        for rel in ("slack/handler.py", "slack/renderer.py"):
            tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "_append_footer_actions":
                    continue
                checked += 1
                if len(node.args) > 6 or any(k.arg == "recommended" for k in node.keywords):
                    offenders.append(f"{rel}:{node.lineno}")
        assert checked == 2, f"expected both channel footer callers, saw {checked}"
        assert offenders == [], f"these callers pass a recommendation: {offenders}"

    def test_the_ast_scan_finds_the_calls_at_all(self):
        # Guards the guard: a renamed builder would make the assertions above vacuous.
        found = sum(len(self._builds(rel)) for rel in self.SEAM_FILES)
        assert found >= 4, f"expected the known seam builds, found {found}"
