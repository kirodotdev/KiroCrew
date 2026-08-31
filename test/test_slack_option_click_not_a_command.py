"""An OPTIONS click sends MODEL-AUTHORED text, so it must not be run as a command.

The Slack option callbacks dispatch the label the model wrote. A leading command token
there is ordinary turn content: interpreting it would let a reply arrange its own
privilege escalation (``!yolo`` turns on process-wide auto-approval) by writing an
option the user only has to click. Telegram and Discord already pass
``interpret_commands=False`` on their callback paths; these pin the Slack parity.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from kiro_crew.slack import interactions
from kiro_crew.slack.handler import handle_message

# Tokens a model could put at the front of an option label. `!yolo` is the finding's
# example; the rest are the other command families the same region reaches.
MODEL_AUTHORED = ["!yolo on", "!agent atlas", "!compact", "!dashboard", "status", "sessions"]


def _top_level_ifs(func) -> list[str]:
    """``if`` statements at the function body's own indentation.

    Only these need the flag: a branch nested inside one is already dominated by its
    parent's condition, so requiring it there would fail on correct code.
    """
    body = inspect.getsource(func)
    return [
        line.strip()
        for line in body.splitlines()
        if line.startswith("    if ") and not line.startswith("     ")
    ]


class TestTheSlackHandlerCanRefuseCommandInterpretation:
    def test_handle_message_accepts_the_flag(self):
        params = inspect.signature(handle_message).parameters
        assert "interpret_commands" in params
        assert params["interpret_commands"].default is True

    def test_the_scan_finds_branches_at_all(self):
        # Guards the guard: an empty scan would make every assertion below vacuous.
        assert len(_top_level_ifs(handle_message)) > 5

    def test_the_decision_is_computed_once(self):
        """No branch may re-derive it: the flag is read once and every branch reads that.

        Structural, not textual: the local's NAME is not asserted, so renaming it or
        reformatting passes, while a future branch that re-reads the PARAMETER
        (``if interpret_commands and ...``) fails -- which is the shape that matters.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(handle_message)))
        fn = tree.body[0]
        reads_in_assign = 0
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id == "interpret_commands":
                    reads_in_assign += 1
        assert reads_in_assign == 1, (
            f"the flag is assigned to a local {reads_in_assign} times -- expected exactly "
            "one, so every branch reads the same decision"
        )
        for stripped in _top_level_ifs(handle_message):
            assert "interpret_commands" not in stripped, stripped

    @pytest.mark.parametrize("token", MODEL_AUTHORED)
    def test_every_reachable_command_branch_tests_the_flag(self, token):
        """Every branch reaching a model-authorable token must gate on the derived local.

        The local is resolved from the AST rather than spelled here, so a rename passes
        and only an UNGATED branch fails.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(handle_message)))
        local = next(
            node.targets[0].id
            for node in ast.walk(tree.body[0])
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id == "interpret_commands"
            and isinstance(node.targets[0], ast.Name)
        )
        word = token.split()[0]
        matched = 0
        for stripped in _top_level_ifs(handle_message):
            reaches = f'"{word}"' in stripped or (
                word.startswith("!") and 'startswith("!")' in stripped
            )
            if reaches:
                matched += 1
                assert local in stripped, stripped
        assert matched, f"no top-level branch reaches {word!r} -- did the region move?"


class TestTheOptionCallbacksPassIt:
    def test_every_option_dispatch_disables_interpretation(self):
        source = inspect.getsource(interactions)
        blocks = source.split("handle_message(")[1:]
        assert blocks, "no handle_message call found -- did the module move?"
        option_calls = [b for b in blocks if "asker_key=_asker_key" in b[:1200]]
        assert option_calls, "no option-click dispatch found"
        for block in option_calls:
            assert "interpret_commands=False" in block[:1200]


class TestEveryModelAuthoredDispatchInTheModule:
    """Filtering on ``asker_key`` cannot see the sibling ``action::`` route.

    That route dispatches a CLICKED BUTTON's text, which an agent authored into the
    OPTIONS control, so the same rationale applies -- but it carries no ``asker_key``
    and the option-only scan above is blind to it. This scan looks at every
    ``handle_message`` call in the module and pins the count, so a NEW dispatch site
    cannot be added without someone classifying it here.
    """

    # The two dispatches whose text is NOT model-authored: the forwarded message the user
    # picked, and the fixed revision template the product owns.
    USER_AUTHORED_MARKERS = ("dm_channel", "revision_prompt")

    def _calls(self) -> list[str]:
        """Argument text of each real ``handle_message(`` call.

        Split on the bare call opening a line, so the module's own prose mention of
        ``handle_message()`` is not counted as a dispatch site.
        """
        source = inspect.getsource(interactions)
        return [chunk[:1500] for chunk in source.split("handle_message(\n")[1:]]

    def test_the_scan_finds_the_dispatch_sites(self):
        # Vacuity guard: an empty scan would satisfy every assertion below. Shape rather
        # than count, so an unrelated refactor adding a site fails classification instead.
        calls = self._calls()
        assert calls, "no handle_message dispatch sites found -- re-anchor this scan"
        assert any("interpret_commands=False" in c for c in calls), "no gated site found"

    def test_the_action_route_disables_interpretation(self):
        action_calls = [c for c in self._calls() if "action_context=action_context" in c]
        assert action_calls, "no action_context dispatch found -- re-anchor this scan"
        for call in action_calls:
            if any(m in call for m in self.USER_AUTHORED_MARKERS):
                continue
            assert "interpret_commands=False" in call, " ".join(call[:200].split())

    def test_only_the_classified_sites_interpret_commands(self):
        ungated = [c for c in self._calls() if "interpret_commands=False" not in c]
        unclassified = [c for c in ungated if not any(m in c for m in self.USER_AUTHORED_MARKERS)]
        assert not unclassified, (
            f"{len(unclassified)} dispatch site(s) interpret commands with no user- or "
            "product-authored provenance: "
            + "; ".join(" ".join(c[:120].split()) for c in unclassified)
        )
