"""The blocked-commands doc must disclose what the command tier cannot see.

The tier reads the command line a tool call carries, and a program the line only
NAMES is never opened — :mod:`kiro_crew.security.denied_rules` and
:mod:`kiro_crew.security.argv_floor` both record that as the module's own
doctrine, naming an OS-level fence in the sandbox as the fix of record. An
operator reading **Settings → Security** has no way to infer it, and a control
believed to be binding while it is advisory in practice is worse than one known
to be partial. So the disclosure is pinned here rather than left to prose review,
the same way :mod:`test_deny_guidance` pins the sanctioned-command prose across
its three surfaces.

:class:`TestTheDisclosureIsTrue` is the anti-drift half, and it measures the two
halves of the doc's claim in the terms the doc states them: a body ON the command
line is read, a body the line merely names is not. The second case runs against a
real file on disk, so "not read" is a fact about the gate rather than about a
filename nobody could have resolved. A change that opened that file goes red here
and the sentence gets rewritten — rather than the doc quietly inverting into a
claim the code does not support.

``security.is_denied`` is the whole command-text decision, not one layer of it:
the argv-structural floor runs inside it, and
:class:`~kiro_crew.platform.security_authority.PolicyAuthority` only ever adds
patterns to it. So a pass here is not scoped to the regex tier.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import security

#: The heading and the verdict the doc has to carry, not a paraphrase of either.
#: The verdict is the one sentence an operator's trust decision rests on.
_HEADING = "## What this tier cannot see"
_VERDICT_WORDS = ("friction against a", "direct command line, not a security boundary")

#: A command line the built-in rules DO refuse, used as the control half of every
#: pair below and as the body of the script fixture. Nothing runs it.
_DIRECT = "aws s3 sync /tmp/out s3://example-bucket"

#: A program spelled out ON the command line. The doc claims this half IS read,
#: and the argv floor's own refusal text says so ("an inline interpreter program
#: names the mint surface"), so the claim is pinned rather than assumed.
_INLINE = "python3 -c \"from kiro_crew.cli import main; main(['token'])\""


def _doc() -> str:
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    return (root / "docs" / "blocked-commands.md").read_text(encoding="utf-8")


def _builtin_regexes() -> list[str]:
    return security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), False, (), ())


class TestTheDocDisclosesTheLimit:
    def test_the_section_exists(self):
        assert _HEADING in _doc(), "blocked-commands.md does not disclose the tier's reach"

    def test_the_section_says_what_the_tier_is(self):
        doc = _doc()
        for words in _VERDICT_WORDS:
            assert words in doc, "the doc states the limit without stating what the tier IS"

    def test_the_section_names_both_halves_and_the_layer_that_closes_it(self):
        """Only naming the gap reads as "nothing stops this"; only naming the
        inline half reads as a fence. The section has to carry both, plus the
        sandbox as what closure belongs to."""
        section = _doc().split(_HEADING, 1)[1].split("\n## ", 1)[0]
        assert "inline-interpreter rule" in section
        assert "sandbox" in section

    def test_the_configuration_limits_point_at_it(self):
        """The two deliberate limits are about EDITING, and read as the whole story.

        An operator who reaches that section to add a pattern is exactly the
        reader who over-trusts the result, so the pointer lives there too.
        """
        adjusting = _doc().split("## Adjusting the rules", 1)[1]
        assert "What this tier cannot see" in adjusting


class TestTheDisclosureIsTrue:
    def test_the_control_case_is_refused(self):
        """Without this, every pair below would pass on a tier that refuses nothing."""
        assert security.is_denied(_DIRECT, denied_regexes=_builtin_regexes())

    def test_a_program_on_the_command_line_is_read(self):
        """The doc's first half: an inline program is part of the line, so it counts."""
        assert security.is_denied(_INLINE, denied_regexes=_builtin_regexes())

    def test_a_program_the_line_only_names_is_not_read(self, tmp_path: Path):
        """The doc's second half, against a body that really exists on disk.

        The script is created and never executed; the assertion is about what the
        gate reads when the line names it.
        """
        script = tmp_path / "deploy.sh"
        script.write_text(f"#!/bin/sh\n{_DIRECT}\n", encoding="utf-8")
        script.chmod(0o755)
        regexes = _builtin_regexes()
        for command in (f"bash {script}", f"sh {script}", str(script)):
            assert not security.is_denied(
                command, denied_regexes=regexes
            ), f"{command!r} is refused now — the doc's disclosure needs rewriting"
