"""An on-disk ``config.json`` written by an older Kiro Crew is adopted, not held pending.

A release from before the endorsed copy saves ``config.json`` without updating it, so
after an upgrade the file differs from its copy. When its writer stamp
(``meta.lastTouchedVersion``) names a release older than the running build, the file
becomes the new endorsed copy: nothing is pending and the next save goes ahead. Any
other stamp (this release, a newer one, missing, unparseable) keeps the file pending,
as does a change to any key outside the short allow list, every other protected leaf and a
read inside a sandbox. A save stopped between its endorsement and its rename is taken
back at the next start, from a note only the gateway can write.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp
directory and every writer and reader below resolves to it with no patching.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

import kiro_crew
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir

#: The running build every test pretends to be.
_RUNNING = "0.13.0.2"


@pytest.fixture
def _enforcing(monkeypatch):
    """Force the scope gate on and pin the running version, so verdicts are fixed."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    monkeypatch.setattr(kiro_crew, "__version__", _RUNNING)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    yield loader
    loader._invalidate_config_cache()


@pytest.fixture
def _sel_events(monkeypatch) -> list[dict[str, Any]]:
    """The security event log rows written during the test."""
    events: list[dict[str, Any]] = []

    class _Log:
        def log_api_access(self, **fields: Any) -> None:
            events.append(fields)

    import kiro_crew.sel as sel_module

    monkeypatch.setattr(sel_module, "sel", lambda: _Log())
    return events


def _older_build_writes(doc: dict) -> bytes:
    """Write *doc* to ``config.json`` the way a Kiro Crew writer stores it; return the bytes."""
    content = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    (config_dir() / "config.json").write_bytes(content)
    return content


def _stamped(stamp: Any) -> dict:
    return {
        "meta": {"lastTouchedVersion": stamp},
        "agent": {"sandbox": "auto"},
        "telemetry": {"enabled": False},
        "dashboard": {"theme_mode": "dark"},
    }


def _endorse_first(loader) -> bytes:
    loader.write_config_atomically(config_dir() / "config.json", {"agent": {"sandbox": "auto"}})
    endorsed = lp.endorsed_bytes("config.json")
    assert endorsed is not None
    return endorsed


def _theme(doc: dict) -> dict:
    doc.setdefault("ui", {})["theme"] = "dark"
    return doc


def test_an_older_writer_is_adopted_and_the_next_save_succeeds(
    _enforcing, _sel_events, caplog
) -> None:
    loader = _enforcing
    _endorse_first(loader)
    disk = _older_build_writes(_stamped("0.12.2.6"))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        assert lp.pending_changes() == []

    assert lp.endorsed_bytes("config.json") == disk
    adoptions = [r for r in caplog.records if "Kiro Crew wrote it (writer:" in r.getMessage()]
    assert len(adoptions) == 1 and "0.12.2.6" in adoptions[0].getMessage()
    assert [(e["operation"], e["resources"]) for e in _sel_events] == [
        (lp.OP_ADOPT_KNOWN_WRITER, "config.json=writer:0.12.2.6")
    ]

    loader.update_config_locked(config_dir() / "config.json", mutate=_theme, stamp_meta=False)
    written = (config_dir() / "config.json").read_bytes()
    assert json.loads(written)["ui"] == {"theme": "dark"}
    assert json.loads(written)["telemetry"] == {"enabled": False}
    assert lp.endorsed_bytes("config.json") == written
    assert lp.pending_changes() == []


def test_a_load_adopts_an_older_writer_and_serves_its_document(_enforcing, _sel_events) -> None:
    loader = _enforcing
    _endorse_first(loader)
    disk = _older_build_writes(_stamped("0.12.2.6"))

    loader._invalidate_config_cache()
    assert loader.read_config_document().data.get("dashboard") == {"theme_mode": "dark"}
    assert lp.endorsed_bytes("config.json") == disk


