"""Whole-file content provenance for the sandbox-sealed keystone config files.

The Linux per-file read-only bind stops sealing ``config.json`` (and the other
protected files) after the gateway's next rename-save, so a prompt-injected agent can
rewrite it directly. ``loosening_provenance`` stores the exact bytes of every gateway
write as the endorsed copy and, where the namespace sandbox seals these files, serves
the endorsed copy whenever the on-disk bytes differ -- covering every key with no key
list. These tests pin: the host-facts-only scope gate, record/serve, a file with
no copy served as written, and the end-to-end substitution through the real load pipeline.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp
directory and every writer/reader below resolves to the same home with no patching.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir


def _config(**agent: object) -> bytes:
    return (json.dumps({"agent": agent}, indent=2) + "\n").encode("utf-8")


#: The real gate, captured at import: the conftest turns it off for every test.
_REAL_ENFORCEMENT_ACTIVE = lp.enforcement_active


def test_enforcement_active_is_false_off_linux(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(lp, "enforcement_active", _REAL_ENFORCEMENT_ACTIVE)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert lp.enforcement_active() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert lp.enforcement_active() is False


def test_reconcile_is_a_noop_when_not_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    lp.record("config.json", _config(sandbox="auto"))
    tampered = _config(sandbox="off")
    assert lp.reconcile("config.json", tampered) == tampered  # on-disk honored


def test_reconcile_serves_endorsed_copy_on_tamper_when_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    safe = _config(sandbox="auto")
    lp.record("config.json", safe)
    assert lp.reconcile("config.json", _config(sandbox="off")) == safe
    assert lp.reconcile("config.json", safe) == safe  # equal -> unchanged


def test_reconcile_returns_disk_when_no_endorsed_copy(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    disk = _config(sandbox="off")
    assert lp.reconcile("config.json", disk) == disk  # pre-first-boot window


def test_endorsed_copy_is_owner_only() -> None:
    import sys

    lp.record("config.json", _config(sandbox="off"))
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits do not apply on Windows")
    assert (lp._endorsed_dir().stat().st_mode & 0o777) == 0o700
    assert ((config_dir() / lp.RECORD_DIR_LEAF).stat().st_mode & 0o777) == 0o700
    assert (lp._endorsed_path("config.json").stat().st_mode & 0o777) == 0o600


def test_a_file_with_no_copy_is_served_as_written_until_the_gateway_saves_it(
    monkeypatch,
) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    path = config_dir() / "config.json"
    path.write_bytes(_config(sandbox="off"))
    assert lp.reconcile("config.json", path.read_bytes()) == _config(sandbox="off")
    assert lp.pending_changes() == []
    with lp.endorsing("config.json", _config(sandbox="auto")):
        path.write_bytes(_config(sandbox="auto"))
    # After the gateway's save a later in-sandbox rewrite is overridden by its copy.
    path.write_bytes(_config(sandbox="off", model="tampered"))
    assert lp.reconcile("config.json", path.read_bytes()) == _config(sandbox="auto")


def test_scope_decision_uses_host_facts_not_the_file(monkeypatch) -> None:
    """The enforce decision reads HOST FACTS only, never the file it is serving.

    If it read ``agent.sandbox`` from the file, writing ``sandbox: off`` would switch
    the substitution off along with the sandbox. Force a Linux host whose namespace
    sandbox can be built, endorse a safe copy, tamper the disk with ``sandbox: off``,
    and confirm the endorsed copy still wins.
    """
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lp, "enforcement_active", _REAL_ENFORCEMENT_ACTIVE)
    monkeypatch.setattr(lp, "_host_verdict", True)
    assert lp.enforcement_active() is True
    lp.record("config.json", _config(sandbox="auto"))
    assert lp.reconcile("config.json", _config(sandbox="off")) == _config(sandbox="auto")


def test_a_reader_inside_the_sandbox_serves_the_endorsed_deny_state(monkeypatch) -> None:
    """Inside the sandbox the namespace calls are refused, which a probe reads as "no
    sandbox here"; the gate must still enforce there, where the agent writes the file."""
    import sys

    from kiro_crew.hook_runtime.denied_commands import load_denied_commands_state

    monkeypatch.setattr(lp, "enforcement_active", _REAL_ENFORCEMENT_ACTIVE)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    endorsed = {"disable_all": False, "disabled_ids": [], "user_added": []}
    lp.record("denied_commands.json", json.dumps(endorsed).encode("utf-8"), strict=True)
    (config_dir() / "denied_commands.json").write_text(
        json.dumps({"disable_all": True}), encoding="utf-8"
    )
    assert load_denied_commands_state() == endorsed


