"""A settings-seed write between the pre-spawn pass and the pin does not refuse the spawn.

The Linux launcher seals the settings-seed record read-only and refuses when the object
at a sealed name is not the one the pre-spawn pass recorded. The record is rewritten
through ``atomic_write`` (a new inode) on every claude-agent-acp seed and release, so
while the record itself was the sealed name, any such write landing in that window made
an unrelated sandboxed spawn fail. These tests run the gateway's write between the
pass and the launcher's seal, and pin that the seal still holds the record and that a
real swap of the sealed name still refuses.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, identity, launch, payload, refusal

from kiro_crew import sandbox, sandbox_launcher_program
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.sandbox_aliases import _referent_identity

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    """A data home of this test's own, and module state no other test sees."""
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(sp, "peek_data_home", lambda: home)
    monkeypatch.setattr(sp, "config_dir", lambda: home)
    monkeypatch.setattr(sp, "_RECORDS", {})
    monkeypatch.setattr(sp, "_LIVE", {})
    monkeypatch.setattr(sp, "_SHARERS", {})
    return home


def _sealed_name_holding_the_record(home: Path) -> Path:
    """The read-only leaf the sandbox seals that the record lives at or under."""
    rel = sp._sidecar_path().relative_to(home).as_posix()
    leaves = [
        leaf for leaf in sandbox._CREW_READONLY_LEAVES if rel == leaf or rel.startswith(leaf + "/")
    ]
    assert leaves, f"no read-only leaf covers {rel}"
    return home / max(leaves, key=len)


def _as_the_pass_records_it(path: Path) -> list[int]:
    """The occupant entry the gateway's pre-spawn pass carries into the launcher."""
    info = os.lstat(path)
    return [
        info.st_dev,
        info.st_ino,
        int(stat.S_ISLNK(info.st_mode)),
        *_referent_identity(str(path), info),
    ]


def _publish() -> None:
    """One sidecar publish, exactly as a seed or a release makes it."""
    with sp._LOCK:
        assert sp._persist() is True


def _seal(tmp_path: Path, sealed: Path, carried: list[int]) -> tuple[str | None, CoveringLibc]:
    libc = CoveringLibc()
    plan = payload(
        readonly_dirs=[str(sealed)],
        required_mask_targets=[str(sealed)],
        mask_occupants={str(sealed): carried},
    )
    run = launch(tmp_path, plan, libc=libc)
    return refusal(sandbox_launcher_program.seal_readonly, run), libc


def test_a_seed_write_after_the_pass_does_not_refuse_the_seal(tmp_path, _isolated) -> None:
    home = _isolated
    _publish()
    sealed = _sealed_name_holding_the_record(home)
    carried = _as_the_pass_records_it(sealed)
    before = identity(sp._sidecar_path())

    _publish()

    assert identity(sp._sidecar_path()) != before, "the write did not replace the sidecar"
    reason, libc = _seal(tmp_path, sealed, carried)
    assert reason is None, f"a routine seed write refused the spawn: {reason}"
    assert identity(sealed) in [call.target_id for call in libc.calls]


def test_a_real_swap_of_the_sealed_name_still_refuses(tmp_path, _isolated) -> None:
    home = _isolated
    _publish()
    sealed = _sealed_name_holding_the_record(home)
    carried = _as_the_pass_records_it(sealed)

    sealed.rename(sealed.with_name(sealed.name + ".moved"))
    sealed.mkdir()

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_record_at_the_old_name_is_neither_trusted_nor_touched(_isolated) -> None:
    """A process started before the upgrade still owns the old file; this one ignores it.

    Copying it would leave two live stores whose leases and revocations cannot see
    each other, so a revoked grant could be adopted later. Ignored, the old record
    answers "not ours": the path is left alone, the safe direction. And the file is
    never rewritten, so no write here replaces the old sealed name under a pin.
    """
    home = _isolated
    legacy = home / "settings_seeds.json"
    work = home / "work" / ".claude" / "settings.local.json"
    work.parent.mkdir(parents=True)
    work.write_text("{}", encoding="utf-8")
    record = {"size": 2, "sha256": sp.digest("{}"), "holders": {"owners": {}, "sharers": {}}}
    legacy.write_text(json.dumps({"seeds": {os.fspath(work): record}}), encoding="utf-8")
    legacy_id, legacy_bytes = identity(legacy), legacy.read_bytes()

    assert sp._read_disk_seeds() == {}
    _publish()

    assert json.loads(sp._sidecar_path().read_text(encoding="utf-8")) == {"seeds": {}}
    assert identity(legacy) == legacy_id and legacy.read_bytes() == legacy_bytes
