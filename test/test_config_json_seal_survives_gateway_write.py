"""A gateway write to ``config.json`` between the pre-spawn pass and the pin does not refuse.

The Linux launcher seals ``config.json`` read-only and refuses when the object at a
sealed name is not the one the pre-spawn pass recorded. The gateway itself rewrites the
file through ``write_config_atomically`` (temp + rename, a new inode) -- the boot meta
stamp, load-time migrations, settings saves -- so any such write landing between the
pass and the pin made an unrelated sandboxed spawn fail. On a freshly provisioned host
that is the first minutes after start, when the MCP probe, app registry clones and the
first-run writers all run at once.

These tests run the gateway's own config writer between the pass and the launcher's seal,
and pin that the seal still lands on the live file, that an unrecorded write still
refuses, and that a real swap of the name still refuses.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, identity, launch, payload, refusal

from kiro_crew import sandbox_launcher_program
from kiro_crew.config import loader_document
from kiro_crew.config import write_provenance as wp
from kiro_crew.sandbox_aliases import _referent_identity

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    published: dict = {}
    monkeypatch.setattr(wp, "_PUBLISHED", published)
    (home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
    yield home
    for ids in published.values():
        for _ident, fd in ids:
            os.close(fd)


def _as_the_pass_records_it(path: Path) -> list[int]:
    """The occupant entry the gateway's pre-spawn pass carries into the launcher."""
    info = os.lstat(path)
    return [
        info.st_dev,
        info.st_ino,
        int(stat.S_ISLNK(info.st_mode)),
        *_referent_identity(str(path), info),
    ]


def _gateway_write(home: Path) -> None:
    """One ``config.json`` save, exactly as the gateway's writers make it."""
    loader_document.update_config_locked(
        home / "config.json", mutate=lambda data: {**data, "touched": True}
    )


def _vouched() -> dict[str, object]:
    """The record descriptor the plan hands the launcher, as ``namespace_payload`` emits it."""
    record, nonce, names = wp.launcher_record()
    return {"record": record, "nonce": nonce, "names": list(names)}


def _seal(tmp_path: Path, sealed: Path, carried: list[int]) -> tuple[str | None, CoveringLibc]:
    libc = CoveringLibc()
    plan = payload(
        readonly_dirs=[str(sealed)],
        required_mask_targets=[str(sealed)],
        mask_occupants={str(sealed): carried},
        published_file_record=_vouched(),
    )
    run = launch(tmp_path, plan, libc=libc)
    return refusal(sandbox_launcher_program.seal_readonly, run), libc


def test_a_gateway_config_write_after_the_pass_does_not_refuse_the_seal(
    tmp_path: Path, _home: Path
) -> None:
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    before = identity(sealed)

    _gateway_write(_home)

    assert identity(sealed) != before, "the write did not replace config.json"
    reason, libc = _seal(tmp_path, sealed, carried)
    assert reason is None, f"a routine config write refused the spawn: {reason}"
    assert identity(sealed) in [call.target_id for call in libc.calls]


def test_the_next_spawn_after_a_write_is_not_refused_either(tmp_path: Path, _home: Path) -> None:
    """The expectation is per spawn: a pass taken after the write carries the new file."""
    sealed = _home / "config.json"
    _gateway_write(_home)
    reason, _ = _seal(tmp_path, sealed, _as_the_pass_records_it(sealed))
    assert reason is None


def test_a_rewrite_the_gateway_did_not_publish_still_refuses(tmp_path: Path, _home: Path) -> None:
    """Same bytes, new inode, but not through the gateway's writer: refused as before."""
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    staged = sealed.with_name("config.json.other")
    staged.write_bytes(sealed.read_bytes())
    os.replace(staged, sealed)

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_real_swap_of_config_json_still_refuses(tmp_path: Path, _home: Path) -> None:
    """The original moved aside and another file put at the name is the substitution."""
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    sealed.rename(sealed.with_name("config.json.moved"))
    sealed.write_text("{}", encoding="utf-8")

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_swap_after_a_gateway_write_still_refuses(tmp_path: Path, _home: Path) -> None:
    """A published identity vouches for that one object, not for whatever replaces it.

    The original file is held open for the whole test. The gateway write unlinks it, and a
    filesystem may hand a freed inode number to the next file created -- the swapped-in one
    -- which would then carry the very identity the pass recorded and never reach the
    comparison this test is about.
    """
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    with open(sealed, "rb"):
        _gateway_write(_home)
        sealed.rename(sealed.with_name("config.json.moved"))
        sealed.write_text("{}", encoding="utf-8")

        reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_file_swapped_in_right_after_the_publish_is_not_vouched_for(
    tmp_path: Path, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record names the object the writer staged, not whatever holds the name later.

    The swap runs inside the publishing rename, after the gateway's file reaches the name
    and before the writer returns, which is the window a stat of the name would read.
    """
    from kiro_crew import atomic_write as aw

    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    real_replace = aw.replace_with_retry
    swapped: list[Path] = []

    def _replace_then_swap(src: Path | str, dst: Path | str) -> None:
        real_replace(src, dst)
        if Path(dst) == sealed and not swapped:
            moved = sealed.with_name("config.json.gateway")
            os.rename(sealed, moved)
            sealed.write_text("{}", encoding="utf-8")
            swapped.append(moved)

    monkeypatch.setattr(aw, "replace_with_retry", _replace_then_swap)
    _gateway_write(_home)
    assert swapped, "the swap did not run"

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_every_vouched_identity_is_held_open(_home: Path) -> None:
    """A held descriptor keeps each listed inode allocated, so its number is not reused."""
    for _ in range(wp._HISTORY + 2):
        _gateway_write(_home)
    held = wp._PUBLISHED["config.json"]
    assert len(held) == wp._HISTORY
    for (dev, ino), fd in held:
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) == (dev, ino)
    assert identity(_home / "config.json") == held[-1][0]


def test_a_failed_record_write_keeps_every_listed_identity_held(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin is released only once the record on disk omits its identity."""
    for _ in range(wp._HISTORY):
        _gateway_write(_home)
    on_disk = wp.record_path().read_text(encoding="utf-8")
    before = list(wp._PUBLISHED["config.json"])

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(wp, "atomic_write", _refuse)
    _gateway_write(_home)

    assert wp.record_path().read_text(encoding="utf-8") == on_disk
    assert list(wp._PUBLISHED["config.json"]) == before
    for (dev, ino), fd in before:
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) == (dev, ino)