def _special(kind: str, path: Path) -> None:
    if kind == "fifo":
        os.mkfifo(path.parent / "fifo")
        path.symlink_to(path.parent / "fifo")
    else:
        path.symlink_to("/dev/zero")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs and /dev/zero")
@pytest.mark.parametrize("kind", ["fifo", "dev-zero"])
def test_a_leaf_pointed_at_a_special_file_is_never_read(monkeypatch, kind) -> None:
    """A FIFO blocks a bare read and ``/dev/zero`` never ends: each reads as removed."""
    from kiro_crew.hook_runtime.denied_commands import load_denied_commands_state

    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    endorsed = {"disable_all": False}
    lp.record("denied_commands.json", json.dumps(endorsed).encode("utf-8"), strict=True)
    path = config_dir() / "denied_commands.json"
    _special(kind, path)
    results: list[object] = []

    def _reads() -> None:
        results.extend([lp.pending_changes(), load_denied_commands_state()])

    # A blocked read would hang the test; the thread turns that into a named failure.
    reader = threading.Thread(target=_reads, daemon=True)
    reader.start()
    reader.join(60)
    assert not reader.is_alive(), "a protected-file read blocked on a special file"
    [entry] = results[0]
    assert (entry["leaf"], entry["removed"]) == ("denied_commands.json", True)
    assert results[1:] == [endorsed]


@requires_symlinks
def test_a_leaf_symlinked_to_a_regular_file_still_reads(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    target = tmp_path / "kept.json"
    target.write_bytes(_config(sandbox="auto"))
    (config_dir() / "config.json").symlink_to(target)
    assert lp.reconciled_bytes("config.json", config_dir() / "config.json") == target.read_bytes()
    assert lp.pending_changes() == []


def test_a_scan_inside_the_endorsing_window_reports_pending(monkeypatch) -> None:
    """Pins a known race: between the copy write and the rename the scan sees the new
    copy beside the old file and lists the leaf. The next sync acknowledges the note."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    path = config_dir() / "config.json"
    path.write_bytes(_config(sandbox="auto"))
    lp.record("config.json", path.read_bytes(), strict=True)
    new = _config(sandbox="auto", model="next")
    with lp.endorsing("config.json", new):
        assert [p["leaf"] for p in lp.pending_changes()] == ["config.json"]
        path.write_bytes(new)
    assert lp.pending_changes() == []


@pytest.fixture
def _enforcing(monkeypatch):
    """Force the Linux-namespace scope gate on, so the substitution is deterministic."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    yield loader
    loader._invalidate_config_cache()


def _load_data(loader) -> dict:
    loader._invalidate_config_cache()
    return loader.read_config_document().data


def test_tampered_file_loads_the_endorsed_copy_whole(_enforcing) -> None:
    """Real-pipeline proof: a tampered config loads EQUAL to the endorsed copy.

    The tamper flips keys across the whole file -- ``agent.sandbox`` off,
    ``agent.apps_allow_third_party`` on, and ``hooks.auto_approve_tools`` (a key no
    per-key clamp covered) -- and every one is overridden by the endorsed copy.
    """
    loader = _enforcing
    home = config_dir()
    cfg_path = home / "config.json"

    endorsed = {"agent": {"sandbox": "auto"}, "hooks": {}}
    loader.write_config_atomically(cfg_path, endorsed)  # endorses the safe copy
    cfg_path.write_text(
        json.dumps(
            {
                "agent": {"sandbox": "off", "apps_allow_third_party": True},
                "hooks": {"auto_approve_tools": ["rm"]},
            }
        ),
        encoding="utf-8",
    )
    data = _load_data(loader)
    assert data.get("agent", {}).get("sandbox") == "auto"
    assert "apps_allow_third_party" not in data.get("agent", {})
    assert "auto_approve_tools" not in data.get("hooks", {})

    # An ordinary save would publish over the pending edit, so it is refused; the
    # owner's Apply (resolving_pending) is the writer that takes effect.
    with pytest.raises(loader.PendingEditBlocksSave):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert _load_data(loader).get("agent", {}).get("sandbox") == "auto"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}}, resolving_pending=True)
    assert _load_data(loader).get("agent", {}).get("sandbox") == "off"