def test_a_save_refusal_check_adopts_an_older_writer(_enforcing, _sel_events) -> None:
    loader = _enforcing
    _endorse_first(loader)
    disk = _older_build_writes(_stamped("0.12.2.6"))

    lp.refuse_save_if_pending("config.json")  # does not raise
    assert lp.endorsed_bytes("config.json") == disk


@pytest.mark.parametrize(
    "stamp",
    ["0.13.0.2", "0.13.0.9", "0.14.0", "1.0.0-insider.1", None, "garbage", "0.1", 12],
    ids=[
        "equal",
        "same-release",
        "newer",
        "newer-prerelease",
        "missing",
        "garbage",
        "short",
        "int",
    ],
)
def test_any_other_stamp_stays_pending(_enforcing, _sel_events, stamp) -> None:
    loader = _enforcing
    endorsed = _endorse_first(loader)
    doc = _stamped(stamp) if stamp is not None else {"agent": {"sandbox": "off"}}
    _older_build_writes(doc)

    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.json"]
    with pytest.raises(loader.PendingEditBlocksSave):
        loader.update_config_locked(config_dir() / "config.json", mutate=_theme, stamp_meta=False)
    assert lp.endorsed_bytes("config.json") == endorsed
    assert _sel_events == []


def test_a_file_not_stored_as_kiro_crew_writes_one_stays_pending(_enforcing, _sel_events) -> None:
    loader = _enforcing
    endorsed = _endorse_first(loader)
    content = json.dumps(_stamped("0.12.2.6"), indent=2).replace("\n", "\r\n").encode("utf-8")
    (config_dir() / "config.json").write_bytes(content)

    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.json"]
    assert lp.endorsed_bytes("config.json") == endorsed


def test_another_leaf_with_an_older_stamp_stays_pending(_enforcing, _sel_events) -> None:
    lp.record("config.local.json", b'{"agent": {}}\n')
    content = (json.dumps(_stamped("0.12.2.6"), indent=2) + "\n").encode("utf-8")
    (config_dir() / "config.local.json").write_bytes(content)

    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.local.json"]
    assert lp.endorsed_bytes("config.local.json") == b'{"agent": {}}\n'


def test_inside_a_sandbox_nothing_is_adopted(_enforcing, _sel_events, monkeypatch) -> None:
    from kiro_crew import sandbox_launch_mark

    loader = _enforcing
    endorsed = _endorse_first(loader)
    _older_build_writes(_stamped("0.12.2.6"))
    monkeypatch.setattr(sandbox_launch_mark, "launched_in_sandbox", lambda: True)

    assert lp.adopt_known_writer("config.json") is None
    assert lp.leaf_pending("config.json") is True
    assert lp.endorsed_bytes("config.json") == endorsed
    assert _sel_events == []


def test_adoption_waits_out_another_writer_instead_of_racing_its_save(
    _enforcing, _sel_events
) -> None:
    """While another thread holds the writers' lock, the older file is not adopted.

    A save endorses its new bytes before it renames them into place; an adoption
    in that window would re-endorse the older file it is replacing.
    """
    import threading

    from kiro_crew.config.loader_document import _config_write_lock

    loader = _enforcing
    endorsed = _endorse_first(loader)
    _older_build_writes(_stamped("0.12.2.6"))
    holding = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        with _config_write_lock(config_dir() / "config.json"):
            holding.set()
            release.wait(timeout=30)

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        assert holding.wait(timeout=30)
        assert lp.adopt_known_writer("config.json") is None
        assert lp.endorsed_bytes("config.json") == endorsed
    finally:
        release.set()
        t.join(timeout=30)
    assert not t.is_alive()
    assert lp.adopt_known_writer("config.json") is not None


