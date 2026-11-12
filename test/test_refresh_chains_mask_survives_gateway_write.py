"""A refresh-token save between the pre-spawn pass and the mask does not refuse the spawn.

The Linux launcher masks ``refresh_chains.json`` and refuses when the object at the name
is not the one the pre-spawn pass recorded. The gateway rewrites the file on every token
rotation (staged temp + rename, a new inode), so a rotation landing between the pass and
the mask made an unrelated sandboxed spawn -- often a script cron -- fail with "a
DIFFERENT object holds that name".

These tests run the gateway's own refresh-state save in that window, and pin that the mask
still lands on the live file and that a rewrite the gateway did not make still refuses.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, identity, launch, payload, refusal

from kiro_crew import sandbox_launcher_program
from kiro_crew.config import write_provenance as wp
from kiro_crew.dashboard.refresh_tokens import RefreshStateManager
from kiro_crew.sandbox_aliases import _referent_identity

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    published: dict = {}
    monkeypatch.setattr(wp, "_PUBLISHED", published)
    (home / "refresh_chains.json").write_text("{}", encoding="utf-8")
    os.chmod(home / "refresh_chains.json", 0o600)
    yield home
    for run in published.values():
        for _replaced, _ident, fds in run:
            for fd in fds:
                os.close(fd)


def _as_the_pass_records_it(path: Path) -> list[int]:
    info = os.lstat(path)
    return [
        info.st_dev,
        info.st_ino,
        int(stat.S_ISLNK(info.st_mode)),
        *_referent_identity(str(path), info),
    ]


def _mask(tmp_path: Path, masked: Path, carried: list[int]) -> str | None:
    record, nonce, names = wp.launcher_record()
    plan = payload(
        sensitive_files=[str(masked)],
        required_mask_targets=[str(masked)],
        mask_occupants={str(masked): carried},
        published_file_record={"record": record, "nonce": nonce, "names": list(names)},
    )
    run = launch(tmp_path, plan, libc=CoveringLibc())
    return refusal(sandbox_launcher_program.mask_sensitive_files, run)


def test_a_token_rotation_after_the_pass_does_not_refuse_the_mask(
    tmp_path: Path, _home: Path
) -> None:
    masked = _home / "refresh_chains.json"
    carried = _as_the_pass_records_it(masked)
    before = identity(masked)

    # The rotation lands between the pass (above) and the mask (below).
    RefreshStateManager(state_path=masked).revoke_chain("chain-1", 4_102_444_800.0)

    assert identity(masked) != before, "the save did not replace refresh_chains.json"
    reason = _mask(tmp_path, masked, carried)
    assert reason is None, f"a routine token rotation refused the spawn: {reason}"


def test_a_rewrite_the_gateway_did_not_make_still_refuses(tmp_path: Path, _home: Path) -> None:
    masked = _home / "refresh_chains.json"
    carried = _as_the_pass_records_it(masked)
    other = masked.with_name("refresh_chains.json.other")
    other.write_text("{}", encoding="utf-8")
    os.replace(other, masked)

    reason = _mask(tmp_path, masked, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_swap_after_a_gateway_save_still_refuses(tmp_path: Path, _home: Path) -> None:
    """A vouched identity covers that one object, not whatever replaces it later."""
    masked = _home / "refresh_chains.json"
    keep = os.open(masked, os.O_RDONLY)  # stop the old inode number being reused
    try:
        carried = _as_the_pass_records_it(masked)
        RefreshStateManager(state_path=masked).revoke_chain("chain-1", 4_102_444_800.0)
        other = masked.with_name("refresh_chains.json.other")
        other.write_text("{}", encoding="utf-8")
        os.replace(other, masked)

        reason = _mask(tmp_path, masked, carried)
    finally:
        os.close(keep)
    assert reason is not None and "DIFFERENT object" in reason