def test_tampered_file_honored_when_not_enforcing(monkeypatch) -> None:
    """Off Linux / no namespace sandbox: the loader serves the on-disk file as written."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    (config_dir() / "config.json").write_bytes(_config(sandbox="off"))
    try:
        assert loader.read_config_document().data.get("agent", {}).get("sandbox") == "off"
    finally:
        loader._invalidate_config_cache()


# --- Read-modify-write writers start from the endorsed copy (no laundering) ----------


def test_unrelated_config_update_does_not_endorse_a_tampered_file(_enforcing) -> None:
    """A later, unrelated save must not re-endorse what an agent wrote past the seal.

    Nor may it publish over that file: the save is refused before anything is written,
    the endorsed copy is unchanged, and the on-disk edit stays pending for the owner.
    """
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    cfg_path.write_text(json.dumps({"agent": {"sandbox": "off"}}), encoding="utf-8")

    def _patch(doc: dict) -> dict:
        doc.setdefault("ui", {})["theme"] = "dark"
        return doc

    with pytest.raises(loader.PendingEditBlocksSave):
        loader.update_config_locked(cfg_path, mutate=_patch, stamp_meta=False)

    assert json.loads(lp.endorsed_bytes("config.json") or b"{}") == {"agent": {"sandbox": "auto"}}
    assert json.loads(cfg_path.read_bytes()) == {"agent": {"sandbox": "off"}}
    assert _load_data(loader)["agent"]["sandbox"] == "auto"
    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.json"]


def test_denied_commands_save_does_not_endorse_a_tampered_file(_enforcing) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import security

    path = config_dir() / "denied_commands.json"
    asyncio.run(security._write_denied_state(lambda d: d.update({"disable_all": False})))
    path.write_text(json.dumps({"disable_all": True}), encoding="utf-8")

    from kiro_crew.config.loader import PendingEditBlocksSave

    with pytest.raises(PendingEditBlocksSave):
        asyncio.run(security._write_denied_state(lambda d: d.update({"disabled_ids": ["x"]})))

    endorsed = json.loads(lp.endorsed_bytes("denied_commands.json") or b"{}")
    assert endorsed == {"disable_all": False}
    assert json.loads(path.read_bytes()) == {"disable_all": True}


def test_computer_use_save_does_not_endorse_a_tampered_file(_enforcing) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import computer_use

    enable_state.save_state({"enabled": False})
    enable_state.computer_use_state_path().write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )

    from kiro_crew.config.loader import PendingEditBlocksSave

    with pytest.raises(PendingEditBlocksSave):
        computer_use._write_state({"allowed_apps": ["Calculator"]})

    assert enable_state.load_state() == {"enabled": False}
    assert json.loads(enable_state.computer_use_state_path().read_bytes()) == {"enabled": True}


# --- Writers endorse their own bytes, before the publish -----------------------------


def test_writer_endorses_its_own_bytes_not_a_reread(_enforcing, monkeypatch) -> None:
    """Bytes an agent lands on the name right after the rename are never endorsed."""
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    real_write = loader.atomic_write

    def _write_then_race(path, *args, **kwargs):
        real_write(path, *args, **kwargs)
        path.write_text(json.dumps({"agent": {"sandbox": "off"}}), encoding="utf-8")

    # Through the loader facade: atomic_write is shared by its owners, and the
    # mirror lands the patch on loader_document, which publishes the file.
    monkeypatch.setattr(loader, "atomic_write", _write_then_race)
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})

    assert json.loads(lp.endorsed_bytes("config.json") or b"{}") == {"agent": {"sandbox": "auto"}}


# --- A failed record never drops the content check ------------------------------------


def test_record_failure_keeps_the_prior_endorsed_copy(monkeypatch) -> None:
    safe = _config(sandbox="auto")
    lp.record("config.json", safe)

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lp, "atomic_write", _fail)
    lp.record("config.json", _config(sandbox="off"))
    assert lp.endorsed_bytes("config.json") == safe


def test_unendorsable_write_is_not_published_when_enforcing(_enforcing, monkeypatch) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    before = cfg_path.read_bytes()

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lp, "atomic_write", _fail)
    with pytest.raises(OSError):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert cfg_path.read_bytes() == before


def test_failed_publish_restores_the_prior_endorsed_copy(_enforcing, monkeypatch) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    prior = lp.endorsed_bytes("config.json")

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(loader, "atomic_write", _fail)  # facade: the name is shared
    with pytest.raises(OSError):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert lp.endorsed_bytes("config.json") == prior


def test_a_failed_publish_whose_restore_fails_does_not_block_the_next_save(monkeypatch) -> None:
    """The copy file keeps the unpublished bytes, but this process still reads
    the previous copy, so the leaf is not pending and the next save goes through."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    cfg_path = config_dir() / "config.json"
    published = _config(x=2)
    with lp.endorsing("config.json", published):
        cfg_path.write_bytes(published)

    real_write = lp.atomic_write
    copy_dir_broken = [False]

    def _write(*args, **kwargs):
        if copy_dir_broken[0]:
            raise OSError(30, "Read-only file system")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(lp, "atomic_write", _write)
    with pytest.raises(OSError):
        with lp.endorsing("config.json", _config(x=3)):
            copy_dir_broken[0] = True
            raise OSError(28, "No space left on device")
    copy_dir_broken[0] = False

    assert lp._endorsed_path("config.json").read_bytes() == _config(x=3)
    assert lp.endorsed_bytes("config.json") == published
    assert lp.leaf_pending("config.json") is False
    nxt = _config(x=4)
    with lp.endorsing("config.json", nxt):
        cfg_path.write_bytes(nxt)
    assert lp.endorsed_bytes("config.json") == nxt
    assert lp._endorsed_path("config.json").read_bytes() == nxt