def test_holding_another_files_lock_does_not_count_as_holding_this_one(
    _enforcing, _sel_events
) -> None:
    """This thread inside config.local.json's write, another inside config.json's: skip."""
    import threading

    from kiro_crew.config.loader_document import _config_write_lock

    loader = _enforcing
    endorsed = _endorse_first(loader)
    _older_build_writes(_stamped("0.12.2.6"))
    holding = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        with _config_write_lock(config_dir() / "config.json"):
            holding.set()
            release.wait(timeout=30)

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        assert holding.wait(timeout=30)
        with _config_write_lock(config_dir() / "config.local.json"):
            assert lp.adopt_known_writer("config.json") is None
        assert lp.endorsed_bytes("config.json") == endorsed
    finally:
        release.set()
        t.join(timeout=30)
    assert not t.is_alive()


def test_adoption_inside_this_threads_own_locked_write_goes_ahead(_enforcing, _sel_events) -> None:
    from kiro_crew.config.loader_document import _config_write_lock

    loader = _enforcing
    _endorse_first(loader)
    disk = _older_build_writes(_stamped("0.12.2.6"))
    with _config_write_lock(config_dir() / "config.json"):
        assert lp.adopt_known_writer("config.json") == disk
    assert lp.endorsed_bytes("config.json") == disk


@pytest.mark.parametrize(
    "loosened",
    [
        {"agent": {"sandbox": "off"}},
        {"agent": {"sandbox": "auto"}, "hooks": {"auto_approve_tools": ["*"]}},
        {"agent": {"sandbox": "auto", "yolo": True}},
        {"agent": {"sandbox": "auto", "env": {"X": "1"}}},
        {},
        {"agent": {"sandbox": "auto", "apps_trusted": ["evil"]}},
        {"agent": {"sandbox": "auto", "sandbox_readonly_paths": []}},
        {"agent": {"sandbox": "auto"}, "hooks": {"auto_approve_sources": ["*"]}},
        {"agent": {"sandbox": "auto"}, "mcpServers": {"x": {"command": "sh"}}},
        {"agent": {"sandbox": "auto"}, "telemetry": {"otlp_endpoint": "https://e.example"}},
        {"agent": {"sandbox": "auto"}, "dashboard": {"browser_local_origins": ["x"]}},
        {"agent": {"sandbox": "auto"}, "telemetry": {"enabled": True}},
    ],
    ids=[
        "sandbox-off",
        "auto-approve",
        "yolo",
        "env",
        "agent-removed",
        "apps-trusted",
        "readonly-paths",
        "approve-sources",
        "new-section",
        "telemetry-endpoint",
        "dashboard-other-key",
        "usage-data-on",
    ],
)
def test_an_older_stamp_carries_only_the_listed_keys(_enforcing, _sel_events, loosened) -> None:
    """The stamp is text in the file, so a change to any other key stays pending."""
    loader = _enforcing
    endorsed = _endorse_first(loader)
    _older_build_writes({"meta": {"lastTouchedVersion": "0.12.2.6"}, **loosened})

    assert lp.adopt_known_writer("config.json") is None
    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.json"]
    assert lp.endorsed_bytes("config.json") == endorsed
    assert _sel_events == []


_NEXT = b'{"agent": {"sandbox": "auto"}, "ui": {"x": 2}}\n'


def _stop_between_endorsement_and_rename(before: bytes) -> None:
    """A save's first half, then the process is gone: no handler of endorsing() runs."""
    lp._mark_inflight("config.json", before)
    lp.record("config.json", _NEXT, strict=True)
    assert (config_dir() / "config.json").read_bytes() == before


def test_a_save_stopped_mid_way_is_taken_back_at_the_next_start(_enforcing, _sel_events) -> None:
    """A crash, kill, restart or update mid-save leaves the version endorsed before."""
    loader = _enforcing
    before = _endorse_first(loader)
    _stop_between_endorsement_and_rename(before)
    assert lp.leaf_pending("config.json") is True  # until the next start

    lp.adopt_unendorsed()

    assert lp.leaf_pending("config.json") is False
    assert lp.endorsed_bytes("config.json") == before
    assert [(e["operation"], e["resources"]) for e in _sel_events] == [
        (lp.OP_ADOPT_KNOWN_WRITER, "config.json=interrupted-save")
    ]
    assert not lp._inflight_path("config.json").exists()
    loader.update_config_locked(mutate=_theme)
    assert lp.leaf_pending("config.json") is False


