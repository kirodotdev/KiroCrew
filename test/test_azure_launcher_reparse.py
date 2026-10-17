"""What a Windows ``.cmd`` launcher actually does to an argument, measured.

``azure_client._ARGUMENT_ALLOWED_CHARACTERS`` and
``azure_client._reject_percent_that_is_not_encoding`` are a security boundary: the Azure
CLI installs as ``az.cmd``, ``CreateProcess`` runs a ``.cmd`` through ``%COMSPEC%``, and
that processor RE-PARSES the command line, so a character inside an ARGUMENT can be read
as syntax instead of as data. A claim about such a character justified by argument rather
than observation is a claim nobody can check, including its author -- so this measures
each one.

These measurements are EVIDENCE, not the enforcement path. Enforcement is
``azure_client._ARGUMENT_ALLOWED_CHARACTERS``, an allowlist that refuses everything a
legitimate value cannot contain, measured or not; ``test_azure_transport.py`` asserts
each character measured here falls outside it, so a widened allowlist fails there rather
than here.

It lives in the top-level tree, not in the app's own ``tests/`` directory, because it
SPAWNS a process: every spawn under ``src/kiro_crew`` must be routed through the sandbox
or registered in ``test_spawn_audit.py``'s allowlist, and a test's own probe spawn
qualifies for neither. Registering one to make this file pass would be telling a
security gate something false.

What is spawned is a ``.cmd`` this test writes itself, which hands its arguments to this
interpreter -- never the real ``az``. What is asserted is therefore what a CHILD of the
launcher receives: exactly the position ``az.exe`` occupies.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest


def _child_argv(tmp: str, value: str) -> list[str] | None:
    """Spawn a self-written ``.cmd`` with *value* and return the child's argv."""
    dumper = os.path.join(tmp, "dumper.py")
    with open(dumper, "w", encoding="utf-8") as fh:
        fh.write(
            "import json, sys\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
            "    json.dump(sys.argv[2:], f)\n"
        )
    out_file = os.path.join(tmp, "argv.json")
    if os.path.exists(out_file):
        os.unlink(out_file)
    launcher = os.path.join(tmp, "probe.cmd")
    with open(launcher, "w", encoding="utf-8") as fh:
        # No ``setlocal enabledelayedexpansion``: a stock installer launcher.
        fh.write("@echo off\r\n")
        fh.write(f'"{sys.executable}" "{dumper}" "{out_file}" %*\r\n')
    # cwd=tmp is load-bearing, not tidiness. The ``a>out`` case exists precisely
    # because the launcher HONOURS a redirection inside an argument, so without this
    # the redirect lands in the inherited working directory -- the checkout -- and
    # leaves a stray file behind. The measurement is the same either way; where the
    # side effect goes is not.
    subprocess.run(  # noqa: S603 - fixed argv, no shell, a launcher this test wrote
        [launcher, value], capture_output=True, timeout=120, check=False, cwd=tmp
    )
    if not os.path.exists(out_file):
        return None
    with open(out_file, encoding="utf-8") as fh:
        return list(json.load(fh))


@unittest.skipUnless(os.name == "nt", "needs a Windows command processor")
class ReparsedLauncherMeasured(unittest.TestCase):
    """Each refusal, and each deliberate non-refusal, against a real launcher."""

    def test_an_ordinary_argument_survives(self):
        """Control: a failure below is the metacharacter, not the harness."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_child_argv(tmp, "abc123"), ["abc123"])

    def test_separators_truncate_the_argument(self):
        """Why ``&``, ``|`` and ``>`` are refused: the remainder becomes syntax."""
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("a&whoami", "a|whoami", "a>out"):
                self.assertEqual(_child_argv(tmp, value), ["a"], f"{value!r} must truncate")

    def test_the_caret_is_consumed_as_an_escape(self):
        """Why ``^`` is refused: it vanishes rather than arriving."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_child_argv(tmp, "a^b"), ["ab"])

    def test_a_variable_reference_is_expanded(self):
        """Why a percent cannot simply be allowed.

        ``%NAME%`` is substituted with the value of a gateway environment variable,
        which also splits one argument into several wherever that value holds a space.
        """
        with tempfile.TemporaryDirectory() as tmp:
            got = _child_argv(tmp, "a%PATH%b")
            self.assertIsNotNone(got)
            self.assertNotEqual(got, ["a%PATH%b"], "%VAR% must be observed expanding")

    def test_a_percent_encoded_octet_is_inert(self):
        """Why a percent cannot simply be refused.

        ``_SEGMENT_RE`` admits a space, so ``_org_url`` emits ``%20`` for a supported
        organization name -- and the launcher passes it through untouched. This pair
        with the test above is the whole justification for judging a percent by whether
        it opens an encoded octet rather than by its presence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _child_argv(tmp, "https://dev.azure.com/My%20Org"),
                ["https://dev.azure.com/My%20Org"],
            )

    def test_the_bang_is_inert_without_delayed_expansion(self):
        """``!`` is inert HERE, and that is precisely why it is refused anyway.

        Delayed expansion is off in a stock launcher, so this arrives intact. That
        observation is a property of this HOST
        (``Software\\Microsoft\\Command Processor\\DelayedExpansion``), not of the
        launcher: with it enabled, the same argument arrives as the expanded ``PATH``
        split across dozens of argv elements. Recorded as the trap it is -- a
        measurement that would have justified allowing the character.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_child_argv(tmp, "a!PATH!b"), ["a!PATH!b"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
