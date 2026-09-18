"""The pinned proof of concept for security audit finding F5.

This file exists at this exact path, with this exact test name, because the
audit ledger's acceptance gate re-runs the finding's proof by its nodeid:
``test/test_s5_boundary_poc.py::test_s5_1_record_owner_must_not_follow_a_symlink``.
Renaming either would make the gate read UNSETTLED rather than pass, so neither
is a free choice. It carries ONE test, F5's; the other findings on that audit
surface are other changes' proofs and are deliberately absent.

The durable regression suite for this invariant is
``test/test_secc_poc_scratch_owner_symlink.py``, which covers both affected
modules, the managed root, and the answers that must NOT become refusals. This
file is narrower on purpose: it is the auditor's original demonstration, kept
verbatim in shape so that what the gate re-runs is the proof that was filed.

``record_owner`` runs in the GATEWAY process after the child has been resumed
(``acp/client.py`` -> ``finish_suspended_spawn``, then ``record_owner``; twin in
``acp/runtime.py``). The child owns that directory by design -- it is its own
``TMPDIR``/``KIROCREW_SCRATCH`` (``agent_scratch.scratch_env``) -- so it can
replace the regular ``.owner`` file with a symlink. ``Path.write_text`` opened
``O_WRONLY|O_CREAT|O_TRUNC`` with no ``lstat`` and no ``O_NOFOLLOW``, so the
unsandboxed gateway truncated whatever the link named.

Nothing here touches a real credential path, a live gateway, a pod, or the
network: the case builds its own directories under ``tmp_path``, and the
boundary function is exercised in-process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import agent_scratch


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
def test_s5_1_record_owner_must_not_follow_a_symlink(tmp_path: Path) -> None:
    scratch = tmp_path / "sess-deadbeef"
    scratch.mkdir()
    owner = scratch / agent_scratch.OWNER_FILENAME
    owner.write_text(str(os.getpid()), encoding="utf-8")  # what allocate_scratch writes

    victim = tmp_path / "victim.json"
    victim.write_text("KEEP-ME")

    # The child, running with $KIROCREW_SCRATCH == scratch, swaps its own marker.
    owner.unlink()
    owner.symlink_to(victim)

    agent_scratch.record_owner(scratch, 4242)

    assert victim.read_text() == "KEEP-ME", (
        "record_owner truncated the symlink target: a sandboxed child redirected "
        "an unsandboxed gateway write to an arbitrary path"
    )