def test_a_load_inside_a_failed_publish_does_not_outlive_it(_enforcing, monkeypatch) -> None:
    """A load between the endorse and the failed rename serves the attempted copy and
    caches it under the unchanged on-disk fingerprint; the rollback must drop it."""
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    loader._invalidate_config_cache()
    seen: list[str] = []

    def _load_then_fail(*_a, **_k):
        seen.append(loader.read_config_document().data["agent"]["sandbox"])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(loader, "atomic_write", _load_then_fail)
    with pytest.raises(OSError):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert seen == ["off"]  # the window really served the attempted copy
    assert loader.read_config_document().data["agent"]["sandbox"] == "auto"


def test_unreadable_endorsed_copy_fails_closed_when_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    lp._endorsed_dir().mkdir(parents=True)
    lp._endorsed_path("config.json").mkdir()  # exists, but read_bytes() raises
    with pytest.raises(OSError):
        lp.reconcile("config.json", _config(sandbox="off"))


# --- A symlinked config is endorsed under its logical name ---------------------------


@requires_symlinks
def test_symlinked_config_save_is_endorsed_and_loaded(_enforcing, tmp_path) -> None:
    loader = _enforcing
    target = tmp_path / "dotfiles" / "crew-settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"agent": {"sandbox": "auto"}}), encoding="utf-8")
    cfg_path = config_dir() / "config.json"
    cfg_path.symlink_to(target)

    def _flip(doc: dict) -> dict:
        doc["agent"]["sandbox"] = "off"
        return doc

    loader.update_config_locked(cfg_path, mutate=_flip, stamp_meta=False)

    assert cfg_path.is_symlink()
    assert lp.endorsed_bytes("config.json") == target.read_bytes()
    assert _load_data(loader)["agent"]["sandbox"] == "off"


def test_leaf_for_matches_by_resolved_location(tmp_path) -> None:
    assert lp.leaf_for(config_dir() / "config.json") == "config.json"
    assert lp.leaf_for(tmp_path / "config.json") is None  # same basename, other file


@requires_symlinks
def test_a_looping_overlay_does_not_fail_an_unrelated_read(tmp_path) -> None:
    """``Path.resolve`` raises ``RuntimeError`` on a loop (3.12); one bad leaf is skipped."""
    looped = config_dir() / lp.LEAF_CONFIG_LOCAL
    looped.symlink_to(looped)
    other = tmp_path / "defaults.json"
    other.write_text("{}", encoding="utf-8")
    assert lp.leaf_for(other) is None
    assert lp.leaf_for(config_dir() / lp.LEAF_CONFIG) == lp.LEAF_CONFIG
    assert lp.leaf_for(looped) == lp.LEAF_CONFIG_LOCAL  # still read as its endorsed copy


# --- The endorsed copy is the published bytes, on every platform --------------------