def test_a_save_through_a_symlinked_config_is_vouched_for(tmp_path: Path, _home: Path) -> None:
    """The gateway writes a symlinked config through to its referent; the record names it."""
    referent = tmp_path / "elsewhere" / "config.json"
    referent.parent.mkdir()
    (_home / "config.json").rename(referent)
    (_home / "config.json").symlink_to(referent)
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)

    # The config writers resolve the link before writing, so this is the path they pass.
    loader_document.update_config_locked(referent, mutate=lambda data: {**data, "touched": True})

    info = os.stat(referent)
    lines = wp.record_path().read_text(encoding="utf-8").splitlines()
    assert f"config.json {info.st_dev} {info.st_ino}" in lines[1:]
    if sys.platform.startswith("linux"):
        # Pinning a link without following it needs O_PATH.
        reason, _ = _seal(tmp_path, sealed, carried)
        assert reason is None, f"a save through the symlink refused the spawn: {reason}"


def test_a_record_from_another_gateway_process_vouches_for_nothing(
    tmp_path: Path, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record whose nonce is not this plan's -- a previous process, or a forgery -- is ignored."""
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    monkeypatch.setattr(wp, "_NONCE", "0" * 32)
    _gateway_write(_home)
    monkeypatch.setattr(wp, "_NONCE", "f" * 32)

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_name_the_plan_does_not_vouch_for_is_refused_after_a_write(
    tmp_path: Path, _home: Path
) -> None:
    """The record answers only for the names the plan lists, never for another sealed file."""
    other = _home / "security_policy.json"
    other.write_text("{}", encoding="utf-8")
    carried = _as_the_pass_records_it(other)
    _gateway_write(_home)
    staged = other.with_name("security_policy.json.tmp")
    staged.write_text("{}", encoding="utf-8")
    os.replace(staged, other)

    reason, _ = _seal(tmp_path, other, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_the_writer_records_only_files_under_the_data_home(_home: Path, tmp_path: Path) -> None:
    """A config written outside the data home is not this gateway's sealed name."""
    elsewhere = tmp_path / "elsewhere" / "config.json"
    elsewhere.parent.mkdir()
    loader_document.write_config_atomically(elsewhere, {"agent": {}})
    assert not wp.record_path().exists()

    _gateway_write(_home)
    lines = wp.record_path().read_text(encoding="utf-8").splitlines()
    info = os.stat(_home / "config.json")
    assert lines[0] == wp._NONCE
    assert f"config.json {info.st_dev} {info.st_ino}" in lines[1:]
    assert stat.S_IMODE(os.stat(wp.record_path()).st_mode) == 0o600


def test_the_rendered_launcher_carries_the_record_descriptor(_home: Path) -> None:
    """The plan hands the launcher the record, this process's nonce and the vouched names."""
    from test_sandbox_launcher_program import rendered_payload

    from kiro_crew.sandbox import _build_launcher_script

    script = _build_launcher_script("standard", published_file_record=wp.launcher_record())
    record = rendered_payload(script)["published_file_record"]
    assert record["record"] == str(_home / "config-provenance" / "published")
    assert record["nonce"] == wp._NONCE
    assert set(record["names"]) == {str(_home / "config.json"), str(_home / "config.local.json")}


def test_a_real_spawn_plan_carries_the_record_and_the_config_identity(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``namespace_argv`` records ``config.json`` and hands the launcher the record with it."""
    from test_sandbox_launcher_program import rendered_payload

    from kiro_crew import sandbox_argv

    monkeypatch.setattr(sandbox_argv, "_resolve_agent_executable", lambda executable: executable)
    monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)
    argv = sandbox_argv.namespace_argv(["/bin/true"], "standard")
    script = next(arg for arg in argv if arg.endswith(".py"))
    try:
        plan = rendered_payload(Path(script).read_text(encoding="utf-8"))
    finally:
        os.unlink(script)
    config = str(_home / "config.json")
    assert plan["mask_occupants"][config][:2] == list(identity(_home / "config.json"))
    assert plan["published_file_record"]["nonce"] == wp._NONCE
    assert config in plan["published_file_record"]["names"]
