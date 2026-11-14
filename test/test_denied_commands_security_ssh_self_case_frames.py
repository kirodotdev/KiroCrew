"""Cased-vs-lowered frame pairing for the ssh-to-self floor.

Split out of ``test_denied_commands_security_ssh_self.py`` (which reached the 2,000-line
cap) as a coherent group: the floor lowercases a line before its argv walk and then pairs
each lowered frame with a cased twin so a case-sensitive option (``sudo -H`` vs ``-h``,
``sudo -E`` vs ``-e``) is read in the case it was written. These pin that the pairing does
not desync when the two walks differ in length, on the same ``sandbox-escape-ssh-self``
rule and the same fixtures as the parent module.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _denied_by

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestSshSelfCaseFrames:
    """The cased/lowered frame pairing stays in step when the walks differ in length."""

    _RULE = "sandbox-escape-ssh-self"

    def test_a_cased_substitution_does_not_desync_the_ssh_payload_frame(self):
        # r25 (opus): a cased product name in a substitution body (a which-lookup of it)
        # binds on the lowercased walk but not the case-sensitive cased walk, so the two
        # frame lists differed in length. The ssh floor then dropped EVERY cased frame and
        # read ``sudo -H`` (no value) as its lowercase twin ``-h`` (takes a value), which
        # shifted the program word off ``s?h`` and ALLOWED the self-connection. The floor
        # now pairs frames by content, so the payload's cased twin stays matched and the
        # command is refused, as on main. A mutant restoring the length-gated positional
        # zip re-allows it and fails here.
        cmd = "F=$(which KiroCrew); echo $F; bash -c 'sudo -H s?h local* id'"  # brand-ok
        assert _denied_by(cmd) == self._RULE, cmd

    def test_a_deduped_lowered_frame_is_judged_under_each_cased_twin(self):
        # r26 (opus): two self-ssh payloads that differ only in sudo option case de-dup
        # to one lowered frame while the cased walk keeps both distinct twins. The floor
        # judges the frame under EACH distinct cased twin and refuses if ANY refuses.
        # Leaving an ambiguous frame as None was fail-OPEN where the lowercase option
        # takes a value: `sudo -H` (no value) read as `-h` (takes a value) shifts the
        # program word off `s?h` and allows the self-connection. Both orderings of
        # `-H`/`-h` and of `-E`/`-e` must refuse, in either order. A mutant that leaves an
        # ambiguous twin None (or picks one twin) re-allows one ordering of each pair.
        reproducing = [
            "bash -c 'sudo -H s?h local* id' && bash -c 'sudo -h s?h local* id'",
            "bash -c 'sudo -h s?h local* id' && bash -c 'sudo -H s?h local* id'",
            "bash -c 'sudo -E s?h local* id' && bash -c 'sudo -e s?h local* id'",
            "bash -c 'sudo -e s?h local* id' && bash -c 'sudo -E s?h local* id'",
            "bash -c 'sudo -E s?h local* id'; bash -c 'sudo -e s?h local* id';",
            # The exact slot-comment spelling is refused here and on main too: its
            # trailing punctuation keeps the two frames distinct, so no de-dup occurs.
            "bash -c 'sudo -E s?h local* id'; bash -c 'sudo -e s?h local* id'",
        ]
        for cmd in reproducing:
            assert _denied_by(cmd) == self._RULE, cmd