def test_every_endorsing_writer_publishes_the_bytes_it_endorses(_enforcing, monkeypatch) -> None:
    """The writers hand ``atomic_write`` the endorsed bytes themselves, not a ``str`` it
    would encode again: text mode writes ``\\r\\n`` on Windows, and a copy of the ``\\n``
    form would differ from the file, so every load would serve the copy and report the
    gateway's own save as pending."""
    import asyncio

    from kiro_crew import atomic_write as aw
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import security

    loader = _enforcing
    published: dict[str, object] = {}

    def _recording(real):
        def _write(path, content, *args, **kwargs):
            published[lp.leaf_for(Path(path)) or str(path)] = content
            return real(path, content, *args, **kwargs)

        return _write

    monkeypatch.setattr(loader, "atomic_write", _recording(loader.atomic_write))
    monkeypatch.setattr(aw, "atomic_write", _recording(aw.atomic_write))
    monkeypatch.setattr(enable_state, "atomic_write", _recording(enable_state.atomic_write))

    loader.write_config_atomically(config_dir() / "config.json", {"agent": {"sandbox": "auto"}})
    asyncio.run(security._write_denied_state(lambda d: d.update({"disable_all": False})))
    enable_state.save_state({"enabled": False})

    for leaf in ("config.json", "denied_commands.json", "computer_use.json"):
        assert isinstance(published[leaf], bytes), leaf
        assert published[leaf] == lp.endorsed_bytes(leaf) == (config_dir() / leaf).read_bytes()
    assert lp.pending_changes() == []


def test_text_bytes_is_what_text_mode_writes(tmp_path) -> None:
    from kiro_crew.atomic_write import atomic_write, text_bytes

    text = '{\n  "a": 1\n}\n'
    atomic_write(tmp_path / "as_text.json", text)
    assert text_bytes(text) == (tmp_path / "as_text.json").read_bytes()


# --- A missing or unreadable file serves the endorsed copy --------------------------


def _remove(leaf: str) -> None:
    (config_dir() / leaf).unlink()


def test_a_deleted_config_loads_the_endorsed_copy(_enforcing) -> None:
    loader = _enforcing
    for leaf in ("config.json", "config.local.json"):
        loader.write_config_atomically(config_dir() / leaf, {"agent": {"sandbox": "strict"}})
        _remove(leaf)
        assert loader.read_config_for_update(config_dir() / leaf)["agent"]["sandbox"] == "strict"
        assert _load_data(loader)["agent"]["sandbox"] == "strict"
        # Discard of the removal (an owner action) puts the file back.
        loader.write_config_atomically(config_dir() / leaf, {}, resolving_pending=True)


def test_an_undecodable_config_loads_the_endorsed_copy(_enforcing) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "strict"}})
    cfg_path.write_bytes(b'{"agent": {"sandbox": "off\xff"}}')
    assert _load_data(loader)["agent"]["sandbox"] == "strict"
    assert loader.read_config_for_update(cfg_path)["agent"]["sandbox"] == "strict"


def test_a_deleted_denied_commands_file_keeps_the_owner_rules(_enforcing) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import security
    from kiro_crew.hook_runtime.denied_commands import load_denied_commands_state

    rule = {"id": "user-1", "pattern": "^rm -rf", "enabled": True}
    asyncio.run(security._write_denied_state(lambda d: d.update({"user_added": [rule]})))
    _remove("denied_commands.json")
    for read in (load_denied_commands_state, security._read_denied_data):
        assert read()["user_added"] == [rule], read
    assert security._read_denied_strict()["user_added"] == [rule]


def test_a_deleted_computer_use_file_keeps_the_owner_allow_list(_enforcing) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import computer_use

    enable_state.save_state({"enabled": True, "allowed_apps": ["calculator"]})
    _remove("computer_use.json")
    assert enable_state.load_state()["allowed_apps"] == ["calculator"]
    assert computer_use._read_state_strict()["allowed_apps"] == ["calculator"]


def test_a_deleted_file_is_pending_as_removed(_enforcing) -> None:
    _enforcing.write_config_atomically(config_dir() / "config.json", {"a": 1})
    _remove("config.json")
    [entry] = lp.pending_changes()
    assert entry["leaf"] == "config.json"
    assert entry["removed"] is True and entry["signature"] == lp.REMOVED_SIGNATURE
    assert entry["disk_valid"] is False


def test_a_deleted_file_reads_as_absent_when_not_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    from kiro_crew.config import loader

    loader.write_config_atomically(config_dir() / "config.json", {"agent": {"sandbox": "strict"}})
    _remove("config.json")
    assert loader.read_config_for_update(config_dir() / "config.json") == {}
    assert lp.pending_changes() == []


def test_endorsements_restored_on_error_puts_every_copy_back(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    lp.record("config.json", _config(sandbox="strict"))
    with pytest.raises(RuntimeError):
        with lp.endorsements_restored_on_error():
            lp.endorse_installed(config_dir() / "config.json", _config(sandbox="off"))
            lp.endorse_installed(config_dir() / "config.local.json", _config(sandbox="off"))
            raise RuntimeError("restore failed")
    assert lp.endorsed_bytes("config.json") == _config(sandbox="strict")
    assert lp.endorsed_bytes("config.local.json") is None