def test_another_file_beside_a_stopped_save_stays_pending(_enforcing, _sel_events) -> None:
    """The note names only the version endorsed before; any other file is an outside edit."""
    loader = _enforcing
    before = _endorse_first(loader)
    _stop_between_endorsement_and_rename(before)
    _older_build_writes({"agent": {"sandbox": "off"}})

    lp.adopt_unendorsed()

    assert lp.leaf_pending("config.json") is True
    assert lp.endorsed_bytes("config.json") == _NEXT
    assert _sel_events == []
    assert not lp._inflight_path("config.json").exists()


def test_the_note_never_outlives_one_start(_enforcing, _sel_events) -> None:
    """A note left by a save that did finish is dropped, so a later replay stays pending."""
    loader = _enforcing
    before = _endorse_first(loader)
    lp._mark_inflight("config.json", before)
    loader.update_config_locked(mutate=_theme)  # a later save that finished
    lp._mark_inflight("config.json", before)  # a note a stop after the rename left

    lp.adopt_unendorsed()
    assert not lp._inflight_path("config.json").exists()
    (config_dir() / "config.json").write_bytes(before)
    lp.adopt_unendorsed()

    assert lp.leaf_pending("config.json") is True
    assert _sel_events == []


def test_a_finished_or_failed_save_leaves_no_note(_enforcing, monkeypatch) -> None:
    loader = _enforcing
    path = config_dir() / "config.json"
    _endorse_first(loader)
    with lp.endorsing("config.json", _NEXT):
        assert lp._inflight_path("config.json").exists()
        path.write_bytes(_NEXT)
    assert not lp._inflight_path("config.json").exists()

    with pytest.raises(OSError):
        with lp.endorsing("config.json", b'{"agent": {}}\n'):
            raise OSError("rename failed")
    assert not lp._inflight_path("config.json").exists()
    assert lp.endorsed_bytes("config.json") == _NEXT


def test_a_start_beside_a_live_save_leaves_its_note_alone(_enforcing, _sel_events) -> None:
    """Another writer holds the lock mid-save: the note is its own, so nothing is touched."""
    import threading

    from kiro_crew.config.loader_document import _config_write_lock

    loader = _enforcing
    before = _endorse_first(loader)
    _stop_between_endorsement_and_rename(before)
    holding = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        with _config_write_lock(config_dir() / "config.json"):
            holding.set()
            release.wait(timeout=30)

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        assert holding.wait(timeout=30)
        lp.adopt_interrupted_save()
        assert lp._inflight_path("config.json").exists()
        assert lp.endorsed_bytes("config.json") == _NEXT
    finally:
        release.set()
        t.join(timeout=30)
    assert not t.is_alive()

    lp.adopt_interrupted_save()
    assert lp.endorsed_bytes("config.json") == before
    assert not lp._inflight_path("config.json").exists()


def test_a_recovery_that_cannot_be_written_keeps_its_note(
    _enforcing, _sel_events, monkeypatch
) -> None:
    loader = _enforcing
    before = _endorse_first(loader)
    _stop_between_endorsement_and_rename(before)
    real_write = lp.atomic_write

    def _failing_write(path, data, **kwargs):
        if path == lp._endorsed_path("config.json"):
            raise OSError("disk full")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(lp, "atomic_write", _failing_write)
    lp.adopt_interrupted_save()
    assert lp._inflight_path("config.json").exists()
    monkeypatch.setattr(lp, "atomic_write", real_write)

    lp.adopt_interrupted_save()
    assert lp.endorsed_bytes("config.json") == before
    assert not lp._inflight_path("config.json").exists()
