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
    for _replaced, _ident, fds in published.values():
        for fd in fds:
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

    The swap runs as soon as the config writer's ``atomic_write`` returns, after the
    gateway's file reached the name: the window a stat of the name would read.
    """
    sealed = _home / "config.json"
    carried = _as_the_pass_records_it(sealed)
    from kiro_crew.config import loader

    real_write = loader.atomic_write
    swapped: list[Path] = []

    def _write_then_swap(path: Path, *args: object, **kwargs: object) -> None:
        real_write(path, *args, **kwargs)
        if Path(path) == sealed and not swapped:
            moved = sealed.with_name("config.json.gateway")
            os.rename(sealed, moved)
            sealed.write_text("{}", encoding="utf-8")
            swapped.append(moved)

    monkeypatch.setattr(loader, "atomic_write", _write_then_swap)
    # Held open so the swapped-in file cannot be handed the original's freed inode
    # number, which would match the pass's identity and never reach the record check.
    with open(sealed, "rb"):
        _gateway_write(_home)
        assert swapped, "the swap did not run"

        reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_only_the_latest_save_is_vouched_for_and_held_open(_home: Path) -> None:
    """The record lists one identity per name, the live one, with a descriptor held on it."""
    for _ in range(3):
        _gateway_write(_home)
    before = identity(_home / "config.json")
    _gateway_write(_home)
    (pdev, pino), (dev, ino), (fd, prev_fd) = wp._PUBLISHED["config.json"]
    info = os.fstat(fd)
    prev = os.fstat(prev_fd)
    assert (prev.st_dev, prev.st_ino) == (pdev, pino)
    assert (info.st_dev, info.st_ino) == (dev, ino) == identity(_home / "config.json")
    assert (pdev, pino) == before
    lines = wp.record_path().read_text(encoding="utf-8").splitlines()
    assert lines[1:] == [f"config.json {pdev} {pino} {dev} {ino}"]


def test_the_replaced_file_is_held_open_so_its_number_is_not_reused(_home: Path) -> None:
    """With the replaced file gone from the name, its inode stays allocated to it."""
    sealed = _home / "config.json"
    _gateway_write(_home)
    (pdev, pino), _ident, fds = wp._PUBLISHED["config.json"]
    assert len(fds) == 2
    for _ in range(20):
        probe = _home / "probe"
        probe.write_text("x", encoding="utf-8")
        assert (probe.stat().st_dev, probe.stat().st_ino) != (pdev, pino)
        probe.unlink()
    assert identity(sealed) != (pdev, pino)


def test_an_older_gateway_file_put_back_at_the_name_still_refuses(
    tmp_path: Path, _home: Path
) -> None:
    """A file the gateway wrote earlier, restored after a later save, is a rollback."""
    sealed = _home / "config.json"
    _gateway_write(_home)
    older = sealed.with_name("config.json.kept")
    os.link(sealed, older)
    _gateway_write(_home)
    carried = _as_the_pass_records_it(sealed)
    os.replace(older, sealed)

    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_save_by_another_process_in_between_is_not_vouched_for(
    tmp_path: Path, _home: Path
) -> None:
    """The record admits the gateway's file only for a pass that saw what it replaced.

    The gateway publishes A; another process (a terminal ``config set``) replaces it with
    B; a later gateway save of the OTHER config file rewrites the record. A pass that sees
    B, then A put back at the name, is a rollback the record does not vouch for.
    """
    sealed = _home / "config.json"
    # Held open so the other process's file cannot be handed the original's freed inode
    # number, which would make it look like the file the gateway's save replaced.
    with open(sealed, "rb"):
        _gateway_write(_home)
        gateway_file = sealed.with_name("config.json.kept")
        os.link(sealed, gateway_file)
        staged = sealed.with_name("config.json.terminal")
        staged.write_text("{}", encoding="utf-8")
        os.replace(staged, sealed)
        loader_document.write_config_atomically(_home / "config.local.json", {"agent": {}})
        carried = _as_the_pass_records_it(sealed)
        os.replace(gateway_file, sealed)

        reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_failed_record_write_withdraws_the_record_before_the_save(
    tmp_path: Path, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that cannot name the new file is removed, so an older file is not vouched."""
    sealed = _home / "config.json"
    _gateway_write(_home)
    older = sealed.with_name("config.json.kept")
    os.link(sealed, older)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(wp, "atomic_write", _refuse)
    _gateway_write(_home)
    assert not wp.record_path().exists()
    assert wp._PUBLISHED == {}

    carried = _as_the_pass_records_it(sealed)
    os.replace(older, sealed)
    reason, _ = _seal(tmp_path, sealed, carried)
    assert reason is not None and "DIFFERENT object" in reason


def test_a_record_that_cannot_be_removed_refuses_the_save(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the record neither rewritten nor removed, the config save itself fails."""
    _gateway_write(_home)
    before = (_home / "config.json").read_bytes()

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    real_unlink = os.unlink

    def _unlink(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)) == wp.record_path():
            raise PermissionError("read-only")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(wp, "atomic_write", _refuse)
    monkeypatch.setattr(wp.os, "unlink", _unlink)
    with pytest.raises(OSError):
        _gateway_write(_home)
    assert (_home / "config.json").read_bytes() == before


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
    assert any(line.endswith(f" {info.st_dev} {info.st_ino}") for line in lines[1:])
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
    assert any(
        line.startswith("config.json ") and line.endswith(f" {info.st_dev} {info.st_ino}")
        for line in lines[1:]
    )
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
