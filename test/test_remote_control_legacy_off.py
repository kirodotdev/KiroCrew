"""A host turned off on an older build stays off after the upgrade.

Every ``config.json`` stores ``tunnel.relay_enabled: false`` (the schema is saved in
full), so on disk a host turned off before this release and a fresh install look the
same: ``relay_enabled: false``, a lease credential, no ``tunnel.relay_turned_off``.
Only the first load by this build can tell them apart. These tests drive the real
loader against the test's own ``KIROCREW_HOME``, so whatever records that first load
lives there too.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.loader_locations import config_path
from kiro_crew.edition_locks import relay_selected
from kiro_crew.remote_control import setting

KEY = "secret://crew-key"


def _write(**tunnel: Any) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    return path


def _stored() -> dict:
    return json.loads(config_path().read_text(encoding="utf-8"))["tunnel"]


def _hosts() -> bool:
    return relay_selected(KiroCrewConfig.load())


def test_a_host_turned_off_before_the_upgrade_stays_off() -> None:
    # What an older build's ``kirocrew remote-control off`` left behind.
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is False
    # And on every later load, not only the first.
    assert _hosts() is False


def test_remote_control_on_clears_the_legacy_off() -> None:
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is False
    setting.turn_on()
    assert _hosts() is True
    # A later load by the same build never turns it off again.
    KiroCrewConfig.load().save()
    assert _hosts() is True


def test_a_key_set_after_the_upgrade_starts_from_the_key_alone() -> None:
    # A fresh install's first load by this build, before any key is set.
    _write(relay_enabled=False)
    assert _hosts() is False
    # The key-only setup: the one config set line saves the whole schema, so the
    # stored relay_enabled is false again, with the key beside it.
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is True


def test_the_off_marker_wins_over_the_key() -> None:
    _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off=True)
    assert _hosts() is False


def test_a_personal_sign_in_never_starts_hosting() -> None:
    _write(relay_enabled=False, relay_lease_credential="")
    assert _hosts() is False
    _write(relay_enabled=False, relay_lease_credential="kiro-sign-in")
    assert _hosts() is False


def test_a_host_that_was_on_keeps_hosting_and_gets_no_marker() -> None:
    _write(relay_enabled=True, relay_lease_credential=KEY)
    assert _hosts() is True
    assert _stored().get("relay_turned_off", False) is False


def test_off_after_the_upgrade_still_wins() -> None:
    _write(relay_enabled=False)
    assert _hosts() is False
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is True
    setting.turn_off()
    assert _hosts() is False
    assert _stored()["relay_turned_off"] is True


def _ledger() -> dict:
    # The raw read the one-shot decides from: it keeps a check recorded as False.
    from kiro_crew.config.superseded_defaults import adopted_superseded_if_readable

    ledger = adopted_superseded_if_readable()
    assert ledger is not None
    return ledger


def test_the_check_is_recorded_on_the_first_load_even_when_nothing_applied() -> None:
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    _write(relay_enabled=False)
    KiroCrewConfig.load()
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is False
    # So a key set later, even in a document that names no marker, is never turned off.
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is True
    assert "relay_turned_off" not in _stored()


def test_the_key_only_setup_on_a_fresh_home_ends_hosting() -> None:
    # First start: this build writes the whole schema, relay_turned_off included.
    KiroCrewConfig.load().save()
    assert _hosts() is False
    # Settings > Secrets stores the key elsewhere; the one config set line names it.
    cfg = KiroCrewConfig.load()
    cfg.tunnel.relay_lease_credential = KEY
    cfg.save()
    # The restart: a fresh load.
    assert _hosts() is True


def test_a_downgrade_then_upgrade_never_runs_the_check_again() -> None:
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is False
    setting.turn_on()
    assert _hosts() is True
    # An older build rewrites the document without the field it does not know.
    tunnel = {k: v for k, v in _stored().items() if k != "relay_turned_off"}
    tunnel["relay_enabled"] = False
    _write(**tunnel)
    # Back on this build: the ledger already names the check.
    assert _hosts() is True
    assert "relay_turned_off" not in _stored()


def test_the_owner_of_an_adopted_host_is_told_to_run_remote_control_on(caplog) -> None:
    from kiro_crew.config.superseded_defaults import (
        RELAY_TURNED_OFF_ADOPTION_KEY,
        RELAY_TURNED_OFF_NOTICE,
        adoption_summary,
    )

    _write(relay_enabled=False, relay_lease_credential=KEY)
    with caplog.at_level("WARNING", logger="kiro_crew.config.loader"):
        KiroCrewConfig.load()
    assert RELAY_TURNED_OFF_NOTICE in caplog.text
    assert "kirocrew remote-control on" in RELAY_TURNED_OFF_NOTICE
    # doctor and `kirocrew config defaults` replay it from the ledger.
    assert RELAY_TURNED_OFF_NOTICE in adoption_summary(RELAY_TURNED_OFF_ADOPTION_KEY, True)


def test_a_seeded_pod_gets_the_marker_once() -> None:
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    # What the pod seed floor writes: the marker beside relay_enabled false.
    _write(relay_enabled=False, relay_turned_off=True, relay_lease_credential=KEY)
    before = _stored()
    assert _hosts() is False
    # Nothing to write for this check: it is recorded as not applied, and the
    # tunnel section is exactly what the floor wrote.
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is False
    assert _stored() == before


def test_a_failed_marker_write_does_not_crash_the_load_and_stays_off(caplog, monkeypatch) -> None:
    from kiro_crew.config import loader as config_loader
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    _write(relay_enabled=False, relay_lease_credential=KEY)

    def _refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    # The write seam itself, so the refusal is the same on every platform.
    with monkeypatch.context() as patch:
        patch.setattr(config_loader, "_persist_config_migration", _refuse)
        with caplog.at_level("INFO", logger="kiro_crew.config.loader"):
            assert _hosts() is False
            # A later load in the same process is still off, cache hit or not.
            assert _hosts() is False
    # Not recorded, so the next load that reads the file (a later process, or any
    # config write) decides again and writes the marker.
    assert RELAY_TURNED_OFF_ADOPTION_KEY not in _ledger()
    assert "Config write-back failed" in caplog.text or "not recorded yet" in caplog.text
    config_loader._invalidate_config_cache()
    assert _hosts() is False
    assert _stored()["relay_turned_off"] is True


def test_two_loads_at_once_write_the_marker_once() -> None:
    import threading

    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    _write(relay_enabled=False, relay_lease_credential=KEY)
    barrier = threading.Barrier(2, timeout=FALLBACK_TIMEOUT_SECS)
    results: list[bool] = []

    def _load() -> None:
        barrier.wait()
        results.append(relay_selected(KiroCrewConfig.load()))

    threads = [threading.Thread(target=_load, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=FALLBACK_TIMEOUT_SECS)
    assert not any(t.is_alive() for t in threads), "a load did not finish in time"
    # Neither load hosts, whichever of them wrote.
    assert results == [False, False]
    assert _stored()["relay_turned_off"] is True
    after = config_path().read_bytes()
    # A later load finds the check recorded (or the marker present) and writes nothing.
    assert _hosts() is False
    assert config_path().read_bytes() == after
    assert _ledger().get(RELAY_TURNED_OFF_ADOPTION_KEY) in (True, False)


def test_a_key_held_only_in_the_overlay_also_stays_off() -> None:
    from kiro_crew.config.loader_locations import config_local_path

    _write(relay_enabled=False)
    config_local_path().write_text(
        json.dumps({"tunnel": {"relay_lease_credential": KEY}}), encoding="utf-8"
    )
    assert _hosts() is False
    # The marker is written to config.json, the file this build owns.
    assert _stored()["relay_turned_off"] is True


def test_an_overlay_off_over_a_base_on_lands_the_marker_and_records_the_check() -> None:
    from kiro_crew.config.loader_locations import config_local_path
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    _write(relay_enabled=True, relay_lease_credential=KEY)
    config_local_path().write_text(
        json.dumps({"tunnel": {"relay_enabled": False}}), encoding="utf-8"
    )
    assert _hosts() is False
    # The marker lands and the check is recorded, so later loads neither retry the
    # write nor drop the config cache.
    assert _stored()["relay_turned_off"] is True
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is True
    assert _hosts() is False


def test_remote_control_on_during_the_overlay_off_marker_write_wins(monkeypatch) -> None:
    from kiro_crew.config import loader as config_loader
    from kiro_crew.config.loader_locations import config_local_path

    _write(relay_enabled=True, relay_lease_credential=KEY)
    config_local_path().write_text(
        json.dumps({"tunnel": {"relay_enabled": False}}), encoding="utf-8"
    )
    original = config_loader._persist_config_migration
    fired: list[bool] = []

    def _on_first(*args, **kwargs):
        # The real turn-on write lands between this load's read and its locked write.
        if not fired:
            fired.append(True)
            setting.turn_on()
        return original(*args, **kwargs)

    monkeypatch.setattr(config_loader, "_persist_config_migration", _on_first)
    KiroCrewConfig.load()
    assert fired == [True]
    # turn_on stores relay_turned_off false beside relay_enabled true; it is not overwritten.
    assert _stored()["relay_turned_off"] is False
    monkeypatch.setattr(config_loader, "_persist_config_migration", original)
    config_loader._invalidate_config_cache()
    assert _hosts() is True


def test_hosting_turned_on_during_the_marker_write_wins(monkeypatch) -> None:
    from kiro_crew.config import loader as config_loader

    _write(relay_enabled=False, relay_lease_credential=KEY)
    original = config_loader._persist_config_migration
    fired: list[bool] = []

    def _on_first(*args, **kwargs):
        # Between this load's read and its locked write, another writer stores
        # relay_enabled true (a config set: no marker beside it).
        if not fired:
            fired.append(True)
            _write(relay_enabled=True, relay_lease_credential=KEY)
        return original(*args, **kwargs)

    monkeypatch.setattr(config_loader, "_persist_config_migration", _on_first)
    KiroCrewConfig.load()
    assert fired == [True]
    assert _stored()["relay_enabled"] is True
    assert "relay_turned_off" not in _stored()
    monkeypatch.setattr(config_loader, "_persist_config_migration", original)
    assert _hosts() is True


def test_a_check_that_changed_nothing_shows_nowhere_and_never_runs_again(capsys) -> None:
    from kiro_crew.config.superseded_defaults import (
        RELAY_TURNED_OFF_ADOPTION_KEY,
        adopted_superseded,
        render_doctor_section,
    )

    _write(relay_enabled=False)
    KiroCrewConfig.load()
    # The one-shot's own read keeps the record...
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is False
    # ...while doctor and config defaults, which list adopted_superseded(), show nothing.
    assert RELAY_TURNED_OFF_ADOPTION_KEY not in adopted_superseded()
    render_doctor_section([])
    assert "relay_turned_off" not in capsys.readouterr().out
    # And the check does not run again: a key set now hosts, no marker written.
    _write(relay_enabled=False, relay_lease_credential=KEY)
    assert _hosts() is True
    assert "relay_turned_off" not in _stored()


def test_a_marker_write_that_keeps_failing_drops_the_cache_once_and_stays_off(
    monkeypatch,
) -> None:
    from kiro_crew.config import loader as config_loader

    _write(relay_enabled=False, relay_lease_credential=KEY)

    def _read_only(*_args, **_kwargs):
        raise PermissionError(30, "Read-only file system")

    drops: list[int] = []
    invalidate = config_loader._invalidate_config_cache

    def _counting_invalidate(*args, **kwargs):
        drops.append(1)
        return invalidate(*args, **kwargs)

    monkeypatch.setattr(config_loader, "_persist_config_migration", _read_only)
    monkeypatch.setattr(config_loader, "_invalidate_config_cache", _counting_invalidate)
    for _ in range(4):
        assert _hosts() is False
    # One retry for a contended lock, then the cache holds: no re-read on every load.
    assert len(drops) == 1
    assert "relay_turned_off" not in _stored()


def test_a_hand_edited_string_marker_keeps_the_host_off_after_the_check() -> None:
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    _write(relay_enabled=False)
    KiroCrewConfig.load()
    assert RELAY_TURNED_OFF_ADOPTION_KEY in _ledger()
    # Validation drops a marker that is not a boolean; it must not read as "never off".
    _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off="true")
    assert _hosts() is False
    assert _hosts() is False


def test_an_unreadable_config_json_keeps_an_overlay_host_that_looks_off_off() -> None:
    from kiro_crew.config.loader_locations import config_local_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    overlay = {"tunnel": {"relay_enabled": False, "relay_lease_credential": KEY}}
    config_local_path().write_text(json.dumps(overlay), encoding="utf-8")
    assert _hosts() is False
    # Nothing is written while the base cannot be read, and never config.local.json.
    assert path.read_text(encoding="utf-8") == "{ this is not json"
    assert json.loads(config_local_path().read_text(encoding="utf-8")) == overlay


def _overlay_off_then_malformed() -> bytes:
    from kiro_crew.config.loader_locations import config_local_path

    # The off choice lives in the overlay (turn_off writes the pair there too), and
    # both base switches are false; then the overlay stops parsing.
    _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off=False)
    config_local_path().write_text("{ this is not json", encoding="utf-8")
    return config_path().read_bytes()


def _nothing_written(before: bytes) -> None:
    from kiro_crew.config.loader_locations import config_local_path

    assert config_path().read_bytes() == before
    assert config_local_path().read_text(encoding="utf-8") == "{ this is not json"


def test_an_unreadable_overlay_keeps_a_credential_only_host_off() -> None:
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    # First boot: the check has not run yet.
    before = _overlay_off_then_malformed()
    assert _hosts() is False
    # The check waits for both files: nothing is written or recorded meanwhile.
    _nothing_written(before)
    assert RELAY_TURNED_OFF_ADOPTION_KEY not in _ledger()


@pytest.mark.parametrize("kind", ["malformed", "not-an-object", "unreadable"])
def test_an_unreadable_overlay_keeps_it_off_after_the_check_was_recorded(kind) -> None:
    from kiro_crew.config.loader_locations import config_local_path
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    if kind == "unreadable" and (sys.platform == "win32" or os.geteuid() == 0):
        pytest.skip("a mode-000 file is readable on Windows and as root")
    _write(relay_enabled=False)
    KiroCrewConfig.load()
    recorded = _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY]
    # GPT's trigger: both base switches false, the off choice only in the overlay.
    _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off=False)
    overlay = config_local_path()
    text = {"malformed": "{ this is not json", "not-an-object": "[]"}.get(kind, "{}")
    overlay.write_text(text, encoding="utf-8")
    if kind == "unreadable":
        overlay.chmod(0)
    before = config_path().read_bytes()
    try:
        assert _hosts() is False
    finally:
        overlay.chmod(0o600)
    assert config_path().read_bytes() == before
    assert overlay.read_text(encoding="utf-8") == text
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is recorded


def test_a_briefly_unreadable_overlay_does_not_keep_a_working_host_off() -> None:
    from kiro_crew.config.loader_locations import config_local_path

    # A team host on its credential alone, the check already recorded.
    _write(relay_enabled=False)
    KiroCrewConfig.load()
    _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off=False)
    assert _hosts() is True
    config_local_path().write_text("{ this is not json", encoding="utf-8")
    assert _hosts() is False
    # The carry holds on every load while the overlay stays unreadable.
    assert _hosts() is False
    # The overlay reads again: the stored document answers, so it hosts again.
    config_local_path().write_text("{}", encoding="utf-8")
    assert _hosts() is True


def test_an_overlay_with_no_config_json_keeps_a_host_that_looks_off_off() -> None:
    from kiro_crew.config.loader_locations import config_local_path
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    # No config.json at all; the overlay holds the off choice an older build honoured.
    overlay = {"tunnel": {"relay_enabled": False, "relay_lease_credential": KEY}}
    local = config_local_path()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(overlay), encoding="utf-8")
    assert not config_path().exists()
    assert _hosts() is False
    # The overlay-only document is cached; the second load is a cache hit, which only
    # the process-wide carry keeps off.
    assert _hosts() is False
    assert json.loads(local.read_text(encoding="utf-8")) == overlay
    assert RELAY_TURNED_OFF_ADOPTION_KEY not in _ledger()
    assert not config_path().exists()


def test_an_unreadable_overlay_leaves_a_stored_relay_enabled_true_as_today() -> None:
    from kiro_crew.config.loader_locations import config_local_path
    from kiro_crew.config.superseded_defaults import RELAY_TURNED_OFF_ADOPTION_KEY

    _write(relay_enabled=True, relay_lease_credential=KEY, relay_turned_off=False)
    config_local_path().write_text("{ this is not json", encoding="utf-8")
    before = config_path().read_bytes()
    assert _hosts() is True
    _nothing_written(before)
    assert RELAY_TURNED_OFF_ADOPTION_KEY not in _ledger()


def test_a_stale_no_op_record_never_overwrites_a_written_marker_record() -> None:
    from kiro_crew.config.superseded_defaults import (
        RELAY_TURNED_OFF_ADOPTION_KEY,
        record_adoptions,
    )

    record_adoptions({RELAY_TURNED_OFF_ADOPTION_KEY: True})
    # A concurrent load that read the marker decides "nothing to write" and records.
    record_adoptions({RELAY_TURNED_OFF_ADOPTION_KEY: False})
    assert _ledger()[RELAY_TURNED_OFF_ADOPTION_KEY] is True


def test_an_unreadable_ledger_keeps_a_host_that_looks_off_off_and_writes_nothing() -> None:
    from kiro_crew.config.superseded_defaults import ack_file_path

    _write(relay_enabled=False, relay_lease_credential=KEY)
    ledger = ack_file_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not json", encoding="utf-8")
    assert _hosts() is False
    # Unknown is never "already checked": the ledger bytes stay for a person to read.
    assert ledger.read_text(encoding="utf-8") == "{not json"
    assert "relay_turned_off" not in _stored()


def test_a_degraded_load_keeps_the_host_off_on_the_next_cached_load() -> None:
    # An unrelated malformed section keeps the write-back (and so the marker) off disk.
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tunnel": {"relay_enabled": False, "relay_lease_credential": KEY},
                "publish": {"allowed_destinations": [1]},
            }
        ),
        encoding="utf-8",
    )
    assert _hosts() is False
    assert _hosts() is False


def test_a_config_that_leaves_relay_enabled_out_also_stays_off() -> None:
    # A hand-written or older document with no relay_enabled key: off on that build.
    _write(relay_lease_credential=KEY)
    assert _hosts() is False
    assert _stored()["relay_turned_off"] is True


def test_a_malformed_off_marker_keeps_the_host_off() -> None:
    # A hand edit that stores the marker as text: validation drops it, so the load
    # must not read that as "not turned off".
    _write(relay_enabled=False, relay_turned_off="true", relay_lease_credential=KEY)
    assert _hosts() is False
    assert _hosts() is False


def test_a_cache_hit_before_the_write_back_keeps_a_malformed_marker_off() -> None:
    # A load that reads the files publishes the validated cache before its own
    # write-back runs, so a concurrent load (the config watcher) can be a cache hit
    # first. The hit must carry the keep-off decision itself.
    from kiro_crew.config.loader_pipeline import _RELAY_KEEP_OFF_PATHS, read_config_document

    _write(relay_enabled=False)
    KiroCrewConfig.load()
    path = _write(relay_enabled=False, relay_lease_credential=KEY, relay_turned_off="true")
    assert read_config_document().relay_keep_off is True
    assert str(path) not in _RELAY_KEEP_OFF_PATHS
    assert read_config_document().disk_read is False
    assert _hosts() is False
